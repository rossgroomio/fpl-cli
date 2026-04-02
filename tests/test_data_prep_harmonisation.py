"""Characterisation tests for data preparation harmonisation.

Pins current scoring outputs for all four agents before refactoring.
Validates positional FDR numeric range compatibility with existing thresholds.
"""

from __future__ import annotations

from tests.conftest import make_player

from fpl_cli.models.player import PlayerPosition
from fpl_cli.services.player_scoring import (
    FDR_EASY,
    FDR_MEDIUM,
    FixtureMatchup,
    build_player_evaluation,
    calculate_bench_score,
    calculate_captain_score,
    calculate_target_score,
    calculate_waiver_score,
)
from fpl_cli.services.team_ratings import TeamRating, TeamRatingsService


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_ratings_service(ratings: dict[str, TeamRating]) -> TeamRatingsService:
    """Build a TeamRatingsService pre-loaded with given ratings."""
    svc = TeamRatingsService.__new__(TeamRatingsService)
    svc._ratings = dict(ratings)
    svc._loaded = True
    svc._metadata = None
    return svc


# Ratings fixtures: strong team vs weak team
STRONG_TEAM = TeamRating(atk_home=1, atk_away=2, def_home=1, def_away=2)  # avg_overall=1.5, fdr=6.5
WEAK_TEAM = TeamRating(atk_home=6, atk_away=7, def_home=6, def_away=7)    # avg_overall=6.5, fdr=1.5
MID_TEAM = TeamRating(atk_home=4, atk_away=4, def_home=4, def_away=4)     # avg_overall=4.0, fdr=4.0


# ---------------------------------------------------------------------------
# Captain characterisation
# ---------------------------------------------------------------------------


class TestCaptainCharacterisation:
    """Pin captain scoring with known inputs."""

    def _make_matchup(self, score=7.0, fdr=2.5, is_home=True, opponent="SHU"):
        return FixtureMatchup(
            opponent_short=opponent,
            is_home=is_home,
            opponent_fdr=fdr,
            matchup_score=score,
            matchup_breakdown={
                "matchup_score": score,
                "attack_matchup": 6.0,
                "defence_matchup": 5.0,
                "form_differential": 0.2,
                "position_differential": 0.1,
                "reasoning": ["Good matchup"],
            },
        )

    def test_fwd_strong_form_easy_fixture(self):
        """FWD with strong form facing a weak defence."""
        player = make_player(
            id=10, web_name="Havertz", team_id=1,
            position=PlayerPosition.FORWARD,
            form=7.5, points_per_game=6.0, minutes=1800, total_points=132,
            expected_goals=10.0, expected_assists=5.0, penalties_order=1,
        )
        fm = [self._make_matchup(score=8.0, fdr=2.0)]
        evaluation, identity = build_player_evaluation(
            player,
            enrichment={"npxG_per_90": 0.45, "team_short": "ARS"},
            fixture_matchups=fm,
        )
        result = calculate_captain_score(evaluation, identity, next_gw_id=20)
        assert result is not None
        # Pin: captain_score and raw for regression detection
        assert result["captain_score"] == 95
        assert result["captain_score_raw"] == 30.5

    def test_mid_moderate_form(self):
        """MID with moderate form and medium FDR."""
        player = make_player(
            id=11, web_name="Saka", team_id=1,
            position=PlayerPosition.MIDFIELDER,
            form=5.0, points_per_game=5.5, minutes=1500, total_points=110,
            expected_goals=6.0, expected_assists=4.0,
        )
        fm = [self._make_matchup(score=5.5, fdr=3.5, is_home=False)]
        evaluation, identity = build_player_evaluation(
            player,
            enrichment={"npxG_per_90": 0.3, "team_short": "ARS"},
            fixture_matchups=fm,
        )
        result = calculate_captain_score(evaluation, identity, next_gw_id=20)
        assert result is not None
        assert result["captain_score"] == 62
        assert result["captain_score_raw"] == 19.88

    def test_def_with_clean_sheet_potential(self):
        """DEF with clean sheet potential and easy fixture."""
        player = make_player(
            id=12, web_name="Saliba", team_id=1,
            position=PlayerPosition.DEFENDER,
            form=5.5, points_per_game=5.0, minutes=1800, total_points=100,
            expected_goals=2.0, expected_assists=2.0,
        )
        fm = [self._make_matchup(score=7.0, fdr=2.0)]
        evaluation, identity = build_player_evaluation(
            player,
            enrichment={"team_short": "ARS"},
            fixture_matchups=fm,
        )
        result = calculate_captain_score(evaluation, identity, next_gw_id=20)
        assert result is not None
        assert result["captain_score"] == 65
        assert result["captain_score_raw"] == 20.76

    def test_bgw_returns_none(self):
        """Player with no fixtures returns None."""
        player = make_player(form=7.0, minutes=1800, total_points=100)
        evaluation, identity = build_player_evaluation(
            player, enrichment={"team_short": "ARS"}, fixture_matchups=[],
        )
        assert calculate_captain_score(evaluation, identity, next_gw_id=20) is None


# ---------------------------------------------------------------------------
# Bench characterisation
# ---------------------------------------------------------------------------


class TestBenchCharacterisation:
    """Pin bench scoring with known inputs."""

    def _fm(self, fdr=2.5, matchup_score=7.0):
        return FixtureMatchup(
            opponent_short="SHU", is_home=True, opponent_fdr=fdr, matchup_score=matchup_score,
        )

    def test_mid_home_strong_matchup(self):
        """MID with home fixture, strong matchup, good form."""
        player = make_player(
            id=1, web_name="Saka", team_id=1,
            position=PlayerPosition.MIDFIELDER,
            form=6.0, points_per_game=6.0, minutes=1500, total_points=120,
        )
        evaluation, identity = build_player_evaluation(
            player, enrichment={"team_short": "ARS"},
            fixture_matchups=[self._fm()],
        )
        result = calculate_bench_score(
            evaluation, identity,
            availability_risks=[{"position": "MID", "risk_level": 3}],
            next_gw_id=20,
        )
        # Pin: priority_score for regression detection (uses single-GW core + coverage bonus)
        assert result["priority_score"] == 71
        assert result["priority_score_raw"] == 23.33

    def test_weak_matchup_small_bonus(self):
        """Player with weak matchup gets small continuous bonus."""
        player = make_player(
            id=2, web_name="Palmer", team_id=5,
            position=PlayerPosition.MIDFIELDER,
            form=4.0, points_per_game=4.5, minutes=1200, total_points=80,
        )
        evaluation, identity = build_player_evaluation(
            player, enrichment={"team_short": "CHE"},
            fixture_matchups=[self._fm(fdr=4.5, matchup_score=3.0)],
        )
        result = calculate_bench_score(
            evaluation, identity, availability_risks=[], next_gw_id=20,
        )
        assert result["priority_score"] == 36
        assert result["priority_score_raw"] == 12.03

    def test_def_home_easy_fixture(self):
        """DEF with home fixture and easy matchup."""
        player = make_player(
            id=4, web_name="Saliba", team_id=1,
            position=PlayerPosition.DEFENDER,
            form=5.5, points_per_game=5.0, minutes=1800, total_points=100,
        )
        evaluation, identity = build_player_evaluation(
            player, enrichment={"team_short": "ARS"},
            fixture_matchups=[self._fm(fdr=2.0, matchup_score=7.0)],
        )
        result = calculate_bench_score(
            evaluation, identity, availability_risks=[], next_gw_id=20,
        )
        assert result["priority_score"] == 56
        assert result["priority_score_raw"] == 18.49

    def test_bgw_no_fixture_bonus(self):
        """BGW player with no fixtures gets 0 from core (no matchups)."""
        player = make_player(
            id=3, web_name="Bench", team_id=1,
            position=PlayerPosition.DEFENDER,
            form=3.0, points_per_game=3.0, minutes=1000, total_points=50,
        )
        evaluation, identity = build_player_evaluation(
            player, enrichment={"team_short": "ARS"},
            fixture_matchups=[],
        )
        result = calculate_bench_score(
            evaluation, identity, availability_risks=[], next_gw_id=20,
        )
        assert result["priority_score"] == 0
        assert result["priority_score_raw"] == 0.0


# ---------------------------------------------------------------------------
# Target characterisation
# ---------------------------------------------------------------------------


class TestTargetCharacterisation:
    """Pin target scoring with known inputs."""

    def test_mid_good_matchup(self):
        """MID with good 3GW matchup and strong stats."""
        evaluation, _ = build_player_evaluation(
            {
                "position": "MID",
                "npxG_per_90": 0.35, "xGChain_per_90": 0.55, "xGI_per_90": 0.45,
                "form": 6.0, "ppg": 5.5, "GI_minus_xGI": -2.0,
                "minutes": 1800, "appearances": 22, "penalty_xG_per_90": 0.1,
            },
            matchup_avg_3gw=7.0,
            positional_fdr=2.5,
        )
        score = calculate_target_score(evaluation, next_gw_id=20)
        assert score == 62

    def test_def_with_dc_per_90(self):
        """DEF uses without_xgi weights, dc_per_90 active."""
        evaluation, _ = build_player_evaluation(
            {
                "position": "DEF",
                "npxG_per_90": 0.05, "xGChain_per_90": 0.1, "xGI_per_90": 0.1,
                "form": 5.0, "ppg": 4.5, "dc_per_90": 3.0,
                "GI_minus_xGI": 0.0,
                "minutes": 1800, "appearances": 22,
            },
            matchup_avg_3gw=6.0,
            positional_fdr=3.0,
        )
        score = calculate_target_score(evaluation, next_gw_id=20)
        assert score == 40


# ---------------------------------------------------------------------------
# Waiver characterisation
# ---------------------------------------------------------------------------


class TestWaiverCharacterisation:
    """Pin waiver scoring with known inputs."""

    def _squad_by_pos(self):
        return {
            "MID": [{"form": 4.0}, {"form": 5.0}],
            "FWD": [],
            "DEF": [{"form": 2.0}],
            "GK": [{"form": 3.0}],
        }

    def _team_counts(self):
        return {"LIV": 2, "ARS": 3}

    def test_fwd_team_stacking_penalty(self):
        """FWD with team stacking penalty (ARS has 3 players)."""
        evaluation, _ = build_player_evaluation(
            {"position": "FWD", "form": 6.0, "ppg": 5.0, "minutes": 1200, "appearances": 15,
             "xGI_per_90": 0.5, "npxG_per_90": 0.3, "xGChain_per_90": 0.4,
             "status": "a", "team_short": "ARS"},
            matchup_avg_3gw=6.0, positional_fdr=3.0,
        )
        score = calculate_waiver_score(
            evaluation, squad_by_position=self._squad_by_pos(),
            team_counts=self._team_counts(), next_gw_id=20,
        )
        # FWD position is empty -> +5 bonus, but ARS stacking -> -5
        assert score == 44

    def test_mid_no_penalties(self):
        """MID without stacking penalty, nailed starter."""
        evaluation, _ = build_player_evaluation(
            {"position": "MID", "form": 7.0, "ppg": 5.5, "minutes": 900, "appearances": 10,
             "xGI_per_90": 0.6, "npxG_per_90": 0.4, "xGChain_per_90": 0.5,
             "status": "a", "team_short": "BHA"},
            matchup_avg_3gw=6.5, positional_fdr=2.5,
        )
        score = calculate_waiver_score(
            evaluation, squad_by_position=self._squad_by_pos(),
            team_counts=self._team_counts(), next_gw_id=20,
        )
        assert score == 47

    def test_def_with_dc_per_90(self):
        """DEF uses without_xgi weights, dc_per_90 active."""
        evaluation, _ = build_player_evaluation(
            {"position": "DEF", "form": 5.0, "ppg": 4.5, "minutes": 1800, "appearances": 22,
             "xGI_per_90": 0.1, "npxG_per_90": 0.05, "xGChain_per_90": 0.1,
             "dc_per_90": 3.0, "status": "a", "team_short": "BHA"},
            matchup_avg_3gw=6.0, positional_fdr=3.0,
        )
        score = calculate_waiver_score(
            evaluation, squad_by_position=self._squad_by_pos(),
            team_counts=self._team_counts(), next_gw_id=20,
        )
        assert score == 47


# ---------------------------------------------------------------------------
# FDR range validation (R7)
# ---------------------------------------------------------------------------


class TestFDRRangeValidation:
    """Validate positional FDR numeric range is compatible with existing thresholds."""

    def setup_method(self):
        self.ratings = {
            "LIV": STRONG_TEAM,
            "SHU": WEAK_TEAM,
            "BHA": MID_TEAM,
        }
        self.svc = _make_ratings_service(self.ratings)

    def test_positional_fdr_in_range(self):
        """Both positional FDR and avg_overall_fdr produce values in 1.0-7.0 range."""
        positions = ["FWD", "MID", "DEF", "GK"]
        teams = ["LIV", "SHU", "BHA"]
        venues = ["home", "away"]

        for pos in positions:
            for team in teams:
                for opp in teams:
                    if team == opp:
                        continue
                    for venue in venues:
                        pos_fdr = self.svc.get_positional_fdr(
                            pos, team, opp, venue, mode="difference",
                        )
                        assert 1.0 <= pos_fdr <= 7.0, (
                            f"Positional FDR out of range: {pos_fdr} for "
                            f"{pos} {team} vs {opp} ({venue})"
                        )

    def test_avg_overall_fdr_in_range(self):
        """avg_overall_fdr is also in 1.0-7.0 range."""
        for name, rating in self.ratings.items():
            fdr = rating.avg_overall_fdr
            assert 1.0 <= fdr <= 7.0, f"avg_overall_fdr out of range for {name}: {fdr}"

    def test_fwd_semantic_ordering(self):
        """FWD vs weak defence should get lower (easier) FDR than vs strong defence."""
        fdr_vs_weak = self.svc.get_positional_fdr("FWD", "LIV", "SHU", "home")
        fdr_vs_strong = self.svc.get_positional_fdr("FWD", "LIV", "LIV", "home")
        # SHU has weak defence (def_home=6, inverted to 2) -> easier for attacker
        # vs strong defence -> harder
        assert fdr_vs_weak < fdr_vs_strong

    def test_def_semantic_ordering(self):
        """DEF vs top attack should get higher (harder) FDR than vs bottom attack."""
        fdr_vs_strong_atk = self.svc.get_positional_fdr("DEF", "SHU", "LIV", "home")
        fdr_vs_weak_atk = self.svc.get_positional_fdr("DEF", "SHU", "SHU", "home")
        # LIV has strong attack (atk_away=2, inverted to 6) -> harder for defender
        assert fdr_vs_strong_atk > fdr_vs_weak_atk

    def test_fdr_fallback_when_unavailable(self):
        """FDR returns 4.0 when team ratings unavailable."""
        fdr = self.svc.get_positional_fdr("FWD", "UNKNOWN", "SHU", "home")
        assert fdr == 4.0

    def test_fdr_thresholds_differentiate(self):
        """Positional FDR produces values both above and below FDR_EASY and FDR_MEDIUM.

        If all values cluster above or below the thresholds, the classification
        would be useless. Check that the test ratings produce at least one easy
        and one hard fixture.
        """
        all_fdrs = []
        for pos in ["FWD", "DEF"]:
            for team in ["LIV", "SHU"]:
                for opp in ["LIV", "SHU"]:
                    if team == opp:
                        continue
                    for venue in ["home", "away"]:
                        all_fdrs.append(
                            self.svc.get_positional_fdr(pos, team, opp, venue)
                        )

        assert any(f <= FDR_EASY for f in all_fdrs), (
            f"No FDR <= {FDR_EASY} (easy) found: {all_fdrs}"
        )
        assert any(f > FDR_MEDIUM for f in all_fdrs), (
            f"No FDR > {FDR_MEDIUM} (hard) found: {all_fdrs}"
        )
