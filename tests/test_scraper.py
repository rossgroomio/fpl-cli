"""Tests for the FPL price scraper module."""

import os
from datetime import datetime, timedelta
from unittest.mock import patch

import pytest

from fpl_cli.scraper.fpl_prices import (
    FPLPriceScraper,
    PlayerSellPrice,
    TeamFinances,
    cache_age_hours,
    cache_file,
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
            with ctx() as mock_p:
                await scraper.scrape()
        `mock_p` is the mocked `playwright.async_api` root object - inspect
        `mock_p.chromium.launch.call_args.kwargs` to assert on launch() args.
        """
        from contextlib import contextmanager
        from unittest.mock import AsyncMock

        mock_login = AsyncMock()
        mock_extract = AsyncMock()
        mock_extract.return_value = TeamFinances(bank=0.0, free_transfers=0, squad=[], total_value=0.0)
        mock_fetch_my_team = AsyncMock(return_value=None)

        @contextmanager
        def ctx():
            with patch.object(scraper, "_login", mock_login), \
                 patch.object(scraper, "_extract_finances", mock_extract), \
                 patch.object(scraper, "_fetch_my_team", mock_fetch_my_team), \
                 patch.object(scraper, "_accept_cookies", AsyncMock()), \
                 patch("playwright.async_api.async_playwright") as mock_pw:
                mock_p = AsyncMock()
                mock_page = AsyncMock()
                # scrape() reads page.url after login to detect IdP-stuck failures.
                mock_page.url = "https://fantasy.premierleague.com/"
                mock_pw.return_value.__aenter__.return_value = mock_p
                mock_p.chromium.launch.return_value = AsyncMock()
                mock_p.chromium.launch.return_value.new_context.return_value = AsyncMock()
                mock_p.chromium.launch.return_value.new_context.return_value.new_page.return_value = mock_page
                yield mock_p

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

    async def test_scrape_honours_browser_executable_env_var(self):
        """scrape() forwards FPL_BROWSER_EXECUTABLE/ARGS/IGNORE_CERTS to chromium.launch()."""
        pytest.importorskip("playwright")

        env = {
            "FPL_EMAIL": "test@example.com",
            "FPL_PASSWORD": "secret",
            "FPL_BROWSER_EXECUTABLE": "/opt/pw-browsers/chromium-1194/chrome-linux/chrome",
            "FPL_BROWSER_ARGS": "--disable-features=EncryptedClientHello --foo=bar",
            "FPL_BROWSER_IGNORE_CERTS": "1",
        }
        with patch.dict(os.environ, env, clear=True):
            scraper = FPLPriceScraper()
            _, ctx = self._mock_playwright_stack(scraper)
            with ctx() as mock_p:
                await scraper.scrape()

        kwargs = mock_p.chromium.launch.call_args.kwargs
        assert kwargs["executable_path"] == "/opt/pw-browsers/chromium-1194/chrome-linux/chrome"
        assert "channel" not in kwargs
        assert "--disable-features=EncryptedClientHello" in kwargs["args"]
        assert "--foo=bar" in kwargs["args"]
        assert "--ignore-certificate-errors" in kwargs["args"]

    async def test_scrape_honours_browser_channel_env_var(self):
        """scrape() forwards FPL_BROWSER_CHANNEL to chromium.launch() when set alone."""
        pytest.importorskip("playwright")

        env = {
            "FPL_EMAIL": "test@example.com",
            "FPL_PASSWORD": "secret",
            "FPL_BROWSER_CHANNEL": "chromium",
        }
        with patch.dict(os.environ, env, clear=True):
            scraper = FPLPriceScraper()
            _, ctx = self._mock_playwright_stack(scraper)
            with ctx() as mock_p:
                await scraper.scrape()

        kwargs = mock_p.chromium.launch.call_args.kwargs
        assert kwargs["channel"] == "chromium"
        assert "executable_path" not in kwargs

    async def test_scrape_omits_browser_override_when_env_unset(self):
        """Without the override env vars, launch() gets no executable_path/channel."""
        pytest.importorskip("playwright")

        clean = {"FPL_EMAIL": "test@example.com", "FPL_PASSWORD": "secret"}
        with patch.dict(os.environ, clean, clear=True):
            scraper = FPLPriceScraper()
            _, ctx = self._mock_playwright_stack(scraper)
            with ctx() as mock_p:
                await scraper.scrape()

        kwargs = mock_p.chromium.launch.call_args.kwargs
        assert "executable_path" not in kwargs
        assert "channel" not in kwargs
        assert kwargs["args"] == []

    async def test_scrape_rejects_conflicting_executable_and_channel_env_vars(self):
        """FPL_BROWSER_EXECUTABLE and FPL_BROWSER_CHANNEL together raise instead of
        silently dropping the channel (Playwright ignores channel once executable_path
        is set)."""
        pytest.importorskip("playwright")

        env = {
            "FPL_EMAIL": "test@example.com",
            "FPL_PASSWORD": "secret",
            "FPL_BROWSER_EXECUTABLE": "/opt/pw-browsers/chromium-1194/chrome-linux/chrome",
            "FPL_BROWSER_CHANNEL": "chromium",
        }
        with patch.dict(os.environ, env, clear=True):
            scraper = FPLPriceScraper()
            with pytest.raises(ValueError, match="mutually exclusive"):
                await scraper.scrape()

    async def test_scrape_raises_clear_error_for_malformed_browser_args(self):
        """A quoting mistake in FPL_BROWSER_ARGS names the env var, not a bare
        shlex error."""
        pytest.importorskip("playwright")

        env = {
            "FPL_EMAIL": "test@example.com",
            "FPL_PASSWORD": "secret",
            "FPL_BROWSER_ARGS": "--foo='bar",
        }
        with patch.dict(os.environ, env, clear=True):
            scraper = FPLPriceScraper()
            with pytest.raises(ValueError, match="FPL_BROWSER_ARGS is not valid shell syntax"):
                await scraper.scrape()

    async def test_scrape_preserves_original_error_when_close_also_fails(self):
        """A failure while closing the browser must not mask the scrape's real error."""
        pytest.importorskip("playwright")
        from unittest.mock import AsyncMock

        env = {"FPL_EMAIL": "test@example.com", "FPL_PASSWORD": "secret"}
        with patch.dict(os.environ, env, clear=True):
            scraper = FPLPriceScraper()
            with patch.object(
                scraper, "_login", AsyncMock(side_effect=RuntimeError("net::ERR_CONNECTION_RESET"))
            ), patch("playwright.async_api.async_playwright") as mock_pw:
                mock_p = AsyncMock()
                mock_browser = AsyncMock()
                mock_browser.close = AsyncMock(side_effect=OSError("close also failed"))
                mock_pw.return_value.__aenter__.return_value = mock_p
                mock_p.chromium.launch.return_value = mock_browser

                with pytest.raises(RuntimeError, match="ERR_CONNECTION_RESET"):
                    await scraper.scrape()

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

    async def test_scrape_raises_when_login_leaves_browser_on_idp(self):
        """If login silently fails, browser stays on account.premierleague.com - raise clearly."""
        pytest.importorskip("playwright")
        from contextlib import contextmanager
        from unittest.mock import AsyncMock

        with patch.dict(os.environ, {"FPL_EMAIL": "x@y.z", "FPL_PASSWORD": "p"}):
            scraper = FPLPriceScraper()

            @contextmanager
            def ctx():
                with patch.object(scraper, "_login", AsyncMock()), \
                     patch("playwright.async_api.async_playwright") as mock_pw:
                    mock_p = AsyncMock()
                    mock_page = AsyncMock()
                    mock_page.url = "https://account.premierleague.com/as/authorize?..."
                    mock_pw.return_value.__aenter__.return_value = mock_p
                    mock_p.chromium.launch.return_value = AsyncMock()
                    mock_p.chromium.launch.return_value.new_context.return_value = AsyncMock()
                    mock_p.chromium.launch.return_value.new_context.return_value.new_page.return_value = mock_page
                    yield

            with ctx(), pytest.raises(ValueError, match="Login did not complete"):
                await scraper.scrape()

    async def test_fetch_my_team_returns_payload(self):
        """_fetch_my_team chains /api/me/ -> /api/my-team/{id}/ via page.evaluate."""
        from unittest.mock import AsyncMock, MagicMock

        scraper = FPLPriceScraper()
        page = MagicMock()
        page.evaluate = AsyncMock(side_effect=[
            {"player": {"entry": 5955459}},
            {"picks": [{"element": 253, "selling_price": 130, "purchase_price": 130}],
             "transfers": {"bank": 29, "limit": 1}},
        ])

        result = await scraper._fetch_my_team(page)
        assert result is not None
        assert result["transfers"]["bank"] == 29
        # Second evaluate gets the entry id as a positional arg
        assert page.evaluate.call_args_list[1].args[1] == 5955459

    async def test_fetch_my_team_returns_none_on_missing_entry(self):
        """If /api/me/ never returns a player.entry, abort and let DOM fallback take over."""
        from unittest.mock import AsyncMock, MagicMock

        scraper = FPLPriceScraper()
        page = MagicMock()
        page.evaluate = AsyncMock(return_value={})
        page.wait_for_timeout = AsyncMock()

        result = await scraper._fetch_my_team(page)
        assert result is None
        assert page.evaluate.call_count == scraper._ME_RETRY_ATTEMPTS
        # No trailing wait after the final failed attempt
        assert page.wait_for_timeout.call_count == scraper._ME_RETRY_ATTEMPTS - 1

    async def test_fetch_my_team_handles_null_player(self):
        """FPL briefly returns {'player': null} post-login; we retry then give up cleanly."""
        from unittest.mock import AsyncMock, MagicMock

        scraper = FPLPriceScraper()
        page = MagicMock()
        page.evaluate = AsyncMock(return_value={"player": None})
        page.wait_for_timeout = AsyncMock()

        result = await scraper._fetch_my_team(page)
        assert result is None
        assert page.evaluate.call_count == scraper._ME_RETRY_ATTEMPTS
        # No trailing wait after the final failed attempt
        assert page.wait_for_timeout.call_count == scraper._ME_RETRY_ATTEMPTS - 1

    async def test_fetch_my_team_retries_until_hydrated(self):
        """Null player on early polls, then a real entry id: retry succeeds and fetches my-team."""
        from unittest.mock import AsyncMock, MagicMock

        scraper = FPLPriceScraper()
        page = MagicMock()
        page.evaluate = AsyncMock(side_effect=[
            {"player": None},
            {"player": None},
            {"player": {"entry": 12345}},
            {"picks": [{"element": 1, "selling_price": 50, "purchase_price": 50}],
             "transfers": {"bank": 10, "limit": 1}},
        ])
        page.wait_for_timeout = AsyncMock()

        result = await scraper._fetch_my_team(page)
        assert result is not None
        assert result["transfers"]["bank"] == 10
        # 3 /api/me/ calls + 1 /api/my-team/ call
        assert page.evaluate.call_count == 4
        # And we waited between the failing /api/me/ attempts (twice)
        assert page.wait_for_timeout.call_count == 2

    async def test_fetch_my_team_evaluate_error_no_retry(self):
        """A hard page.evaluate exception is a different failure mode - don't retry."""
        from unittest.mock import AsyncMock, MagicMock

        scraper = FPLPriceScraper()
        page = MagicMock()
        page.evaluate = AsyncMock(side_effect=RuntimeError("eval failed"))
        page.wait_for_timeout = AsyncMock()

        result = await scraper._fetch_my_team(page)
        assert result is None
        assert page.evaluate.call_count == 1
        assert page.wait_for_timeout.call_count == 0

    async def test_extract_finances_records_api_failure_in_errors(self):
        """When the API path is skipped, the DOM fallback's result gets a hint appended."""
        from unittest.mock import AsyncMock, MagicMock

        scraper = FPLPriceScraper()
        dom_result = TeamFinances(bank=0.0, free_transfers=0, squad=[], total_value=0.0)
        with patch.object(scraper, "_extract_via_dom", AsyncMock(return_value=dom_result)):
            result = await scraper._extract_finances(MagicMock(), my_entry_response=None)

        assert result is dom_result
        assert any("api/me" in err.lower() for err in result.extraction_errors)

    async def test_extract_finances_records_intercepted_failure_in_errors(self):
        """If /api/me/ succeeded but the intercepted payload is unusable, the DOM fallback still gets a hint."""
        from unittest.mock import AsyncMock, MagicMock

        scraper = FPLPriceScraper()
        dom_result = TeamFinances(bank=0.0, free_transfers=0, squad=[], total_value=0.0)
        with (
            patch.object(scraper, "_extract_from_intercepted", AsyncMock(return_value=None)),
            patch.object(scraper, "_extract_via_dom", AsyncMock(return_value=dom_result)),
        ):
            result = await scraper._extract_finances(
                MagicMock(), my_entry_response={"picks": [], "transfers": {}}
            )

        assert result is dom_result
        assert any("my-team" in err.lower() for err in result.extraction_errors)

    def test_cache_file_path(self, tmp_path, monkeypatch):
        """The cache file resolves under the data dir at call time, not at import."""
        from fpl_cli.paths import user_data_dir

        assert cache_file().name == "team_finances.json"

        # Resolution is lazy: an override set after import is still honoured.
        moved = tmp_path / "relocated-data"
        monkeypatch.setenv("FPL_CLI_DATA_DIR", str(moved))
        user_data_dir.cache_clear()
        assert cache_file() == moved / "team_finances.json"


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
        cache_path = tmp_path / "team_finances.json"
        with patch("fpl_cli.scraper.fpl_prices.cache_file", return_value=cache_path):
            save_cache(finances)
            assert cache_path.exists()

            loaded = load_cache()
        assert loaded is not None
        assert loaded.bank == 1.5
        assert loaded.free_transfers == 1
        assert len(loaded.squad) == 2

    def test_load_cache_not_exists(self, tmp_path):
        """Test loading cache when file doesn't exist."""
        cache_path = tmp_path / "team_finances.json"
        with patch("fpl_cli.scraper.fpl_prices.cache_file", return_value=cache_path):
            assert not cache_path.exists()
            loaded = load_cache()
        assert loaded is None

    def test_load_cache_invalid_json(self, tmp_path):
        """Test loading cache with invalid JSON."""
        cache_path = tmp_path / "team_finances.json"
        cache_path.write_text("invalid json {{{", encoding="utf-8")
        with patch("fpl_cli.scraper.fpl_prices.cache_file", return_value=cache_path):
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
        cache_path = tmp_path / "team_finances.json"
        with patch("fpl_cli.scraper.fpl_prices.cache_file", return_value=cache_path):
            save_cache(finances)
            age = cache_age_hours()
        assert age is not None
        assert 4.9 < age < 5.1  # Allow small margin for test execution time

    def test_cache_age_hours_no_cache(self, tmp_path):
        """Test cache age when no cache exists."""
        cache_path = tmp_path / "team_finances.json"
        with patch("fpl_cli.scraper.fpl_prices.cache_file", return_value=cache_path):
            age = cache_age_hours()
        assert age is None

    def test_cache_creates_directory(self, tmp_path):
        """Test that save_cache creates data directory if needed."""
        cache_path = tmp_path / "subdir" / "team_finances.json"
        assert not cache_path.parent.exists()
        finances = TeamFinances(
            bank=1.0, free_transfers=1, squad=[], total_value=100.0, scraped_at=datetime.now().isoformat()
        )
        with patch("fpl_cli.scraper.fpl_prices.cache_file", return_value=cache_path):
            save_cache(finances)
        assert cache_path.parent.exists()
        assert cache_path.exists()
