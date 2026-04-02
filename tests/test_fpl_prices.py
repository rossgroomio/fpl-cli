"""Tests for FPL price scraper data models."""

from __future__ import annotations

from fpl_cli.scraper.fpl_prices import PlayerSellPrice, TeamFinances


class TestPlayerSellPrice:
    def test_element_id_default_none(self):
        p = PlayerSellPrice(name="Salah", sell_price=13.0)
        assert p.element_id is None

    def test_element_id_set(self):
        p = PlayerSellPrice(name="Salah", sell_price=13.0, element_id=253)
        assert p.element_id == 253


class TestTeamFinancesSerialisation:
    def test_to_dict_includes_element_id(self):
        squad = [PlayerSellPrice(name="Salah", sell_price=13.0, element_id=253)]
        tf = TeamFinances(bank=1.5, free_transfers=2, squad=squad)
        d = tf.to_dict()
        assert d["squad"][0]["element_id"] == 253

    def test_from_dict_reconstructs_element_id(self):
        data = {
            "bank": 1.5,
            "free_transfers": 2,
            "squad": [{"name": "Salah", "sell_price": 13.0, "element_id": 253}],
        }
        tf = TeamFinances.from_dict(data)
        assert tf.squad[0].element_id == 253

    def test_from_dict_old_cache_without_element_id(self):
        """Old cached JSON without element_id deserialises with element_id=None."""
        data = {
            "bank": 1.5,
            "free_transfers": 2,
            "squad": [{"name": "Salah", "sell_price": 13.0}],
        }
        tf = TeamFinances.from_dict(data)
        assert tf.squad[0].element_id is None

    def test_roundtrip(self):
        squad = [
            PlayerSellPrice(name="Salah", sell_price=13.0, position="MID", element_id=253),
            PlayerSellPrice(name="Haaland", sell_price=14.5, position="FWD", element_id=355),
        ]
        original = TeamFinances(bank=0.5, free_transfers=1, squad=squad, total_value=28.0)
        restored = TeamFinances.from_dict(original.to_dict())
        assert len(restored.squad) == 2
        assert restored.squad[0].element_id == 253
        assert restored.squad[1].element_id == 355
        assert restored.bank == 0.5


class TestExtractFromIntercepted:
    def test_element_id_populated_from_picks(self):
        """_extract_from_intercepted stores element_id from pick['element']."""
        import asyncio
        from unittest.mock import AsyncMock, MagicMock

        scraper_mod = __import__("fpl_cli.scraper.fpl_prices", fromlist=["FPLPriceScraper"])
        scraper = scraper_mod.FPLPriceScraper()

        page = MagicMock()
        page.evaluate = AsyncMock(return_value={
            "elements": [
                {"id": 253, "web_name": "Salah", "element_type": 3, "now_cost": 130},
            ],
            "element_types": [
                {"id": 3, "singular_name_short": "MID"},
            ],
        })

        my_entry = {
            "picks": [
                {"element": 253, "selling_price": 130, "purchase_price": 120},
            ],
            "transfers": {"bank": 15, "limit": 1},
        }

        result = asyncio.run(scraper._extract_from_intercepted(page, my_entry))
        assert result is not None
        assert result.squad[0].element_id == 253
        assert result.squad[0].name == "Salah"

    def test_dom_fallback_has_no_element_id(self):
        """DOM fallback produces PlayerSellPrice with element_id=None."""
        p = PlayerSellPrice(name="Salah", sell_price=13.0)
        assert p.element_id is None
