"""Tests for gameweek review functionality."""

import pytest

from fpl_cli.cli._fines import compute_bench_analysis
from fpl_cli.cli._helpers import _gw_position_with_half, _net_transfer_ids
from fpl_cli.cli._review_classic import _collapse_transfer_churn, _format_review_classic_player
from fpl_cli.models.player import PlayerPosition
from fpl_cli.prompts.review import (
    REVIEW_RESEARCH_SYSTEM_PROMPT,
    _build_system_prompt,
    get_review_research_prompt,
    get_review_synthesis_prompt,
    validate_research_teams,
)
from tests.conftest import make_player, make_team


class TestReviewPrompts:
    """Tests for review prompt templates."""

    def test_research_system_prompt_exists(self):
        """Test research system prompt is defined."""
        assert REVIEW_RESEARCH_SYSTEM_PROMPT
        assert "FPL analyst" in REVIEW_RESEARCH_SYSTEM_PROMPT
        assert "Jonathan Liew" in REVIEW_RESEARCH_SYSTEM_PROMPT

    def test_research_user_prompt_formatting(self):
        """Test research user prompt formats correctly."""
        prompt = get_review_research_prompt(gameweek=22)
        assert "Gameweek 22" in prompt
        assert "GW22" in prompt

    def test_synthesis_system_prompt_exists(self):
        """Test synthesis system prompt is defined."""
        assert _build_system_prompt(has_fines=True)
        assert "Classic" in _build_system_prompt(has_fines=True)
        assert "Draft" in _build_system_prompt(has_fines=True)
        assert "fine" in _build_system_prompt(has_fines=True).lower()

    def test_synthesis_system_prompt_position_inference_rule(self):
        """Rule forbidding the LLM from inferring position when it isn't in the data."""
        prompt = _build_system_prompt(has_fines=False)
        assert "Team-grouping and position-grouping are independent" in prompt
        assert "Never infer position from context" in prompt

    def test_synthesis_system_prompt_bench_analysis(self):
        """Test synthesis system prompt includes bench analysis guidance."""
        assert "bench" in _build_system_prompt(has_fines=True).lower()
        assert "BENCH" in _build_system_prompt(has_fines=True)
        assert "selection" in _build_system_prompt(has_fines=True).lower()
        assert "Bench vs Starters" in _build_system_prompt(has_fines=True)
        assert "pre-computed" in _build_system_prompt(has_fines=True)
        assert "formation-valid" in _build_system_prompt(has_fines=True)

    def test_synthesis_system_prompt_no_fines(self):
        """Test system prompt without fines omits fine-specific content."""
        prompt = _build_system_prompt(has_fines=False)
        assert prompt
        assert "Classic" in prompt
        assert "Draft" in prompt
        assert "fine" not in prompt.lower()
        assert "Fine Check" not in prompt
        assert "fine trigger" not in prompt.lower()

    def test_synthesis_user_prompt_formatting(self):
        """Test synthesis user prompt formats correctly with all parameters."""
        _, prompt = get_review_synthesis_prompt(
            gameweek=22,
            research_summary="Test community summary",
            classic_points=55,
            classic_average=50,
            classic_highest=95,
            classic_gw_rank=500000,
            classic_overall_rank=100000,
            classic_captain="Salah",
            classic_captain_points=14,
            classic_captain_hindsight="N/A",
            classic_players="- Salah (LIV): 14 pts (C)",
            classic_transfers="No transfers this week",
            classic_league_name="Test League",
            classic_gw_position=3,
            classic_position=5,
            classic_total=11,
            classic_rivals="- 4. John: 1,200 pts",
            classic_worst_performers="1. Mike - 35 pts\n2. Bob - 38 pts",
            classic_transfer_impact=None,
            draft_points=42,
            draft_league_name="Draft League",
            draft_players="- Haaland (MCI): 8 pts",
            draft_transactions="No waivers this week",
            draft_gw_position=2,
            draft_position=3,
            draft_total=10,
            draft_worst_performers="1. Bob - 28 pts\n2. Mike - 31 pts",
        )

        assert "Gameweek 22" in prompt
        assert "Draft League" in prompt
        assert "55" in prompt  # classic_points
        assert "Salah" in prompt
        assert "Test League" in prompt
        assert "Draft" in prompt
        assert "42" in prompt  # draft_points

    def test_synthesis_prompt_includes_fine_results(self):
        """Test synthesis user prompt includes pre-computed fine results."""
        fine_results = (
            "## Classic (Test League)\n"
            "- No last-place fine. Jane Doe finished bottom with 54 net pts.\n"
            "- No red card fine.\n"
            "- No fines this week.\n"
            "\n"
            "## Draft League\n"
            "- FINE TRIGGERED: You finished last in the gameweek with 27 pts. Pint on video.\n"
            "- No sub-25 fine. You scored 27 points (27 >= 25).\n"
        )
        _, prompt = get_review_synthesis_prompt(
            gameweek=22,
            research_summary="",
            classic_points=0,
            classic_average=0,
            classic_highest=0,
            classic_gw_rank=0,
            classic_overall_rank=0,
            classic_captain="",
            classic_captain_points=0,
            classic_captain_hindsight="N/A",
            classic_players="",
            classic_transfers="",
            classic_league_name="",
            classic_gw_position=0,
            classic_position=0,
            classic_total=0,
            classic_rivals="",
            classic_worst_performers="",
            classic_transfer_impact=None,
            draft_points=27,
            draft_league_name="Draft League",
            draft_players="",
            draft_transactions="",
            draft_gw_position=0,
            draft_position=0,
            draft_total=0,
            draft_worst_performers="No data",
            fine_results=fine_results,
        )

        assert "<fine_results>" in prompt
        assert "FINE TRIGGERED" in prompt
        assert "No sub-25 fine" in prompt
        assert "Pint on video" in prompt


class TestTransferVerdict:
    """Tests for transfer verdict logic."""

    def test_verdict_hit_positive_net(self):
        """Test verdict is Hit when net > 1."""
        # Simulating the verdict logic from cli.py
        net = 5
        if net > 1:
            verdict = "✓ Hit"
        elif net < -1:
            verdict = "✗ Miss"
        else:
            verdict = "→ Neutral"

        assert verdict == "✓ Hit"

    def test_verdict_miss_negative_net(self):
        """Test verdict is Miss when net < -1."""
        net = -3
        if net > 1:
            verdict = "✓ Hit"
        elif net < -1:
            verdict = "✗ Miss"
        else:
            verdict = "→ Neutral"

        assert verdict == "✗ Miss"

    def test_verdict_neutral_small_positive(self):
        """Test verdict is Neutral when net is +1."""
        net = 1
        if net > 1:
            verdict = "✓ Hit"
        elif net < -1:
            verdict = "✗ Miss"
        else:
            verdict = "→ Neutral"

        assert verdict == "→ Neutral"

    def test_verdict_neutral_small_negative(self):
        """Test verdict is Neutral when net is -1."""
        net = -1
        if net > 1:
            verdict = "✓ Hit"
        elif net < -1:
            verdict = "✗ Miss"
        else:
            verdict = "→ Neutral"

        assert verdict == "→ Neutral"

    def test_verdict_neutral_zero(self):
        """Test verdict is Neutral when net is 0."""
        net = 0
        if net > 1:
            verdict = "✓ Hit"
        elif net < -1:
            verdict = "✗ Miss"
        else:
            verdict = "→ Neutral"

        assert verdict == "→ Neutral"


class TestNetPointsCalculation:
    """Tests for net points calculation in Classic league fine check."""

    def test_net_points_sorting(self):
        """Test that worst performers are sorted by net points, not gross."""
        performers = [
            {
                "name": "Alice", "gross_points": 45, "transfer_cost": 0,
                "net_points": 45, "is_user": False
            },
            {
                "name": "Bob", "gross_points": 38, "transfer_cost": 0,
                "net_points": 38, "is_user": False
            },
            {
                "name": "Charlie", "gross_points": 50, "transfer_cost": 8,
                "net_points": 42, "is_user": False
            },
        ]
        # Sort by net points ascending (lowest = last place)
        sorted_performers = sorted(performers, key=lambda x: x["net_points"])

        # Bob (38 net) should be first (last place), not Charlie who had higher gross but hit
        assert sorted_performers[0]["name"] == "Bob"
        assert sorted_performers[0]["net_points"] == 38
        # Charlie (50 gross - 8 hit = 42 net) should be second
        assert sorted_performers[1]["name"] == "Charlie"
        assert sorted_performers[1]["net_points"] == 42

    def test_transfer_impact_user_hit_causes_last(self):
        """Test narrative when user's hit drops them to last place."""
        # Scenario: User scored 45 gross with -8 hit = 37 net
        # Other player scored 38 gross with no hit = 38 net
        # User would NOT be last by gross (45 > 38), but IS last by net (37 < 38)
        performers = [
            {
                "name": "You", "gross_points": 45, "transfer_cost": 8,
                "net_points": 37, "is_user": True
            },
            {
                "name": "Bob", "gross_points": 38, "transfer_cost": 0,
                "net_points": 38, "is_user": False
            },
        ]
        sorted_performers = sorted(performers, key=lambda x: x["net_points"])

        last_place = sorted_performers[0]
        user_entry = next((p for p in sorted_performers if p["is_user"]), None)
        user_is_last = user_entry == last_place
        user_transfer_cost = user_entry["transfer_cost"]

        assert user_is_last
        assert user_transfer_cost == 8

        # Check if user's hit caused them to drop to last
        if user_is_last and user_transfer_cost > 0:
            user_gross_rank = sorted(
                sorted_performers, key=lambda x: x["gross_points"]
            ).index(user_entry)
            if user_gross_rank > 0:  # Not last by gross
                transfer_impact = f"Your -{user_transfer_cost} hit dropped you to last place"
            else:
                transfer_impact = None
        else:
            transfer_impact = None

        assert transfer_impact == "Your -8 hit dropped you to last place"

    def test_transfer_impact_other_hit_saves_user(self):
        """Test narrative when someone else's hit saves the user from last place."""
        # Scenario: Bob scored 45 gross with -8 hit = 37 net
        # User scored 38 gross with no hit = 38 net
        # Bob is last by net, user is saved
        performers = [
            {
                "name": "Bob", "gross_points": 45, "transfer_cost": 8,
                "net_points": 37, "is_user": False
            },
            {
                "name": "You", "gross_points": 38, "transfer_cost": 0,
                "net_points": 38, "is_user": True
            },
        ]
        sorted_performers = sorted(performers, key=lambda x: x["net_points"])

        last_place = sorted_performers[0]
        user_entry = next((p for p in sorted_performers if p["is_user"]), None)
        user_is_last = user_entry == last_place

        assert not user_is_last
        assert last_place["name"] == "Bob"
        assert last_place["transfer_cost"] == 8

        # Check if someone else's hit saved the user
        if not user_is_last and last_place["transfer_cost"] > 0:
            # Would user be last if that person had no hit?
            last_without_hit = last_place["gross_points"]
            if user_entry["net_points"] < last_without_hit:
                name = last_place['name']
                cost = last_place['transfer_cost']
                transfer_impact = f"{name}'s -{cost} hit saved you from last place"
            else:
                transfer_impact = None
        else:
            transfer_impact = None

        assert transfer_impact == "Bob's -8 hit saved you from last place"

    def test_no_transfer_impact_when_no_hits(self):
        """Test no narrative when no transfer hits were taken."""
        performers = [
            {
                "name": "Alice", "gross_points": 38, "transfer_cost": 0,
                "net_points": 38, "is_user": False
            },
            {
                "name": "You", "gross_points": 42, "transfer_cost": 0,
                "net_points": 42, "is_user": True
            },
        ]
        sorted_performers = sorted(performers, key=lambda x: x["net_points"])

        last_place = sorted_performers[0]
        user_entry = next((p for p in sorted_performers if p["is_user"]), None)
        user_is_last = user_entry == last_place
        user_transfer_cost = user_entry["transfer_cost"]

        # Run the same logic as cli.py
        transfer_impact = None
        if user_entry:
            # Check if user's hit caused them to drop to last
            if user_is_last and user_transfer_cost > 0:
                user_gross_rank = sorted(
                    sorted_performers, key=lambda x: x["gross_points"]
                ).index(user_entry)
                if user_gross_rank > 0:
                    transfer_impact = f"Your -{user_transfer_cost} hit dropped you"
            # Check if someone else's hit saved the user
            elif not user_is_last and last_place["transfer_cost"] > 0:
                last_without_hit = last_place["gross_points"]
                if user_entry["net_points"] < last_without_hit:
                    name = last_place['name']
                    cost = last_place['transfer_cost']
                    transfer_impact = f"{name}'s -{cost} hit saved you"

        # No hits taken, so no narrative should be generated
        assert transfer_impact is None
        assert last_place["transfer_cost"] == 0
        assert user_entry["transfer_cost"] == 0

    def test_high_gross_player_with_hit_in_worst_performers(self):
        """Test that high-gross players with hits appear in worst performers.

        This tests the bug fix where we now fetch transfer costs for ALL managers,
        not just the bottom 5 by gross points. Previously, a player ranked 6th by
        gross (38 pts) with a -4 hit (34 net) wouldn't appear in worst performers
        because only the bottom 5 by gross were checked.
        """
        # Simulate an 11-person league like "Test League"
        # Key scenario: Ed (36 gross, -4 hit = 32 net) and Sam (38 gross, -4 hit = 34 net)
        # should both appear in worst 5 despite having higher gross than some others
        all_managers = [
            {"name": "Matt", "gross_points": 37, "transfer_cost": 0, "net_points": 37, "is_user": False},
            {"name": "Oliver", "gross_points": 36, "transfer_cost": 0, "net_points": 36, "is_user": False},
            {"name": "Alex", "gross_points": 35, "transfer_cost": 0, "net_points": 35, "is_user": False},
            {"name": "Ben", "gross_points": 38, "transfer_cost": 4, "net_points": 34, "is_user": False},  # Hit!
            {"name": "Sam", "gross_points": 38, "transfer_cost": 4, "net_points": 34, "is_user": False},  # Hit!
            {"name": "Ed", "gross_points": 36, "transfer_cost": 4, "net_points": 32, "is_user": False},   # Hit!
            {"name": "William", "gross_points": 31, "transfer_cost": 0, "net_points": 31, "is_user": False},
            {"name": "Walter", "gross_points": 30, "transfer_cost": 0, "net_points": 30, "is_user": False},
            {"name": "You", "gross_points": 30, "transfer_cost": 0, "net_points": 30, "is_user": True},
            {"name": "Cam", "gross_points": 34, "transfer_cost": 0, "net_points": 34, "is_user": False},
            {"name": "Bob", "gross_points": 34, "transfer_cost": 0, "net_points": 34, "is_user": False},
        ]

        # Sort all managers by net points ascending (lowest = last place)
        sorted_by_net = sorted(all_managers, key=lambda x: x["net_points"])

        # Get bottom 5 performers
        worst_5 = sorted_by_net[:5]
        worst_names = [p["name"] for p in worst_5]

        # Ed (32 net after -4 hit) should be in worst 5
        assert "Ed" in worst_names, "Ed with -4 hit should appear in worst performers"

        # Walter and You (30 net each) should be worst
        assert sorted_by_net[0]["net_points"] == 30
        assert sorted_by_net[1]["net_points"] == 30

        # Ed (32 net) should be 3rd worst
        ed_entry = next(p for p in sorted_by_net if p["name"] == "Ed")
        assert ed_entry["net_points"] == 32
        assert ed_entry["transfer_cost"] == 4

        # Verify hit display format would be correct
        if ed_entry["transfer_cost"] > 0:
            display = f"{ed_entry['net_points']} net pts ({ed_entry['gross_points']} gross, -{ed_entry['transfer_cost']} hit)"
        else:
            display = f"{ed_entry['net_points']} pts"
        assert display == "32 net pts (36 gross, -4 hit)"

    def test_prompt_includes_net_points_data(self):
        """Test that the prompt function includes worst performers with net points."""
        _, prompt = get_review_synthesis_prompt(
            gameweek=22,
            research_summary="Test summary",
            classic_points=55,
            classic_average=50,
            classic_highest=95,
            classic_gw_rank=500000,
            classic_overall_rank=100000,
            classic_captain="Salah",
            classic_captain_points=14,
            classic_captain_hindsight="N/A",
            classic_players="- Salah (LIV): 14 pts (C)",
            classic_transfers="No transfers this week",
            classic_league_name="Test League",
            classic_gw_position=10,
            classic_position=5,
            classic_total=11,
            classic_rivals="- 4. John: 1,200 pts",
            classic_worst_performers="1. Bob - 37 net pts (45 gross, -8 hit)\n2. You - 38 pts",
            classic_transfer_impact="Bob's -8 hit saved you from last place",
            draft_points=42,
            draft_league_name="Draft League",
            draft_players="- Haaland (MCI): 8 pts",
            draft_transactions="No waivers this week",
            draft_gw_position=3,
            draft_position=3,
            draft_total=10,
            draft_worst_performers="1. Steve - 28 pts\n2. Bob - 31 pts",
        )

        # Check worst performers section is in prompt (no suffix without use_net_points)
        assert "## Worst GW Performers\n" in prompt
        assert "37 net pts (45 gross, -8 hit)" in prompt
        assert "Bob's -8 hit saved you from last place" in prompt
        # Fine results section absent when no fines passed
        assert "<fine_results>" not in prompt


class TestFixtureGroundTruth:
    """Verify synthesis prompt exposes authoritative DGW/BGW team lists."""

    def _build(self, **overrides):
        base = dict(
            gameweek=33, research_summary="test", classic_points=109,
            classic_average=66, classic_highest=177, classic_gw_rank=1,
            classic_overall_rank=1, classic_captain="X (10 pts = 5 raw × 2)",
            classic_captain_points=10, classic_captain_hindsight="N/A",
            classic_players="-", classic_transfers="-",
            classic_league_name="L", classic_gw_position=1, classic_position=1,
            classic_total=10, classic_rivals="-", classic_worst_performers="-",
            classic_transfer_impact=None, draft_points=53, draft_league_name="D",
            draft_players="-", draft_transactions="-", draft_gw_position=1,
            draft_position=1, draft_total=10,
        )
        base.update(overrides)
        _, prompt = get_review_synthesis_prompt(**base)
        return prompt

    def test_dgw_bgw_lines_present_when_provided(self):
        prompt = self._build(dgw_teams="BHA, BOU, CHE", bgw_teams="ARS")
        assert "<gw_fixtures>" in prompt
        assert "Double Gameweek teams (played twice): BHA, BOU, CHE" in prompt
        assert "Blank Gameweek teams (did not play): ARS" in prompt

    def test_dgw_bgw_defaults_when_absent(self):
        prompt = self._build()
        assert "Double Gameweek teams (played twice): none this gameweek" in prompt
        assert "Blank Gameweek teams (did not play): none this gameweek" in prompt


class TestCaptainHindsight:
    """Verify the hindsight-best-captain string is computed raw-to-raw."""

    def _ctx(self, team):
        from fpl_cli.cli._review_summarisation import _format_league_context
        return _format_league_context(
            classic_league_data=None, draft_league_data=None,
            team_points_data=team, draft_squad_points_data=[], settings={},
        )

    def test_captain_suboptimal_names_better_alternative_with_swing(self):
        team = [
            {"name": "Welbeck", "points": 16, "display_points": 32, "contributed": True,
             "is_captain": True, "is_vice": False, "is_triple_captain": False},
            {"name": "Gibbs-White", "points": 20, "display_points": 20, "contributed": True,
             "is_captain": False, "is_vice": False},
            {"name": "Haaland", "points": 13, "display_points": 13, "contributed": True,
             "is_captain": False, "is_vice": False},
        ]
        s = self._ctx(team)["captain_hindsight"]
        assert "Gibbs-White" in s
        assert "20 raw" in s and "16 raw" in s
        assert "+4" in s  # (20-16)*(2-1) — armband swap only adds the extra multiplier

    def test_captain_optimal_confirmed(self):
        team = [
            {"name": "Welbeck", "points": 20, "display_points": 40, "contributed": True,
             "is_captain": True, "is_vice": False, "is_triple_captain": False},
            {"name": "Haaland", "points": 13, "display_points": 13, "contributed": True,
             "is_captain": False, "is_vice": False},
        ]
        s = self._ctx(team)["captain_hindsight"]
        assert "optimal captain" in s and "Welbeck" in s

    def test_captain_triple_captain_uses_triple_multiplier(self):
        team = [
            {"name": "Salah", "points": 10, "display_points": 30, "contributed": True,
             "is_captain": True, "is_vice": False, "is_triple_captain": True},
            {"name": "Haaland", "points": 15, "display_points": 15, "contributed": True,
             "is_captain": False, "is_vice": False},
        ]
        s = self._ctx(team)["captain_hindsight"]
        assert "Haaland" in s
        assert "+10" in s  # (15-10)*(3-1) — TC adds 2× extra on the raw diff
        assert "×3" in s

    def test_captain_dnp_baselines_off_vice_not_zero(self):
        # Captain didn't play; vice auto-promoted and got the multiplier.
        # Swing must be (best_alt - vice_raw) * multiplier, NOT best_alt * multiplier.
        team = [
            {"name": "Salah", "points": 0, "display_points": 0, "contributed": False,
             "is_captain": True, "is_vice": False, "is_vice_active": False,
             "is_triple_captain": False},
            {"name": "Saka", "points": 9, "display_points": 18, "contributed": True,
             "is_captain": False, "is_vice": True, "is_vice_active": True},
            {"name": "Palmer", "points": 12, "display_points": 12, "contributed": True,
             "is_captain": False, "is_vice": False, "is_vice_active": False},
        ]
        s = self._ctx(team)["captain_hindsight"]
        assert "Palmer" in s
        assert "12 raw" in s and "9 raw" in s  # baselined off vice Saka (9), not captain (0)
        assert "+3" in s  # (12 - 9) * (2 - 1) — armband-swap delta off the vice baseline
        assert "+6" not in s  # old buggy (best - baseline) * multiplier
        assert "+24" not in s  # would be the wrong (best * 2) inflation
        assert "vice" in s.lower()

    def test_captain_swing_gw34_regression(self):
        # Regression: GW34 review reported "+16 pts with the armband" for
        # Bruno (5 raw, captained) vs Gibbs-White (13 raw). Correct swing is
        # (13 - 5) * (2 - 1) = 8, because Gibbs-White's 13 already counted
        # uncaptained — the armband swap only changes the extra multiplier.
        team = [
            {"name": "B.Fernandes", "points": 5, "display_points": 10, "contributed": True,
             "is_captain": True, "is_vice": False, "is_triple_captain": False},
            {"name": "Gibbs-White", "points": 13, "display_points": 13, "contributed": True,
             "is_captain": False, "is_vice": False},
        ]
        s = self._ctx(team)["captain_hindsight"]
        assert "Gibbs-White" in s
        assert "+8" in s
        assert "+16" not in s

    def test_captain_dnp_and_vice_dnp_skips_hindsight(self):
        team = [
            {"name": "Salah", "points": 0, "display_points": 0, "contributed": False,
             "is_captain": True, "is_vice": False, "is_vice_active": False,
             "is_triple_captain": False},
            {"name": "Saka", "points": 0, "display_points": 0, "contributed": False,
             "is_captain": False, "is_vice": True, "is_vice_active": True},
            {"name": "Palmer", "points": 8, "display_points": 8, "contributed": True,
             "is_captain": False, "is_vice": False, "is_vice_active": False},
        ]
        s = self._ctx(team)["captain_hindsight"]
        assert "no raw baseline" in s

    def test_captain_dnp_vice_optimal_no_swing(self):
        # Captain DNP'd; vice was actually the highest raw scorer — no better alt.
        team = [
            {"name": "Salah", "points": 0, "display_points": 0, "contributed": False,
             "is_captain": True, "is_vice": False, "is_vice_active": False,
             "is_triple_captain": False},
            {"name": "Saka", "points": 14, "display_points": 28, "contributed": True,
             "is_captain": False, "is_vice": True, "is_vice_active": True},
            {"name": "Palmer", "points": 10, "display_points": 10, "contributed": True,
             "is_captain": False, "is_vice": False, "is_vice_active": False},
        ]
        s = self._ctx(team)["captain_hindsight"]
        assert "Saka" in s and "optimal captain" in s


class TestLeagueContextUserMasking:
    """Verify the user row is rendered as 'You' in LLM-facing league context strings."""

    def _context(self, classic=None, draft=None):
        from fpl_cli.cli._review_summarisation import _format_league_context
        return _format_league_context(
            classic_league_data=classic,
            draft_league_data=draft,
            team_points_data=[],
            draft_squad_points_data=[],
            settings={},
        )

    def test_draft_worst_performers_masks_user(self):
        ctx = self._context(draft={
            "worst_performers": [
                {"rank_str": "1", "name": "Alex", "points": 40, "is_user": False},
                {"rank_str": "3", "name": "Manager", "points": 53, "is_user": True},
            ],
        })
        assert "You - 53 pts" in ctx["draft_worst_performers"]
        assert "Manager" not in ctx["draft_worst_performers"]

    def test_classic_nearby_rivals_masks_user(self):
        ctx = self._context(classic={
            "nearby_rivals": [
                {"rank": 4, "manager_name": "John", "total": 1200, "is_user": False},
                {"rank": 5, "manager_name": "Manager", "total": 1180, "is_user": True},
            ],
        })
        assert "5. You: 1,180 pts" in ctx["classic_rivals"]
        assert "Manager" not in ctx["classic_rivals"]


class TestAutoSubFormatting:
    """Tests for auto-sub player string formatting."""

    def _format_player(self, p):
        """Delegate to the real extracted helper."""
        return _format_review_classic_player(p)

    def test_format_classic_player_auto_sub_in(self):
        """Test formatting a player who came on via auto-sub."""
        player = {
            "name": "Watkins",
            "team": "AVL",
            "position": "FWD",
            "display_points": 8,
            "contributed": True,
            "is_captain": False,
            "red_cards": 0,
            "auto_sub_in": True,
            "auto_sub_out": False,
        }

        line = self._format_player(player)
        assert line == "- Watkins (AVL, FWD): 8 [AUTO-SUB IN] pts"
        assert "[AUTO-SUB IN]" in line

    def test_format_classic_player_auto_sub_out(self):
        """Test formatting a starter who was auto-subbed out."""
        player = {
            "name": "Salah",
            "team": "LIV",
            "position": "MID",
            "display_points": 0,
            "contributed": False,
            "is_captain": False,
            "red_cards": 0,
            "auto_sub_in": False,
            "auto_sub_out": True,
        }

        line = self._format_player(player)
        assert line == "- Salah (LIV, MID): (0) [DIDN'T PLAY - auto-subbed out] pts"
        assert "[DIDN'T PLAY - auto-subbed out]" in line

    def test_format_classic_player_auto_sub_in_under_bb(self):
        player = {
            "name": "Williams", "team": "NOT", "position": "DEF",
            "display_points": 2, "contributed": True, "is_captain": False,
            "red_cards": 0, "auto_sub_in": True, "auto_sub_out": False,
            "bb_no_sub_impact": True,
        }
        line = self._format_player(player)
        assert "no points impact: BB active" in line

    def test_format_classic_player_auto_sub_out_under_bb(self):
        player = {
            "name": "Gordon", "team": "NEW", "position": "MID",
            "display_points": 0, "contributed": True, "is_captain": False,
            "red_cards": 0, "auto_sub_in": False, "auto_sub_out": True,
            "bb_no_sub_impact": True,
        }
        line = self._format_player(player)
        assert "no points impact: BB active" in line

    def test_format_classic_player_bench_high_points(self):
        """Test formatting a bench player with high points (unused)."""
        player = {
            "name": "Gordon",
            "team": "NEW",
            "position": "MID",
            "display_points": 13,
            "contributed": False,
            "is_captain": False,
            "red_cards": 0,
            "auto_sub_in": False,
            "auto_sub_out": False,
        }

        line = self._format_player(player)
        assert line == "- Gordon (NEW, MID): (13) [BENCH - 13 pts unused!] pts"
        assert "[BENCH - 13 pts unused!]" in line

    def test_format_classic_player_bench_low_points(self):
        """Test formatting a bench player with low points (no warning)."""
        player = {
            "name": "Neto",
            "team": "ARS",
            "position": "MID",
            "display_points": 2,
            "contributed": False,
            "is_captain": False,
            "red_cards": 0,
            "auto_sub_in": False,
            "auto_sub_out": False,
        }

        line = self._format_player(player)
        assert line == "- Neto (ARS, MID): (2) [BENCH] pts"
        assert "[BENCH]" in line
        assert "unused" not in line

    def test_format_classic_player_normal_starter(self):
        """Test formatting a normal starting player (no auto-sub)."""
        player = {
            "name": "Haaland",
            "team": "MCI",
            "position": "FWD",
            "display_points": 12,
            "contributed": True,
            "is_captain": True,
            "red_cards": 0,
            "auto_sub_in": False,
            "auto_sub_out": False,
        }

        line = self._format_player(player)
        assert line == "- Haaland (MCI, FWD): 12 pts (C)"
        assert "[AUTO-SUB" not in line
        assert "[BENCH" not in line

    def test_auto_sub_summary_generation(self):
        """Test that auto-sub summary is correctly generated."""
        # Simulate the auto-sub summary logic from cli.py
        automatic_subs = [
            {"element_in": 123, "element_out": 456},
            {"element_in": 789, "element_out": 101},
        ]

        # Mock player data
        class MockPlayer:
            def __init__(self, id, web_name):
                self.id = id
                self.web_name = web_name

        player_map = {
            123: MockPlayer(123, "Watkins"),
            456: MockPlayer(456, "Salah"),
            789: MockPlayer(789, "Gordon"),
            101: MockPlayer(101, "Palmer"),
        }

        team_points_data = [
            {"name": "Watkins", "points": 8},
            {"name": "Gordon", "points": 6},
        ]

        # Build the summary like cli.py does
        sub_details = []
        for sub in automatic_subs:
            in_player = player_map.get(sub["element_in"])
            out_player = player_map.get(sub["element_out"])
            if in_player and out_player:
                in_data = next((p for p in team_points_data if p["name"] == in_player.web_name), None)
                in_pts = in_data["points"] if in_data else 0
                sub_details.append(f"{in_player.web_name} on for {out_player.web_name} ({in_pts} pts)")

        summary = f"Auto-subs: {', '.join(sub_details)}"

        assert "Watkins on for Salah (8 pts)" in summary
        assert "Gordon on for Palmer (6 pts)" in summary

    def test_format_classic_section_bb_auto_sub_suffix(self):
        """Under Bench Boost, the Auto-subs line must declare zero points impact."""
        from fpl_cli.cli._review_summarisation import _format_classic_section

        class MockPlayer:
            def __init__(self, id, web_name):
                self.id = id
                self.web_name = web_name

        automatic_subs = [{"element_in": 1, "element_out": 2}]
        player_map = {1: MockPlayer(1, "Williams"), 2: MockPlayer(2, "Salah")}
        team_points_data = [{
            "name": "Williams", "team": "NOT", "position": "DEF",
            "points": 2, "display_points": 2, "contributed": True,
            "is_captain": False, "red_cards": 0,
            "auto_sub_in": True, "auto_sub_out": False,
        }]

        bb_result = _format_classic_section(
            team_points_data, automatic_subs, player_map, [], active_chip="bboost",
        )
        assert "Auto-subs: Williams on for Salah (2 pts)" in bb_result["players"]
        assert "no points impact: Bench Boost active" in bb_result["players"]

        no_chip_result = _format_classic_section(
            team_points_data, automatic_subs, player_map, [], active_chip=None,
        )
        assert "Auto-subs: Williams on for Salah (2 pts)" in no_chip_result["players"]
        assert "no points impact" not in no_chip_result["players"]

    def test_system_prompt_auto_sub_context(self):
        """Test that the system prompt includes auto-sub context."""
        assert "[AUTO-SUB IN]" in _build_system_prompt(has_fines=True)
        assert "[DIDN'T PLAY - auto-subbed out]" in _build_system_prompt(has_fines=True)
        assert "[BENCH - X pts unused!]" in _build_system_prompt(has_fines=True)
        assert "Analyse auto-sub outcomes" in _build_system_prompt(has_fines=True)


class TestResearchPromptWithGWData:
    """Tests for research prompt with Dream Team and Blankers data."""

    def test_research_prompt_without_gw_data(self):
        """Test research prompt works without dream_team/blankers data."""
        prompt = get_review_research_prompt(gameweek=22)
        assert "Gameweek 22" in prompt
        assert "GW22" in prompt
        # Should not have gw_results section when no data provided
        assert "<gw_results>" not in prompt

    def test_research_prompt_with_dream_team(self):
        """Test research prompt includes Dream Team when provided."""
        dream_team = """| Player | Team | Pos | Pts |
|--------|------|-----|-----|
| Dorgu | MUN | DEF | 15 |
| Sánchez | CHE | GK | 11 |
| Wirtz | LIV | MID | 10 |"""

        prompt = get_review_research_prompt(gameweek=22, dream_team=dream_team)

        assert "<gw_results>" in prompt
        assert "GW22 Dream Team (Official Top Performers)" in prompt
        assert "Dorgu | MUN | DEF | 15" in prompt
        assert "Sánchez | CHE | GK | 11" in prompt
        assert "MUST feature players from the Dream Team above" in prompt

    def test_research_prompt_with_blankers(self):
        """Test research prompt includes Blankers when provided."""
        blankers = """| Player | Team | Ownership | Pts |
|--------|------|-----------|-----|
| Haaland | MCI | 74.1% | 2 |
| Semenyo | MCI | 45.4% | 2 |
| Guéhi | MCI | 38.1% | 0 |"""

        prompt = get_review_research_prompt(gameweek=22, blankers=blankers)

        assert "<gw_results>" in prompt
        assert "GW22 Disappointments (High-Ownership Blankers)" in prompt
        assert "Haaland | MCI | 74.1% | 2" in prompt
        assert "MUST feature players from the Blankers list above" in prompt

    def test_research_prompt_with_both_dream_team_and_blankers(self):
        """Test research prompt includes both Dream Team and Blankers."""
        dream_team = """| Player | Team | Pos | Pts |
|--------|------|-----|-----|
| Dorgu | MUN | DEF | 15 |"""

        blankers = """| Player | Team | Ownership | Pts |
|--------|------|-----------|-----|
| Haaland | MCI | 74.1% | 2 |"""

        prompt = get_review_research_prompt(
            gameweek=22,
            dream_team=dream_team,
            blankers=blankers,
        )

        assert "<gw_results>" in prompt
        assert "GW22 Dream Team (Official Top Performers)" in prompt
        assert "GW22 Disappointments (High-Ownership Blankers)" in prompt
        assert "Dorgu | MUN | DEF | 15" in prompt
        assert "Haaland | MCI | 74.1% | 2" in prompt
        assert "</gw_results>" in prompt

    def test_research_prompt_instructions_for_grounding(self):
        """Test research prompt includes explicit grounding instructions."""
        dream_team = "| Dorgu | MUN | DEF | 15 |"
        blankers = "| Haaland | MCI | 74.1% | 2 |"

        prompt = get_review_research_prompt(
            gameweek=22,
            dream_team=dream_team,
            blankers=blankers,
        )

        # Check for explicit grounding instructions
        assert "MUST feature players from the Dream Team above" in prompt
        assert "MUST feature players from the Blankers list above" in prompt
        assert "Do not highlight players based on general form or transfer trends" in prompt
        assert "use the actual GW data provided" in prompt

    def test_research_prompt_with_manager_context(self):
        """Test research prompt includes manager context when provided."""
        manager_context = "Current PL managers: ARS: Arteta, CHE: Rosenior, LIV: Slot"
        match_results = "ARS 2-1 CHE | LIV 3-0 MUN"

        prompt = get_review_research_prompt(
            gameweek=25,
            match_results=match_results,
            manager_context=manager_context,
        )

        assert "<team_context>" in prompt
        assert "CHE: Rosenior" in prompt
        assert "ARS: Arteta" in prompt
        assert "</team_context>" in prompt
        # team_context should appear inside gw_results
        assert prompt.index("<team_context>") > prompt.index("<gw_results>")
        assert prompt.index("</team_context>") < prompt.index("</gw_results>")

    def test_research_prompt_without_manager_context(self):
        """Test research prompt omits team_context when no manager data."""
        prompt = get_review_research_prompt(
            gameweek=25,
            match_results="ARS 2-1 CHE",
        )

        assert "<team_context>" not in prompt

    def test_research_system_prompt_manager_grounding_rule(self):
        """Test research system prompt includes manager grounding rule."""
        assert "manager names provided in <team_context>" in REVIEW_RESEARCH_SYSTEM_PROMPT
        assert "do not substitute from training data" in REVIEW_RESEARCH_SYSTEM_PROMPT

    def test_research_prompt_with_bgw_teams(self):
        prompt = get_review_research_prompt(
            gameweek=31,
            match_results="EVE 3-0 CHE",
            bgw_teams="ARS, CRY, MCI, WOL",
        )
        assert "Blank Gameweek Teams" in prompt
        assert "ARS, CRY, MCI, WOL" in prompt
        assert "did NOT play" in prompt

    def test_research_prompt_with_dgw_teams(self):
        prompt = get_review_research_prompt(
            gameweek=31,
            match_results="EVE 3-0 CHE",
            dgw_teams="EVE, BHA",
        )
        assert "Double Gameweek Teams" in prompt
        assert "EVE, BHA" in prompt
        assert "played TWICE" in prompt

    def test_research_prompt_no_bgw_dgw_omits_sections(self):
        prompt = get_review_research_prompt(
            gameweek=31,
            match_results="EVE 3-0 CHE",
        )
        assert "Blank Gameweek" not in prompt
        assert "Double Gameweek" not in prompt

    def test_research_system_prompt_bgw_never_rules(self):
        assert "blank-gameweek teams as disappointments" in REVIEW_RESEARCH_SYSTEM_PROMPT
        assert "fabricate match narratives" in REVIEW_RESEARCH_SYSTEM_PROMPT

    def test_research_system_prompt_dgw_always_rule(self):
        assert "double-gameweek teams" in REVIEW_RESEARCH_SYSTEM_PROMPT
        assert "two matches" in REVIEW_RESEARCH_SYSTEM_PROMPT

    def test_research_system_prompt_team_code_not_surname_rule(self):
        """Rule preventing LLM from rendering team codes (LEE, NEW, MAN) as surnames."""
        assert "LEE is Leeds United" in REVIEW_RESEARCH_SYSTEM_PROMPT
        assert "surnames" in REVIEW_RESEARCH_SYSTEM_PROMPT

    def test_research_prompt_with_team_glossary(self):
        prompt = get_review_research_prompt(
            gameweek=32,
            match_results="MUN 1-2 LEE",
            team_glossary="ARS = Arsenal, LEE = Leeds United, NEW = Newcastle",
        )
        assert "<team_context>" in prompt
        assert "Team code glossary" in prompt
        assert "LEE = Leeds United" in prompt

    def test_research_prompt_glossary_omitted_when_empty(self):
        prompt = get_review_research_prompt(
            gameweek=32,
            match_results="MUN 1-2 LEE",
        )
        assert "Team code glossary" not in prompt

    def test_research_system_prompt_dgw_never_speculation_rule(self):
        assert "Speculate about future double or blank gameweeks" in REVIEW_RESEARCH_SYSTEM_PROMPT

    def test_research_prompt_with_predicted_dgw_teams(self):
        prompt = get_review_research_prompt(
            gameweek=31,
            match_results="EVE 3-0 CHE",
            predicted_dgw_teams="GW33: EVE, BHA (high confidence)\nGW35: MCI (medium confidence)",
        )
        assert "Predicted Double Gameweeks (upcoming)" in prompt
        assert "GW33: EVE, BHA (high confidence)" in prompt
        assert "GW35: MCI (medium confidence)" in prompt

    def test_research_prompt_empty_predicted_dgw_omits_section(self):
        prompt = get_review_research_prompt(
            gameweek=31,
            match_results="EVE 3-0 CHE",
            predicted_dgw_teams="",
        )
        assert "Predicted Double Gameweeks" not in prompt


class TestFormatResearchContext:
    """Tests for _format_research_context predicted DGW formatting."""

    def test_predicted_dgws_formatted(self):
        from fpl_cli.cli._review_analysis import GlobalReviewData
        from fpl_cli.cli._review_summarisation import _format_research_context
        from fpl_cli.services.fixture_predictions import Confidence, DoublePrediction

        global_data: GlobalReviewData = {
            "predicted_dgw_teams": [
                DoublePrediction(gameweek=33, teams=["EVE", "BHA"], confidence=Confidence.HIGH),
                DoublePrediction(gameweek=35, teams=["MCI"], confidence=Confidence.MEDIUM),
            ],
        }
        result = _format_research_context(global_data, {})
        assert result["predicted_dgw_teams"] == (
            "GW33: EVE, BHA (high confidence)\n"
            "GW35: MCI (medium confidence)"
        )

    def test_empty_predicted_dgws_returns_empty_string(self):
        from fpl_cli.cli._review_analysis import GlobalReviewData
        from fpl_cli.cli._review_summarisation import _format_research_context

        global_data: GlobalReviewData = {}
        result = _format_research_context(global_data, {})
        assert result["predicted_dgw_teams"] == ""

    def test_blankers_include_position_column(self):
        """LLM receives position per blanker so it never has to infer positions from context."""
        from fpl_cli.cli._review_analysis import GlobalReviewData
        from fpl_cli.cli._review_summarisation import _format_research_context

        global_data: GlobalReviewData = {
            "blankers": [
                {"name": "Haaland", "team": "MCI", "position": "FWD", "ownership": 74.1, "points": 2},
                {"name": "Van Hecke", "team": "BHA", "position": "DEF", "ownership": 12.4, "points": 1},
            ],
        }
        result = _format_research_context(global_data, {})
        assert "| Pos |" in result["blankers"]
        assert "| Haaland | MCI | FWD |" in result["blankers"]
        assert "| Van Hecke | BHA | DEF |" in result["blankers"]


class TestBlankersCalculation:
    """Tests for blankers calculation logic."""

    def test_blankers_filter_by_ownership_and_points(self):
        """Test blankers are filtered by ownership >5% and points ≤2."""
        # Simulate the blankers calculation logic from cli.py
        elements_live = [
            {"id": 1, "stats": {"total_points": 2}},  # Blanked
            {"id": 2, "stats": {"total_points": 5}},  # Didn't blank
            {"id": 3, "stats": {"total_points": 0}},  # Blanked
            {"id": 4, "stats": {"total_points": 1}},  # Blanked
            {"id": 5, "stats": {"total_points": 2}},  # Blanked but low ownership
        ]

        # Mock player data with ownership
        class MockPlayer:
            def __init__(self, id, web_name, selected_by_percent, team_id):
                self.id = id
                self.web_name = web_name
                self.selected_by_percent = selected_by_percent
                self.team_id = team_id

        player_map = {
            1: MockPlayer(1, "Haaland", 74.1, 1),   # High ownership, blanked
            2: MockPlayer(2, "Salah", 50.0, 2),     # High ownership, didn't blank
            3: MockPlayer(3, "Guéhi", 38.1, 3),     # High ownership, blanked
            4: MockPlayer(4, "Foden", 27.5, 1),     # High ownership, blanked
            5: MockPlayer(5, "Neto", 3.2, 4),       # Low ownership, blanked
        }

        # Calculate blankers (ownership >5%, points ≤2)
        blankers_list = []
        for elem in elements_live:
            elem_id = elem["id"]
            gw_pts = elem["stats"]["total_points"]
            player = player_map.get(elem_id)
            if player and gw_pts <= 2:
                ownership = player.selected_by_percent
                if ownership > 5.0:
                    blankers_list.append({
                        "name": player.web_name,
                        "ownership": ownership,
                        "points": gw_pts,
                    })

        # Sort by ownership descending
        blankers_list.sort(key=lambda x: x["ownership"], reverse=True)

        assert len(blankers_list) == 3
        assert blankers_list[0]["name"] == "Haaland"
        assert blankers_list[0]["ownership"] == 74.1
        assert blankers_list[1]["name"] == "Guéhi"
        assert blankers_list[2]["name"] == "Foden"
        # Neto should NOT be included (ownership < 5%)
        assert not any(b["name"] == "Neto" for b in blankers_list)
        # Salah should NOT be included (scored 5 pts)
        assert not any(b["name"] == "Salah" for b in blankers_list)

    def test_blankers_limited_to_top_10(self):
        """Test blankers list is limited to top 10 by ownership."""
        # Create 15 blankers with varying ownership
        blankers_list = [
            {"name": f"Player{i}", "ownership": 50.0 - i, "points": 2}
            for i in range(15)
        ]

        # Sort by ownership descending, take top 10
        blankers_list.sort(key=lambda x: x["ownership"], reverse=True)
        blankers_list = blankers_list[:10]

        assert len(blankers_list) == 10
        assert blankers_list[0]["ownership"] == 50.0  # Player0
        assert blankers_list[9]["ownership"] == 41.0  # Player9

    def test_blankers_exclude_bgw_teams(self):
        """BGW team players should not appear in blankers even with high ownership."""
        elements_live = [
            {"id": 1, "stats": {"total_points": 0}},  # BGW team
            {"id": 2, "stats": {"total_points": 0}},  # Non-BGW team
        ]

        class MockPlayer:
            def __init__(self, id, web_name, selected_by_percent, team_id):
                self.id = id
                self.web_name = web_name
                self.selected_by_percent = selected_by_percent
                self.team_id = team_id

        player_map = {
            1: MockPlayer(1, "Haaland", 54.9, 10),  # MCI, team_id=10, BGW
            2: MockPlayer(2, "Gabriel", 43.1, 20),   # Non-BGW team
        }
        bgw_team_ids = {10}  # MCI

        blankers_list = []
        for elem in elements_live:
            elem_id = elem["id"]
            gw_pts = elem["stats"]["total_points"]
            player = player_map.get(elem_id)
            if player and gw_pts <= 2 and player.team_id not in bgw_team_ids:
                ownership = player.selected_by_percent
                if ownership > 5.0:
                    blankers_list.append({"name": player.web_name, "ownership": ownership})

        assert len(blankers_list) == 1
        assert blankers_list[0]["name"] == "Gabriel"
        assert not any(b["name"] == "Haaland" for b in blankers_list)

    def test_blankers_format_for_prompt(self):
        """Blankers are formatted with Pos so the LLM never has to infer position."""
        blankers_list = [
            {"name": "Haaland", "team": "MCI", "position": "FWD", "ownership": 74.1, "points": 2},
            {"name": "Semenyo", "team": "BOU", "position": "MID", "ownership": 45.4, "points": 2},
        ]

        blankers_lines = [
            "| Player | Team | Pos | Ownership | Pts |",
            "|--------|------|-----|-----------|-----|",
        ]
        for b in blankers_list:
            blankers_lines.append(
                f"| {b['name']} | {b['team']} | {b.get('position', '???')} | {b['ownership']:.1f}% | {b['points']} |"
            )
        blankers_str = "\n".join(blankers_lines)

        assert "| Haaland | MCI | FWD | 74.1% | 2 |" in blankers_str
        assert "| Semenyo | BOU | MID | 45.4% | 2 |" in blankers_str


class TestBenchAnalysis:
    """Tests for compute_bench_analysis formation-aware bench comparison."""

    def _make_player(self, name, position, points, contributed=True,
                     auto_sub_in=False, auto_sub_out=False,
                     display_points=None, team="TST"):
        """Helper to build a player dict for bench analysis tests."""
        return {
            "name": name,
            "position": position,
            "points": points,
            "display_points": display_points if display_points is not None else points,
            "team": team,
            "contributed": contributed,
            "auto_sub_in": auto_sub_in,
            "auto_sub_out": auto_sub_out,
        }

    def _make_442_team(self):
        """Build a standard 4-4-2 starting XI with bench."""
        return [
            # Starters (4-4-2)
            self._make_player("GK1", "GK", 3),
            self._make_player("DEF1", "DEF", 6),
            self._make_player("DEF2", "DEF", 5),
            self._make_player("Konsa", "DEF", 2),
            self._make_player("Murillo", "DEF", 2),
            self._make_player("MID1", "MID", 7),
            self._make_player("MID2", "MID", 4),
            self._make_player("MID3", "MID", 3),
            self._make_player("MID4", "MID", 2),
            self._make_player("FWD1", "FWD", 8),
            self._make_player("FWD2", "FWD", 3),
            # Bench
            self._make_player("GK2", "GK", 1, contributed=False),
            self._make_player("O'Brien", "DEF", 5, contributed=False),
            self._make_player("BenchMID", "MID", 1, contributed=False),
            self._make_player("BenchFWD", "FWD", 1, contributed=False),
        ]

    def test_same_position_bench_outscores_starter(self):
        """O'Brien (DEF, 5) on bench should flag against Konsa (DEF, 2) and Murillo (DEF, 2)."""
        team = self._make_442_team()
        result = compute_bench_analysis(team)
        assert result is not None
        assert "O'Brien" in result
        assert "Konsa" in result
        assert "Murillo" in result
        # Same-position DEF swaps should appear without [formation change] tag
        # (they may be followed by cross-position swaps that do have the tag)
        assert "Konsa (DEF, 2)," in result or result.endswith("Konsa (DEF, 2)")
        assert "Konsa (DEF, 2) [formation change]" not in result
        assert "Murillo (DEF, 2) [formation change]" not in result

    def test_cross_position_valid_formation(self):
        """Bench MID outscores starting DEF in 4-4-2 -> valid 3-5-2."""
        team = [
            self._make_player("GK1", "GK", 3),
            self._make_player("DEF1", "DEF", 6),
            self._make_player("DEF2", "DEF", 5),
            self._make_player("DEF3", "DEF", 4),
            self._make_player("DEF4", "DEF", 1),  # Low-scoring DEF
            self._make_player("MID1", "MID", 7),
            self._make_player("MID2", "MID", 4),
            self._make_player("MID3", "MID", 3),
            self._make_player("MID4", "MID", 2),
            self._make_player("FWD1", "FWD", 8),
            self._make_player("FWD2", "FWD", 3),
            # Bench
            self._make_player("GK2", "GK", 1, contributed=False),
            self._make_player("SuperMID", "MID", 10, contributed=False),
            self._make_player("BenchDEF", "DEF", 0, contributed=False),
            self._make_player("BenchFWD", "FWD", 0, contributed=False),
        ]
        result = compute_bench_analysis(team)
        assert result is not None
        assert "SuperMID" in result
        assert "DEF4" in result
        assert "[formation change]" in result

    def test_cross_position_invalid_formation(self):
        """Bench MID can't replace DEF when already at 3 DEF minimum."""
        team = [
            # 3-5-2 formation (already at DEF minimum)
            self._make_player("GK1", "GK", 3),
            self._make_player("DEF1", "DEF", 6),
            self._make_player("DEF2", "DEF", 5),
            self._make_player("DEF3", "DEF", 1),  # Low-scoring DEF
            self._make_player("MID1", "MID", 7),
            self._make_player("MID2", "MID", 4),
            self._make_player("MID3", "MID", 3),
            self._make_player("MID4", "MID", 2),
            self._make_player("MID5", "MID", 2),
            self._make_player("FWD1", "FWD", 8),
            self._make_player("FWD2", "FWD", 3),
            # Bench
            self._make_player("GK2", "GK", 1, contributed=False),
            self._make_player("BenchMID", "MID", 5, contributed=False),
            self._make_player("BenchDEF", "DEF", 0, contributed=False),
            self._make_player("BenchFWD", "FWD", 0, contributed=False),
        ]
        result = compute_bench_analysis(team)
        # BenchMID (5) outscores DEF3 (1) but swap would drop DEF to 2 (below min 3)
        # BenchMID also outscores MID4 and MID5 (both 2) - same position, valid
        assert result is not None
        assert "BenchMID" in result
        # DEF3 should NOT appear (invalid cross-position swap)
        bench_mid_line = [line for line in result.split("\n") if "BenchMID" in line][0]
        assert "DEF3" not in bench_mid_line
        # But same-position MID swaps should appear
        assert "MID4" in bench_mid_line or "MID5" in bench_mid_line

    def test_gk_only_swaps_with_gk(self):
        """Bench GK cannot replace outfield starters, even if outscoring them."""
        team = [
            self._make_player("GK1", "GK", 1),
            self._make_player("DEF1", "DEF", 6),
            self._make_player("DEF2", "DEF", 5),
            self._make_player("DEF3", "DEF", 4),
            self._make_player("DEF4", "DEF", 2),
            self._make_player("MID1", "MID", 7),
            self._make_player("MID2", "MID", 4),
            self._make_player("MID3", "MID", 3),
            self._make_player("MID4", "MID", 2),
            self._make_player("FWD1", "FWD", 8),
            self._make_player("FWD2", "FWD", 3),
            # Bench
            self._make_player("GK2", "GK", 9, contributed=False),
            self._make_player("BenchDEF", "DEF", 0, contributed=False),
            self._make_player("BenchMID", "MID", 0, contributed=False),
            self._make_player("BenchFWD", "FWD", 0, contributed=False),
        ]
        result = compute_bench_analysis(team)
        # GK2 (9) outscores GK1 (1) - valid GK-for-GK swap
        assert result is not None
        assert "GK2" in result
        assert "GK1" in result
        # GK2 should NOT flag against any outfield players
        gk2_line = [line for line in result.split("\n") if "GK2" in line][0]
        assert "DEF4" not in gk2_line
        assert "MID4" not in gk2_line

    def test_no_bench_mistakes(self):
        """No output when all starters outscored bench."""
        team = [
            self._make_player("GK1", "GK", 5),
            self._make_player("DEF1", "DEF", 6),
            self._make_player("DEF2", "DEF", 5),
            self._make_player("DEF3", "DEF", 4),
            self._make_player("DEF4", "DEF", 3),
            self._make_player("MID1", "MID", 7),
            self._make_player("MID2", "MID", 6),
            self._make_player("MID3", "MID", 5),
            self._make_player("MID4", "MID", 4),
            self._make_player("FWD1", "FWD", 8),
            self._make_player("FWD2", "FWD", 3),
            # Bench - all score less than any starter
            self._make_player("GK2", "GK", 1, contributed=False),
            self._make_player("BenchDEF", "DEF", 1, contributed=False),
            self._make_player("BenchMID", "MID", 0, contributed=False),
            self._make_player("BenchFWD", "FWD", 0, contributed=False),
        ]
        result = compute_bench_analysis(team)
        assert result is None

    def test_auto_sub_players_excluded(self):
        """Auto-sub in/out players aren't compared as selection decisions."""
        team = [
            self._make_player("GK1", "GK", 3),
            self._make_player("DEF1", "DEF", 6),
            self._make_player("DEF2", "DEF", 5),
            self._make_player("DEF3", "DEF", 4),
            self._make_player("DEF4", "DEF", 2),
            self._make_player("MID1", "MID", 7),
            self._make_player("MID2", "MID", 4),
            self._make_player("MID3", "MID", 3),
            self._make_player("MID4", "MID", 2),
            self._make_player("FWD1", "FWD", 8),
            # Auto-subbed out starter (0 pts, not a selection decision)
            self._make_player("SubOut", "FWD", 0, contributed=False, auto_sub_out=True),
            # Auto-subbed in bench player (contributed via auto-sub, not selection)
            self._make_player("SubIn", "FWD", 10, contributed=True, auto_sub_in=True),
            # Regular bench
            self._make_player("GK2", "GK", 1, contributed=False),
            self._make_player("BenchDEF", "DEF", 1, contributed=False),
            self._make_player("BenchMID", "MID", 1, contributed=False),
        ]
        result = compute_bench_analysis(team)
        # SubOut should not appear in starters (excluded by auto_sub_out filter on bench side,
        # and by contributed=False on starter side)
        # SubIn should not appear in bench (excluded by auto_sub_in filter)
        if result:
            assert "SubOut" not in result
            assert "SubIn" not in result

    def test_formation_change_tagged(self):
        """Cross-position swaps include [formation change] tag in output."""
        team = [
            # 4-4-2
            self._make_player("GK1", "GK", 3),
            self._make_player("DEF1", "DEF", 6),
            self._make_player("DEF2", "DEF", 5),
            self._make_player("DEF3", "DEF", 4),
            self._make_player("DEF4", "DEF", 1),
            self._make_player("MID1", "MID", 7),
            self._make_player("MID2", "MID", 4),
            self._make_player("MID3", "MID", 3),
            self._make_player("MID4", "MID", 2),
            self._make_player("FWD1", "FWD", 8),
            self._make_player("FWD2", "FWD", 3),
            # Bench FWD who outscores a DEF (cross-position, 4-4-2 -> 4-3-3)
            self._make_player("GK2", "GK", 1, contributed=False),
            self._make_player("BenchFWD", "FWD", 5, contributed=False),
            self._make_player("BenchDEF", "DEF", 0, contributed=False),
            self._make_player("BenchMID", "MID", 0, contributed=False),
        ]
        result = compute_bench_analysis(team)
        assert result is not None
        assert "BenchFWD" in result
        # The DEF4 (1 pt) swap should be tagged as formation change
        bench_fwd_line = [line for line in result.split("\n") if "BenchFWD" in line][0]
        assert "[formation change]" in bench_fwd_line
        # MID4 (2 pts) swap should also be tagged (FWD replacing MID = formation change)
        assert "MID4" in bench_fwd_line


class TestTripleCaptainDetection:
    """Tests for Triple Captain chip detection, multiplier derivation, and display."""

    def test_captain_multiplier_from_pick_data_tc(self):
        """captain_multiplier derived from pick multiplier=3 for Triple Captain."""
        picks_response = {
            "active_chip": "3xc",
            "picks": [
                {"element": 1, "is_captain": False, "multiplier": 1},
                {"element": 2, "is_captain": True, "multiplier": 3},
            ],
        }
        captain_pick_raw = next(
            (p for p in picks_response.get("picks", []) if p.get("is_captain")), None
        )
        if captain_pick_raw and captain_pick_raw.get("multiplier", 0) > 0:
            captain_multiplier = captain_pick_raw["multiplier"]
        else:
            captain_multiplier = 3 if picks_response.get("active_chip") == "3xc" else 2
        is_triple_captain = captain_multiplier == 3

        assert captain_multiplier == 3
        assert is_triple_captain is True

    def test_captain_multiplier_from_pick_data_normal(self):
        """captain_multiplier derived from pick multiplier=2 for normal captain."""
        picks_response = {
            "active_chip": None,
            "picks": [
                {"element": 1, "is_captain": True, "multiplier": 2},
            ],
        }
        captain_pick_raw = next(
            (p for p in picks_response.get("picks", []) if p.get("is_captain")), None
        )
        if captain_pick_raw and captain_pick_raw.get("multiplier", 0) > 0:
            captain_multiplier = captain_pick_raw["multiplier"]
        else:
            captain_multiplier = 3 if picks_response.get("active_chip") == "3xc" else 2
        is_triple_captain = captain_multiplier == 3

        assert captain_multiplier == 2
        assert is_triple_captain is False

    def test_captain_multiplier_fallback_to_active_chip(self):
        """Falls back to active_chip when captain didn't play (multiplier=0)."""
        picks_response = {
            "active_chip": "3xc",
            "picks": [
                {"element": 1, "is_captain": True, "multiplier": 0},
            ],
        }
        captain_pick_raw = next(
            (p for p in picks_response.get("picks", []) if p.get("is_captain")), None
        )
        if captain_pick_raw and captain_pick_raw.get("multiplier", 0) > 0:
            captain_multiplier = captain_pick_raw["multiplier"]
        else:
            captain_multiplier = 3 if picks_response.get("active_chip") == "3xc" else 2
        is_triple_captain = captain_multiplier == 3

        assert captain_multiplier == 3
        assert is_triple_captain is True

    def test_display_points_triple_captain(self):
        """display_points = base * 3 for Triple Captain."""
        base_points = 7
        captain_multiplier = 3
        display_points = base_points * captain_multiplier
        assert display_points == 21

    def test_format_classic_player_tc_marker(self):
        """format_classic_player appends (TC) for triple captain."""
        player = {
            "name": "Gabriel",
            "team": "ARS",
            "position": "DEF",
            "display_points": 21,
            "contributed": True,
            "is_captain": True,
            "is_triple_captain": True,
            "red_cards": 0,
            "auto_sub_in": False,
            "auto_sub_out": False,
        }
        pts = player["display_points"]
        pts_str = str(pts)
        line = f"- {player['name']} ({player['team']}, {player['position']}): {pts_str} pts"
        if player.get("is_triple_captain"):
            line += " (TC)"
        elif player.get("is_captain"):
            line += " (C)"

        assert line == "- Gabriel (ARS, DEF): 21 pts (TC)"
        assert "(C)" not in line

    def test_synthesis_prompt_includes_active_chip(self):
        """Synthesis prompt includes 'Active Chip: Triple Captain' for TC."""
        _, prompt = get_review_synthesis_prompt(
            gameweek=26,
            research_summary="",
            classic_points=55,
            classic_average=50,
            classic_highest=95,
            classic_gw_rank=500000,
            classic_overall_rank=100000,
            classic_captain="Gabriel (TC)",
            classic_captain_points=21,
            classic_captain_hindsight="N/A",
            classic_players="- Gabriel (ARS, DEF): 21 pts (TC)",
            classic_transfers="No transfers this week",
            classic_league_name="Test League",
            classic_gw_position=3,
            classic_position=5,
            classic_total=11,
            classic_rivals="",
            classic_worst_performers="No data",
            classic_transfer_impact=None,
            draft_points=42,
            draft_league_name="Draft League",
            draft_players="- Haaland (MCI): 8 pts",
            draft_transactions="No waivers this week",
            draft_gw_position=2,
            draft_position=3,
            draft_total=10,
            active_chip="3xc",
        )

        assert "Active Chip: Triple Captain" in prompt
        assert "(TC)" in prompt
        assert "21 pts" in prompt

    def test_synthesis_prompt_no_chip_line_when_none(self):
        """Synthesis prompt omits chip line when no chip is active."""
        _, prompt = get_review_synthesis_prompt(
            gameweek=26,
            research_summary="",
            classic_points=55,
            classic_average=50,
            classic_highest=95,
            classic_gw_rank=500000,
            classic_overall_rank=100000,
            classic_captain="Salah",
            classic_captain_points=14,
            classic_captain_hindsight="N/A",
            classic_players="- Salah (LIV, MID): 14 pts (C)",
            classic_transfers="No transfers this week",
            classic_league_name="Test League",
            classic_gw_position=3,
            classic_position=5,
            classic_total=11,
            classic_rivals="",
            classic_worst_performers="No data",
            classic_transfer_impact=None,
            draft_points=42,
            draft_league_name="Draft League",
            draft_players="- Haaland (MCI): 8 pts",
            draft_transactions="No waivers this week",
            draft_gw_position=2,
            draft_position=3,
            draft_total=10,
        )

        assert "Active Chip:" not in prompt

    @pytest.mark.parametrize("chip_code,display_name", [
        ("wildcard", "Wildcard"),
        ("freehit", "Free Hit"),
        ("bboost", "Bench Boost"),
    ])
    def test_synthesis_prompt_includes_non_tc_chips(self, chip_code, display_name):
        """Synthesis prompt includes human-readable chip name for WC/FH/BB."""
        _, prompt = get_review_synthesis_prompt(
            gameweek=26,
            research_summary="",
            classic_points=55,
            classic_average=50,
            classic_highest=95,
            classic_gw_rank=500000,
            classic_overall_rank=100000,
            classic_captain="Salah",
            classic_captain_points=14,
            classic_captain_hindsight="N/A",
            classic_players="- Salah (LIV, MID): 14 pts (C)",
            classic_transfers="No transfers this week",
            classic_league_name="Test League",
            classic_gw_position=3,
            classic_position=5,
            classic_total=11,
            classic_rivals="",
            classic_worst_performers="No data",
            classic_transfer_impact=None,
            draft_points=42,
            draft_league_name="Draft League",
            draft_players="- Haaland (MCI): 8 pts",
            draft_transactions="No waivers this week",
            draft_gw_position=2,
            draft_position=3,
            draft_total=10,
            active_chip=chip_code,
        )
        assert f"Active Chip: {display_name}" in prompt

    def test_synthesis_system_prompt_includes_all_chip_context(self):
        """System prompt explains all chip effects."""
        system = _build_system_prompt(has_fines=True)
        assert "Triple Captain (TC)" in system
        assert "Bench Boost (BB)" in system
        assert "Free Hit (FH)" in system
        assert "Wildcard (WC)" in system

    def test_synthesis_system_prompt_includes_tc_context(self):
        """System prompt explains Triple Captain chip effects."""
        assert "Triple Captain" in _build_system_prompt(has_fines=True)
        assert "tripled" in _build_system_prompt(has_fines=True)
        assert "(TC)" in _build_system_prompt(has_fines=True)

    def test_template_renders_tc_marker(self):
        """Jinja2 template renders (TC) for triple captain players."""
        from jinja2 import Environment, FileSystemLoader

        from fpl_cli.paths import TEMPLATE_DIR

        env = Environment(loader=FileSystemLoader(str(TEMPLATE_DIR)))
        template = env.get_template("gw_review.md.j2")

        data = {
            "generated_at": "2026-02-20",
            "team_points": [
                {
                    "name": "Gabriel",
                    "team": "ARS",
                    "position": "DEF",
                    "display_points": 21,
                    "contributed": True,
                    "is_captain": True,
                    "is_triple_captain": True,
                    "is_vice_active": False,
                    "red_cards": 0,
                    "auto_sub_in": False,
                    "auto_sub_out": False,
                },
            ],
        }
        result = template.render(**data)
        assert "Gabriel (TC)" in result
        assert "Gabriel (C)" not in result
        assert "21" in result


class TestGwPositionWithHalf:
    """Tests for _gw_position_with_half helper."""

    def test_top_half_even_league(self):
        assert _gw_position_with_half(1, 8) == "1 [TOP HALF]"
        assert _gw_position_with_half(4, 8) == "4 [TOP HALF]"

    def test_bottom_half_even_league(self):
        assert _gw_position_with_half(5, 8) == "5 [BOTTOM HALF, 4 worst]"
        assert _gw_position_with_half(8, 8) == "8 [BOTTOM HALF, 1 worst]"

    def test_top_half_odd_league(self):
        assert _gw_position_with_half(1, 11) == "1 [TOP HALF]"
        assert _gw_position_with_half(5, 11) == "5 [TOP HALF]"

    def test_exact_middle_odd_league(self):
        assert _gw_position_with_half(6, 11) == "6 [EXACT MIDDLE]"

    def test_bottom_half_odd_league(self):
        assert _gw_position_with_half(7, 11) == "7 [BOTTOM HALF, 5 worst]"
        assert _gw_position_with_half(11, 11) == "11 [BOTTOM HALF, 1 worst]"

    def test_tie_rank_string(self):
        assert _gw_position_with_half("3=", 8) == "3= [TOP HALF]"
        assert _gw_position_with_half("5=", 8) == "5= [BOTTOM HALF, 4= worst]"

    def test_invalid_position_passthrough(self):
        assert _gw_position_with_half("?", 8) == "?"


class TestValidateResearchTeams:
    """Tests for validate_research_teams."""

    @pytest.fixture
    def players_and_teams(self):
        """Build a minimal player_map and teams dict for testing."""
        players = [
            make_player(id=1, web_name="Salah", team_id=14),
            make_player(id=2, web_name="Haaland", team_id=13),
            make_player(id=3, web_name="Saka", team_id=1),
            make_player(id=4, web_name="Gu\u00e9hi", team_id=6),
        ]
        teams_list = [
            make_team(id=1, short_name="ARS"),
            make_team(id=6, short_name="CRY"),
            make_team(id=13, short_name="MCI"),
            make_team(id=14, short_name="LIV"),
        ]
        player_map = {p.id: p for p in players}
        teams_dict = {t.id: t for t in teams_list}
        return player_map, teams_dict

    def _make_table(self, header_type, rows):
        """Build a markdown table string from header type and row tuples."""
        if header_type == "performers":
            header = "| Player | Club | Pts | Why They Hauled | Source |"
            sep = "|--------|------|-----|-----------------|--------|"
        else:
            header = "| Player | Club | Pts | What Went Wrong | Concern Level |"
            sep = "|--------|------|-----|-----------------|---------------|"
        lines = [header, sep]
        for name, club, pts, note in rows:
            lines.append(f"| {name} | {club} | {pts} | {note} |")
        return "\n".join(lines)

    def test_correct_club_code_unchanged(self, players_and_teams):
        player_map, teams = players_and_teams
        table = self._make_table("performers", [("Salah", "LIV", "12", "Two goals")])
        result, corrections = validate_research_teams(table, player_map, teams)
        assert "| LIV |" in result
        assert corrections == []

    def test_wrong_club_code_corrected(self, players_and_teams):
        player_map, teams = players_and_teams
        table = self._make_table("performers", [("Salah", "MCI", "12", "Two goals")])
        result, corrections = validate_research_teams(table, player_map, teams)
        assert "| LIV |" in result
        assert "| MCI |" not in result
        assert len(corrections) == 1
        assert "Salah: MCI -> LIV" in corrections[0]

    def test_unknown_player_skipped(self, players_and_teams):
        player_map, teams = players_and_teams
        table = self._make_table("performers", [("Unknown", "XXX", "5", "Mystery")])
        result, corrections = validate_research_teams(table, player_map, teams)
        assert "| XXX |" in result
        assert corrections == []

    def test_accented_name_matches(self, players_and_teams):
        player_map, teams = players_and_teams
        # Research provider writes "Guehi" (no accent), API has "Guéhi"
        table = self._make_table("disappointments", [("Guehi", "BHA", "2", "Poor display")])
        result, corrections = validate_research_teams(table, player_map, teams)
        assert "| CRY |" in result
        assert len(corrections) == 1
        assert "Guehi: BHA -> CRY" in corrections[0]

    def test_returns_corrected_text_and_log(self, players_and_teams):
        player_map, teams = players_and_teams
        table = self._make_table("performers", [("Haaland", "LIV", "15", "Hat trick")])
        result, corrections = validate_research_teams(table, player_map, teams)
        assert isinstance(result, str)
        assert isinstance(corrections, list)
        assert all(isinstance(c, str) for c in corrections)

    def test_only_table_rows_corrected(self, players_and_teams):
        player_map, teams = players_and_teams
        prose = "MCI's attack was devastating, with Haaland leading the line for MCI.\n\n"
        table = self._make_table("performers", [("Salah", "LIV", "12", "Two goals")])
        text = prose + table
        result, corrections = validate_research_teams(text, player_map, teams)
        # Prose MCI references must be untouched
        assert result.startswith("MCI's attack was devastating")
        assert corrections == []

    def test_duplicate_web_name_skipped(self, players_and_teams):
        player_map, teams = players_and_teams
        # Add a second player with web_name "Salah" on a different team
        dupe = make_player(id=99, web_name="Salah", team_id=1)
        player_map[dupe.id] = dupe
        table = self._make_table("performers", [("Salah", "TOT", "8", "Surprise")])
        result, corrections = validate_research_teams(table, player_map, teams)
        # Ambiguous name - should not be corrected
        assert "| TOT |" in result
        assert corrections == []

    def test_accented_name_in_research_matches_plain_api(self, players_and_teams):
        """Research provider writes accented name, API has plain - should still match."""
        player_map, teams = players_and_teams
        # Add a player with no accent in the API
        plain = make_player(id=50, web_name="Cunha", team_id=1)
        player_map[plain.id] = plain
        # Research provider writes it with an accent
        table = self._make_table("performers", [("Cunhã", "MCI", "10", "Great game")])
        result, corrections = validate_research_teams(table, player_map, teams)
        assert "| ARS |" in result
        assert len(corrections) == 1

    def test_no_table_headers_returns_unchanged(self, players_and_teams):
        player_map, teams = players_and_teams
        text = "Just some prose about Salah playing for MCI. No tables here."
        result, corrections = validate_research_teams(text, player_map, teams)
        assert result == text
        assert corrections == []

    def test_variable_spacing_in_cells(self, players_and_teams):
        """Research provider may produce cells with inconsistent whitespace padding."""
        player_map, teams = players_and_teams
        header = "| Player | Club | Pts | Why They Hauled | Source |"
        sep = "|--------|------|-----|-----------------|--------|"
        # Extra spaces around club code
        row = "|  Salah  |  MCI  | 12 | Two goals |"
        text = f"{header}\n{sep}\n{row}"
        result, corrections = validate_research_teams(text, player_map, teams)
        assert "| LIV |" in result
        assert "MCI" not in result
        assert len(corrections) == 1


class TestCollapseTransferChurn:
    """Tests for `_collapse_transfer_churn` — same-GW net squad delta."""

    def _player_map(self, *players):
        return {p.id: p for p in players}

    def test_round_trip_transfers_are_dropped(self):
        """Player transferred in then back out in same GW -> no rows."""
        saka = make_player(id=10, web_name="Saka", position=PlayerPosition.MIDFIELDER)
        palmer = make_player(id=20, web_name="Palmer", position=PlayerPosition.MIDFIELDER)
        gw_transfers = [
            {"element_in": palmer.id, "element_out": saka.id, "event": 30},
            {"element_in": saka.id, "element_out": palmer.id, "event": 30},
        ]
        paired = _collapse_transfer_churn(gw_transfers, self._player_map(saka, palmer))
        assert paired == []

    def test_simple_single_transfer_preserved(self):
        saka = make_player(id=10, web_name="Saka", position=PlayerPosition.MIDFIELDER)
        palmer = make_player(id=20, web_name="Palmer", position=PlayerPosition.MIDFIELDER)
        gw_transfers = [{"element_in": palmer.id, "element_out": saka.id, "event": 30}]
        paired = _collapse_transfer_churn(gw_transfers, self._player_map(saka, palmer))
        assert len(paired) == 1
        pin, pout = paired[0]
        assert pin.web_name == "Palmer"
        assert pout.web_name == "Saka"

    def test_waiver_chain_keeps_net_delta(self):
        """(in A, out B) then (in C, out A) should collapse to net (+C, -B)."""
        a = make_player(id=1, web_name="Alpha", position=PlayerPosition.MIDFIELDER)
        b = make_player(id=2, web_name="Bravo", position=PlayerPosition.MIDFIELDER)
        c = make_player(id=3, web_name="Charlie", position=PlayerPosition.MIDFIELDER)
        gw_transfers = [
            {"element_in": a.id, "element_out": b.id, "event": 30},
            {"element_in": c.id, "element_out": a.id, "event": 30},
        ]
        paired = _collapse_transfer_churn(gw_transfers, self._player_map(a, b, c))
        assert len(paired) == 1
        pin, pout = paired[0]
        assert {pin.web_name, pout.web_name} == {"Charlie", "Bravo"}
        assert pin.web_name == "Charlie"
        assert pout.web_name == "Bravo"

    def test_multi_transfer_pairs_by_position(self):
        """Two transfers across DEF and MID pair like-for-like."""
        trent = make_player(id=1, web_name="Trent", position=PlayerPosition.DEFENDER)
        van_dijk = make_player(id=2, web_name="VanDijk", position=PlayerPosition.DEFENDER)
        saka = make_player(id=3, web_name="Saka", position=PlayerPosition.MIDFIELDER)
        palmer = make_player(id=4, web_name="Palmer", position=PlayerPosition.MIDFIELDER)
        gw_transfers = [
            {"element_in": van_dijk.id, "element_out": saka.id, "event": 30},
            {"element_in": palmer.id, "element_out": trent.id, "event": 30},
        ]
        paired = _collapse_transfer_churn(
            gw_transfers, self._player_map(trent, van_dijk, saka, palmer)
        )
        assert len(paired) == 2
        pairs = {(pin.web_name, pout.web_name) for pin, pout in paired}
        assert pairs == {("VanDijk", "Trent"), ("Palmer", "Saka")}

    def test_unknown_player_ids_are_dropped(self):
        known = make_player(id=10, web_name="Known", position=PlayerPosition.MIDFIELDER)
        gw_transfers = [{"element_in": known.id, "element_out": 999, "event": 30}]
        paired = _collapse_transfer_churn(gw_transfers, self._player_map(known))
        # Out player unknown -> dropped; no pair emitted
        assert paired == []

    def test_empty_transfers_returns_empty(self):
        assert _collapse_transfer_churn([], {}) == []


class TestNetTransferIds:
    """Tests for `_net_transfer_ids` — the id-level churn collapse used by both
    classic and draft review paths."""

    def test_round_trip_cancels(self):
        """A->B then B->A same GW -> no net delta."""
        txns = [
            {"element_in": 1, "element_out": 2},
            {"element_in": 2, "element_out": 1},
        ]
        net_in, net_out = _net_transfer_ids(txns)
        assert net_in == []
        assert net_out == []

    def test_waiver_chain_yields_net_delta(self):
        """(in A, out B), (in C, out A) -> net +C / -B (A cancels)."""
        txns = [
            {"element_in": 1, "element_out": 2},  # in A, out B
            {"element_in": 3, "element_out": 1},  # in C, out A
        ]
        net_in, net_out = _net_transfer_ids(txns)
        assert net_in == [3]  # C
        assert net_out == [2]  # B

    def test_draft_balanced_pairs_preserved(self):
        """Draft always drops a player per pickup, so balanced txns survive intact."""
        txns = [
            {"element_in": 10, "element_out": 20},
            {"element_in": 11, "element_out": 21},
        ]
        net_in, net_out = _net_transfer_ids(txns)
        assert sorted(net_in) == [10, 11]
        assert sorted(net_out) == [20, 21]

    def test_sort_key_orders_both_lists(self):
        """Sort key applies to both lists so callers can zip like-for-like."""
        # Names: in = {b, a}, out = {d, c}; sort alphabetically
        txns = [
            {"element_in": "b", "element_out": "d"},
            {"element_in": "a", "element_out": "c"},
        ]
        net_in, net_out = _net_transfer_ids(txns, sort_key=lambda x: x)
        assert net_in == ["a", "b"]
        assert net_out == ["c", "d"]

    def test_none_ids_excluded(self):
        """Missing element_in/out (None) is ignored, not counted."""
        txns = [
            {"element_in": 1, "element_out": None},
            {"element_in": None, "element_out": 2},
        ]
        net_in, net_out = _net_transfer_ids(txns)
        assert net_in == [1]
        assert net_out == [2]
