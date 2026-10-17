"""Tests for bringing cached sell prices forward to today's prices (#426)."""

from __future__ import annotations

import pytest

from fpl_cli.scraper.fpl_prices import PlayerSellPrice, TeamFinances
from fpl_cli.services.sell_prices import (
    AS_SCRAPED,
    PLAYER_NOT_CACHED,
    carried_warnings,
    out_player_warnings,
    reprice_squad,
    selling_price,
)
from tests.conftest import make_player


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
    finances = TeamFinances(bank=0.5, free_transfers=1, squad=[row], total_value=row.sell_price + 0.5)
    return reprice_squad(finances, [make_player(id=element_id, code=code, web_name="Mbeumo", now_cost=today)])


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

    def test_every_row_of_a_cache_from_before_scrape_time_prices_is_flagged(self):
        """Nothing to check the rule against, so not even a price that still fits is vouched for."""
        result = _reprice(_row(sell=5.0, purchase=5.0, at_scrape=None), today=51)

        assert result.finances.squad[0].sell_price == 5.0
        assert [p.name for p in result.unverified] == ["Mbeumo"]

    def test_a_player_missing_from_todays_list_is_flagged(self):
        result = _reprice(_row(sell=5.0, purchase=5.0, at_scrape=5.0), today=52, element_id=8)

        assert result.finances.squad[0].sell_price == 5.0
        assert [p.name for p in result.unverified] == ["Mbeumo"]

    def test_an_id_now_held_by_another_footballer_is_not_repriced(self):
        """A cache from an earlier season: the id names someone else this season."""
        result = _reprice(_row(sell=5.0, purchase=5.0, at_scrape=5.0), today=52, code=9009)

        assert result.finances.squad[0].sell_price == 5.0
        assert [p.name for p in result.unverified] == ["Mbeumo"]

    def test_the_view_totals_todays_prices(self):
        result = _reprice(_row(sell=5.0, purchase=5.0, at_scrape=5.0), today=52)

        assert result.finances.total_value == pytest.approx(5.6)  # 5.1 sell + 0.5 bank

    def test_the_cached_rows_are_left_as_scraped(self):
        row = _row(sell=5.0, purchase=5.0, at_scrape=5.0)
        _reprice(row, today=52)

        assert (row.sell_price, row.scraped_sell_price) == (5.0, 5.0)

    def test_saving_the_view_still_writes_fpls_own_figures(self):
        """The next load's check needs the scraped sell price beside the scrape-time price."""
        result = _reprice(_row(sell=5.0, purchase=5.0, at_scrape=5.0), today=52)

        [saved] = result.finances.to_dict()["squad"]
        assert (saved["sell_price"], saved["price_at_scrape"]) == (5.0, 5.0)

    def test_repricing_twice_keeps_the_scraped_figure(self):
        once = _reprice(_row(sell=5.0, purchase=5.0, at_scrape=5.0), today=52)
        twice = reprice_squad(once.finances, [make_player(id=7, code=7007, web_name="Mbeumo", now_cost=54)])

        [row] = twice.finances.squad
        assert (row.sell_price, row.scraped_sell_price) == (5.2, 5.0)


class TestRepricingMessages:
    def test_the_note_names_each_move(self):
        result = _reprice(_row(sell=5.0, purchase=5.0, at_scrape=5.0), today=52)

        assert result.notes() == [
            "1 sell price updated for price changes since the scrape: Mbeumo £5.0m → £5.1m",
        ]

    def test_the_warning_names_each_unverified_player_and_the_fix(self):
        result = _reprice(_row(sell=5.0, purchase=0.0, at_scrape=5.0), today=52)

        [warning] = result.warnings()
        assert warning["code"] == AS_SCRAPED
        assert "1 of 1 sell prices" in warning["message"]
        assert "Mbeumo" in warning["message"]
        assert "fpl squad sell-prices --refresh" in warning["message"]

    def test_nothing_to_say_when_nothing_moved(self):
        result = _reprice(_row(sell=5.0, purchase=5.0, at_scrape=5.0), today=50)

        assert result.notes() == result.warnings() == []


class TestOutPlayerWarnings:
    """`transfer-eval` builds affordability on one sell price, so it says when that one is suspect."""

    OUT = make_player(id=7, code=7007, web_name="Mbeumo", now_cost=52)

    def test_none_for_a_checked_price(self):
        result = _reprice(_row(sell=5.0, purchase=5.0, at_scrape=5.0), today=52)
        assert out_player_warnings(result, self.OUT) == []

    def test_an_unchecked_price_is_named(self):
        result = _reprice(_row(sell=5.0, purchase=0.0, at_scrape=5.0), today=52)

        [warning] = out_player_warnings(result, self.OUT)
        assert warning["code"] == AS_SCRAPED
        assert warning["message"].startswith("Mbeumo's sell price could not be checked")

    def test_a_player_not_in_the_cached_squad(self):
        result = _reprice(_row(sell=5.0, purchase=5.0, at_scrape=5.0, element_id=8), today=52, element_id=8)

        [warning] = out_player_warnings(result, self.OUT)
        assert warning["code"] == PLAYER_NOT_CACHED
        assert "fpl squad sell-prices --refresh" in warning["message"]

    def test_a_cache_with_no_player_ids_cannot_be_matched(self):
        dom_row = PlayerSellPrice(name="Mbeumo", sell_price=5.0)
        result = reprice_squad(TeamFinances(bank=0.5, free_transfers=1, squad=[dom_row]), [self.OUT])

        [warning] = out_player_warnings(result, self.OUT)
        assert warning["code"] == PLAYER_NOT_CACHED


class TestCarriedWarnings:
    def test_keeps_only_the_as_scraped_entries(self):
        warnings = [
            {"code": "sell_prices_code_missing", "message": "codes"},
            {"code": AS_SCRAPED, "message": "Salah may be out of date"},
            "not a warning",
        ]
        assert carried_warnings(warnings) == [{"code": AS_SCRAPED, "message": "Salah may be out of date"}]
