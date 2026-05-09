"""Tests for league recap data collection and awards."""
# pyright: reportTypedDictNotRequiredAccess=false

from __future__ import annotations

from pathlib import Path

from fpl_cli.agents.orchestration.report import ReportAgent, _format_standings_block
from fpl_cli.cli._league_recap_data import (
    _bucket_draft_txns_by_league_entry,
    _classic_pick_flags,
    _compute_shared_awards,
    _compute_standings_movement,
    _compute_transfer_awards,
    _compute_waiver_awards,
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
    format_recap_awards_context,
    format_recap_chips_context,
    format_recap_fines_context,
    format_recap_standings_context,
    get_recap_synthesis_prompt,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_manager(
    name: str = "Manager A",
    entry_id: int = 1,
    gw_points: int = 50,
    total_points: int = 500,
    gw_rank: int = 1,
    overall_rank: int = 1,
    previous_rank: int = 1,
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
    """Factory for RecapManagerEntry with sensible defaults."""
    result = RecapManagerEntry(
        manager_name=name,
        entry_id=entry_id,
        gw_points=gw_points,
        total_points=total_points,
        gw_rank=gw_rank,
        overall_rank=overall_rank,
        previous_rank=previous_rank,
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
        assert awards.get("transfer_genius", {}).get("manager_name") == "Alice"  # type: ignore[union-attr]
        assert "Palmer" in awards.get("transfer_genius", {}).get("detail", "")  # type: ignore[union-attr]

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

    def test_synthesis_prompt_includes_chips_section(self):
        _, user = get_recap_synthesis_prompt(
            gw=32, league_name="Test", fpl_format="classic",
            awards_text="x", standings_text="| t |", fines_text="",
            chips_text="- **Wildcard** (4): Cam, Ed, Oliver, Ross",
        )
        assert "## Chips Played" in user
        assert "Wildcard** (4)" in user

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

    def test_synthesis_prompt_omits_fines_when_empty(self):
        _, user = get_recap_synthesis_prompt(
            gw=10, league_name="Test", fpl_format="draft",
            awards_text="awards", standings_text="standings",
            fines_text="",
        )
        assert "Fines" not in user


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
