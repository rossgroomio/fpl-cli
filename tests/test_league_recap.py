"""Tests for league recap data collection and awards."""
# pyright: reportTypedDictNotRequiredAccess=false

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from fpl_cli.agents.orchestration.report import ReportAgent, _format_standings_block
from fpl_cli.cli._league_recap_data import (
    _PICKS_CONCURRENCY,
    RecapReconciliationError,
    _apply_league_start_offset,
    _bucket_draft_txns_by_league_entry,
    _classic_pick_flags,
    _compute_shared_awards,
    _compute_standings_movement,
    _compute_transfer_awards,
    _compute_waiver_awards,
    _contract_draft_txn_chains,
    _fetch_all_manager_data,
    _has_previous_gameweek,
    _reconcile_classic_headline_numbers,
    collect_classic_recap_data,
    collect_draft_recap_data,
    derive_point_in_time_positions,
    evaluate_league_fines,
)
from fpl_cli.cli._league_recap_types import (
    RecapAwards,
    RecapDraftTransaction,
    RecapManagerEntry,
    RecapManagerPlayer,
    RecapTransfer,
)
from fpl_cli.prompts.league_recap import (
    collect_player_clubs,
    format_recap_awards_context,
    format_recap_captains_context,
    format_recap_chips_context,
    format_recap_fines_context,
    format_recap_league_history_context,
    format_recap_player_clubs_context,
    format_recap_standings_context,
    get_recap_synthesis_prompt,
)
from fpl_cli.services.league_history_notes import (
    GameweekWindow,
    NoteKind,
    NotesPack,
    NotesPackEntry,
    NoteSurface,
    SeasonPhase,
)
from tests.conftest import make_draft_player, make_player

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_manager(
    name: str = "Manager A",
    entry_id: int = 1,
    gw_points: int = 50,
    gross_points: int | None = None,
    total_points: int | None = 500,
    gw_rank: int = 1,
    overall_rank: int | None = 1,
    previous_rank: int | None = 1,
    captain: str = "Salah",
    captain_points: int = 10,
    captain_played: bool = True,
    vice_captain: str = "Saka",
    vice_captain_points: int = 0,
    bench_points: int = 5,
    transfer_cost: int = 0,
    active_chip: str | None = None,
    squad: list[RecapManagerPlayer] | None = None,
    transfers: list[RecapTransfer] | None = None,
) -> RecapManagerEntry:
    """Factory for RecapManagerEntry with sensible defaults.

    `gross_points` defaults to `gw_points` -- the common case where a test
    doesn't care about the gross/net distinction. Pass `total_points`,
    `overall_rank`, or `previous_rank` as None to build an entry with that
    key absent, e.g. an unreconstructable draft replay (U2/U3).
    """
    result = RecapManagerEntry(
        manager_name=name,
        entry_id=entry_id,
        gw_points=gw_points,
        gross_points=gross_points if gross_points is not None else gw_points,
        total_points=total_points if total_points is not None else 0,
        gw_rank=gw_rank,
        overall_rank=overall_rank if overall_rank is not None else 0,
        previous_rank=previous_rank if previous_rank is not None else 0,
        captain=captain,
        captain_points=captain_points,
        captain_played=captain_played,
        vice_captain=vice_captain,
        vice_captain_points=vice_captain_points,
        active_chip=active_chip,
        squad=squad or [],
        bench_points=bench_points,
        transfer_cost=transfer_cost,
        auto_subs=[],
    )
    if total_points is None:
        del result["total_points"]
    if overall_rank is None:
        del result["overall_rank"]
    if previous_rank is None:
        del result["previous_rank"]
    if transfers is not None:
        result["transfers"] = transfers
    return result


def _make_squad_player(
    name: str = "Player",
    points: int = 5,
    contributed: bool = True,
    auto_sub_out: bool = False,
    **kwargs,
) -> RecapManagerPlayer:
    return RecapManagerPlayer(
        name=name,
        team="ARS",
        position="MID",
        points=points,
        is_captain=kwargs.get("is_captain", False),
        is_vice_captain=kwargs.get("is_vice_captain", False),
        contributed=contributed,
        is_bench_boost_player=kwargs.get("is_bench_boost_player", False),
        auto_sub_in=kwargs.get("auto_sub_in", False),
        auto_sub_out=auto_sub_out,
        red_cards=kwargs.get("red_cards", 0),
        unmatched=kwargs.get("unmatched", False),
    )


# ---------------------------------------------------------------------------
# Awards: clear winners
# ---------------------------------------------------------------------------


class TestAwardsClearWinner:
    def test_gw_winner_highest_points(self):
        managers = [
            _make_manager(name="Alice", gw_points=80),
            _make_manager(name="Bob", gw_points=60),
            _make_manager(name="Charlie", gw_points=40),
        ]
        awards = _compute_shared_awards(managers)
        assert awards["gw_winner"]["manager_name"] == "Alice"
        assert awards["gw_winner"]["value"] == 80

    def test_gw_loser_lowest_points(self):
        managers = [
            _make_manager(name="Alice", gw_points=80),
            _make_manager(name="Bob", gw_points=20),
        ]
        awards = _compute_shared_awards(managers)
        assert awards["gw_loser"]["manager_name"] == "Bob"
        assert awards["gw_loser"]["value"] == 20

    def test_biggest_bench_haul(self):
        bench_player = _make_squad_player(name="Benchman", points=15, contributed=False)
        managers = [
            _make_manager(name="Alice", gw_points=29, bench_points=15, squad=[bench_player]),
            _make_manager(name="Bob", bench_points=3),
        ]
        awards = _compute_shared_awards(managers)
        assert awards["biggest_bench_haul"]["manager_name"] == "Alice"
        assert "15 pts on the bench" in awards["biggest_bench_haul"]["detail"]
        assert "team scored 29 pts" in awards["biggest_bench_haul"]["detail"]
        assert "Benchman (15)" in awards["biggest_bench_haul"]["detail"]

    def test_best_captain_by_raw_points(self):
        managers = [
            _make_manager(name="Alice", captain="Salah", captain_points=15),
            _make_manager(name="Bob", captain="Haaland", captain_points=2),
        ]
        awards = _compute_shared_awards(managers)
        assert awards["best_captain"]["manager_name"] == "Alice"
        assert "Salah" in awards["best_captain"]["detail"]

    def test_worst_captain(self):
        managers = [
            _make_manager(name="Alice", captain="Salah", captain_points=15),
            _make_manager(name="Bob", captain="Haaland", captain_points=0),
        ]
        awards = _compute_shared_awards(managers)
        assert awards["worst_captain"]["manager_name"] == "Bob"
        assert "Haaland" in awards["worst_captain"]["detail"]


# ---------------------------------------------------------------------------
# Awards: ties
# ---------------------------------------------------------------------------


class TestAwardsTies:
    def test_tied_gw_winner(self):
        managers = [
            _make_manager(name="Alice", gw_points=80),
            _make_manager(name="Bob", gw_points=80),
            _make_manager(name="Charlie", gw_points=40),
        ]
        awards = _compute_shared_awards(managers)
        assert awards["gw_winner"]["value"] == 80
        assert "Alice" in awards["gw_winner"]["detail"]
        assert "Bob" in awards["gw_winner"]["detail"]
        assert "Alice and Bob" == awards["gw_winner"]["manager_name"]

    def test_tied_gw_loser(self):
        managers = [
            _make_manager(name="Alice", gw_points=80),
            _make_manager(name="Bob", gw_points=20),
            _make_manager(name="Charlie", gw_points=20),
        ]
        awards = _compute_shared_awards(managers)
        assert awards["gw_loser"]["value"] == 20
        assert "Bob" in awards["gw_loser"]["detail"]
        assert "Charlie" in awards["gw_loser"]["detail"]
        assert "Bob and Charlie" == awards["gw_loser"]["manager_name"]

    def test_tied_bench_haul(self):
        bench_a = _make_squad_player(name="BenchA", points=12, contributed=False)
        bench_b = _make_squad_player(name="BenchB", points=12, contributed=False)
        managers = [
            _make_manager(name="Alice", gw_points=40, bench_points=12, squad=[bench_a]),
            _make_manager(name="Bob", gw_points=55, bench_points=12, squad=[bench_b]),
        ]
        awards = _compute_shared_awards(managers)
        assert awards["biggest_bench_haul"]["value"] == 12
        assert "Alice and Bob" == awards["biggest_bench_haul"]["manager_name"]
        assert "BenchA (12)" in awards["biggest_bench_haul"]["detail"]
        assert "BenchB (12)" in awards["biggest_bench_haul"]["detail"]
        assert "team scored 40 pts" in awards["biggest_bench_haul"]["detail"]
        assert "team scored 55 pts" in awards["biggest_bench_haul"]["detail"]

    def test_wide_bench_haul_tie_caps_managers(self):
        """Each bench entry is verbose, so a wide tie is capped like the other awards."""
        managers = [
            _make_manager(
                name=f"M{i:02d}", entry_id=i, gw_points=60 - i, bench_points=12,
                squad=[_make_squad_player(name=f"Bench{i:02d}", points=12, contributed=False)],
            )
            for i in range(6)
        ]
        awards = _compute_shared_awards(managers)
        detail = awards["biggest_bench_haul"]["detail"]
        assert detail.count("left 12 pts on the bench") == 3
        assert "3 more managers omitted" in detail
        # The award still records every tied manager, only the prose is trimmed
        assert awards["biggest_bench_haul"]["manager_name"].count(" and ") == 5
        # All tied on bench points, so the ones named are those it cost most:
        # M05 scored least (55), M00 most (60).
        assert [n for n in ("M00", "M01", "M02", "M03", "M04", "M05") if n in detail] == [
            "M03", "M04", "M05",
        ]
        assert detail.index("M05") < detail.index("M04") < detail.index("M03")

    def test_tied_captain_same_captain(self):
        """Two managers captaining the same player - grouped output."""
        managers = [
            _make_manager(name="Alice", captain="Salah", captain_points=10),
            _make_manager(name="Bob", captain="Salah", captain_points=10),
        ]
        awards = _compute_shared_awards(managers)
        assert awards["best_captain"]["value"] == 10
        assert awards["best_captain"]["detail"] == "Alice and Bob captained Salah (10 pts)"
        assert awards["best_captain"]["manager_name"] == "Alice and Bob"

    def test_tied_captain_different_captains(self):
        """Two managers captaining different players with equal points."""
        managers = [
            _make_manager(name="Alice", captain="Salah", captain_points=10),
            _make_manager(name="Bob", captain="Haaland", captain_points=10),
        ]
        awards = _compute_shared_awards(managers)
        assert awards["best_captain"]["value"] == 10
        assert "Alice captained Salah (10 pts)" in awards["best_captain"]["detail"]
        assert "Bob captained Haaland (10 pts)" in awards["best_captain"]["detail"]

    def test_worst_captain_tie_same_captain(self):
        """Two managers tied for worst captain on the same player."""
        managers = [
            _make_manager(name="Alice", captain="Salah", captain_points=15),
            _make_manager(name="Bob", captain="Haaland", captain_points=2),
            _make_manager(name="Charlie", captain="Haaland", captain_points=2),
        ]
        awards = _compute_shared_awards(managers)
        assert awards["worst_captain"]["value"] == 2
        assert awards["worst_captain"]["detail"] == "Bob and Charlie captained Haaland (2 pts) [2 of 3 managers]"
        assert awards["worst_captain"]["manager_name"] == "Bob and Charlie"

    def test_total_managers_uses_full_league_not_just_fetched(self):
        """A failed picks fetch must not shrink the '[n of total]' denominator.

        Passing the full league size (as collect_classic/draft_recap_data do)
        keeps the fraction honest even when some managers dropped out of
        `managers` because their picks fetch failed.
        """
        managers = [
            _make_manager(name="Alice", captain="Salah", captain_points=15),
            _make_manager(name="Bob", captain="Haaland", captain_points=2),
            _make_manager(name="Charlie", captain="Haaland", captain_points=2),
        ]
        awards = _compute_shared_awards(managers, total_managers=20)
        assert awards["worst_captain"]["detail"] == "Bob and Charlie captained Haaland (2 pts) [2 of 20 managers]"

    def test_three_way_captain_tie_same_captain(self):
        """Three managers all captaining the same player - uses 'all captained'."""
        managers = [
            _make_manager(name="Alice", captain="Salah", captain_points=10),
            _make_manager(name="Bob", captain="Salah", captain_points=10),
            _make_manager(name="Charlie", captain="Salah", captain_points=10),
        ]
        awards = _compute_shared_awards(managers)
        assert awards["best_captain"]["value"] == 10
        assert awards["best_captain"]["detail"] == (
            "Alice, Bob and Charlie all captained Salah (10 pts)"
        )
        assert awards["best_captain"]["manager_name"] == "Alice and Bob and Charlie"

    def test_three_way_captain_tie_mixed_captains(self):
        """Three managers tied - two share a captain, one different."""
        managers = [
            _make_manager(name="Alice", captain="Salah", captain_points=10),
            _make_manager(name="Bob", captain="Salah", captain_points=10),
            _make_manager(name="Charlie", captain="Palmer", captain_points=10),
        ]
        awards = _compute_shared_awards(managers)
        assert awards["best_captain"]["value"] == 10
        assert "Alice and Bob captained Salah (10 pts)" in awards["best_captain"]["detail"]
        assert "Charlie captained Palmer (10 pts)" in awards["best_captain"]["detail"]

    def test_worst_captain_vc_rescue_not_penalised(self):
        """Captain dnp but VC covered (high VC pts): should not win worst captain."""
        managers = [
            _make_manager(name="Alice", captain="Salah", captain_points=0, captain_played=False, vice_captain_points=15),
            _make_manager(name="Bob", captain="Haaland", captain_points=2),
            _make_manager(name="Charlie", captain="Palmer", captain_points=8),
        ]
        awards = _compute_shared_awards(managers)
        # Alice's effective captain pts = 15 (VC rescued); Bob (2) is worst
        assert awards["worst_captain"]["manager_name"] == "Bob"
        assert awards["worst_captain"]["value"] == 2

    def test_worst_captain_vc_also_blanked(self):
        """Captain dnp AND VC scored 0: genuinely worst captain outcome."""
        managers = [
            _make_manager(name="Alice", captain="Salah", captain_points=0, captain_played=False, vice_captain_points=0),
            _make_manager(name="Bob", captain="Haaland", captain_points=2),
            _make_manager(name="Charlie", captain="Palmer", captain_points=8),
        ]
        awards = _compute_shared_awards(managers)
        assert awards["worst_captain"]["manager_name"] == "Alice"
        assert awards["worst_captain"]["value"] == 0

    def test_worst_captain_all_captains_didnt_play(self):
        """All captains dnp with 0 VC pts: all tied at 0."""
        managers = [
            _make_manager(name="Alice", captain="Salah", captain_points=0, captain_played=False, vice_captain_points=0),
            _make_manager(name="Bob", captain="Haaland", captain_points=0, captain_played=False, vice_captain_points=0),
        ]
        awards = _compute_shared_awards(managers)
        assert awards["worst_captain"]["value"] == 0

    def test_worst_captain_mixed_tie_reports_effective_points(self):
        """Tie struck on effective pts must not print a tied manager's raw captain_points."""
        managers = [
            _make_manager(name="Alice", captain="Salah", captain_points=0, captain_played=False,
                          vice_captain="Gordon", vice_captain_points=3),
            _make_manager(name="Bob", captain="Haaland", captain_points=3),
            _make_manager(name="Charlie", captain="Palmer", captain_points=10),
        ]
        awards = _compute_shared_awards(managers)
        detail = awards["worst_captain"]["detail"]
        assert awards["worst_captain"]["value"] == 3
        assert "Bob captained Haaland (3 pts)" in detail
        # Salah blanked - the 3 came from Alice's vice, so don't credit it to Salah
        assert "Alice captained Salah (dnp; vice scored 3)" in detail
        assert "(0 pts)" not in detail

    def test_wide_captain_tie_caps_groups(self):
        """A tie spread across many captains is capped, with the omitted count reported."""
        managers = [
            _make_manager(name=f"M{i:02d}", entry_id=i, captain=f"Cap{i}", captain_points=0)
            for i in range(6)
        ] + [_make_manager(name="Top", entry_id=99, captain="Salah", captain_points=12)]
        awards = _compute_shared_awards(managers)
        detail = awards["worst_captain"]["detail"]
        assert detail.count("captained") == 3
        assert "3 more managers omitted" in detail


# ---------------------------------------------------------------------------
# Awards: edge cases
# ---------------------------------------------------------------------------


class TestAwardsEdgeCases:
    def test_empty_managers(self):
        awards = _compute_shared_awards([])
        assert awards == RecapAwards()

    def test_single_manager(self):
        managers = [_make_manager(name="Solo")]
        awards = _compute_shared_awards(managers)
        assert awards["gw_winner"]["manager_name"] == "Solo"
        assert awards["gw_loser"]["manager_name"] == "Solo"

    def test_zero_bench_points_no_award(self):
        managers = [_make_manager(name="Alice", bench_points=0)]
        awards = _compute_shared_awards(managers)
        assert "biggest_bench_haul" not in awards

    def test_all_captains_zero(self):
        managers = [
            _make_manager(name="Alice", captain="Salah", captain_points=0),
            _make_manager(name="Bob", captain="Haaland", captain_points=0),
        ]
        awards = _compute_shared_awards(managers)
        # best_captain not awarded when all are 0
        assert "best_captain" not in awards
        # worst_captain is always awarded
        assert awards["worst_captain"]["value"] == 0


# ---------------------------------------------------------------------------
# Awards: active chip
# ---------------------------------------------------------------------------


class TestAwardsChips:
    def test_manager_with_triple_captain(self):
        managers = [
            _make_manager(name="Alice", active_chip="3xc", captain_points=15),
            _make_manager(name="Bob", captain_points=5),
        ]
        awards = _compute_shared_awards(managers)
        assert awards["best_captain"]["manager_name"] == "Alice"

    def test_manager_with_bench_boost(self):
        """Bench boost means bench_points=0 (all contribute). No bench award expected.

        Uses the display-form chip ("BB") that _fetch_all_manager_data actually stores
        on RecapManagerEntry — the raw "bboost" never reaches _compute_shared_awards.
        """
        bb_player = _make_squad_player(
            name="AliceBench", points=0, contributed=True, is_bench_boost_player=True,
        )
        managers = [
            _make_manager(name="Alice", active_chip="BB", bench_points=0, squad=[bb_player]),
            _make_manager(name="Bob", bench_points=10),
        ]
        awards = _compute_shared_awards(managers)
        assert awards["biggest_bench_haul"]["manager_name"] == "Bob"

    def test_bb_manager_excluded_even_if_bench_points_stale(self):
        # Regression: the defensive filter must not rely on the raw "bboost" string —
        # RecapManagerEntry.active_chip is stored in display form ("BB"). Detection
        # runs off the per-player is_bench_boost_player flag instead.
        bench_bb = _make_squad_player(
            name="BBBench", points=30, contributed=True, is_bench_boost_player=True,
        )
        non_bb_bench = _make_squad_player(name="Benchman", points=15, contributed=False)
        managers = [
            _make_manager(
                name="Alice", active_chip="BB", gw_points=99,
                bench_points=30, squad=[bench_bb],
            ),
            _make_manager(name="Bob", gw_points=60, bench_points=15, squad=[non_bb_bench]),
        ]
        awards = _compute_shared_awards(managers)
        assert awards["biggest_bench_haul"]["manager_name"] == "Bob"
        assert "Alice" not in awards["biggest_bench_haul"]["detail"]

    def test_all_managers_played_bench_boost(self):
        bb_player = _make_squad_player(
            name="Bench", points=0, contributed=True, is_bench_boost_player=True,
        )
        managers = [
            _make_manager(name="Alice", active_chip="BB", bench_points=0, squad=[bb_player]),
            _make_manager(name="Bob", active_chip="BB", bench_points=0, squad=[bb_player]),
        ]
        awards = _compute_shared_awards(managers)
        assert "biggest_bench_haul" not in awards


# ---------------------------------------------------------------------------
# Transfer awards
# ---------------------------------------------------------------------------


class TestTransferAwards:
    def test_transfer_genius(self):
        transfers = [
            RecapTransfer(
                player_in="Palmer", player_in_team="CHE", player_in_points=15,
                player_out="Wilson", player_out_team="FUL", player_out_points=2,
                net=13, cost=0,
            ),
        ]
        managers = [
            _make_manager(name="Alice", transfers=transfers),
            _make_manager(name="Bob"),
        ]
        awards: RecapAwards = {}  # type: ignore[typeddict-item]
        _compute_transfer_awards(managers, awards)
        assert "transfer_genius" in awards
        award = awards["transfer_genius"]
        assert award["manager_name"] == "Alice"
        assert award["value"] == 13
        assert "Best: Palmer for Wilson (+13)" in award["detail"]

    def test_transfer_disaster(self):
        transfers = [
            RecapTransfer(
                player_in="Dud", player_in_team="LEI", player_in_points=0,
                player_out="Star", player_out_team="LIV", player_out_points=12,
                net=-12, cost=4,
            ),
        ]
        managers = [
            _make_manager(name="Alice", transfers=transfers),
            _make_manager(name="Bob"),
        ]
        awards: RecapAwards = {}  # type: ignore[typeddict-item]
        _compute_transfer_awards(managers, awards)
        assert "transfer_disaster" in awards
        assert awards.get("transfer_disaster", {}).get("manager_name") == "Alice"  # type: ignore[union-attr]

    def test_no_transfers_no_awards(self):
        managers = [_make_manager(name="Alice"), _make_manager(name="Bob")]
        awards: RecapAwards = {}  # type: ignore[typeddict-item]
        _compute_transfer_awards(managers, awards)
        assert "transfer_genius" not in awards
        assert "transfer_disaster" not in awards

    def test_positive_net_only_no_disaster(self):
        transfers = [
            RecapTransfer(
                player_in="Good", player_in_team="ARS", player_in_points=10,
                player_out="OK", player_out_team="TOT", player_out_points=5,
                net=5, cost=0,
            ),
        ]
        managers = [_make_manager(name="Alice", transfers=transfers)]
        awards: RecapAwards = {}  # type: ignore[typeddict-item]
        _compute_transfer_awards(managers, awards)
        assert "transfer_genius" in awards
        assert "transfer_disaster" not in awards

    def test_disaster_fires_from_hit_alone(self):
        # +3 raw aggregate, -4 hit -> true_net -1, should fire disaster.
        # Pre-fix this case was suppressed (raw aggregate ignored hit).
        transfers = [
            RecapTransfer(
                player_in="Mid", player_in_team="ARS", player_in_points=4,
                player_out="Out", player_out_team="TOT", player_out_points=1,
                net=3, cost=4,
            ),
        ]
        managers = [
            _make_manager(name="Alice", transfers=transfers, transfer_cost=4),
            _make_manager(name="Bob"),
        ]
        awards: RecapAwards = {}  # type: ignore[typeddict-item]
        _compute_transfer_awards(managers, awards)
        assert "transfer_disaster" in awards
        assert awards["transfer_disaster"]["manager_name"] == "Alice"
        assert awards["transfer_disaster"]["value"] == -1
        detail = awards["transfer_disaster"]["detail"]
        assert "lost 1 net pts overall" in detail
        assert "+3 raw" in detail
        assert "-4 hit" in detail

    def test_genius_value_is_post_hit_true_net(self):
        # Raw aggregate +13, -4 hit -> true_net +9. Genius value should be 9.
        transfers = [
            RecapTransfer(
                player_in="A", player_in_team="ARS", player_in_points=10,
                player_out="B", player_out_team="TOT", player_out_points=2,
                net=8, cost=4,
            ),
            RecapTransfer(
                player_in="C", player_in_team="CHE", player_in_points=7,
                player_out="D", player_out_team="WHU", player_out_points=2,
                net=5, cost=4,
            ),
        ]
        managers = [
            _make_manager(name="Alice", transfers=transfers, transfer_cost=4),
            _make_manager(name="Bob"),
        ]
        awards: RecapAwards = {}  # type: ignore[typeddict-item]
        _compute_transfer_awards(managers, awards)
        assert "transfer_genius" in awards
        assert awards["transfer_genius"]["value"] == 9
        detail = awards["transfer_genius"]["detail"]
        assert "gained 9 net pts overall" in detail
        assert "+13 raw across 2 transfers" in detail
        assert "-4 hit" in detail
        # Top move first, second move via "also"
        assert "Best: A for B (+8)" in detail
        assert "also C for D (+5)" in detail

    def test_detail_caps_at_three_with_omitted_count(self):
        # 5 transfers, cap=3, expect "2 more omitted" tail.
        transfers = [
            RecapTransfer(
                player_in=f"In{i}", player_in_team="ARS", player_in_points=10 - i,
                player_out=f"Out{i}", player_out_team="TOT", player_out_points=0,
                net=10 - i, cost=0,
            )
            for i in range(5)
        ]
        managers = [
            _make_manager(name="Alice", transfers=transfers),
            _make_manager(name="Bob"),
        ]
        awards: RecapAwards = {}  # type: ignore[typeddict-item]
        _compute_transfer_awards(managers, awards)
        detail = awards["transfer_genius"]["detail"]
        # Top 3 by net (descending): In0 (+10), In1 (+9), In2 (+8)
        assert "Best: In0 for Out0 (+10)" in detail
        assert "In1 for Out1 (+9)" in detail
        assert "In2 for Out2 (+8)" in detail
        assert "In3" not in detail
        assert "In4" not in detail
        assert "2 more omitted" in detail

    def test_disaster_side_caps_at_three(self):
        # 5 negative-net transfers, no hit. Disaster sorts ascending; top 3
        # worst should appear, the two least-bad should be omitted.
        transfers = [
            RecapTransfer(
                player_in=f"Bust{i}", player_in_team="ARS", player_in_points=0,
                player_out=f"Star{i}", player_out_team="TOT", player_out_points=2 + i,
                net=-(2 + i), cost=0,
            )
            for i in range(5)
        ]
        managers = [
            _make_manager(name="Alice", transfers=transfers),
            _make_manager(name="Bob"),
        ]
        awards: RecapAwards = {}  # type: ignore[typeddict-item]
        _compute_transfer_awards(managers, awards)
        detail = awards["transfer_disaster"]["detail"]
        # Top 3 worst by net (ascending): Bust4 (-6), Bust3 (-5), Bust2 (-4)
        assert "Worst: Bust4 for Star4 (-6)" in detail
        assert "Bust3 for Star3 (-5)" in detail
        assert "Bust2 for Star2 (-4)" in detail
        assert "Bust0" not in detail
        assert "Bust1" not in detail
        assert "2 more omitted" in detail

    def test_disaster_from_hit_alone_uses_neutral_framing(self):
        # +3 raw, -4 hit -> true_net -1. No individual move is negative.
        # Detail must not label a positive move as "Worst".
        transfers = [
            RecapTransfer(
                player_in="Mid", player_in_team="ARS", player_in_points=4,
                player_out="Out", player_out_team="TOT", player_out_points=1,
                net=3, cost=4,
            ),
        ]
        managers = [
            _make_manager(name="Alice", transfers=transfers, transfer_cost=4),
            _make_manager(name="Bob"),
        ]
        awards: RecapAwards = {}  # type: ignore[typeddict-item]
        _compute_transfer_awards(managers, awards)
        detail = awards["transfer_disaster"]["detail"]
        assert "Worst: Mid for Out (+3)" not in detail
        assert "All swaps were profitable" in detail
        assert "the hit cost produced the loss" in detail
        assert "Mid for Out (+3)" in detail

    def test_single_transfer_no_hit_compact_format(self):
        # Single transfer, no hit: skip the redundant raw/count parenthetical.
        transfers = [
            RecapTransfer(
                player_in="Solo", player_in_team="ARS", player_in_points=7,
                player_out="Gone", player_out_team="TOT", player_out_points=0,
                net=7, cost=0,
            ),
        ]
        managers = [
            _make_manager(name="Alice", transfers=transfers),
            _make_manager(name="Bob"),
        ]
        awards: RecapAwards = {}  # type: ignore[typeddict-item]
        _compute_transfer_awards(managers, awards)
        detail = awards["transfer_genius"]["detail"]
        assert "Alice gained 7 net pts overall." in detail
        assert "Best: Solo for Gone (+7)." in detail
        assert "raw across" not in detail
        assert "hit" not in detail


# ---------------------------------------------------------------------------
# Standings movement
# ---------------------------------------------------------------------------


class TestStandingsMovement:
    def test_movement_computed_from_point_diff(self):
        managers = [
            _make_manager(name="Alice", entry_id=1, gw_points=80, total_points=500),
            _make_manager(name="Bob", entry_id=2, gw_points=20, total_points=480),
            _make_manager(name="Charlie", entry_id=3, gw_points=50, total_points=470),
        ]
        _compute_standings_movement(managers)
        # Previous totals: Alice=420, Bob=460, Charlie=420
        # Previous order: Bob(460)=1st, Alice(420)=2nd, Charlie(420)=3rd
        assert managers[1]["previous_rank"] == 1  # Bob was 1st
        # Alice and Charlie both had 420 - rank depends on sort stability
        assert managers[0]["previous_rank"] in (2, 3)  # Alice was 2nd or 3rd

    def test_movement_single_manager(self):
        managers = [_make_manager(name="Solo", entry_id=1)]
        _compute_standings_movement(managers)
        assert managers[0]["previous_rank"] == 1

    def test_movement_no_change(self):
        """When everyone scored the same, previous ranks match current."""
        managers = [
            _make_manager(name="Alice", entry_id=1, gw_points=50, total_points=500),
            _make_manager(name="Bob", entry_id=2, gw_points=50, total_points=400),
        ]
        _compute_standings_movement(managers)
        # Previous: Alice=450, Bob=350 -> Alice 1st, Bob 2nd (same as current)
        assert managers[0]["previous_rank"] == 1
        assert managers[1]["previous_rank"] == 2

    def test_dropped_manager_does_not_fabricate_movement(self):
        """A failed picks fetch must not renumber the managers below the gap.

        overall_rank comes from the unfiltered standings, so previous_rank has to
        be ranked over the same full league or every manager below the missing
        entry is reported as having dropped a place.
        """
        league_rows = [(i, 500 - i * 10, 50) for i in range(1, 6)]
        managers = [
            _make_manager(name=f"M{i}", entry_id=i, gw_points=50,
                          total_points=500 - i * 10, overall_rank=i)
            for i in (1, 2, 4, 5)  # entry 3 failed to fetch
        ]
        _compute_standings_movement(managers, league_rows)
        assert [m["previous_rank"] for m in managers] == [1, 2, 4, 5]
        assert all(m["previous_rank"] == m["overall_rank"] for m in managers)

    def test_tied_previous_totals_break_on_standings_order(self):
        """Managers level on the previous table keep the order the league
        itself put them in, rather than being reshuffled arbitrarily.

        (This is the shape GW1 would produce -- every previous total zero --
        but the collectors never call this function there: see
        TestFirstGameweekHasNoPreviousPosition.)"""
        league_rows = [(i, 500 + 80 - i, 80 - i) for i in range(1, 20)]
        managers = [
            _make_manager(name=f"M{i:02d}", entry_id=i, gw_points=80 - i,
                          total_points=500 + 80 - i, overall_rank=i)
            for i in range(1, 20)
        ]
        _compute_standings_movement(managers, league_rows)
        assert [m["previous_rank"] for m in managers] == list(range(1, 20))

    def test_tied_previous_totals_survive_a_dropped_manager(self):
        """The all-tied case must survive a manager dropping out mid-table:
        ranking survivors alone would renumber everyone below the gap."""
        league_rows = [(i, 500 + 80 - i, 80 - i) for i in range(1, 20)]
        managers = [
            _make_manager(name=f"M{i:02d}", entry_id=i, gw_points=80 - i,
                          total_points=500 + 80 - i, overall_rank=i)
            for i in range(1, 20) if i != 5
        ]
        _compute_standings_movement(managers, league_rows)
        assert [m["manager_name"] for m in managers
                if m["previous_rank"] != m["overall_rank"]] == []

    def test_a_league_that_started_late_has_no_movement_on_its_first_gameweek(self):
        """A league created at GW12 has no table before GW12 either, even
        though GW11 exists for the rest of FPL."""
        assert _has_previous_gameweek(12, 12) is False
        assert _has_previous_gameweek(13, 12) is True

    def test_fetched_points_take_precedence_over_standings_row(self):
        """use_net_points adjusts gw_points on the entry; the standings row must not win."""
        league_rows = [(1, 500, 60), (2, 450, 0)]
        managers = [
            _make_manager(name="Alice", entry_id=1, gw_points=40, total_points=500, overall_rank=1),
            _make_manager(name="Bob", entry_id=2, gw_points=0, total_points=450, overall_rank=2),
        ]
        _compute_standings_movement(managers, league_rows)
        # Alice's own figures give prev 460 (1st); the row's gross 60 would give 440 (2nd)
        assert managers[0]["previous_rank"] == 1
        assert managers[1]["previous_rank"] == 2

    def test_hit_this_gw_is_netted_out_of_previous_total(self):
        """total_points is always net of every hit; gw_points is gross unless
        use_net_points is on. Off, the hit must still be subtracted here or a
        manager who took it has their previous total over-credited."""
        managers = [
            _make_manager(name="Alice", entry_id=1, gw_points=50, total_points=500, transfer_cost=8),
            _make_manager(name="Bob", entry_id=2, gw_points=10, total_points=460, transfer_cost=0),
        ]
        _compute_standings_movement(managers)
        # True previous totals: Alice 500-42=458, Bob 460-10=450 -> Alice was 1st.
        # The unfixed formula (500-50=450 vs 460-10=450) would report a tie.
        assert managers[0]["previous_rank"] == 1
        assert managers[1]["previous_rank"] == 2

    def test_use_net_points_mode_does_not_double_subtract_the_hit(self):
        """With use_net_points on, gw_points is already net; subtracting
        transfer_cost again would double-count the hit."""
        managers = [
            _make_manager(name="Alice", entry_id=1, gw_points=42, total_points=500, transfer_cost=8),
            _make_manager(name="Bob", entry_id=2, gw_points=10, total_points=470, transfer_cost=0),
        ]
        _compute_standings_movement(managers, use_net_points=True)
        # True previous: Alice 500-42=458, Bob 470-10=460 -> Bob was 1st.
        # Double-subtracting the hit (500-(42-8)=466) would wrongly rank Alice 1st.
        assert managers[1]["previous_rank"] == 1
        assert managers[0]["previous_rank"] == 2


# ---------------------------------------------------------------------------
# Issue #147: the season's first gameweek has nothing to have moved from
# ---------------------------------------------------------------------------


class TestFirstGameweekHasNoPreviousPosition:
    """Neither collector may hand a GW1 manager a previous position.

    Derived, it is always their current one (every previous total is zero,
    so the tie-break gives everyone their place back); taken from draft's
    `last_rank`, it is whatever the API puts there before a table exists.
    Either way "held their position" is indistinguishable from "there was
    no previous gameweek" -- most damagingly in the ledger row, which
    outlives the API that could settle it.
    """

    async def test_classic_gw1_derives_no_previous_rank(self):
        standings = [
            {"entry": i, "player_name": f"M{i}", "event_total": 80 - i, "total": 80 - i}
            for i in range(1, 4)
        ]
        client = _FakeClassicClient(
            _standings_response(standings),
            {i: _picks_response(points=80 - i, total_points=80 - i) for i in range(1, 4)},
        )
        data = await collect_classic_recap_data(
            client, {"fpl": {"classic_league_id": 1}}, gw=1,
            live_stats={}, player_map={}, teams={}, is_live_gw=True,
        )
        assert [m["overall_rank"] for m in data["managers"]] == [1, 2, 3]
        assert all("previous_rank" not in m for m in data["managers"])

    async def test_classic_gw2_still_derives_movement(self):
        standings = [
            {"entry": 1, "player_name": "Alice", "event_total": 20, "total": 100},
            {"entry": 2, "player_name": "Bob", "event_total": 70, "total": 90},
        ]
        client = _FakeClassicClient(
            _standings_response(standings),
            {
                1: _picks_response(points=20, total_points=100),
                2: _picks_response(points=70, total_points=90),
            },
        )
        data = await collect_classic_recap_data(
            client, {"fpl": {"classic_league_id": 1}}, gw=2,
            live_stats={}, player_map={}, teams={}, is_live_gw=True,
        )
        # Previous totals: Alice 80, Bob 20 -- so Alice held 1st and Bob rose.
        by_name = {m["manager_name"]: m for m in data["managers"]}
        assert by_name["Alice"]["previous_rank"] == 1
        assert by_name["Bob"]["previous_rank"] == 2

    async def _draft_data(self, *, gw: int, last_rank: int | None):
        """One live draft manager. `last_rank=None` omits the field, which is
        what sends the collector to its derived-movement fallback instead of
        trusting the league's own figure."""
        draft_player = make_draft_player(id=900, web_name="Star", team=1, element_type=3)
        main_player = make_player(id=5, web_name="Star", team_id=1)
        client = MagicMock()
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock(return_value=False)
        client.get_league_details = AsyncMock(return_value={
            "league": {"name": "Draft League"},
            "standings": [
                {"league_entry": 10, "event_total": 52, "total": 500, "rank": 1}
                | ({} if last_rank is None else {"last_rank": last_rank}),
            ],
            "league_entries": [
                {"id": 10, "entry_id": 1, "player_first_name": "A", "player_last_name": "B"},
            ],
        })
        client.get_bootstrap_static = AsyncMock(return_value={"elements": [draft_player]})
        client.get_league_transactions = AsyncMock(return_value={"transactions": []})
        client.get_entry_picks = AsyncMock(
            return_value={"picks": [{"element": 900, "position": 1}], "subs": []},
        )
        with patch("fpl_cli.api.fpl_draft.FPLDraftClient", return_value=client):
            return await collect_draft_recap_data(
                {"fpl": {"draft_league_id": 1}}, gw=gw, live_stats={5: {"total_points": 52}},
                players=[main_player], teams={}, is_live_gw=True,
            )

    async def test_draft_gw1_ignores_the_api_last_rank(self):
        data = await self._draft_data(gw=1, last_rank=1)
        assert data["managers"][0]["overall_rank"] == 1
        assert "previous_rank" not in data["managers"][0]

    async def test_draft_gw2_carries_the_api_last_rank(self):
        data = await self._draft_data(gw=2, last_rank=3)
        assert data["managers"][0]["previous_rank"] == 3

    async def test_draft_gw1_derives_no_movement_when_the_api_omits_last_rank(self):
        """Without `last_rank` the collector falls back to deriving movement
        itself, which is the path GW1 breaks: every previous total is zero,
        so each manager would be handed back their own current position."""
        data = await self._draft_data(gw=1, last_rank=None)
        assert data["managers"][0]["overall_rank"] == 1
        assert "previous_rank" not in data["managers"][0]

    async def test_draft_gw2_derives_movement_when_the_api_omits_last_rank(self):
        """The same fallback still runs once a previous gameweek exists --
        otherwise the test above would pass with the derivation removed."""
        data = await self._draft_data(gw=2, last_rank=None)
        assert data["managers"][0]["previous_rank"] == 1

    async def test_draft_treats_a_zero_last_rank_as_no_previous_table(self):
        """Zero is the API's "nobody stood anywhere yet" sentinel, not a
        position -- stored as one it renders as a drop from 0th."""
        data = await self._draft_data(gw=2, last_rank=0)
        assert "previous_rank" not in data["managers"][0]


# ---------------------------------------------------------------------------
# Per-manager fetch concurrency
# ---------------------------------------------------------------------------


class TestManagerFetchConcurrency:
    async def test_both_network_calls_respect_the_concurrency_cap(self):
        """Picks and transfers are two calls per manager; both must hold the permit."""
        live = {"picks": 0, "transfers": 0}
        peak = {"picks": 0, "transfers": 0}

        # Transfers are held open far longer than picks so that, if they are not
        # throttled, managers released by the picks semaphore pile into the
        # transfers stage faster than it drains and the peak exceeds the cap.
        ticks = {"picks": 1, "transfers": 30}

        class _FakeClient:
            async def _tracked(self, kind: str) -> None:
                live[kind] += 1
                peak[kind] = max(peak[kind], live[kind])
                for _ in range(ticks[kind]):
                    await asyncio.sleep(0)
                live[kind] -= 1

            async def get_manager_picks(self, entry_id: int, gw: int) -> dict:
                await self._tracked("picks")
                return {
                    "picks": [],
                    "entry_history": {"event_transfers": 2, "event_transfers_cost": 0},
                    "active_chip": None,
                    "automatic_subs": [],
                }

            async def get_manager_transfers(self, entry_id: int) -> list:
                await self._tracked("transfers")
                return []

        standings = [
            {"entry": i, "player_name": f"M{i:02d}", "event_total": 50, "total": 50}
            for i in range(1, 20)
        ]
        managers = await _fetch_all_manager_data(
            _FakeClient(), standings, 2, {}, {}, {},
        )

        assert len(managers) == 19
        assert peak["picks"] <= _PICKS_CONCURRENCY
        assert peak["transfers"] <= _PICKS_CONCURRENCY


# ---------------------------------------------------------------------------
# U1: classic point-in-time headline numbers
# ---------------------------------------------------------------------------


class _FakeClassicClient:
    """Fake FPLClient covering exactly what collect_classic_recap_data uses."""

    def __init__(
        self,
        standings_response: dict,
        picks_by_entry: dict[int, dict],
        transfers_by_entry: dict[int, list] | None = None,
        history_by_entry: dict[int, dict] | None = None,
    ):
        self._standings_response = standings_response
        self._picks_by_entry = picks_by_entry
        self._transfers_by_entry = transfers_by_entry or {}
        self._history_by_entry = history_by_entry or {}

    async def get_classic_league_standings(self, league_id, page=1):
        return self._standings_response

    async def get_manager_picks(self, entry_id, gameweek):
        return self._picks_by_entry[entry_id]

    async def get_manager_transfers(self, entry_id):
        return self._transfers_by_entry.get(entry_id, [])

    async def get_manager_history(self, entry_id):
        return self._history_by_entry.get(entry_id, {"current": []})


def _standings_response(rows: list[dict], start_event: int | None = None) -> dict:
    league: dict[str, Any] = {"name": "Test League"}
    if start_event is not None:
        league["start_event"] = start_event
    return {"league": league, "standings": {"results": rows}}


def _picks_response(points: int, total_points: int, transfers_cost: int = 0) -> dict:
    return {
        "picks": [],
        "entry_history": {
            "points": points,
            "total_points": total_points,
            "event_transfers_cost": transfers_cost,
            "event_transfers": 0,
        },
        "active_chip": None,
        "automatic_subs": [],
    }


class TestClassicHeadlineNumberSourcing:
    async def test_ae1_replay_reports_entry_history_not_standings(self):
        """Covers AE1: standings carry only the always-current numbers;
        entry_history carries the point-in-time truth for a replay."""
        standings = [{"entry": 1, "player_name": "Alice", "event_total": 60, "total": 200}]
        client = _FakeClassicClient(
            _standings_response(standings),
            {1: _picks_response(points=45, total_points=120)},
        )
        data = await collect_classic_recap_data(
            client, {"fpl": {"classic_league_id": 1}}, gw=10,
            live_stats={}, player_map={}, teams={}, is_live_gw=False,
        )
        m = data["managers"][0]
        assert m["gw_points"] == 45
        assert m["gross_points"] == 45
        assert m["total_points"] == 120

    async def test_picks_fetch_failure_manager_absent_but_others_unaffected(self):
        """A manager whose picks fetch raises is dropped from `managers`;
        `_compute_standings_movement`'s existing league_rows fallback (not
        touched by U1) still accounts for them without renumbering others."""
        class _RaisingClient(_FakeClassicClient):
            async def get_manager_picks(self, entry_id, gameweek):
                if entry_id == 2:
                    raise RuntimeError("boom")
                return await super().get_manager_picks(entry_id, gameweek)

        standings = [
            {"entry": 1, "player_name": "Alice", "event_total": 50, "total": 500},
            {"entry": 2, "player_name": "Bob", "event_total": 40, "total": 480},
        ]
        client = _RaisingClient(
            _standings_response(standings),
            {1: _picks_response(points=50, total_points=500)},
        )
        data = await collect_classic_recap_data(
            client, {"fpl": {"classic_league_id": 1}}, gw=10,
            live_stats={}, player_map={}, teams={}, is_live_gw=True,
        )
        assert [m["manager_name"] for m in data["managers"]] == ["Alice"]

    async def test_gross_field_identical_regardless_of_use_net_points(self):
        standings = [{"entry": 1, "player_name": "Alice", "event_total": 50, "total": 500}]
        picks = {1: _picks_response(points=50, total_points=500, transfers_cost=8)}

        data_gross = await collect_classic_recap_data(
            _FakeClassicClient(_standings_response(standings), picks),
            {"fpl": {"classic_league_id": 1}, "use_net_points": False}, gw=10,
            live_stats={}, player_map={}, teams={}, is_live_gw=False,
        )
        data_net = await collect_classic_recap_data(
            _FakeClassicClient(_standings_response(standings), picks),
            {"fpl": {"classic_league_id": 1}, "use_net_points": True}, gw=10,
            live_stats={}, player_map={}, teams={}, is_live_gw=False,
        )
        assert data_gross["managers"][0]["gross_points"] == 50
        assert data_net["managers"][0]["gross_points"] == 50
        assert data_gross["managers"][0]["gw_points"] == 50
        assert data_net["managers"][0]["gw_points"] == 42

    async def test_missing_entry_history_falls_back_to_standings(self):
        standings = [{"entry": 1, "player_name": "Alice", "event_total": 33, "total": 333}]
        client = _FakeClassicClient(
            _standings_response(standings),
            {1: {"picks": [], "active_chip": None, "automatic_subs": []}},
        )
        data = await collect_classic_recap_data(
            client, {"fpl": {"classic_league_id": 1}}, gw=10,
            live_stats={}, player_map={}, teams={}, is_live_gw=True,
        )
        m = data["managers"][0]
        assert m["gross_points"] == 33
        assert m["total_points"] == 333

    async def test_reconciliation_does_not_run_for_a_past_gameweek(self):
        """A replay is allowed to diverge from (always-current) standings."""
        standings = [{"entry": 1, "player_name": "Alice", "event_total": 999, "total": 999}]
        client = _FakeClassicClient(
            _standings_response(standings),
            {1: _picks_response(points=45, total_points=120, transfers_cost=4)},
        )
        data = await collect_classic_recap_data(
            client, {"fpl": {"classic_league_id": 1}}, gw=5,
            live_stats={}, player_map={}, teams={}, is_live_gw=False,
        )
        assert data["managers"][0]["gross_points"] == 45

    async def test_reconciliation_raises_for_the_live_gameweek(self):
        standings = [{"entry": 1, "player_name": "Alice", "event_total": 999, "total": 999}]
        client = _FakeClassicClient(
            _standings_response(standings),
            {1: _picks_response(points=45, total_points=120, transfers_cost=4)},
        )
        with pytest.raises(RecapReconciliationError):
            await collect_classic_recap_data(
                client, {"fpl": {"classic_league_id": 1}}, gw=5,
                live_stats={}, player_map={}, teams={}, is_live_gw=True,
            )


class TestReconcileClassicHeadlineNumbers:
    def test_inconclusive_when_no_manager_took_a_hit(self, caplog):
        managers = [_make_manager(entry_id=1, gw_points=50, gross_points=50, transfer_cost=0, total_points=999)]
        standings = [{"entry": 1, "event_total": 999, "total": 999}]
        with caplog.at_level(logging.DEBUG, logger="fpl_cli.cli._league_recap_data"):
            _reconcile_classic_headline_numbers(managers, standings)  # must not raise
        assert "inconclusive" in caplog.text

    def test_raises_when_a_hit_taker_disagrees_naming_both_values(self):
        managers = [_make_manager(entry_id=1, gw_points=50, gross_points=50, transfer_cost=8, total_points=500)]
        standings = [{"entry": 1, "event_total": 45, "total": 500}]
        with pytest.raises(RecapReconciliationError) as exc_info:
            _reconcile_classic_headline_numbers(managers, standings)
        assert "50" in str(exc_info.value)
        assert "45" in str(exc_info.value)

    def test_passes_silently_when_hit_taker_agrees(self):
        managers = [_make_manager(entry_id=1, gw_points=50, gross_points=50, transfer_cost=8, total_points=500)]
        standings = [{"entry": 1, "event_total": 50, "total": 500}]
        _reconcile_classic_headline_numbers(managers, standings)  # must not raise

    def test_hit_taker_netted_in_standings_is_not_a_mapping_failure(self, caplog):
        """The FPL league table displays a gameweek net of the hit. A
        standings row exactly the hit lower than entry_history is that same
        gross/net difference, not the wrong field, and must not abort."""
        managers = [_make_manager(entry_id=1, gw_points=50, gross_points=50, transfer_cost=4, total_points=500)]
        standings = [{"entry": 1, "event_total": 46, "total": 500}]
        with caplog.at_level(logging.INFO, logger="fpl_cli.cli._league_recap_data"):
            _reconcile_classic_headline_numbers(managers, standings)  # must not raise
        assert "net of the hit" in caplog.text

    def test_gap_that_is_not_the_hit_still_raises(self):
        managers = [_make_manager(entry_id=1, gw_points=50, gross_points=50, transfer_cost=4, total_points=500)]
        standings = [{"entry": 1, "event_total": 30, "total": 500}]
        with pytest.raises(RecapReconciliationError):
            _reconcile_classic_headline_numbers(managers, standings)

    def test_cumulative_total_divergence_warns_not_raises(self, caplog):
        managers = [_make_manager(entry_id=1, gw_points=50, gross_points=50, transfer_cost=0, total_points=120)]
        standings = [{"entry": 1, "event_total": 50, "total": 500}]
        with caplog.at_level(logging.WARNING, logger="fpl_cli.cli._league_recap_data"):
            _reconcile_classic_headline_numbers(managers, standings)  # must not raise
        assert "divergence" in caplog.text


# ---------------------------------------------------------------------------
# U3: point-in-time league position
# ---------------------------------------------------------------------------


class TestDerivePointInTimePositions:
    def test_ranks_descending_by_total(self):
        result = derive_point_in_time_positions([(1, 100), (2, 150), (3, 90)])
        assert result == {2: 1, 1: 2, 3: 3}

    def test_entries_with_no_known_total_are_simply_omitted(self):
        result = derive_point_in_time_positions([(1, 100)])
        assert 2 not in result
        assert result == {1: 1}

    def test_ties_break_on_input_order_stably(self):
        totals = [(1, 100), (2, 100), (3, 90)]
        first = derive_point_in_time_positions(totals)
        second = derive_point_in_time_positions(totals)
        assert first == second
        assert first[1] == 1
        assert first[2] == 2
        assert first[3] == 3

    def test_empty_input_returns_empty_mapping(self):
        assert derive_point_in_time_positions([]) == {}


class TestClassicLeaguePosition:
    async def test_ae1_positions_derived_from_totals_not_standings_order(self):
        """Covers AE1/R13: standings order (by event_total) differs from the
        point-in-time total order; overall_rank follows the latter."""
        standings = [
            {"entry": 1, "player_name": "Alice", "event_total": 80, "total": 999},
            {"entry": 2, "player_name": "Bob", "event_total": 70, "total": 999},
            {"entry": 3, "player_name": "Charlie", "event_total": 60, "total": 999},
        ]
        picks = {
            1: _picks_response(points=80, total_points=300),
            2: _picks_response(points=70, total_points=320),
            3: _picks_response(points=60, total_points=310),
        }
        client = _FakeClassicClient(_standings_response(standings), picks)
        data = await collect_classic_recap_data(
            client, {"fpl": {"classic_league_id": 1}}, gw=15,
            live_stats={}, player_map={}, teams={}, is_live_gw=False,
        )
        by_name = {m["manager_name"]: m for m in data["managers"]}
        assert by_name["Bob"]["overall_rank"] == 1
        assert by_name["Charlie"]["overall_rank"] == 2
        assert by_name["Alice"]["overall_rank"] == 3

    async def test_no_start_event_makes_no_manager_history_call(self):
        standings = [{"entry": 1, "player_name": "Alice", "event_total": 50, "total": 500}]
        history_calls: list[int] = []

        class _TrackedClient(_FakeClassicClient):
            async def get_manager_history(self, entry_id):
                history_calls.append(entry_id)
                return await super().get_manager_history(entry_id)

        client = _TrackedClient(
            _standings_response(standings),  # no start_event
            {1: _picks_response(points=50, total_points=500)},
        )
        await collect_classic_recap_data(
            client, {"fpl": {"classic_league_id": 1}}, gw=10,
            live_stats={}, player_map={}, teams={}, is_live_gw=True,
        )
        assert history_calls == []

    async def test_start_event_offset_applied_from_manager_history_baseline(self):
        """A league starting at GW5: ranking uses total_points minus each
        manager's season total as of GW4, not the raw season-wide total --
        matching the standings' own (already league-scoped) totals."""
        standings = [
            {"entry": 1, "player_name": "Alice", "event_total": 50, "total": 200},
            {"entry": 2, "player_name": "Bob", "event_total": 40, "total": 210},
        ]
        # Season-wide order would put Alice ahead (500 > 480); league-scoped
        # order (matching the standings totals above) puts Bob ahead.
        picks = {
            1: _picks_response(points=50, total_points=500),
            2: _picks_response(points=40, total_points=480),
        }
        history = {
            1: {"current": [{"event": 4, "total_points": 300}]},
            2: {"current": [{"event": 4, "total_points": 270}]},
        }
        client = _FakeClassicClient(
            _standings_response(standings, start_event=5), picks, history_by_entry=history,
        )
        data = await collect_classic_recap_data(
            client, {"fpl": {"classic_league_id": 1}}, gw=10,
            live_stats={}, player_map={}, teams={}, is_live_gw=True,
        )
        by_name = {m["manager_name"]: m for m in data["managers"]}
        assert by_name["Bob"]["overall_rank"] == 1
        assert by_name["Alice"]["overall_rank"] == 2
        assert by_name["Bob"]["total_points"] == 210
        assert by_name["Alice"]["total_points"] == 200


class TestClassicPositionCohort:
    async def test_live_gw_failed_fetch_does_not_renumber_the_managers_below(self):
        """A manager whose picks fetch fails still holds their place in the
        real table; on a live capture the standings row stands in for them so
        everyone below keeps their true position."""
        class _RaisingClient(_FakeClassicClient):
            async def get_manager_picks(self, entry_id, gameweek):
                if entry_id == 2:
                    raise RuntimeError("boom")
                return await super().get_manager_picks(entry_id, gameweek)

        standings = [
            {"entry": 1, "player_name": "Alice", "event_total": 50, "total": 500},
            {"entry": 2, "player_name": "Bob", "event_total": 40, "total": 450},
            {"entry": 3, "player_name": "Cara", "event_total": 30, "total": 400},
        ]
        picks = {
            1: _picks_response(points=50, total_points=500),
            3: _picks_response(points=30, total_points=400),
        }
        data = await collect_classic_recap_data(
            _RaisingClient(_standings_response(standings), picks),
            {"fpl": {"classic_league_id": 1}}, gw=10,
            live_stats={}, player_map={}, teams={}, is_live_gw=True,
        )
        by_name = {m["manager_name"]: m for m in data["managers"]}
        assert by_name["Alice"]["overall_rank"] == 1
        # Cara is 3rd, not promoted to 2nd by Bob's absence.
        assert by_name["Cara"]["overall_rank"] == 3

    async def test_replay_failed_fetch_leaves_positions_unavailable(self):
        """On a replay the standings belong to a later gameweek, so they
        cannot stand in for the missing manager. Rather than rank one
        current-state total against point-in-time ones, no position is
        derived at all."""
        class _RaisingClient(_FakeClassicClient):
            async def get_manager_picks(self, entry_id, gameweek):
                if entry_id == 2:
                    raise RuntimeError("boom")
                return await super().get_manager_picks(entry_id, gameweek)

        standings = [
            {"entry": 1, "player_name": "Alice", "event_total": 50, "total": 500},
            {"entry": 2, "player_name": "Bob", "event_total": 40, "total": 450},
        ]
        picks = {1: _picks_response(points=50, total_points=300)}
        data = await collect_classic_recap_data(
            _RaisingClient(_standings_response(standings), picks),
            {"fpl": {"classic_league_id": 1}}, gw=10,
            live_stats={}, player_map={}, teams={}, is_live_gw=False,
        )
        alice = data["managers"][0]
        assert "overall_rank" not in alice
        assert "previous_rank" not in alice

    async def test_replay_with_a_complete_cohort_still_derives_positions(self):
        standings = [
            {"entry": 1, "player_name": "Alice", "event_total": 50, "total": 500},
            {"entry": 2, "player_name": "Bob", "event_total": 40, "total": 450},
        ]
        picks = {
            1: _picks_response(points=50, total_points=300),
            2: _picks_response(points=40, total_points=320),
        }
        data = await collect_classic_recap_data(
            _FakeClassicClient(_standings_response(standings), picks),
            {"fpl": {"classic_league_id": 1}}, gw=10,
            live_stats={}, player_map={}, teams={}, is_live_gw=False,
        )
        by_name = {m["manager_name"]: m for m in data["managers"]}
        assert by_name["Bob"]["overall_rank"] == 1
        assert by_name["Alice"]["overall_rank"] == 2

    async def test_gameweek_before_league_start_leaves_totals_unavailable(self):
        """Replaying GW3 of a league that started at GW5: there is no league
        table to place anyone on, and the GW4 baseline would drive every
        total negative and invert the order."""
        standings = [
            {"entry": 1, "player_name": "Alice", "event_total": 50, "total": 200},
            {"entry": 2, "player_name": "Bob", "event_total": 40, "total": 210},
        ]
        picks = {
            1: _picks_response(points=50, total_points=150),
            2: _picks_response(points=40, total_points=140),
        }
        history_calls: list[int] = []

        class _TrackedClient(_FakeClassicClient):
            async def get_manager_history(self, entry_id):
                history_calls.append(entry_id)
                return await super().get_manager_history(entry_id)

        client = _TrackedClient(
            _standings_response(standings, start_event=5), picks,
            history_by_entry={1: {"current": [{"event": 4, "total_points": 300}]}},
        )
        data = await collect_classic_recap_data(
            client, {"fpl": {"classic_league_id": 1}}, gw=3,
            live_stats={}, player_map={}, teams={}, is_live_gw=False,
        )
        assert history_calls == []
        for m in data["managers"]:
            assert "total_points" not in m
            assert "overall_rank" not in m


class TestApplyLeagueStartOffset:
    async def test_history_fetch_failure_drops_the_unscoped_total(self):
        """An un-offset season-wide total is on a different scale to the
        league-scoped ones: keeping it would rank that manager top and shift
        everyone below. It is dropped instead."""
        class _FailingHistoryClient:
            async def get_manager_history(self, entry_id):
                raise RuntimeError("network blip")

        managers = [_make_manager(entry_id=1, total_points=500)]
        await _apply_league_start_offset(_FailingHistoryClient(), managers, start_event=5)
        assert "total_points" not in managers[0]

    async def test_missing_baseline_row_drops_the_unscoped_total(self):
        class _NoBaselineClient:
            async def get_manager_history(self, entry_id):
                return {"current": [{"event": 6, "total_points": 999}]}

        managers = [_make_manager(entry_id=1, total_points=500)]
        await _apply_league_start_offset(_NoBaselineClient(), managers, start_event=5)
        assert "total_points" not in managers[0]

    async def test_one_failure_does_not_disturb_the_offset_managers(self):
        class _PartialClient:
            async def get_manager_history(self, entry_id):
                if entry_id == 2:
                    raise RuntimeError("network blip")
                return {"current": [{"event": 4, "total_points": 300}]}

        managers = [
            _make_manager(name="Alice", entry_id=1, total_points=500),
            _make_manager(name="Bob", entry_id=2, total_points=900),
        ]
        await _apply_league_start_offset(_PartialClient(), managers, start_event=5)
        assert managers[0]["total_points"] == 200
        assert "total_points" not in managers[1]


class TestStandingsMovementOptionalTotal:
    def test_manager_with_no_total_gets_no_previous_rank_and_others_unaffected(self):
        managers = [
            _make_manager(name="Alice", entry_id=1, gw_points=50, total_points=500),
            _make_manager(name="Bob", entry_id=2, gw_points=50, total_points=450),
            _make_manager(
                name="NoData", entry_id=3, gw_points=50,
                total_points=None, overall_rank=None, previous_rank=None,
            ),
        ]
        _compute_standings_movement(managers)
        assert "previous_rank" not in managers[2]
        # Alice prev=450, Bob prev=400 -> unaffected by NoData's exclusion.
        assert managers[0]["previous_rank"] == 1
        assert managers[1]["previous_rank"] == 2


class TestFormatStandingsBlockUnavailable:
    def test_missing_total_and_position_render_unavailable(self):
        """Covers AE8: an unreconstructable draft replay names both fields
        rather than rendering a blank or a zero."""
        managers = [
            _make_manager(
                name="Ghost", entry_id=1, gw_points=40,
                total_points=None, overall_rank=None, previous_rank=None,
            ),
        ]
        block = _format_standings_block(managers)
        assert "position unavailable" in block
        assert "total unavailable" in block

    def test_missing_total_only_still_renders_position(self):
        managers = [
            _make_manager(
                name="Ghost", entry_id=1, gw_points=40,
                total_points=None, overall_rank=3, previous_rank=None,
            ),
        ]
        block = _format_standings_block(managers)
        assert "3rd" in block
        assert "total unavailable" in block


# ---------------------------------------------------------------------------
# U2: draft point-in-time reconstruction
# ---------------------------------------------------------------------------


class TestDraftPointInTimeReconstruction:
    def _make_draft_client(self, league_details, bootstrap_elements, txns, picks_by_entry):
        client = MagicMock()
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock(return_value=False)
        client.get_league_details = AsyncMock(return_value=league_details)
        client.get_bootstrap_static = AsyncMock(return_value={"elements": bootstrap_elements})
        client.get_league_transactions = AsyncMock(return_value={"transactions": txns})
        client.get_entry_picks = AsyncMock(side_effect=lambda entry_id, gw: picks_by_entry[entry_id])
        return client

    def _league_details(self, standings, entries):
        return {"league": {"name": "Draft League"}, "standings": standings, "league_entries": entries}

    async def test_ae1_computed_points_override_standings_event_total(self):
        """Covers AE1: event_total is always-current; the squad summed
        against live stats is the point-in-time truth."""
        draft_player = make_draft_player(id=900, web_name="Star", team=1, element_type=3)
        main_player = make_player(id=5, web_name="Star", team_id=1)
        league_details = self._league_details(
            standings=[{"league_entry": 10, "event_total": 70, "total": 500}],
            entries=[{"id": 10, "entry_id": 1, "player_first_name": "A", "player_last_name": "B"}],
        )
        picks = {1: {"picks": [{"element": 900, "position": 1}], "subs": []}}
        client = self._make_draft_client(league_details, [draft_player], [], picks)

        with patch("fpl_cli.api.fpl_draft.FPLDraftClient", return_value=client):
            data = await collect_draft_recap_data(
                {"fpl": {"draft_league_id": 1}}, gw=15, live_stats={5: {"total_points": 52}},
                players=[main_player], teams={}, is_live_gw=False,
            )
        m = data["managers"][0]
        assert m["gw_points"] == 52
        assert m["gross_points"] == 52

    async def test_ae8_replay_leaves_total_and_position_unavailable(self):
        """Covers AE8: no ledger exists yet in Phase A, so a replay can never
        reconstruct a cumulative total or a league position for draft."""
        draft_player = make_draft_player(id=900, web_name="Star", team=1, element_type=3)
        main_player = make_player(id=5, web_name="Star", team_id=1)
        league_details = self._league_details(
            standings=[{"league_entry": 10, "event_total": 52, "total": 500}],
            entries=[{"id": 10, "entry_id": 1, "player_first_name": "A", "player_last_name": "B"}],
        )
        picks = {1: {"picks": [{"element": 900, "position": 1}], "subs": []}}
        client = self._make_draft_client(league_details, [draft_player], [], picks)

        with patch("fpl_cli.api.fpl_draft.FPLDraftClient", return_value=client):
            data = await collect_draft_recap_data(
                {"fpl": {"draft_league_id": 1}}, gw=15, live_stats={5: {"total_points": 52}},
                players=[main_player], teams={}, is_live_gw=False,
            )
        m = data["managers"][0]
        assert "total_points" not in m
        assert "overall_rank" not in m
        block = _format_standings_block(data["managers"])
        assert "position unavailable" in block
        assert "total unavailable" in block

    async def test_reconciliation_silent_when_computed_matches_standings(self):
        draft_player = make_draft_player(id=900, web_name="Star", team=1, element_type=3)
        main_player = make_player(id=5, web_name="Star", team_id=1)
        league_details = self._league_details(
            standings=[{"league_entry": 10, "event_total": 52, "total": 500}],
            entries=[{"id": 10, "entry_id": 1, "player_first_name": "A", "player_last_name": "B"}],
        )
        picks = {1: {"picks": [{"element": 900, "position": 1}], "subs": []}}
        client = self._make_draft_client(league_details, [draft_player], [], picks)

        with patch("fpl_cli.api.fpl_draft.FPLDraftClient", return_value=client):
            data = await collect_draft_recap_data(
                {"fpl": {"draft_league_id": 1}}, gw=15, live_stats={5: {"total_points": 52}},
                players=[main_player], teams={}, is_live_gw=True,
            )
        assert data["managers"][0]["gw_points"] == 52
        assert data["managers"][0]["total_points"] == 500

    async def test_reconciliation_raises_on_divergence_naming_manager(self):
        draft_player = make_draft_player(id=900, web_name="Star", team=1, element_type=3)
        main_player = make_player(id=5, web_name="Star", team_id=1)
        league_details = self._league_details(
            standings=[{"league_entry": 10, "event_total": 999, "total": 500}],
            entries=[{"id": 10, "entry_id": 1, "player_first_name": "A", "player_last_name": "B"}],
        )
        picks = {1: {"picks": [{"element": 900, "position": 1}], "subs": []}}
        client = self._make_draft_client(league_details, [draft_player], [], picks)

        with patch("fpl_cli.api.fpl_draft.FPLDraftClient", return_value=client):
            with pytest.raises(RecapReconciliationError) as exc_info:
                await collect_draft_recap_data(
                    {"fpl": {"draft_league_id": 1}}, gw=15, live_stats={5: {"total_points": 52}},
                    players=[main_player], teams={}, is_live_gw=True,
                )
        message = str(exc_info.value)
        assert "999" in message
        assert "52" in message
        assert "A B" in message

    async def test_unmatched_player_marked_and_not_silently_zero(self):
        """A draft player whose web-name/team match to a main player fails
        carries the unmatched marker rather than a silent, indistinguishable
        zero (U4's row shape relies on this to detect the difference)."""
        draft_player = make_draft_player(id=900, web_name="Mystery", team=1, element_type=3)
        league_details = self._league_details(
            standings=[{"league_entry": 10, "event_total": 0, "total": 500}],
            entries=[{"id": 10, "entry_id": 1, "player_first_name": "A", "player_last_name": "B"}],
        )
        picks = {1: {"picks": [{"element": 900, "position": 1}], "subs": []}}
        client = self._make_draft_client(league_details, [draft_player], [], picks)

        with patch("fpl_cli.api.fpl_draft.FPLDraftClient", return_value=client):
            data = await collect_draft_recap_data(
                {"fpl": {"draft_league_id": 1}}, gw=15, live_stats={},
                players=[], teams={}, is_live_gw=True,
            )
        squad_player = data["managers"][0]["squad"][0]
        assert squad_player["unmatched"] is True
        assert squad_player["points"] == 0

    async def test_unmatched_player_shortfall_does_not_abort_the_recap(self):
        """One name/team mapping miss makes the computed sum short by
        construction. That is a known gap in the mapping, not the wrong
        field, so the live gameweek falls back to the standings number
        instead of killing the whole recap."""
        matched = make_draft_player(id=900, web_name="Star", team=1, element_type=3)
        mystery = make_draft_player(id=901, web_name="Mystery", team=1, element_type=3)
        main_player = make_player(id=5, web_name="Star", team_id=1)
        league_details = self._league_details(
            standings=[{"league_entry": 10, "event_total": 70, "total": 500}],
            entries=[{"id": 10, "entry_id": 1, "player_first_name": "A", "player_last_name": "B"}],
        )
        picks = {1: {"picks": [
            {"element": 900, "position": 1},
            {"element": 901, "position": 2},
        ], "subs": []}}
        client = self._make_draft_client(league_details, [matched, mystery], [], picks)

        with patch("fpl_cli.api.fpl_draft.FPLDraftClient", return_value=client):
            data = await collect_draft_recap_data(
                {"fpl": {"draft_league_id": 1}}, gw=15, live_stats={5: {"total_points": 52}},
                players=[main_player], teams={}, is_live_gw=True,
            )
        m = data["managers"][0]
        assert m["gw_points"] == 70
        assert [p["unmatched"] for p in m["squad"]] == [False, True]

    async def test_live_gw_keeps_the_leagues_own_rank_over_a_derived_one(self):
        """Draft h2h scores league points, so ties on `total` are common and
        the API breaks them on its own tiebreaks. A live capture uses the
        league's rank/last_rank rather than re-sorting `total`."""
        draft_player = make_draft_player(id=900, web_name="Star", team=1, element_type=3)
        main_player = make_player(id=5, web_name="Star", team_id=1)
        league_details = self._league_details(
            standings=[
                {"league_entry": 10, "event_total": 52, "total": 6, "rank": 2, "last_rank": 1},
                {"league_entry": 11, "event_total": 52, "total": 6, "rank": 1, "last_rank": 3},
            ],
            entries=[
                {"id": 10, "entry_id": 1, "player_first_name": "A", "player_last_name": "B"},
                {"id": 11, "entry_id": 2, "player_first_name": "C", "player_last_name": "D"},
            ],
        )
        picks = {
            1: {"picks": [{"element": 900, "position": 1}], "subs": []},
            2: {"picks": [{"element": 900, "position": 1}], "subs": []},
        }
        client = self._make_draft_client(league_details, [draft_player], [], picks)

        with patch("fpl_cli.api.fpl_draft.FPLDraftClient", return_value=client):
            data = await collect_draft_recap_data(
                {"fpl": {"draft_league_id": 1}}, gw=15, live_stats={5: {"total_points": 52}},
                players=[main_player], teams={}, is_live_gw=True,
            )
        by_name = {m["manager_name"]: m for m in data["managers"]}
        assert by_name["A B"]["overall_rank"] == 2
        assert by_name["C D"]["overall_rank"] == 1
        assert by_name["A B"]["previous_rank"] == 1
        assert by_name["C D"]["previous_rank"] == 3

    async def test_standings_without_ranks_still_derive_movement(self):
        draft_player = make_draft_player(id=900, web_name="Star", team=1, element_type=3)
        main_player = make_player(id=5, web_name="Star", team_id=1)
        league_details = self._league_details(
            standings=[{"league_entry": 10, "event_total": 52, "total": 500}],
            entries=[{"id": 10, "entry_id": 1, "player_first_name": "A", "player_last_name": "B"}],
        )
        picks = {1: {"picks": [{"element": 900, "position": 1}], "subs": []}}
        client = self._make_draft_client(league_details, [draft_player], [], picks)

        with patch("fpl_cli.api.fpl_draft.FPLDraftClient", return_value=client):
            data = await collect_draft_recap_data(
                {"fpl": {"draft_league_id": 1}}, gw=15, live_stats={5: {"total_points": 52}},
                players=[main_player], teams={}, is_live_gw=True,
            )
        assert data["managers"][0]["previous_rank"] == 1


# ---------------------------------------------------------------------------
# compute_classic_awards
# ---------------------------------------------------------------------------


class TestClassicAwardsIntegration:
    def test_includes_transfer_awards(self):
        transfers = [
            RecapTransfer(
                player_in="Star", player_in_team="ARS", player_in_points=20,
                player_out="Bench", player_out_team="BOU", player_out_points=1,
                net=19, cost=0,
            ),
        ]
        managers = [
            _make_manager(name="Alice", transfers=transfers),
            _make_manager(name="Bob"),
        ]
        awards = _compute_shared_awards(managers, format_name="classic")
        assert "transfer_genius" in awards


# ---------------------------------------------------------------------------
# Waiver awards (draft)
# ---------------------------------------------------------------------------


def _make_manager_with_txns(
    name: str,
    transactions: list[RecapDraftTransaction],
    **kwargs,
) -> RecapManagerEntry:
    m = _make_manager(name=name, **kwargs)
    m["transactions"] = transactions
    return m


class TestWaiverAwards:
    def test_waiver_genius(self):
        txns = [
            RecapDraftTransaction(
                player_in="Star", player_in_team="ARS", player_in_points=18,
                player_out="Dud", player_out_team="LEI", player_out_points=1,
                net=17, kind="w",
            ),
        ]
        managers = [
            _make_manager_with_txns("Alice", txns),
            _make_manager(name="Bob"),
        ]
        awards: RecapAwards = {}  # type: ignore[typeddict-item]
        _compute_waiver_awards(managers, awards)
        assert "waiver_genius" in awards
        assert awards.get("waiver_genius", {}).get("manager_name") == "Alice"  # type: ignore[union-attr]

    def test_waiver_disaster(self):
        txns = [
            RecapDraftTransaction(
                player_in="Flop", player_in_team="WHU", player_in_points=0,
                player_out="Legend", player_out_team="MCI", player_out_points=15,
                net=-15, kind="w",
            ),
        ]
        managers = [
            _make_manager_with_txns("Alice", txns),
            _make_manager(name="Bob"),
        ]
        awards: RecapAwards = {}  # type: ignore[typeddict-item]
        _compute_waiver_awards(managers, awards)
        assert "waiver_disaster" in awards
        assert awards.get("waiver_disaster", {}).get("manager_name") == "Alice"  # type: ignore[union-attr]

    def test_disaster_detail_names_chain_endpoints_not_intermediate(self):
        # Manager runs Trossard out → Georginio → Dango. The literal worst raw
        # txn is Georginio-for-Trossard (-6), but Trossard's real replacement
        # is Dango (-1). The detail should name the endpoint pair.
        txns = [
            RecapDraftTransaction(
                player_in="Georginio", player_in_team="???", player_in_points=1,
                player_out="Trossard", player_out_team="???", player_out_points=7,
                net=-6, kind="w",
            ),
            RecapDraftTransaction(
                player_in="Dango", player_in_team="???", player_in_points=6,
                player_out="Georginio", player_out_team="???", player_out_points=1,
                net=5, kind="f",
            ),
            # Add a clear loss elsewhere so manager total is negative
            RecapDraftTransaction(
                player_in="Bust", player_in_team="???", player_in_points=0,
                player_out="Star", player_out_team="???", player_out_points=10,
                net=-10, kind="w",
            ),
        ]
        managers = [
            _make_manager_with_txns("Alice", txns),
            _make_manager(name="Bob"),
        ]
        awards: RecapAwards = {}  # type: ignore[typeddict-item]
        _compute_waiver_awards(managers, awards)
        detail = awards["waiver_disaster"]["detail"]
        assert "Bust for Star" in detail  # the real worst (-10), unaffected by chain
        assert "Georginio" not in detail  # the intermediate must not appear

    def test_no_transactions_no_awards(self):
        managers = [_make_manager(name="Alice"), _make_manager(name="Bob")]
        awards: RecapAwards = {}  # type: ignore[typeddict-item]
        _compute_waiver_awards(managers, awards)
        assert "waiver_genius" not in awards
        assert "waiver_disaster" not in awards

    def test_draft_awards_via_shared(self):
        txns = [
            RecapDraftTransaction(
                player_in="Pick", player_in_team="EVE", player_in_points=10,
                player_out="Drop", player_out_team="NFO", player_out_points=3,
                net=7, kind="w",
            ),
        ]
        managers = [
            _make_manager_with_txns("Alice", txns),
            _make_manager(name="Bob"),
        ]
        awards = _compute_shared_awards(managers, format_name="draft")
        assert "waiver_genius" in awards
        assert "transfer_genius" not in awards
        assert "best_captain" not in awards
        assert "worst_captain" not in awards

    def test_waiver_genius_value_is_aggregate_with_no_hit_clause(self):
        # Draft has no transfer cost, so the detail should never mention "hit".
        txns = [
            RecapDraftTransaction(
                player_in="A", player_in_team="ARS", player_in_points=10,
                player_out="B", player_out_team="TOT", player_out_points=2,
                net=8, kind="w",
            ),
            RecapDraftTransaction(
                player_in="C", player_in_team="CHE", player_in_points=7,
                player_out="D", player_out_team="WHU", player_out_points=0,
                net=7, kind="w",
            ),
        ]
        managers = [
            _make_manager_with_txns("Alice", txns),
            _make_manager(name="Bob"),
        ]
        awards: RecapAwards = {}  # type: ignore[typeddict-item]
        _compute_waiver_awards(managers, awards)
        assert awards["waiver_genius"]["value"] == 15
        detail = awards["waiver_genius"]["detail"]
        assert "gained 15 net pts overall" in detail
        assert "+15 raw across 2 waivers" in detail
        assert "hit" not in detail
        assert "Best: A for B (+8)" in detail
        assert "also C for D (+7)" in detail

    def test_waiver_disaster_compact_format(self):
        # Single negative-net waiver. Verify the new helper format applies on
        # the waiver disaster path: compact (no "raw across" suffix), no
        # "hit" mention (waivers have no transfer cost).
        txns = [
            RecapDraftTransaction(
                player_in="Flop", player_in_team="WHU", player_in_points=0,
                player_out="Legend", player_out_team="MCI", player_out_points=8,
                net=-8, kind="w",
            ),
        ]
        managers = [
            _make_manager_with_txns("Alice", txns),
            _make_manager(name="Bob"),
        ]
        awards: RecapAwards = {}  # type: ignore[typeddict-item]
        _compute_waiver_awards(managers, awards)
        assert awards["waiver_disaster"]["value"] == -8
        detail = awards["waiver_disaster"]["detail"]
        assert "Alice lost 8 net pts overall." in detail
        assert "Worst: Flop for Legend (-8)." in detail
        assert "raw across" not in detail
        assert "hit" not in detail

    def test_waiver_detail_caps_at_three(self):
        txns = [
            RecapDraftTransaction(
                player_in=f"In{i}", player_in_team="ARS", player_in_points=10 - i,
                player_out=f"Out{i}", player_out_team="TOT", player_out_points=0,
                net=10 - i, kind="w",
            )
            for i in range(5)
        ]
        managers = [
            _make_manager_with_txns("Alice", txns),
            _make_manager(name="Bob"),
        ]
        awards: RecapAwards = {}  # type: ignore[typeddict-item]
        _compute_waiver_awards(managers, awards)
        detail = awards["waiver_genius"]["detail"]
        assert "Best: In0 for Out0 (+10)" in detail
        assert "2 more omitted" in detail
        assert "In4" not in detail


def _txn(pin: str, pin_pts: int, pout: str, pout_pts: int, kind: str = "w") -> RecapDraftTransaction:
    return RecapDraftTransaction(
        player_in=pin, player_in_team="???", player_in_points=pin_pts,
        player_out=pout, player_out_team="???", player_out_points=pout_pts,
        net=pin_pts - pout_pts, kind=kind,
    )


class TestContractDraftTxnChains:
    """Chain rebuilds within a manager-GW collapse to endpoint pairs."""

    def test_no_chain_passthrough(self):
        txns = [_txn("Truffert", 7, "Frimpong", 1), _txn("Doku", 15, "Gordon", 0)]
        assert _contract_draft_txn_chains(txns) == txns

    def test_single_txn_passthrough(self):
        txns = [_txn("Doku", 15, "Gordon", 0)]
        assert _contract_draft_txn_chains(txns) == txns

    def test_collapses_three_step_chain(self):
        # Trossard out → Georginio → Dango (Oliver's GW35)
        txns = [
            _txn("Truffert", 7, "Frimpong", 1),
            _txn("Georginio", 1, "Trossard", 7),
            _txn("Dango", 6, "Georginio", 1, kind="f"),
        ]
        result = _contract_draft_txn_chains(txns)
        # Expect Truffert/Frimpong unchanged, plus Dango/Trossard collapsed
        assert len(result) == 2
        names = {(t["player_in"], t["player_out"]): t["net"] for t in result}
        assert names[("Truffert", "Frimpong")] == 6
        assert names[("Dango", "Trossard")] == 6 - 7  # -1
        # Sum is preserved
        assert sum(t["net"] for t in result) == sum(t["net"] for t in txns)

    def test_closed_loop_contracts_to_nothing(self):
        # Drop X for Y, then drop Y for X — back where we started
        txns = [_txn("Y", 5, "X", 3), _txn("X", 3, "Y", 5)]
        assert _contract_draft_txn_chains(txns) == []

    def test_multi_hop_chain(self):
        # A→B→C→D: only A out, D in survive
        txns = [
            _txn("B", 4, "A", 1),
            _txn("C", 6, "B", 4),
            _txn("D", 9, "C", 6),
        ]
        result = _contract_draft_txn_chains(txns)
        assert len(result) == 1
        assert result[0]["player_in"] == "D"
        assert result[0]["player_out"] == "A"
        assert result[0]["net"] == 9 - 1


class TestBucketDraftTxns:
    """Regression: draft txn `entry` field is FPL entry_id, not league_entry id."""

    def test_remaps_entry_id_to_league_entry_id(self):
        league_entries = [
            {"id": 1528, "entry_id": 1528, "player_first_name": "Oliver"},
            {"id": 94885, "entry_id": 97719, "player_first_name": "Alex"},
            {"id": 93633, "entry_id": 96472, "player_first_name": "Jonathan"},
        ]
        gw_txns = [
            {"entry": 1528, "element_in": 1, "element_out": 2, "kind": "w"},
            {"entry": 97719, "element_in": 3, "element_out": 4, "kind": "w"},
            {"entry": 97719, "element_in": 5, "element_out": 6, "kind": "w"},
            {"entry": 96472, "element_in": 7, "element_out": 8, "kind": "w"},
        ]

        bucketed = _bucket_draft_txns_by_league_entry(gw_txns, league_entries)

        assert set(bucketed.keys()) == {1528, 94885, 93633}
        assert len(bucketed[94885]) == 2
        assert len(bucketed[93633]) == 1
        assert len(bucketed[1528]) == 1

    def test_drops_unknown_entry_ids(self):
        league_entries = [{"id": 100, "entry_id": 200}]
        gw_txns = [
            {"entry": 200, "element_in": 1},
            {"entry": 999, "element_in": 2},
            {"entry": None, "element_in": 3},
        ]

        bucketed = _bucket_draft_txns_by_league_entry(gw_txns, league_entries)

        assert bucketed == {100: [{"entry": 200, "element_in": 1}]}


# ---------------------------------------------------------------------------
# Fines evaluation
# ---------------------------------------------------------------------------


class TestEvaluateLeagueFines:
    def _settings_with_fines(self, rules: list[dict] | None = None) -> dict:
        """Build settings with fines configured."""
        if rules is None:
            rules = [{"type": "last-place", "penalty": "Pint on video"}]
        return {"fines": {"classic": rules, "draft": rules}}

    def test_no_fines_config_returns_empty(self):
        managers = [_make_manager(name="Alice")]
        result = evaluate_league_fines(managers, {}, "classic")
        assert result == []

    def test_last_place_fine_triggered(self):
        squad = [_make_squad_player(name=f"P{i}") for i in range(11)]
        managers = [
            _make_manager(name="Alice", gw_points=80, squad=squad),
            _make_manager(name="Bob", gw_points=20, entry_id=2, squad=squad),
        ]
        result = evaluate_league_fines(managers, self._settings_with_fines(), "classic")
        assert len(result) == 1
        assert result[0]["manager_name"] == "Bob"
        assert result[0]["rule_type"] == "last-place"
        assert "Finished last" in result[0]["message"]
        assert "Pint on video" in result[0]["message"]

    def test_red_card_fine_triggered(self):
        red_player = _make_squad_player(name="Hothead", red_cards=1, contributed=True)
        squad = [red_player] + [_make_squad_player(name=f"P{i}") for i in range(10)]
        rules = [{"type": "red-card", "penalty": "Buy the round"}]
        managers = [
            _make_manager(name="Alice", squad=squad),
        ]
        result = evaluate_league_fines(managers, self._settings_with_fines(rules), "classic")
        assert len(result) == 1
        assert result[0]["rule_type"] == "red-card"
        assert "Hothead" in result[0]["message"]

    def test_red_card_fine_triggers_on_bench_boost_bench_slot(self):
        # BB-flagged bench slot counts as contributed; a red card there must fine.
        bb_bench_red = _make_squad_player(
            name="BBRed", red_cards=1, contributed=True, is_bench_boost_player=True,
        )
        squad = [bb_bench_red] + [_make_squad_player(name=f"P{i}") for i in range(10)]
        rules = [{"type": "red-card", "penalty": "Round"}]
        managers = [_make_manager(name="Alice", active_chip="bboost", squad=squad)]
        result = evaluate_league_fines(managers, self._settings_with_fines(rules), "classic")
        assert len(result) == 1
        assert result[0]["rule_type"] == "red-card"
        assert "BBRed" in result[0]["message"]

    def test_red_card_on_non_bb_bench_does_not_trigger(self):
        # Pre-existing behaviour: benched red card without BB is not fined.
        bench_red = _make_squad_player(name="BenchRed", red_cards=1, contributed=False)
        squad = [bench_red] + [_make_squad_player(name=f"P{i}") for i in range(10)]
        rules = [{"type": "red-card", "penalty": "Round"}]
        managers = [_make_manager(name="Alice", squad=squad)]
        result = evaluate_league_fines(managers, self._settings_with_fines(rules), "classic")
        assert result == []

    def test_fines_failure_for_one_manager_doesnt_affect_others(self):
        squad = [_make_squad_player(name=f"P{i}") for i in range(11)]
        managers = [
            _make_manager(name="Alice", gw_points=80, squad=squad),
            _make_manager(name="Bob", gw_points=20, entry_id=2, squad=[]),
        ]
        # Bob has empty squad - fines eval might fail for edge cases, but shouldn't crash
        result = evaluate_league_fines(managers, self._settings_with_fines(), "classic")
        # Should still get Alice's result (not last place, so no fine for her)
        # Bob is last place and should get fined despite empty squad
        assert any(r["manager_name"] == "Bob" for r in result)

    def test_multiple_fines_for_same_manager(self):
        red_player = _make_squad_player(name="Hothead", red_cards=1, contributed=True)
        squad = [red_player] + [_make_squad_player(name=f"P{i}") for i in range(10)]
        rules = [
            {"type": "last-place", "penalty": "Pint"},
            {"type": "red-card", "penalty": "Round"},
        ]
        managers = [
            _make_manager(name="Alice", gw_points=80, squad=squad),
            _make_manager(name="Bob", gw_points=20, entry_id=2, squad=squad),
        ]
        result = evaluate_league_fines(managers, self._settings_with_fines(rules), "classic")
        bob_fines = [r for r in result if r["manager_name"] == "Bob"]
        # Bob should get last-place fine and red-card fine
        assert len(bob_fines) >= 1  # At least last-place
        rule_types = {f["rule_type"] for f in bob_fines}
        assert "last-place" in rule_types


# ---------------------------------------------------------------------------
# CLI stop condition
# ---------------------------------------------------------------------------


class TestLeagueRecapCommandStopCondition:
    def test_reconciliation_failure_exits_non_zero(self):
        """A reconciliation failure is a stop condition, so the command must
        exit non-zero -- a scripted caller (gw-prep) has no other way to tell
        it apart from a successful run."""
        from click.testing import CliRunner

        from fpl_cli.cli.league_recap import league_recap_command

        client = MagicMock()
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock(return_value=False)
        client.get_players = AsyncMock(return_value=[])
        client.get_teams = AsyncMock(return_value=[])
        client.get_gameweek_live = AsyncMock(return_value={"elements": []})
        client.get_fixtures = AsyncMock(return_value=[])
        client.get_gameweeks = AsyncMock(return_value=[{"id": 5, "finished": True}])

        with (
            patch("fpl_cli.cli.league_recap.load_settings", return_value={"fpl": {"classic_league_id": 1}}),
            patch("fpl_cli.api.fpl.FPLClient", return_value=client),
            patch("fpl_cli.cli.review._review_resolve_gw", AsyncMock(return_value={"gw": 5})),
            patch(
                "fpl_cli.cli._league_recap_data.collect_classic_recap_data",
                AsyncMock(side_effect=RecapReconciliationError("numbers disagree")),
            ),
        ):
            result = CliRunner().invoke(league_recap_command, [])

        assert result.exit_code != 0
        assert "numbers disagree" in result.output


# ---------------------------------------------------------------------------
# LLM prompt formatting
# ---------------------------------------------------------------------------


def _make_recap_data(managers=None, awards=None, fines=None):
    """Build a minimal LeagueRecapData for prompt tests."""
    from fpl_cli.cli._league_recap_types import LeagueRecapData, RecapAwards

    data = LeagueRecapData(
        gameweek=10,
        league_name="Test League",
        fpl_format="classic",
        managers=managers or [],
        awards=awards or RecapAwards(),
    )
    if fines is not None:
        data["fines"] = fines
    return data


class TestPromptFormatting:
    def test_awards_context_includes_winners(self):
        awards = _compute_shared_awards([
            _make_manager(name="Alice", gw_points=80, captain="Salah", captain_points=15),
            _make_manager(name="Bob", gw_points=30, captain="Haaland", captain_points=2),
        ])
        data = _make_recap_data(awards=awards)
        text = format_recap_awards_context(data)
        assert "Alice" in text
        assert "Gw Winner" in text

    def test_standings_context_includes_movement(self):
        managers = [
            _make_manager(name="Alice", entry_id=1, gw_points=80, total_points=500, overall_rank=1, previous_rank=2),
            _make_manager(name="Bob", entry_id=2, gw_points=30, total_points=400, overall_rank=2, previous_rank=1),
        ]
        data = _make_recap_data(managers=managers)
        text = format_recap_standings_context(data)
        assert "Alice" in text
        assert "Bob" in text
        assert "|" in text  # markdown table

    def test_standings_context_sorts_unranked_last_like_the_report(self):
        """A manager with no derivable position sorts after every ranked one
        in both the prompt table and the rendered standings block -- the two
        must not disagree on order."""
        managers = [
            _make_manager(name="Ghost", entry_id=1, gw_points=60, total_points=None,
                          overall_rank=None, previous_rank=None),
            _make_manager(name="Alice", entry_id=2, gw_points=50, overall_rank=1, previous_rank=1),
        ]
        text = format_recap_standings_context(_make_recap_data(managers=managers))
        assert text.index("Alice") < text.index("Ghost")

    def test_standings_context_shows_chip_for_classic(self):
        managers = [
            _make_manager(name="Alice", entry_id=1, gw_points=80, total_points=500, overall_rank=1, active_chip="WC"),
            _make_manager(name="Bob", entry_id=2, gw_points=30, total_points=400, overall_rank=2),
        ]
        data = _make_recap_data(managers=managers)
        text = format_recap_standings_context(data)
        assert "[WC]" in text
        assert "Alice [WC]" in text
        # Bob has no chip - no tag
        assert "Bob [" not in text

    def test_standings_context_hides_chip_for_draft(self):
        managers = [
            _make_manager(name="Alice", entry_id=1, gw_points=80, total_points=500, overall_rank=1, active_chip="WC"),
        ]
        data = _make_recap_data(managers=managers)
        data["fpl_format"] = "draft"
        text = format_recap_standings_context(data)
        assert "[WC]" not in text

    def test_chips_context_groups_by_chip_with_counts(self):
        # Deliberately unsorted input — formatter must order by gw_points desc within each chip
        managers = [
            _make_manager(name="Ross", entry_id=4, gw_points=46, active_chip="WC"),
            _make_manager(name="Ed", entry_id=2, gw_points=58, active_chip="WC"),
            _make_manager(name="Cam", entry_id=1, gw_points=70, active_chip="WC"),
            _make_manager(name="Oliver", entry_id=3, gw_points=55, active_chip="WC"),
            _make_manager(name="Walter", entry_id=6, gw_points=76, active_chip="BB"),
            _make_manager(name="Matt", entry_id=5, gw_points=85, active_chip="BB"),
            _make_manager(name="Alex", entry_id=7, gw_points=60),
        ]
        data = _make_recap_data(managers=managers)
        text = format_recap_chips_context(data)
        assert "Wildcard" in text and "(4)" in text
        assert "Bench Boost" in text and "(2)" in text
        assert "Alex" not in text
        # Names must appear in descending gw_points order within each chip group
        wc_line = next(line for line in text.splitlines() if "Wildcard" in line)
        assert wc_line.index("Cam") < wc_line.index("Ed") < wc_line.index("Oliver") < wc_line.index("Ross")
        bb_line = next(line for line in text.splitlines() if "Bench Boost" in line)
        assert bb_line.index("Matt") < bb_line.index("Walter")

    def test_chips_context_empty_when_no_chips(self):
        managers = [_make_manager(name="Alex", entry_id=1, gw_points=60)]
        data = _make_recap_data(managers=managers)
        assert format_recap_chips_context(data) == ""

    def test_chips_context_empty_for_draft(self):
        managers = [_make_manager(name="Cam", entry_id=1, gw_points=70, active_chip="WC")]
        data = _make_recap_data(managers=managers)
        data["fpl_format"] = "draft"
        assert format_recap_chips_context(data) == ""

    def test_captains_context_groups_by_intended_pick(self):
        managers = [
            _make_manager(name="Alice", entry_id=1, captain="Haaland", captain_points=7),
            _make_manager(name="Bob", entry_id=2, captain="Haaland", captain_points=7),
            _make_manager(name="Cam", entry_id=3, captain="Haaland", captain_points=7),
            _make_manager(name="Dave", entry_id=4, captain="Salah", captain_points=10),
            _make_manager(name="Eve", entry_id=5, captain="Gibbs-White", captain_points=4),
        ]
        data = _make_recap_data(managers=managers)
        text = format_recap_captains_context(data)
        assert text.startswith("Total captains: 5")
        # Modal pick first (×3), then alphabetical tiebreak between the two ×1 groups
        haaland_line_idx = next(i for i, ln in enumerate(text.splitlines()) if "Haaland" in ln)
        gibbs_line_idx = next(i for i, ln in enumerate(text.splitlines()) if "Gibbs-White" in ln)
        salah_line_idx = next(i for i, ln in enumerate(text.splitlines()) if "Salah" in ln)
        assert haaland_line_idx < gibbs_line_idx < salah_line_idx
        # Within Haaland group, managers alphabetical
        haaland_line = text.splitlines()[haaland_line_idx]
        assert haaland_line.index("Alice") < haaland_line.index("Bob") < haaland_line.index("Cam")
        assert "(×3)" in haaland_line
        assert "(7 pts)" in haaland_line

    def test_captains_context_annotates_dnp_with_vc_takeover(self):
        managers = [
            _make_manager(
                name="Alice",
                entry_id=1,
                captain="Haaland",
                captain_points=0,
                captain_played=False,
                vice_captain="Salah",
                vice_captain_points=12,
            ),
            _make_manager(name="Bob", entry_id=2, captain="Haaland", captain_points=7),
        ]
        data = _make_recap_data(managers=managers)
        text = format_recap_captains_context(data)
        assert "Alice (dnp; vice Salah scored 12 pts)" in text
        assert "Bob (7 pts)" in text

    def test_captains_context_empty_for_draft(self):
        managers = [_make_manager(name="Cam", entry_id=1, captain="Haaland")]
        data = _make_recap_data(managers=managers)
        data["fpl_format"] = "draft"
        assert format_recap_captains_context(data) == ""

    def test_captains_context_empty_when_no_managers(self):
        data = _make_recap_data(managers=[])
        assert format_recap_captains_context(data) == ""

    def test_synthesis_prompt_includes_chips_section(self):
        _, user = get_recap_synthesis_prompt(
            gw=32, league_name="Test", fpl_format="classic",
            awards_text="x", standings_text="| t |", fines_text="",
            chips_text="- **Wildcard** (4): Cam, Ed, Oliver, Ross",
        )
        assert "## Chips Played" in user
        assert "Wildcard** (4)" in user

    def test_synthesis_system_prompt_locks_captain_outliers_to_section(self):
        from fpl_cli.prompts.league_recap import RECAP_SYNTHESIS_SYSTEM_PROMPT

        assert "## Captains" in RECAP_SYNTHESIS_SYSTEM_PROMPT
        assert "verbatim" in RECAP_SYNTHESIS_SYSTEM_PROMPT.lower()

    def test_synthesis_prompt_includes_captains_section_for_classic(self):
        _, user = get_recap_synthesis_prompt(
            gw=35, league_name="Test", fpl_format="classic",
            awards_text="x", standings_text="| t |", fines_text="",
            captains_text="Total captains: 3\n- **Haaland** (×2): Alice (7 pts), Bob (7 pts)",
        )
        assert "## Captains" in user
        assert "Total captains: 3" in user

    def test_synthesis_prompt_omits_captains_section_when_empty(self):
        _, user = get_recap_synthesis_prompt(
            gw=35, league_name="Test", fpl_format="draft",
            awards_text="x", standings_text="| t |", fines_text="",
            captains_text="",
        )
        assert "## Captains" not in user

    def test_fines_context_includes_triggered(self):
        from fpl_cli.cli._league_recap_types import RecapFineResult

        fines = [RecapFineResult(manager_name="Bob", rule_type="last-place", message="Bob finished last")]
        data = _make_recap_data(fines=fines)
        text = format_recap_fines_context(data)
        assert "Bob" in text
        assert "last" in text

    def test_fines_context_empty_when_no_fines(self):
        data = _make_recap_data()
        text = format_recap_fines_context(data)
        assert text == ""

    def test_synthesis_prompt_returns_tuple(self):
        system, user = get_recap_synthesis_prompt(
            gw=10, league_name="Test League", fpl_format="classic",
            awards_text="Alice won", standings_text="| table |",
            fines_text="Bob fined",
        )
        assert "newsletter" in system.lower()
        assert "Gameweek 10" in user
        assert "Alice won" in user
        assert "Bob fined" in user

    def test_gw1_prompt_states_that_no_transfers_exist(self):
        """GW1 squads are built pre-deadline, so the game records no transfers."""
        _, user = get_recap_synthesis_prompt(
            gw=1, league_name="Test", fpl_format="classic",
            awards_text="x", standings_text="| t |", fines_text="",
        )
        assert "No transfers were made this gameweek" in user
        assert "GW1 squads are built before the" in user

    def test_later_gameweek_prompt_has_no_transfer_note(self):
        _, user = get_recap_synthesis_prompt(
            gw=2, league_name="Test", fpl_format="classic",
            awards_text="x", standings_text="| t |", fines_text="",
        )
        assert "No transfers were made this gameweek" not in user

    def test_draft_gw1_prompt_has_no_classic_transfer_note(self):
        """Draft uses waivers, not transfers - the classic GW1 note must not appear."""
        _, user = get_recap_synthesis_prompt(
            gw=1, league_name="Test", fpl_format="draft",
            awards_text="x", standings_text="| t |", fines_text="",
        )
        assert "No transfers were made this gameweek" not in user

    def test_synthesis_system_prompt_fences_transfer_invention(self):
        from fpl_cli.prompts.league_recap import RECAP_SYNTHESIS_SYSTEM_PROMPT

        assert "Only reference transfers that appear explicitly" in RECAP_SYNTHESIS_SYSTEM_PROMPT
        assert "not licence to invent" in RECAP_SYNTHESIS_SYSTEM_PROMPT

    def test_synthesis_system_prompt_binds_club_claims_to_the_data(self):
        """#150: clubs are supplied now, so the rule binds to them rather than
        banning the subject outright."""
        from fpl_cli.prompts.league_recap import RECAP_SYNTHESIS_SYSTEM_PROMPT

        assert (
            "NEVER state a club for a player other than the club given for them"
            in RECAP_SYNTHESIS_SYSTEM_PROMPT
        )
        assert "## Player Clubs" in RECAP_SYNTHESIS_SYSTEM_PROMPT
        assert "goes a season out of date" in RECAP_SYNTHESIS_SYSTEM_PROMPT

    def test_synthesis_prompt_carries_the_player_clubs_section(self):
        _, user = get_recap_synthesis_prompt(
            gw=10, league_name="Test", fpl_format="classic",
            awards_text="awards", standings_text="standings", fines_text="",
            player_clubs_text="- Gyokeres: Arsenal",
        )
        assert "## Player Clubs" in user
        assert "- Gyokeres: Arsenal" in user

    def test_synthesis_prompt_omits_player_clubs_when_unresolved(self):
        """No club map (a replay, or a team lookup that failed) leaves the
        section out, and the rule then forbids naming any club."""
        _, user = get_recap_synthesis_prompt(
            gw=10, league_name="Test", fpl_format="classic",
            awards_text="awards", standings_text="standings", fines_text="",
        )
        assert "## Player Clubs" not in user


    def test_synthesis_prompt_omits_fines_when_empty(self):
        _, user = get_recap_synthesis_prompt(
            gw=10, league_name="Test", fpl_format="draft",
            awards_text="awards", standings_text="standings",
            fines_text="",
        )
        assert "Fines" not in user


# ---------------------------------------------------------------------------
# U12: League History prompt section and season framing
# ---------------------------------------------------------------------------


def _history_entry(
    text: str = "Alice: Captain blank run of 3, 3 in a row (GW4-GW6).",
    *,
    kind: NoteKind = NoteKind.STREAK,
    surfaces: frozenset[NoteSurface] = frozenset({NoteSurface.CONSOLE, NoteSurface.REPORT, NoteSurface.PROMPT}),
    window: GameweekWindow | None = GameweekWindow(start_gameweek=4, end_gameweek=6),
) -> NotesPackEntry:
    return NotesPackEntry(kind=kind, text=text, surfaces=surfaces, window=window, length=3)


def _history_pack(
    entries: list[NotesPackEntry] | None = None,
    coverage_entries: list[NotesPackEntry] | None = None,
    *,
    phase: SeasonPhase = SeasonPhase.MIDPOINT,
    phase_text: str = "GW20 is the season midpoint.",
    fpl_format: str = "classic",
) -> NotesPack:
    return NotesPack(
        season="2026-27", fpl_format=fpl_format, league_id=42, gameweek=20, phase=phase,
        league_start_gameweek=1,
        season_phase_entry=NotesPackEntry(
            kind=NoteKind.SEASON_PHASE, text=phase_text,
            surfaces=frozenset({NoteSurface.REPORT, NoteSurface.PROMPT}),
        ),
        entries=entries or [],
        coverage_entries=coverage_entries if coverage_entries is not None else [
            NotesPackEntry(
                kind=NoteKind.COVERAGE, text="Recorded history is complete from its start (GW1) through GW20.",
                surfaces=frozenset({NoteSurface.REPORT, NoteSurface.PROMPT}),
            ),
        ],
    )


class TestFormatRecapLeagueHistoryContext:
    def test_ae3_a_streak_renders_with_its_window(self):
        pack = _history_pack(entries=[_history_entry()])
        text = format_recap_league_history_context(pack)

        assert "Alice" in text
        assert "GW4-GW6" in text
        assert "Total League History streak entries: 1" in text

    def test_a_below_minimum_entry_is_withheld_from_the_prompt(self):
        entry = _history_entry(text="Alice: single blank", surfaces=frozenset())
        pack = _history_pack(entries=[entry])
        text = format_recap_league_history_context(pack)

        assert "single blank" not in text
        assert "Total League History streak entries: 0" in text

    def test_coverage_entries_always_render(self):
        pack = _history_pack(entries=[])
        text = format_recap_league_history_context(pack)

        assert "Recorded history is complete from its start (GW1) through GW20." in text

    def test_the_season_phase_line_is_included(self):
        pack = _history_pack(phase_text="GW38 is the season finale.")
        text = format_recap_league_history_context(pack)

        assert "GW38 is the season finale." in text

    def test_no_pack_at_all_states_absence_explicitly_rather_than_returning_empty(self):
        text = format_recap_league_history_context(None)

        assert text != ""
        assert "No league history" in text

    def test_streak_count_is_followed_only_by_streak_bullets_before_coverage_appears(self):
        """Both categories populated together -- the gap the earlier
        single-category tests never exercised. The streak count's N must
        bound exactly N bullets before any coverage-labeled text; a
        coverage caveat must not read as an uncounted (N+1)th streak."""
        streak_entries = [
            _history_entry(text="Alice: Captain blank run of 3, 3 in a row (GW4-GW6)."),
            _history_entry(text="Bob: Hit run of 2 (GW5-GW6)."),
        ]
        coverage_entries = [
            NotesPackEntry(
                kind=NoteKind.COVERAGE,
                text="Recorded history is complete from its start (GW1) through GW20.",
                surfaces=frozenset({NoteSurface.REPORT, NoteSurface.PROMPT}),
            ),
            NotesPackEntry(
                kind=NoteKind.COVERAGE,
                text=(
                    "Carol: recorded history begins at GW10, later than the league's "
                    "start (GW1); earlier gameweeks are not available for this manager."
                ),
                surfaces=frozenset({NoteSurface.REPORT, NoteSurface.PROMPT}),
                manager_key=3,
                manager_name="Carol",
            ),
        ]
        pack = _history_pack(entries=streak_entries, coverage_entries=coverage_entries)
        text = format_recap_league_history_context(pack)
        lines = text.splitlines()

        count_index = lines.index("Total League History streak entries: 2")
        streak_bullet_lines = lines[count_index + 1 : count_index + 3]
        assert streak_bullet_lines == [
            "- Alice: Captain blank run of 3, 3 in a row (GW4-GW6).",
            "- Bob: Hit run of 2 (GW5-GW6).",
        ]

        # Nothing coverage-labeled leaks into the counted streak lines.
        for line in streak_bullet_lines:
            assert "Coverage" not in line
            assert "Carol" not in line
            assert "Recorded history is complete" not in line

        # The very next non-blank content is a coverage label, not a
        # third, uncounted streak-shaped bullet.
        remainder = [line for line in lines[count_index + 3 :] if line]
        assert remainder[0] == "Coverage:"
        assert remainder[1] == "- Recorded history is complete from its start (GW1) through GW20."
        assert remainder[2] == (
            "- Carol: recorded history begins at GW10, later than the league's "
            "start (GW1); earlier gameweeks are not available for this manager."
        )


class TestLeagueHistoryPromptSection:
    def test_ae3_a_streak_renders_under_its_own_heading(self):
        pack = _history_pack(entries=[_history_entry()])
        _, user = get_recap_synthesis_prompt(
            gw=20, league_name="Test", fpl_format="classic",
            awards_text="x", standings_text="| t |", fines_text="",
            league_history_text=format_recap_league_history_context(pack),
        )

        assert "## League History" in user
        assert "GW4-GW6" in user

    def test_the_history_rules_appear_even_when_no_pack_was_supplied(self):
        """KTD9: the rule is unconditional -- present in the system prompt
        whether or not this call site even has a pack to inject."""
        system, _ = get_recap_synthesis_prompt(
            gw=20, league_name="Test", fpl_format="classic",
            awards_text="x", standings_text="| t |", fines_text="",
        )

        assert "League History" in system
        assert "forbidden" in system.lower()

    def test_the_blanket_stick_to_gameweek_rule_now_names_the_bounded_exception(self):
        from fpl_cli.prompts.league_recap import RECAP_SYNTHESIS_SYSTEM_PROMPT

        assert "Stick to what happened this gameweek, with one exception" in RECAP_SYNTHESIS_SYSTEM_PROMPT
        assert '"## League History" section' in RECAP_SYNTHESIS_SYSTEM_PROMPT

    def test_a_held_run_must_not_be_simplified_to_consecutive(self):
        from fpl_cli.prompts.league_recap import RECAP_SYNTHESIS_SYSTEM_PROMPT

        assert "not recorded" in RECAP_SYNTHESIS_SYSTEM_PROMPT
        assert "never simplified to" in RECAP_SYNTHESIS_SYSTEM_PROMPT

    def test_research_summary_keeps_its_own_heading_alongside_league_history(self):
        pack = _history_pack(entries=[_history_entry()])
        _, user = get_recap_synthesis_prompt(
            gw=20, league_name="Test", fpl_format="classic",
            awards_text="x", standings_text="| t |", fines_text="",
            research_summary="Some research-role output.",
            league_history_text=format_recap_league_history_context(pack),
        )

        assert "## GW Context (from research)" in user
        assert "Some research-role output." in user
        assert "## League History" in user
        assert user.index("## League History") < user.index("## GW Context (from research)")

    def test_ae6_the_finale_framing_instruction_is_present(self):
        from fpl_cli.prompts.league_recap import RECAP_SYNTHESIS_SYSTEM_PROMPT

        assert "finale" in RECAP_SYNTHESIS_SYSTEM_PROMPT.lower()
        assert "season phase" in RECAP_SYNTHESIS_SYSTEM_PROMPT.lower()

    def test_a_draft_pack_has_no_captain_derived_entries(self):
        """Delegated to U8/U9's format filtering -- confirmed at the prompt
        consumption boundary too."""
        pack = _history_pack(
            entries=[_history_entry(text="Bob: Waiver win run of 2 (GW19-GW20).")],
            fpl_format="draft",
        )
        text = format_recap_league_history_context(pack)

        assert "Captain" not in text
        assert "Waiver win run" in text

    def test_the_rendered_prompt_states_the_packs_declared_entry_count(self):
        pack = _history_pack(entries=[_history_entry(), _history_entry(text="Bob: Hit run of 3 (GW1-GW3).")])
        _, user = get_recap_synthesis_prompt(
            gw=20, league_name="Test", fpl_format="classic",
            awards_text="x", standings_text="| t |", fines_text="",
            league_history_text=format_recap_league_history_context(pack),
        )

        assert "Total League History streak entries: 2" in user

    def test_no_league_history_section_when_the_caller_supplies_no_text(self):
        """Every other optional section already follows this convention;
        League History does too when a caller genuinely has nothing (an
        empty string is distinct from `format_recap_league_history_context`'s
        own always-non-empty output -- callers that actually run the
        formatter never hit this branch)."""
        _, user = get_recap_synthesis_prompt(
            gw=20, league_name="Test", fpl_format="classic",
            awards_text="x", standings_text="| t |", fines_text="",
        )

        assert "## League History" not in user


# ---------------------------------------------------------------------------
# Standings block rendering (WhatsApp-friendly text format)
# ---------------------------------------------------------------------------


class TestFormatStandingsBlock:
    def test_sorts_by_gw_points_descending(self):
        managers = [
            _make_manager(name="Alice", gw_points=40, overall_rank=1, previous_rank=1, total_points=2000),
            _make_manager(name="Bob", gw_points=80, overall_rank=2, previous_rank=3, total_points=1900),
            _make_manager(name="Charlie", gw_points=60, overall_rank=3, previous_rank=2, total_points=1850),
        ]
        block = _format_standings_block(managers)
        lines = block.splitlines()
        assert lines[0].lstrip().startswith("1.")
        assert "Bob" in lines[0]
        assert "Charlie" in lines[1]
        assert "Alice" in lines[2]

    def test_tie_on_gw_points_resolves_by_overall_rank(self):
        managers = [
            _make_manager(name="Lower", gw_points=70, overall_rank=5, previous_rank=5),
            _make_manager(name="Higher", gw_points=70, overall_rank=2, previous_rank=2),
        ]
        block = _format_standings_block(managers)
        lines = block.splitlines()
        assert "Higher" in lines[0]
        assert "Lower" in lines[1]

    def test_no_pipe_characters(self):
        managers = [_make_manager(name="Solo", gw_points=50)]
        assert "|" not in _format_standings_block(managers)

    def test_unchanged_rank_has_no_arrow(self):
        managers = [_make_manager(name="Stable", overall_rank=4, previous_rank=4)]
        block = _format_standings_block(managers)
        assert "↑" not in block
        assert "↓" not in block

    def test_climbed_rank_shows_up_arrow(self):
        managers = [_make_manager(name="Climber", overall_rank=3, previous_rank=7)]
        block = _format_standings_block(managers)
        assert "↑4" in block

    def test_dropped_rank_shows_down_arrow(self):
        managers = [_make_manager(name="Faller", overall_rank=8, previous_rank=5)]
        block = _format_standings_block(managers)
        assert "↓3" in block

    def test_chip_marker_appended_to_name(self):
        managers = [
            _make_manager(name="Short", gw_points=777, active_chip="BB"),
            _make_manager(name="LongerManagerName", gw_points=555),
        ]
        block = _format_standings_block(managers)
        assert "Short (BB)" in block
        # Padding adapts to longest display string: GW points column aligns.
        # Use distinctive values that can't collide with rank/ordinal/total substrings.
        lines = block.splitlines()
        pts_positions = [line.index(str(pts)) for line, pts in zip(lines, [777, 555], strict=True)]
        assert pts_positions[0] == pts_positions[1]

    def test_empty_string_active_chip_treated_as_no_chip(self):
        managers = [_make_manager(name="Solo", gw_points=50, active_chip="")]
        block = _format_standings_block(managers)
        assert "()" not in block
        assert "Solo" in block

    def test_ordinal_rendering(self):
        managers = [
            _make_manager(name=f"M{r}", gw_points=100 - r, overall_rank=r, previous_rank=r)
            for r in (1, 2, 3, 11, 12, 13, 21, 22, 23)
        ]
        block = _format_standings_block(managers)
        for expected in ("1st", "2nd", "3rd", "11th", "12th", "13th", "21st", "22nd", "23rd"):
            assert expected in block, f"missing ordinal {expected}"

    def test_empty_managers_returns_empty_string(self):
        assert _format_standings_block([]) == ""

    def test_includes_total_points(self):
        managers = [_make_manager(name="Solo", gw_points=50, total_points=1735)]
        assert "1735" in _format_standings_block(managers)


class TestLeagueRecapTemplateRender:
    async def test_rendered_recap_has_no_pipes_in_standings(self, tmp_path):
        agent = ReportAgent(config={"output_dir": str(tmp_path)})
        data = {
            "gameweek": 10,
            "league_name": "Test League",
            "fpl_format": "classic",
            "managers": [
                _make_manager(name="Alice", gw_points=80, overall_rank=1, previous_rank=2, total_points=900, active_chip="WC"),
                _make_manager(name="Bob", gw_points=60, overall_rank=2, previous_rank=1, total_points=890),
            ],
            "awards": {},
        }
        result = await agent.run(context={
            "report_type": "league-recap",
            "gameweek": 10,
            "data": data,
        })
        assert result.data is not None
        report_path = result.data.get("report_path")
        assert report_path
        content = Path(report_path).read_text()

        standings_start = content.index("# GW Standings")
        standings_end = content.index("---", standings_start)
        standings_section = content[standings_start:standings_end]

        assert "|" not in standings_section
        # Arrow, chip, and ordinal survive the Jinja fenced-block render
        assert "↑1" in standings_section
        assert "(WC)" in standings_section
        assert "1st" in standings_section
        # Highest GW points appears first
        assert standings_section.index("Alice") < standings_section.index("Bob")
        # Block is wrapped in a fenced code block for Obsidian monospace
        assert "```" in standings_section


# ---------------------------------------------------------------------------
# Unit 1: chip-aware classic pick flags
# ---------------------------------------------------------------------------


class TestClassicPickFlags:
    """Cover _classic_pick_flags: (is_bench, is_bench_boost_player, contributed)."""

    def test_non_bb_starter_contributes(self):
        pick = {"position": 5, "multiplier": 1}
        is_bench, is_bbp, contributed = _classic_pick_flags(
            pick=pick, active_chip=None, player_id=1, auto_sub_in_ids=set(),
        )
        assert (is_bench, is_bbp, contributed) == (False, False, True)

    def test_non_bb_bench_does_not_contribute(self):
        pick = {"position": 13, "multiplier": 0}
        is_bench, is_bbp, contributed = _classic_pick_flags(
            pick=pick, active_chip=None, player_id=1, auto_sub_in_ids=set(),
        )
        assert (is_bench, is_bbp, contributed) == (True, False, False)

    def test_bb_bench_contributes_and_flagged(self):
        pick = {"position": 13, "multiplier": 0}
        is_bench, is_bbp, contributed = _classic_pick_flags(
            pick=pick, active_chip="bboost", player_id=1, auto_sub_in_ids=set(),
        )
        assert (is_bench, is_bbp, contributed) == (True, True, True)

    def test_bb_starter_not_flagged_as_bench_boost(self):
        pick = {"position": 1, "multiplier": 1}
        is_bench, is_bbp, contributed = _classic_pick_flags(
            pick=pick, active_chip="bboost", player_id=1, auto_sub_in_ids=set(),
        )
        assert (is_bench, is_bbp, contributed) == (False, False, True)

    def test_auto_sub_in_contributes_without_bb(self):
        pick = {"position": 13, "multiplier": 0}
        is_bench, is_bbp, contributed = _classic_pick_flags(
            pick=pick, active_chip=None, player_id=99, auto_sub_in_ids={99},
        )
        # Auto-sub target still counts as bench slot by position, but contributes
        assert (is_bench, is_bbp, contributed) == (True, False, True)

    def test_bb_and_auto_sub_in_both_contribute(self):
        pick = {"position": 13, "multiplier": 0}
        is_bench, is_bbp, contributed = _classic_pick_flags(
            pick=pick, active_chip="bboost", player_id=99, auto_sub_in_ids={99},
        )
        assert (is_bench, is_bbp, contributed) == (True, True, True)

    def test_other_chips_do_not_flip_bench_contributed(self):
        # Only bboost flips bench contributed; wildcard, freehit, 3xc do not.
        pick = {"position": 13, "multiplier": 0}
        for chip in ("wildcard", "freehit", "3xc"):
            _, is_bbp, contributed = _classic_pick_flags(
                pick=pick, active_chip=chip, player_id=1, auto_sub_in_ids=set(),
            )
            assert is_bbp is False
            assert contributed is False


# ---------------------------------------------------------------------------
# U4: ledger row model and collector contract
# ---------------------------------------------------------------------------


class TestCollectorLedgerContract:
    """The collector must carry everything capture needs to build a row."""

    async def test_cohort_holds_every_standings_row_including_failed_fetches(self):
        class _RaisingClient(_FakeClassicClient):
            async def get_manager_picks(self, entry_id, gameweek):
                if entry_id == 2:
                    raise RuntimeError("boom")
                return await super().get_manager_picks(entry_id, gameweek)

        standings = [
            {"entry": 1, "player_name": "Alice", "event_total": 50, "total": 500},
            {"entry": 2, "player_name": "Bob", "event_total": 40, "total": 480},
            {"entry": 3, "player_name": "Cara", "event_total": 30, "total": 460},
        ]
        client = _RaisingClient(
            _standings_response(standings),
            {
                1: _picks_response(points=50, total_points=500),
                3: _picks_response(points=30, total_points=460),
            },
        )
        data = await collect_classic_recap_data(
            client, {"fpl": {"classic_league_id": 77}}, gw=10,
            live_stats={}, player_map={}, teams={}, is_live_gw=True,
        )
        cohort = data["standings_cohort"]
        assert [c["manager_key"] for c in cohort] == [1, 2, 3]
        assert [c["manager_name"] for c in cohort] == ["Alice", "Bob", "Cara"]
        assert [c["gw_points"] for c in cohort] == [50, 40, 30]
        assert [c["total_points"] for c in cohort] == [500, 480, 460]
        assert data["league_id"] == 77
        assert data["standings_truncated"] is False

    async def test_classic_truncated_standings_are_flagged(self):
        standings = [{"entry": 1, "player_name": "Alice", "event_total": 50, "total": 500}]
        response = _standings_response(standings)
        response["standings"]["has_next"] = True
        client = _FakeClassicClient(response, {1: _picks_response(points=50, total_points=500)})
        data = await collect_classic_recap_data(
            client, {"fpl": {"classic_league_id": 1}}, gw=10,
            live_stats={}, player_map={}, teams={}, is_live_gw=True,
        )
        assert data["standings_truncated"] is True

    async def test_classic_entry_carries_the_four_rollover_only_fields(self):
        standings = [{"entry": 1, "player_name": "Alice", "event_total": 50, "total": 500}]
        picks = _picks_response(points=50, total_points=500)
        picks["entry_history"].update(
            {"value": 1013, "bank": 7, "overall_rank": 412_345, "event_transfers": 2},
        )
        client = _FakeClassicClient(_standings_response(standings), {1: picks})
        data = await collect_classic_recap_data(
            client, {"fpl": {"classic_league_id": 1}}, gw=10,
            live_stats={}, player_map={}, teams={}, is_live_gw=True,
        )
        m = data["managers"][0]
        # The API's `value` verbatim, bank included -- hence `team_value`.
        assert m["team_value"] == 1013
        assert m["bank"] == 7
        assert m["global_rank"] == 412_345
        assert m["transfers_made"] == 2
        # Global rank and league position are separate fields on the entry.
        assert m["overall_rank"] == 1

    async def test_classic_squad_player_carries_the_stable_code(self):
        player = make_player(id=5, code=99_001, web_name="Star", team_id=1)
        standings = [{"entry": 1, "player_name": "Alice", "event_total": 6, "total": 6}]
        picks = _picks_response(points=6, total_points=6)
        picks["picks"] = [{"element": 5, "position": 1, "multiplier": 1, "is_captain": True}]
        client = _FakeClassicClient(_standings_response(standings), {1: picks})
        data = await collect_classic_recap_data(
            client, {"fpl": {"classic_league_id": 1}}, gw=10,
            live_stats={5: {"total_points": 6, "minutes": 90}},
            player_map={5: player}, teams={}, is_live_gw=False,
        )
        squad = data["managers"][0]["squad"]
        assert [p["code"] for p in squad] == [99_001]
        assert squad[0]["had_fixture"] is True

    async def test_blank_gameweek_marks_only_the_clubs_without_a_fixture(self):
        playing = make_player(id=5, code=1, web_name="Plays", team_id=1)
        blanking = make_player(id=6, code=2, web_name="Blanks", team_id=2)
        standings = [{"entry": 1, "player_name": "Alice", "event_total": 6, "total": 6}]
        picks = _picks_response(points=6, total_points=6)
        picks["picks"] = [
            {"element": 5, "position": 1, "multiplier": 1},
            {"element": 6, "position": 2, "multiplier": 1},
        ]
        client = _FakeClassicClient(_standings_response(standings), {1: picks})
        data = await collect_classic_recap_data(
            client, {"fpl": {"classic_league_id": 1}}, gw=10,
            live_stats={}, player_map={5: playing, 6: blanking}, teams={},
            is_live_gw=False, bgw_team_ids=frozenset({2}),
        )
        by_name = {p["name"]: p for p in data["managers"][0]["squad"]}
        assert by_name["Plays"]["had_fixture"] is True
        assert by_name["Blanks"]["had_fixture"] is False

    async def test_normal_gameweek_marks_no_player_as_fixtureless(self):
        player = make_player(id=5, code=1, web_name="Plays", team_id=1)
        standings = [{"entry": 1, "player_name": "Alice", "event_total": 6, "total": 6}]
        picks = _picks_response(points=6, total_points=6)
        picks["picks"] = [{"element": 5, "position": 1, "multiplier": 1}]
        client = _FakeClassicClient(_standings_response(standings), {1: picks})
        data = await collect_classic_recap_data(
            client, {"fpl": {"classic_league_id": 1}}, gw=10,
            live_stats={}, player_map={5: player}, teams={}, is_live_gw=False,
        )
        assert all(p["had_fixture"] for p in data["managers"][0]["squad"])

    async def test_classic_transfers_carry_both_player_codes(self):
        pin = make_player(id=5, code=111, web_name="In", team_id=1)
        pout = make_player(id=6, code=222, web_name="Out", team_id=1)
        standings = [{"entry": 1, "player_name": "Alice", "event_total": 6, "total": 6}]
        picks = _picks_response(points=6, total_points=6)
        picks["entry_history"]["event_transfers"] = 1
        client = _FakeClassicClient(
            _standings_response(standings),
            {1: picks},
            transfers_by_entry={1: [{"event": 10, "element_in": 5, "element_out": 6}]},
        )
        data = await collect_classic_recap_data(
            client, {"fpl": {"classic_league_id": 1}}, gw=10,
            live_stats={}, player_map={5: pin, 6: pout}, teams={}, is_live_gw=False,
        )
        tr = data["managers"][0]["transfers"][0]
        assert tr["player_in_code"] == 111
        assert tr["player_out_code"] == 222

    def test_fines_are_keyed_by_manager_not_by_display_name(self):
        """Two managers can share a display name, so a stored ruling has to
        carry the key the row is written under."""
        rules = [{"type": "last-place", "penalty": "Pint on video"}]
        squad = [_make_squad_player(name=f"P{i}") for i in range(11)]
        managers = [
            _make_manager(name="Same Name", entry_id=1, gw_points=80, squad=squad),
            _make_manager(name="Same Name", entry_id=2, gw_points=10, squad=squad),
        ]
        fines = evaluate_league_fines(managers, {"fines": {"classic": rules}}, "classic")
        assert [f["manager_key"] for f in fines] == [2]

    def test_draft_fines_key_on_the_league_local_id(self):
        rules = [{"type": "last-place", "penalty": "Pint on video"}]
        squad = [_make_squad_player(name=f"P{i}") for i in range(11)]
        top = _make_manager(name="Alice", entry_id=0, gw_points=80, squad=squad)
        bottom = _make_manager(name="Bob", entry_id=0, gw_points=10, squad=squad)
        top["league_entry_id"] = 10
        bottom["league_entry_id"] = 11
        fines = evaluate_league_fines([top, bottom], {"fines": {"draft": rules}}, "draft")
        assert [f["manager_key"] for f in fines] == [11]

    async def test_draft_keys_two_unclaimed_teams_distinctly(self):
        draft_player = make_draft_player(id=900, web_name="Star", team=1, element_type=3)
        main_player = make_player(id=5, code=555, web_name="Star", team_id=1)
        league_details = {
            "league": {"name": "Draft League"},
            "standings": [
                {"league_entry": 10, "event_total": 5, "total": 5},
                {"league_entry": 11, "event_total": 5, "total": 5},
            ],
            "league_entries": [
                {"id": 10, "entry_id": None, "entry_name": "Ghost A"},
                {"id": 11, "entry_id": None, "entry_name": "Ghost B"},
            ],
        }
        picks = {None: {"picks": [{"element": 900, "position": 1}], "subs": []}}
        client = MagicMock()
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock(return_value=False)
        client.get_league_details = AsyncMock(return_value=league_details)
        client.get_bootstrap_static = AsyncMock(return_value={"elements": [draft_player]})
        client.get_league_transactions = AsyncMock(return_value={"transactions": []})
        client.get_entry_picks = AsyncMock(side_effect=lambda entry_id, gw: picks[entry_id])

        with patch("fpl_cli.api.fpl_draft.FPLDraftClient", return_value=client):
            data = await collect_draft_recap_data(
                {"fpl": {"draft_league_id": 3}}, gw=15, live_stats={5: {"total_points": 5}},
                players=[main_player], teams={}, is_live_gw=False,
            )
        assert sorted(m["league_entry_id"] for m in data["managers"]) == [10, 11]
        assert sorted(c["manager_key"] for c in data["standings_cohort"]) == [10, 11]
        assert all(c["entry_id"] is None for c in data["standings_cohort"])
        assert data["league_id"] == 3

    async def test_draft_unmatched_player_carries_no_code(self):
        matched = make_draft_player(id=900, web_name="Star", team=1, element_type=3)
        stranger = make_draft_player(id=901, web_name="Nobody", team=1, element_type=3)
        main_player = make_player(id=5, code=555, web_name="Star", team_id=1)
        league_details = {
            "league": {"name": "Draft League"},
            "standings": [{"league_entry": 10, "event_total": 5, "total": 5}],
            "league_entries": [{"id": 10, "entry_id": 1, "player_first_name": "A", "player_last_name": "B"}],
        }
        picks = {
            1: {
                "picks": [
                    {"element": 900, "position": 1},
                    {"element": 901, "position": 2},
                ],
                "subs": [],
            },
        }
        client = MagicMock()
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock(return_value=False)
        client.get_league_details = AsyncMock(return_value=league_details)
        client.get_bootstrap_static = AsyncMock(return_value={"elements": [matched, stranger]})
        client.get_league_transactions = AsyncMock(return_value={"transactions": []})
        client.get_entry_picks = AsyncMock(side_effect=lambda entry_id, gw: picks[entry_id])

        with patch("fpl_cli.api.fpl_draft.FPLDraftClient", return_value=client):
            data = await collect_draft_recap_data(
                {"fpl": {"draft_league_id": 1}}, gw=15, live_stats={5: {"total_points": 5}},
                players=[main_player], teams={}, is_live_gw=False,
            )
        by_name = {p["name"]: p for p in data["managers"][0]["squad"]}
        assert by_name["Star"]["code"] == 555
        assert by_name["Star"]["unmatched"] is False
        assert by_name["Nobody"]["code"] is None
        assert by_name["Nobody"]["unmatched"] is True


class TestRecapPlayerClubs:
    """#150: the recap prompt carried no club data at all, so any club the
    narrative named came from training data a transfer window out of date."""

    @staticmethod
    def _data(**overrides):
        manager = {
            "manager_name": "Manager A",
            "captain": "Gyökeres",
            "captain_points": 9,
            "captain_played": True,
            "gw_points": 70,
            "squad": [
                {"name": "Gyökeres", "team": "ARS", "team_name": "Arsenal"},
                {"name": "Wissa", "team": "NEW", "team_name": "Newcastle"},
            ],
        }
        manager.update(overrides)
        return {"fpl_format": "classic", "managers": [manager]}

    def _roster(self, **overrides):
        return format_recap_player_clubs_context(collect_player_clubs(self._data(**overrides)))

    def test_roster_lists_every_squad_player_with_a_full_club_name(self):
        text = self._roster()
        assert "- Gyökeres: Arsenal" in text
        assert "- Wissa: Newcastle" in text

    def test_roster_covers_transfers_as_well_as_squads(self):
        text = self._roster(transfers=[{
            "player_in": "Semenyo", "player_in_team": "MUN", "player_in_team_name": "Man Utd",
            "player_out": "Watkins", "player_out_team": "AVL", "player_out_team_name": "Aston Villa",
        }])
        assert "- Semenyo: Man Utd" in text
        assert "- Watkins: Aston Villa" in text

    def test_roster_covers_draft_waiver_transactions(self):
        text = self._roster(transactions=[{
            "player_in": "Semenyo", "player_in_team": "MUN", "player_in_team_name": "Man Utd",
            "player_out": "Watkins", "player_out_team": "AVL", "player_out_team_name": "Aston Villa",
        }])
        assert "- Semenyo: Man Utd" in text

    def test_roster_drops_a_name_two_clubs_claim(self):
        """Two players share a web_name most seasons. The recap names players by
        name alone, so neither club can be attributed - better absent than wrong."""
        text = self._roster(squad=[
            {"name": "Martínez", "team": "AVL", "team_name": "Aston Villa"},
            {"name": "Martínez", "team": "MUN", "team_name": "Man Utd"},
            {"name": "Wissa", "team": "NEW", "team_name": "Newcastle"},
        ])
        assert "Martínez" not in text
        assert "- Wissa: Newcastle" in text

    def test_ambiguity_survives_a_later_repeat_of_the_first_club(self):
        """A third sighting matching the first club must not resurrect the name."""
        clubs = collect_player_clubs(self._data(squad=[
            {"name": "Martínez", "team": "AVL", "team_name": "Aston Villa"},
            {"name": "Martínez", "team": "MUN", "team_name": "Man Utd"},
            {"name": "Martínez", "team": "AVL", "team_name": "Aston Villa"},
        ]))
        assert "Martínez" not in clubs

    def test_roster_drops_a_player_whose_club_never_resolved(self):
        """Collection sets team_name to None rather than a placeholder, so the
        player simply has no club to state."""
        assert self._roster(squad=[{"name": "Calvert-Lewin", "team": "???", "team_name": None}]) == ""

    def test_roster_empty_when_no_squads_carry_clubs(self):
        assert format_recap_player_clubs_context({}) == ""

    def test_roster_states_it_is_the_only_source(self):
        assert "only source for a player's club" in self._roster()

    def test_captain_group_header_carries_the_club_inline(self):
        data = self._data()
        text = format_recap_captains_context(data, collect_player_clubs(data))
        assert "- **Gyökeres (Arsenal)** (×1): Manager A (9 pts)" in text

    def test_captain_group_header_unchanged_without_a_roster(self):
        text = format_recap_captains_context(self._data(), None)
        assert "- **Gyökeres** (×1): Manager A (9 pts)" in text

    def test_captain_group_header_omits_an_ambiguous_club(self):
        data = self._data(
            captain="Martínez",
            squad=[
                {"name": "Martínez", "team": "AVL", "team_name": "Aston Villa"},
                {"name": "Martínez", "team": "MUN", "team_name": "Man Utd"},
            ],
        )
        text = format_recap_captains_context(data, collect_player_clubs(data))
        assert "- **Martínez** (×1):" in text
        assert "Aston Villa" not in text
