"""Tests for the notes pack and season-phase marker (U9)."""

from __future__ import annotations

from fpl_cli.models.league_history import FidelityTier, LedgerCaptaincy, LedgerTransaction
from fpl_cli.cli._league_recap_data import derive_point_in_time_positions
from fpl_cli.services.league_history import LeagueHistoryStore
from fpl_cli.services.league_history_notes import (
    GameweekWindow,
    NoteSurface,
    SeasonPhase,
    build_notes_pack,
    derive_season_phase,
    is_season_milestone,
)
from tests.conftest import make_history_row


class TestSeasonMilestones:
    """A once-a-season set-piece fires at a moment, not across a phase."""

    def test_the_halfway_boundary_is_a_milestone(self):
        assert is_season_milestone(19) is True

    def test_the_finale_is_a_milestone(self):
        assert is_season_milestone(38) is True

    def test_a_gameweek_past_the_constant_is_not_a_second_finale(self):
        """`derive_season_phase` calls GW38 *and beyond* the finale, which is
        right for a phase and wrong for a moment: gating on it would print a
        once-a-season set-piece at GW38 and again at GW39."""
        assert is_season_milestone(39) is False
        assert derive_season_phase(39) is SeasonPhase.FINALE

    def test_the_rest_of_the_midpoint_phase_is_not(self):
        """`derive_season_phase` calls GW19-31 the midpoint. Gating on the
        phase would fire a once-a-season table thirteen times."""
        assert all(derive_season_phase(gw) is SeasonPhase.MIDPOINT for gw in range(19, 32))
        assert [gw for gw in range(20, 32) if is_season_milestone(gw)] == []

    def test_no_ordinary_gameweek_qualifies(self):
        assert [gw for gw in range(1, 39) if is_season_milestone(gw)] == [19, 38]

    def test_a_shorter_season_moves_both_milestones_with_it(self):
        assert is_season_milestone(10, total_gameweeks=20, chip_split_gw=10) is True
        assert is_season_milestone(20, total_gameweeks=20, chip_split_gw=10) is True
        assert is_season_milestone(19, total_gameweeks=20, chip_split_gw=10) is False


# ---------------------------------------------------------------------------
# Local helpers
# ---------------------------------------------------------------------------


def _captain(points: int, had_fixture: bool | None = True) -> LedgerCaptaincy:
    return LedgerCaptaincy(name="Cap", points=points, played=True, had_fixture=had_fixture)


def _transaction(net: int) -> LedgerTransaction:
    return LedgerTransaction(
        player_in="In Guy", player_in_team="AAA", player_out="Out Guy", player_out_team="BBB", net=net,
    )


# ---------------------------------------------------------------------------
# Season phase boundaries (R15)
# ---------------------------------------------------------------------------


class TestSeasonPhaseBoundaries:
    """Every boundary gameweek pinned explicitly under the default constants
    (TOTAL_GAMEWEEKS=38, CHIP_SPLIT_GW=19): opener=1, pre_chip_boundary=2-18,
    midpoint=19-31, run_in=32-37, finale=38+."""

    def test_gw1_is_the_opener(self):
        assert derive_season_phase(1) is SeasonPhase.OPENER

    def test_gw2_is_the_first_pre_chip_boundary_gameweek(self):
        assert derive_season_phase(2) is SeasonPhase.PRE_CHIP_BOUNDARY

    def test_gw18_is_the_last_pre_chip_boundary_gameweek(self):
        assert derive_season_phase(18) is SeasonPhase.PRE_CHIP_BOUNDARY

    def test_gw19_is_the_first_midpoint_gameweek(self):
        assert derive_season_phase(19) is SeasonPhase.MIDPOINT

    def test_gw31_is_the_last_midpoint_gameweek(self):
        assert derive_season_phase(31) is SeasonPhase.MIDPOINT

    def test_gw32_is_the_first_run_in_gameweek(self):
        assert derive_season_phase(32) is SeasonPhase.RUN_IN

    def test_gw37_is_the_last_run_in_gameweek(self):
        assert derive_season_phase(37) is SeasonPhase.RUN_IN

    def test_gw38_is_the_finale(self):
        assert derive_season_phase(38) is SeasonPhase.FINALE

    def test_a_gameweek_beyond_the_constant_is_still_the_finale(self):
        """`>=` not `==`, so a season whose real final gameweek differs from
        the constant still reaches finale rather than never reaching it."""
        assert derive_season_phase(39) is SeasonPhase.FINALE

    def test_boundaries_respect_overridden_constants(self):
        # total=20, split=10, run_in_start = 20-6 = 14: pre_chip_boundary
        # 2-9, midpoint 10-13, run_in 14-19, finale 20+.
        assert derive_season_phase(20, total_gameweeks=20, chip_split_gw=10) is SeasonPhase.FINALE
        assert derive_season_phase(15, total_gameweeks=20, chip_split_gw=10) is SeasonPhase.RUN_IN
        assert derive_season_phase(10, total_gameweeks=20, chip_split_gw=10) is SeasonPhase.MIDPOINT
        assert derive_season_phase(9, total_gameweeks=20, chip_split_gw=10) is SeasonPhase.PRE_CHIP_BOUNDARY
        assert derive_season_phase(1, total_gameweeks=20, chip_split_gw=10) is SeasonPhase.OPENER


class TestSeasonPhasePartitionIsExhaustive:
    def test_every_gameweek_from_one_to_the_final_one_has_exactly_one_phase(self):
        """Walks the whole season (two-plus iterations per phase): confirms
        the partition never gaps and every phase's membership matches the
        pinned boundaries above."""
        phases = [derive_season_phase(gw) for gw in range(1, 39)]

        assert phases[0] is SeasonPhase.OPENER  # GW1
        assert all(p is SeasonPhase.PRE_CHIP_BOUNDARY for p in phases[1:18])  # GW2-18
        assert all(p is SeasonPhase.MIDPOINT for p in phases[18:31])  # GW19-31
        assert all(p is SeasonPhase.RUN_IN for p in phases[31:37])  # GW32-37
        assert phases[37] is SeasonPhase.FINALE  # GW38


# ---------------------------------------------------------------------------
# Streak entries (AE3, point 2)
# ---------------------------------------------------------------------------


class TestStreakEntries:
    """A run with any held gameweek is rendered as an observed count over its
    true span, never as a consecutive run."""

    def test_three_consecutive_blanks_produce_one_entry_naming_manager_and_window(self):
        """AE3: a captain blanked in GW4, GW5, and GW6 with a fixture each
        time produces exactly one entry, correctly windowed, prompt-visible."""
        store = LeagueHistoryStore("2026-27", "classic", 1)
        for gw in (4, 5, 6):
            store.append_rows(gw, [
                make_history_row(gameweek=gw, manager_key=1, manager_name="Alice", captain=_captain(1)),
            ])

        pack = build_notes_pack(store, 6)

        assert len(pack.entries) == 1
        entry = pack.entries[0]
        assert entry.condition_key == "captain_blank_run"
        assert entry.manager_name == "Alice"
        assert entry.length == 3
        assert entry.window == GameweekWindow(start_gameweek=4, end_gameweek=6)
        assert entry.held_count == 0
        assert "Alice" in entry.text
        assert "in a row" in entry.text
        assert NoteSurface.CONSOLE in entry.surfaces
        assert NoteSurface.REPORT in entry.surfaces
        assert NoteSurface.PROMPT in entry.surfaces

    def test_a_run_with_two_held_gameweeks_states_an_observed_count_not_a_row(self):
        """The same shape of run, but two of the five gameweeks in its span
        held (no fixture) rather than extended: length 3 across a 5-gameweek
        span, not "3 in a row"."""
        store = LeagueHistoryStore("2026-27", "classic", 1)
        blank_gameweeks = {4, 6, 8}
        for gw in range(4, 9):
            captain = _captain(1) if gw in blank_gameweeks else _captain(0, had_fixture=False)
            store.append_rows(gw, [
                make_history_row(gameweek=gw, manager_key=1, manager_name="Alice", captain=captain),
            ])

        pack = build_notes_pack(store, 8)

        entry = next(e for e in pack.entries if e.condition_key == "captain_blank_run")
        assert entry.length == 3
        assert entry.held_count == 2
        assert entry.window == GameweekWindow(start_gameweek=4, end_gameweek=8)
        assert entry.window is not None
        assert entry.window.span_length == 5
        assert "in a row" not in entry.text
        assert "not recorded" in entry.text

    def test_a_run_at_exactly_zero_length_produces_no_entry(self):
        """A captain who never blanked opens no run at all -- skipped
        entirely, not emitted as a zero-length entry."""
        store = LeagueHistoryStore("2026-27", "classic", 1)
        store.append_rows(1, [
            make_history_row(gameweek=1, manager_key=1, manager_name="Alice", captain=_captain(10)),
        ])

        pack = build_notes_pack(store, 1)

        assert not any(e.condition_key == "captain_blank_run" for e in pack.entries)

    def test_a_run_below_its_minimum_is_present_but_carries_no_surfaces(self):
        """A single blank (length 1) is below captain_blank_run's minimum of
        2 -- still present in the pack (a future JSON-emission unit needs to
        see it per KTD8), but reaches no rendering surface."""
        store = LeagueHistoryStore("2026-27", "classic", 1)
        store.append_rows(1, [
            make_history_row(gameweek=1, manager_key=1, manager_name="Alice", captain=_captain(1)),
        ])

        pack = build_notes_pack(store, 1)

        entry = next(e for e in pack.entries if e.condition_key == "captain_blank_run")
        assert entry.length == 1
        assert entry.surfaces == frozenset()

    def test_a_gameweek_the_manager_is_wholly_absent_from_does_not_shrink_the_window(self):
        """GW3 has no row at all for this manager (not unknown -- genuinely
        absent, e.g. a capture gap later backfilled around them). Folding
        skips it without counting it as held, so `length` and `held_count`
        alone would under-date the window if `end_gameweek` were derived
        from them instead of taken as the pack's own target gameweek."""
        store = LeagueHistoryStore("2026-27", "classic", 1)
        store.append_rows(1, [make_history_row(gameweek=1, manager_key=1, manager_name="Alice", captain=_captain(1))])
        store.append_rows(2, [make_history_row(gameweek=2, manager_key=1, manager_name="Alice", captain=_captain(1))])
        # GW3: no row for manager 1 at all.
        store.append_rows(3, [make_history_row(gameweek=3, manager_key=2, manager_name="Bob", captain=_captain(1))])
        store.append_rows(4, [make_history_row(gameweek=4, manager_key=1, manager_name="Alice", captain=_captain(1))])

        pack = build_notes_pack(store, 4)

        entry = next(e for e in pack.entries if e.manager_name == "Alice" and e.condition_key == "captain_blank_run")
        assert entry.length == 3
        assert entry.held_count == 0
        assert entry.window == GameweekWindow(start_gameweek=1, end_gameweek=4)
        # The window (4) is wider than the length (3) despite held_count == 0
        # -- must not be rendered as "3 in a row", which would falsely claim
        # a continuous run over a span that actually has a gap in it.
        assert "in a row" not in entry.text
        assert entry.text == (
            "Alice: Captain blank run of 3 in the last 4 (GW1-GW4), with 0 not recorded."
        )

    def test_multiple_managers_and_conditions_fold_independently(self):
        """Two managers, several conditions each: the top-of-table, gameweek
        winner also opens a different run than the bottom-of-table, gameweek
        loser, and neither manager's own opposite condition fires."""
        store = LeagueHistoryStore("2026-27", "classic", 1)
        for gw in (1, 2, 3):
            store.append_rows(gw, [
                make_history_row(gameweek=gw, manager_key=1, manager_name="Alice", league_position=1, gross_points=87),
                make_history_row(gameweek=gw, manager_key=2, manager_name="Bob", league_position=5, gross_points=40),
            ])

        pack = build_notes_pack(store, 3)
        reportable_pairs = {(e.manager_name, e.condition_key) for e in pack.entries if e.surfaces}

        assert ("Alice", "weeks_on_top") in reportable_pairs
        assert ("Alice", "gw_win_streak") in reportable_pairs
        # Bob's bottom-half run is tracked and counted, but never surfaces
        # as a streak: the condition carries no `min_run`, since restating
        # where the table already shows him is not news.
        assert ("Bob", "bottom_half_run") not in reportable_pairs
        assert ("Bob", "gw_loss_streak") in reportable_pairs
        assert ("Alice", "bottom_half_run") not in reportable_pairs
        assert ("Bob", "gw_win_streak") not in reportable_pairs

    def test_draft_only_conditions_are_absent_from_a_classic_pack(self):
        store = LeagueHistoryStore("2026-27", "classic", 1)
        for gw in (1, 2):
            store.append_rows(gw, [
                make_history_row(gameweek=gw, manager_key=1, manager_name="Alice", transfer_cost=4),
            ])

        pack = build_notes_pack(store, 2)

        assert not any(e.condition_key in ("waiver_win_run", "waiver_burn_run") for e in pack.entries)
        assert any(e.condition_key == "hit_run" for e in pack.entries)

    def test_classic_only_conditions_are_absent_from_a_draft_pack(self):
        store = LeagueHistoryStore("2026-27", "draft", 1)
        transaction = {
            "player_in": "In Guy", "player_in_team": "AAA",
            "player_out": "Out Guy", "player_out_team": "BBB", "net": 3,
        }
        for gw in (1, 2):
            store.append_rows(gw, [
                make_history_row(
                    gameweek=gw, manager_key=1, manager_name="Alice", fpl_format="draft",
                    transactions=[transaction],
                ),
            ])

        pack = build_notes_pack(store, 2)

        assert not any(e.condition_key in ("captain_blank_run", "hit_run") for e in pack.entries)
        assert any(e.condition_key == "waiver_win_run" for e in pack.entries)

    def test_entries_are_sorted_by_descending_excess(self):
        store = LeagueHistoryStore("2026-27", "classic", 1)
        for gw in range(1, 6):
            bob_position = 5 if gw <= 2 else 1  # Bob's weeks_on_top opens later, at GW3
            store.append_rows(gw, [
                make_history_row(gameweek=gw, manager_key=1, manager_name="Alice", league_position=1),
                make_history_row(gameweek=gw, manager_key=2, manager_name="Bob", league_position=bob_position),
            ])

        pack = build_notes_pack(store, 5)

        weeks_on_top = [e for e in pack.entries if e.condition_key == "weeks_on_top"]
        assert [e.manager_name for e in weeks_on_top] == ["Alice", "Bob"]
        first_excess = weeks_on_top[0].excess if weeks_on_top[0].excess is not None else 0
        second_excess = weeks_on_top[1].excess if weeks_on_top[1].excess is not None else 0
        assert first_excess > second_excess
        # Whole-pack invariant: never ascending anywhere in the list.
        excesses = [e.excess if e.excess is not None else 0 for e in pack.entries]
        assert excesses == sorted(excesses, reverse=True)

    def test_surfaces_nest_console_within_report_within_prompt(self):
        """No entry anywhere in the pack -- streak, season-phase, or
        coverage -- is ever console-only or report-without-prompt."""
        store = LeagueHistoryStore("2026-27", "classic", 1)
        for gw in range(1, 4):
            rows = [make_history_row(gameweek=gw, manager_key=1, manager_name="Alice", league_position=1)]
            if gw >= 2:
                rows.append(make_history_row(gameweek=gw, manager_key=2, manager_name="Bob", league_position=2))
            store.append_rows(gw, rows)

        pack = build_notes_pack(store, 3, league_start_gameweek=1)

        assert len(pack.all_entries) >= 2  # sanity: the nesting check below isn't vacuous
        for entry in pack.all_entries:
            if NoteSurface.CONSOLE in entry.surfaces:
                assert NoteSurface.REPORT in entry.surfaces
            if NoteSurface.REPORT in entry.surfaces:
                assert NoteSurface.PROMPT in entry.surfaces


# ---------------------------------------------------------------------------
# Season-count entries (issue #164)
# ---------------------------------------------------------------------------


class TestTiedPositionsDoNotDependOnCohortOrder:
    """Issue #164 review: `league_position` was an ordinal ranking whose ties
    broke on cohort standings order, so three conditions read a position
    nothing in the data supported -- and standings order shifts through the
    season, so a backfill could rule the same gameweek differently."""

    def _pack_for(self, order: list[int], totals: dict[int, int]):
        store = LeagueHistoryStore("2026-27", "classic", 1)
        positions = derive_point_in_time_positions([(k, totals[k]) for k in order])
        store.append_rows(1, [
            make_history_row(
                gameweek=1, manager_key=k, manager_name=f"M{k}",
                total_points=totals[k], league_position=positions[k],
            )
            for k in order
        ])
        return build_notes_pack(store, 1)

    def test_a_four_way_tie_rules_the_same_whichever_order_the_cohort_arrives_in(self):
        totals = dict.fromkeys((1, 2, 3, 4), 60)

        def verdicts(order):
            pack = self._pack_for(order, totals)
            return {
                (e.manager_name, e.condition_key): e.occurrences
                for e in pack.season_count_entries
            }

        assert verdicts([1, 2, 3, 4]) == verdicts([4, 3, 2, 1])

    def test_a_whole_cohort_tied_on_points_is_nobody_in_the_bottom_half(self):
        """Every manager level means no one is below the median, so the
        bottom-half count opens for nobody rather than for whichever half
        the cohort order happened to put last."""
        pack = self._pack_for([1, 2, 3, 4], dict.fromkeys((1, 2, 3, 4), 60))
        assert not [
            e for e in pack.season_count_entries if e.condition_key == "bottom_half_run"
        ]

    def test_two_managers_tied_at_the_summit_are_both_credited(self):
        """The same defect #163 fixed for gameweek wins: a shared lead must
        not hand one manager the week on top and the other nothing."""
        pack = self._pack_for([1, 2, 3], {1: 90, 2: 90, 3: 50})
        on_top = {
            e.manager_name for e in pack.season_count_entries
            if e.condition_key == "weeks_on_top"
        }
        assert on_top == {"M1", "M2"}


class TestSeasonCountEntries:
    def _count_entry(self, pack, manager_name: str, condition_key: str):
        return next(
            e for e in pack.season_count_entries
            if e.manager_name == manager_name and e.condition_key == condition_key
        )

    def test_a_count_survives_a_reset_and_names_the_span(self):
        """The issue's headline: the recap can now say "their second week on
        top of the season" after the run in between was reset."""
        store = LeagueHistoryStore("2026-27", "classic", 1)
        for gw, position in ((1, 1), (2, 2), (3, 1)):
            store.append_rows(gw, [
                make_history_row(gameweek=gw, manager_key=1, manager_name="Alice", league_position=position),
                make_history_row(gameweek=gw, manager_key=2, manager_name="Bob", league_position=3 - position),
            ])

        pack = build_notes_pack(store, 3)
        entry = self._count_entry(pack, "Alice", "weeks_on_top")

        assert entry.occurrences == 2
        assert entry.text == (
            "Alice: 2 gameweeks on top of the league this season (GW1-GW3), "
            "the latest this gameweek."
        )
        assert entry.window == GameweekWindow(start_gameweek=1, end_gameweek=3)

    def test_a_count_off_its_conditions_rule_is_withheld_from_every_surface(self):
        """A hit taken this gameweek is real but not yet notable: hits fire
        on multiples of three, so a first one stays off the weekly render.
        Retained for `--format json` regardless (KTD8)."""
        store = LeagueHistoryStore("2026-27", "classic", 1)
        store.append_rows(1, [make_history_row(
            gameweek=1, manager_key=1, manager_name="Alice", transfer_cost=4,
        )])

        pack = build_notes_pack(store, 1)
        entry = self._count_entry(pack, "Alice", "hit_run")

        assert entry.occurrences == 1
        assert entry.surfaces == frozenset()
        assert entry.text == (
            "Alice: 1 gameweek with a transfer hit this season (GW1-GW1), "
            "the first this gameweek."
        )

    def test_a_count_landing_on_its_step_fires_and_the_week_before_does_not(self):
        """Weeks on top fire on multiples of five: GW5 surfaces, GW4 does
        not."""
        store = LeagueHistoryStore("2026-27", "classic", 1)
        for gw in range(1, 6):
            store.append_rows(gw, [make_history_row(
                gameweek=gw, manager_key=1, manager_name="Alice", league_position=1,
            )])

        fourth = self._count_entry(build_notes_pack(store, 4), "Alice", "weeks_on_top")
        fifth = self._count_entry(build_notes_pack(store, 5), "Alice", "weeks_on_top")

        assert (fourth.occurrences, fourth.surfaces) == (4, frozenset())
        assert fifth.occurrences == 5
        assert fifth.surfaces == frozenset({NoteSurface.REPORT, NoteSurface.PROMPT})

    def test_a_first_win_fires_in_the_second_half_but_not_the_first(self):
        """A manager's first gameweek win is unremarkable in September and
        a story in April, so the first-occurrence rule is gated on the
        season's half."""
        store = LeagueHistoryStore("2026-27", "classic", 1)

        def _append(gw: int, winner: int) -> None:
            store.append_rows(gw, [
                make_history_row(
                    gameweek=gw, manager_key=key, manager_name=f"M{key}",
                    gross_points=60 if key == winner else 40, transfer_cost=0,
                )
                for key in (1, 2)
            ])

        _append(1, winner=1)  # first half: M1's first win
        for gw in range(2, 21):
            _append(gw, winner=1)
        _append(21, winner=2)  # second half: M2's first win

        early = self._count_entry(build_notes_pack(store, 1), "M1", "gw_win_streak")
        late = self._count_entry(build_notes_pack(store, 21), "M2", "gw_win_streak")

        assert (early.occurrences, early.surfaces) == (1, frozenset())
        assert late.occurrences == 1
        assert late.surfaces == frozenset({NoteSurface.REPORT, NoteSurface.PROMPT})

    def test_bottom_half_stays_silent_in_the_first_half_of_the_season(self):
        """Ten gameweeks in the bottom half is the rule's step, but the
        condition says nothing at all before the halfway boundary."""
        store = LeagueHistoryStore("2026-27", "classic", 1)
        for gw in range(1, 11):
            store.append_rows(gw, [
                make_history_row(gameweek=gw, manager_key=1, manager_name="Alice", league_position=1),
                make_history_row(gameweek=gw, manager_key=2, manager_name="Bob", league_position=2),
            ])

        entry = self._count_entry(build_notes_pack(store, 10), "Bob", "bottom_half_run")

        assert entry.occurrences == 10  # on the step, and still withheld
        assert entry.surfaces == frozenset()

    def test_a_firing_condition_carries_only_peers_past_its_ride_along_floor(self):
        """Captain blanks fire on a multiple of five and carry other
        blankers whose own total has reached three -- so a peer on two
        stays out, and a peer who did not blank at all this gameweek stays
        out however high their total."""
        store = LeagueHistoryStore("2026-27", "classic", 1)
        # Alice blanks every gameweek (reaching 5 at GW5); Bob blanks GW1-3
        # then captains well at GW5; Carol blanks GW4-5 only (total 2).
        blanks = {
            1: (True, True, False),
            2: (True, True, False),
            3: (True, True, False),
            4: (True, False, True),
            5: (True, False, True),
        }
        names = {1: "Alice", 2: "Bob", 3: "Carol"}
        for gw, flags in blanks.items():
            store.append_rows(gw, [
                make_history_row(
                    gameweek=gw, manager_key=key, manager_name=names[key],
                    captain=_captain(0 if blanked else 12),
                )
                for key, blanked in zip((1, 2, 3), flags)
            ])

        pack = build_notes_pack(store, 5)
        surfaced = frozenset({NoteSurface.REPORT, NoteSurface.PROMPT})

        alice = self._count_entry(pack, "Alice", "captain_blank_run")
        assert (alice.occurrences, alice.surfaces) == (5, surfaced)

        # Carol blanked this gameweek too, but her total of 2 is below the
        # ride-along floor of 3.
        carol = self._count_entry(pack, "Carol", "captain_blank_run")
        assert (carol.occurrences, carol.surfaces) == (2, frozenset())

        # Bob's total is 3, past the floor -- but he did not blank this
        # gameweek, so he is not part of the moment.
        bob = self._count_entry(pack, "Bob", "captain_blank_run")
        assert (bob.occurrences, bob.surfaces) == (3, frozenset())

    def test_bottom_half_carries_only_peers_level_with_the_milestone(self):
        """Bottom-half totals climb all season, so its ride-along window is
        relative: a manager genuinely level with the milestone shows, one
        far behind it does not -- which is what stops five bottom-half
        lines landing in the report every other week."""
        store = LeagueHistoryStore("2026-27", "classic", 1)
        # Four managers, positions 3-4 of 4 are the bottom half. Alice and
        # Bob sit there from GW1 (reaching 20 and 19 by GW20); Carol drops
        # in only for the last three gameweeks, ending far behind on 3.
        for gw in range(1, 21):
            bottom = [1, 2] if gw <= 17 else [1, 3]
            middle = [3, 4] if gw <= 17 else [2, 4]
            rows = []
            for rank, key in enumerate([*middle, *bottom], start=1):
                rows.append(make_history_row(
                    gameweek=gw, manager_key=key, manager_name={1: "Alice", 2: "Bob", 3: "Carol", 4: "Dan"}[key],
                    league_position=rank,
                ))
            store.append_rows(gw, rows)

        pack = build_notes_pack(store, 20)
        alice = self._count_entry(pack, "Alice", "bottom_half_run")
        carol = self._count_entry(pack, "Carol", "bottom_half_run")

        assert alice.occurrences == 20  # on the step, in the second half
        assert alice.surfaces == frozenset({NoteSurface.REPORT, NoteSurface.PROMPT})
        # Carol dropped in this gameweek too, but 3 is nowhere near 20.
        assert carol.occurrences == 3
        assert carol.surfaces == frozenset()

    def test_a_condition_with_no_ride_along_shows_only_who_fired(self):
        """Waiver counts fire on a multiple of five and carry nobody: a
        second manager's waiver haul in the same gameweek is not part of
        someone else's milestone."""
        store = LeagueHistoryStore("2026-27", "draft", 1)
        for gw in range(1, 6):
            rows = [make_history_row(
                gameweek=gw, fpl_format="draft", manager_key=1, manager_name="Alice",
                transactions=[_transaction(6)],
            )]
            if gw == 5:  # Bob hauls only in the milestone gameweek
                rows.append(make_history_row(
                    gameweek=gw, fpl_format="draft", manager_key=2, manager_name="Bob",
                    transactions=[_transaction(4)],
                ))
            store.append_rows(gw, rows)

        pack = build_notes_pack(store, 5)

        alice = self._count_entry(pack, "Alice", "waiver_win_run")
        assert alice.occurrences == 5
        assert alice.surfaces == frozenset({NoteSurface.REPORT, NoteSurface.PROMPT})
        assert "waiver hauls" in alice.text

        bob = self._count_entry(pack, "Bob", "waiver_win_run")
        assert (bob.occurrences, bob.surfaces) == (1, frozenset())

    def test_a_green_arrow_drought_fires_on_an_unbroken_run_and_carries_nobody(self):
        """The drought is only interesting as a streak, so its rule reads
        the open run rather than the season total -- and it names nobody
        else, however many managers also failed to climb."""
        store = LeagueHistoryStore("2026-27", "classic", 1)
        # Alice sits 2nd throughout (never improves: a 5-gameweek drought
        # by GW6); Bob holds 3rd, also never improving.
        for gw in range(1, 7):
            store.append_rows(gw, [
                make_history_row(gameweek=gw, manager_key=3, manager_name="Zoe", league_position=1),
                make_history_row(gameweek=gw, manager_key=1, manager_name="Alice", league_position=2),
                make_history_row(gameweek=gw, manager_key=2, manager_name="Bob", league_position=3),
            ])

        pack = build_notes_pack(store, 6)
        alice = self._count_entry(pack, "Alice", "green_arrow_drought")

        assert alice.occurrences == 5
        assert alice.surfaces == frozenset({NoteSurface.REPORT, NoteSurface.PROMPT})
        # Run-framed: the line leads with the run it fired on, so a reader
        # is never left wondering why a season total showed up this week.
        assert alice.text.startswith(
            "Alice: 5 gameweeks without a green arrow in a row, 5 this season",
        )
        # Bob's drought is identical, but the condition carries no
        # ride-alongs -- only the managers who fired it themselves show.
        bob = self._count_entry(pack, "Bob", "green_arrow_drought")
        assert bob.occurrences == 5
        assert bob.surfaces == frozenset({NoteSurface.REPORT, NoteSurface.PROMPT})

    def test_a_permanently_stuck_manager_stops_firing_past_the_last_milestone(self):
        """The run milestones are capped at 5 and 10 rather than every
        multiple: a manager rooted to one table position would otherwise
        re-announce the same non-fact every fifth gameweek forever."""
        store = LeagueHistoryStore("2026-27", "classic", 1)
        for gw in range(1, 17):
            store.append_rows(gw, [
                make_history_row(gameweek=gw, manager_key=3, manager_name="Zoe", league_position=1),
                make_history_row(gameweek=gw, manager_key=1, manager_name="Alice", league_position=2),
            ])

        # GW11 is drought run 10 (fires); GW16 would be run 15 (does not).
        at_ten = self._count_entry(build_notes_pack(store, 11), "Alice", "green_arrow_drought")
        at_fifteen = self._count_entry(build_notes_pack(store, 16), "Alice", "green_arrow_drought")

        assert at_ten.surfaces == frozenset({NoteSurface.REPORT, NoteSurface.PROMPT})
        assert at_fifteen.occurrences == 15
        assert at_fifteen.surfaces == frozenset()

    def test_a_milestone_gameweek_surfaces_every_nonzero_count_to_report_and_prompt(self):
        """At the halfway boundary and the finale the whole nonzero set is
        the season set-piece -- a count that did not grow this week included,
        exactly as the fines table prints everyone's totals at a milestone."""
        store = LeagueHistoryStore("2026-27", "classic", 1)
        store.append_rows(1, [make_history_row(gameweek=1, manager_key=1, manager_name="Alice", league_position=1)])
        for gw in (2, 3, 4):
            store.append_rows(gw, [make_history_row(gameweek=gw, manager_key=1, manager_name="Alice", league_position=2)])

        # GW4 is the halfway milestone under these shortened constants; the
        # weeks_on_top count last grew at GW1.
        pack = build_notes_pack(store, 4, total_gameweeks=8, chip_split_gw=4)
        entry = self._count_entry(pack, "Alice", "weeks_on_top")

        assert entry.occurrences == 1
        assert entry.surfaces == frozenset({NoteSurface.REPORT, NoteSurface.PROMPT})
        assert "this gameweek" not in entry.text  # stale total, honestly phrased

    def test_a_count_that_did_not_grow_this_gameweek_is_retained_without_surfaces(self):
        """A season total for something that did not happen this week is
        stale colour: still in the pack for `--format json` (KTD8), off
        every rendering surface."""
        store = LeagueHistoryStore("2026-27", "classic", 1)
        store.append_rows(1, [make_history_row(gameweek=1, manager_key=1, manager_name="Alice", league_position=1)])
        store.append_rows(2, [make_history_row(gameweek=2, manager_key=1, manager_name="Alice", league_position=2)])

        pack = build_notes_pack(store, 2)
        entry = self._count_entry(pack, "Alice", "weeks_on_top")

        assert entry.surfaces == frozenset()
        assert entry.text == "Alice: 1 gameweek on top of the league this season (GW1-GW2)."

    def test_a_held_gameweek_is_stated_as_not_judged_beside_the_count(self):
        """The #136 rule applied to counts: an un-judged gameweek is not an
        innocent one, so the number never appears bare across one."""
        store = LeagueHistoryStore("2026-27", "classic", 1)
        store.append_rows(1, [make_history_row(gameweek=1, manager_key=1, manager_name="Alice", league_position=1)])
        store.append_rows(2, [make_history_row(gameweek=2, manager_key=1, manager_name="Alice", capture_status="unknown")])
        store.append_rows(3, [make_history_row(gameweek=3, manager_key=1, manager_name="Alice", league_position=1)])

        pack = build_notes_pack(store, 3)
        entry = self._count_entry(pack, "Alice", "weeks_on_top")

        assert entry.occurrences == 2
        assert entry.held_count == 1
        assert entry.text == (
            "Alice: 2 gameweeks on top of the league this season (GW1-GW3), "
            "the latest this gameweek, with 1 gameweek not judged either way."
        )

    def test_a_zero_count_produces_no_entry(self):
        store = LeagueHistoryStore("2026-27", "classic", 1)
        store.append_rows(1, [make_history_row(gameweek=1, manager_key=1, manager_name="Alice", league_position=2)])

        pack = build_notes_pack(store, 1)

        assert not any(
            e.condition_key == "weeks_on_top" for e in pack.season_count_entries
        )

    def test_entries_are_sorted_by_descending_occurrences(self):
        store = LeagueHistoryStore("2026-27", "classic", 1)
        for gw in (1, 2, 3):
            store.append_rows(gw, [
                make_history_row(gameweek=gw, manager_key=1, manager_name="Alice", league_position=1),
                make_history_row(gameweek=gw, manager_key=2, manager_name="Bob",
                                 league_position=2 if gw < 3 else 3),
                make_history_row(gameweek=gw, manager_key=3, manager_name="Carol",
                                 league_position=3 if gw < 3 else 2),
            ])

        pack = build_notes_pack(store, 3)
        counts = [e.occurrences for e in pack.season_count_entries]

        assert counts == sorted(counts, key=lambda c: -(c or 0))
        assert pack.season_count_entries[0].occurrences == 3

    def test_a_departed_managers_frozen_count_is_not_surfaced(self):
        """Same cohort rule as streaks: a manager absent from this
        gameweek's rows keeps a frozen count in the projection, and the pack
        must not present it as live."""
        store = LeagueHistoryStore("2026-27", "classic", 1)
        store.append_rows(1, [
            make_history_row(gameweek=1, manager_key=1, manager_name="Alice", league_position=1),
            make_history_row(gameweek=1, manager_key=2, manager_name="Bob", league_position=2),
        ])
        store.append_rows(2, [
            make_history_row(gameweek=2, manager_key=2, manager_name="Bob", league_position=1),
        ])

        pack = build_notes_pack(store, 2)

        assert not any(e.manager_name == "Alice" for e in pack.season_count_entries)


# ---------------------------------------------------------------------------
# Fidelity tier (point 1)
# ---------------------------------------------------------------------------


class TestStreakEntryTier:
    def test_an_entry_derived_entirely_from_coarse_rows_records_coarse(self):
        store = LeagueHistoryStore("2026-27", "classic", 1)
        for gw in (1, 2):
            store.append_rows(gw, [
                make_history_row(gameweek=gw, manager_key=1, manager_name="Alice", tier="coarse", league_position=1),
            ])

        pack = build_notes_pack(store, 2)

        entry = next(e for e in pack.entries if e.condition_key == "weeks_on_top")
        assert entry.tier is FidelityTier.COARSE

    def test_a_window_mixing_coarse_and_detailed_rows_records_the_weaker_tier(self):
        store = LeagueHistoryStore("2026-27", "classic", 1)
        store.append_rows(1, [
            make_history_row(gameweek=1, manager_key=1, manager_name="Alice", tier="coarse", league_position=1),
        ])
        store.append_rows(2, [
            make_history_row(gameweek=2, manager_key=1, manager_name="Alice", tier="detailed", league_position=1),
        ])

        pack = build_notes_pack(store, 2)

        entry = next(e for e in pack.entries if e.condition_key == "weeks_on_top")
        assert entry.window == GameweekWindow(start_gameweek=1, end_gameweek=2)
        assert entry.tier is FidelityTier.COARSE  # weakest of {coarse, detailed}


# ---------------------------------------------------------------------------
# Season phase marker and the finale's full-season read (AE6, point 4)
# ---------------------------------------------------------------------------


class TestFinaleReadsTheWholeSeason:
    def test_finale_reads_beyond_the_trailing_window_while_a_normal_week_does_not(self):
        """A `weeks_on_top` run open since GW1 (coarse) through a small
        season's last gameweek (detailed from GW2 on). At the finale the
        pack must read all the way back to GW1 and report coarse; the same
        store read as an ordinary (non-finale) week, with a larger season
        length so GW1 falls outside the trailing window, must not."""
        store = LeagueHistoryStore("2026-27", "classic", 1)
        store.append_rows(1, [
            make_history_row(gameweek=1, manager_key=1, manager_name="Alice", tier="coarse", league_position=1),
        ])
        for gw in range(2, 11):
            store.append_rows(gw, [
                make_history_row(gameweek=gw, manager_key=1, manager_name="Alice", tier="detailed", league_position=1),
            ])

        finale_pack = build_notes_pack(store, 10, total_gameweeks=10, chip_split_gw=5)
        assert finale_pack.phase is SeasonPhase.FINALE
        finale_entry = next(e for e in finale_pack.entries if e.condition_key == "weeks_on_top")
        assert finale_entry.window == GameweekWindow(start_gameweek=1, end_gameweek=10)
        assert finale_entry.tier is FidelityTier.COARSE

        # Same store, same target gameweek, but a season long enough that
        # GW10 is an ordinary week (trailing window GW5-10 excludes GW1).
        weekly_pack = build_notes_pack(store, 10)
        assert weekly_pack.phase is not SeasonPhase.FINALE
        weekly_entry = next(e for e in weekly_pack.entries if e.condition_key == "weeks_on_top")
        assert weekly_entry.tier is FidelityTier.DETAILED

    def test_finale_phase_is_reported_on_the_pack(self):
        store = LeagueHistoryStore("2026-27", "classic", 1)
        store.append_rows(5, [make_history_row(gameweek=5, manager_key=1, manager_name="Alice")])
        pack = build_notes_pack(store, 5, total_gameweeks=5, chip_split_gw=3)
        assert pack.phase is SeasonPhase.FINALE
        assert "finale" in pack.season_phase_entry.text.lower()
        assert NoteSurface.CONSOLE not in pack.season_phase_entry.surfaces
        assert NoteSurface.REPORT in pack.season_phase_entry.surfaces
        assert NoteSurface.PROMPT in pack.season_phase_entry.surfaces


class TestMidpointAndRunInPhases:
    """MIDPOINT and RUN_IN are two of `_season_phase_text`'s five branches;
    every other test in this module lands in OPENER, PRE_CHIP_BOUNDARY, or
    FINALE, so these are the only exercise of the two through
    `build_notes_pack` rather than `derive_season_phase` alone."""

    def test_midpoint_phase_and_marker_text(self):
        store = LeagueHistoryStore("2026-27", "classic", 1)
        store.append_rows(25, [make_history_row(gameweek=25, manager_key=1, manager_name="Alice")])

        pack = build_notes_pack(store, 25, total_gameweeks=38, chip_split_gw=19)

        assert pack.phase is SeasonPhase.MIDPOINT
        assert "GW25 is the season midpoint" in pack.season_phase_entry.text
        assert "GW19" in pack.season_phase_entry.text

    def test_run_in_phase_and_marker_text(self):
        store = LeagueHistoryStore("2026-27", "classic", 1)
        store.append_rows(35, [make_history_row(gameweek=35, manager_key=1, manager_name="Alice")])

        pack = build_notes_pack(store, 35, total_gameweeks=38, chip_split_gw=19)

        assert pack.phase is SeasonPhase.RUN_IN
        assert "GW35 is in the run-in" in pack.season_phase_entry.text
        assert "GW38" in pack.season_phase_entry.text


class TestOpenerHasNoPriorHistory:
    def test_opener_phase_and_no_prior_history_statement(self):
        store = LeagueHistoryStore("2026-27", "classic", 1)
        store.append_rows(1, [make_history_row(gameweek=1, manager_key=1, manager_name="Alice")])

        pack = build_notes_pack(store, 1)

        assert pack.phase is SeasonPhase.OPENER
        assert any(
            "No league history has been recorded before GW1." in e.text for e in pack.coverage_entries
        )


# ---------------------------------------------------------------------------
# Coverage / negative context and the R17 "since GW X" qualifier
# ---------------------------------------------------------------------------


class TestCoverageAndR17Qualifier:
    def test_a_partition_with_no_rows_is_an_empty_pack_with_an_explicit_count_and_statement(self):
        store = LeagueHistoryStore("2026-27", "classic", 1)

        pack = build_notes_pack(store, 5)

        assert pack.entries == []
        assert pack.entry_count == 0
        assert len(pack.coverage_entries) >= 1
        assert "No league history has been recorded before GW5." in pack.coverage_entries[0].text

    def test_coverage_from_the_leagues_own_start_carries_no_since_gw_qualifier(self):
        """Full coverage from league_start_gameweek onward -- no "since GW X,
        later than the league's start" qualifier anywhere in the pack, even
        on a streak entry, which is inherently "windowed" already."""
        store = LeagueHistoryStore("2026-27", "classic", 1)
        for gw in range(1, 7):
            store.append_rows(gw, [
                make_history_row(gameweek=gw, manager_key=1, manager_name="Alice", league_position=1),
            ])

        pack = build_notes_pack(store, 6, league_start_gameweek=1)

        assert not any("later than" in e.text for e in pack.all_entries)
        assert "complete from its start" in pack.coverage_entries[0].text

    def test_a_genuine_gap_between_the_leagues_start_and_the_target_is_not_claimed_complete(self):
        """GW3 was never captured at all -- no file, distinct from a
        captured-but-unknown row -- so "complete from its start" must not be
        claimed even though the earliest captured gameweek (GW1) is early
        enough on its own."""
        store = LeagueHistoryStore("2026-27", "classic", 1)
        for gw in (1, 2, 4, 5):
            store.append_rows(gw, [
                make_history_row(gameweek=gw, manager_key=1, manager_name="Alice", league_position=1),
            ])
        # GW3: no file written at all -- a genuine hole, not just unknown.

        pack = build_notes_pack(store, 5, league_start_gameweek=1)

        text = pack.coverage_entries[0].text
        assert "complete from its start" not in text
        assert "begins at GW1" in text
        assert "missing GW3" in text

    def test_a_manager_who_joined_after_the_league_started_gets_the_qualifier(self):
        store = LeagueHistoryStore("2026-27", "classic", 1)
        for gw in range(1, 7):
            rows = [make_history_row(gameweek=gw, manager_key=1, manager_name="Alice", league_position=1)]
            if gw >= 4:
                rows.append(make_history_row(gameweek=gw, manager_key=2, manager_name="Bob", league_position=2))
            store.append_rows(gw, rows)

        pack = build_notes_pack(store, 6, league_start_gameweek=1)

        bob_entries = [e for e in pack.coverage_entries if e.manager_key == 2]
        assert len(bob_entries) == 1
        assert "Bob" in bob_entries[0].text
        assert "later than" in bob_entries[0].text
        assert "GW4" in bob_entries[0].text
        # Alice joined with the partition itself -- no qualifier for her.
        assert not any(e.manager_key == 1 for e in pack.coverage_entries)

    def test_two_managers_joining_at_different_points_both_get_their_own_entry(self):
        """Two-plus iterations through the joiner-detection loop: Bob and
        Carol join at different gameweeks, both later than the league start
        and later than the partition's own earliest capture."""
        store = LeagueHistoryStore("2026-27", "classic", 1)
        for gw in range(1, 6):
            rows = [make_history_row(gameweek=gw, manager_key=1, manager_name="Alice", league_position=1)]
            if gw >= 3:
                rows.append(make_history_row(gameweek=gw, manager_key=2, manager_name="Bob", league_position=2))
            if gw >= 4:
                rows.append(make_history_row(gameweek=gw, manager_key=3, manager_name="Carol", league_position=3))
            store.append_rows(gw, rows)

        pack = build_notes_pack(store, 5, league_start_gameweek=1)

        joiner_names = sorted(
            e.manager_name for e in pack.coverage_entries if e.manager_key is not None and e.manager_name is not None
        )
        assert joiner_names == ["Bob", "Carol"]

    def test_a_partition_wide_late_start_does_not_duplicate_per_manager(self):
        """Mid-season tool adoption (R17): the whole partition's history
        begins at GW10, later than the league's own GW1 start, but every
        manager present since that same GW10 baseline is not an individual
        "joiner" -- the fact is already carried once by the partition-level
        statement, not repeated per manager."""
        store = LeagueHistoryStore("2026-27", "classic", 1)
        for gw in (10, 11):
            store.append_rows(gw, [
                make_history_row(gameweek=gw, manager_key=1, manager_name="Alice", league_position=1),
                make_history_row(gameweek=gw, manager_key=2, manager_name="Bob", league_position=2),
            ])

        pack = build_notes_pack(store, 11, league_start_gameweek=1)

        assert not any(e.manager_key is not None for e in pack.coverage_entries)
        assert "later than the league's start (GW1)" in pack.coverage_entries[0].text
        assert "GW10" in pack.coverage_entries[0].text


# ---------------------------------------------------------------------------
# Earliest-gameweek cache persists across calls (R17 performance)
# ---------------------------------------------------------------------------


class TestEarliestGameweekCacheReducesRescans:
    """`_earliest_gameweeks_for_managers` persists what it discovers (see its
    own docstring): once a manager's earliest gameweek is cached, a later
    call -- even against a brand-new `LeagueHistoryStore` instance,
    simulating the next weekly `league-recap` invocation -- must not rescan
    the partition from GW1 to re-derive it."""

    def test_a_fully_cached_cohort_costs_no_scan_at_all(self, monkeypatch):
        from fpl_cli.services import league_history_notes as svc

        store = LeagueHistoryStore("2026-27", "classic", 1)
        for gw in range(1, 4):
            store.append_rows(gw, [make_history_row(gameweek=gw, manager_key=1, manager_name="Alice")])
        for gw in range(3, 6):
            store.append_rows(gw, [
                make_history_row(gameweek=gw, manager_key=1, manager_name="Alice"),
                make_history_row(gameweek=gw, manager_key=2, manager_name="Bob"),
            ])
        captured = store.captured_gameweeks()

        first = svc._earliest_gameweeks_for_managers(store, {1, 2}, captured)
        assert first == {1: 1, 2: 3}

        # A brand-new store instance -- simulating a later, separate call --
        # must serve both managers from the persisted cache with no scan at
        # all: monkeypatching `resolved_gameweek` to explode proves it is
        # never reached.
        second_store = LeagueHistoryStore("2026-27", "classic", 1)

        def _boom(_gameweek):
            raise AssertionError("resolved_gameweek must not be called: both managers are cached")

        monkeypatch.setattr(second_store, "resolved_gameweek", _boom)

        second = svc._earliest_gameweeks_for_managers(second_store, {1, 2}, captured)
        assert second == {1: 1, 2: 3}

    def test_a_newly_seen_manager_is_scanned_once_then_cached_for_later_calls(self, monkeypatch):
        """A cohort with one already-cached manager and one brand-new one:
        the scan still runs (only the new manager's join gameweek is
        unknown), but a later call finds both cached."""
        from fpl_cli.services import league_history_notes as svc

        store = LeagueHistoryStore("2026-27", "classic", 1)
        store.append_rows(1, [make_history_row(gameweek=1, manager_key=1, manager_name="Alice")])
        store.append_rows(2, [
            make_history_row(gameweek=2, manager_key=1, manager_name="Alice"),
            make_history_row(gameweek=2, manager_key=2, manager_name="Bob"),
        ])
        captured = store.captured_gameweeks()

        svc._earliest_gameweeks_for_managers(store, {1}, captured)  # caches Alice only

        second_store = LeagueHistoryStore("2026-27", "classic", 1)
        result = svc._earliest_gameweeks_for_managers(second_store, {1, 2}, captured)
        assert result == {1: 1, 2: 2}

        third_store = LeagueHistoryStore("2026-27", "classic", 1)

        def _boom(_gameweek):
            raise AssertionError("resolved_gameweek must not be called: both managers are now cached")

        monkeypatch.setattr(third_store, "resolved_gameweek", _boom)
        assert svc._earliest_gameweeks_for_managers(third_store, {1, 2}, captured) == {1: 1, 2: 2}

    def test_the_joiner_qualifier_stays_correct_on_a_later_cache_backed_call(self):
        """End-to-end sanity check through the public API: the cache changes
        performance, never the answer -- Bob's earliest gameweek is still
        correctly reported on a second `build_notes_pack` call made against
        a fresh store instance."""
        store = LeagueHistoryStore("2026-27", "classic", 1)
        for gw in range(1, 5):
            rows = [make_history_row(gameweek=gw, manager_key=1, manager_name="Alice", league_position=1)]
            if gw >= 3:
                rows.append(make_history_row(gameweek=gw, manager_key=2, manager_name="Bob", league_position=2))
            store.append_rows(gw, rows)

        build_notes_pack(store, 4, league_start_gameweek=1)  # first call populates the cache

        second_store = LeagueHistoryStore("2026-27", "classic", 1)
        second_store.append_rows(5, [
            make_history_row(gameweek=5, manager_key=1, manager_name="Alice", league_position=1),
            make_history_row(gameweek=5, manager_key=2, manager_name="Bob", league_position=2),
        ])
        pack = build_notes_pack(second_store, 5, league_start_gameweek=1)

        bob_entries = [e for e in pack.coverage_entries if e.manager_key == 2]
        assert len(bob_entries) == 1
        assert "GW3" in bob_entries[0].text


# ---------------------------------------------------------------------------
# Pack-level structure
# ---------------------------------------------------------------------------


class TestNotesPackStructure:
    def test_all_entries_combines_streaks_season_phase_and_coverage(self):
        store = LeagueHistoryStore("2026-27", "classic", 1)
        store.append_rows(1, [
            make_history_row(gameweek=1, manager_key=1, manager_name="Alice", captain=_captain(1)),
        ])

        pack = build_notes_pack(store, 1)

        assert pack.season_phase_entry in pack.all_entries
        for coverage_entry in pack.coverage_entries:
            assert coverage_entry in pack.all_entries
        for streak_entry in pack.entries:
            assert streak_entry in pack.all_entries
        for count_entry in pack.season_count_entries:
            assert count_entry in pack.all_entries
        assert len(pack.all_entries) == (
            len(pack.entries) + len(pack.season_count_entries) + 1 + len(pack.coverage_entries)
        )

    def test_entry_count_reflects_only_streak_entries(self):
        store = LeagueHistoryStore("2026-27", "classic", 1)
        store.append_rows(1, [
            make_history_row(gameweek=1, manager_key=1, manager_name="Alice", captain=_captain(10)),
        ])

        pack = build_notes_pack(store, 1)  # captain never blanked -> no streak entries

        assert pack.entry_count == 0
        assert len(pack.entries) == 0
        # Yet the pack is not "nothing" -- the season-phase marker and a
        # coverage statement are still present.
        assert pack.season_phase_entry is not None
        assert len(pack.coverage_entries) >= 1

    def test_pack_is_stamped_with_the_partition_and_the_requested_gameweek(self):
        store = LeagueHistoryStore("2026-27", "draft", 7)
        pack = build_notes_pack(store, 3, league_start_gameweek=2)

        assert pack.season == "2026-27"
        assert pack.fpl_format == "draft"
        assert pack.league_id == 7
        assert pack.gameweek == 3
        assert pack.league_start_gameweek == 2


# ---------------------------------------------------------------------------
# Multi-iteration loop coverage (repo-wide Definition of Done)
# ---------------------------------------------------------------------------


class TestMultiIterationLoops:
    """Every loop this module writes, exercised with at least two iterations
    at once: multiple gameweeks read into `rows_by_gameweek`, multiple
    managers and conditions folded into streak entries, and multiple
    managers scanned for their earliest captured gameweek."""

    def test_three_gameweeks_three_managers_produce_independent_entries(self):
        store = LeagueHistoryStore("2026-27", "classic", 1)
        for gw in (1, 2, 3):
            store.append_rows(gw, [
                make_history_row(gameweek=gw, manager_key=key, manager_name=f"Manager{key}", league_position=key)
                for key in (1, 2, 3)
            ])

        pack = build_notes_pack(store, 3)

        top = next(e for e in pack.entries if e.condition_key == "weeks_on_top" and e.manager_key == 1)
        assert top.length == 3
        assert not any(
            e.condition_key == "weeks_on_top" and e.manager_key in (2, 3) for e in pack.entries
        )

    def test_the_raw_row_read_window_covers_every_requested_trailing_gameweek(self):
        """Six gameweeks captured; a request at GW6 reads all six (the
        trailing window is exactly six wide), each contributing to tier
        resolution independently."""
        store = LeagueHistoryStore("2026-27", "classic", 1)
        for gw in range(1, 7):
            store.append_rows(gw, [
                make_history_row(gameweek=gw, manager_key=1, manager_name="Alice", tier="coarse", league_position=1),
            ])

        pack = build_notes_pack(store, 6)

        entry = next(e for e in pack.entries if e.condition_key == "weeks_on_top")
        assert entry.window == GameweekWindow(start_gameweek=1, end_gameweek=6)
        assert entry.tier is FidelityTier.COARSE
