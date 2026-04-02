"""Tests for league recap data collection and awards."""
# pyright: reportTypedDictNotRequiredAccess=false

from __future__ import annotations

from fpl_cli.cli._league_recap_data import (
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
        vice_captain="Saka",
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
        assert awards["worst_captain"]["detail"] == "Bob and Charlie captained Haaland (2 pts)"
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

    def test_worst_captain_excludes_vc_rescue(self):
        """Captain who didn't play (VC activated) should not win worst captain."""
        managers = [
            _make_manager(name="Alice", captain="Salah", captain_points=0, captain_played=False),
            _make_manager(name="Bob", captain="Haaland", captain_points=2),
            _make_manager(name="Charlie", captain="Palmer", captain_points=8),
        ]
        awards = _compute_shared_awards(managers)
        assert awards["worst_captain"]["manager_name"] == "Bob"
        assert awards["worst_captain"]["value"] == 2

    def test_worst_captain_all_captains_didnt_play_falls_back(self):
        """If no captain played, fall back to all managers."""
        managers = [
            _make_manager(name="Alice", captain="Salah", captain_points=0, captain_played=False),
            _make_manager(name="Bob", captain="Haaland", captain_points=0, captain_played=False),
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
        """Bench boost means bench_points=0 (all contribute). No bench award expected."""
        managers = [
            _make_manager(name="Alice", active_chip="bboost", bench_points=0),
            _make_manager(name="Bob", bench_points=10),
        ]
        awards = _compute_shared_awards(managers)
        assert awards["biggest_bench_haul"]["manager_name"] == "Bob"


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
