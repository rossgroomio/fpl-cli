"""Tests for FPL price scraper data models."""

from __future__ import annotations

import pytest

from fpl_cli.scraper.fpl_prices import PlayerSellPrice, TeamFinances, reprice_squad, selling_price
from tests.conftest import make_player


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


class TestSellingPrice:
    """FPL keeps half of any rise, rounded down, and passes on all of a fall (£0.1m units)."""

    @pytest.mark.parametrize(("purchase", "current", "expected"), [
        (50, 50, 50),
        (50, 51, 50),  # a rise of 0.1 leaves the sell price where it was bought
        (50, 52, 51),  # a rise of 0.2 adds 0.1
        (50, 53, 51),
        (50, 48, 48),  # a fall is taken in full
    ])
    def test_rule(self, purchase, current, expected):
        assert selling_price(purchase, current) == expected


def _row(*, sell: float, purchase: float, at_scrape: float | None, element_id: int = 7, code: int | None = 7007):
    return PlayerSellPrice(
        name="Mbeumo", sell_price=sell, position="MID", purchase_price=purchase,
        element_id=element_id, code=code, price_at_scrape=at_scrape,
    )


def _reprice(row: PlayerSellPrice, *, today: int, element_id: int = 7, code: int = 7007):
    finances = TeamFinances(bank=0.5, free_transfers=1, squad=[row])
    return reprice_squad(finances, [make_player(id=element_id, code=code, now_cost=today)])


class TestRepriceSquad:
    def test_a_rise_since_the_scrape_is_halved_into_the_sell_price(self):
        result = _reprice(_row(sell=5.0, purchase=5.0, at_scrape=5.0), today=52)

        [row] = result.finances.squad
        assert (row.sell_price, row.scraped_sell_price) == (5.1, 5.0)
        assert result.updated == [row]
        assert result.unverified == []

    def test_a_rise_of_one_tenth_leaves_the_sell_price_alone(self):
        result = _reprice(_row(sell=5.0, purchase=5.0, at_scrape=5.0), today=51)

        assert result.finances.squad[0].sell_price == 5.0
        assert result.updated == result.unverified == []

    def test_a_fall_below_the_purchase_price_gives_todays_price(self):
        result = _reprice(_row(sell=5.1, purchase=5.0, at_scrape=5.2), today=48)

        assert result.finances.squad[0].sell_price == 4.8
        assert [p.name for p in result.updated] == ["Mbeumo"]

    def test_a_row_whose_scrape_time_check_fails_keeps_its_scraped_price(self):
        # The rule gives 5.2 at the scrape's 5.4, but FPL said 5.0: not to be trusted with 5.6.
        result = _reprice(_row(sell=5.0, purchase=5.0, at_scrape=5.4), today=56)

        assert result.finances.squad[0].sell_price == 5.0
        assert [p.name for p in result.unverified] == ["Mbeumo"]

    def test_an_unknown_purchase_price_keeps_the_scraped_price(self):
        result = _reprice(_row(sell=5.0, purchase=0.0, at_scrape=5.0), today=52)

        assert result.finances.squad[0].sell_price == 5.0
        assert [p.name for p in result.unverified] == ["Mbeumo"]

    def test_an_unchanged_price_needs_no_purchase_price(self):
        result = _reprice(_row(sell=5.0, purchase=0.0, at_scrape=5.0), today=50)

        assert result.finances.squad[0].sell_price == 5.0
        assert result.updated == result.unverified == []

    def test_an_older_cache_is_never_repriced_and_only_flagged_where_today_disagrees(self):
        # No scrape-time price to check the rule against. At 5.1 the rule still
        # gives the scraped 5.0, so it stands unflagged; at 5.2 it would not.
        steady = _reprice(_row(sell=5.0, purchase=5.0, at_scrape=None), today=51)
        moved = _reprice(_row(sell=5.0, purchase=5.0, at_scrape=None), today=52)

        assert steady.updated == steady.unverified == []
        assert moved.finances.squad[0].sell_price == 5.0
        assert [p.name for p in moved.unverified] == ["Mbeumo"]

    def test_a_player_missing_from_todays_list_is_flagged(self):
        result = _reprice(_row(sell=5.0, purchase=5.0, at_scrape=5.0), today=52, element_id=8)

        assert result.finances.squad[0].sell_price == 5.0
        assert [p.name for p in result.unverified] == ["Mbeumo"]

    def test_an_id_now_held_by_another_footballer_is_not_repriced(self):
        """A cache from an earlier season: the id names someone else this season."""
        result = _reprice(_row(sell=5.0, purchase=5.0, at_scrape=5.0), today=52, code=9009)

        assert result.finances.squad[0].sell_price == 5.0
        assert [p.name for p in result.unverified] == ["Mbeumo"]

    def test_the_cached_rows_are_left_as_scraped(self):
        row = _row(sell=5.0, purchase=5.0, at_scrape=5.0)
        _reprice(row, today=52)

        assert (row.sell_price, row.scraped_sell_price) == (5.0, None)
