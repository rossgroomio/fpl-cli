"""Tests for centralised player scoring engine."""

import dataclasses

import pytest

from fpl_cli.models.player import PlayerPosition, PlayerStatus
from fpl_cli.services.player_scoring import (
    ATTACKING_POSITIONS,
    DIFFERENTIAL_QUALITY_WEIGHTS,
    TARGET_CEILING,
    TARGET_QUALITY_WEIGHTS,
    VALUE_CEILING,
    VALUE_QUALITY_WEIGHTS,
    WAIVER_QUALITY_WEIGHTS,
    FixtureMatchup,
    PlayerEvaluation,
    PlayerIdentity,
    ScoringContext,
    ScoringData,
    StatWeight,
    _matchup_bonus,
    build_fixture_matchups,
    build_player_evaluation,
    build_scoring_context,
    prepare_scoring_data,
    calculate_bench_score,
    calculate_captain_score,
    calculate_lineup_score,
    select_starting_xi,
    VALID_FORMATIONS,
    calculate_differential_score,
    calculate_mins_factor,
    calculate_player_quality_score,
    compute_form_trajectory,
    calculate_target_score,
    calculate_waiver_score,
    compute_aggregate_matchup,
    normalise_score,
)
from tests.conftest import make_player


# ---------------------------------------------------------------------------
# Characterisation snapshot: pins exact output of all 5 formulas before refactor
# ---------------------------------------------------------------------------


class TestCharacterisationSnapshot:
    """Pin current scoring output for all 5 formulas.

    These tests intentionally break when formula logic changes.
    Update expected values in each unit that modifies scoring.
    """

    @staticmethod
    def _mid_matchup():
        return FixtureMatchup(
            opponent_short="SHU", is_home=True, opponent_fdr=3.0,
            matchup_score=6.5,
            matchup_breakdown={
                "matchup_score": 6.5, "attack_matchup": 6.0, "defence_matchup": 5.0,
                "form_differential": 0.2, "position_differential": 0.1,
                "reasoning": ["Good matchup"],
            },
        )

    @staticmethod
    def _def_matchup():
        return FixtureMatchup(
            opponent_short="BOU", is_home=False, opponent_fdr=3.5,
            matchup_score=5.5,
            matchup_breakdown={
                "matchup_score": 5.5, "attack_matchup": 5.0, "defence_matchup": 5.5,
                "form_differential": 0.1, "position_differential": 0.05,
                "reasoning": ["Average matchup"],
            },
        )

    @staticmethod
    def _mid_player():
        return make_player(
            id=100, web_name="CharMID", team_id=1,
            position=PlayerPosition.MIDFIELDER,
            form=5.5, points_per_game=5.0, minutes=1500, total_points=100,
            expected_goals=6.0, expected_assists=4.0,
            penalties_order=1,
        )

    @staticmethod
    def _def_player():
        return make_player(
            id=200, web_name="CharDEF", team_id=2,
            position=PlayerPosition.DEFENDER,
            form=4.5, points_per_game=4.0, minutes=1600, total_points=80,
            expected_goals=1.0, expected_assists=0.5,
        )

    def _build_mid(self):
        eval_, identity = build_player_evaluation(
            self._mid_player(),
            enrichment={
                "npxG_per_90": 0.30, "xGChain_per_90": 0.45,
                "penalty_xG_per_90": 0.15, "team_short": "ARS",
            },
            fixture_matchups=[self._mid_matchup()],
            matchup_avg_3gw=6.0, positional_fdr=3.0,
        )
        return eval_, identity

    def _build_def(self):
        eval_, identity = build_player_evaluation(
            self._def_player(),
            enrichment={"dc_per_90": 3.5, "team_short": "CHE"},
            fixture_matchups=[self._def_matchup()],
            matchup_avg_3gw=5.5, positional_fdr=3.5,
        )
        return eval_, identity

    # --- Target ---

    def test_target_mid(self):
        eval_, _ = self._build_mid()
        assert calculate_target_score(eval_, next_gw_id=20) == 50

    def test_target_def(self):
        eval_, _ = self._build_def()
        assert calculate_target_score(eval_, next_gw_id=20) == 38

    # --- Differential ---

    def test_differential_mid(self):
        eval_, _ = self._build_mid()
        assert calculate_differential_score(
            eval_, semi_differential_threshold=20.0, next_gw_id=20,
        ) == 54

    def test_differential_def(self):
        eval_, _ = self._build_def()
        assert calculate_differential_score(
            eval_, semi_differential_threshold=20.0, next_gw_id=20,
        ) == 42

    # --- Waiver ---

    def test_waiver_mid(self):
        eval_, _ = self._build_mid()
        squad = {"MID": [{"form": 4.0}, {"form": 3.0}], "DEF": [{"form": 5.0}, {"form": 4.0}]}
        assert calculate_waiver_score(
            eval_, squad_by_position=squad, next_gw_id=20,
        ) == 46

    def test_waiver_def(self):
        eval_, _ = self._build_def()
        squad = {"MID": [{"form": 4.0}, {"form": 3.0}], "DEF": [{"form": 5.0}, {"form": 4.0}]}
        assert calculate_waiver_score(
            eval_, squad_by_position=squad, next_gw_id=20,
        ) == 37

    # --- Captain ---

    def test_captain_mid(self):
        eval_, identity = self._build_mid()
        result = calculate_captain_score(eval_, identity, next_gw_id=20)
        assert result is not None
        assert result["captain_score"] == 77
        assert result["captain_score_raw"] == 24.58
        assert result["pen_bonus"] == 1.12

    def test_captain_def(self):
        eval_, identity = self._build_def()
        result = calculate_captain_score(eval_, identity, next_gw_id=20)
        assert result is not None
        assert result["captain_score"] == 48
        assert result["captain_score_raw"] == 15.45

    # --- Bench ---

    def test_bench_mid(self):
        eval_, identity = self._build_mid()
        result = calculate_bench_score(eval_, identity, availability_risks=[], next_gw_id=20)
        assert result["priority_score"] == 67
        assert result["priority_score_raw"] == 22.03

    def test_bench_def(self):
        eval_, identity = self._build_def()
        result = calculate_bench_score(eval_, identity, availability_risks=[], next_gw_id=20)
        assert result["priority_score"] == 40
        assert result["priority_score_raw"] == 13.11


class TestNormaliseScore:
    def test_mid_range(self):
        assert normalise_score(25.0, 31.5) == 79

    def test_zero(self):
        assert normalise_score(0.0, 31.5) == 0

    def test_at_ceiling(self):
        assert normalise_score(31.5, 31.5) == 100

    def test_above_ceiling_clipped(self):
        assert normalise_score(50.0, 31.5) == 100

    def test_target_ceiling(self):
        assert normalise_score(16.75, TARGET_CEILING) == 51


class TestCalculateMinsFactorCanonical:
    """Verify calculate_mins_factor from the canonical location."""

    def test_nailed_starter(self):
        assert calculate_mins_factor(1800, 22, 25) == 1.0

    def test_rotation_prone(self):
        result = calculate_mins_factor(1446, 22, 25)
        assert 0.82 <= result <= 0.83

    def test_sub_only(self):
        result = calculate_mins_factor(56, 6, 25)
        assert 0.11 <= result <= 0.12

    def test_zero_appearances(self):
        assert calculate_mins_factor(0, 0, 25) == 0.0

    def test_early_season(self):
        assert calculate_mins_factor(90, 2, 3) == 1.0


class TestQualityScoreCanonical:
    """Verify quality score from the canonical location matches old path."""

    def test_mid_with_npxg(self):
        player = {
            "npxG_per_90": 0.35,
            "xGChain_per_90": 0.55,
            "xGI_per_90": 0.45,
            "form": 6.0,
            "ppg": 5.5,
        }
        score = calculate_player_quality_score(player, TARGET_QUALITY_WEIGHTS)
        # npxG: min(0.35*10, 8)=3.5, xGChain: min(0.55*2, 3)=1.1
        # form: min(6.0*1.0, 5)=5.0, ppg: min(5.5*0.5, 4)=2.75
        assert 12.3 <= score <= 12.4

    def test_gk_without_xgi(self):
        player = {
            "position": "GK",
            "npxG_per_90": 0.0,
            "xGChain_per_90": 0.0,
            "xGI_per_90": 0.0,
            "form": 4.0,
            "ppg": 4.5,
            "dc_per_90": 3.0,
        }
        weights = TARGET_QUALITY_WEIGHTS.without_xgi()
        score = calculate_player_quality_score(player, weights)
        # dc: min(3.0*0.5, 2)=1.5, form: min(4.0*1.0, 5)=4.0, ppg: min(4.5*0.5, 4)=2.25
        assert 7.7 <= score <= 7.8

    def test_mins_factor_scales_per90(self):
        player = {
            "npxG_per_90": 0.5,
            "xGChain_per_90": 0.4,
            "form": 5.0,
            "ppg": 4.0,
        }
        full = calculate_player_quality_score(player, TARGET_QUALITY_WEIGHTS, mins_factor=1.0)
        half = calculate_player_quality_score(player, TARGET_QUALITY_WEIGHTS, mins_factor=0.5)
        # per-90 halved, form+ppg unchanged
        assert half < full
        # form(5.0) + ppg(2.0) = 7.0 unchanged in both
        per90_full = full - 7.0
        per90_half = half - 7.0
        assert abs(per90_half - per90_full * 0.5) < 0.01


class TestWeightConfigs:
    """Verify weight configs match their original definitions."""

    def test_target_weights_form_cap(self):
        assert TARGET_QUALITY_WEIGHTS.form.cap == 5

    def test_differential_weights_form_cap(self):
        assert DIFFERENTIAL_QUALITY_WEIGHTS.form.cap == 7

    def test_waiver_weights_no_penalty_xg(self):
        assert WAIVER_QUALITY_WEIGHTS.penalty_xg == StatWeight(8, 3)

    def test_target_penalty_xg(self):
        assert TARGET_QUALITY_WEIGHTS.penalty_xg == StatWeight(8, 3)

    def test_attacking_positions(self):
        assert ATTACKING_POSITIONS == frozenset({"MID", "FWD"})

    def test_value_weights_form_cap(self):
        assert VALUE_QUALITY_WEIGHTS.form == StatWeight(1.3, 7)

    def test_value_weights_ppg_cap(self):
        assert VALUE_QUALITY_WEIGHTS.ppg == StatWeight(0.8, 5)

    def test_value_weights_xg_chain_downweighted(self):
        assert VALUE_QUALITY_WEIGHTS.xg_chain == StatWeight(1, 2)


class TestValueQualityScore:
    """Verify VALUE_QUALITY_WEIGHTS scoring and VALUE_CEILING normalisation."""

    def test_elite_mid_normalises_to_85_95(self):
        """Salah-tier MID: high npxG, strong form, good PPG, on pens."""
        player = {
            "npxG_per_90": 0.55, "xGChain_per_90": 0.65,
            "form": 8.0, "ppg": 7.5, "penalty_xG_per_90": 0.12,
            "form_trajectory": 1.15,
        }
        raw = calculate_player_quality_score(player, VALUE_QUALITY_WEIGHTS)
        score = normalise_score(raw, VALUE_CEILING)
        assert 85 <= score <= 95, f"Elite MID scored {score}, expected 85-95"

    def test_without_xgi_def_produces_meaningful_score(self):
        """Strong DEF: good dc_per_90, solid form and PPG."""
        player = {
            "npxG_per_90": 0.0, "xGChain_per_90": 0.0,
            "xGI_per_90": 0.0, "form": 6.0, "ppg": 5.0,
            "dc_per_90": 3.5,
        }
        weights = VALUE_QUALITY_WEIGHTS.without_xgi()
        raw = calculate_player_quality_score(player, weights)
        assert raw > 0
        score = normalise_score(raw, VALUE_CEILING)
        assert 30 <= score <= 60, f"Strong DEF scored {score}, expected 30-60"

    def test_zero_minutes_player(self):
        """Zero-minute player: per-90 zeroed via mins_factor, form/PPG still contribute."""
        player = {
            "npxG_per_90": 0.8, "xGChain_per_90": 0.5,
            "form": 3.0, "ppg": 2.0,
        }
        raw = calculate_player_quality_score(player, VALUE_QUALITY_WEIGHTS, mins_factor=0.0)
        # Only form (min(3.9, 7)=3.9) + ppg (min(1.6, 5)=1.6) = 5.5
        assert 5.4 <= raw <= 5.6

    def test_gk_without_attacking_stats(self):
        """GK with no attacking output scores via dc_per_90 + form + PPG."""
        player = {
            "npxG_per_90": 0.0, "xGChain_per_90": 0.0,
            "xGI_per_90": 0.0, "form": 4.5, "ppg": 4.0,
            "dc_per_90": 2.5,
        }
        weights = VALUE_QUALITY_WEIGHTS.without_xgi()
        raw = calculate_player_quality_score(player, weights)
        # dc: min(1.25, 2)=1.25, form: min(5.85, 7)=5.85, ppg: min(3.2, 5)=3.2
        assert raw > 0
        score = normalise_score(raw, VALUE_CEILING)
        assert 25 <= score <= 50

    def test_value_differs_from_target_for_same_player(self):
        """Same quality_dict produces different scores with VALUE vs TARGET weights."""
        player = make_player(
            form=6.0, points_per_game=5.5, minutes=1800,
            expected_goals=8.0, expected_assists=5.0, team_id=1,
        )
        eval_, _ = build_player_evaluation(
            player,
            enrichment={
                "npxG_per_90": 0.35, "xGChain_per_90": 0.45,
                "team_short": "ARS",
            },
        )
        quality_dict = eval_.as_quality_dict()
        value_raw = calculate_player_quality_score(quality_dict, VALUE_QUALITY_WEIGHTS)
        target_raw = calculate_player_quality_score(quality_dict, TARGET_QUALITY_WEIGHTS)
        assert value_raw != target_raw


class TestBuildPlayerEvaluation:
    """Tests for build_player_evaluation factory."""

    def test_from_player_model(self):
        player = make_player(
            form=6.0, points_per_game=5.5, minutes=1800, total_points=110,
            expected_goals=8.0, expected_assists=5.0,
            selected_by_percent=25.0, team_id=3,
        )
        evaluation, identity = build_player_evaluation(player)

        assert isinstance(evaluation, PlayerEvaluation)
        assert isinstance(identity, PlayerIdentity)
        assert evaluation.form == 6.0
        assert evaluation.ppg == 5.5
        assert evaluation.minutes == 1800
        assert evaluation.position == "MID"
        assert evaluation.team_id == 3
        assert identity.web_name == "TestPlayer"
        assert identity.price == 10.0
        assert identity.ownership == 25.0
        assert identity.expected_goals == 8.0

    def test_from_dict(self):
        player_dict = {
            "id": 42,
            "web_name": "Salah",
            "team_id": 11,
            "team_short": "LIV",
            "position": "MID",
            "position_name": "MID",
            "form": 8.0,
            "ppg": 7.0,
            "minutes": 2000,
            "appearances": 25,
            "price": 13.5,
            "ownership": 55.0,
            "expected_goals": 15.0,
            "expected_assists": 10.0,
            "xGI_per_90": 0.8,
            "npxG_per_90": 0.6,
            "xGChain_per_90": 0.9,
            "dc_per_90": 0.1,
            "status": "a",
        }
        evaluation, identity = build_player_evaluation(player_dict)

        assert evaluation.form == 8.0
        assert evaluation.npxg_per_90 == 0.6
        assert evaluation.position == "MID"
        assert identity.id == 42
        assert identity.web_name == "Salah"

    def test_with_enrichment_overlay(self):
        player = make_player(form=4.0)
        enrichment = {
            "npxG_per_90": 0.45,
            "xGChain_per_90": 0.7,
            "team_short": "ARS",
        }
        evaluation, identity = build_player_evaluation(
            player, enrichment=enrichment,
        )
        assert evaluation.npxg_per_90 == 0.45
        assert evaluation.xg_chain_per_90 == 0.7
        assert identity.team_short == "ARS"

    def test_none_understat_fields(self):
        player = make_player()
        evaluation, _ = build_player_evaluation(player)
        assert evaluation.npxg_per_90 is None
        assert evaluation.xg_chain_per_90 is None
        assert evaluation.penalty_xg_per_90 is None

    def test_empty_fixture_matchups(self):
        player = make_player()
        evaluation, _ = build_player_evaluation(player)
        assert evaluation.fixture_matchups == []

    def test_zero_appearances(self):
        player = make_player(minutes=0, total_points=0, points_per_game=0.0)
        evaluation, _ = build_player_evaluation(player)
        assert evaluation.appearances == 0

    def test_early_season_mins_factor(self):
        """Early season mins_factor is tested via calculate_mins_factor directly."""
        assert calculate_mins_factor(90, 2, 3) == 1.0

    def test_quality_dict_roundtrip(self):
        player = make_player(form=6.0, points_per_game=5.0)
        enrichment = {
            "npxG_per_90": 0.35,
            "xGChain_per_90": 0.55,
            "xGI_per_90": 0.45,
            "penalty_xG_per_90": 0.1,
        }
        evaluation, _ = build_player_evaluation(
            player, enrichment=enrichment,
        )
        qd = evaluation.as_quality_dict()
        score = calculate_player_quality_score(qd, TARGET_QUALITY_WEIGHTS)
        # Same as computing directly from the enrichment dict
        direct = calculate_player_quality_score(
            {**enrichment, "form": 6.0, "ppg": 5.0},
            TARGET_QUALITY_WEIGHTS,
        )
        assert abs(score - direct) < 0.01

    def test_gk_position_from_model(self):
        player = make_player(position=PlayerPosition.GOALKEEPER)
        evaluation, identity = build_player_evaluation(player)
        assert evaluation.position == "GK"
        assert identity.position_name == "GK"

    def test_prior_confidence_default(self):
        """prior_confidence defaults to 1.0 when not provided."""
        player = make_player()
        evaluation, _ = build_player_evaluation(player)
        assert evaluation.prior_confidence == 1.0

    def test_prior_confidence_from_enrichment(self):
        """prior_confidence flows through enrichment dict."""
        player = make_player()
        evaluation, _ = build_player_evaluation(
            player, enrichment={"prior_confidence": 0.6},
        )
        assert evaluation.prior_confidence == 0.6

    def test_prior_confidence_in_quality_dict(self):
        """as_quality_dict() includes prior_confidence."""
        player = make_player()
        evaluation, _ = build_player_evaluation(
            player, enrichment={"prior_confidence": 0.75},
        )
        qd = evaluation.as_quality_dict()
        assert qd["prior_confidence"] == 0.75


class TestCalculateTargetScore:
    """Characterisation tests for target scoring (exact values from pre-extraction)."""

    def test_mid_with_npxg_and_regression(self):
        """MID with npxG, penalty_xG, underperformance, good FDR and matchup."""

        eval, _ = build_player_evaluation(
            {
                "position": "MID",
                "npxG_per_90": 0.35, "xGChain_per_90": 0.55, "xGI_per_90": 0.45,
                "form": 6.0, "ppg": 5.5, "GI_minus_xGI": -2.0,
                "minutes": 1800, "appearances": 22, "penalty_xG_per_90": 0.1,
            },
            matchup_avg_3gw=7.0,
            positional_fdr=2.5,
        )
        score = calculate_target_score(eval, next_gw_id=20)
        assert score == 62

    def test_gk_def_path(self):
        """GK uses without_xgi weights, dc_per_90 active."""

        eval, _ = build_player_evaluation(
            {
                "position": "GK",
                "npxG_per_90": 0.0, "xGChain_per_90": 0.0, "xGI_per_90": 0.0,
                "form": 4.0, "ppg": 4.5, "dc_per_90": 3.0,
                "GI_minus_xGI": 0.0,
                "minutes": 1800, "appearances": 22,
            },
            matchup_avg_3gw=6.0,
            positional_fdr=3.0,
        )
        score = calculate_target_score(eval, next_gw_id=20)
        assert score == 37

    def test_zero_minutes(self):
        """Player with 0 appearances: mins_factor=0, matchup zeroed."""

        eval, _ = build_player_evaluation(
            {
                "position": "MID", "xGI_per_90": 0.8, "form": 5.0, "ppg": 4.0,
                "GI_minus_xGI": 0.0, "minutes": 0, "appearances": 0,
            },
        )
        score = calculate_target_score(eval, next_gw_id=20)
        assert score == 21


class TestTargetDiffAvailabilityPenalty:
    """Availability penalty in _calculate_quality_based_score."""

    def _eval(self, status="a", chance=None):
        return build_player_evaluation(
            {
                "position": "MID", "xGI_per_90": 0.5, "form": 5.0, "ppg": 4.0,
                "GI_minus_xGI": 0.0, "minutes": 1500, "appearances": 20,
                "status": status, "chance_of_playing": chance,
            },
            matchup_avg_3gw=6.0,
        )[0]

    def test_available_no_penalty(self):
        score = calculate_target_score(self._eval(), next_gw_id=20)
        assert score == calculate_target_score(self._eval(status="a"), next_gw_id=20)

    def test_flagged_below_threshold(self):
        available = calculate_target_score(self._eval(), next_gw_id=20)
        flagged = calculate_target_score(self._eval(status="d", chance=50), next_gw_id=20)
        assert flagged < available

    def test_flagged_above_threshold_no_penalty(self):
        available = calculate_target_score(self._eval(), next_gw_id=20)
        flagged = calculate_target_score(self._eval(status="d", chance=80), next_gw_id=20)
        assert flagged == available

    def test_flagged_none_chance_no_penalty(self):
        available = calculate_target_score(self._eval(), next_gw_id=20)
        flagged = calculate_target_score(self._eval(status="d", chance=None), next_gw_id=20)
        assert flagged == available

    def test_differential_also_penalised(self):
        available = calculate_differential_score(
            self._eval(), semi_differential_threshold=20, next_gw_id=20,
        )
        flagged = calculate_differential_score(
            self._eval(status="d", chance=50), semi_differential_threshold=20, next_gw_id=20,
        )
        assert flagged < available


class TestCalculateDifferentialScore:
    """Characterisation tests for differential scoring."""

    def test_low_ownership_mid(self):
        """Low ownership MID with underperformance and good matchup."""

        eval, _ = build_player_evaluation(
            {
                "position": "MID",
                "npxG_per_90": 0.35, "xGChain_per_90": 0.55, "xGI_per_90": 0.45,
                "form": 6.0, "ppg": 5.5, "ownership": 3.0,
                "GI_minus_xGI": -2.0,
                "minutes": 1800, "appearances": 22, "penalty_xG_per_90": 0.1,
            },
            matchup_avg_3gw=7.0,
            positional_fdr=2.5,
        )
        score = calculate_differential_score(eval, semi_differential_threshold=10, next_gw_id=20)
        assert score == 61

    def test_no_matchup_avg_fallback(self):
        """Without matchup_avg_3gw, matchup contribution is 0 (fallback=0.0)."""

        eval, _ = build_player_evaluation(
            {
                "position": "MID", "xGI_per_90": 0.6, "form": 5.0, "ppg": 4.0,
                "ownership": 8.0, "GI_minus_xGI": 0.0,
                "minutes": 1500, "appearances": 20,
            },
            positional_fdr=4.0,
        )
        score = calculate_differential_score(
            eval, semi_differential_threshold=10, next_gw_id=20,
        )
        assert score == 37


class TestCalculateWaiverScore:
    """Characterisation tests for waiver scoring."""

    def _squad_by_pos(self):
        return {
            "MID": [{"form": 4.0}, {"form": 5.0}],
            "FWD": [],
            "DEF": [{"form": 2.0}],
            "GK": [{"form": 3.0}],
        }

    def _team_counts(self):
        return {"LIV": 2, "ARS": 3}

    def test_nailed_mid(self):
        eval, _ = build_player_evaluation(
            {"position": "MID", "form": 7.0, "ppg": 5.5, "minutes": 900, "appearances": 10,
             "xGI_per_90": 0.6, "npxG_per_90": 0.4, "xGChain_per_90": 0.5,
             "status": "a", "team_short": "BHA"},
            matchup_avg_3gw=6.5, positional_fdr=2.5,
        )
        score = calculate_waiver_score(
            eval, squad_by_position=self._squad_by_pos(),
            team_counts=self._team_counts(), next_gw_id=20,
        )
        assert score == 47

    def test_zero_appearances(self):
        eval, _ = build_player_evaluation(
            {"position": "FWD", "form": 3.0, "ppg": 2.0, "minutes": 0, "appearances": 0,
             "xGI_per_90": 0.0, "status": "a", "team_short": "NFO"},
            matchup_avg_3gw=5.0, positional_fdr=4.0,
        )
        score = calculate_waiver_score(
            eval, squad_by_position=self._squad_by_pos(),
            team_counts=self._team_counts(), next_gw_id=20,
        )
        assert score == 26

    def test_team_stacking_penalty(self):
        eval, _ = build_player_evaluation(
            {"position": "MID", "form": 6.0, "ppg": 5.0, "minutes": 1500, "appearances": 20,
             "xGI_per_90": 0.5, "status": "a", "team_short": "ARS"},
            matchup_avg_3gw=6.0, positional_fdr=3.0,
        )
        score = calculate_waiver_score(
            eval, squad_by_position=self._squad_by_pos(),
            team_counts=self._team_counts(), next_gw_id=20,
        )
        assert score == 30

    def test_availability_penalty(self):
        eval, _ = build_player_evaluation(
            {"position": "DEF", "form": 4.0, "ppg": 3.5, "minutes": 800, "appearances": 10,
             "xGI_per_90": 0.2, "dc_per_90": 2.5, "status": "d", "chance_of_playing": 50,
             "team_short": "NFO"},
            matchup_avg_3gw=5.0, positional_fdr=3.5,
        )
        score = calculate_waiver_score(
            eval, squad_by_position=self._squad_by_pos(),
            team_counts=self._team_counts(), next_gw_id=20,
        )
        assert score == 32

    def test_position_need_empty(self):
        eval, _ = build_player_evaluation(
            {"position": "FWD", "form": 5.0, "ppg": 4.0, "minutes": 1200, "appearances": 15,
             "xGI_per_90": 0.4, "status": "a", "team_short": "BHA"},
            matchup_avg_3gw=5.5, positional_fdr=3.0,
        )
        score = calculate_waiver_score(
            eval, squad_by_position=self._squad_by_pos(),
            team_counts=self._team_counts(), next_gw_id=20,
        )
        assert score == 52

    def test_early_season_combined_mins_factor_defaults_to_one(self):
        """Before GW5, combined_mins_factor hardcodes to 1.0 regardless of minutes."""
        eval, _ = build_player_evaluation(
            {"position": "MID", "form": 5.0, "ppg": 4.0, "minutes": 270, "appearances": 3,
             "xGI_per_90": 0.5, "status": "a", "team_short": "BHA"},
            matchup_avg_3gw=6.0, positional_fdr=3.0,
        )
        squad = {"MID": [{"form": 4.0}], "FWD": [], "DEF": [], "GK": []}
        early = calculate_waiver_score(
            eval, squad_by_position=squad, team_counts={}, next_gw_id=3,
        )
        midseason = calculate_waiver_score(
            eval, squad_by_position=squad, team_counts={}, next_gw_id=20,
        )
        assert early == 41
        assert midseason == 34
        assert early > midseason  # Early season is more generous


class TestMatchupBonus:
    """Tests for _matchup_bonus helper."""

    def test_none_returns_zero(self):
        assert _matchup_bonus(None, 0.9) == 0.0

    def test_with_value(self):
        assert abs(_matchup_bonus(7.0, 0.9) - 4.725) < 0.001

    def test_zero_mins_factor(self):
        assert _matchup_bonus(7.0, 0.0) == 0.0


class TestWaiverUnderperformanceBonus:
    """Tests for underperformance bonus added to waiver scoring."""

    def _squad_by_pos(self):
        return {
            "MID": [{"form": 4.0}, {"form": 5.0}],
            "FWD": [],
            "DEF": [{"form": 2.0}],
            "GK": [{"form": 3.0}],
        }

    def test_underperforming_player_gets_bonus(self):
        """Player with gi_minus_xgi=-2.5 gets +2.5 bonus."""
        eval, _ = build_player_evaluation(
            {"position": "MID", "form": 7.0, "ppg": 5.5, "minutes": 900, "appearances": 10,
             "xGI_per_90": 0.6, "npxG_per_90": 0.4, "xGChain_per_90": 0.5,
             "GI_minus_xGI": -2.5,
             "status": "a", "team_short": "BHA"},
            matchup_avg_3gw=6.5, positional_fdr=2.5,
        )
        score_with = calculate_waiver_score(
            eval, squad_by_position=self._squad_by_pos(),
            team_counts={}, next_gw_id=20,
        )
        # Same player without underperformance
        eval_no, _ = build_player_evaluation(
            {"position": "MID", "form": 7.0, "ppg": 5.5, "minutes": 900, "appearances": 10,
             "xGI_per_90": 0.6, "npxG_per_90": 0.4, "xGChain_per_90": 0.5,
             "GI_minus_xGI": 0.0,
             "status": "a", "team_short": "BHA"},
            matchup_avg_3gw=6.5, positional_fdr=2.5,
        )
        score_without = calculate_waiver_score(
            eval_no, squad_by_position=self._squad_by_pos(),
            team_counts={}, next_gw_id=20,
        )
        assert score_with > score_without

    def test_no_bonus_when_overperforming(self):
        """Player with gi_minus_xgi=0 gets no underperformance bonus."""
        eval, _ = build_player_evaluation(
            {"position": "MID", "form": 7.0, "ppg": 5.5, "minutes": 900, "appearances": 10,
             "xGI_per_90": 0.6, "npxG_per_90": 0.4, "xGChain_per_90": 0.5,
             "GI_minus_xGI": 0.0,
             "status": "a", "team_short": "BHA"},
            matchup_avg_3gw=6.5, positional_fdr=2.5,
        )
        score = calculate_waiver_score(
            eval, squad_by_position=self._squad_by_pos(),
            team_counts={}, next_gw_id=20,
        )
        # Same as the previous nailed_mid test (without team_counts penalty)
        assert score == 47

    def test_boundary_no_bonus_at_negative_one(self):
        """Player with gi_minus_xgi=-1.0 exactly does NOT get bonus (threshold is < -1)."""
        eval_boundary, _ = build_player_evaluation(
            {"position": "MID", "form": 7.0, "ppg": 5.5, "minutes": 900, "appearances": 10,
             "xGI_per_90": 0.6, "npxG_per_90": 0.4, "xGChain_per_90": 0.5,
             "GI_minus_xGI": -1.0,
             "status": "a", "team_short": "BHA"},
            matchup_avg_3gw=6.5, positional_fdr=2.5,
        )
        eval_zero, _ = build_player_evaluation(
            {"position": "MID", "form": 7.0, "ppg": 5.5, "minutes": 900, "appearances": 10,
             "xGI_per_90": 0.6, "npxG_per_90": 0.4, "xGChain_per_90": 0.5,
             "GI_minus_xGI": 0.0,
             "status": "a", "team_short": "BHA"},
            matchup_avg_3gw=6.5, positional_fdr=2.5,
        )
        score_boundary = calculate_waiver_score(
            eval_boundary, squad_by_position=self._squad_by_pos(),
            team_counts={}, next_gw_id=20,
        )
        score_zero = calculate_waiver_score(
            eval_zero, squad_by_position=self._squad_by_pos(),
            team_counts={}, next_gw_id=20,
        )
        assert score_boundary == score_zero

    def test_large_underperformance_capped_at_three(self):
        """Player with gi_minus_xgi=-5.0 gets bonus capped at 3, not 5."""
        eval_large, _ = build_player_evaluation(
            {"position": "MID", "form": 7.0, "ppg": 5.5, "minutes": 900, "appearances": 10,
             "xGI_per_90": 0.6, "npxG_per_90": 0.4, "xGChain_per_90": 0.5,
             "GI_minus_xGI": -5.0,
             "status": "a", "team_short": "BHA"},
            matchup_avg_3gw=6.5, positional_fdr=2.5,
        )
        eval_three, _ = build_player_evaluation(
            {"position": "MID", "form": 7.0, "ppg": 5.5, "minutes": 900, "appearances": 10,
             "xGI_per_90": 0.6, "npxG_per_90": 0.4, "xGChain_per_90": 0.5,
             "GI_minus_xGI": -4.0,
             "status": "a", "team_short": "BHA"},
            matchup_avg_3gw=6.5, positional_fdr=2.5,
        )
        score_large = calculate_waiver_score(
            eval_large, squad_by_position=self._squad_by_pos(),
            team_counts={}, next_gw_id=20,
        )
        score_three = calculate_waiver_score(
            eval_three, squad_by_position=self._squad_by_pos(),
            team_counts={}, next_gw_id=20,
        )
        # Both capped at +3 bonus, so scores should be equal
        assert score_large == score_three


class TestThinWrappers:
    """Verify target/differential are thin wrappers over _calculate_quality_based_score."""

    @staticmethod
    def _body_lines(func):
        """Count non-blank, non-comment, non-docstring lines in function body (after signature)."""
        import inspect
        source = inspect.getsource(func)
        lines = source.splitlines()
        # Skip until after the closing ')' of the signature
        body_start = 0
        for i, line in enumerate(lines):
            if line.rstrip().endswith(":") and ("def " in lines[0] or i > 0):
                body_start = i + 1
                break
        body = lines[body_start:]
        return [
            ln for ln in body
            if ln.strip() and not ln.strip().startswith(('"""', '#'))
        ]

    def test_target_is_thin(self):
        """calculate_target_score body is < 10 lines (just delegates)."""
        body = self._body_lines(calculate_target_score)
        assert len(body) <= 10, f"Body has {len(body)} lines: {body}"

    def test_differential_is_thin(self):
        """calculate_differential_score body is < 10 lines."""
        body = self._body_lines(calculate_differential_score)
        assert len(body) <= 10, f"Body has {len(body)} lines: {body}"

    def test_waiver_has_not_regrown(self):
        """calculate_waiver_score stays compact (delegates shared flow to raw)."""
        body = self._body_lines(calculate_waiver_score)
        assert len(body) <= 40, f"Waiver body has {len(body)} lines - may have re-duplicated shared logic: {body}"


class TestCalculateCaptainScore:
    """Characterisation tests for captain scoring."""

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

    def test_sgw_home_fwd_good_form(self):
        player = make_player(
            id=10, web_name="Havertz", team_id=1,
            position=PlayerPosition.FORWARD,
            form=7.5, points_per_game=6.0, minutes=1800, total_points=132,
            expected_goals=10.0, expected_assists=5.0, penalties_order=1,
        )
        fm = [self._make_matchup()]
        eval, identity = build_player_evaluation(
            player,
            enrichment={"npxG_per_90": 0.45, "team_short": "ARS", "penalty_xG_per_90": 0.20},
            fixture_matchups=fm,
        )
        result = calculate_captain_score(eval, identity, next_gw_id=20)
        assert result is not None
        assert result["captain_score"] == 94
        assert result["captain_score_raw"] == 30.1
        assert result["pen_bonus"] == 1.6
        assert "Good matchup" in result["reasons"]
        assert "Excellent FDR (2.5)" in result["reasons"]
        assert "In great form (7.5)" in result["reasons"]
        assert "Playing at home" in result["reasons"]
        assert "Primary penalty taker" in result["reasons"]

    def test_dgw_sums_matchups_and_scales_xgi(self):
        """DGW: matchup_total sums across fixtures, xGI scales by fixture_count."""
        player = make_player(
            id=10, web_name="Salah", team_id=11,
            position=PlayerPosition.MIDFIELDER,
            form=8.0, points_per_game=7.0, minutes=1800, total_points=154,
            expected_goals=12.0, expected_assists=8.0,
        )
        fm_home = self._make_matchup(score=7.5, fdr=2.0, is_home=True, opponent="SHU")
        fm_away = FixtureMatchup(
            opponent_short="LEI", is_home=False, opponent_fdr=2.5,
            matchup_score=6.0, matchup_breakdown={
                "matchup_score": 6.0, "attack_matchup": 5.5, "defence_matchup": 4.5,
                "form_differential": 0.1, "position_differential": 0.15,
                "reasoning": ["Good attack matchup (5.5)"],
            },
        )
        eval, identity = build_player_evaluation(
            player,
            enrichment={"npxG_per_90": 0.5, "team_short": "LIV"},
            fixture_matchups=[fm_home, fm_away],
        )
        result = calculate_captain_score(eval, identity, next_gw_id=20)
        assert result is not None
        assert result["fixture_count"] == 2
        assert result["captain_score"] == 100  # Clips at ceiling
        assert result["captain_score_raw"] == 47.0  # Well above SGW ceiling
        # Reasoning aggregates from both fixtures + DGW bonus
        assert "Double gameweek (2 games)" in result["reasons"]
        assert "Good matchup" in result["reasons"]  # From fm_home
        assert "Good attack matchup (5.5)" in result["reasons"]  # From fm_away
        assert "Excellent FDR (2.2)" in result["reasons"]
        assert "In great form (8.0)" in result["reasons"]
        assert "Playing at home" in result["reasons"]

    def test_def_position_multiplier(self):
        player = make_player(
            id=20, web_name="Saliba", team_id=1,
            position=PlayerPosition.DEFENDER,
            form=5.0, points_per_game=5.5, minutes=1800, total_points=121,
            expected_goals=2.0, expected_assists=1.0,
        )
        fm = [self._make_matchup()]
        eval, identity = build_player_evaluation(
            player,
            enrichment={"team_short": "ARS"},
            fixture_matchups=fm,
        )
        result = calculate_captain_score(eval, identity, next_gw_id=20)
        assert result is not None
        assert result["captain_score"] == 62
        assert result["captain_score_raw"] == 19.91

    def test_zero_appearances(self):
        player = make_player(
            id=30, web_name="NewSign", team_id=1,
            position=PlayerPosition.MIDFIELDER,
            form=0.0, points_per_game=0.0, minutes=0, total_points=0,
        )
        fm = [self._make_matchup()]
        eval, identity = build_player_evaluation(
            player,
            enrichment={"team_short": "ARS"},
            fixture_matchups=fm,
        )
        result = calculate_captain_score(eval, identity, next_gw_id=20)
        assert result is not None
        assert result["captain_score"] == 3
        assert result["captain_score_raw"] == 1.0

    def test_blank_gw_returns_none(self):
        player = make_player()
        eval, identity = build_player_evaluation(
            player, enrichment={"team_short": "ARS"},
            fixture_matchups=[],
        )
        assert calculate_captain_score(eval, identity, next_gw_id=20) is None

    def test_raw_and_normalised_preserved(self):
        """Both captain_score and captain_score_raw must be in the result."""
        player = make_player(form=6.0, minutes=1500, total_points=100, penalties_order=1)
        fm = [self._make_matchup()]
        eval, identity = build_player_evaluation(
            player, enrichment={"team_short": "ARS"}, fixture_matchups=fm,
        )
        result = calculate_captain_score(eval, identity, next_gw_id=20)
        assert "captain_score" in result
        assert "captain_score_raw" in result
        assert isinstance(result["captain_score"], int)
        assert isinstance(result["captain_score_raw"], float)

    def test_no_understat_uses_xgi_fallback(self):
        """Player without npxG enrichment falls back to FPL-derived xGI."""
        player = make_player(
            id=40, web_name="NoUnderstat", team_id=1,
            position=PlayerPosition.FORWARD,
            form=6.0, points_per_game=5.0, minutes=1800, total_points=100,
            expected_goals=8.0, expected_assists=4.0,
        )
        fm = [self._make_matchup()]
        # No npxG_per_90 in enrichment → fallback path
        eval, identity = build_player_evaluation(
            player, enrichment={"team_short": "ARS"}, fixture_matchups=fm,
        )
        assert eval.npxg_per_90 is None
        result = calculate_captain_score(eval, identity, next_gw_id=20)
        assert result is not None
        # xg_per_90 = 8/1800*90 = 0.4, xa_per_90 = 4/1800*90 = 0.2
        # xgi_fallback = (0.4+0.2)*5 = 3.0, capped at 10 → 3.0
        # form = min(6.0*1.5, 10) = 9.0
        # ceiling = (7*2 + 9.0 + 3.0) * 1.0 * 1.0 = 26.0
        # score = 26.0 + 1.0 (home) + 0.0 (no pen) = 27.0
        assert result["captain_score_raw"] == 27.0
        assert result["pen_bonus"] == 0.0

    def test_xg_chain_weight_zero_ignored(self):
        """xg_chain evaluation field is ignored (weight is 0,0)."""
        player = make_player(
            id=41, web_name="ChainPlayer", team_id=1,
            position=PlayerPosition.MIDFIELDER,
            form=7.0, points_per_game=6.0, minutes=1800, total_points=120,
            expected_goals=10.0, expected_assists=5.0,
        )
        fm = [self._make_matchup()]
        # Provide npxG and xGChain — chain should be ignored
        eval_with_chain, identity = build_player_evaluation(
            player,
            enrichment={"npxG_per_90": 0.4, "xGChain_per_90": 0.9, "team_short": "ARS"},
            fixture_matchups=fm,
        )
        eval_without_chain, _ = build_player_evaluation(
            player,
            enrichment={"npxG_per_90": 0.4, "xGChain_per_90": 0.0, "team_short": "ARS"},
            fixture_matchups=fm,
        )
        r1 = calculate_captain_score(eval_with_chain, identity, next_gw_id=20)
        r2 = calculate_captain_score(eval_without_chain, identity, next_gw_id=20)
        assert r1 is not None and r2 is not None
        assert r1["captain_score_raw"] == r2["captain_score_raw"]

    def test_availability_warning_flagged_player(self):
        """Flagged player gets availability warning in reasons, no score change."""
        player = make_player(
            id=42, web_name="Flagged", team_id=1,
            position=PlayerPosition.MIDFIELDER,
            form=6.0, points_per_game=5.0, minutes=1500, total_points=100,
            expected_goals=6.0, expected_assists=3.0,
            status=PlayerStatus.DOUBTFUL, chance_of_playing_next_round=75,
        )
        fm = [self._make_matchup()]
        eval_flagged, identity = build_player_evaluation(
            player, enrichment={"team_short": "ARS"}, fixture_matchups=fm,
        )
        # Build an identical available player for score comparison
        player_avail = make_player(
            id=42, web_name="Flagged", team_id=1,
            position=PlayerPosition.MIDFIELDER,
            form=6.0, points_per_game=5.0, minutes=1500, total_points=100,
            expected_goals=6.0, expected_assists=3.0,
        )
        eval_avail, identity_avail = build_player_evaluation(
            player_avail, enrichment={"team_short": "ARS"}, fixture_matchups=fm,
        )
        result_flagged = calculate_captain_score(eval_flagged, identity, next_gw_id=20)
        result_avail = calculate_captain_score(eval_avail, identity_avail, next_gw_id=20)
        assert result_flagged is not None and result_avail is not None
        assert "Flagged (75% chance)" in result_flagged["reasons"]
        assert "Flagged" not in " ".join(result_avail["reasons"])
        # Score is unchanged — availability warning is informational only
        assert result_flagged["captain_score"] == result_avail["captain_score"]


class TestCalculateBenchScore:
    """Characterisation tests for bench scoring."""

    def _fm(self, fdr=2.5, matchup_score=7.0):
        return FixtureMatchup(
            opponent_short="SHU", is_home=True, opponent_fdr=fdr, matchup_score=matchup_score,
        )

    def test_good_ppg_mid_with_form(self):
        player = make_player(
            id=1, web_name="Saka", team_id=1,
            position=PlayerPosition.MIDFIELDER,
            form=6.0, points_per_game=6.0, minutes=1500, total_points=120,
        )
        eval, identity = build_player_evaluation(
            player, enrichment={"team_short": "ARS"},
            fixture_matchups=[self._fm()],
        )
        result = calculate_bench_score(
            eval, identity,
            availability_risks=[{"position": "MID", "risk_level": 3}],
            next_gw_id=20,
        )
        assert result["priority_score"] == 71
        assert result["priority_score_raw"] == 23.33
        assert "Covers risky starter" in result["reasons"]

    def test_zero_minutes(self):
        player = make_player(
            id=2, web_name="NewSign", team_id=1,
            position=PlayerPosition.FORWARD,
            form=0.0, points_per_game=0.0, minutes=0, total_points=0,
        )
        eval, identity = build_player_evaluation(
            player, enrichment={"team_short": "ARS"},
            fixture_matchups=[self._fm()],
        )
        result = calculate_bench_score(eval, identity, availability_risks=[], next_gw_id=20)
        assert result["priority_score"] == 3
        assert result["priority_score_raw"] == 1.0

    def test_doubtful_player(self):
        player = make_player(
            id=3, web_name="Injury", team_id=1,
            position=PlayerPosition.DEFENDER,
            form=4.0, points_per_game=4.5, minutes=800, total_points=50,
            status=PlayerStatus.DOUBTFUL, chance_of_playing_next_round=25,
        )
        eval, identity = build_player_evaluation(
            player, enrichment={"team_short": "ARS"},
            fixture_matchups=[self._fm()],
        )
        result = calculate_bench_score(eval, identity, availability_risks=[], next_gw_id=20)
        assert result["priority_score"] == 36
        assert result["priority_score_raw"] == 11.92
        assert "Doubt (25%)" in result["reasons"]

    def test_dgw_fixture_bonus(self):
        """DGW bench player: DGW advantage implicit in matchup sum + xGI * fixture_count."""
        player = make_player(
            id=5, web_name="Bench", team_id=1,
            position=PlayerPosition.MIDFIELDER,
            form=4.0, points_per_game=4.0, minutes=1200, total_points=60,
        )
        eval, identity = build_player_evaluation(
            player, enrichment={"team_short": "ARS"},
            fixture_matchups=[self._fm(fdr=2.0, matchup_score=8.0), self._fm(fdr=2.5, matchup_score=7.0)],
        )
        result = calculate_bench_score(eval, identity, availability_risks=[], next_gw_id=20)
        assert result["priority_score"] == 100
        assert result["priority_score_raw"] == 34.98

    def test_penalty_taker(self):
        player = make_player(
            id=4, web_name="PenTaker", team_id=1,
            position=PlayerPosition.MIDFIELDER,
            form=5.0, points_per_game=5.0, minutes=1200, total_points=80,
            penalties_order=1,
        )
        eval, identity = build_player_evaluation(
            player, enrichment={"team_short": "ARS"},
            fixture_matchups=[self._fm()],
        )
        result = calculate_bench_score(eval, identity, availability_risks=[], next_gw_id=20)
        assert result["priority_score"] == 63
        assert result["priority_score_raw"] == 20.94
        assert "Primary penalty taker" in result["reasons"]


class TestScoringContext:
    """Tests for ScoringContext dataclass and build_scoring_context factory."""

    def _make_ratings_service(self):
        from fpl_cli.services.team_ratings import TeamRatingsService
        svc = TeamRatingsService.__new__(TeamRatingsService)
        svc._ratings = {}
        svc._loaded = True
        svc._metadata = None
        return svc

    async def test_build_with_all_options(self):
        """Build context with team form enabled, verify all fields populated."""
        from tests.conftest import make_fixture, make_team

        teams = [
            make_team(id=1, short_name="ARS"),
            make_team(id=2, short_name="SHU"),
        ]
        fixtures = [make_fixture(gameweek=25, home_team_id=1, away_team_id=2)]
        all_fixtures = fixtures + [
            make_fixture(id=2, gameweek=26, home_team_id=2, away_team_id=1, finished=True,
                         home_score=1, away_score=2),
        ]

        ctx = await build_scoring_context(
            teams=teams,
            fixtures=fixtures,
            ratings_service=self._make_ratings_service(),
            next_gw_id=25,
            all_fixtures=all_fixtures,
            include_team_form=True,
            understat_lookup={1: {"npxG_per_90": 0.5}},
        )

        assert isinstance(ctx, ScoringContext)
        assert len(ctx.team_map) == 2
        assert 1 in ctx.team_fixture_map  # ARS has a fixture
        assert ctx.team_form_by_id is not None
        assert ctx.understat_lookup is not None
        assert ctx.gw_fixture_maps is not None
        assert ctx.next_gw_id == 25

    async def test_build_without_team_form(self):
        """Build context with include_team_form=False, verify team_form_by_id is None."""
        from tests.conftest import make_fixture, make_team

        teams = [make_team(id=1, short_name="ARS")]
        fixtures = [make_fixture(gameweek=25, home_team_id=1, away_team_id=2)]

        ctx = await build_scoring_context(
            teams=teams,
            fixtures=fixtures,
            ratings_service=self._make_ratings_service(),
            next_gw_id=25,
        )

        assert ctx.team_form_by_id is None
        assert ctx.understat_lookup is None
        assert ctx.gw_fixture_maps is None

    async def test_build_with_empty_fixtures(self):
        """Build context with empty fixtures list, verify team_fixture_map is empty."""
        from tests.conftest import make_team

        teams = [make_team(id=1, short_name="ARS")]

        ctx = await build_scoring_context(
            teams=teams,
            fixtures=[],
            ratings_service=self._make_ratings_service(),
            next_gw_id=25,
        )

        assert ctx.team_fixture_map == {}

    async def test_build_with_no_ratings(self):
        """Build context when ratings_service has no ratings, verify context still builds."""
        from tests.conftest import make_fixture, make_team

        teams = [make_team(id=1, short_name="ARS")]
        fixtures = [make_fixture(gameweek=25, home_team_id=1, away_team_id=2)]

        ctx = await build_scoring_context(
            teams=teams,
            fixtures=fixtures,
            ratings_service=self._make_ratings_service(),
            next_gw_id=25,
        )

        assert isinstance(ctx, ScoringContext)
        assert ctx.ratings_service is not None

    def test_scoring_context_is_frozen(self):
        """ScoringContext should be immutable."""
        import pytest
        ctx = ScoringContext(
            team_map={}, team_fixture_map={},
            ratings_service=None,  # type: ignore[arg-type]
        )
        with pytest.raises(dataclasses.FrozenInstanceError):
            ctx.team_map = {}  # type: ignore[misc]


class TestPrepareScoringData:
    """Tests for prepare_scoring_data shared helper."""

    async def test_returns_scoring_data_with_base_fields(self):
        """Base call populates teams, fixtures, next_gw_id, scoring_ctx, ratings_service."""
        from unittest.mock import AsyncMock

        from tests.conftest import make_fixture, make_team

        teams = [make_team(id=1, short_name="ARS"), make_team(id=2, short_name="SHU")]
        fixture = make_fixture(gameweek=25, home_team_id=1, away_team_id=2)
        all_fixtures = [fixture]

        client = AsyncMock()
        client.get_teams.return_value = teams
        client.get_fixtures.return_value = all_fixtures
        client.get_next_gameweek.return_value = {"id": 25, "deadline_time": "2026-03-15T11:30:00Z"}

        data = await prepare_scoring_data(client)

        assert isinstance(data, ScoringData)
        assert data.teams == teams
        assert len(data.team_map) == 2
        assert data.all_fixtures == all_fixtures
        assert data.next_gw_fixtures == [fixture]
        assert data.next_gw_id == 25
        assert data.next_gw == {"id": 25, "deadline_time": "2026-03-15T11:30:00Z"}
        assert data.scoring_ctx is not None
        assert data.ratings_service is not None
        assert data.players is None
        assert data.understat_lookup is None

    async def test_include_players_populates_players(self):
        """include_players=True fetches and returns players."""
        from unittest.mock import AsyncMock

        from tests.conftest import make_fixture, make_team

        teams = [make_team(id=1, short_name="ARS")]
        players = [make_player(id=1, web_name="Saka", team_id=1)]

        client = AsyncMock()
        client.get_teams.return_value = teams
        client.get_fixtures.return_value = [make_fixture(gameweek=25, home_team_id=1, away_team_id=2)]
        client.get_next_gameweek.return_value = {"id": 25}
        client.get_players.return_value = players

        data = await prepare_scoring_data(client, include_players=True)

        assert data.players == players
        assert data.understat_lookup is None

    async def test_include_understat_and_players_populates_lookup(self):
        """include_understat=True with include_players=True populates understat_lookup."""
        from unittest.mock import AsyncMock, patch

        from tests.conftest import make_fixture, make_team

        teams = [make_team(id=1, short_name="ARS")]
        players = [make_player(id=1, web_name="Saka", team_id=1)]
        mock_us = {1: {"npxG_per_90": 0.45, "xGChain_per_90": 0.55, "penalty_xG_per_90": 0.1}}

        client = AsyncMock()
        client.get_teams.return_value = teams
        client.get_fixtures.return_value = [make_fixture(gameweek=25, home_team_id=1, away_team_id=2)]
        client.get_next_gameweek.return_value = {"id": 25}
        client.get_players.return_value = players

        with patch(
            "fpl_cli.services.player_scoring.build_understat_by_player_id",
            new_callable=AsyncMock,
            return_value=mock_us,
        ) as mock_build_us:
            data = await prepare_scoring_data(
                client, include_players=True, include_understat=True,
            )

        assert data.players == players
        assert data.understat_lookup == mock_us
        mock_build_us.assert_awaited_once()

    async def test_include_understat_requires_include_players(self):
        """include_understat=True without include_players raises ValueError."""
        import pytest
        from unittest.mock import AsyncMock

        client = AsyncMock()
        with pytest.raises(ValueError, match="include_understat requires include_players"):
            await prepare_scoring_data(client, include_understat=True, include_players=False)

    async def test_next_gw_none_defaults_to_38(self):
        """When get_next_gameweek returns None, next_gw_id defaults to 38."""
        from unittest.mock import AsyncMock

        from tests.conftest import make_team

        client = AsyncMock()
        client.get_teams.return_value = [make_team(id=1, short_name="ARS")]
        client.get_fixtures.return_value = []
        client.get_next_gameweek.return_value = None

        data = await prepare_scoring_data(client)

        assert data.next_gw_id == 38
        assert data.next_gw is None
        assert data.next_gw_fixtures == []


class TestBuildFixtureMatchups:
    """Tests for build_fixture_matchups helper."""

    def _make_context(self, *, with_form: bool = True):
        from fpl_cli.services.team_ratings import TeamRating, TeamRatingsService
        from tests.conftest import make_fixture, make_team

        teams = [
            make_team(id=1, name="Arsenal", short_name="ARS"),
            make_team(id=2, name="Sheffield Utd", short_name="SHU"),
            make_team(id=3, name="Brighton", short_name="BHA"),
        ]

        svc = TeamRatingsService.__new__(TeamRatingsService)
        svc._ratings = {
            "ARS": TeamRating(atk_home=1, atk_away=2, def_home=1, def_away=2),
            "SHU": TeamRating(atk_home=6, atk_away=7, def_home=6, def_away=7),
            "BHA": TeamRating(atk_home=4, atk_away=4, def_home=4, def_away=4),
        }
        svc._loaded = True
        svc._metadata = None

        fixtures = [make_fixture(id=1, gameweek=25, home_team_id=1, away_team_id=2)]
        from fpl_cli.services.matchup import build_team_fixture_map
        team_fixture_map = build_team_fixture_map(fixtures)

        team_form_by_id = None
        if with_form:
            team_form_by_id = {
                1: {"team_id": 1, "league_position": 1, "pts_6": 15, "gs_6": 12, "gc_6": 2,
                    "gs_home": 8, "gc_home": 1, "gs_away": 4, "gc_away": 1,
                    "pts_home": 12, "pts_away": 6},
                2: {"team_id": 2, "league_position": 20, "pts_6": 2, "gs_6": 2, "gc_6": 14,
                    "gs_home": 1, "gc_home": 8, "gs_away": 1, "gc_away": 6,
                    "pts_home": 1, "pts_away": 1},
            }

        return ScoringContext(
            team_map={t.id: t for t in teams},
            team_fixture_map=team_fixture_map,
            ratings_service=svc,
            team_form_by_id=team_form_by_id,
        )

    def test_single_fixture(self):
        """ARS home vs SHU: single FixtureMatchup with positional FDR."""
        ctx = self._make_context()
        matchups = build_fixture_matchups(1, "FWD", ctx)
        assert len(matchups) == 1
        fm = matchups[0]
        assert fm.opponent_short == "SHU"
        assert fm.is_home is True
        # FWD vs SHU (weak defence): positional FDR should be low (easy)
        assert fm.opponent_fdr < 4.0
        assert fm.matchup_score > 0

    def test_dgw_returns_two(self):
        """Team with two fixtures returns two FixtureMatchup objects."""
        from tests.conftest import make_fixture
        from fpl_cli.services.matchup import build_team_fixture_map

        ctx = self._make_context()
        # Add second fixture for ARS
        fixtures = [
            make_fixture(id=1, gameweek=25, home_team_id=1, away_team_id=2),
            make_fixture(id=2, gameweek=25, home_team_id=3, away_team_id=1),
        ]
        ctx2 = dataclasses.replace(ctx, team_fixture_map=build_team_fixture_map(fixtures))
        matchups = build_fixture_matchups(1, "FWD", ctx2)
        assert len(matchups) == 2

    def test_bgw_returns_empty(self):
        """Team with no fixtures returns empty list."""
        ctx = self._make_context()
        matchups = build_fixture_matchups(99, "FWD", ctx)
        assert matchups == []

    def test_matchup_with_form(self):
        """When team form available, matchup_score and breakdown are populated."""
        ctx = self._make_context(with_form=True)
        matchups = build_fixture_matchups(1, "FWD", ctx)
        fm = matchups[0]
        assert fm.matchup_breakdown is not None
        assert "matchup_score" in fm.matchup_breakdown

    def test_matchup_without_form(self):
        """Without team form, matchup_score falls back to 5.0."""
        ctx = self._make_context(with_form=False)
        matchups = build_fixture_matchups(1, "FWD", ctx)
        fm = matchups[0]
        assert fm.matchup_score == 5.0

    def test_positional_fdr_semantic_ordering(self):
        """FWD vs weak defence should get lower FDR than DEF vs strong attack."""
        ctx = self._make_context()
        fwd_matchups = build_fixture_matchups(1, "FWD", ctx)
        def_matchups = build_fixture_matchups(2, "DEF", ctx)
        # ARS FWD vs SHU weak defence = easy
        # SHU DEF vs ARS strong attack = hard
        assert fwd_matchups[0].opponent_fdr < def_matchups[0].opponent_fdr

    def test_missing_opponent_in_team_map(self):
        """Opponent not in team_map produces opponent_short='???' and FDR fallback."""
        from tests.conftest import make_fixture
        from fpl_cli.services.matchup import build_team_fixture_map

        ctx = self._make_context()
        # Fixture with away_team_id=99 not in team_map
        fixtures = [make_fixture(id=10, gameweek=25, home_team_id=1, away_team_id=99)]
        ctx2 = dataclasses.replace(ctx, team_fixture_map=build_team_fixture_map(fixtures))
        matchups = build_fixture_matchups(1, "FWD", ctx2)
        assert len(matchups) == 1
        assert matchups[0].opponent_short == "???"
        # FDR falls back to 4.0 (unknown opponent)
        assert matchups[0].opponent_fdr == 4.0

    def test_missing_player_team_in_team_map(self):
        """Player team not in team_map produces empty team_short, FDR fallback."""
        from tests.conftest import make_fixture
        from fpl_cli.services.matchup import build_team_fixture_map

        ctx = self._make_context()
        # Team 99 has a fixture but isn't in team_map
        fixtures = [make_fixture(id=10, gameweek=25, home_team_id=99, away_team_id=2)]
        ctx2 = dataclasses.replace(ctx, team_fixture_map=build_team_fixture_map(fixtures))
        matchups = build_fixture_matchups(99, "FWD", ctx2)
        assert len(matchups) == 1
        # Player team unknown -> empty team_short -> FDR fallback
        assert matchups[0].opponent_fdr == 4.0


class TestComputeAggregateMatchup:
    """Tests for compute_aggregate_matchup helper."""

    def _make_context(self, *, with_gw_maps: bool = True):
        from fpl_cli.services.team_ratings import TeamRating, TeamRatingsService
        from tests.conftest import make_fixture, make_team
        from fpl_cli.services.matchup import build_gw_fixture_maps, build_team_fixture_map

        teams = [
            make_team(id=1, name="Arsenal", short_name="ARS"),
            make_team(id=2, name="Sheffield Utd", short_name="SHU"),
        ]

        svc = TeamRatingsService.__new__(TeamRatingsService)
        svc._ratings = {
            "ARS": TeamRating(atk_home=1, atk_away=2, def_home=1, def_away=2),
            "SHU": TeamRating(atk_home=6, atk_away=7, def_home=6, def_away=7),
        }
        svc._loaded = True
        svc._metadata = None

        all_fixtures = [
            make_fixture(id=1, gameweek=25, home_team_id=1, away_team_id=2),
            make_fixture(id=2, gameweek=26, home_team_id=2, away_team_id=1),
            make_fixture(id=3, gameweek=27, home_team_id=1, away_team_id=2),
        ]
        next_gw_fixtures = [f for f in all_fixtures if f.gameweek == 25]

        team_form_by_id = {
            1: {"team_id": 1, "league_position": 1, "pts_6": 15, "gs_6": 12, "gc_6": 2,
                "gs_home": 8, "gc_home": 1, "gs_away": 4, "gc_away": 1,
                "pts_home": 12, "pts_away": 6},
            2: {"team_id": 2, "league_position": 20, "pts_6": 2, "gs_6": 2, "gc_6": 14,
                "gs_home": 1, "gc_home": 8, "gs_away": 1, "gc_away": 6,
                "pts_home": 1, "pts_away": 1},
        }

        gw_fixture_maps = build_gw_fixture_maps(all_fixtures, 25) if with_gw_maps else None

        return ScoringContext(
            team_map={t.id: t for t in teams},
            team_fixture_map=build_team_fixture_map(next_gw_fixtures),
            ratings_service=svc,
            team_form_by_id=team_form_by_id,
            gw_fixture_maps=gw_fixture_maps,
            next_gw_id=25,
        )

    def test_returns_both_values(self):
        """Returns (matchup_avg_3gw, positional_fdr) for team with fixtures."""
        ctx = self._make_context()
        avg_3gw, pos_fdr = compute_aggregate_matchup(1, "FWD", ctx)
        assert avg_3gw is not None
        assert pos_fdr is not None
        assert isinstance(avg_3gw, float)
        assert isinstance(pos_fdr, float)

    def test_no_gw_maps_returns_none_matchup(self):
        """When gw_fixture_maps is None, matchup_avg_3gw is None."""
        ctx = self._make_context(with_gw_maps=False)
        avg_3gw, pos_fdr = compute_aggregate_matchup(1, "FWD", ctx)
        assert avg_3gw is None
        # positional_fdr still works from next-GW fixtures
        assert pos_fdr is not None

    def test_no_fixtures_returns_none_fdr(self):
        """Team with no next-GW fixtures returns None for positional_fdr."""
        ctx = self._make_context()
        avg_3gw, pos_fdr = compute_aggregate_matchup(99, "FWD", ctx)
        # No fixtures for team 99 -> no fdr, no matchup (no entries in gw_maps)
        assert pos_fdr is None

    def test_no_team_form_returns_none_matchup(self):
        """When team_form_by_id is None but gw_fixture_maps present, matchup_avg_3gw is None."""
        ctx = self._make_context()
        # Replace team_form_by_id with None while keeping gw_fixture_maps
        ctx_no_form = dataclasses.replace(ctx, team_form_by_id=None)
        avg_3gw, pos_fdr = compute_aggregate_matchup(1, "FWD", ctx_no_form)
        assert avg_3gw is None
        # positional_fdr still works from next-GW fixtures
        assert pos_fdr is not None

    def test_cache_is_populated(self):
        """matchup_cache is populated and reused on second call."""
        ctx = self._make_context()
        cache: dict[tuple[int, str], float] = {}
        avg1, _ = compute_aggregate_matchup(1, "FWD", ctx, matchup_cache=cache)
        assert (1, "FWD") in cache
        avg2, _ = compute_aggregate_matchup(1, "FWD", ctx, matchup_cache=cache)
        assert avg1 == avg2

    def test_prediction_lookup_passed_through(self):
        """prediction_lookup on context is forwarded to compute_3gw_matchup."""
        ctx = self._make_context()
        # Team 99 has no confirmed fixtures - predictions should affect its score
        predictions = {25: {99: ("double", 0.8)}}
        ctx_with_pred = dataclasses.replace(ctx, prediction_lookup=predictions)

        avg_no_pred, _ = compute_aggregate_matchup(99, "FWD", ctx_with_pred)
        # Team 99 has no fixtures in gw_maps, but with predictions it should
        # get a non-zero matchup (predicted DGW at 10.0 in GW25)
        assert avg_no_pred is not None
        assert avg_no_pred > 0.0

    def test_prediction_lookup_none_preserves_behaviour(self):
        """prediction_lookup=None on context -> same as before."""
        ctx = self._make_context()
        assert ctx.prediction_lookup is None
        avg, fdr = compute_aggregate_matchup(1, "FWD", ctx)
        assert avg is not None
        assert fdr is not None


# ---------------------------------------------------------------------------
# compute_form_trajectory
# ---------------------------------------------------------------------------


class TestComputeFormTrajectory:
    """Tests for compute_form_trajectory()."""

    @staticmethod
    def _gw(round_num: int, total_points: int, minutes: int = 90) -> dict:
        return {"round": round_num, "total_points": total_points, "minutes": minutes}

    def test_rising_trajectory(self):
        history = [self._gw(r, pts) for r, pts in zip(range(20, 27), [2, 4, 6, 8, 10, 12, 14])]
        result = compute_form_trajectory(history, current_gw=26)
        assert result > 1.0

    def test_falling_trajectory(self):
        history = [self._gw(r, pts) for r, pts in zip(range(20, 27), [14, 12, 10, 8, 6, 4, 2])]
        result = compute_form_trajectory(history, current_gw=26)
        assert result < 1.0

    def test_stable_trajectory(self):
        """Flat form -> slope=0 -> exactly 1.0 (neutral)."""
        history = [self._gw(r, 6) for r in range(20, 27)]
        result = compute_form_trajectory(history, current_gw=26)
        assert result == 1.0

    def test_beto_sarr_pattern(self):
        """One-off haul amid low scores -> median filter neutralises it."""
        history = [self._gw(r, pts) for r, pts in zip(range(20, 27), [2, 2, 2, 15, 3, 2, 2])]
        result = compute_form_trajectory(history, current_gw=26)
        assert 0.9 <= result <= 1.0

    def test_exactly_4_qualifying_gws(self):
        history = [self._gw(r, pts) for r, pts in zip(range(23, 27), [3, 5, 7, 9])]
        result = compute_form_trajectory(history, current_gw=26)
        assert isinstance(result, float)
        assert 0.8 <= result <= 1.2

    def test_3_gws_returns_neutral(self):
        history = [self._gw(r, pts) for r, pts in zip(range(24, 27), [3, 5, 7])]
        assert compute_form_trajectory(history, current_gw=26) == 1.0

    def test_empty_history(self):
        assert compute_form_trajectory([], current_gw=26) == 1.0

    def test_all_zero_minutes(self):
        history = [self._gw(r, 5, minutes=0) for r in range(20, 27)]
        assert compute_form_trajectory(history, current_gw=26) == 1.0

    def test_12_gw_lookback_cap(self):
        """Only GWs within 12 of current_gw qualify (round > current_gw - 12)."""
        # GWs 10-26, current_gw=26: cutoff=14, so only rounds 15-26 qualify
        history = [self._gw(r, 5) for r in range(10, 27)]
        result = compute_form_trajectory(history, current_gw=26)
        assert result == 1.0  # all same points -> slope=0 -> neutral
        # round 14 excluded (14 > 14 is False)
        history_boundary = [self._gw(14, 100)] + [self._gw(r, 5) for r in range(15, 22)]
        assert compute_form_trajectory(history_boundary, current_gw=26) == 1.0

    def test_clamping_steep_positive(self):
        """Extremely steep upward slope still clamped to 1.2."""
        history = [self._gw(r, pts) for r, pts in zip(range(20, 27), [0, 5, 10, 20, 30, 35, 40])]
        result = compute_form_trajectory(history, current_gw=26)
        assert result == 1.2

    def test_early_season_returns_neutral(self):
        history = [self._gw(r, 5) for r in range(1, 4)]
        assert compute_form_trajectory(history, current_gw=3) == 1.0

    def test_tie_removal_prefers_central_position(self):
        """When min/max has ties, remove the instance closest to centre.

        Welbeck pattern: [1, 2, 9, 9, 1, 2, 12] - clearly trending up.
        Max=12 at pos 6 (edge), min=1 at pos 0 and 4.
        Central removal drops min at pos 4 (closer to centre=3),
        keeping the early 1 which anchors the rising slope.
        """
        history = [self._gw(r, pts) for r, pts in zip(range(20, 27), [1, 2, 9, 9, 1, 2, 12])]
        result = compute_form_trajectory(history, current_gw=26)
        # After central removal: [1, 2, 9, 9, 2] -> rising slope -> multiplier > 1.0
        assert result > 1.0


# ---------------------------------------------------------------------------
# form_trajectory in scoring functions
# ---------------------------------------------------------------------------


class TestTrajectoryInScoring:
    """Verify form_trajectory multiplier affects scoring in both families."""

    @staticmethod
    def _mid_matchup():
        return FixtureMatchup(
            opponent_short="SHU", is_home=True, opponent_fdr=2.5,
            matchup_score=7.0,
            matchup_breakdown={"matchup_score": 7.0, "reasoning": ["Test"]},
        )

    def test_captain_trajectory_rising(self):
        """Rising trajectory increases captain score vs neutral."""
        player = make_player(
            id=1, web_name="Rising", team_id=1,
            position=PlayerPosition.MIDFIELDER,
            form=6.0, points_per_game=5.0, minutes=1500, total_points=100,
        )
        fm = [self._mid_matchup()]

        eval_neutral, id_n = build_player_evaluation(
            player, enrichment={"team_short": "ARS"},
            fixture_matchups=fm,
        )
        eval_rising, id_r = build_player_evaluation(
            player, enrichment={"team_short": "ARS", "form_trajectory": 1.2},
            fixture_matchups=fm,
        )
        neutral = calculate_captain_score(eval_neutral, id_n, next_gw_id=20)
        rising = calculate_captain_score(eval_rising, id_r, next_gw_id=20)
        assert neutral is not None and rising is not None
        assert rising["captain_score_raw"] > neutral["captain_score_raw"]

    def test_captain_trajectory_falling(self):
        """Falling trajectory decreases captain score vs neutral."""
        player = make_player(
            id=1, web_name="Falling", team_id=1,
            position=PlayerPosition.MIDFIELDER,
            form=6.0, points_per_game=5.0, minutes=1500, total_points=100,
        )
        fm = [self._mid_matchup()]

        eval_neutral, id_n = build_player_evaluation(
            player, enrichment={"team_short": "ARS"},
            fixture_matchups=fm,
        )
        eval_falling, id_f = build_player_evaluation(
            player, enrichment={"team_short": "ARS", "form_trajectory": 0.8},
            fixture_matchups=fm,
        )
        neutral = calculate_captain_score(eval_neutral, id_n, next_gw_id=20)
        falling = calculate_captain_score(eval_falling, id_f, next_gw_id=20)
        assert neutral is not None and falling is not None
        assert falling["captain_score_raw"] < neutral["captain_score_raw"]

    def test_target_trajectory_increases(self):
        """Rising trajectory increases target score."""
        eval_neutral, _ = build_player_evaluation(
            {"position": "MID", "form": 5.0, "ppg": 4.0, "xGI_per_90": 0.5,
             "minutes": 1500, "appearances": 20, "GI_minus_xGI": 0.0},
            matchup_avg_3gw=6.0, positional_fdr=3.0,
        )
        eval_rising, _ = build_player_evaluation(
            {"position": "MID", "form": 5.0, "ppg": 4.0, "xGI_per_90": 0.5,
             "minutes": 1500, "appearances": 20, "GI_minus_xGI": 0.0,
             "form_trajectory": 1.15},
            matchup_avg_3gw=6.0, positional_fdr=3.0,
        )
        neutral = calculate_target_score(eval_neutral, next_gw_id=20)
        rising = calculate_target_score(eval_rising, next_gw_id=20)
        assert rising >= neutral

    def test_neutral_trajectory_matches_baseline(self):
        """form_trajectory=1.0 produces same score as no trajectory."""
        eval_, _ = build_player_evaluation(
            {"position": "MID", "form": 5.0, "ppg": 4.0, "xGI_per_90": 0.5,
             "minutes": 1500, "appearances": 20, "GI_minus_xGI": 0.0,
             "form_trajectory": 1.0},
            matchup_avg_3gw=6.0, positional_fdr=3.0,
        )
        eval_default, _ = build_player_evaluation(
            {"position": "MID", "form": 5.0, "ppg": 4.0, "xGI_per_90": 0.5,
             "minutes": 1500, "appearances": 20, "GI_minus_xGI": 0.0},
            matchup_avg_3gw=6.0, positional_fdr=3.0,
        )
        assert calculate_target_score(eval_, next_gw_id=20) == calculate_target_score(eval_default, next_gw_id=20)

    def test_differential_trajectory_increases(self):
        """Rising trajectory increases differential score."""
        eval_neutral, _ = build_player_evaluation(
            {"position": "MID", "form": 5.0, "ppg": 4.0, "xGI_per_90": 0.5,
             "minutes": 1500, "appearances": 20, "GI_minus_xGI": 0.0,
             "selected_by_percent": 3.0},
            matchup_avg_3gw=6.0, positional_fdr=3.0,
        )
        eval_rising, _ = build_player_evaluation(
            {"position": "MID", "form": 5.0, "ppg": 4.0, "xGI_per_90": 0.5,
             "minutes": 1500, "appearances": 20, "GI_minus_xGI": 0.0,
             "selected_by_percent": 3.0, "form_trajectory": 1.15},
            matchup_avg_3gw=6.0, positional_fdr=3.0,
        )
        neutral = calculate_differential_score(eval_neutral, semi_differential_threshold=10, next_gw_id=20)
        rising = calculate_differential_score(eval_rising, semi_differential_threshold=10, next_gw_id=20)
        assert rising >= neutral

    def test_waiver_trajectory_decreases(self):
        """Falling trajectory decreases waiver score."""
        eval_neutral, _ = build_player_evaluation(
            {"position": "MID", "form": 5.0, "ppg": 4.0, "xGI_per_90": 0.5,
             "minutes": 1500, "appearances": 20, "GI_minus_xGI": 0.0},
            matchup_avg_3gw=6.0, positional_fdr=3.0,
        )
        eval_falling, _ = build_player_evaluation(
            {"position": "MID", "form": 5.0, "ppg": 4.0, "xGI_per_90": 0.5,
             "minutes": 1500, "appearances": 20, "GI_minus_xGI": 0.0,
             "form_trajectory": 0.85},
            matchup_avg_3gw=6.0, positional_fdr=3.0,
        )
        neutral = calculate_waiver_score(
            eval_neutral, squad_by_position={"MID": []}, next_gw_id=20,
        )
        falling = calculate_waiver_score(
            eval_falling, squad_by_position={"MID": []}, next_gw_id=20,
        )
        assert falling <= neutral

    def test_bench_trajectory_rising(self):
        """Rising trajectory increases bench priority score."""
        player = make_player(
            id=1, web_name="Rising", team_id=1,
            position=PlayerPosition.MIDFIELDER,
            form=6.0, points_per_game=5.0, minutes=1500, total_points=100,
        )
        fm = [self._mid_matchup()]

        eval_neutral, id_n = build_player_evaluation(
            player, enrichment={"team_short": "ARS"},
            fixture_matchups=fm,
        )
        eval_rising, id_r = build_player_evaluation(
            player, enrichment={"team_short": "ARS", "form_trajectory": 1.2},
            fixture_matchups=fm,
        )
        neutral = calculate_bench_score(eval_neutral, id_n, availability_risks=[], next_gw_id=20)
        rising = calculate_bench_score(eval_rising, id_r, availability_risks=[], next_gw_id=20)
        assert rising["priority_score_raw"] > neutral["priority_score_raw"]


# ---------------------------------------------------------------------------
# prepare_scoring_data history fetch
# ---------------------------------------------------------------------------


class TestPrepareHistoryFetch:
    """Tests for include_history in prepare_scoring_data."""

    @staticmethod
    def _make_client():
        from unittest.mock import AsyncMock

        from tests.conftest import make_fixture, make_team

        client = AsyncMock()
        client.get_teams.return_value = [
            make_team(id=1, short_name="ARS"),
            make_team(id=2, short_name="SHU"),
        ]
        client.get_fixtures.return_value = [
            make_fixture(gameweek=25, home_team_id=1, away_team_id=2),
        ]
        client.get_next_gameweek.return_value = {"id": 25}
        return client

    async def test_no_flags_returns_none(self):
        """Without include_history, player_histories is None."""
        client = self._make_client()
        data = await prepare_scoring_data(client)
        assert data.player_histories is None

    async def test_include_history_populates(self):
        """include_history=True populates player_histories dict."""
        client = self._make_client()
        client.get_players.return_value = [
            make_player(id=10, web_name="Saka", team_id=1, minutes=900),
        ]
        client.get_player_detail.return_value = {
            "history": [{"round": 24, "total_points": 8}],
        }
        data = await prepare_scoring_data(client, include_history=True)
        assert data.player_histories is not None
        assert 10 in data.player_histories
        assert data.player_histories[10] == [{"round": 24, "total_points": 8}]

    async def test_zero_minutes_excluded(self):
        """Players with 0 minutes excluded; players with 45 minutes included."""
        client = self._make_client()
        client.get_players.return_value = [
            make_player(id=10, web_name="Bench", team_id=1, minutes=0),
            make_player(id=11, web_name="Sub", team_id=1, minutes=45),
        ]
        client.get_player_detail.return_value = {"history": [{"round": 1}]}
        data = await prepare_scoring_data(client, include_history=True)
        assert data.player_histories is not None
        assert 10 not in data.player_histories
        assert 11 in data.player_histories

    async def test_failed_detail_skipped(self):
        """A failed get_player_detail for one player doesn't break others."""
        from unittest.mock import AsyncMock

        client = self._make_client()
        client.get_players.return_value = [
            make_player(id=10, web_name="OK", team_id=1, minutes=900),
            make_player(id=11, web_name="Fail", team_id=1, minutes=800),
        ]

        async def side_effect(pid: int) -> dict:
            if pid == 11:
                raise ConnectionError("API error")
            return {"history": [{"round": 24}]}

        client.get_player_detail = AsyncMock(side_effect=side_effect)
        data = await prepare_scoring_data(client, include_history=True)
        assert data.player_histories is not None
        assert 10 in data.player_histories
        assert 11 not in data.player_histories

    async def test_include_players_and_history_single_fetch(self):
        """When both flags are True, get_players called once."""
        client = self._make_client()
        players = [make_player(id=10, web_name="Saka", team_id=1, minutes=900)]
        client.get_players.return_value = players
        client.get_player_detail.return_value = {"history": []}
        data = await prepare_scoring_data(
            client, include_players=True, include_history=True,
        )
        assert data.players == players
        assert data.player_histories is not None
        client.get_players.assert_awaited_once()

    async def test_include_prior_requires_include_players(self):
        """include_prior=True without include_players=True raises ValueError."""
        import pytest as _pytest

        client = self._make_client()
        with _pytest.raises(ValueError, match="include_prior requires include_players"):
            await prepare_scoring_data(client, include_prior=True)

    async def test_include_prior_populates_player_priors(self):
        """include_prior=True populates ScoringData.player_priors."""
        from unittest.mock import patch
        from fpl_cli.services.player_prior import PlayerPrior

        client = self._make_client()
        players = [make_player(id=10, code=100, web_name="Saka", team_id=1)]
        client.get_players.return_value = players

        fake_priors = {10: PlayerPrior(prior_strength=0.7, confidence=0.5, source="history")}
        with patch("fpl_cli.services.player_prior.load_cached_priors", return_value=fake_priors):
            data = await prepare_scoring_data(
                client, include_players=True, include_prior=True,
            )
        assert data.player_priors is not None
        assert data.player_priors[10].confidence == 0.5

    async def test_include_prior_false_leaves_none(self):
        """Default include_prior=False leaves player_priors as None."""
        client = self._make_client()
        data = await prepare_scoring_data(client)
        assert data.player_priors is None


# ---------------------------------------------------------------------------
# shrink_scores
# ---------------------------------------------------------------------------

from fpl_cli.services.player_prior import PlayerPrior
from fpl_cli.services.player_scoring import shrink_scores


class TestShrinkScores:
    """Tests for confidence-weighted shrinkage toward position means."""

    def test_equal_confidence_shrinks_toward_mean(self):
        """With equal confidence < 1, all scores move toward position mean."""
        prior_map = {
            1: PlayerPrior(0.5, 0.5, "history"),
            2: PlayerPrior(0.5, 0.5, "history"),
            3: PlayerPrior(0.5, 0.5, "history"),
        }
        scores = [(1, 80.0, "MID"), (2, 60.0, "MID"), (3, 40.0, "MID")]
        result = shrink_scores(scores, prior_map, current_gw=3, cutoff_gw=10)

        ids_scores = {pid: s for pid, s, _ in result}
        # Mean is ~60 (confidence-weighted, all equal -> simple mean)
        assert ids_scores[1] < 80.0  # shrunk down
        assert ids_scores[3] > 40.0  # shrunk up
        assert ids_scores[2] == pytest.approx(60.0)  # at mean, unchanged

    def test_confidence_1_is_identity(self):
        """confidence=1.0 for all players -> scores unchanged."""
        prior_map = {
            1: PlayerPrior(1.0, 1.0, "history"),
            2: PlayerPrior(1.0, 1.0, "history"),
        }
        scores = [(1, 80.0, "MID"), (2, 40.0, "DEF")]
        result = shrink_scores(scores, prior_map, current_gw=3, cutoff_gw=10)
        assert result == scores

    def test_confidence_0_fully_shrinks_to_mean(self):
        """confidence=0.0 -> score becomes position mean."""
        prior_map = {
            1: PlayerPrior(0.0, 1.0, "history"),
            2: PlayerPrior(0.0, 0.5, "history"),
            3: PlayerPrior(0.0, 0.0, "price"),
        }
        scores = [(1, 80.0, "MID"), (2, 60.0, "MID"), (3, 40.0, "MID")]
        result = shrink_scores(scores, prior_map, current_gw=3, cutoff_gw=10)

        # Player 3 has conf=0.0, so fully shrunk to mean
        ids_scores = {pid: s for pid, s, _ in result}
        # All three contribute to the weighted mean; player 3 (conf=0) contributes 0
        # mean = (1.0*80 + 0.5*60) / (1.0 + 0.5) ≈ 73.33 (player 3 excluded from mean)
        assert ids_scores[3] == pytest.approx((1.0 * 80 + 0.5 * 60) / 1.5, abs=0.1)

    def test_at_cutoff_returns_unmodified(self):
        prior_map = {1: PlayerPrior(0.5, 0.5, "history")}
        scores = [(1, 80.0, "MID")]
        result = shrink_scores(scores, prior_map, current_gw=10, cutoff_gw=10)
        assert result == scores

    def test_beyond_cutoff_returns_unmodified(self):
        prior_map = {1: PlayerPrior(0.5, 0.5, "history")}
        scores = [(1, 80.0, "MID")]
        result = shrink_scores(scores, prior_map, current_gw=15, cutoff_gw=10)
        assert result == scores

    def test_none_prior_map_returns_unmodified(self):
        scores = [(1, 80.0, "MID")]
        result = shrink_scores(scores, None, current_gw=3, cutoff_gw=10)
        assert result == scores

    def test_empty_scores_returns_empty(self):
        prior_map = {1: PlayerPrior(0.5, 0.5, "history")}
        result = shrink_scores([], prior_map, current_gw=3, cutoff_gw=10)
        assert result == []

    def test_player_not_in_prior_map_gets_no_shrinkage(self):
        """Players missing from prior_map default to confidence=1.0."""
        prior_map = {1: PlayerPrior(0.5, 0.5, "history")}
        scores = [(1, 80.0, "MID"), (99, 40.0, "MID")]
        result = shrink_scores(scores, prior_map, current_gw=3, cutoff_gw=10)

        ids_scores = {pid: s for pid, s, _ in result}
        # Player 99 has conf=1.0, so: mean + 1.0 * (40 - mean) = 40
        assert ids_scores[99] == pytest.approx(40.0)

    def test_single_player_in_position(self):
        """Single player in a position: mean equals their score, no change."""
        prior_map = {1: PlayerPrior(0.0, 0.3, "price")}
        scores = [(1, 75.0, "GK")]
        result = shrink_scores(scores, prior_map, current_gw=3, cutoff_gw=10)
        assert result[0][1] == pytest.approx(75.0)

    def test_mixed_positions_independent_means(self):
        """Each position gets its own mean."""
        prior_map = {
            1: PlayerPrior(0.5, 0.5, "history"),
            2: PlayerPrior(0.5, 0.5, "history"),
            3: PlayerPrior(0.5, 0.5, "history"),
            4: PlayerPrior(0.5, 0.5, "history"),
        }
        scores = [
            (1, 80.0, "MID"), (2, 40.0, "MID"),
            (3, 90.0, "DEF"), (4, 30.0, "DEF"),
        ]
        result = shrink_scores(scores, prior_map, current_gw=3, cutoff_gw=10)

        ids_scores = {pid: s for pid, s, _ in result}
        # MID mean = 60, DEF mean = 60 (equal conf -> simple mean)
        assert ids_scores[1] == pytest.approx(70.0)  # 60 + 0.5*(80-60)
        assert ids_scores[3] == pytest.approx(75.0)  # 60 + 0.5*(90-60)

    def test_compression_property(self):
        """Shrinkage compresses the score range toward the mean."""
        prior_map = {
            1: PlayerPrior(0.5, 0.5, "history"),
            2: PlayerPrior(0.5, 0.5, "history"),
        }
        scores = [(1, 90.0, "FWD"), (2, 30.0, "FWD")]
        result = shrink_scores(scores, prior_map, current_gw=3, cutoff_gw=10)

        original_range = 90.0 - 30.0
        shrunk_range = result[0][1] - result[1][1]
        assert shrunk_range < original_range


class TestCalculateLineupScore:
    """Tests for starting XI lineup scoring."""

    def _fm(self, fdr=2.5, matchup_score=7.0, is_home=True):
        return FixtureMatchup(
            opponent_short="SHU", is_home=is_home, opponent_fdr=fdr, matchup_score=matchup_score,
        )

    def test_available_mid_with_good_fixtures(self):
        player = make_player(
            id=1, web_name="Saka", team_id=1,
            position=PlayerPosition.MIDFIELDER,
            form=6.0, points_per_game=6.0, minutes=1500, total_points=120,
        )
        ev, ident = build_player_evaluation(
            player, enrichment={"team_short": "ARS"},
            fixture_matchups=[self._fm()],
        )
        result = calculate_lineup_score(ev, ident, next_gw_id=20)
        assert 0 < result["lineup_score"] <= 100
        assert result["lineup_score_raw"] > 0
        assert result["excluded"] is False
        assert result["exclusion_reason"] is None
        assert result["position"] == "MID"

    def test_dgw_scores_higher_than_sgw(self):
        player = make_player(
            id=1, web_name="Saka", team_id=1,
            position=PlayerPosition.MIDFIELDER,
            form=5.0, points_per_game=5.0, minutes=1200, total_points=80,
        )
        ev_sgw, ident_sgw = build_player_evaluation(
            player, enrichment={"team_short": "ARS"},
            fixture_matchups=[self._fm()],
        )
        ev_dgw, ident_dgw = build_player_evaluation(
            player, enrichment={"team_short": "ARS"},
            fixture_matchups=[self._fm(), self._fm(fdr=3.0, matchup_score=6.0)],
        )
        sgw = calculate_lineup_score(ev_sgw, ident_sgw, next_gw_id=20)
        dgw = calculate_lineup_score(ev_dgw, ident_dgw, next_gw_id=20)
        assert dgw["lineup_score_raw"] > sgw["lineup_score_raw"]

    def test_excluded_below_50_chance(self):
        player = make_player(
            id=2, web_name="Injured", team_id=1,
            position=PlayerPosition.DEFENDER,
            form=5.0, points_per_game=5.0, minutes=1000, total_points=60,
            status=PlayerStatus.DOUBTFUL, chance_of_playing_next_round=40,
        )
        ev, ident = build_player_evaluation(
            player, enrichment={"team_short": "ARS"},
            fixture_matchups=[self._fm()],
        )
        result = calculate_lineup_score(ev, ident, next_gw_id=20)
        assert result["excluded"] is True
        assert "Excluded (40% chance)" in result["reasons"]
        assert result["lineup_score_raw"] > 0  # score still computed

    def test_none_chance_of_playing_no_penalty(self):
        player = make_player(
            id=3, web_name="Fit", team_id=1,
            position=PlayerPosition.FORWARD,
            form=5.0, points_per_game=5.0, minutes=1200, total_points=70,
        )
        ev, ident = build_player_evaluation(
            player, enrichment={"team_short": "ARS"},
            fixture_matchups=[self._fm()],
        )
        result = calculate_lineup_score(ev, ident, next_gw_id=20)
        assert result["excluded"] is False
        assert result["reasons"] == ["Available"]

    def test_75_chance_gets_minus_1_not_minus_3(self):
        player = make_player(
            id=4, web_name="Minor", team_id=1,
            position=PlayerPosition.MIDFIELDER,
            form=5.0, points_per_game=5.0, minutes=1200, total_points=70,
            status=PlayerStatus.DOUBTFUL, chance_of_playing_next_round=75,
        )
        ev_75, ident_75 = build_player_evaluation(
            player, enrichment={"team_short": "ARS"},
            fixture_matchups=[self._fm()],
        )
        result_75 = calculate_lineup_score(ev_75, ident_75, next_gw_id=20)
        assert "Minor doubt (75%)" in result_75["reasons"]
        assert result_75["excluded"] is False

        # Compare with a 60% player who gets -3
        player_60 = make_player(
            id=5, web_name="Doubt", team_id=1,
            position=PlayerPosition.MIDFIELDER,
            form=5.0, points_per_game=5.0, minutes=1200, total_points=70,
            status=PlayerStatus.DOUBTFUL, chance_of_playing_next_round=60,
        )
        ev_60, ident_60 = build_player_evaluation(
            player_60, enrichment={"team_short": "ARS"},
            fixture_matchups=[self._fm()],
        )
        result_60 = calculate_lineup_score(ev_60, ident_60, next_gw_id=20)
        assert "Availability doubt (60%)" in result_60["reasons"]
        # -3 penalty vs -1 penalty: 60% player scores lower
        assert result_60["lineup_score_raw"] < result_75["lineup_score_raw"]

    def test_bgw_no_fixtures_scores_zero(self):
        player = make_player(
            id=6, web_name="BGW", team_id=1,
            position=PlayerPosition.MIDFIELDER,
            form=6.0, points_per_game=6.0, minutes=1500, total_points=120,
        )
        ev, ident = build_player_evaluation(
            player, enrichment={"team_short": "ARS"},
            fixture_matchups=[],
        )
        result = calculate_lineup_score(ev, ident, next_gw_id=20)
        assert result["lineup_score_raw"] == 0.0
        assert result["excluded"] is False

    def test_gk_no_xgi_contribution(self):
        gk = make_player(
            id=7, web_name="Raya", team_id=1,
            position=PlayerPosition.GOALKEEPER,
            form=5.0, points_per_game=5.0, minutes=1500, total_points=90,
        )
        mid = make_player(
            id=8, web_name="Saka", team_id=1,
            position=PlayerPosition.MIDFIELDER,
            form=5.0, points_per_game=5.0, minutes=1500, total_points=90,
        )
        fm = [self._fm()]
        ev_gk, id_gk = build_player_evaluation(gk, enrichment={"team_short": "ARS"}, fixture_matchups=fm)
        ev_mid, id_mid = build_player_evaluation(mid, enrichment={"team_short": "ARS"}, fixture_matchups=fm)
        r_gk = calculate_lineup_score(ev_gk, id_gk, next_gw_id=20)
        r_mid = calculate_lineup_score(ev_mid, id_mid, next_gw_id=20)
        # GK gets position multiplier 0.7 and no xGI -> lower score
        assert r_gk["lineup_score_raw"] < r_mid["lineup_score_raw"]

    def test_zero_appearances_scores_zero(self):
        player = make_player(
            id=9, web_name="NewSign", team_id=1,
            position=PlayerPosition.FORWARD,
            form=0.0, points_per_game=0.0, minutes=0, total_points=0,
        )
        ev, ident = build_player_evaluation(
            player, enrichment={"team_short": "ARS"},
            fixture_matchups=[self._fm()],
        )
        result = calculate_lineup_score(ev, ident, next_gw_id=20)
        assert result["lineup_score_raw"] == 1.0  # only home bonus

    def test_different_raw_scores_vs_bench(self):
        """Same evaluation produces different raw scores for lineup vs bench."""
        player = make_player(
            id=10, web_name="Test", team_id=1,
            position=PlayerPosition.MIDFIELDER,
            form=5.0, points_per_game=5.0, minutes=1200, total_points=80,
            status=PlayerStatus.DOUBTFUL, chance_of_playing_next_round=60,
        )
        ev, ident = build_player_evaluation(
            player, enrichment={"team_short": "ARS"},
            fixture_matchups=[self._fm()],
        )
        lineup = calculate_lineup_score(ev, ident, next_gw_id=20)
        bench = calculate_bench_score(
            ev, ident, availability_risks=[], next_gw_id=20,
        )
        # Same core but different availability adjustment patterns
        assert lineup["lineup_score_raw"] != bench["priority_score_raw"]


class TestSelectStartingXI:
    """Tests for formation optimiser."""

    @staticmethod
    def _scored(pid, name, pos, team, raw, excluded=False):
        """Minimal scored player dict matching calculate_lineup_score() output."""
        return {
            "id": pid, "name": name, "position": pos, "team": team,
            "lineup_score": round(raw / 31.0 * 100), "lineup_score_raw": raw,
            "excluded": excluded, "exclusion_reason": None,
            "positional_fdr": None, "price": 6.0, "form": 5.0, "ppg": 5.0,
            "reasons": ["Available"],
        }

    def _squad_15(self):
        """15 players: 2 GK, 5 DEF, 5 MID, 3 FWD with clear ranking."""
        return [
            self._scored(1, "GK1", "GK", "ARS", 10.0),
            self._scored(2, "GK2", "GK", "BUR", 5.0),
            self._scored(3, "DEF1", "DEF", "ARS", 15.0),
            self._scored(4, "DEF2", "DEF", "CHE", 14.0),
            self._scored(5, "DEF3", "DEF", "LIV", 13.0),
            self._scored(6, "DEF4", "DEF", "TOT", 8.0),
            self._scored(7, "DEF5", "DEF", "WHU", 7.0),
            self._scored(8, "MID1", "MID", "ARS", 20.0),
            self._scored(9, "MID2", "MID", "LIV", 18.0),
            self._scored(10, "MID3", "MID", "CHE", 16.0),
            self._scored(11, "MID4", "MID", "TOT", 12.0),
            self._scored(12, "MID5", "MID", "WHU", 9.0),
            self._scored(13, "FWD1", "FWD", "LIV", 19.0),
            self._scored(14, "FWD2", "FWD", "ARS", 17.0),
            self._scored(15, "FWD3", "FWD", "CHE", 11.0),
        ]

    def test_valid_formation_and_xi_count(self):
        result = select_starting_xi(self._squad_15())
        assert len(result["starting_xi"]) == 11
        assert len(result["bench"]) == 4
        formation_parts = result["formation"].split("-")
        assert len(formation_parts) == 3
        d, m, f = (int(x) for x in formation_parts)
        assert (d, m, f) in VALID_FORMATIONS

    def test_picks_343_when_fwds_outscore_extra_def(self):
        result = select_starting_xi(self._squad_15())
        # FWD1(19)+FWD2(17)+FWD3(11)=47 vs DEF4(8)+DEF5(7)=15
        # 3-4-3 should win: 3 DEF + 4 MID + 3 FWD
        assert result["formation"] == "3-4-3"

    def test_picks_532_when_defs_outscore(self):
        squad = self._squad_15()
        # Boost DEFs, nerf FWDs and MIDs so 5 DEF preferred over extra MID/FWD
        for p in squad:
            if p["position"] == "DEF":
                p["lineup_score_raw"] = 20.0
            if p["position"] == "FWD":
                p["lineup_score_raw"] = 3.0
            if p["position"] == "MID":
                p["lineup_score_raw"] = 2.0
        result = select_starting_xi(squad)
        assert result["formation"] == "5-3-2"

    def test_tiebreak_prefers_fewer_def(self):
        squad = self._squad_15()
        # Make all outfield players score equally
        for p in squad:
            if p["position"] != "GK":
                p["lineup_score_raw"] = 10.0
        result = select_starting_xi(squad)
        # When all equal, 3-4-3 comes first in VALID_FORMATIONS (most attacking)
        assert result["formation"] == "3-4-3"

    def test_excluded_player_placed_on_bench(self):
        squad = self._squad_15()
        # Make top MID excluded - should be benched despite high score
        squad[8]["excluded"] = True  # MID2 (raw=18)
        squad[8]["lineup_score_raw"] = 25.0  # Even higher than MID1
        result = select_starting_xi(squad)
        bench_ids = {p["id"] for p in result["bench"]}
        assert 9 in bench_ids

    def test_bgw_multiple_zero_score_players(self):
        squad = self._squad_15()
        # Give several players 0 score (BGW)
        for p in squad:
            if p["id"] in (11, 12, 15):
                p["lineup_score_raw"] = 0.0
        result = select_starting_xi(squad)
        assert len(result["starting_xi"]) == 11
        formation_parts = result["formation"].split("-")
        d, m, f = (int(x) for x in formation_parts)
        assert (d, m, f) in VALID_FORMATIONS

    def test_all_one_team_heavy_exposure_penalty(self):
        squad = self._squad_15()
        for p in squad:
            p["team"] = "ARS"
        team_fixtures = {"ARS": {"atk_fdr": 5.0, "def_fdr": 5.0}}
        result = select_starting_xi(squad, team_fixtures=team_fixtures)
        assert len(result["team_exposure_penalties"]) > 0
        assert result["total_score"] < sum(
            p["lineup_score_raw"] for p in result["starting_xi"]
        )

    def test_exposure_penalty_flips_formation(self):
        squad = self._squad_15()
        # Make 3 FWDs from same team facing tough FDR
        for p in squad:
            if p["position"] == "FWD":
                p["team"] = "LIV"
                p["lineup_score_raw"] = 16.0  # Still decent
        team_fixtures = {"LIV": {"atk_fdr": 5.0, "def_fdr": 2.0}}
        result_with = select_starting_xi(squad, team_fixtures=team_fixtures)
        result_without = select_starting_xi(squad)
        # Without penalty, 3 FWDs likely. With penalty, fewer FWDs preferred.
        fwd_count_with = sum(1 for p in result_with["starting_xi"] if p["position"] == "FWD")
        fwd_count_without = sum(1 for p in result_without["starting_xi"] if p["position"] == "FWD")
        assert fwd_count_with <= fwd_count_without

    def test_deterministic_same_input_same_output(self):
        squad = self._squad_15()
        r1 = select_starting_xi(squad)
        r2 = select_starting_xi(squad)
        assert r1["formation"] == r2["formation"]
        assert r1["total_score"] == r2["total_score"]
        assert [p["id"] for p in r1["starting_xi"]] == [p["id"] for p in r2["starting_xi"]]

    def test_integration_with_calculate_lineup_score_output(self):
        """Verify scored_players from calculate_lineup_score() work as input."""
        fm = FixtureMatchup(
            opponent_short="SHU", is_home=True, opponent_fdr=2.5, matchup_score=7.0,
        )
        scored = []
        positions = (
            [PlayerPosition.GOALKEEPER] * 2
            + [PlayerPosition.DEFENDER] * 5
            + [PlayerPosition.MIDFIELDER] * 5
            + [PlayerPosition.FORWARD] * 3
        )
        for i, pos in enumerate(positions):
            p = make_player(
                id=i + 1, web_name=f"P{i+1}", team_id=(i % 5) + 1,
                position=pos, form=float(4 + i % 3),
                points_per_game=float(3 + i % 4), minutes=900 + i * 50,
                total_points=50 + i * 5,
            )
            ev, ident = build_player_evaluation(
                p, enrichment={"team_short": f"T{(i % 5) + 1}"},
                fixture_matchups=[fm],
            )
            scored.append(calculate_lineup_score(ev, ident, next_gw_id=20))
        result = select_starting_xi(scored)
        assert len(result["starting_xi"]) == 11
        assert len(result["bench"]) == 4
