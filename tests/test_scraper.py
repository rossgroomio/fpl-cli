"""Tests for the FPL price scraper module."""

import json
import os
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest

from fpl_cli.scraper.fpl_prices import (
    CACHE_FILE,
    FPLPriceScraper,
    PlayerSellPrice,
    TeamFinances,
    cache_age_hours,
    load_cache,
    save_cache,
)


class TestPlayerSellPrice:
    """Tests for PlayerSellPrice dataclass."""

    def test_create_player_sell_price(self):
        """Test creating a PlayerSellPrice instance."""
        player = PlayerSellPrice(name="Haaland", sell_price=14.8)
        assert player.name == "Haaland"
        assert player.sell_price == 14.8
        assert player.position == ""

    def test_player_sell_price_with_position(self):
        """Test PlayerSellPrice with position."""
        player = PlayerSellPrice(name="Salah", sell_price=13.2, position="MID")
        assert player.name == "Salah"
        assert player.sell_price == 13.2
        assert player.position == "MID"


class TestTeamFinances:
    """Tests for TeamFinances dataclass."""

    def test_create_team_finances(self):
        """Test creating a TeamFinances instance."""
        finances = TeamFinances(
            bank=1.5,
            free_transfers=2,
            squad=[
                PlayerSellPrice(name="Haaland", sell_price=14.8),
                PlayerSellPrice(name="Salah", sell_price=13.2),
            ],
            total_value=105.0,
            scraped_at="2026-01-18T10:00:00",
        )
        assert finances.bank == 1.5
        assert finances.free_transfers == 2
        assert len(finances.squad) == 2
        assert finances.total_value == 105.0

    def test_team_finances_to_dict(self):
        """Test serialization to dict."""
        finances = TeamFinances(
            bank=1.0,
            free_transfers=1,
            squad=[PlayerSellPrice(name="Raya", sell_price=5.8)],
            total_value=100.0,
            scraped_at="2026-01-18T10:00:00",
        )
        data = finances.to_dict()

        assert data["bank"] == 1.0
        assert data["free_transfers"] == 1
        assert len(data["squad"]) == 1
        assert data["squad"][0]["name"] == "Raya"
        assert data["squad"][0]["sell_price"] == 5.8
        assert data["total_value"] == 100.0
        assert data["scraped_at"] == "2026-01-18T10:00:00"

    def test_team_finances_from_dict(self):
        """Test deserialization from dict."""
        data = {
            "bank": 2.5,
            "free_transfers": 1,
            "squad": [
                {"name": "Haaland", "sell_price": 14.8, "position": "FWD"},
                {"name": "Salah", "sell_price": 13.2},
            ],
            "total_value": 115.0,
            "scraped_at": "2026-01-18T12:00:00",
        }
        finances = TeamFinances.from_dict(data)

        assert finances.bank == 2.5
        assert finances.free_transfers == 1
        assert len(finances.squad) == 2
        assert finances.squad[0].name == "Haaland"
        assert finances.squad[0].sell_price == 14.8
        assert finances.squad[0].position == "FWD"
        assert finances.squad[1].name == "Salah"
        assert finances.squad[1].position == ""
        assert finances.total_value == 115.0

    def test_team_finances_roundtrip(self):
        """Test serialization/deserialization roundtrip."""
        original = TeamFinances(
            bank=1.0,
            free_transfers=2,
            squad=[
                PlayerSellPrice(name="Player1", sell_price=10.0, position="GK"),
                PlayerSellPrice(name="Player2", sell_price=8.5, position="DEF"),
            ],
            total_value=100.0,
            scraped_at="2026-01-18T10:00:00",
        )

        data = original.to_dict()
        restored = TeamFinances.from_dict(data)

        assert restored.bank == original.bank
        assert restored.free_transfers == original.free_transfers
        assert len(restored.squad) == len(original.squad)
        assert restored.squad[0].name == original.squad[0].name
        assert restored.total_value == original.total_value


class TestFPLPriceScraper:
    """Tests for FPLPriceScraper class."""

    @staticmethod
    def _mock_playwright_stack(scraper):
        """Set up mocked playwright browser stack for scraper tests.

        Returns (mock_login, context manager) - use as:
            mock_login, ctx = self._mock_playwright_stack(scraper)
            with ctx:
                await scraper.scrape()
        """
        from contextlib import contextmanager
        from unittest.mock import AsyncMock

        mock_login = AsyncMock()
        mock_extract = AsyncMock()
        mock_extract.return_value = TeamFinances(bank=0.0, free_transfers=0, squad=[], total_value=0.0)

        @contextmanager
        def ctx():
            with patch.object(scraper, "_login", mock_login), \
                 patch.object(scraper, "_extract_finances", mock_extract), \
                 patch.object(scraper, "_accept_cookies", AsyncMock()), \
                 patch("playwright.async_api.async_playwright") as mock_pw:
                mock_p = AsyncMock()
                mock_page = AsyncMock()
                mock_pw.return_value.__aenter__.return_value = mock_p
                mock_p.chromium.launch.return_value = AsyncMock()
                mock_p.chromium.launch.return_value.new_context.return_value = AsyncMock()
                mock_p.chromium.launch.return_value.new_context.return_value.new_page.return_value = mock_page
                mock_page.on = lambda *_a, **_k: None
                yield

        return mock_login, ctx

    def test_no_credential_attributes(self):
        """Scraper instance stores no credentials as attributes."""
        scraper = FPLPriceScraper()
        assert not hasattr(scraper, "email")
        assert not hasattr(scraper, "password")

    async def test_scrape_resolves_env_var_credentials(self):
        """scrape() reads credentials from env vars and passes them to _login."""
        pytest.importorskip("playwright")
        with patch.dict(os.environ, {"FPL_EMAIL": "test@example.com", "FPL_PASSWORD": "secret"}):
            scraper = FPLPriceScraper()
            mock_login, ctx = self._mock_playwright_stack(scraper)
            with ctx():
                await scraper.scrape()

            mock_login.assert_called_once()
            _, call_email, call_password = mock_login.call_args[0]
            assert call_email == "test@example.com"
            assert call_password == "secret"

    async def test_scrape_missing_credentials(self):
        """scrape() raises ValueError when no credentials available."""
        pytest.importorskip("playwright")
        with patch.dict(os.environ, {}, clear=True):
            os.environ.pop("FPL_EMAIL", None)
            os.environ.pop("FPL_PASSWORD", None)
            with patch("fpl_cli.scraper.fpl_prices.keyring.get_password", return_value=None):
                scraper = FPLPriceScraper()
                with pytest.raises(ValueError, match="FPL credentials required"):
                    await scraper.scrape()

    async def test_scrape_keyring_fallback(self):
        """scrape() falls back to keyring when env vars absent."""
        pytest.importorskip("playwright")
        with patch.dict(os.environ, {}, clear=True):
            os.environ.pop("FPL_EMAIL", None)
            os.environ.pop("FPL_PASSWORD", None)

            def _get_password(_service, key):
                return {"email": "keyring@example.com", "password": "keyring_pass"}[key]

            with patch("fpl_cli.scraper.fpl_prices.keyring.get_password", side_effect=_get_password):
                scraper = FPLPriceScraper()
                mock_login, ctx = self._mock_playwright_stack(scraper)
                with ctx():
                    await scraper.scrape()

                mock_login.assert_called_once()
                _, call_email, call_password = mock_login.call_args[0]
                assert call_email == "keyring@example.com"
                assert call_password == "keyring_pass"

    def test_cache_file_path(self):
        """Test cache file path is correct."""
        assert CACHE_FILE.name == "team_finances.json"
        assert "fpl-cli" in str(CACHE_FILE)


class TestTeamFinancesValidation:
    """Tests for TeamFinances data quality validation."""

    def _make_full_squad(self, count=15):
        return [PlayerSellPrice(name=f"Player{i}", sell_price=5.0 + i * 0.5) for i in range(count)]

    def test_is_suspect_empty_squad_zero_bank(self):
        """Complete failure: no squad and bank is zero."""
        finances = TeamFinances(bank=0.0, free_transfers=0, squad=[], total_value=0.0)
        assert finances.is_suspect is True

    def test_is_suspect_partial_squad(self):
        """Partial failure: fewer than 11 players extracted."""
        finances = TeamFinances(
            bank=1.0, free_transfers=1, squad=self._make_full_squad(8), total_value=50.0
        )
        assert finances.is_suspect is True

    def test_is_suspect_zero_total_value(self):
        """Total value is zero despite having a squad."""
        finances = TeamFinances(
            bank=0.0, free_transfers=1, squad=self._make_full_squad(15), total_value=0.0
        )
        assert finances.is_suspect is True

    def test_not_suspect_full_squad(self):
        """Normal data: full squad with reasonable values."""
        squad = self._make_full_squad(15)
        total = sum(p.sell_price for p in squad) + 1.5
        finances = TeamFinances(bank=1.5, free_transfers=2, squad=squad, total_value=total)
        assert finances.is_suspect is False

    def test_not_suspect_zero_bank_full_squad(self):
        """Zero bank with full squad is legitimate (spent all money)."""
        squad = self._make_full_squad(15)
        total = sum(p.sell_price for p in squad)
        finances = TeamFinances(bank=0.0, free_transfers=1, squad=squad, total_value=total)
        assert finances.is_suspect is False

    def test_not_suspect_eleven_players(self):
        """Exactly 11 players is not suspect (bench extraction may fail)."""
        squad = self._make_full_squad(11)
        total = sum(p.sell_price for p in squad) + 2.0
        finances = TeamFinances(bank=2.0, free_transfers=1, squad=squad, total_value=total)
        assert finances.is_suspect is False

    def test_warnings_complete_failure(self):
        """Warnings for complete scrape failure."""
        finances = TeamFinances(bank=0.0, free_transfers=0, squad=[], total_value=0.0)
        warnings = finances.warnings
        assert any("scrape likely failed" in w for w in warnings)
        assert any("£0.0m" in w for w in warnings)

    def test_warnings_partial_squad(self):
        """Warnings for partial squad extraction."""
        finances = TeamFinances(
            bank=1.0, free_transfers=1, squad=self._make_full_squad(5), total_value=30.0
        )
        warnings = finances.warnings
        assert any("5 players" in w for w in warnings)

    def test_warnings_with_extraction_errors(self):
        """Extraction errors appear in warnings."""
        finances = TeamFinances(
            bank=0.0, free_transfers=0, squad=[], total_value=0.0,
            extraction_errors=["Budget extraction failed: timeout"],
        )
        warnings = finances.warnings
        assert any("Budget extraction failed" in w for w in warnings)

    def test_warnings_empty_for_good_data(self):
        """No warnings for normal data."""
        squad = self._make_full_squad(15)
        total = sum(p.sell_price for p in squad) + 1.0
        finances = TeamFinances(bank=1.0, free_transfers=2, squad=squad, total_value=total)
        assert finances.warnings == []

    def test_extraction_errors_roundtrip(self):
        """extraction_errors survives to_dict/from_dict."""
        original = TeamFinances(
            bank=1.0, free_transfers=1, squad=[], total_value=1.0,
            scraped_at="2026-02-09T10:00:00",
            extraction_errors=["Squad extraction failed: TimeoutError"],
        )
        data = original.to_dict()
        assert data["extraction_errors"] == ["Squad extraction failed: TimeoutError"]

        restored = TeamFinances.from_dict(data)
        assert restored.extraction_errors == ["Squad extraction failed: TimeoutError"]

    def test_from_dict_backwards_compatible(self):
        """Old cache format without extraction_errors loads cleanly."""
        old_data = {
            "bank": 2.0,
            "free_transfers": 1,
            "squad": [{"name": "Haaland", "sell_price": 14.8}],
            "total_value": 102.0,
            "scraped_at": "2026-01-15T10:00:00",
        }
        finances = TeamFinances.from_dict(old_data)
        assert finances.extraction_errors == []
        assert finances.bank == 2.0


class TestFPLPriceScraperCache:
    """Tests for cache functionality."""

    def test_save_and_load_cache(self, tmp_path):
        """Test saving and loading cache."""
        finances = TeamFinances(
            bank=1.5,
            free_transfers=1,
            squad=[
                PlayerSellPrice(name="Haaland", sell_price=14.8),
                PlayerSellPrice(name="Salah", sell_price=13.2),
            ],
            total_value=115.5,
            scraped_at=datetime.now().isoformat(),
        )
        cache_file = tmp_path / "team_finances.json"
        with patch("fpl_cli.scraper.fpl_prices.CACHE_FILE", cache_file):
            save_cache(finances)
            assert cache_file.exists()

            loaded = load_cache()
        assert loaded is not None
        assert loaded.bank == 1.5
        assert loaded.free_transfers == 1
        assert len(loaded.squad) == 2

    def test_load_cache_not_exists(self, tmp_path):
        """Test loading cache when file doesn't exist."""
        cache_file = tmp_path / "team_finances.json"
        with patch("fpl_cli.scraper.fpl_prices.CACHE_FILE", cache_file):
            assert not cache_file.exists()
            loaded = load_cache()
        assert loaded is None

    def test_load_cache_invalid_json(self, tmp_path):
        """Test loading cache with invalid JSON."""
        cache_file = tmp_path / "team_finances.json"
        cache_file.write_text("invalid json {{{", encoding="utf-8")
        with patch("fpl_cli.scraper.fpl_prices.CACHE_FILE", cache_file):
            loaded = load_cache()
        assert loaded is None

    def test_cache_age_hours(self, tmp_path):
        """Test calculating cache age."""
        finances = TeamFinances(
            bank=1.0,
            free_transfers=1,
            squad=[],
            total_value=100.0,
            scraped_at=(datetime.now() - timedelta(hours=5)).isoformat(),
        )
        cache_file = tmp_path / "team_finances.json"
        with patch("fpl_cli.scraper.fpl_prices.CACHE_FILE", cache_file):
            save_cache(finances)
            age = cache_age_hours()
        assert age is not None
        assert 4.9 < age < 5.1  # Allow small margin for test execution time

    def test_cache_age_hours_no_cache(self, tmp_path):
        """Test cache age when no cache exists."""
        cache_file = tmp_path / "team_finances.json"
        with patch("fpl_cli.scraper.fpl_prices.CACHE_FILE", cache_file):
            age = cache_age_hours()
        assert age is None

    def test_cache_creates_directory(self, tmp_path):
        """Test that save_cache creates data directory if needed."""
        cache_file = tmp_path / "subdir" / "team_finances.json"
        assert not cache_file.parent.exists()
        finances = TeamFinances(
            bank=1.0, free_transfers=1, squad=[], total_value=100.0, scraped_at=datetime.now().isoformat()
        )
        with patch("fpl_cli.scraper.fpl_prices.CACHE_FILE", cache_file):
            save_cache(finances)
        assert cache_file.parent.exists()
        assert cache_file.exists()
