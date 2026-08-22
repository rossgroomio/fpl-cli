"""Tests for the notes pack and season-phase marker (U9)."""

from __future__ import annotations

from fpl_cli.models.league_history import FidelityTier, LedgerCaptaincy
from fpl_cli.services.league_history import LeagueHistoryStore
from fpl_cli.services.league_history_notes import (
    GameweekWindow,
    NoteSurface,
    SeasonPhase,
    build_notes_pack,
    derive_season_phase,
)
from tests.conftest import make_history_row

# ---------------------------------------------------------------------------
# Local helpers
# ---------------------------------------------------------------------------


def _captain(points: int, had_fixture: bool | None = True) -> LedgerCaptaincy:
    return LedgerCaptaincy(name="Cap", points=points, played=True, had_fixture=had_fixture)


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

    def test_multiple_managers_and_conditions_fold_independently(self):
        """Two managers, several conditions each: the top-of-table, gameweek
        winner also opens a different run than the bottom-of-table, gameweek
        loser, and neither manager's own opposite condition fires."""
        store = LeagueHistoryStore("2026-27", "classic", 1)
        for gw in (1, 2, 3):
            store.append_rows(gw, [
                make_history_row(gameweek=gw, manager_key=1, manager_name="Alice", league_position=1, gw_rank=1),
                make_history_row(gameweek=gw, manager_key=2, manager_name="Bob", league_position=5, gw_rank=2),
            ])

        pack = build_notes_pack(store, 3)
        reportable_pairs = {(e.manager_name, e.condition_key) for e in pack.entries if e.surfaces}

        assert ("Alice", "weeks_on_top") in reportable_pairs
        assert ("Alice", "gw_win_streak") in reportable_pairs
        assert ("Bob", "bottom_half_run") in reportable_pairs
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
        assert len(pack.all_entries) == len(pack.entries) + 1 + len(pack.coverage_entries)

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
