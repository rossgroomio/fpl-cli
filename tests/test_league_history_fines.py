"""Tests for the season fine tally folded out of the ledger (issue #136)."""

from __future__ import annotations

from fpl_cli.models.league_history import FidelityTier, LedgerFine
from fpl_cli.services.league_history import LeagueHistoryStore
from fpl_cli.services.league_history_fines import build_season_fines_tally
from tests.conftest import make_history_row

# ---------------------------------------------------------------------------
# Local helpers
# ---------------------------------------------------------------------------

ALL_RULES = ["last-place", "below-threshold", "red-card"]
COHORT_RULES = ["last-place", "below-threshold"]


def _fine(manager_key: int, rule_type: str = "last-place") -> LedgerFine:
    return LedgerFine(manager_key=manager_key, rule_type=rule_type, message=f"{rule_type} fine.")


def _store(fpl_format: str = "classic") -> LeagueHistoryStore:
    return LeagueHistoryStore("2026-27", fpl_format, 1)  # type: ignore[arg-type]


def _tally_for(store: LeagueHistoryStore, through: int, **kwargs):
    return build_season_fines_tally(store, through, **kwargs)


def _by_name(tally, name: str):
    return next(m for m in tally.managers if m.manager_name == name)


# ---------------------------------------------------------------------------
# Counting
# ---------------------------------------------------------------------------


class TestFineCounts:
    def test_fines_are_summed_per_manager_and_rule_across_gameweeks(self):
        store = _store()
        for gw in (1, 2, 3):
            store.append_rows(gw, [
                make_history_row(
                    gameweek=gw, manager_key=1, manager_name="Alice",
                    fine_rules_evaluated=ALL_RULES,
                    fines=[_fine(1)] if gw != 2 else [],
                ),
                make_history_row(
                    gameweek=gw, manager_key=2, manager_name="Bob",
                    fine_rules_evaluated=ALL_RULES,
                    fines=[_fine(2, "red-card")] if gw == 2 else [],
                ),
            ])

        tally = _tally_for(store, 3, rule_types=ALL_RULES)

        assert _by_name(tally, "Alice").counts == {"last-place": 2}
        assert _by_name(tally, "Alice").total == 2
        assert _by_name(tally, "Bob").counts == {"red-card": 1}
        assert tally.total_fines == 3

    def test_two_fines_in_one_gameweek_both_count_but_the_gameweek_counts_once(self):
        store = _store()
        store.append_rows(1, [
            make_history_row(
                gameweek=1, manager_key=1, manager_name="Alice",
                fine_rules_evaluated=ALL_RULES,
                fines=[_fine(1, "last-place"), _fine(1, "red-card")],
            ),
        ])

        alice = _by_name(_tally_for(store, 1, rule_types=ALL_RULES), "Alice")

        assert alice.counts == {"last-place": 1, "red-card": 1}
        assert alice.total == 2
        assert alice.fined_gameweeks == [1]

    def test_a_rename_keeps_one_tally_under_the_current_name(self):
        """`LedgerFine` keys on `manager_key` precisely so a mid-season rename
        cannot split someone's tally in two."""
        store = _store()
        store.append_rows(1, [make_history_row(
            gameweek=1, manager_key=1, manager_name="Alice",
            fine_rules_evaluated=ALL_RULES, fines=[_fine(1)],
        )])
        store.append_rows(2, [make_history_row(
            gameweek=2, manager_key=1, manager_name="Alice Renamed",
            fine_rules_evaluated=ALL_RULES, fines=[_fine(1)],
        )])

        tally = _tally_for(store, 2, rule_types=ALL_RULES)

        assert len(tally.managers) == 1
        assert tally.managers[0].manager_name == "Alice Renamed"
        assert tally.managers[0].total == 2

    def test_two_managers_sharing_a_display_name_stay_separate(self):
        store = _store()
        store.append_rows(1, [
            make_history_row(
                gameweek=1, manager_key=1, manager_name="Same Name",
                fine_rules_evaluated=ALL_RULES, fines=[_fine(1)],
            ),
            make_history_row(
                gameweek=1, manager_key=2, manager_name="Same Name",
                fine_rules_evaluated=ALL_RULES,
            ),
        ])

        tally = _tally_for(store, 1, rule_types=ALL_RULES)

        assert [m.total for m in tally.managers] == [1, 0]
        assert {m.manager_key for m in tally.managers} == {1, 2}

    def test_managers_are_ranked_by_total_then_name(self):
        store = _store()
        store.append_rows(1, [
            make_history_row(gameweek=1, manager_key=1, manager_name="Zoe",
                             fine_rules_evaluated=ALL_RULES),
            make_history_row(gameweek=1, manager_key=2, manager_name="Bob",
                             fine_rules_evaluated=ALL_RULES, fines=[_fine(2)]),
            make_history_row(gameweek=1, manager_key=3, manager_name="Alice",
                             fine_rules_evaluated=ALL_RULES),
        ])

        tally = _tally_for(store, 1, rule_types=ALL_RULES)

        assert [m.manager_name for m in tally.managers] == ["Bob", "Alice", "Zoe"]
        assert [m.manager_name for m in tally.fined_managers] == ["Bob"]

    def test_a_gameweek_past_the_through_point_is_not_counted(self):
        store = _store()
        for gw in (1, 2):
            store.append_rows(gw, [make_history_row(
                gameweek=gw, manager_key=1, manager_name="Alice",
                fine_rules_evaluated=ALL_RULES, fines=[_fine(1)],
            )])

        assert _tally_for(store, 1, rule_types=ALL_RULES).total_fines == 1


# ---------------------------------------------------------------------------
# Coverage honesty
# ---------------------------------------------------------------------------


class TestCoverageQualifiers:
    def test_a_fully_ruled_span_says_so_rather_than_saying_nothing(self):
        store = _store()
        for gw in (1, 2):
            store.append_rows(gw, [make_history_row(
                gameweek=gw, manager_key=1, manager_name="Alice",
                fine_rules_evaluated=ALL_RULES,
            )])

        tally = _tally_for(store, 2, league_start_gameweek=1, rule_types=ALL_RULES)

        assert tally.qualifiers == [
            "Every gameweek from GW1 through GW2 was ruled, so these totals cover the whole span.",
        ]
        assert _by_name(tally, "Alice").is_fully_ruled

    def test_an_uncaptured_gameweek_is_named_rather_than_read_as_innocent(self):
        store = _store()
        for gw in (1, 3):
            store.append_rows(gw, [make_history_row(
                gameweek=gw, manager_key=1, manager_name="Alice",
                fine_rules_evaluated=ALL_RULES,
            )])

        tally = _tally_for(store, 3, league_start_gameweek=1, rule_types=ALL_RULES)

        assert any("GW2 was never captured" in line for line in tally.qualifiers)
        assert _by_name(tally, "Alice").unruled_gameweeks == [2]
        assert not _by_name(tally, "Alice").is_fully_ruled

    def test_an_unknown_capture_row_leaves_that_manager_unruled_not_unfined(self):
        """R19: the capture never reached them, so nothing was ruled against
        them -- a naive fold would score the gameweek as clean."""
        store = _store()
        store.append_rows(1, [
            make_history_row(gameweek=1, manager_key=1, manager_name="Alice",
                             fine_rules_evaluated=ALL_RULES),
            make_history_row(gameweek=1, manager_key=2, manager_name="Bob",
                             capture_status="unknown"),
        ])

        tally = _tally_for(store, 1, league_start_gameweek=1, rule_types=ALL_RULES)

        assert _by_name(tally, "Bob").ruled_gameweeks == []
        assert _by_name(tally, "Bob").unruled_gameweeks == [1]
        assert any(
            "Bob: no fine was ruled against them in GW1" in line for line in tally.qualifiers
        )

    def test_a_league_wide_gap_is_stated_once_not_once_per_manager(self):
        """One missing gameweek would otherwise produce a line per league
        member, burying the gaps that really are personal."""
        store = _store()
        for gw in (1, 3):
            store.append_rows(gw, [
                make_history_row(gameweek=gw, manager_key=1, manager_name="Alice",
                                 fine_rules_evaluated=ALL_RULES),
                make_history_row(gameweek=gw, manager_key=2, manager_name="Bob",
                                 fine_rules_evaluated=ALL_RULES),
            ])

        tally = _tally_for(store, 3, league_start_gameweek=1, rule_types=ALL_RULES)

        assert sum("was never captured" in line for line in tally.qualifiers) == 1
        assert not any("no fine was ruled against them" in line for line in tally.qualifiers)
        # The coverage is still recorded per manager, for the table's own marker.
        assert _by_name(tally, "Alice").unruled_gameweeks == [2]
        assert not _by_name(tally, "Alice").is_fully_ruled

    def test_a_rule_the_gameweek_could_not_rule_is_named_with_its_cause(self):
        """A coarse gameweek carries no squad, so `red-card` is structurally
        unrulable there -- and saying nothing would present a partial ruling
        as a complete one."""
        store = _store()
        store.append_rows(1, [make_history_row(
            gameweek=1, manager_key=1, manager_name="Alice",
            tier=FidelityTier.COARSE, fine_rules_evaluated=COHORT_RULES,
        )])

        tally = _tally_for(store, 1, league_start_gameweek=1, rule_types=ALL_RULES)

        line = next(line for line in tally.qualifiers if "'red-card'" in line)
        assert "was not ruled in GW1" in line
        assert "That gameweek is recorded at the coarse tier, which carries no squad" in line

    def test_a_detailed_gameweek_missing_a_rule_is_named_without_the_coarse_cause(self):
        store = _store()
        store.append_rows(1, [make_history_row(
            gameweek=1, manager_key=1, manager_name="Alice",
            fine_rules_evaluated=COHORT_RULES,
        )])

        tally = _tally_for(store, 1, league_start_gameweek=1, rule_types=ALL_RULES)

        line = next(line for line in tally.qualifiers if "'red-card'" in line)
        assert "coarse tier" not in line

    def test_a_row_predating_the_ruling_field_is_qualified_not_trusted(self):
        """A pre-schema-3 row records fines but not what was checked, so its
        zeroes prove nothing."""
        store = _store()
        store.append_rows(1, [make_history_row(
            gameweek=1, manager_key=1, manager_name="Alice", fines=[_fine(1)],
        )])

        tally = _tally_for(store, 1, league_start_gameweek=1, rule_types=ALL_RULES)

        assert _by_name(tally, "Alice").total == 1, "a recorded fine is still recorded history"
        assert _by_name(tally, "Alice").ruled_gameweeks == []
        assert any("holds no record of what was ruled" in line for line in tally.qualifiers)

    def test_a_gameweek_captured_with_no_rules_configured_reads_as_uncovered(self):
        store = _store()
        store.append_rows(1, [make_history_row(
            gameweek=1, manager_key=1, manager_name="Alice", fine_rules_evaluated=[],
        )])

        tally = _tally_for(store, 1, league_start_gameweek=1, rule_types=[])

        assert any("recorded a ruling on no rules at all" in line for line in tally.qualifiers)

    def test_a_gameweek_the_capture_reached_nobody_says_so(self):
        """Every row unknown is a failed capture, not an unconfigured one.
        Reading it as "no fine rules configured" hides the real cause and
        tells the reader nothing they can act on (#165 review)."""
        store = _store()
        store.append_rows(1, [
            make_history_row(gameweek=1, manager_key=1, manager_name="Alice",
                             capture_status="unknown"),
            make_history_row(gameweek=1, manager_key=2, manager_name="Bob",
                             capture_status="unknown"),
        ])

        tally = _tally_for(store, 1, league_start_gameweek=1, rule_types=ALL_RULES)

        assert any("the capture reached nobody" in line for line in tally.qualifiers)
        assert not any("no rules at all" in line for line in tally.qualifiers)

    def test_one_unknown_row_beside_a_ruled_one_is_not_an_unreached_gameweek(self):
        store = _store()
        store.append_rows(1, [
            make_history_row(gameweek=1, manager_key=1, manager_name="Alice",
                             fine_rules_evaluated=ALL_RULES),
            make_history_row(gameweek=1, manager_key=2, manager_name="Bob",
                             capture_status="unknown"),
        ])

        tally = _tally_for(store, 1, league_start_gameweek=1, rule_types=ALL_RULES)

        assert not any("reached nobody" in line for line in tally.qualifiers)

    def test_an_empty_ruling_at_the_coarse_tier_names_the_missing_squad(self):
        """A league configuring only squad-dependent rules records `[]` at the
        coarse tier. Saying "no rules configured" there is false -- the rule
        is configured, just not rulable without a squad (#165 review)."""
        store = _store()
        store.append_rows(1, [make_history_row(
            gameweek=1, manager_key=1, manager_name="Alice",
            tier=FidelityTier.COARSE, fine_rules_evaluated=[],
        )])

        tally = _tally_for(store, 1, league_start_gameweek=1, rule_types=["red-card"])

        line = next(line for line in tally.qualifiers if "no rules at all" in line)
        assert "carries no squad" in line

    def test_an_empty_ruling_at_the_detailed_tier_claims_no_tier_cause(self):
        store = _store()
        store.append_rows(1, [make_history_row(
            gameweek=1, manager_key=1, manager_name="Alice", fine_rules_evaluated=[],
        )])

        tally = _tally_for(store, 1, league_start_gameweek=1, rule_types=[])

        line = next(line for line in tally.qualifiers if "no rules at all" in line)
        assert "coarse tier" not in line

    def test_an_unreadable_gameweek_is_reported_rather_than_raising(self):
        store = _store()
        store.append_rows(1, [make_history_row(
            gameweek=1, manager_key=1, manager_name="Alice", fine_rules_evaluated=ALL_RULES,
        )])
        store.gameweek_file(2).write_text("{not json\n", encoding="utf-8")

        tally = _tally_for(store, 2, league_start_gameweek=1, rule_types=ALL_RULES)

        assert any("GW2 could not be read" in line for line in tally.qualifiers)
        assert _by_name(tally, "Alice").total == 0

    def test_an_empty_partition_says_nothing_is_recorded(self):
        tally = _tally_for(_store(), 5, rule_types=ALL_RULES)

        assert tally.managers == []
        assert tally.has_records is False
        assert tally.qualifiers == [
            "No league history has been recorded through GW5, so there is nothing to tally.",
        ]


# ---------------------------------------------------------------------------
# Joiners and leavers (R17)
# ---------------------------------------------------------------------------


class TestJoinersAndLeavers:
    def test_a_mid_season_joiner_keeps_their_lower_total_and_is_qualified(self):
        store = _store()
        for gw in (1, 2, 3):
            rows = [make_history_row(
                gameweek=gw, manager_key=1, manager_name="Alice",
                fine_rules_evaluated=ALL_RULES, fines=[_fine(1)],
            )]
            if gw == 3:
                rows.append(make_history_row(
                    gameweek=gw, manager_key=2, manager_name="Newbie",
                    fine_rules_evaluated=ALL_RULES,
                ))
            store.append_rows(gw, rows)

        tally = _tally_for(store, 3, league_start_gameweek=1, rule_types=ALL_RULES)
        newbie = _by_name(tally, "Newbie")

        assert newbie.total == 0
        assert newbie.first_recorded_gameweek == 3
        assert newbie.unruled_gameweeks == [], "GW1-2 were never theirs to be ruled in"
        assert any(
            "Newbie: recorded history begins at GW3, later than GW1" in line
            for line in tally.qualifiers
        )

    def test_a_manager_who_left_is_not_charged_for_gameweeks_after_they_went(self):
        store = _store()
        store.append_rows(1, [
            make_history_row(gameweek=1, manager_key=1, manager_name="Alice",
                             fine_rules_evaluated=ALL_RULES),
            make_history_row(gameweek=1, manager_key=2, manager_name="Departed",
                             fine_rules_evaluated=ALL_RULES, fines=[_fine(2)]),
        ])
        store.append_rows(2, [make_history_row(
            gameweek=2, manager_key=1, manager_name="Alice", fine_rules_evaluated=ALL_RULES,
        )])

        tally = _tally_for(store, 2, league_start_gameweek=1, rule_types=ALL_RULES)
        departed = _by_name(tally, "Departed")

        assert departed.total == 1, "fines already ruled against them still stand"
        assert departed.last_recorded_gameweek == 1
        assert departed.unruled_gameweeks == []

    def test_a_present_manager_is_charged_for_a_trailing_uncaptured_gameweek(self):
        """The counterpart to a leaver: a manager whose last row is in the
        most recent populated gameweek is still in the league, so a gameweek
        after it that nobody captured is a real gap in their coverage."""
        store = _store()
        store.append_rows(1, [make_history_row(
            gameweek=1, manager_key=1, manager_name="Alice", fine_rules_evaluated=ALL_RULES,
        )])

        alice = _by_name(_tally_for(store, 3, league_start_gameweek=1, rule_types=ALL_RULES), "Alice")

        assert alice.unruled_gameweeks == [2, 3]


# ---------------------------------------------------------------------------
# Span and column shaping
# ---------------------------------------------------------------------------


class TestSpanAndColumns:
    def test_an_unknown_league_start_falls_back_to_the_earliest_capture(self):
        """A standalone ledger read cannot know the league's start gameweek,
        so it must not invent a gap before its own first row."""
        store = _store()
        store.append_rows(12, [make_history_row(
            gameweek=12, manager_key=1, manager_name="Alice", fine_rules_evaluated=ALL_RULES,
        )])

        tally = _tally_for(store, 12, rule_types=ALL_RULES)

        assert tally.start_gameweek == 12
        assert not any("never captured" in line for line in tally.qualifiers)

    def test_a_known_league_start_before_the_first_capture_reports_the_gap(self):
        store = _store()
        store.append_rows(3, [make_history_row(
            gameweek=3, manager_key=1, manager_name="Alice", fine_rules_evaluated=ALL_RULES,
        )])

        tally = _tally_for(store, 3, league_start_gameweek=1, rule_types=ALL_RULES)

        assert tally.start_gameweek == 1
        assert any("GW1-2 was never captured" in line for line in tally.qualifiers)

    def test_configured_rules_lead_the_columns_and_survive_never_triggering(self):
        store = _store()
        store.append_rows(1, [make_history_row(
            gameweek=1, manager_key=1, manager_name="Alice", fine_rules_evaluated=ALL_RULES,
        )])

        tally = _tally_for(store, 1, rule_types=ALL_RULES)

        assert tally.rule_types == ALL_RULES

    def test_a_rule_ruled_in_history_but_no_longer_configured_is_kept(self):
        """Dropping it would lose recorded history: it was ruled, and someone
        may still owe for it."""
        store = _store()
        store.append_rows(1, [make_history_row(
            gameweek=1, manager_key=1, manager_name="Alice",
            fine_rules_evaluated=["red-card"], fines=[_fine(1, "red-card")],
        )])

        tally = _tally_for(store, 1, rule_types=["last-place"])

        assert tally.rule_types == ["last-place", "red-card"]
        assert _by_name(tally, "Alice").counts == {"red-card": 1}
