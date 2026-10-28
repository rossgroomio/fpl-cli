"""Tests for Manager and Dud of the Month, folded out of the ledger (issue #425)."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from fpl_cli.cli._league_recap_data import month_award_entries, month_table_mismatches
from fpl_cli.cli._league_recap_types import (
    format_month_honours_line,
    month_awards_withheld_line,
    month_table_withheld_reason,
    recap_month_tables,
)
from fpl_cli.cli.league_recap import _month_table_disagrees_message
from fpl_cli.models.season_month import SeasonMonth, season_months
from fpl_cli.paths import user_data_dir
from fpl_cli.services.league_history import LeagueHistoryStore
from fpl_cli.services.league_history_months import (
    MonthAwards,
    MonthlyHonours,
    MonthTotal,
    build_monthly_honours,
)
from tests.conftest import PHASES_2026_27, make_history_row, make_season_months

MONTHS = make_season_months()
SEPTEMBER = next(month for month in MONTHS if month.name == "September")  # GW3-5

# (manager key, name, gross points, transfer cost) for one gameweek's rows.
Score = tuple[int, str, int | None, int | None]


def _store(fpl_format: str = "classic") -> LeagueHistoryStore:
    return LeagueHistoryStore("2026-27", fpl_format, 1)  # type: ignore[arg-type]


def _record(store: LeagueHistoryStore, gameweek: int, *scores: Score, **kwargs) -> None:
    store.append_rows(gameweek, [
        make_history_row(
            fpl_format=store.fpl_format, gameweek=gameweek, manager_key=key, manager_name=name,
            gross_points=points, transfer_cost=cost, **kwargs,
        )
        for key, name, points, cost in scores
    ])


def _september(store: LeagueHistoryStore, **kwargs) -> MonthAwards:
    awards = build_monthly_honours(store, MONTHS, 5, **kwargs).ending_at(5)
    assert awards is not None
    return awards


def _names(totals: list[MonthTotal]) -> list[str]:
    return [total.manager_name for total in totals]


def _awards(month: SeasonMonth, *totals: tuple[str, int]) -> MonthAwards:
    """A month's awards as the fold would rank them, built by hand."""
    points = [p for _, p in totals]
    return MonthAwards(month=month, totals=[
        MonthTotal(
            manager_key=index, manager_name=name, points=p,
            rank=1 + sum(other > p for other in points),
        )
        for index, (name, p) in enumerate(totals, start=1)
    ])


# ---------------------------------------------------------------------------
# FPL's months
# ---------------------------------------------------------------------------


class TestSeasonMonths:
    def test_the_season_long_phase_is_left_out_and_the_months_kept_in_order(self):
        months = season_months(PHASES_2026_27)

        assert [month.name for month in months] == [
            "August", "September", "October", "November", "December",
            "January", "February", "March", "April", "May",
        ]
        assert (months[0].id, months[0].first_gameweek, months[0].last_gameweek) == (2, 1, 2)
        assert (months[-1].id, months[-1].first_gameweek, months[-1].last_gameweek) == (11, 34, 38)

    def test_the_season_long_phase_is_told_apart_by_its_span_not_its_name_or_place(self):
        """A payload that renames or reorders it still leaves the months alone."""
        phases = [*PHASES_2026_27[1:], {**PHASES_2026_27[0], "id": 99, "name": "Season"}]

        assert [month.name for month in season_months(phases)][:2] == ["August", "September"]
        assert all(month.id != 99 for month in season_months(phases))

    def test_a_lone_phase_is_the_whole_season_and_gives_no_months(self):
        assert season_months(PHASES_2026_27[:1]) == []
        assert season_months([]) == []

    def test_a_month_missing_a_boundary_cannot_be_read(self):
        with pytest.raises(ValidationError):
            season_months([PHASES_2026_27[0], {"id": 2, "name": "August", "start_event": 1}])

    def test_a_month_is_named_with_its_gameweeks(self):
        assert SEPTEMBER.label == "September (GW3-5)"
        assert list(SEPTEMBER.gameweeks) == [3, 4, 5]


# ---------------------------------------------------------------------------
# Scoring a month
# ---------------------------------------------------------------------------


class TestMonthScoring:
    def test_each_managers_points_are_summed_over_the_months_gameweeks(self):
        store = _store()
        _record(store, 3, (1, "Alice", 60, 0), (2, "Bob", 50, 0))
        _record(store, 4, (1, "Alice", 70, 0), (2, "Bob", 50, 0))
        _record(store, 5, (1, "Alice", 80, 0), (2, "Bob", 50, 0))

        september = _september(store)

        assert september.awarded
        assert [(t.manager_name, t.points, t.rank) for t in september.totals] == [
            ("Alice", 210, 1), ("Bob", 150, 2),
        ]
        assert _names(september.managers_of_the_month) == ["Alice"]
        assert _names(september.duds_of_the_month) == ["Bob"]

    def test_a_hit_is_charged_in_every_gameweek_but_the_months_first(self):
        """FPL's own month table charges no hit taken for the month's first
        gameweek, and every other one: a GW3 hit leaves September alone, a
        GW4 hit comes off it."""
        store = _store()
        _record(store, 3, (1, "Alice", 70, 8), (2, "Bob", 60, 0))
        _record(store, 4, (1, "Alice", 60, 0), (2, "Bob", 70, 8))
        _record(store, 5, (1, "Alice", 60, 0), (2, "Bob", 60, 0))

        totals = {t.manager_name: t.points for t in _september(store).totals}

        assert totals == {"Alice": 190, "Bob": 182}

    def test_a_hit_that_changes_the_months_order_ranks_on_net_points(self):
        store = _store()
        _record(store, 3, (1, "Alice", 70, 0), (2, "Bob", 64, 0))
        _record(store, 4, (1, "Alice", 70, 12), (2, "Bob", 63, 0))
        _record(store, 5, (1, "Alice", 60, 0), (2, "Bob", 63, 0))

        september = _september(store)

        # 200 gross against 190, but 188 net: Bob's month.
        assert _names(september.managers_of_the_month) == ["Bob"]
        assert _names(september.duds_of_the_month) == ["Alice"]
        assert {t.manager_name: t.points for t in september.totals} == {"Alice": 188, "Bob": 190}

    def test_a_draft_month_is_a_plain_sum_on_the_main_games_boundaries(self):
        """Draft rows carry no hit, and the draft game publishes no months, so
        its ledger is read against the main game's."""
        store = _store("draft")
        for gameweek in (3, 4, 5):
            _record(store, gameweek, (10, "Alice", 40, None), (11, "Bob", 45, None))

        september = _september(store)

        assert {t.manager_name: t.points for t in september.totals} == {"Alice": 120, "Bob": 135}
        assert _names(september.managers_of_the_month) == ["Bob"]


class TestTies:
    @staticmethod
    def _month(*totals: tuple[str, int, int]) -> MonthAwards:
        """September with each manager's (name, GW3, GW4) and 0 in GW5."""
        store = _store()
        managers = list(enumerate(totals, start=1))
        _record(store, 3, *((key, name, gw3, 0) for key, (name, gw3, _) in managers))
        _record(store, 4, *((key, name, gw4, 0) for key, (name, _, gw4) in managers))
        _record(store, 5, *((key, name, 0, 0) for key, (name, _, _) in managers))
        return _september(store)

    def test_a_tie_on_the_top_total_shares_manager_of_the_month(self):
        september = self._month(("Alice", 100, 100), ("Bob", 120, 80), ("Cara", 80, 70), ("Dan", 70, 70))

        assert _names(september.managers_of_the_month) == ["Alice", "Bob"]
        assert [t.rank for t in september.totals] == [1, 1, 3, 4]
        assert _names(september.top_three) == ["Alice", "Bob", "Cara"]
        assert _names(september.duds_of_the_month) == ["Dan"]

    def test_a_tie_on_the_lowest_total_shares_dud_of_the_month(self):
        september = self._month(("Alice", 100, 100), ("Bob", 70, 70), ("Cara", 80, 60))

        assert _names(september.duds_of_the_month) == ["Bob", "Cara"]

    def test_a_tie_on_third_puts_everyone_on_it_in_the_top_three(self):
        september = self._month(
            ("Alice", 100, 100), ("Bob", 100, 90), ("Cara", 90, 90), ("Dan", 100, 80), ("Eve", 50, 50),
        )

        assert _names(september.top_three) == ["Alice", "Bob", "Cara", "Dan"]

    def test_a_league_level_on_points_has_no_dud(self):
        """The same managers under both headings would be one fact twice, so
        the Dud goes and the Manager of the Month stays (issue #145)."""
        september = self._month(("Alice", 100, 100), ("Bob", 120, 80))

        assert _names(september.managers_of_the_month) == ["Alice", "Bob"]
        assert september.duds_of_the_month == []


# ---------------------------------------------------------------------------
# Gaps
# ---------------------------------------------------------------------------


class TestAGapWithholdsBothAwards:
    def test_an_unknown_row_withholds_both_and_says_why(self):
        """Summing what is there would make the manager with the missing
        points the Dud by default, and a missing score could have won it.
        The unknown row's own figure, the league table's, is never read."""
        store = _store()
        _record(store, 3, (1, "Alice", 60, 0), (2, "Bob", 50, 0))
        _record(store, 4, (2, "Bob", 50, 0))
        _record(store, 4, (1, "Alice", 75, None), capture_status="unknown")
        _record(store, 5, (1, "Alice", 60, 0), (2, "Bob", 50, 0))

        september = _september(store)

        assert not september.awarded
        assert september.withheld_reason == "no score is recorded for Alice in GW4"
        assert september.totals == []
        assert september.managers_of_the_month == []
        assert september.duds_of_the_month == []

    def test_a_gameweek_never_captured_withholds_both(self):
        store = _store()
        _record(store, 3, (1, "Alice", 60, 0))
        _record(store, 5, (1, "Alice", 60, 0))

        assert _september(store).withheld_reason == "GW4 was never captured"

    def test_a_manager_with_no_row_for_one_of_the_gameweeks_withholds_both(self):
        """Cara joined at GW4: she is in the table as the month closed, with
        GW3 missing."""
        store = _store()
        _record(store, 3, (1, "Alice", 60, 0))
        _record(store, 4, (1, "Alice", 60, 0), (3, "Cara", 90, 0))
        _record(store, 5, (1, "Alice", 60, 0), (3, "Cara", 90, 0))

        assert _september(store).withheld_reason == "no score is recorded for Cara in GW3"

    def test_an_unreadable_gameweek_withholds_both(self):
        store = _store()
        _record(store, 3, (1, "Alice", 60, 0))
        _record(store, 5, (1, "Alice", 60, 0))
        path = store.gameweek_file(4)
        path.write_text("not json{{{\n", encoding="utf-8")

        assert _september(store).withheld_reason == "GW4 could not be read"

    def test_a_row_that_records_no_score_withholds_both(self):
        store = _store()
        _record(store, 3, (1, "Alice", 60, 0))
        _record(store, 4, (1, "Alice", None, 0))
        _record(store, 5, (1, "Alice", 60, 0))

        assert _september(store).withheld_reason == "no score is recorded for Alice in GW4"

    def test_a_classic_row_with_no_hit_recorded_withholds_both(self):
        """Classic records a hit every gameweek, zero included, so a missing
        one is a hit nobody read (the coarse tier's field absent) rather than
        none taken, and it could have cost the month. The month's first
        gameweek is charged no hit, so one missing there costs nothing."""
        store = _store()
        _record(store, 3, (1, "Alice", 60, None), (2, "Bob", 50, 0))
        _record(store, 4, (1, "Alice", 60, 0), (2, "Bob", 50, None))
        _record(store, 5, (1, "Alice", 60, 0), (2, "Bob", 50, 0))

        assert _september(store).withheld_reason == "no transfer cost is recorded for Bob in GW4"

    def test_a_missing_score_and_a_missing_hit_are_each_named(self):
        store = _store()
        _record(store, 3, (1, "Alice", 60, 0), (2, "Bob", 50, 0))
        _record(store, 4, (1, "Alice", None, 0), (2, "Bob", 50, None))
        _record(store, 5, (1, "Alice", 60, 0), (2, "Bob", 50, 0))

        assert _september(store).withheld_reason == (
            "no score is recorded for Alice in GW4 and no transfer cost is recorded for Bob in GW4"
        )

    def test_a_long_list_of_gaps_names_three_managers_and_counts_the_rest(self):
        store = _store()
        names = ["Alice", "Bob", "Cara", "Dan", "Eve"]
        _record(store, 3, *((key, name, 60, 0) for key, name in enumerate(names, start=1)))
        _record(store, 4, *((key, name, None, None) for key, name in enumerate(names, start=1)),
                capture_status="unknown")
        _record(store, 5, *((key, name, 60, 0) for key, name in enumerate(names, start=1)))

        assert _september(store).withheld_reason == (
            "no score is recorded for Alice in GW4, Bob in GW4, Cara in GW4 and 2 other managers"
        )

    def test_a_manager_who_left_partway_through_is_not_in_the_months_table(self):
        """The month's table is the league as it closed, as FPL's own is:
        Dan left after GW4, so he is neither a gap nor the Dud."""
        store = _store()
        _record(store, 3, (1, "Alice", 60, 0), (2, "Bob", 50, 0), (4, "Dan", 10, 0))
        _record(store, 4, (1, "Alice", 60, 0), (2, "Bob", 50, 0), (4, "Dan", 10, 0))
        _record(store, 5, (1, "Alice", 60, 0), (2, "Bob", 50, 0))

        september = _september(store)

        assert september.awarded
        assert _names(september.totals) == ["Alice", "Bob"]
        assert _names(september.duds_of_the_month) == ["Bob"]


# ---------------------------------------------------------------------------
# The honours list
# ---------------------------------------------------------------------------


class TestHonoursList:
    @staticmethod
    def _season(store: LeagueHistoryStore, through: int) -> None:
        """Alice outscores Bob every week of the season, Bob takes no hits."""
        for gameweek in range(1, through + 1):
            _record(store, gameweek, (1, "Alice", 60, 0), (2, "Bob", 40, 0))

    def test_only_months_completed_by_the_gameweek_are_listed_in_order(self):
        store = _store()
        self._season(store, 7)

        honours = build_monthly_honours(store, MONTHS, 7)

        assert [awards.month.name for awards in honours.months] == ["August", "September"]
        assert honours.through_gameweek == 7
        assert (honours.season, honours.fpl_format, honours.league_id) == ("2026-27", "classic", 1)

    def test_the_list_grows_month_by_month_and_the_finale_lists_all_ten(self):
        store = _store()
        self._season(store, 38)

        lengths = [len(build_monthly_honours(store, MONTHS, month.last_gameweek).months) for month in MONTHS]
        finale = build_monthly_honours(store, MONTHS, 38)

        assert lengths == list(range(1, 11))
        assert len(finale.months) == 10
        assert all(awards.awarded for awards in finale.months)
        assert {_names(awards.managers_of_the_month)[0] for awards in finale.months} == {"Alice"}
        assert finale.ending_at(38) is finale.months[-1]

    def test_only_the_last_gameweek_of_a_month_closes_it(self):
        store = _store()
        self._season(store, 5)

        assert build_monthly_honours(store, MONTHS, 5).ending_at(5) is not None
        assert build_monthly_honours(store, MONTHS, 4).ending_at(4) is None

    def test_a_month_before_the_league_began_is_left_out_and_one_it_began_in_starts_there(self):
        """FPL's own table for a league begun partway through a month counts
        the month from the league's first gameweek (an August of GW2 alone,
        for a league begun at GW2)."""
        store = _store()
        for gameweek in range(4, 10):
            _record(store, gameweek, (1, "Alice", 60, 0), (2, "Bob", 40, 0))

        honours = build_monthly_honours(store, MONTHS, 9, league_start_gameweek=4)

        assert [awards.month.label for awards in honours.months] == ["September (GW4-5)", "October (GW6-9)"]
        september = honours.months[0]
        assert september.awarded
        assert september.month.id == 3, "still FPL's September, for the cross-check"
        assert {t.manager_name: t.points for t in september.totals} == {"Alice": 120, "Bob": 80}
        assert honours.ending_at(5) is september

    def test_the_leagues_first_gameweek_opens_its_month_for_the_hit_rule(self):
        store = _store()
        _record(store, 4, (1, "Alice", 70, 8))
        _record(store, 5, (1, "Alice", 60, 4))

        september = build_monthly_honours(store, MONTHS, 5, league_start_gameweek=4).months[0]

        assert {t.manager_name: t.points for t in september.totals} == {"Alice": 126}

    def test_a_recaptured_gameweek_changes_the_award_with_no_stale_value(self):
        """Derived, never stored: the superseding row is all it takes."""
        store = _store()
        _record(store, 3, (1, "Alice", 60, 0), (2, "Bob", 50, 0))
        _record(store, 4, (1, "Alice", 60, 0), (2, "Bob", 50, 0))
        _record(store, 5, (1, "Alice", 60, 0), (2, "Bob", 50, 0))
        assert _names(_september(store).managers_of_the_month) == ["Alice"]

        _record(store, 4, (2, "Bob", 90, 0), captured_at=datetime(2026, 10, 1, tzinfo=timezone.utc))

        recomputed = _september(_store())
        assert _names(recomputed.managers_of_the_month) == ["Bob"]
        assert {t.manager_name: t.points for t in recomputed.totals} == {"Alice": 180, "Bob": 190}

    def test_a_league_read_in_part_withholds_every_month(self):
        """The members the recap never read are a gap the store cannot see,
        and the Dud is almost always one of them, so a full ledger for the
        managers it did read awards nothing."""
        store = _store()
        self._season(store, 9)
        reason = "the recap reads only the first 50 of the league's members"

        honours = build_monthly_honours(store, MONTHS, 9, partial_league=reason)

        assert [awards.month.name for awards in honours.months] == ["August", "September", "October"]
        assert [awards.withheld_reason for awards in honours.months] == [reason] * 3
        assert not any(awards.totals for awards in honours.months)

    def test_withholding_one_month_leaves_the_rest_and_the_original_alone(self):
        store = _store()
        self._season(store, 9)
        honours = build_monthly_honours(store, MONTHS, 9)
        october = honours.months[2]

        withheld = honours.withholding(october.month, "FPL's own table for the month disagrees")

        assert [awards.awarded for awards in withheld.months] == [True, True, False]
        assert withheld.months[:2] == honours.months[:2]
        assert withheld.ending_at(9) == MonthAwards(
            month=october.month, withheld_reason="FPL's own table for the month disagrees",
        )
        assert october.awarded, "the list it was made from is unchanged"

    def test_nothing_is_written_beside_the_ledger(self):
        store = _store()
        self._season(store, 5)
        before = sorted(path.relative_to(user_data_dir()) for path in user_data_dir().rglob("*"))

        build_monthly_honours(store, MONTHS, 5)

        assert sorted(path.relative_to(user_data_dir()) for path in user_data_dir().rglob("*")) == before


# ---------------------------------------------------------------------------
# How the recap words it
# ---------------------------------------------------------------------------


class TestTheAwardsWording:
    def test_manager_of_the_month_names_the_rest_of_the_top_three(self):
        entries = month_award_entries(_awards(SEPTEMBER, ("Alice", 245), ("Bob", 238), ("Cara", 230), ("Dan", 172)))

        assert entries["manager_of_the_month"]["detail"] == (
            "Alice with 245 pts across September (GW3-5), ahead of Bob (238) and Cara (230)"
        )
        assert entries["manager_of_the_month"]["value"] == 245
        assert entries["dud_of_the_month"]["detail"] == "Dan with 172 pts across September (GW3-5)"
        assert entries["dud_of_the_month"]["manager_name"] == "Dan"

    def test_a_shared_award_says_each(self):
        entries = month_award_entries(_awards(SEPTEMBER, ("Alice", 245), ("Bob", 245), ("Cara", 230), ("Dan", 172), ("Eve", 172)))

        assert entries["manager_of_the_month"]["detail"] == (
            "Alice and Bob with 245 pts each across September (GW3-5), ahead of Cara (230)"
        )
        assert entries["manager_of_the_month"]["manager_name"] == "Alice and Bob"
        assert entries["dud_of_the_month"]["detail"] == "Dan and Eve with 172 pts each across September (GW3-5)"

    def test_a_wide_tie_behind_the_winner_names_three_and_counts_the_rest(self):
        entries = month_award_entries(_awards(
            SEPTEMBER, ("Alice", 245), ("Bob", 230), ("Cara", 230), ("Dan", 230), ("Eve", 230), ("Fay", 100),
        ))

        assert entries["manager_of_the_month"]["detail"].endswith(
            "ahead of Bob (230), Cara (230), Dan (230) and 1 more on 230 pts",
        )

    def test_a_level_league_has_a_manager_of_the_month_and_no_dud(self):
        entries = month_award_entries(_awards(SEPTEMBER, ("Alice", 200), ("Bob", 200)))

        assert "dud_of_the_month" not in entries
        assert format_month_honours_line(_awards(SEPTEMBER, ("Alice", 200), ("Bob", 200))) == (
            "September (GW3-5): Manager of the Month Alice and Bob with 200 pts each;"
            " no Dud of the Month, as nobody finished below them"
        )

    def test_a_withheld_month_gives_no_awards_and_says_why_everywhere(self):
        withheld = MonthAwards(month=SEPTEMBER, withheld_reason="GW4 was never captured")

        assert month_award_entries(withheld) == {}
        assert month_awards_withheld_line(withheld) == "Withheld for September (GW3-5), as GW4 was never captured."
        assert format_month_honours_line(withheld) == "September (GW3-5): not awarded, as GW4 was never captured"

    def test_a_month_fpls_own_table_disagrees_with_says_so_in_its_place(self):
        withheld = MonthAwards(month=SEPTEMBER, withheld_reason=month_table_withheld_reason(2))

        assert month_awards_withheld_line(withheld) == (
            "Withheld for September (GW3-5), as FPL's own table for the month disagrees with"
            " this recap's totals for 2 managers."
        )
        assert month_table_withheld_reason(1).endswith("for 1 manager")

    def test_an_honours_line_names_both_awards(self):
        line = format_month_honours_line(_awards(SEPTEMBER, ("Alice", 245), ("Bob", 238), ("Dan", 172)))

        assert line == (
            "September (GW3-5): Manager of the Month Alice with 245 pts; Dud of the Month Dan with 172 pts"
        )


class TestTheMonthTableCrossCheck:
    def test_agreement_finds_nothing(self):
        awards = _awards(SEPTEMBER, ("Alice", 245), ("Bob", 238))

        assert month_table_mismatches(awards, [{"entry": 1, "total": 245}, {"entry": 2, "total": 238}]) == []

    def test_a_disagreement_names_the_manager_and_both_totals(self):
        awards = _awards(SEPTEMBER, ("Alice", 245), ("Bob", 238))

        assert month_table_mismatches(awards, [{"entry": 1, "total": 241}, {"entry": 2, "total": 238}]) == [
            ("Alice", 245, 241),
        ]

    def test_a_manager_only_one_side_holds_is_no_disagreement(self):
        """FPL's table is the league as it stands when read, so a manager who
        has left since is missing from it, and one who joined since is new."""
        awards = _awards(SEPTEMBER, ("Alice", 245), ("Bob", 238))

        assert month_table_mismatches(awards, [{"entry": 1, "total": 245}, {"entry": 9, "total": 300}]) == []

    def test_a_row_it_cannot_read_is_skipped(self):
        awards = _awards(SEPTEMBER, ("Alice", 245))

        assert month_table_mismatches(awards, [{"entry": 1}, "junk", {"entry": "1", "total": 200}]) == []

    def test_the_warning_names_three_disagreements_and_counts_the_rest(self):
        """A change to how FPL scores a month disagrees for everyone who took
        a hit, which must not read out the league."""
        awards = _awards(SEPTEMBER, ("Alice", 245))
        mismatches = [(name, 200, 196) for name in ("Alice", "Bob", "Cara", "Dan", "Eve")]

        message = _month_table_disagrees_message(awards, mismatches)

        assert message == (
            "FPL's own September table disagrees with the ledger's totals for 5 managers"
            " (Alice: 200 here, 196 on FPL; Bob: 200 here, 196 on FPL; Cara: 200 here, 196 on FPL;"
            " 2 more), so this recap withholds the month's Manager and Dud of the Month rather"
            " than contradict it."
        )
        assert "for 1 manager (" in _month_table_disagrees_message(awards, mismatches[:1])


class TestTheMonthTablesTheTieCheckReads:
    def test_each_awarded_month_gives_its_places_and_the_closing_one_is_marked(self):
        honours = MonthlyHonours(
            season="2026-27", fpl_format="classic", league_id=1, through_gameweek=5,
            months=[
                MonthAwards(month=MONTHS[0], withheld_reason="GW1-2 were never captured"),
                _awards(SEPTEMBER, ("Alice", 200), ("Bob", 200), ("Cara", 150)),
            ],
        )

        assert recap_month_tables(honours) == [
            {"name": "September", "closing": True, "ranks": [("Alice", 1), ("Bob", 1), ("Cara", 3)]},
        ]
