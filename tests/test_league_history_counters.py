"""Tests for the streak-condition registry and counters projection (U8)."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from fpl_cli.models.league_history import (
    LEAGUE_HISTORY_COUNTERS_VERSION,
    ConditionRunState,
    LeagueHistoryCountersProjection,
    LedgerCaptaincy,
    LedgerTransaction,
)
from fpl_cli.services.league_history import LeagueHistoryStore
from fpl_cli.services.league_history_counters import (
    CONDITIONS,
    ConditionRunView,
    CountSurfacePolicy,
    all_condition_views,
    compute_counters_through,
    conditions_for_format,
    counters_file,
    counters_partition_dir,
    invalidate_if_repaired,
    manager_condition_views,
    rebuild_counters_through,
)
from tests.conftest import make_history_row

# ---------------------------------------------------------------------------
# Local helpers
# ---------------------------------------------------------------------------


def _captain(points: int, had_fixture: bool | None = True) -> LedgerCaptaincy:
    return LedgerCaptaincy(name="Cap", points=points, played=True, had_fixture=had_fixture)


def _transaction(net: int) -> LedgerTransaction:
    return LedgerTransaction(
        player_in="In Guy", player_in_team="AAA", player_out="Out Guy", player_out_team="BBB", net=net,
    )


LATER = datetime(2027, 1, 1, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


class TestConditionRegistry:
    def test_exactly_nine_conditions_are_registered(self):
        assert len(CONDITIONS) == 9
        assert len({c.key for c in CONDITIONS}) == 9

    def test_min_run_lengths_match_the_spec(self):
        """`None` marks a condition that never surfaces as a streak at all:
        the bottom half and the green-arrow drought restate where the table
        already shows you are, so their run exists only to drive the season
        count's firing rule."""
        min_runs = {c.key: c.min_run for c in CONDITIONS}
        assert min_runs == {
            "weeks_on_top": 2,
            "bottom_half_run": None,
            "gw_win_streak": 2,
            "gw_loss_streak": 2,
            "green_arrow_drought": None,
            "captain_blank_run": 2,
            "hit_run": 2,
            "waiver_win_run": 3,
            "waiver_burn_run": 3,
        }

    def test_a_streakless_condition_is_never_reportable_however_long_the_run(self):
        view = _view(occurrences=40, length=40)
        streakless = ConditionRunView(**{**view.__dict__, "min_run": None})
        assert streakless.is_reportable is False
        assert streakless.excess == 0

    def test_conditions_for_classic_excludes_waiver_conditions(self):
        keys = {c.key for c in conditions_for_format("classic")}
        assert keys == {
            "weeks_on_top", "bottom_half_run", "gw_win_streak", "gw_loss_streak",
            "green_arrow_drought", "captain_blank_run", "hit_run",
        }

    def test_conditions_for_draft_excludes_classic_only_conditions(self):
        keys = {c.key for c in conditions_for_format("draft")}
        assert keys == {
            "weeks_on_top", "bottom_half_run", "gw_win_streak", "gw_loss_streak",
            "green_arrow_drought", "waiver_win_run", "waiver_burn_run",
        }

    def test_every_condition_names_its_occurrence_in_both_forms(self):
        """Issue #164: the season count needs a noun for one occurrence, and
        the drought's must state the *absence* it counts -- "gameweeks
        without a green arrow" -- rather than leaving the inversion for the
        reader to infer from the run label."""
        assert all(c.count_label_one and c.count_label_many for c in CONDITIONS)
        drought = next(c for c in CONDITIONS if c.key == "green_arrow_drought")
        assert drought.count_label_one == "gameweek without a green arrow"
        assert drought.count_label_many == "gameweeks without a green arrow"

    def test_count_surface_policies_match_the_spec(self):
        """Each condition's weekly-render rule, locked to the agreed spec.
        A rare, discrete event affords a generous rule; a standing table
        position half the league increments every gameweek needs a strict
        one, or the weekly report is wallpaper."""
        policies = {c.key: c.count_policy for c in CONDITIONS}
        assert policies == {
            "gw_win_streak": CountSurfacePolicy(step=3, first_in_second_half=True),
            "gw_loss_streak": CountSurfacePolicy(step=3, first_in_second_half=True),
            "captain_blank_run": CountSurfacePolicy(step=5, ride_along_min=3),
            "hit_run": CountSurfacePolicy(step=3, ride_along_min=2),
            "waiver_win_run": CountSurfacePolicy(step=5),
            "waiver_burn_run": CountSurfacePolicy(step=5),
            "weeks_on_top": CountSurfacePolicy(step=5),
            "bottom_half_run": CountSurfacePolicy(
                step=10, ride_along_within=5, second_half_only=True,
            ),
            "green_arrow_drought": CountSurfacePolicy(run_milestones=frozenset({5, 10})),
        }

    def test_the_waiver_counts_name_the_outcome_they_actually_measure(self):
        """Both waiver conditions key off whether the week's moves netted
        points, not off winning a claim -- so the count labels say haul and
        backfire rather than win and burn."""
        labels = {
            c.key: (c.count_label_one, c.count_label_many)
            for c in CONDITIONS if c.key.startswith("waiver_")
        }
        assert labels == {
            "waiver_win_run": ("waiver haul", "waiver hauls"),
            "waiver_burn_run": ("waiver backfire", "waiver backfires"),
        }


# ---------------------------------------------------------------------------
# captain_blank_run (classic only)
# ---------------------------------------------------------------------------


class TestCaptainBlankRun:
    def test_three_consecutive_blanks_yield_a_run_of_three_holding_none(self):
        store = LeagueHistoryStore("2026-27", "classic", 1)
        for gw in (1, 2, 3):
            store.append_rows(gw, [make_history_row(gameweek=gw, manager_key=1, captain=_captain(1))])

        projection = rebuild_counters_through(store, 3)
        view = manager_condition_views(projection, 1)["captain_blank_run"]

        assert view.length == 3
        assert view.start_gameweek == 1
        assert view.held_in_run == 0
        assert view.is_reportable is True

    def test_ae7_unknown_row_between_two_blanks_holds(self):
        """Given one manager's picks fetch fails at GW7 while the rest succeed,
        a GW7 row exists for that manager marked unknown, and their
        captain-blank streak spanning GW6 and GW8 is neither extended nor
        reset by GW7."""
        store = LeagueHistoryStore("2026-27", "classic", 1)
        store.append_rows(6, [make_history_row(gameweek=6, manager_key=1, captain=_captain(1))])
        store.append_rows(7, [make_history_row(gameweek=7, manager_key=1, capture_status="unknown")])
        store.append_rows(8, [make_history_row(gameweek=8, manager_key=1, captain=_captain(1))])

        projection = rebuild_counters_through(store, 8)
        view = manager_condition_views(projection, 1)["captain_blank_run"]

        assert view.length == 2
        assert view.start_gameweek == 6
        assert view.held_in_run == 1

    def test_ae7_a_later_run_supersedes_the_unknown_row_and_the_streak_extends(self):
        """A later run re-attempts the manager and supersedes the unknown row:
        once GW7 is filled in as a real blank, the streak becomes an
        unbroken three rather than staying at two-with-one-held."""
        store = LeagueHistoryStore("2026-27", "classic", 1)
        store.append_rows(6, [make_history_row(gameweek=6, manager_key=1, captain=_captain(1))])
        store.append_rows(7, [make_history_row(gameweek=7, manager_key=1, capture_status="unknown")])
        store.append_rows(8, [make_history_row(gameweek=8, manager_key=1, captain=_captain(1))])
        before = manager_condition_views(rebuild_counters_through(store, 8), 1)["captain_blank_run"]
        assert (before.length, before.held_in_run) == (2, 1)

        store.append_rows(7, [
            make_history_row(gameweek=7, manager_key=1, captain=_captain(1), captured_at=LATER),
        ])

        after = manager_condition_views(rebuild_counters_through(store, 8), 1)["captain_blank_run"]
        assert (after.length, after.held_in_run) == (3, 0)

    def test_captain_with_no_fixture_holds_neither_extends_nor_resets(self):
        store = LeagueHistoryStore("2026-27", "classic", 1)
        store.append_rows(1, [make_history_row(gameweek=1, manager_key=1, captain=_captain(1))])
        store.append_rows(2, [
            make_history_row(gameweek=2, manager_key=1, captain=_captain(0, had_fixture=False)),
        ])
        store.append_rows(3, [make_history_row(gameweek=3, manager_key=1, captain=_captain(1))])

        projection = rebuild_counters_through(store, 3)
        view = manager_condition_views(projection, 1)["captain_blank_run"]

        assert view.length == 2
        assert view.start_gameweek == 1
        assert view.held_in_run == 1

    def test_captain_had_fixture_unset_also_holds(self):
        """`had_fixture=None` (not recorded) must hold too, not be treated as True."""
        store = LeagueHistoryStore("2026-27", "classic", 1)
        store.append_rows(1, [make_history_row(gameweek=1, manager_key=1, captain=_captain(1))])
        store.append_rows(2, [
            make_history_row(gameweek=2, manager_key=1, captain=_captain(1, had_fixture=None)),
        ])

        projection = rebuild_counters_through(store, 2)
        view = manager_condition_views(projection, 1)["captain_blank_run"]

        assert view.length == 1
        assert view.held_in_run == 1

    def test_coarse_row_lacking_captain_detail_holds_but_weeks_on_top_still_evaluates(self):
        store = LeagueHistoryStore("2026-27", "classic", 1)
        store.append_rows(1, [
            make_history_row(gameweek=1, manager_key=1, tier="coarse", league_position=1, captain=None),
        ])

        projection = rebuild_counters_through(store, 1)
        views = manager_condition_views(projection, 1)

        assert views["captain_blank_run"].length == 0
        assert views["captain_blank_run"].held_in_run == 0  # hold before any run opens records nothing
        assert views["weeks_on_top"].length == 1
        assert views["weeks_on_top"].start_gameweek == 1

    def test_a_run_spanning_eight_held_gameweeks_reports_its_held_count(self):
        store = LeagueHistoryStore("2026-27", "classic", 1)
        store.append_rows(1, [make_history_row(gameweek=1, manager_key=1, captain=_captain(1))])
        for gw in range(2, 10):  # GW2..GW9 inclusive: 8 held gameweeks
            store.append_rows(gw, [
                make_history_row(gameweek=gw, manager_key=1, captain=_captain(0, had_fixture=False)),
            ])
        store.append_rows(10, [make_history_row(gameweek=10, manager_key=1, captain=_captain(1))])

        projection = rebuild_counters_through(store, 10)
        view = manager_condition_views(projection, 1)["captain_blank_run"]

        assert view.length == 2
        assert view.held_in_run == 8
        assert view.start_gameweek == 1

    def test_captain_blank_run_reads_the_shared_blank_threshold(self):
        from fpl_cli.models.player import BLANK_POINTS_THRESHOLD

        store = LeagueHistoryStore("2026-27", "classic", 1)
        store.append_rows(1, [
            make_history_row(gameweek=1, manager_key=1, captain=_captain(BLANK_POINTS_THRESHOLD)),
        ])
        store.append_rows(2, [
            make_history_row(gameweek=2, manager_key=1, captain=_captain(BLANK_POINTS_THRESHOLD + 1)),
        ])

        projection = rebuild_counters_through(store, 2)
        view = manager_condition_views(projection, 1)["captain_blank_run"]

        # Exactly-at-threshold extends (GW1); one point above resets (GW2).
        assert view.length == 0


# ---------------------------------------------------------------------------
# gw_win_streak / gw_loss_streak
# ---------------------------------------------------------------------------


class TestGwRankStreaks:
    def test_top_and_bottom_scorer_each_get_a_run_of_two(self):
        store = LeagueHistoryStore("2026-27", "classic", 1)
        for gw in (1, 2):
            store.append_rows(gw, [
                make_history_row(gameweek=gw, manager_key=1, gross_points=87),
                make_history_row(gameweek=gw, manager_key=2, gross_points=60),
                make_history_row(gameweek=gw, manager_key=3, gross_points=40),
            ])

        projection = rebuild_counters_through(store, 2)

        assert manager_condition_views(projection, 1)["gw_win_streak"].length == 2
        assert manager_condition_views(projection, 3)["gw_loss_streak"].length == 2
        # Neither condition falsely extends for the middle-ranked manager.
        assert manager_condition_views(projection, 2)["gw_win_streak"].length == 0
        assert manager_condition_views(projection, 2)["gw_loss_streak"].length == 0

    def test_a_tie_for_the_week_extends_every_tied_manager(self):
        """A shared gameweek win or loss must credit every tied manager, not
        just whichever the cohort's ordinal gw_rank happened to land on
        first (issue #163) -- and, since resetting a genuine run on a mere
        tie would be the destructive failure mode, must not reset the
        untied manager's run either."""
        store = LeagueHistoryStore("2026-27", "classic", 1)
        for gw in (1, 2):
            store.append_rows(gw, [
                make_history_row(gameweek=gw, manager_key=1, gross_points=87),
                make_history_row(gameweek=gw, manager_key=2, gross_points=87),
                make_history_row(gameweek=gw, manager_key=3, gross_points=40),
            ])

        projection = rebuild_counters_through(store, 2)

        assert manager_condition_views(projection, 1)["gw_win_streak"].length == 2
        assert manager_condition_views(projection, 2)["gw_win_streak"].length == 2
        assert manager_condition_views(projection, 3)["gw_loss_streak"].length == 2

    def test_a_fully_tied_cohort_holds_both_streaks_instead_of_extending_both(self):
        """When every known cohort member scores the same, there is no
        winner to distinguish from a loser -- extending both would credit
        the same manager with a win streak and a loss streak in the same
        gameweek. This must hold rather than reset, so a genuine run open
        before the tie survives it."""
        store = LeagueHistoryStore("2026-27", "classic", 1)
        store.append_rows(1, [
            make_history_row(gameweek=1, manager_key=1, gross_points=60),
            make_history_row(gameweek=1, manager_key=2, gross_points=40),
        ])
        for gw in (2, 3, 4):
            store.append_rows(gw, [
                make_history_row(gameweek=gw, manager_key=1, gross_points=50),
                make_history_row(gameweek=gw, manager_key=2, gross_points=50),
            ])

        projection = rebuild_counters_through(store, 4)

        # GW1 opens a win streak for manager 1 and a loss streak for
        # manager 2; GW2-4 tie every week and must hold, not reset, either.
        win_view = manager_condition_views(projection, 1)["gw_win_streak"]
        loss_view = manager_condition_views(projection, 2)["gw_loss_streak"]
        assert win_view.length == 1
        assert win_view.held_in_run == 3
        assert loss_view.length == 1
        assert loss_view.held_in_run == 3

        # Neither manager is credited with the opposite streak from the tie.
        assert manager_condition_views(projection, 1)["gw_loss_streak"].length == 0
        assert manager_condition_views(projection, 2)["gw_win_streak"].length == 0

    def test_a_single_member_cohort_holds_rather_than_extending_both_streaks(self):
        """A lone cohort member (e.g. a one-manager league, or every other
        fetch failed) has nobody to be better or worse than -- the same
        max-equals-min case as a fully tied cohort."""
        store = LeagueHistoryStore("2026-27", "classic", 1)
        store.append_rows(1, [make_history_row(gameweek=1, manager_key=1, gross_points=60)])

        projection = rebuild_counters_through(store, 1)

        assert manager_condition_views(projection, 1)["gw_win_streak"].length == 0
        assert manager_condition_views(projection, 1)["gw_loss_streak"].length == 0

    def test_a_tie_for_the_week_is_the_same_regardless_of_cohort_order(self):
        """The predicates must not depend on the order rows are given in --
        unlike the ordinal `gw_rank` they used to compare, which broke ties
        on cohort order (issue #163)."""
        store = LeagueHistoryStore("2026-27", "classic", 1)
        store.append_rows(1, [
            make_history_row(gameweek=1, manager_key=2, gross_points=87),
            make_history_row(gameweek=1, manager_key=1, gross_points=87),
            make_history_row(gameweek=1, manager_key=3, gross_points=40),
        ])

        projection = rebuild_counters_through(store, 1)

        assert manager_condition_views(projection, 1)["gw_win_streak"].length == 1
        assert manager_condition_views(projection, 2)["gw_win_streak"].length == 1

    def test_tie_compares_net_of_transfer_cost_not_raw_points(self):
        store = LeagueHistoryStore("2026-27", "classic", 1)
        store.append_rows(1, [
            make_history_row(gameweek=1, manager_key=1, gross_points=87, transfer_cost=4),
            make_history_row(gameweek=1, manager_key=2, gross_points=83),
            make_history_row(gameweek=1, manager_key=3, gross_points=40),
        ])

        projection = rebuild_counters_through(store, 1)

        assert manager_condition_views(projection, 1)["gw_win_streak"].length == 1
        assert manager_condition_views(projection, 2)["gw_win_streak"].length == 1

    def test_unknown_manager_holds_but_last_place_is_still_correctly_identified(self):
        """Gameweek points are read over every cohort member, so a manager
        whose picks fetch failed does not hand the week's worst score to
        whoever actually finished second-last. Both gameweek-rank
        conditions hold for the failed-fetch manager; the cohort still
        includes their row when deciding who is genuinely last."""
        store = LeagueHistoryStore("2026-27", "classic", 1)
        for gw in (1, 2):
            store.append_rows(gw, [
                make_history_row(gameweek=gw, manager_key=1, gross_points=87),
                make_history_row(gameweek=gw, manager_key=2, gross_points=60),
                make_history_row(
                    gameweek=gw, manager_key=3, gross_points=50, capture_status="unknown",
                ),
                make_history_row(gameweek=gw, manager_key=4, gross_points=40),
            ])

        projection = rebuild_counters_through(store, 2)

        unknown_view = manager_condition_views(projection, 3)["gw_loss_streak"]
        assert unknown_view.length == 0
        assert unknown_view.held_in_run == 0  # hold before any run opens records nothing

        # Manager 2 finished second-last but must not be handed the week's
        # worst just because manager 3's fetch failed.
        second_last = manager_condition_views(projection, 2)["gw_loss_streak"]
        assert second_last.length == 0

        # Manager 4 is genuinely last, accounting for manager 3's points.
        genuinely_last = manager_condition_views(projection, 4)["gw_loss_streak"]
        assert genuinely_last.length == 2

        top = manager_condition_views(projection, 1)["gw_win_streak"]
        assert top.length == 2

    def test_missing_gross_points_holds(self):
        store = LeagueHistoryStore("2026-27", "classic", 1)
        store.append_rows(1, [make_history_row(gameweek=1, manager_key=1, gross_points=None)])
        projection = rebuild_counters_through(store, 1)
        view = manager_condition_views(projection, 1)["gw_win_streak"]
        assert view.length == 0
        assert view.held_in_run == 0


# ---------------------------------------------------------------------------
# weeks_on_top / bottom_half_run / green_arrow_drought
# ---------------------------------------------------------------------------


class TestPositionConditions:
    def test_weeks_on_top_extends_and_resets(self):
        store = LeagueHistoryStore("2026-27", "classic", 1)
        store.append_rows(1, [make_history_row(gameweek=1, manager_key=1, league_position=1)])
        store.append_rows(2, [make_history_row(gameweek=2, manager_key=1, league_position=1)])
        store.append_rows(3, [make_history_row(gameweek=3, manager_key=1, league_position=2)])

        projection = rebuild_counters_through(store, 3)
        view = manager_condition_views(projection, 1)["weeks_on_top"]

        assert view.length == 0  # reset by GW3

    def test_run_shorter_than_minimum_is_tracked_but_not_reportable(self):
        store = LeagueHistoryStore("2026-27", "classic", 1)
        store.append_rows(1, [make_history_row(gameweek=1, manager_key=1, league_position=1)])

        projection = rebuild_counters_through(store, 1)
        view = manager_condition_views(projection, 1)["weeks_on_top"]  # min_run == 2

        assert view.length == 1
        assert view.is_reportable is False

    def test_even_cohort_the_exact_half_boundary_is_not_bottom_half(self):
        """4 members: ceil(4/2) == 2. Position 2 is top half; position 3 is bottom half."""
        store = LeagueHistoryStore("2026-27", "classic", 1)
        store.append_rows(1, [
            make_history_row(gameweek=1, manager_key=key, league_position=key) for key in (1, 2, 3, 4)
        ])

        projection = rebuild_counters_through(store, 1)

        assert manager_condition_views(projection, 2)["bottom_half_run"].length == 0
        assert manager_condition_views(projection, 3)["bottom_half_run"].length == 1

    def test_odd_cohort_rounds_the_half_boundary_up(self):
        """5 members: ceil(5/2) == 3. The exact middle (position 3) is top half."""
        store = LeagueHistoryStore("2026-27", "classic", 1)
        store.append_rows(1, [
            make_history_row(gameweek=1, manager_key=key, league_position=key)
            for key in (1, 2, 3, 4, 5)
        ])

        projection = rebuild_counters_through(store, 1)

        assert manager_condition_views(projection, 3)["bottom_half_run"].length == 0
        assert manager_condition_views(projection, 4)["bottom_half_run"].length == 1

    def test_green_arrow_drought_extends_when_position_does_not_improve(self):
        store = LeagueHistoryStore("2026-27", "classic", 1)
        store.append_rows(1, [make_history_row(gameweek=1, manager_key=1, league_position=5)])
        store.append_rows(2, [make_history_row(gameweek=2, manager_key=1, league_position=5)])
        store.append_rows(3, [make_history_row(gameweek=3, manager_key=1, league_position=7)])

        projection = rebuild_counters_through(store, 3)
        view = manager_condition_views(projection, 1)["green_arrow_drought"]

        assert view.length == 2  # GW1 has no previous row -> hold; GW2, GW3 extend
        assert view.start_gameweek == 2

    def test_green_arrow_drought_resets_when_position_improves(self):
        store = LeagueHistoryStore("2026-27", "classic", 1)
        store.append_rows(1, [make_history_row(gameweek=1, manager_key=1, league_position=5)])
        store.append_rows(2, [make_history_row(gameweek=2, manager_key=1, league_position=5)])
        store.append_rows(3, [make_history_row(gameweek=3, manager_key=1, league_position=2)])

        projection = rebuild_counters_through(store, 3)
        view = manager_condition_views(projection, 1)["green_arrow_drought"]

        assert view.length == 0

    def test_green_arrow_drought_holds_while_top_of_the_table(self):
        """First place has nowhere to climb, so a gameweek that began there
        could not have produced a green arrow however well it went. That is
        a structural impossibility rather than a failure to improve, and
        counting it would score the league leader as the league's worst
        offender."""
        store = LeagueHistoryStore("2026-27", "classic", 1)
        for gw in (1, 2, 3, 4):
            store.append_rows(gw, [make_history_row(gameweek=gw, manager_key=1, league_position=1)])

        view = manager_condition_views(rebuild_counters_through(store, 4), 1)["green_arrow_drought"]

        assert view.length == 0
        assert view.occurrences == 0
        # Held, not silently ignored: the gameweeks are stated as unjudged
        # rather than counted as clean.
        assert view.held_total == 4

    def test_climbing_to_first_still_breaks_a_drought(self):
        """Gated on where the gameweek *began*, not where it ended: gating
        on this gameweek's position would suppress the biggest green arrow
        there is and leave the run open."""
        store = LeagueHistoryStore("2026-27", "classic", 1)
        for gw, position in ((1, 4), (2, 4), (3, 4), (4, 1)):
            store.append_rows(gw, [make_history_row(
                gameweek=gw, manager_key=1, league_position=position,
            )])

        third = manager_condition_views(rebuild_counters_through(store, 3), 1)["green_arrow_drought"]
        fourth = manager_condition_views(rebuild_counters_through(store, 4), 1)["green_arrow_drought"]

        assert third.length == 2  # GW2 and GW3 failed to improve on 4th
        assert fourth.length == 0  # climbing to the summit resets it

    def test_falling_off_the_top_holds_rather_than_extending(self):
        """They had nowhere to climb from either, so the gameweek is still
        unjudgeable for this condition -- and the drop is already told by
        standings movement and by `weeks_on_top` resetting."""
        store = LeagueHistoryStore("2026-27", "classic", 1)
        for gw, position in ((1, 1), (2, 1), (3, 5)):
            store.append_rows(gw, [make_history_row(
                gameweek=gw, manager_key=1, league_position=position,
            )])

        view = manager_condition_views(rebuild_counters_through(store, 3), 1)["green_arrow_drought"]

        assert view.occurrences == 0
        assert view.held_total == 3

    def test_a_drought_resumes_normally_once_off_the_top(self):
        store = LeagueHistoryStore("2026-27", "classic", 1)
        for gw, position in ((1, 1), (2, 1), (3, 5), (4, 5), (5, 6)):
            store.append_rows(gw, [make_history_row(
                gameweek=gw, manager_key=1, league_position=position,
            )])

        view = manager_condition_views(rebuild_counters_through(store, 5), 1)["green_arrow_drought"]

        # GW4 and GW5 both began outside the top and failed to improve.
        assert view.length == 2
        assert view.start_gameweek == 4

    def test_green_arrow_drought_holds_on_the_first_ever_gameweek(self):
        store = LeagueHistoryStore("2026-27", "classic", 1)
        store.append_rows(1, [make_history_row(gameweek=1, manager_key=1, league_position=5)])

        projection = rebuild_counters_through(store, 1)
        view = manager_condition_views(projection, 1)["green_arrow_drought"]

        assert view.length == 0
        assert view.held_in_run == 0

    def test_green_arrow_drought_holds_when_the_previous_row_is_unknown(self):
        store = LeagueHistoryStore("2026-27", "classic", 1)
        store.append_rows(1, [make_history_row(gameweek=1, manager_key=1, league_position=5)])
        store.append_rows(2, [make_history_row(gameweek=2, manager_key=1, capture_status="unknown")])
        store.append_rows(3, [make_history_row(gameweek=3, manager_key=1, league_position=5)])

        projection = rebuild_counters_through(store, 3)
        view = manager_condition_views(projection, 1)["green_arrow_drought"]

        # GW1: no previous row -> hold. GW2: unknown row -> R19 holds its own
        # streak. GW3: previous row (GW2) is unknown -> its position cannot be
        # trusted for the comparison either, so this also holds.
        assert view.length == 0
        assert view.held_in_run == 0


# ---------------------------------------------------------------------------
# Independence between conditions
# ---------------------------------------------------------------------------


class TestConditionsAreIndependent:
    def test_top_of_table_with_a_gw_loss_streak_and_winning_gws_from_bottom_half(self):
        """A manager can hold a gameweek-loss run while sitting top of the
        league table, and a bottom-half run while winning a gameweek -- the
        two pairs must not interfere with each other."""
        store = LeagueHistoryStore("2026-27", "classic", 1)
        for gw in (1, 2):
            store.append_rows(gw, [
                make_history_row(gameweek=gw, manager_key=1, league_position=1, gross_points=40),
                make_history_row(gameweek=gw, manager_key=2, league_position=5, gross_points=87),
                make_history_row(gameweek=gw, manager_key=3, league_position=3, gross_points=60),
            ])

        projection = rebuild_counters_through(store, 2)
        top_manager = manager_condition_views(projection, 1)
        bottom_manager = manager_condition_views(projection, 2)

        assert top_manager["weeks_on_top"].length == 2
        assert top_manager["gw_loss_streak"].length == 2

        assert bottom_manager["gw_win_streak"].length == 2
        assert bottom_manager["bottom_half_run"].length == 2


# ---------------------------------------------------------------------------
# hit_run (classic only)
# ---------------------------------------------------------------------------


class TestHitRun:
    def test_extends_while_positive_resets_at_zero(self):
        store = LeagueHistoryStore("2026-27", "classic", 1)
        store.append_rows(1, [make_history_row(gameweek=1, manager_key=1, transfer_cost=4)])
        store.append_rows(2, [make_history_row(gameweek=2, manager_key=1, transfer_cost=4)])
        store.append_rows(3, [make_history_row(gameweek=3, manager_key=1, transfer_cost=0)])

        projection = rebuild_counters_through(store, 3)
        view = manager_condition_views(projection, 1)["hit_run"]

        assert view.length == 0

    def test_missing_transfer_cost_holds(self):
        store = LeagueHistoryStore("2026-27", "classic", 1)
        store.append_rows(1, [make_history_row(gameweek=1, manager_key=1, transfer_cost=4)])
        store.append_rows(2, [make_history_row(gameweek=2, manager_key=1, transfer_cost=None)])
        store.append_rows(3, [make_history_row(gameweek=3, manager_key=1, transfer_cost=4)])

        projection = rebuild_counters_through(store, 3)
        view = manager_condition_views(projection, 1)["hit_run"]

        assert view.length == 2
        assert view.held_in_run == 1


# ---------------------------------------------------------------------------
# waiver_win_run / waiver_burn_run (draft only)
# ---------------------------------------------------------------------------


class TestWaiverConditions:
    def test_no_transactions_holds_both_conditions(self):
        store = LeagueHistoryStore("2026-27", "draft", 1)
        store.append_rows(1, [
            make_history_row(gameweek=1, manager_key=1, fpl_format="draft", transactions=[]),
        ])

        projection = rebuild_counters_through(store, 1)
        views = manager_condition_views(projection, 1)

        assert views["waiver_win_run"].length == 0
        assert views["waiver_win_run"].held_in_run == 0
        assert views["waiver_burn_run"].length == 0
        assert views["waiver_burn_run"].held_in_run == 0

    def test_net_is_summed_across_multiple_transactions_in_one_gameweek(self):
        store = LeagueHistoryStore("2026-27", "draft", 1)
        store.append_rows(1, [make_history_row(
            gameweek=1, manager_key=1, fpl_format="draft",
            transactions=[_transaction(5), _transaction(-2)],  # net +3
        )])
        store.append_rows(2, [make_history_row(
            gameweek=2, manager_key=1, fpl_format="draft",
            transactions=[_transaction(-5), _transaction(1)],  # net -4
        )])

        projection = rebuild_counters_through(store, 2)
        views = manager_condition_views(projection, 1)

        assert views["waiver_win_run"].length == 0  # GW1 extended then GW2 reset
        assert views["waiver_burn_run"].length == 1  # GW2 extended fresh


# ---------------------------------------------------------------------------
# Weekly-path advance vs. rebuild (KTD10)
# ---------------------------------------------------------------------------


class TestAdvanceAndRebuild:
    def test_advances_incrementally_when_the_gameweek_is_stamp_plus_one(self):
        store = LeagueHistoryStore("2026-27", "classic", 1)
        store.append_rows(1, [make_history_row(gameweek=1, manager_key=1, league_position=1)])
        first = compute_counters_through(store, 1)
        assert first.computed_through_gameweek == 1

        store.append_rows(2, [make_history_row(gameweek=2, manager_key=1, league_position=1)])
        second = compute_counters_through(store, 2)

        assert second.computed_through_gameweek == 2
        assert manager_condition_views(second, 1)["weeks_on_top"].length == 2

    def test_fast_path_never_calls_the_rebuild_when_stamp_matches(self, monkeypatch):
        from fpl_cli.services import league_history_counters as svc

        store = LeagueHistoryStore("2026-27", "classic", 1)
        store.append_rows(1, [make_history_row(gameweek=1, manager_key=1, league_position=1)])
        svc.compute_counters_through(store, 1)
        store.append_rows(2, [make_history_row(gameweek=2, manager_key=1, league_position=1)])

        def _boom(*_args, **_kwargs):
            raise AssertionError("rebuild_counters_through must not run on the fast path")

        monkeypatch.setattr(svc, "rebuild_counters_through", _boom)

        projection = svc.compute_counters_through(store, 2)
        assert projection.computed_through_gameweek == 2

    def test_exact_match_returns_the_cached_projection_without_recomputing(self, monkeypatch):
        store = LeagueHistoryStore("2026-27", "classic", 1)
        store.append_rows(1, [make_history_row(gameweek=1, manager_key=1, league_position=1)])
        store.append_rows(2, [make_history_row(gameweek=2, manager_key=1, league_position=1)])
        first = compute_counters_through(store, 2)
        assert manager_condition_views(first, 1)["weeks_on_top"].length == 2

        def _boom(*_args, **_kwargs):
            raise AssertionError("a re-run for an already-cached gameweek must not re-read any rows")

        monkeypatch.setattr(store, "resolved_gameweek", _boom)

        # Re-running for the same, already-stamped gameweek -- e.g. checking
        # console output, then re-running with --summarise -- must be free.
        second = compute_counters_through(store, 2)
        assert second.computed_through_gameweek == 2
        assert second.runs == first.runs

    def test_multi_gameweek_catchup_over_a_stale_stamp_rebuilds_and_matches_a_full_rebuild(self):
        store = LeagueHistoryStore("2026-27", "classic", 1)
        for gw in range(1, 7):
            store.append_rows(gw, [make_history_row(gameweek=gw, manager_key=1, league_position=1)])

        stamped_at_three = compute_counters_through(store, 3)
        assert stamped_at_three.computed_through_gameweek == 3

        jumped = compute_counters_through(store, 6)  # stamp(3) != 6 - 1 -> must rebuild
        expected = rebuild_counters_through(store, 6)

        assert jumped.computed_through_gameweek == 6
        assert jumped.runs == expected.runs
        # A buggy "just bump the stamp" implementation would leave this at 3.
        assert manager_condition_views(jumped, 1)["weeks_on_top"].length == 6

    def test_a_repair_at_the_stamp_is_not_served_stale_when_invalidated_first(self):
        """The exact-match shortcut (stamp == target) trusts `existing.runs`
        exactly as much as the stamp+1 fast path does -- neither notices a
        repair on its own (see `compute_counters_through`'s docstring). A
        caller that repairs a gameweek at or before the stamp must call
        `invalidate_if_repaired` before asking again, the same pattern
        `test_an_unknown_gameweek_repaired_behind_a_matching_stamp_is_not_folded_onto_stale_state`
        exercises for the stamp+1 case."""
        store = LeagueHistoryStore("2026-27", "classic", 1)
        for gw in (1, 2, 3):
            store.append_rows(gw, [make_history_row(gameweek=gw, manager_key=1, league_position=1)])
        first = compute_counters_through(store, 3)
        assert manager_condition_views(first, 1)["weeks_on_top"].length == 3

        # Repair GW2: this manager was actually mid-table that gameweek.
        store.append_rows(2, [
            make_history_row(gameweek=2, manager_key=1, league_position=5, captured_at=LATER),
        ])

        # Same target as the existing stamp -- not stamp+1 -- so a caller
        # that repairs and re-asks must invalidate first, exactly as it
        # would for a stamp+1 repair.
        invalidate_if_repaired(store, {2})
        repaired = compute_counters_through(store, 3)
        view = manager_condition_views(repaired, 1)["weeks_on_top"]

        assert view.length == 1
        assert view.start_gameweek == 3

    def test_an_exact_match_without_invalidation_returns_the_stale_cached_projection(self):
        """Documents the other half of the contract above: skip
        `invalidate_if_repaired` and the exact-match shortcut returns
        `existing` as-is, repair or not -- the shortcut itself has no way to
        notice. This is the tradeoff the shortcut makes for a free re-run of
        an already-processed gameweek; every actual caller in this codebase
        (`capture_recap_history`) invalidates first, so this never happens
        in production."""
        store = LeagueHistoryStore("2026-27", "classic", 1)
        for gw in (1, 2, 3):
            store.append_rows(gw, [make_history_row(gameweek=gw, manager_key=1, league_position=1)])
        compute_counters_through(store, 3)

        store.append_rows(2, [
            make_history_row(gameweek=2, manager_key=1, league_position=5, captured_at=LATER),
        ])

        stale = compute_counters_through(store, 3)  # no invalidate_if_repaired call
        view = manager_condition_views(stale, 1)["weeks_on_top"]
        assert view.length == 3  # the pre-repair count, not the corrected 1

    def test_an_unknown_gameweek_repaired_behind_a_matching_stamp_is_not_folded_onto_stale_state(self):
        """Reproduces the fast-path bug `invalidate_if_repaired` exists to close:
        the counters cache advances through GW1 while its row is still unknown,
        GW1 is then repaired -- the automatic, unconditional part of
        `_backfill` in `fpl_cli/cli/_league_recap_history.py` -- and only then
        is GW2 captured. `through_gameweek(2) == stamp(1) + 1`, so without
        invalidating first the fast path would fold GW2 onto the run state
        `existing.runs` still carries from before the repair: length=1,
        start_gameweek=2, undercounting the run by the gameweek it was
        wrongly denied."""
        store = LeagueHistoryStore("2026-27", "classic", 1)

        # (1) GW1 captured unknown -- e.g. this manager's picks fetch failed.
        store.append_rows(1, [make_history_row(gameweek=1, manager_key=1, capture_status="unknown")])

        # (2) The counters cache advances through GW1 while it is still
        # unknown: weeks_on_top holds (R19), so no run has opened.
        stamped = compute_counters_through(store, 1)
        assert stamped.computed_through_gameweek == 1
        assert manager_condition_views(stamped, 1)["weeks_on_top"].length == 0

        # (3) GW1 is superseded with a real row -- the automatic backfill
        # repair, unconditional and independent of any explicit rebuild.
        store.append_rows(1, [
            make_history_row(gameweek=1, manager_key=1, league_position=1, captured_at=LATER),
        ])

        # (4) GW2 is captured, and the fix path runs: invalidate first, then
        # advance.
        store.append_rows(2, [make_history_row(gameweek=2, manager_key=1, league_position=1)])
        invalidate_if_repaired(store, {1})
        fixed = compute_counters_through(store, 2)

        # (5) Matches a full rebuild's ground truth: both gameweeks extend the
        # run, which started at GW1 now that GW1 is known to qualify -- not
        # the stale fast path's length=1/start=2.
        ground_truth = rebuild_counters_through(store, 2)
        assert fixed.runs == ground_truth.runs
        view = manager_condition_views(fixed, 1)["weeks_on_top"]
        assert (view.length, view.start_gameweek) == (2, 1)

    def test_invalidate_if_repaired_is_a_no_op_when_every_repair_is_ahead_of_the_stamp(self):
        """A repaired gameweek strictly after the cache's own stamp needs no
        forced invalidation: `compute_counters_through` already rebuilds on
        its own for any target beyond stamp+1 (KTD10), so the cache file is
        left alone rather than discarded for nothing."""
        store = LeagueHistoryStore("2026-27", "classic", 1)
        store.append_rows(1, [make_history_row(gameweek=1, manager_key=1, league_position=1)])
        compute_counters_through(store, 1)  # stamp == 1

        invalidate_if_repaired(store, {2})  # 2 > stamp(1) -- nothing to force

        assert counters_file("2026-27", "classic", 1).is_file()

    def test_invalidate_if_repaired_is_a_no_op_with_no_cache_yet(self):
        """Nothing to invalidate before `compute_counters_through` has ever
        run for this partition -- must not raise trying to read or delete a
        file that was never written."""
        store = LeagueHistoryStore("2026-27", "classic", 1)
        store.append_rows(1, [make_history_row(gameweek=1, manager_key=1, league_position=1)])

        invalidate_if_repaired(store, {1})  # must not raise

        assert not counters_file("2026-27", "classic", 1).exists()

    def test_a_corrupt_gameweek_behind_a_matching_stamp_falls_through_to_a_full_rebuild(self):
        """The fast path's own `store.resolved_gameweek` reads can fail even
        when the cached stamp matches `through_gameweek - 1` -- a ledger file
        corrupted after the cache was written. Every other test in this class
        forces the rebuild fallback through a different door (a stale stamp,
        a version mismatch, a missing cache file); this is the one that
        corrupts a gameweek file while a matching, otherwise-valid cache is
        already on disk."""
        store = LeagueHistoryStore("2026-27", "classic", 1)
        store.append_rows(1, [make_history_row(gameweek=1, manager_key=1, league_position=1)])
        compute_counters_through(store, 1)  # stamp == 1, matches through_gameweek(2) - 1

        store.gameweek_file(2).write_text("not json{{{\n", encoding="utf-8")

        projection = compute_counters_through(store, 2)  # must not raise

        assert projection.runs == rebuild_counters_through(store, 2).runs

    def test_version_mismatched_projection_is_rebuilt_rather_than_served(self):
        import json

        from fpl_cli.services import league_history_counters as svc

        store = LeagueHistoryStore("2026-27", "classic", 1)
        store.append_rows(1, [make_history_row(gameweek=1, manager_key=1, league_position=1)])
        compute_counters_through(store, 1)

        path = counters_file("2026-27", "classic", 1)
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["version"] = 999
        path.write_text(json.dumps(payload), encoding="utf-8")

        assert svc._load_projection(store) is None  # exercising the fail-open path directly

        store.append_rows(2, [make_history_row(gameweek=2, manager_key=1, league_position=1)])
        projection = compute_counters_through(store, 2)

        assert manager_condition_views(projection, 1)["weeks_on_top"].length == 2

    def test_missing_projection_file_rebuilds_silently(self):
        store = LeagueHistoryStore("2026-27", "classic", 1)
        store.append_rows(1, [make_history_row(gameweek=1, manager_key=1, league_position=1)])

        projection = compute_counters_through(store, 1)  # no prior cache exists at all

        assert projection.computed_through_gameweek == 1
        assert manager_condition_views(projection, 1)["weeks_on_top"].length == 1

    def test_unreadable_projection_file_rebuilds_silently(self):
        store = LeagueHistoryStore("2026-27", "classic", 1)
        store.append_rows(1, [make_history_row(gameweek=1, manager_key=1, league_position=1)])
        compute_counters_through(store, 1)

        path = counters_file("2026-27", "classic", 1)
        path.write_text("not json{{{", encoding="utf-8")

        store.append_rows(2, [make_history_row(gameweek=2, manager_key=1, league_position=1)])
        projection = compute_counters_through(store, 2)  # must not raise

        assert manager_condition_views(projection, 1)["weeks_on_top"].length == 2

    def test_unreadable_gameweek_is_skipped_during_rebuild_not_fatal(self):
        store = LeagueHistoryStore("2026-27", "classic", 1)
        store.append_rows(1, [make_history_row(gameweek=1, manager_key=1, league_position=1)])
        store.gameweek_file(2).write_text("not json{{{\n", encoding="utf-8")
        store.append_rows(3, [make_history_row(gameweek=3, manager_key=1, league_position=1)])

        projection = rebuild_counters_through(store, 3)  # must not raise
        view = manager_condition_views(projection, 1)["weeks_on_top"]

        assert view.length == 2
        assert view.start_gameweek == 1
        assert view.held_in_run == 0  # a corrupt gameweek is a gap, not a hold


# ---------------------------------------------------------------------------
# Partitioning: season and format
# ---------------------------------------------------------------------------


class TestPartitioning:
    def test_counters_do_not_carry_across_a_season_boundary(self):
        old_store = LeagueHistoryStore("2026-27", "classic", 1)
        for gw in (1, 2, 3):
            old_store.append_rows(gw, [
                make_history_row(season="2026-27", gameweek=gw, manager_key=1, league_position=1),
            ])
        old_projection = compute_counters_through(old_store, 3)
        assert manager_condition_views(old_projection, 1)["weeks_on_top"].length == 3

        new_store = LeagueHistoryStore("2027-28", "classic", 1)
        new_store.append_rows(1, [
            make_history_row(season="2027-28", gameweek=1, manager_key=1, league_position=1),
        ])
        new_projection = compute_counters_through(new_store, 1)

        # If counters leaked across the boundary this would read 4, not 1.
        assert manager_condition_views(new_projection, 1)["weeks_on_top"].length == 1

    def test_classic_only_conditions_are_absent_from_a_draft_partition(self):
        store = LeagueHistoryStore("2026-27", "draft", 1)
        store.append_rows(1, [
            make_history_row(gameweek=1, manager_key=1, fpl_format="draft", league_position=1),
        ])
        views = manager_condition_views(compute_counters_through(store, 1), 1)

        assert "captain_blank_run" not in views
        assert "hit_run" not in views
        assert "waiver_win_run" in views
        assert "waiver_burn_run" in views

    def test_draft_only_conditions_are_absent_from_a_classic_partition(self):
        store = LeagueHistoryStore("2026-27", "classic", 1)
        store.append_rows(1, [make_history_row(gameweek=1, manager_key=1, league_position=1)])
        views = manager_condition_views(compute_counters_through(store, 1), 1)

        assert "waiver_win_run" not in views
        assert "waiver_burn_run" not in views
        assert "captain_blank_run" in views
        assert "hit_run" in views


# ---------------------------------------------------------------------------
# Persisted models
# ---------------------------------------------------------------------------


class TestCountersModels:
    def test_default_projection_is_stamped_with_the_current_version(self):
        projection = LeagueHistoryCountersProjection(
            season="2026-27", fpl_format="classic", league_id=1, computed_through_gameweek=0,
        )
        assert projection.version == LEAGUE_HISTORY_COUNTERS_VERSION
        assert projection.runs == {}

    def test_round_trips_through_json_with_nested_runs(self):
        projection = LeagueHistoryCountersProjection(
            season="2026-27", fpl_format="classic", league_id=1, computed_through_gameweek=5,
            runs={
                1: {"weeks_on_top": ConditionRunState(length=3, start_gameweek=3, held_in_run=1)},
                2: {"hit_run": ConditionRunState()},
            },
        )
        restored = LeagueHistoryCountersProjection.model_validate_json(projection.model_dump_json())
        assert restored == projection
        assert isinstance(next(iter(restored.runs)), int)

    def test_unknown_extra_field_on_the_projection_is_rejected(self):
        payload = LeagueHistoryCountersProjection(
            season="2026-27", fpl_format="classic", league_id=1, computed_through_gameweek=0,
        ).model_dump(mode="json")
        payload["storyline_theme"] = "a field from a future version"
        with pytest.raises(ValidationError):
            LeagueHistoryCountersProjection.model_validate(payload)

    def test_unknown_extra_field_on_the_run_state_is_rejected(self):
        payload = ConditionRunState().model_dump(mode="json")
        payload["narrative_flavour"] = "a field from a future version"
        with pytest.raises(ValidationError):
            ConditionRunState.model_validate(payload)


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------


class TestCountersPaths:
    def test_honours_the_data_dir_override_at_point_of_use(self, tmp_path, monkeypatch):
        from fpl_cli.paths import user_data_dir

        redirected = tmp_path / "elsewhere"
        user_data_dir.cache_clear()
        monkeypatch.setenv("FPL_CLI_DATA_DIR", str(redirected))
        try:
            path = counters_file("2026-27", "classic", 1)
        finally:
            user_data_dir.cache_clear()
        assert redirected in path.parents

    def test_partition_dir_is_separate_from_the_ledgers_own_directory(self):
        store = LeagueHistoryStore("2026-27", "classic", 1)
        assert store.partition_dir() != counters_partition_dir("2026-27", "classic", 1)


# ---------------------------------------------------------------------------
# Public read views
# ---------------------------------------------------------------------------


class TestPublicViews:
    def test_all_condition_views_covers_every_manager_touched(self):
        store = LeagueHistoryStore("2026-27", "classic", 1)
        store.append_rows(1, [
            make_history_row(gameweek=1, manager_key=1, league_position=1),
            make_history_row(gameweek=1, manager_key=2, league_position=2),
        ])

        projection = rebuild_counters_through(store, 1)
        views = all_condition_views(projection)

        assert sorted(views) == [1, 2]
        assert "weeks_on_top" in views[1]
        assert "weeks_on_top" in views[2]

    def test_manager_condition_views_returns_a_fresh_entry_for_an_untouched_condition(self):
        store = LeagueHistoryStore("2026-27", "classic", 1)
        store.append_rows(1, [make_history_row(gameweek=1, manager_key=1, league_position=1)])

        projection = rebuild_counters_through(store, 1)
        views = manager_condition_views(projection, 1)

        # hit_run never extended for this manager -- still present, at zero.
        assert views["hit_run"].length == 0
        assert views["hit_run"].start_gameweek is None
        assert views["hit_run"].is_reportable is False

    def test_manager_condition_views_for_an_unseen_manager_returns_all_fresh_entries(self):
        store = LeagueHistoryStore("2026-27", "classic", 1)
        store.append_rows(1, [make_history_row(gameweek=1, manager_key=1, league_position=1)])

        projection = rebuild_counters_through(store, 1)
        views = manager_condition_views(projection, 999)

        assert len(views) == len(conditions_for_format("classic"))
        assert all(view.length == 0 for view in views.values())

    def test_excess_measures_how_far_a_run_has_gone_past_its_minimum(self):
        """R12: surfaced entries are ranked by how far each run exceeds its
        condition's minimum. `excess` is the raw signal a caller ranks by;
        it is negative (not yet reportable) or zero/positive (reportable)."""
        store = LeagueHistoryStore("2026-27", "classic", 1)
        for gw in (1, 2, 3, 4, 5):
            store.append_rows(gw, [make_history_row(gameweek=gw, manager_key=1, league_position=1)])

        projection = rebuild_counters_through(store, 5)
        view = manager_condition_views(projection, 1)["weeks_on_top"]  # min_run == 2

        assert view.length == 5
        assert view.excess == 3
        assert view.is_reportable is True

        below_minimum = manager_condition_views(rebuild_counters_through(store, 1), 1)["weeks_on_top"]
        assert below_minimum.excess == -1
        assert below_minimum.is_reportable is False


# ---------------------------------------------------------------------------
# Count surface policies (issue #164)
# ---------------------------------------------------------------------------


def _view(occurrences: int = 1, length: int = 1) -> ConditionRunView:
    """A view carrying only what `CountSurfacePolicy` reads."""
    return ConditionRunView(
        condition_key="k", label="L", length=length, start_gameweek=1, held_in_run=0,
        min_run=2, occurrences=occurrences, held_total=0, last_occurrence_gameweek=1,
        first_evaluated_gameweek=1, count_label_one="thing", count_label_many="things",
        count_policy=CountSurfacePolicy(),
    )


class TestCountSurfacePolicy:
    def test_step_fires_only_on_a_multiple(self):
        policy = CountSurfacePolicy(step=3)
        fired = [
            n for n in range(1, 10)
            if policy.qualifies(_view(occurrences=n), second_half=False)
        ]
        assert fired == [3, 6, 9]

    def test_run_milestones_fire_on_the_open_run_not_the_season_total(self):
        """A drought only reads as a story unbroken: the run length is what
        fires it, and a manager whose season total is high but whose
        current run is short says nothing."""
        policy = CountSurfacePolicy(run_milestones=frozenset({5, 10}))
        assert policy.qualifies(_view(occurrences=5, length=5), second_half=False) is True
        assert policy.qualifies(_view(occurrences=20, length=3), second_half=False) is False
        # Capped deliberately: a manager stuck at the top or bottom of the
        # table must not re-fire forever on a fact the table already shows.
        assert policy.qualifies(_view(occurrences=15, length=15), second_half=False) is False

    def test_a_first_occurrence_fires_only_in_the_second_half(self):
        policy = CountSurfacePolicy(step=3, first_in_second_half=True)
        assert policy.qualifies(_view(occurrences=1), second_half=True) is True
        assert policy.qualifies(_view(occurrences=1), second_half=False) is False
        # The step still applies in both halves.
        assert policy.qualifies(_view(occurrences=3), second_half=False) is True

    def test_second_half_only_silences_the_whole_condition_early(self):
        policy = CountSurfacePolicy(step=10, second_half_only=True)
        assert policy.qualifies(_view(occurrences=10), second_half=False) is False
        assert policy.qualifies(_view(occurrences=10), second_half=True) is True

    def test_ride_along_needs_the_floor_and_is_off_by_default(self):
        no_ride = CountSurfacePolicy(step=3)
        assert no_ride.rides_along(_view(occurrences=9), fired_totals=[9]) is False
        policy = CountSurfacePolicy(step=5, ride_along_min=3)
        assert policy.rides_along(_view(occurrences=3), fired_totals=[5]) is True
        assert policy.rides_along(_view(occurrences=2), fired_totals=[5]) is False

    def test_a_relative_window_measures_against_the_nearest_firing_total(self):
        """A count that climbs all season outgrows any fixed floor -- by
        midseason every peer has passed it -- so the window is measured
        against the milestone itself, and against the *nearest* one when
        two managers fired at once."""
        policy = CountSurfacePolicy(step=10, ride_along_within=5)
        assert policy.rides_along(_view(occurrences=26), fired_totals=[30]) is True
        assert policy.rides_along(_view(occurrences=24), fired_totals=[30]) is False
        # Nearest, not largest: 24 is far from 30 but level with 20.
        assert policy.rides_along(_view(occurrences=24), fired_totals=[20, 30]) is True

    def test_a_relative_window_with_no_firing_totals_carries_nobody(self):
        policy = CountSurfacePolicy(step=10, ride_along_within=5)
        assert policy.rides_along(_view(occurrences=10), fired_totals=[]) is False


# ---------------------------------------------------------------------------
# Season occurrence counts (issue #164)
# ---------------------------------------------------------------------------


class TestSeasonOccurrenceCounts:
    """Every extending gameweek counts once for the season, across resets;
    holds are tallied separately as the count's coverage qualifier."""

    def test_occurrences_accumulate_across_resets(self):
        """The issue's own repro shape: a manager whose condition fired in
        GW2, GW7, GW11 and GW19 shows a season count of 4 after GW19, even
        though the currently-open run is back to 1."""
        store = LeagueHistoryStore("2026-27", "classic", 1)
        top_gameweeks = {2, 7, 11, 19}
        for gw in range(1, 20):
            store.append_rows(gw, [make_history_row(
                gameweek=gw, manager_key=1,
                league_position=1 if gw in top_gameweeks else 2,
            )])

        view = manager_condition_views(rebuild_counters_through(store, 19), 1)["weeks_on_top"]

        assert view.occurrences == 4
        assert view.length == 1
        assert view.last_occurrence_gameweek == 19
        assert view.first_evaluated_gameweek == 1

    def test_a_reset_wipes_the_run_but_not_the_season_fields(self):
        store = LeagueHistoryStore("2026-27", "classic", 1)
        store.append_rows(1, [make_history_row(gameweek=1, manager_key=1, league_position=1)])
        store.append_rows(2, [make_history_row(gameweek=2, manager_key=1, league_position=2)])

        view = manager_condition_views(rebuild_counters_through(store, 2), 1)["weeks_on_top"]

        assert view.length == 0
        assert view.start_gameweek is None
        assert view.held_in_run == 0
        assert view.occurrences == 1
        assert view.last_occurrence_gameweek == 1
        assert view.first_evaluated_gameweek == 1

    def test_a_hold_is_tallied_but_never_counted(self):
        """R19 applied to the season count: an unknown gameweek neither
        advances the count nor acquits it -- it lands in `held_total`, the
        "not judged" figure a consumer must state beside the number."""
        store = LeagueHistoryStore("2026-27", "classic", 1)
        store.append_rows(1, [make_history_row(gameweek=1, manager_key=1, league_position=1)])
        store.append_rows(2, [make_history_row(gameweek=2, manager_key=1, capture_status="unknown")])
        store.append_rows(3, [make_history_row(gameweek=3, manager_key=1, league_position=1)])

        view = manager_condition_views(rebuild_counters_through(store, 3), 1)["weeks_on_top"]

        assert view.occurrences == 2
        assert view.held_total == 1
        assert view.length == 2
        assert view.held_in_run == 1
        assert view.last_occurrence_gameweek == 3

    def test_a_hold_before_any_run_opens_still_counts_toward_held_total(self):
        """A run has nothing to annotate before it opens, but the season
        count's coverage does: GW1 was not judged whether or not a run ever
        follows it."""
        store = LeagueHistoryStore("2026-27", "classic", 1)
        store.append_rows(1, [make_history_row(
            gameweek=1, manager_key=1, capture_status="unknown",
        )])

        view = manager_condition_views(rebuild_counters_through(store, 1), 1)["weeks_on_top"]

        assert view.occurrences == 0
        assert view.held_total == 1
        assert view.length == 0
        assert view.held_in_run == 0
        assert view.first_evaluated_gameweek == 1

    def test_first_evaluated_gameweek_is_the_managers_own_first_row(self):
        """A mid-season joiner's count spans the gameweeks it was actually
        folded over, not the partition's -- the honest window for their
        season total (R17's per-manager coverage, applied here)."""
        store = LeagueHistoryStore("2026-27", "classic", 1)
        store.append_rows(1, [make_history_row(gameweek=1, manager_key=1, league_position=1)])
        for gw in (3, 4):
            store.append_rows(gw, [
                make_history_row(gameweek=gw, manager_key=1, league_position=1),
                make_history_row(
                    gameweek=gw, manager_key=2, manager_name="Bob", league_position=2,
                ),
            ])

        views = manager_condition_views(rebuild_counters_through(store, 4), 2)

        assert views["weeks_on_top"].first_evaluated_gameweek == 3
        # A gameweek the manager is wholly absent from is neither judged nor
        # held for them -- GW1 and the never-captured GW2 land in neither
        # counter.
        assert views["weeks_on_top"].held_total == 0

    def test_incremental_advance_agrees_with_a_full_rebuild_on_season_fields(self):
        """The weekly fast path folds one gameweek onto persisted state; the
        season fields must come out identical to a from-scratch rebuild, or
        a cache hit would change a manager's season total."""
        store = LeagueHistoryStore("2026-27", "classic", 1)
        for gw, position in ((1, 1), (2, 2), (3, 1)):
            store.append_rows(gw, [make_history_row(
                gameweek=gw, manager_key=1, league_position=position,
            )])

        for gw in (1, 2, 3):
            advanced = compute_counters_through(store, gw)

        assert advanced.runs == rebuild_counters_through(store, 3).runs
        view = manager_condition_views(advanced, 1)["weeks_on_top"]
        assert view.occurrences == 2
        assert view.length == 1


# ---------------------------------------------------------------------------
# Multi-iteration loop coverage (repo-wide Definition of Done)
# ---------------------------------------------------------------------------


class TestMultiIterationLoops:
    """Every loop `_fold_gameweek` and `rebuild_counters_through` write is
    exercised here with at least two iterations at once: multiple managers,
    multiple conditions per manager, and multiple gameweeks."""

    def test_three_gameweeks_three_managers_fold_independently(self):
        store = LeagueHistoryStore("2026-27", "classic", 1)
        for gw in (1, 2, 3):
            store.append_rows(gw, [
                make_history_row(gameweek=gw, manager_key=key, league_position=key)
                for key in (1, 2, 3)
            ])

        projection = rebuild_counters_through(store, 3)

        assert manager_condition_views(projection, 1)["weeks_on_top"].length == 3
        for key in (2, 3):
            assert manager_condition_views(projection, key)["weeks_on_top"].length == 0
        assert sorted(all_condition_views(projection)) == [1, 2, 3]
