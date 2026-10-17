"""Tests for FPL price scraper data models."""

from __future__ import annotations

from fpl_cli.scraper.fpl_prices import PlayerSellPrice, TeamFinances
from tests.conftest import make_player


class TestPlayerSellPrice:
    def test_element_id_default_none(self):
        p = PlayerSellPrice(name="Salah", sell_price=13.0)
        assert p.element_id is None

    def test_element_id_set(self):
        p = PlayerSellPrice(name="Salah", sell_price=13.0, element_id=253)
        assert p.element_id == 253


class TestIsPlayer:
    """A cache row is matched to a player on id, then on `code`, or on name without one."""

    def test_same_id_and_code(self):
        row = PlayerSellPrice(name="Salah", sell_price=13.0, element_id=253, code=118748)
        assert row.is_player(make_player(id=253, code=118748, web_name="Salah"))

    def test_an_earlier_seasons_row_under_the_same_id_is_another_footballer(self):
        row = PlayerSellPrice(name="Salah", sell_price=13.0, element_id=253, code=118748)
        assert not row.is_player(make_player(id=253, code=99999, web_name="Salah"))

    def test_a_row_without_code_needs_the_name_to_agree_too(self):
        """A cache from before codes were stored is also the likeliest to be last season's."""
        row = PlayerSellPrice(name="Salah", sell_price=13.0, element_id=253)
        assert row.is_player(make_player(id=253, code=118748, web_name="Salah"))
        assert not row.is_player(make_player(id=253, code=99999, web_name="Gordon"))

    def test_a_row_without_an_id_is_nobody(self):
        """A DOM-fallback row: a name alone is never enough."""
        row = PlayerSellPrice(name="Salah", sell_price=13.0)
        assert not row.is_player(make_player(id=253, code=118748, web_name="Salah"))


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

    def test_price_at_scrape_round_trips_and_an_older_cache_has_none(self):
        squad = [PlayerSellPrice(name="Salah", sell_price=13.0, element_id=253, price_at_scrape=13.5)]
        restored = TeamFinances.from_dict(TeamFinances(bank=0.0, free_transfers=1, squad=squad).to_dict())
        assert restored.squad[0].price_at_scrape == 13.5

        older = TeamFinances.from_dict({"squad": [{"name": "Salah", "sell_price": 13.0, "element_id": 253}]})
        assert older.squad[0].price_at_scrape is None

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
        # The price the sell price was worked out at, for the next load to check the rule against.
        assert result.squad[0].price_at_scrape == 13.0

    def test_dom_fallback_has_no_element_id(self):
        """DOM fallback produces PlayerSellPrice with element_id=None."""
        p = PlayerSellPrice(name="Salah", sell_price=13.0)
        assert p.element_id is None

    def test_extract_handles_null_transfers_field(self):
        """FPL briefly returns {'transfers': null} post-login; must not crash."""
        import asyncio
        from unittest.mock import AsyncMock, MagicMock

        scraper_mod = __import__("fpl_cli.scraper.fpl_prices", fromlist=["FPLPriceScraper"])
        scraper = scraper_mod.FPLPriceScraper()

        page = MagicMock()
        page.evaluate = AsyncMock(return_value={"elements": [], "element_types": []})

        my_entry = {"picks": None, "transfers": None}
        result = asyncio.run(scraper._extract_from_intercepted(page, my_entry))
        # No picks -> _extract_from_intercepted returns None, caller falls back to DOM
        assert result is None
