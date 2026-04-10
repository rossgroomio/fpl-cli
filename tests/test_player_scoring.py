"""Tests for centralised player scoring engine."""

import dataclasses

import pytest

from fpl_cli.api.core_insights import MatchRecord
from fpl_cli.models.player import PlayerPosition, PlayerStatus
from fpl_cli.services.player_prior import PlayerPrior
from fpl_cli.services.player_scoring import (
    ATTACKING_POSITIONS,
    DIFFERENTIAL_QUALITY_WEIGHTS,
    GK_VALUE_CEILING,
    NEUTRAL_SIGNALS,
    TARGET_CEILING,
    TARGET_QUALITY_WEIGHTS,
    VALID_FORMATIONS,
    VALUE_CEILING,
    VALUE_QUALITY_WEIGHTS,
    WAIVER_QUALITY_WEIGHTS,
    ConsistencySignals,
    FixtureMatchup,
    PlayerEvaluation,
    PlayerIdentity,
    ScoringContext,
    ScoringData,
    StatWeight,
    _assign_percentile_ranks,
    _consistency_phase,
    _matchup_bonus,
    apply_adjusted_npxg,
    apply_consistency,
    build_adjusted_npxg_lookup,
    build_consistency_lookup,
    build_fixture_matchups,
    build_player_evaluation,
    build_scoring_context,
    calculate_bench_score,
    calculate_captain_score,
    calculate_differential_score,
    calculate_lineup_score,
    calculate_mins_factor,
    calculate_player_quality_score,
    calculate_target_score,
    calculate_waiver_score,
    compute_adjusted_npxg,
    compute_aggregate_matchup,
    compute_blank_rate,
    compute_cv_xgi,
    compute_cv_xgi_fallback,
    compute_floor_xgi,
    compute_floor_xgi_fallback,
    compute_form_trajectory,
    compute_gk_consistency,
    compute_involvement_rate,
    compute_xgi_sustainability,
    normalise_score,
    prepare_scoring_data,
    select_starting_xi,
    shrink_scores,
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
        assert calculate_target_score(eval_, next_gw_id=20) == 52

    def test_target_def(self):
        eval_, _ = self._build_def()
        # DEF target ceiling = DEF_TARGET_CEILING (empirical, from without_xgi caps × 0.85 + matchup).
        assert calculate_target_score(eval_, next_gw_id=20) == 66

    # --- Differential ---

    def test_differential_mid(self):
        eval_, _ = self._build_mid()
        assert calculate_differential_score(
            eval_, semi_differential_threshold=20.0, next_gw_id=20,
        ) == 56

    def test_differential_def(self):
        eval_, _ = self._build_def()
        assert calculate_differential_score(
            eval_, semi_differential_threshold=20.0, next_gw_id=20,
        ) == 64

    # --- Waiver ---

    def test_waiver_mid(self):
        eval_, _ = self._build_mid()
        squad = {"MID": [{"form": 4.0}, {"form": 3.0}], "DEF": [{"form": 5.0}, {"form": 4.0}]}
        assert calculate_waiver_score(
            eval_, squad_by_position=squad, next_gw_id=20,
        ) == 47

    def test_waiver_def(self):
        eval_, _ = self._build_def()
        squad = {"MID": [{"form": 4.0}, {"form": 3.0}], "DEF": [{"form": 5.0}, {"form": 4.0}]}
        assert calculate_waiver_score(
            eval_, squad_by_position=squad, next_gw_id=20,
        ) == 51

    # --- Captain ---

    def test_captain_mid(self):
        eval_, identity = self._build_mid()
        result = calculate_captain_score(eval_, identity, next_gw_id=20)
        assert result is not None
        assert result["captain_score"] == 72
        assert result["captain_score_raw"] == 24.58
        assert result["pen_bonus"] == 1.12

    def test_captain_def(self):
        eval_, identity = self._build_def()
        result = calculate_captain_score(eval_, identity, next_gw_id=20)
        assert result is not None
        assert result["captain_score"] == 45
        assert result["captain_score_raw"] == 15.45

    # --- Bench ---

    def test_bench_mid(self):
        eval_, identity = self._build_mid()
        result = calculate_bench_score(eval_, identity, availability_risks=[], next_gw_id=20)
        assert result["priority_score"] == 60
        assert result["priority_score_raw"] == 22.03

    def test_bench_def(self):
        eval_, identity = self._build_def()
        result = calculate_bench_score(eval_, identity, availability_risks=[], next_gw_id=20)
        assert result["priority_score"] == 36
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
        assert normalise_score(16.75, TARGET_CEILING) == 53


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
        """Salah-tier MID: high npxG, strong form, good PPG, on pens, xGI-backed."""
        player = {
            "npxG_per_90": 0.55, "xGChain_per_90": 0.65,
            "form": 8.0, "ppg": 7.5, "penalty_xG_per_90": 0.12,
            "form_trajectory": 1.15, "xgi_sustainability": 1.15,
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
        """MID with npxG, penalty_xG, good FDR and matchup. Season-level regression bonus removed."""

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
        assert score == 58

    def test_gk_def_path(self):
        """GK uses for_gk() weights: xGI zeroed, GK signals active. Score normalised to GK_TARGET_CEILING."""
        # GK with no saves/xgc/cs data — only form+ppg+matchup contribute
        # form: min(4.0*1.0, 5)=4.0, ppg: min(4.5*0.5, 4)=2.25
        # matchup: 6.0*0.75*1.0=4.5 (mins_factor=min(1800/1760,1)=1.0)
        # raw=10.75, normalise(10.75, GK_TARGET_CEILING=30.4) = round(35.36) = 35
        eval, _ = build_player_evaluation(
            {
                "position": "GK",
                "npxG_per_90": 0.0, "xGChain_per_90": 0.0, "xGI_per_90": 0.0,
                "form": 4.0, "ppg": 4.5,
                "GI_minus_xGI": 0.0,
                "minutes": 1800, "appearances": 22,
            },
            matchup_avg_3gw=6.0,
            positional_fdr=3.0,
        )
        score = calculate_target_score(eval, next_gw_id=20)
        # Post-2026-04-10 GK ceiling = 23.08 (was 30.4); quality attenuated by 0.7
        assert score == 38

    def test_zero_minutes(self):
        """Player with 0 appearances: mins_factor=0, matchup zeroed."""

        eval, _ = build_player_evaluation(
            {
                "position": "MID", "xGI_per_90": 0.8, "form": 5.0, "ppg": 4.0,
                "GI_minus_xGI": 0.0, "minutes": 0, "appearances": 0,
            },
        )
        score = calculate_target_score(eval, next_gw_id=20)
        assert score == 22


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
        """Low ownership MID with good matchup. Season-level regression bonus removed."""

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
        assert score == 58

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
        assert score == 38


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
        assert score == 48

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
        assert score == 27

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
        assert score == 31

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
        # DEF waiver ceiling = DEF_WAIVER_CEILING (empirical, from without_xgi caps × 0.85 + bonuses).
        assert score == 44

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
        assert score == 53

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
        assert early == 42
        assert midseason == 35
        assert early > midseason  # Early season is more generous


class TestMatchupBonus:
    """Tests for _matchup_bonus helper."""

    def test_none_returns_zero(self):
        assert _matchup_bonus(None, 0.9) == 0.0

    def test_with_value(self):
        assert abs(_matchup_bonus(7.0, 0.9) - 4.725) < 0.001

    def test_zero_mins_factor(self):
        assert _matchup_bonus(7.0, 0.0) == 0.0


class TestXgiSustainabilityReplacesScoringBonus:
    """Season-level underperformance bonus (gi_minus_xgi) removed; rolling-window
    xgi_sustainability multiplier replaces it.  These tests verify the boundary:
    gi_minus_xgi no longer drives scoring, xgi_sustainability does."""

    def _squad_by_pos(self):
        return {
            "MID": [{"form": 4.0}, {"form": 5.0}],
            "FWD": [],
            "DEF": [{"form": 2.0}],
            "GK": [{"form": 3.0}],
        }

    def _base_eval(self, gi_minus_xgi=0.0):
        eval, _ = build_player_evaluation(
            {"position": "MID", "form": 7.0, "ppg": 5.5, "minutes": 900, "appearances": 10,
             "xGI_per_90": 0.6, "npxG_per_90": 0.4, "xGChain_per_90": 0.5,
             "GI_minus_xGI": gi_minus_xgi,
             "status": "a", "team_short": "BHA"},
            matchup_avg_3gw=6.5, positional_fdr=2.5,
        )
        return eval

    def test_gi_minus_xgi_no_longer_drives_scoring(self):
        """Season-level GI-xGI gap no longer affects waiver score (bonus removed)."""
        score_under = calculate_waiver_score(
            self._base_eval(gi_minus_xgi=-2.5),
            squad_by_position=self._squad_by_pos(), team_counts={}, next_gw_id=20,
        )
        score_neutral = calculate_waiver_score(
            self._base_eval(gi_minus_xgi=0.0),
            squad_by_position=self._squad_by_pos(), team_counts={}, next_gw_id=20,
        )
        # Both score identically: season-level bonus is gone
        assert score_under == score_neutral

    def test_xgi_sustainability_boosts_underperformer(self):
        """xgi_sustainability > 1.0 (underperforming: regression upside) raises score."""
        eval_under, _ = build_player_evaluation(
            {"position": "MID", "form": 7.0, "ppg": 5.5, "minutes": 900, "appearances": 10,
             "xGI_per_90": 0.6, "npxG_per_90": 0.4, "xGChain_per_90": 0.5,
             "GI_minus_xGI": 0.0, "status": "a", "team_short": "BHA",
             "xgi_sustainability": 1.15},
            matchup_avg_3gw=6.5, positional_fdr=2.5,
        )
        score_under = calculate_waiver_score(
            eval_under, squad_by_position=self._squad_by_pos(), team_counts={}, next_gw_id=20,
        )
        score_neutral = calculate_waiver_score(
            self._base_eval(), squad_by_position=self._squad_by_pos(),
            team_counts={}, next_gw_id=20,
        )
        assert score_under > score_neutral

    def test_xgi_sustainability_discounts_overperformer(self):
        """xgi_sustainability < 1.0 (overperforming: regression risk) lowers score."""
        eval_over, _ = build_player_evaluation(
            {"position": "MID", "form": 7.0, "ppg": 5.5, "minutes": 900, "appearances": 10,
             "xGI_per_90": 0.6, "npxG_per_90": 0.4, "xGChain_per_90": 0.5,
             "GI_minus_xGI": 0.0, "status": "a", "team_short": "BHA",
             "xgi_sustainability": 0.85},
            matchup_avg_3gw=6.5, positional_fdr=2.5,
        )
        score_over = calculate_waiver_score(
            eval_over, squad_by_position=self._squad_by_pos(), team_counts={}, next_gw_id=20,
        )
        score_neutral = calculate_waiver_score(
            self._base_eval(), squad_by_position=self._squad_by_pos(),
            team_counts={}, next_gw_id=20,
        )
        assert score_over < score_neutral

    def test_gi_minus_xgi_any_value_scores_same_without_history(self):
        """gi_minus_xgi=-5.0 and -1.0 both score identically (bonus is gone)."""
        score_large = calculate_waiver_score(
            self._base_eval(gi_minus_xgi=-5.0),
            squad_by_position=self._squad_by_pos(), team_counts={}, next_gw_id=20,
        )
        score_small = calculate_waiver_score(
            self._base_eval(gi_minus_xgi=-1.0),
            squad_by_position=self._squad_by_pos(), team_counts={}, next_gw_id=20,
        )
        assert score_large == score_small


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
        """calculate_differential_score body stays compact (ceiling selection + delegate)."""
        body = self._body_lines(calculate_differential_score)
        assert len(body) <= 12, f"Body has {len(body)} lines: {body}"

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
        assert result["captain_score"] == 88
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
        assert result["captain_score"] == 58
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
        assert result["priority_score"] == 63
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
        assert result["priority_score"] == 32
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
        assert result["priority_score"] == 95
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
        assert result["priority_score"] == 57
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
        from unittest.mock import AsyncMock

        import pytest

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
        from fpl_cli.services.matchup import build_team_fixture_map
        from tests.conftest import make_fixture

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
        from fpl_cli.services.matchup import build_team_fixture_map
        from tests.conftest import make_fixture

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
        from fpl_cli.services.matchup import build_team_fixture_map
        from tests.conftest import make_fixture

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
        from fpl_cli.services.matchup import build_gw_fixture_maps, build_team_fixture_map
        from fpl_cli.services.team_ratings import TeamRating, TeamRatingsService
        from tests.conftest import make_fixture, make_team

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
# compute_xgi_sustainability
# ---------------------------------------------------------------------------


class TestComputeXgiSustainability:
    """Tests for compute_xgi_sustainability()."""

    @staticmethod
    def _gw(
        round_num: int,
        goals: int = 0,
        assists: int = 0,
        xg: float = 0.0,
        xa: float = 0.0,
        minutes: int = 90,
    ) -> dict:
        return {
            "round": round_num,
            "goals_scored": goals,
            "assists": assists,
            "expected_goals": str(xg),  # FPL API returns strings
            "expected_assists": str(xa),
            "minutes": minutes,
        }

    def test_overperformer_mid_clamped_to_minimum(self):
        """MID with GI consistently +0.3/match above xGI -> multiplier 0.85."""
        history = [self._gw(r, goals=1, xg=0.7) for r in range(20, 27)]
        mult, div = compute_xgi_sustainability(history, current_gw=26, position="MID")
        assert mult == pytest.approx(0.85, abs=0.001)
        assert div == pytest.approx(0.3, abs=0.001)

    def test_underperformer_fwd_clamped_to_maximum(self):
        """FWD with GI consistently -0.3/match below xGI -> multiplier 1.15."""
        history = [self._gw(r, xg=0.5, xa=0.3) for r in range(20, 27)]
        mult, div = compute_xgi_sustainability(history, current_gw=26, position="FWD")
        assert mult == pytest.approx(1.15, abs=0.001)
        assert div == pytest.approx(-0.8, abs=0.001)

    def test_neutral_mid_returns_one(self):
        """MID matching xGI exactly -> multiplier 1.0."""
        history = [self._gw(r, goals=1, xg=0.7, assists=0, xa=0.3) for r in range(20, 27)]
        mult, div = compute_xgi_sustainability(history, current_gw=26, position="MID")
        assert mult == pytest.approx(1.0, abs=0.001)
        assert div == pytest.approx(0.0, abs=0.001)

    def test_def_position_returns_neutral(self):
        """DEF -> always (1.0, 0.0) regardless of history."""
        history = [self._gw(r, goals=1, xg=0.1) for r in range(20, 27)]
        assert compute_xgi_sustainability(history, current_gw=26, position="DEF") == (1.0, 0.0)

    def test_gk_position_returns_neutral(self):
        """GK -> always (1.0, 0.0) regardless of history."""
        history = [self._gw(r, goals=1, xg=0.1) for r in range(20, 27)]
        assert compute_xgi_sustainability(history, current_gw=26, position="GK") == (1.0, 0.0)

    def test_fewer_than_4_qualifying_returns_neutral(self):
        history = [self._gw(r, goals=1, xg=0.1) for r in range(24, 27)]
        assert compute_xgi_sustainability(history, current_gw=26, position="MID") == (1.0, 0.0)

    def test_empty_history_returns_neutral(self):
        assert compute_xgi_sustainability([], current_gw=26, position="MID") == (1.0, 0.0)

    def test_all_zero_minute_gws_returns_neutral(self):
        history = [self._gw(r, goals=1, xg=0.1, minutes=0) for r in range(20, 27)]
        assert compute_xgi_sustainability(history, current_gw=26, position="MID") == (1.0, 0.0)

    def test_extreme_overperformance_clamped(self):
        """Divergence +1.0/match -> clamped to 0.85."""
        history = [self._gw(r, goals=2, xg=0.5, xa=0.5) for r in range(20, 27)]
        mult, _ = compute_xgi_sustainability(history, current_gw=26, position="MID")
        assert mult == pytest.approx(0.85, abs=0.001)

    def test_extreme_underperformance_clamped(self):
        """Divergence -1.0/match -> clamped to 1.15."""
        history = [self._gw(r, xg=1.5) for r in range(20, 27)]
        mult, _ = compute_xgi_sustainability(history, current_gw=26, position="FWD")
        assert mult == pytest.approx(1.15, abs=0.001)

    def test_dgw_entries_both_count_as_qualifying(self):
        """Two entries with same round (DGW) both consume window slots."""
        # 6 GWs + 1 DGW (2 entries) = 8 entries, but only 7 most recent qualify.
        # Critical: both DGW entries count and together shift the average.
        history = [self._gw(r, xg=0.5) for r in range(19, 25)]  # 6 GWs
        history += [self._gw(25, xg=0.5), self._gw(25, xg=0.5)]  # DGW round 25
        mult, _ = compute_xgi_sustainability(history, current_gw=26, position="MID")
        assert mult == pytest.approx(1.15, abs=0.001)  # all underperforming (no goals)

    def test_12_gw_lookback_excludes_old_gws(self):
        """GWs outside the 12-GW window are excluded."""
        old = [self._gw(r, goals=2, xg=0.1) for r in range(10, 15)]  # outside window
        recent = [self._gw(r) for r in range(15, 27)]  # at-rate, inside window
        history = old + recent
        mult, _ = compute_xgi_sustainability(history, current_gw=26, position="MID")
        assert mult == pytest.approx(1.0, abs=0.001)  # old hauls excluded

    def test_exactly_4_qualifying_gws_computes(self):
        """4 qualifying GWs (minimum) produces a valid computation."""
        history = [self._gw(r, goals=1, xg=0.5) for r in range(23, 27)]
        mult, div = compute_xgi_sustainability(history, current_gw=26, position="MID")
        assert isinstance(mult, float)
        assert 0.85 <= mult <= 1.15
        assert div == pytest.approx(0.5, abs=0.001)

    def test_divergence_at_positive_threshold_exactly(self):
        """Divergence exactly +0.3 -> multiplier exactly 0.85."""
        history = [self._gw(r, goals=1, xg=0.7) for r in range(20, 27)]
        mult, div = compute_xgi_sustainability(history, current_gw=26, position="MID")
        assert div == pytest.approx(0.3, abs=0.001)
        assert mult == pytest.approx(0.85, abs=0.001)

    def test_divergence_at_negative_threshold_exactly(self):
        """Divergence exactly -0.3 -> multiplier exactly 1.15."""
        history = [self._gw(r, xg=0.3) for r in range(20, 27)]
        mult, div = compute_xgi_sustainability(history, current_gw=26, position="FWD")
        assert div == pytest.approx(-0.3, abs=0.001)
        assert mult == pytest.approx(1.15, abs=0.001)

    def test_xg_fields_as_strings_converted_correctly(self):
        """FPL API returns xG values as strings - must handle float conversion."""
        history = [
            {
                "round": r,
                "goals_scored": 1,
                "assists": 0,
                "expected_goals": "0.45",  # string, not float
                "expected_assists": "0.10",  # string, not float
                "minutes": 90,
            }
            for r in range(20, 27)
        ]
        mult, div = compute_xgi_sustainability(history, current_gw=26, position="MID")
        assert div == pytest.approx(0.45, abs=0.001)  # 1 - (0.45+0.10)
        assert mult < 1.0  # overperforming -> regression risk


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
# xgi_sustainability in scoring functions
# ---------------------------------------------------------------------------


class TestXgiSustainabilityInScoring:
    """Verify xgi_sustainability multiplier affects scoring in all families."""

    def _mid_player(self):
        return make_player(
            id=50, web_name="TestMID", team_id=1,
            position=PlayerPosition.MIDFIELDER,
            form=6.0, points_per_game=5.5, minutes=1500, total_points=100,
        )

    def _mid_matchup(self):
        return FixtureMatchup(
            opponent_short="SHU", is_home=True, opponent_fdr=3.0, matchup_score=7.0,
        )

    def _build_mid(self, sustainability=None):
        enrichment: dict = {"team_short": "ARS", "xGI_per_90": 0.5}
        if sustainability is not None:
            enrichment["xgi_sustainability"] = sustainability
        eval, identity = build_player_evaluation(
            self._mid_player(),
            enrichment=enrichment,
            fixture_matchups=[self._mid_matchup()],
            matchup_avg_3gw=6.0, positional_fdr=3.0,
        )
        return eval, identity

    def test_overperformer_gets_lower_captain_score(self):
        """sustainability=0.85 (overperforming) -> lower captain_score_raw than 1.0."""
        eval_default, id_d = self._build_mid()
        eval_over, id_o = self._build_mid(sustainability=0.85)
        result_d = calculate_captain_score(eval_default, id_d, next_gw_id=20)
        result_o = calculate_captain_score(eval_over, id_o, next_gw_id=20)
        assert result_d is not None and result_o is not None
        assert result_o["captain_score_raw"] < result_d["captain_score_raw"]

    def test_underperformer_gets_higher_captain_score(self):
        """sustainability=1.15 (underperforming, upside) -> higher captain_score_raw."""
        eval_default, id_d = self._build_mid()
        eval_under, id_u = self._build_mid(sustainability=1.15)
        result_d = calculate_captain_score(eval_default, id_d, next_gw_id=20)
        result_u = calculate_captain_score(eval_under, id_u, next_gw_id=20)
        assert result_d is not None and result_u is not None
        assert result_u["captain_score_raw"] > result_d["captain_score_raw"]

    def test_neutral_sustainability_matches_default(self):
        """sustainability=1.0 produces identical scores to no sustainability data."""
        eval_neutral, id_n = self._build_mid(sustainability=1.0)
        eval_default, id_d = self._build_mid()
        result_n = calculate_captain_score(eval_neutral, id_n, next_gw_id=20)
        result_d = calculate_captain_score(eval_default, id_d, next_gw_id=20)
        assert result_n is not None and result_d is not None
        assert result_n["captain_score_raw"] == result_d["captain_score_raw"]

    def test_target_score_responds_to_sustainability(self):
        eval_over, _ = self._build_mid(sustainability=0.85)
        eval_default, _ = self._build_mid()
        assert calculate_target_score(eval_over, next_gw_id=20) < calculate_target_score(eval_default, next_gw_id=20)

    def test_differential_score_responds_to_sustainability(self):
        eval_over, _ = self._build_mid(sustainability=0.85)
        eval_default, _ = self._build_mid()
        assert (
            calculate_differential_score(eval_over, semi_differential_threshold=10, next_gw_id=20)
            < calculate_differential_score(eval_default, semi_differential_threshold=10, next_gw_id=20)
        )

    def test_waiver_score_responds_to_sustainability(self):
        eval_over, _ = self._build_mid(sustainability=0.85)
        eval_default, _ = self._build_mid()
        assert (
            calculate_waiver_score(eval_over, squad_by_position={"MID": []}, next_gw_id=20)
            < calculate_waiver_score(eval_default, squad_by_position={"MID": []}, next_gw_id=20)
        )

    def test_bench_score_responds_to_sustainability(self):
        eval_over, id_o = self._build_mid(sustainability=0.85)
        eval_default, id_d = self._build_mid()
        bench_over = calculate_bench_score(eval_over, id_o, availability_risks=[], next_gw_id=20)
        bench_default = calculate_bench_score(eval_default, id_d, availability_risks=[], next_gw_id=20)
        assert bench_over["priority_score_raw"] < bench_default["priority_score_raw"]

    def test_combined_trajectory_and_sustainability(self):
        """trajectory=1.2, sustainability=0.85 -> combined ~1.02 form multiplier."""
        eval_combined, id_c = build_player_evaluation(
            self._mid_player(),
            enrichment={"team_short": "ARS", "xGI_per_90": 0.5,
                        "form_trajectory": 1.2, "xgi_sustainability": 0.85},
            fixture_matchups=[self._mid_matchup()],
            matchup_avg_3gw=6.0, positional_fdr=3.0,
        )
        eval_neutral, id_n = self._build_mid()
        result_c = calculate_captain_score(eval_combined, id_c, next_gw_id=20)
        result_n = calculate_captain_score(eval_neutral, id_n, next_gw_id=20)
        assert result_c is not None and result_n is not None
        # 1.2 * 0.85 = 1.02: should be only slightly higher than neutral (1.0)
        assert result_c["captain_score_raw"] > result_n["captain_score_raw"]

    def test_def_player_sustainability_neutral(self):
        """DEF player with sustainability 1.0 (default) produces same scores."""
        def_player = make_player(
            id=51, web_name="TestDEF", team_id=1,
            position=PlayerPosition.DEFENDER,
            form=5.0, points_per_game=4.5, minutes=1500, total_points=80,
        )
        eval_default, _ = build_player_evaluation(
            def_player, enrichment={"team_short": "ARS"},
            fixture_matchups=[self._mid_matchup()], matchup_avg_3gw=5.5, positional_fdr=3.5,
        )
        eval_1_0, _ = build_player_evaluation(
            def_player, enrichment={"team_short": "ARS", "xgi_sustainability": 1.0},
            fixture_matchups=[self._mid_matchup()], matchup_avg_3gw=5.5, positional_fdr=3.5,
        )
        assert calculate_target_score(eval_default, next_gw_id=20) == calculate_target_score(eval_1_0, next_gw_id=20)


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


# ---------------------------------------------------------------------------
# GK scoring path — end-to-end integration
# ---------------------------------------------------------------------------


class TestGKScoringPath:
    """GK-specific scoring path: for_gk() weights, GK signals, GK ceilings.

    Reference GK: form=5.0, ppg=4.0, minutes=1800, appearances=22
    GK signals (via enrichment): saves_per_90=3.5, xgc_quality=1.2, cs_rate=0.4
    Signal contributions (TARGET weights):
      saves: min(3.5*1.5, 6)=5.25, xgc: min(1.2*3, 3.5)=3.5, cs: min(0.4*8, 4)=3.2
      form: min(5.0*1.0, 5)=5.0, ppg: min(4.0*0.5, 4)=2.0 → quality raw=18.95
    """

    @staticmethod
    def _gk_matchup():
        return FixtureMatchup(
            opponent_short="SHU", is_home=True, opponent_fdr=2.5,
            matchup_score=6.0,
        )

    @staticmethod
    def _gk_player():
        return make_player(
            id=300, web_name="CharGK", team_id=3,
            position=PlayerPosition.GOALKEEPER,
            form=5.0, points_per_game=4.0, minutes=1800, total_points=88,
            expected_goals=0.0, expected_assists=0.0,
        )

    def _build_gk(self):
        eval_, identity = build_player_evaluation(
            self._gk_player(),
            enrichment={
                "gk_saves_per_90": 3.5,
                "gk_xgc_quality": 1.2,
                "gk_cs_rate": 0.4,
                "team_short": "LIV",
            },
            fixture_matchups=[self._gk_matchup()],
            matchup_avg_3gw=6.0, positional_fdr=2.5,
        )
        return eval_, identity

    def test_gk_target_score(self):
        """GK target: (quality raw 17.35 * 0.7) + matchup 4.5 = 16.645; normalise(16.645, 23.08)=72.

        Post-2026-04-10: position multiplier on ownership path + gk_cs_rate halved.
        """
        eval_, _ = self._build_gk()
        assert calculate_target_score(eval_, next_gw_id=20) == 72

    def test_gk_target_vs_def_score(self):
        """GK target uses for_gk() and GK_TARGET_CEILING, scoring differently from DEF."""
        gk_eval, _ = self._build_gk()
        # DEF characterisation from TestCharacterisationSnapshot._build_def
        def_eval, _ = build_player_evaluation(
            make_player(
                id=200, web_name="CharDEF", team_id=2,
                position=PlayerPosition.DEFENDER,
                form=4.5, points_per_game=4.0, minutes=1600, total_points=80,
                expected_goals=1.0, expected_assists=0.5,
            ),
            enrichment={"dc_per_90": 3.5, "team_short": "CHE"},
            fixture_matchups=[FixtureMatchup(
                opponent_short="BOU", is_home=False, opponent_fdr=3.5, matchup_score=5.5,
            )],
            matchup_avg_3gw=5.5, positional_fdr=3.5,
        )
        gk_score = calculate_target_score(gk_eval, next_gw_id=20)
        def_score = calculate_target_score(def_eval, next_gw_id=20)
        assert gk_score != def_score  # distinct scoring paths

    def test_gk_signals_flow_through_evaluation(self):
        """GK signals propagate from enrichment into PlayerEvaluation."""
        eval_, _ = self._build_gk()
        assert eval_.gk_saves_per_90 == pytest.approx(3.5)
        assert eval_.gk_xgc_quality == pytest.approx(1.2)
        assert eval_.gk_cs_rate == pytest.approx(0.4)

    def test_gk_quality_dict_includes_signals(self):
        """as_quality_dict() includes all three GK keys."""
        eval_, _ = self._build_gk()
        q = eval_.as_quality_dict()
        assert q["gk_saves_per_90"] == pytest.approx(3.5)
        assert q["gk_xgc_quality"] == pytest.approx(1.2)
        assert q["gk_cs_rate"] == pytest.approx(0.4)

    def test_gk_zero_stats_lower_than_with_signals(self):
        """GK with no save/xgc/cs data scores lower than one with good signals."""
        with_signals_eval, _ = self._build_gk()
        no_signals_eval, _ = build_player_evaluation(
            self._gk_player(),
            enrichment={"gk_saves_per_90": 0.0, "gk_xgc_quality": 0.0, "gk_cs_rate": 0.0, "team_short": "LIV"},
            fixture_matchups=[self._gk_matchup()],
            matchup_avg_3gw=6.0, positional_fdr=2.5,
        )
        with_score = calculate_target_score(with_signals_eval, next_gw_id=20)
        without_score = calculate_target_score(no_signals_eval, next_gw_id=20)
        assert with_score > without_score

    def test_gk_xgc_quality_guards_zero_minutes(self):
        """GK enrichment: 0 minutes → gk_xgc_quality=0.0 (not 2.0 from inversion)."""
        from fpl_cli.services.player_scoring import build_scoring_enrichment

        gk = make_player(
            id=999, web_name="ZeroMin", team_id=1,
            position=PlayerPosition.GOALKEEPER,
            form=4.0, points_per_game=4.0, minutes=0, total_points=0,
            saves_per_90=0.0, expected_goals_conceded=0.0, clean_sheets=0,
        )
        enrichment = build_scoring_enrichment(gk, us_match={}, team_short="TST", gw_history=None, next_gw_id=20)
        assert enrichment["gk_xgc_quality"] == 0.0

    def test_gk_cs_rate_no_div_zero(self):
        """GK enrichment: 0 appearances → no ZeroDivisionError (max(appearances, 1) guard)."""
        from fpl_cli.services.player_scoring import build_scoring_enrichment

        gk = make_player(
            id=998, web_name="ZeroApp", team_id=1,
            position=PlayerPosition.GOALKEEPER,
            form=4.0, points_per_game=0.0, minutes=0, total_points=0,
            saves_per_90=0.0, expected_goals_conceded=0.0, clean_sheets=0,
        )
        enrichment = build_scoring_enrichment(gk, us_match={}, team_short="TST", gw_history=None, next_gw_id=20)
        assert enrichment["gk_cs_rate"] == 0.0  # 0 cs / max(0, 1) = 0

    def test_gk_sample_ramp_attenuates_low_minutes(self):
        """GK with 90 minutes gets signals at 20% (90/450) of face value."""
        from fpl_cli.services.player_scoring import build_scoring_enrichment

        gk = make_player(
            id=997, web_name="OneApp", team_id=1,
            position=PlayerPosition.GOALKEEPER,
            form=4.0, points_per_game=5.0, minutes=90, total_points=5,
            saves_per_90=4.0, expected_goals_conceded=0.5, clean_sheets=1,
        )
        enrichment = build_scoring_enrichment(gk, us_match={}, team_short="TST", gw_history=None, next_gw_id=20)
        ramp = 90 / 450  # 0.2
        assert enrichment["gk_saves_per_90"] == pytest.approx(4.0 * ramp)
        xgc_per_90 = (0.5 / 90) * 90  # = 0.5
        assert enrichment["gk_xgc_quality"] == pytest.approx((2.0 - xgc_per_90) * ramp)
        assert enrichment["gk_cs_rate"] == pytest.approx((1 / 1) * ramp)

    def test_gk_sample_ramp_full_at_450_minutes(self):
        """GK with 450+ minutes gets full signal values (ramp = 1.0)."""
        from fpl_cli.services.player_scoring import build_scoring_enrichment

        gk = make_player(
            id=996, web_name="FiveApp", team_id=1,
            position=PlayerPosition.GOALKEEPER,
            form=4.0, points_per_game=4.0, minutes=450, total_points=20,
            saves_per_90=3.0, expected_goals_conceded=4.0, clean_sheets=2,
        )
        enrichment = build_scoring_enrichment(gk, us_match={}, team_short="TST", gw_history=None, next_gw_id=20)
        assert enrichment["gk_saves_per_90"] == pytest.approx(3.0)
        xgc_per_90 = (4.0 / 450) * 90  # = 0.8
        assert enrichment["gk_xgc_quality"] == pytest.approx(2.0 - xgc_per_90)
        assert enrichment["gk_cs_rate"] == pytest.approx(2 / 5)

    def test_def_target_score_uses_pos_mult(self):
        """Regression guard: DEF path (without_xgi) is attenuated by POSITION_SCORE_MULTIPLIER[DEF]
        and normalised against DEF_TARGET_CEILING (empirical DEF cap, not MID-anchored × 0.85).
        """
        from tests.test_player_scoring import TestCharacterisationSnapshot
        snap = TestCharacterisationSnapshot()
        def_eval, _ = snap._build_def()
        assert calculate_target_score(def_eval, next_gw_id=20) == 66

    def test_gk_value_score(self):
        """GK value path: for_gk() from VALUE_QUALITY_WEIGHTS, normalised to GK_VALUE_CEILING.

        Post-2026-04-10 (position multiplier + gk_cs_rate halved):
        saves 5.25 + xgc 3.5 + cs 1.636 + form 6.5 + ppg 3.2 ≈ 20.09
        attenuated = 20.09 * 0.7 = 14.06
        normalise(14.06, 19.71) = 71
        """
        from fpl_cli.services.player_scoring import compute_quality_value

        gk = make_player(
            id=301, web_name="ValGK", team_id=3,
            position=PlayerPosition.GOALKEEPER,
            form=5.0, points_per_game=4.0, minutes=1800, total_points=88,
            now_cost=50,
            saves_per_90=3.5, expected_goals_conceded=11.54, clean_sheets=9,
        )
        score, _ = compute_quality_value(gk, us_match={}, next_gw_id=20, team_short="LIV")
        assert score == 71

    def test_gk_value_uses_gk_ceiling_not_value_ceiling(self):
        """GK value score normalised against GK_VALUE_CEILING, not VALUE_CEILING."""
        from fpl_cli.services.player_scoring import compute_quality_value

        gk = make_player(
            id=302, web_name="CeilGK", team_id=3,
            position=PlayerPosition.GOALKEEPER,
            form=5.0, points_per_game=4.0, minutes=1800, total_points=88,
            now_cost=50,
            saves_per_90=3.5, expected_goals_conceded=11.54, clean_sheets=9,
        )
        gk_score, _ = compute_quality_value(gk, us_match={}, next_gw_id=20, team_short="LIV")
        # Recover raw from the normalised score and confirm it would not
        # normalise against VALUE_CEILING (the MID/FWD anchor).
        raw = (gk_score / 100) * GK_VALUE_CEILING
        assert gk_score != normalise_score(raw, VALUE_CEILING)
        assert gk_score == pytest.approx(normalise_score(raw, GK_VALUE_CEILING), abs=1)


# ---------------------------------------------------------------------------
# compute_adjusted_npxg / build_adjusted_npxg_lookup
# ---------------------------------------------------------------------------

def _make_match(
    gameweek: int,
    xg: float,
    minutes_played: int,
    opponent_elo: float,
    penalties_scored: int = 0,
    penalties_missed: int = 0,
    xa: float = 0.0,
    total_shots: int = 0,
    chances_created: int = 0,
    touches_opposition_box: int = 0,
    clearances: int = 0,
    blocks: int = 0,
    interceptions: int = 0,
    tackles_won: int = 0,
    recoveries: int = 0,
    saves: int = 0,
    xgot_faced: float = 0.0,
    goals_prevented: float = 0.0,
) -> MatchRecord:
    return MatchRecord(
        player_id=0,
        gameweek=gameweek,
        xg=xg,
        xa=xa,
        minutes_played=minutes_played,
        opponent_elo=opponent_elo,
        penalties_scored=penalties_scored,
        penalties_missed=penalties_missed,
        is_home=True,
        total_shots=total_shots,
        chances_created=chances_created,
        touches_opposition_box=touches_opposition_box,
        clearances=clearances,
        blocks=blocks,
        interceptions=interceptions,
        tackles_won=tackles_won,
        recoveries=recoveries,
        saves=saves,
        xgot_faced=xgot_faced,
        goals_prevented=goals_prevented,
    )


MEDIAN_ELO = 1700.0


class TestComputeAdjustedNpxg:
    def test_adjustment_scales_up_against_strong_opponent(self):
        """xG against high-Elo opponent is scaled up (factor > 1)."""
        # opponent_elo=1600 < median=1700 => factor = 1700/1600 = 1.0625
        records = [_make_match(gw, xg=0.40, minutes_played=90, opponent_elo=1600.0)
                   for gw in range(1, 8)]
        result = compute_adjusted_npxg(records, current_gw=10, median_elo=MEDIAN_ELO)
        raw_per_90 = 0.40  # xg/90 raw
        assert result is not None
        assert result > raw_per_90  # adjusted up for tough opponents

    def test_adjustment_scales_down_against_weak_opponent(self):
        """xG against low-Elo opponent is scaled down (factor < 1)."""
        # opponent_elo=1800 > median=1700 => factor = 1700/1800 = 0.944
        records = [_make_match(gw, xg=0.40, minutes_played=90, opponent_elo=1800.0)
                   for gw in range(1, 8)]
        result = compute_adjusted_npxg(records, current_gw=10, median_elo=MEDIAN_ELO)
        raw_per_90 = 0.40
        assert result is not None
        assert result < raw_per_90  # adjusted down for easy opponents

    def test_equal_elo_factor_is_one(self):
        """opponent_elo == median_elo => factor 1.0, adjusted == raw."""
        records = [_make_match(gw, xg=0.50, minutes_played=90, opponent_elo=MEDIAN_ELO)
                   for gw in range(1, 8)]
        result = compute_adjusted_npxg(records, current_gw=10, median_elo=MEDIAN_ELO)
        assert result == pytest.approx(0.50, abs=1e-6)

    def test_cap_floor_at_0_80(self):
        """Very high opponent Elo capped at factor 0.80."""
        # opponent_elo=2500 would give factor 1700/2500=0.68 < 0.80 -> capped at 0.80
        records = [_make_match(gw, xg=0.50, minutes_played=90, opponent_elo=2500.0)
                   for gw in range(1, 8)]
        result = compute_adjusted_npxg(records, current_gw=10, median_elo=MEDIAN_ELO)
        assert result == pytest.approx(0.50 * 0.80, abs=1e-6)

    def test_cap_ceiling_at_1_25(self):
        """Very low opponent Elo capped at factor 1.25."""
        # opponent_elo=500 would give factor 1700/500=3.4 > 1.25 -> capped at 1.25
        records = [_make_match(gw, xg=0.50, minutes_played=90, opponent_elo=500.0)
                   for gw in range(1, 8)]
        result = compute_adjusted_npxg(records, current_gw=10, median_elo=MEDIAN_ELO)
        assert result == pytest.approx(0.50 * 1.25, abs=1e-6)

    def test_penalties_reduce_npxg(self):
        """Penalty attempts are subtracted: npxG = xG - penalties * 0.76."""
        # 1 scored, 0 missed -> npxg = 0.76 - 1*0.76 = 0.0, factor 1.0
        records = [_make_match(gw, xg=0.76, minutes_played=90, opponent_elo=MEDIAN_ELO,
                               penalties_scored=1, penalties_missed=0)
                   for gw in range(1, 8)]
        result = compute_adjusted_npxg(records, current_gw=10, median_elo=MEDIAN_ELO)
        assert result == pytest.approx(0.0, abs=1e-6)

    def test_penalties_missed_also_subtracted(self):
        """Both scored and missed penalties are subtracted."""
        # 1 scored + 1 missed = 2 total -> npxg = 1.52 - 2*0.76 = 0.0
        records = [_make_match(gw, xg=1.52, minutes_played=90, opponent_elo=MEDIAN_ELO,
                               penalties_scored=1, penalties_missed=1)
                   for gw in range(1, 8)]
        result = compute_adjusted_npxg(records, current_gw=10, median_elo=MEDIAN_ELO)
        assert result == pytest.approx(0.0, abs=1e-6)

    def test_fewer_than_4_matches_returns_none(self):
        """< 4 qualifying matches returns None (triggers fallback)."""
        records = [_make_match(gw, xg=0.50, minutes_played=90, opponent_elo=MEDIAN_ELO)
                   for gw in range(1, 4)]  # 3 matches
        result = compute_adjusted_npxg(records, current_gw=10, median_elo=MEDIAN_ELO)
        assert result is None

    def test_exactly_4_matches_returns_value(self):
        """Exactly 4 qualifying matches returns a value (minimum threshold)."""
        records = [_make_match(gw, xg=0.50, minutes_played=90, opponent_elo=MEDIAN_ELO)
                   for gw in range(1, 5)]  # 4 matches
        result = compute_adjusted_npxg(records, current_gw=10, median_elo=MEDIAN_ELO)
        assert result is not None

    def test_zero_minutes_in_window_returns_none(self):
        """All matches have 0 minutes_played -> no qualifying matches -> None."""
        records = [_make_match(gw, xg=0.50, minutes_played=0, opponent_elo=MEDIAN_ELO)
                   for gw in range(1, 8)]
        result = compute_adjusted_npxg(records, current_gw=10, median_elo=MEDIAN_ELO)
        assert result is None

    def test_12gw_lookback_excludes_old_matches(self):
        """Matches older than 12 GWs back from current_gw are excluded."""
        # current_gw=20, cutoff=8. GW1-8 excluded, GW9-20 included.
        old = [_make_match(gw, xg=0.50, minutes_played=90, opponent_elo=MEDIAN_ELO)
               for gw in range(1, 9)]    # GW1-8: outside window
        recent = [_make_match(gw, xg=0.30, minutes_played=90, opponent_elo=MEDIAN_ELO)
                  for gw in range(9, 13)]  # GW9-12: inside window (4 matches)
        result = compute_adjusted_npxg(old + recent, current_gw=20, median_elo=MEDIAN_ELO)
        # Should use only 4 recent matches (0.30 xg each), ignoring old 0.50 matches
        assert result == pytest.approx(0.30, abs=1e-6)

    def test_7_match_window_limit(self):
        """Only most recent 7 matches used even when more are available."""
        # 10 matches in window; oldest 3 have xg=1.0, newest 7 have xg=0.20
        old = [_make_match(gw, xg=1.0, minutes_played=90, opponent_elo=MEDIAN_ELO)
               for gw in range(1, 4)]
        recent = [_make_match(gw, xg=0.20, minutes_played=90, opponent_elo=MEDIAN_ELO)
                  for gw in range(4, 11)]
        result = compute_adjusted_npxg(old + recent, current_gw=15, median_elo=MEDIAN_ELO)
        assert result == pytest.approx(0.20, abs=1e-6)

    def test_all_matches_outside_lookback_returns_none(self):
        """All matches before 12-GW lookback returns None."""
        records = [_make_match(gw, xg=0.50, minutes_played=90, opponent_elo=MEDIAN_ELO)
                   for gw in range(1, 8)]
        result = compute_adjusted_npxg(records, current_gw=25, median_elo=MEDIAN_ELO)
        assert result is None


class TestBuildAdjustedNpxgLookup:
    def test_returns_dict_keyed_by_player_id(self):
        records = {
            100: [_make_match(gw, xg=0.50, minutes_played=90, opponent_elo=MEDIAN_ELO)
                  for gw in range(1, 8)],
            200: [_make_match(gw, xg=0.30, minutes_played=90, opponent_elo=MEDIAN_ELO)
                  for gw in range(1, 8)],
        }
        result = build_adjusted_npxg_lookup(records, current_gw=10, median_elo=MEDIAN_ELO)
        assert set(result.keys()) == {100, 200}
        assert result[100] == pytest.approx(0.50, abs=1e-6)
        assert result[200] == pytest.approx(0.30, abs=1e-6)

    def test_player_with_insufficient_data_absent(self):
        """Player with < 4 qualifying matches excluded from lookup."""
        records = {
            100: [_make_match(gw, xg=0.50, minutes_played=90, opponent_elo=MEDIAN_ELO)
                  for gw in range(1, 8)],
            999: [_make_match(1, xg=0.50, minutes_played=90, opponent_elo=MEDIAN_ELO)],
        }
        result = build_adjusted_npxg_lookup(records, current_gw=10, median_elo=MEDIAN_ELO)
        assert 100 in result
        assert 999 not in result


class TestApplyAdjustedNpxg:
    """Tests for apply_adjusted_npxg enrichment helper (Unit 3)."""

    def test_sets_adjusted_when_player_in_lookup(self):
        enrichment = {"npxG_per_90": 0.30}
        lookup = {42: 0.22}
        apply_adjusted_npxg(enrichment, player_id=42, lookup=lookup)
        assert enrichment["npxG_per_90"] == pytest.approx(0.22)

    def test_sets_raw_alongside_adjusted(self):
        enrichment = {"npxG_per_90": 0.30}
        lookup = {42: 0.22}
        apply_adjusted_npxg(enrichment, player_id=42, lookup=lookup)
        assert enrichment["raw_npxG_per_90"] == pytest.approx(0.30)

    def test_no_op_when_lookup_is_none(self):
        enrichment = {"npxG_per_90": 0.30}
        apply_adjusted_npxg(enrichment, player_id=42, lookup=None)
        assert enrichment["npxG_per_90"] == pytest.approx(0.30)

    def test_sets_raw_when_lookup_is_none(self):
        """raw_npxG_per_90 is always written, even when lookup is None."""
        enrichment = {"npxG_per_90": 0.30}
        apply_adjusted_npxg(enrichment, player_id=42, lookup=None)
        assert enrichment["raw_npxG_per_90"] == pytest.approx(0.30)

    def test_player_absent_from_lookup_leaves_npxg_unchanged(self):
        enrichment = {"npxG_per_90": 0.30}
        lookup = {99: 0.22}  # player 42 not in lookup
        apply_adjusted_npxg(enrichment, player_id=42, lookup=lookup)
        assert enrichment["npxG_per_90"] == pytest.approx(0.30)
        assert enrichment["raw_npxG_per_90"] == pytest.approx(0.30)

    def test_raw_npxg_none_when_not_in_enrichment(self):
        """raw_npxG_per_90 is None when npxG_per_90 absent from enrichment."""
        enrichment: dict = {}
        apply_adjusted_npxg(enrichment, player_id=42, lookup=None)
        assert enrichment["raw_npxG_per_90"] is None

    def test_pre_populated_npxg_with_lookup(self):
        """Caller pre-populates npxG_per_90 in enrichment; lookup overrides it."""
        enrichment: dict = {"npxG_per_90": 0.25}
        lookup = {42: 0.19}
        apply_adjusted_npxg(enrichment, player_id=42, lookup=lookup)
        assert enrichment["npxG_per_90"] == pytest.approx(0.19)
        assert enrichment["raw_npxG_per_90"] == pytest.approx(0.25)

    def test_raw_npxg_propagates_to_player_evaluation(self):
        """raw_npxg_per_90 field on PlayerEvaluation populated via enrichment."""
        enrichment = {"npxG_per_90": 0.30}
        lookup = {42: 0.20}
        apply_adjusted_npxg(enrichment, player_id=42, lookup=lookup)
        evaluation, _ = build_player_evaluation(make_player(id=42), enrichment=enrichment)
        assert evaluation.npxg_per_90 == pytest.approx(0.20)
        assert evaluation.raw_npxg_per_90 == pytest.approx(0.30)


def _make_fm(score=7.0, fdr=2.5):
    return FixtureMatchup(
        opponent_short="SHU", is_home=True, opponent_fdr=fdr,
        matchup_score=score,
        matchup_breakdown={
            "matchup_score": score, "attack_matchup": 6.0, "defence_matchup": 5.0,
            "form_differential": 0.2, "position_differential": 0.1, "reasoning": [],
        },
    )


class TestCaptainCandidateAdjustedNpxgFields:
    """Unit 5: adjusted npxG context in CaptainCandidate output."""

    def _make_eval_with_adjusted(self, adjusted: float, raw: float):
        enrichment = {
            "npxG_per_90": adjusted, "raw_npxG_per_90": raw,
            "xGChain_per_90": 0.5, "xGI_per_90": 0.4, "team_short": "ARS",
        }
        evaluation, identity = build_player_evaluation(
            make_player(id=1, position=PlayerPosition.FORWARD,
                        form=6.0, points_per_game=5.5, minutes=1800),
            enrichment=enrichment,
            fixture_matchups=[_make_fm()],
        )
        return evaluation, identity

    def test_captain_output_includes_raw_npxg_when_present(self):
        eval_, identity = self._make_eval_with_adjusted(adjusted=0.20, raw=0.30)
        result = calculate_captain_score(eval_, identity, next_gw_id=20)
        assert result is not None
        assert "raw_npxg_per_90" in result
        assert result["raw_npxg_per_90"] == pytest.approx(0.30, abs=1e-4)

    def test_captain_output_includes_adjusted_when_differs_from_raw(self):
        eval_, identity = self._make_eval_with_adjusted(adjusted=0.20, raw=0.30)
        result = calculate_captain_score(eval_, identity, next_gw_id=20)
        assert result is not None
        assert "adjusted_npxg_per_90" in result
        assert result["adjusted_npxg_per_90"] == pytest.approx(0.20, abs=1e-4)

    def test_captain_output_omits_adjusted_when_equal_to_raw(self):
        """No adjustment active: adjusted equals raw -> field absent."""
        eval_, identity = self._make_eval_with_adjusted(adjusted=0.30, raw=0.30)
        result = calculate_captain_score(eval_, identity, next_gw_id=20)
        assert result is not None
        assert "raw_npxg_per_90" in result
        assert "adjusted_npxg_per_90" not in result

    def test_captain_output_omits_npxg_fields_when_no_understat(self):
        """No Understat data: both fields absent from output."""
        evaluation, identity = build_player_evaluation(
            make_player(id=1, position=PlayerPosition.FORWARD,
                        form=6.0, points_per_game=5.5, minutes=1800),
            fixture_matchups=[_make_fm()],
        )
        result = calculate_captain_score(evaluation, identity, next_gw_id=20)
        assert result is not None
        assert "raw_npxg_per_90" not in result
        assert "adjusted_npxg_per_90" not in result


class TestTransferEvalAdjustedNpxgFields:
    """Unit 5: adjusted npxG context in TransferEvalAgent output dicts."""

    def _make_target_entry_with_adjusted(self, adjusted: float | None, raw: float | None) -> dict:
        entry: dict = {
            "id": 1, "web_name": "Salah", "team_short": "LIV", "position": "MID",
            "target_score": 60, "fixture_matchups": [], "form": 7.0,
            "status": "a", "chance_of_playing": None, "reliability": None,
            "price": 13.5, "quality_score": 80, "quality_per_m": 5.9,
            "rolling_pts_per_m": 4.2, "rolling_fixture_count": 5,
            "cv_xgi_percentile": 0.5, "raw_npxg_per_90": raw,
        }
        if adjusted is not None and raw is not None and adjusted != raw:
            entry["adjusted_npxg_per_90"] = round(adjusted, 4)
        return entry

    def _make_lineup_entry(self) -> dict:
        return {"lineup_score": 45, "excluded": False}

    def test_transfer_player_dict_includes_raw_when_present(self):
        from fpl_cli.agents.analysis.transfer_eval import TransferEvalAgent
        target = self._make_target_entry_with_adjusted(adjusted=0.22, raw=0.30)
        lineup = self._make_lineup_entry()
        result = TransferEvalAgent._build_player_dict(target, lineup, outlook_delta=5, gw_delta=2)
        assert "raw_npxg_per_90" in result
        assert result["raw_npxg_per_90"] == pytest.approx(0.30, abs=1e-4)

    def test_transfer_player_dict_includes_adjusted_when_differs(self):
        from fpl_cli.agents.analysis.transfer_eval import TransferEvalAgent
        target = self._make_target_entry_with_adjusted(adjusted=0.22, raw=0.30)
        lineup = self._make_lineup_entry()
        result = TransferEvalAgent._build_player_dict(target, lineup, outlook_delta=5, gw_delta=2)
        assert "adjusted_npxg_per_90" in result
        assert result["adjusted_npxg_per_90"] == pytest.approx(0.22, abs=1e-4)

    def test_transfer_player_dict_omits_npxg_fields_when_none(self):
        from fpl_cli.agents.analysis.transfer_eval import TransferEvalAgent
        target = self._make_target_entry_with_adjusted(adjusted=None, raw=None)
        lineup = self._make_lineup_entry()
        result = TransferEvalAgent._build_player_dict(target, lineup, outlook_delta=5, gw_delta=2)
        assert "raw_npxg_per_90" not in result
        assert "adjusted_npxg_per_90" not in result


# ---------------------------------------------------------------------------
# Consistency signal computation
# ---------------------------------------------------------------------------


class TestComputeCvXgi:
    def test_stable_series_returns_low_cv(self):
        records = [
            _make_match(gw, xg=0.3, xa=0.1, minutes_played=90, opponent_elo=MEDIAN_ELO)
            for gw in range(1, 8)
        ]
        result = compute_cv_xgi(records, current_gw=10, median_elo=MEDIAN_ELO)
        assert result is not None
        assert result == 0.0  # identical values -> zero CV

    def test_volatile_series_returns_high_cv(self):
        xgis = [0.0, 0.0, 2.0, 0.0, 0.0, 0.0, 0.3]
        records = [
            _make_match(gw, xg=x, xa=0.1, minutes_played=90, opponent_elo=MEDIAN_ELO)
            for gw, x in enumerate(xgis, start=1)
        ]
        result = compute_cv_xgi(records, current_gw=10, median_elo=MEDIAN_ELO)
        assert result is not None
        assert result > 1.0  # highly volatile

    def test_fewer_than_6_returns_none(self):
        records = [
            _make_match(gw, xg=0.4, xa=0.1, minutes_played=90, opponent_elo=MEDIAN_ELO)
            for gw in range(1, 6)
        ]
        assert compute_cv_xgi(records, current_gw=10, median_elo=MEDIAN_ELO) is None

    def test_all_zeros_returns_none(self):
        records = [
            _make_match(gw, xg=0.0, xa=0.0, minutes_played=90, opponent_elo=MEDIAN_ELO)
            for gw in range(1, 8)
        ]
        assert compute_cv_xgi(records, current_gw=10, median_elo=MEDIAN_ELO) is None

    def test_single_nonzero_in_window_returns_high_cv(self):
        records = [
            _make_match(gw, xg=0.0 if gw < 7 else 0.5, xa=0.0, minutes_played=90, opponent_elo=MEDIAN_ELO)
            for gw in range(1, 8)
        ]
        result = compute_cv_xgi(records, current_gw=10, median_elo=MEDIAN_ELO)
        assert result is not None
        assert result > 2.0

    def test_elo_adjustment_changes_values(self):
        records = [
            _make_match(gw, xg=0.4, xa=0.1, minutes_played=90, opponent_elo=1500.0)
            for gw in range(1, 8)
        ]
        result = compute_cv_xgi(records, current_gw=10, median_elo=MEDIAN_ELO)
        assert result is not None
        # All same opponent -> still zero CV (uniform adjustment)
        assert result == 0.0

    def test_dgw_records_both_counted(self):
        records = [
            _make_match(gw, xg=0.3, xa=0.1, minutes_played=90, opponent_elo=MEDIAN_ELO)
            for gw in range(1, 7)
        ]
        # Add second match in GW6 (DGW)
        records.append(
            _make_match(6, xg=0.5, xa=0.2, minutes_played=90, opponent_elo=MEDIAN_ELO)
        )
        result = compute_cv_xgi(records, current_gw=10, median_elo=MEDIAN_ELO)
        assert result is not None
        assert result > 0.0  # DGW introduces variance

    def test_cameo_appearances_excluded(self):
        """Matches with < 60 minutes are excluded from the window."""
        # 5 full starts + 2 cameos = only 5 qualifying -> None (< 6 min matches)
        records = [
            _make_match(gw, xg=0.3, xa=0.1, minutes_played=90, opponent_elo=MEDIAN_ELO)
            for gw in range(1, 6)
        ]
        records.append(_make_match(6, xg=0.0, xa=0.0, minutes_played=17, opponent_elo=MEDIAN_ELO))
        records.append(_make_match(7, xg=0.1, xa=0.0, minutes_played=45, opponent_elo=MEDIAN_ELO))
        assert compute_cv_xgi(records, current_gw=10, median_elo=MEDIAN_ELO) is None

    def test_cameo_excluded_but_enough_starts(self):
        """With 6+ full starts, cameos don't inflate CV."""
        records = [
            _make_match(gw, xg=0.3, xa=0.1, minutes_played=90, opponent_elo=MEDIAN_ELO)
            for gw in range(1, 7)
        ]
        # Cameo that would inflate CV if included
        records.append(_make_match(7, xg=0.0, xa=0.0, minutes_played=17, opponent_elo=MEDIAN_ELO))
        result = compute_cv_xgi(records, current_gw=10, median_elo=MEDIAN_ELO)
        assert result is not None
        assert result == 0.0  # all 6 starts identical -> zero CV


class TestComputeCvXgiFallback:
    def test_produces_cv_without_elo(self):
        history = [
            {"round": gw, "minutes": 90, "expected_goals": 0.3, "expected_assists": 0.1}
            for gw in range(1, 8)
        ]
        result = compute_cv_xgi_fallback(history, current_gw=10)
        assert result is not None
        assert result == 0.0  # identical values

    def test_fewer_than_6_returns_none(self):
        history = [
            {"round": gw, "minutes": 90, "expected_goals": 0.3, "expected_assists": 0.1}
            for gw in range(1, 4)
        ]
        assert compute_cv_xgi_fallback(history, current_gw=10) is None

    def test_all_zeros_returns_none(self):
        history = [
            {"round": gw, "minutes": 90, "expected_goals": 0.0, "expected_assists": 0.0}
            for gw in range(1, 8)
        ]
        assert compute_cv_xgi_fallback(history, current_gw=10) is None


class TestComputeBlankRate:
    def test_happy_path(self):
        # pts <= 2 at GW1(2), GW3(1), GW5(2), GW7(2) = 4 blanks
        history = [
            {"round": gw, "minutes": 90, "total_points": pts}
            for gw, pts in [(1, 2), (2, 8), (3, 1), (4, 6), (5, 2), (6, 10), (7, 2)]
        ]
        result = compute_blank_rate(history, current_gw=10)
        assert result is not None
        assert result == pytest.approx(4 / 7)

    def test_def_with_blanks(self):
        history = [
            {"round": gw, "minutes": 90, "total_points": pts}
            for gw, pts in [(1, 6), (2, 6), (3, 2), (4, 6), (5, 6), (6, 2), (7, 6)]
        ]
        result = compute_blank_rate(history, current_gw=10)
        assert result is not None
        assert result == pytest.approx(2 / 7)

    def test_fewer_than_6_returns_none(self):
        history = [
            {"round": gw, "minutes": 90, "total_points": 5}
            for gw in range(1, 4)
        ]
        assert compute_blank_rate(history, current_gw=10) is None


class TestComputeFloorXgi:
    def test_happy_path_p25(self):
        xg_vals = [0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7]
        records = [
            _make_match(gw, xg=x, xa=0.0, minutes_played=90, opponent_elo=MEDIAN_ELO)
            for gw, x in enumerate(xg_vals, start=1)
        ]
        result = compute_floor_xgi(records, current_gw=10, median_elo=MEDIAN_ELO)
        assert result is not None
        # p25 of [0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7] = index 1.5 -> 0.25
        assert result == pytest.approx(0.25, abs=0.01)

    def test_fewer_than_6_returns_none(self):
        records = [
            _make_match(gw, xg=0.3, xa=0.0, minutes_played=90, opponent_elo=MEDIAN_ELO)
            for gw in range(1, 4)
        ]
        assert compute_floor_xgi(records, current_gw=10, median_elo=MEDIAN_ELO) is None


class TestComputeFloorXgiFallback:
    def test_happy_path_p25(self):
        xg_vals = [0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7]
        history = [
            {"round": gw, "minutes": 90, "expected_goals": x, "expected_assists": 0.0}
            for gw, x in enumerate(xg_vals, start=1)
        ]
        result = compute_floor_xgi_fallback(history, current_gw=10)
        assert result is not None
        assert result == pytest.approx(0.25, abs=0.01)


class TestComputeInvolvementRate:
    def test_atk_happy_path(self):
        records = [
            _make_match(gw, xg=0.3, minutes_played=90, opponent_elo=MEDIAN_ELO,
                        total_shots=2, chances_created=1, touches_opposition_box=5)
            for gw in range(1, 8)
        ]
        result = compute_involvement_rate(records, [], current_gw=10, position="FWD")
        assert result == pytest.approx(1.0)

    def test_atk_partial_involvement(self):
        records = []
        for gw in range(1, 8):
            if gw <= 5:
                records.append(_make_match(gw, xg=0.3, minutes_played=90, opponent_elo=MEDIAN_ELO,
                                           total_shots=2))
            else:
                records.append(_make_match(gw, xg=0.0, minutes_played=90, opponent_elo=MEDIAN_ELO))
        result = compute_involvement_rate(records, [], current_gw=10, position="MID")
        assert result is not None
        assert result == pytest.approx(5 / 7)

    def test_def_core_insights(self):
        records = [
            _make_match(gw, xg=0.0, minutes_played=90, opponent_elo=MEDIAN_ELO,
                        clearances=3, blocks=1, interceptions=1, tackles_won=tw)
            for gw, tw in [(1, 3), (2, 1), (3, 4), (4, 2), (5, 1), (6, 3), (7, 0)]
        ]
        # CBIT+tackles: 8, 6, 9, 7, 6, 8, 5 -> 6 involved (>= 6)
        result = compute_involvement_rate(records, [], current_gw=10, position="DEF")
        # Only GW7 (sum=5) is not involved
        expected = 6 / 7
        assert result == pytest.approx(expected)

    def test_def_fallback_to_fpl_api(self):
        history = [
            {"round": gw, "minutes": 90,
             "clearances_blocks_interceptions": cbi, "tackles": t}
            for gw, cbi, t in [
                (1, 4, 3), (2, 2, 1), (3, 5, 2), (4, 3, 1),
                (5, 4, 2), (6, 5, 3), (7, 1, 1),
            ]
        ]
        result = compute_involvement_rate(None, history, current_gw=10, position="DEF")
        # CBI+tackles: 7, 3, 7, 4, 6, 8, 2 -> 4 involved
        assert result == pytest.approx(4 / 7)

    def test_gk_returns_none(self):
        assert compute_involvement_rate([], [], current_gw=10, position="GK") is None

    def test_atk_no_records_returns_none(self):
        assert compute_involvement_rate(None, [], current_gw=10, position="FWD") is None

    def test_fewer_than_6_returns_none(self):
        records = [
            _make_match(gw, xg=0.3, minutes_played=90, opponent_elo=MEDIAN_ELO,
                        total_shots=2)
            for gw in range(1, 4)
        ]
        assert compute_involvement_rate(records, [], current_gw=10, position="FWD") is None


class TestComputeGkConsistency:
    def test_stable_saves_returns_low_cv(self):
        records = [
            _make_match(gw, xg=0.0, minutes_played=90, opponent_elo=MEDIAN_ELO, saves=3)
            for gw in range(1, 8)
        ]
        result = compute_gk_consistency(records, current_gw=10)
        assert result is not None
        assert result == 0.0  # identical saves/90

    def test_variable_saves_returns_positive_cv(self):
        save_counts = [1, 5, 2, 6, 3, 4, 7]
        records = [
            _make_match(gw, xg=0.0, minutes_played=90, opponent_elo=MEDIAN_ELO, saves=s)
            for gw, s in enumerate(save_counts, start=1)
        ]
        result = compute_gk_consistency(records, current_gw=10)
        assert result is not None
        assert result > 0.3  # meaningfully variable

    def test_fewer_than_6_returns_none(self):
        records = [
            _make_match(gw, xg=0.0, minutes_played=90, opponent_elo=MEDIAN_ELO, saves=3)
            for gw in range(1, 4)
        ]
        assert compute_gk_consistency(records, current_gw=10) is None

    def test_zero_saves_returns_none(self):
        records = [
            _make_match(gw, xg=0.0, minutes_played=90, opponent_elo=MEDIAN_ELO, saves=0)
            for gw in range(1, 8)
        ]
        assert compute_gk_consistency(records, current_gw=10) is None


# ---------------------------------------------------------------------------
# Percentile ranks + build_consistency_lookup
# ---------------------------------------------------------------------------


class TestAssignPercentileRanks:
    def test_inverted_lowest_value_gets_highest_percentile(self):
        values = {1: 0.1, 2: 0.5, 3: 0.9}
        # Need 15+ for real percentiles, pad with more
        for i in range(4, 20):
            values[i] = 0.3 + i * 0.01
        result = _assign_percentile_ranks(values, invert=True)
        assert result[1] > result[3]  # lowest CV -> highest percentile

    def test_below_min_pool_all_neutral(self):
        values = {1: 0.1, 2: 0.5, 3: 0.9}
        result = _assign_percentile_ranks(values, invert=True)
        assert all(v == 0.5 for v in result.values())

    def test_ties_get_same_percentile(self):
        values = {}
        for i in range(1, 16):
            values[i] = 0.5 if i <= 3 else 0.3 + i * 0.01
        result = _assign_percentile_ranks(values, invert=False)
        assert result[1] == result[2] == result[3]

    def test_exactly_15_computes_percentiles(self):
        values = {i: float(i) for i in range(1, 16)}
        result = _assign_percentile_ranks(values, invert=False)
        assert result[1] == pytest.approx(0.0)
        assert result[15] == pytest.approx(1.0)

    def test_all_identical_values_get_neutral(self):
        values = {i: 0.5 for i in range(1, 20)}
        result = _assign_percentile_ranks(values, invert=True)
        assert all(v == pytest.approx(0.5) for v in result.values())


class TestBuildConsistencyLookup:
    def _make_fwd_records(self, pid: int, xg_vals: list[float]):
        return [
            _make_match(gw, xg=x, xa=0.1, minutes_played=90, opponent_elo=MEDIAN_ELO,
                        total_shots=2, chances_created=1)
            for gw, x in enumerate(xg_vals, start=1)
        ]

    def test_happy_path_20_fwd_players(self):
        """20 FWD players with varying CVs produce percentiles spanning 0-1."""
        match_records: dict[int, list] = {}
        positions: dict[int, str] = {}
        histories: dict[int, list] = {}
        for pid in range(1, 21):
            volatility = pid * 0.05
            xg_vals = [0.3 + (volatility if gw % 2 == 0 else -volatility) for gw in range(7)]
            match_records[pid] = self._make_fwd_records(pid, xg_vals)
            positions[pid] = "FWD"
            histories[pid] = [
                {"round": gw, "minutes": 90, "total_points": 5}
                for gw in range(1, 8)
            ]

        result = build_consistency_lookup(
            match_records, histories, positions, current_gw=10, median_elo=MEDIAN_ELO,
        )
        assert len(result) > 0
        percentiles = [s.cv_xgi_percentile for s in result.values()]
        assert min(percentiles) < 0.2
        assert max(percentiles) > 0.8

    def test_lowest_cv_gets_highest_percentile(self):
        """Most consistent player (lowest CV) gets percentile closest to 1.0."""
        match_records: dict[int, list] = {}
        positions: dict[int, str] = {}
        histories: dict[int, list] = {}
        for pid in range(1, 21):
            if pid == 1:
                xg_vals = [0.4] * 7  # perfectly consistent
            else:
                xg_vals = [0.4 + (pid * 0.1 if gw % 2 == 0 else 0) for gw in range(7)]
            match_records[pid] = self._make_fwd_records(pid, xg_vals)
            positions[pid] = "FWD"
            histories[pid] = [
                {"round": gw, "minutes": 90, "total_points": 5}
                for gw in range(1, 8)
            ]

        result = build_consistency_lookup(
            match_records, histories, positions, current_gw=10, median_elo=MEDIAN_ELO,
        )
        # Player 1 has CV = 0 (all identical) -> should get highest percentile
        assert result[1].cv_xgi_percentile == pytest.approx(1.0)

    def test_player_without_records_uses_fallback(self):
        """Player with FPL API history but no match records enters the lookup."""
        match_records: dict[int, list] = {}
        positions: dict[int, str] = {}
        histories: dict[int, list] = {}
        # 19 players with varying match records
        for pid in range(1, 20):
            volatility = pid * 0.05
            xg_vals = [0.3 + (volatility if gw % 2 == 0 else -volatility) for gw in range(7)]
            match_records[pid] = self._make_fwd_records(pid, xg_vals)
            positions[pid] = "FWD"
            histories[pid] = [
                {"round": gw, "minutes": 90, "total_points": 5}
                for gw in range(1, 8)
            ]
        # Player 20: history only, no match records, with varying xGI
        positions[20] = "FWD"
        histories[20] = [
            {"round": gw, "minutes": 90, "total_points": 5,
             "expected_goals": 0.2 + gw * 0.05, "expected_assists": 0.1}
            for gw in range(1, 8)
        ]

        result = build_consistency_lookup(
            match_records, histories, positions, current_gw=10, median_elo=MEDIAN_ELO,
        )
        assert 20 in result  # fallback path included player in lookup

    def test_position_group_below_15_gets_neutral(self):
        """Position group with fewer than 15 qualifying players -> all get 0.5 percentile."""
        match_records: dict[int, list] = {}
        positions: dict[int, str] = {}
        histories: dict[int, list] = {}
        for pid in range(1, 11):  # only 10 GKs
            match_records[pid] = [
                _make_match(gw, xg=0.0, minutes_played=90, opponent_elo=MEDIAN_ELO, saves=3)
                for gw in range(1, 8)
            ]
            positions[pid] = "GK"
            histories[pid] = [
                {"round": gw, "minutes": 90, "total_points": 5}
                for gw in range(1, 8)
            ]

        result = build_consistency_lookup(
            match_records, histories, positions, current_gw=10, median_elo=MEDIAN_ELO,
        )
        for pid in range(1, 11):
            if pid in result:
                assert result[pid].gk_consistency_percentile == 0.5

    def test_no_data_player_absent_from_lookup(self):
        """Player with no records and no history is absent from the lookup."""
        result = build_consistency_lookup(
            {}, {}, {42: "FWD"}, current_gw=10, median_elo=MEDIAN_ELO,
        )
        assert 42 not in result

    def test_neutral_signals_has_correct_defaults(self):
        assert NEUTRAL_SIGNALS.cv_xgi_percentile == 0.5
        assert NEUTRAL_SIGNALS.blank_rate is None
        assert NEUTRAL_SIGNALS.floor_percentile == 0.5
        assert NEUTRAL_SIGNALS.involvement_rate is None
        assert NEUTRAL_SIGNALS.gk_consistency_percentile == 0.5


# ---------------------------------------------------------------------------
# apply_consistency + PlayerEvaluation consistency fields
# ---------------------------------------------------------------------------


class TestApplyConsistency:
    def test_injects_all_fields(self):
        enrichment: dict = {}
        lookup = {42: ConsistencySignals(
            cv_xgi_percentile=0.8, blank_rate=0.2,
            floor_percentile=0.7, involvement_rate=0.9,
            gk_consistency_percentile=0.5,
        )}
        apply_consistency(enrichment, 42, lookup)
        assert enrichment["cv_xgi_percentile"] == 0.8
        assert enrichment["blank_rate"] == 0.2
        assert enrichment["floor_percentile"] == 0.7
        assert enrichment["involvement_rate"] == 0.9

    def test_missing_player_no_injection(self):
        enrichment: dict = {}
        lookup = {99: ConsistencySignals()}
        apply_consistency(enrichment, 42, lookup)
        assert "cv_xgi_percentile" not in enrichment

    def test_none_lookup_no_injection(self):
        enrichment: dict = {}
        apply_consistency(enrichment, 42, None)
        assert "cv_xgi_percentile" not in enrichment


class TestPlayerEvaluationConsistencyFields:
    def test_neutral_defaults_when_no_enrichment(self):
        player = make_player(id=1)
        evaluation, _ = build_player_evaluation(player)
        assert evaluation.cv_xgi_percentile == 0.5
        assert evaluation.blank_rate is None
        assert evaluation.floor_percentile == 0.5
        assert evaluation.involvement_rate is None
        assert evaluation.gk_consistency_percentile == 0.5

    def test_enrichment_populates_fields(self):
        player = make_player(id=1)
        enrichment = {
            "cv_xgi_percentile": 0.9,
            "blank_rate": 0.15,
            "floor_percentile": 0.75,
            "involvement_rate": 0.85,
            "gk_consistency_percentile": 0.6,
        }
        evaluation, _ = build_player_evaluation(player, enrichment=enrichment)
        assert evaluation.cv_xgi_percentile == 0.9
        assert evaluation.blank_rate == 0.15
        assert evaluation.floor_percentile == 0.75
        assert evaluation.involvement_rate == 0.85
        assert evaluation.gk_consistency_percentile == 0.6

    def test_frozen_immutable(self):
        player = make_player(id=1)
        evaluation, _ = build_player_evaluation(player)
        with pytest.raises(AttributeError):
            evaluation.cv_xgi_percentile = 0.9  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Phase 2: consistency scoring integration
# ---------------------------------------------------------------------------


class TestConsistencyPhaseIn:
    def test_before_window_zero(self):
        assert _consistency_phase(5) == 0.0
        assert _consistency_phase(1) == 0.0

    def test_after_window_full(self):
        assert _consistency_phase(10) == 1.0
        assert _consistency_phase(38) == 1.0

    def test_midpoint(self):
        assert _consistency_phase(8) == pytest.approx(0.6)

    def test_start_boundary(self):
        assert _consistency_phase(6) == pytest.approx(0.2)


class TestConsistencyScoringIntegration:
    """Verify consistency signals affect all scoring families."""

    @staticmethod
    def _matchup():
        return FixtureMatchup(
            opponent_short="SHU", is_home=True, opponent_fdr=3.0, matchup_score=7.0,
        )

    def _build_mid(self, cv=0.5, floor=0.5, involvement=None):
        enrichment = {
            "team_short": "ARS", "xGI_per_90": 0.5,
            "cv_xgi_percentile": cv,
            "floor_percentile": floor,
        }
        if involvement is not None:
            enrichment["involvement_rate"] = involvement
        return build_player_evaluation(
            make_player(
                id=50, web_name="TestMID", team_id=1,
                position=PlayerPosition.MIDFIELDER,
                form=6.0, points_per_game=5.5, minutes=1500, total_points=100,
            ),
            enrichment=enrichment,
            fixture_matchups=[self._matchup()],
            matchup_avg_3gw=6.0, positional_fdr=3.0,
        )

    # -- Target family --

    def test_target_consistent_scores_higher(self):
        eval_con, _ = self._build_mid(cv=0.9)
        eval_vol, _ = self._build_mid(cv=0.1)
        assert calculate_target_score(eval_con, next_gw_id=20) > calculate_target_score(eval_vol, next_gw_id=20)

    def test_target_neutral_no_change(self):
        eval_a, _ = self._build_mid(cv=0.5)
        eval_b, _ = self._build_mid(cv=0.5)
        assert calculate_target_score(eval_a, next_gw_id=20) == calculate_target_score(eval_b, next_gw_id=20)

    # -- Differential family (inverted) --

    def test_differential_volatile_scores_higher(self):
        eval_vol, _ = self._build_mid(cv=0.1)
        eval_con, _ = self._build_mid(cv=0.9)
        assert (
            calculate_differential_score(eval_vol, semi_differential_threshold=10, next_gw_id=20)
            > calculate_differential_score(eval_con, semi_differential_threshold=10, next_gw_id=20)
        )

    # -- Waiver family --

    def test_waiver_consistent_scores_higher(self):
        eval_con, _ = self._build_mid(cv=0.9)
        eval_vol, _ = self._build_mid(cv=0.1)
        assert (
            calculate_waiver_score(eval_con, squad_by_position={"MID": []}, next_gw_id=20)
            > calculate_waiver_score(eval_vol, squad_by_position={"MID": []}, next_gw_id=20)
        )

    # -- Bench family --

    def test_bench_high_floor_scores_higher(self):
        eval_hi, id_hi = self._build_mid(floor=0.9)
        eval_lo, id_lo = self._build_mid(floor=0.1)
        bench_hi = calculate_bench_score(eval_hi, id_hi, availability_risks=[], next_gw_id=20)
        bench_lo = calculate_bench_score(eval_lo, id_lo, availability_risks=[], next_gw_id=20)
        assert bench_hi["priority_score_raw"] > bench_lo["priority_score_raw"]

    def test_bench_involvement_adds_bonus(self):
        eval_inv, id_inv = self._build_mid(involvement=0.9)
        eval_none, id_none = self._build_mid(involvement=0.5)
        bench_inv = calculate_bench_score(eval_inv, id_inv, availability_risks=[], next_gw_id=20)
        bench_none = calculate_bench_score(eval_none, id_none, availability_risks=[], next_gw_id=20)
        assert bench_inv["priority_score_raw"] > bench_none["priority_score_raw"]

    def test_bench_no_involvement_no_crash(self):
        """involvement_rate=None should not crash."""
        eval_no, id_no = self._build_mid(involvement=None)
        result = calculate_bench_score(eval_no, id_no, availability_risks=[], next_gw_id=20)
        assert result["priority_score_raw"] > 0

    # -- Captain / lineup (single-GW tiebreaker) --

    def test_captain_consistent_scores_higher(self):
        eval_con, id_con = self._build_mid(cv=0.9)
        eval_vol, id_vol = self._build_mid(cv=0.1)
        cap_con = calculate_captain_score(eval_con, id_con, next_gw_id=20)
        cap_vol = calculate_captain_score(eval_vol, id_vol, next_gw_id=20)
        assert cap_con is not None and cap_vol is not None
        assert cap_con["captain_score_raw"] > cap_vol["captain_score_raw"]

    def test_lineup_consistent_scores_higher(self):
        eval_con, id_con = self._build_mid(cv=0.9)
        eval_vol, id_vol = self._build_mid(cv=0.1)
        lu_con = calculate_lineup_score(eval_con, id_con, next_gw_id=20)
        lu_vol = calculate_lineup_score(eval_vol, id_vol, next_gw_id=20)
        assert lu_con["lineup_score_raw"] > lu_vol["lineup_score_raw"]

    # -- Phase-in --

    def test_no_effect_at_gw5(self):
        eval_con, _ = self._build_mid(cv=0.9)
        eval_neut, _ = self._build_mid(cv=0.5)
        assert calculate_target_score(eval_con, next_gw_id=5) == calculate_target_score(eval_neut, next_gw_id=5)

    def test_partial_effect_at_gw8(self):
        eval_con, _ = self._build_mid(cv=0.9)
        eval_neut, _ = self._build_mid(cv=0.5)
        # At GW8, phase=0.6, so there should be some but not full effect
        score_con = calculate_target_score(eval_con, next_gw_id=8)
        score_neut = calculate_target_score(eval_neut, next_gw_id=8)
        score_con_full = calculate_target_score(eval_con, next_gw_id=20)
        score_neut_full = calculate_target_score(eval_neut, next_gw_id=20)
        # Partial effect: gap at GW8 should be smaller than gap at GW20
        gap_8 = score_con - score_neut
        gap_20 = score_con_full - score_neut_full
        assert gap_8 >= 0
        assert gap_20 >= gap_8

    def test_bench_no_effect_at_gw5(self):
        eval_hi, id_hi = self._build_mid(floor=0.9)
        eval_lo, id_lo = self._build_mid(floor=0.1)
        bench_hi = calculate_bench_score(eval_hi, id_hi, availability_risks=[], next_gw_id=5)
        bench_lo = calculate_bench_score(eval_lo, id_lo, availability_risks=[], next_gw_id=5)
        assert bench_hi["priority_score_raw"] == bench_lo["priority_score_raw"]

    # -- Neutral signals produce zero change --

    def test_neutral_consistency_no_scoring_impact(self):
        """All signals at 0.5 / None should produce identical scores."""
        eval_a, id_a = self._build_mid(cv=0.5, floor=0.5, involvement=None)
        eval_b, _ = self._build_mid()  # defaults
        assert calculate_target_score(eval_a, next_gw_id=20) == calculate_target_score(eval_b, next_gw_id=20)

    # -- as_quality_dict includes consistency --

    def test_as_quality_dict_includes_consistency(self):
        eval_con, _ = self._build_mid(cv=0.8, floor=0.7, involvement=0.9)
        qd = eval_con.as_quality_dict()
        assert qd["cv_xgi_percentile"] == 0.8
        assert qd["floor_percentile"] == 0.7
        assert qd["involvement_rate"] == 0.9
        assert qd["gk_consistency_percentile"] == 0.5


# ---------------------------------------------------------------------------
# Position multiplier regression tests (2026-04-10)
# ---------------------------------------------------------------------------


class TestPositionMultiplierLock:
    """Version-lock POSITION_SCORE_MULTIPLIER and verify attenuation wiring."""

    def test_constant_values_locked(self):
        """Editing POSITION_SCORE_MULTIPLIER requires an explicit test update."""
        from fpl_cli.services.player_scoring import POSITION_SCORE_MULTIPLIER
        assert POSITION_SCORE_MULTIPLIER == {
            "FWD": 1.0,
            "MID": 1.0,
            "DEF": 0.85,
            "GK": 0.7,
        }

    def test_position_none_preserves_legacy_behaviour(self):
        """position=None keeps legacy score (no multiplier)."""
        player = {"form": 5.0, "ppg": 4.0, "xGI_per_90": 0.5}
        legacy = calculate_player_quality_score(player, VALUE_QUALITY_WEIGHTS)
        explicit_none = calculate_player_quality_score(
            player, VALUE_QUALITY_WEIGHTS, position=None,
        )
        assert legacy == pytest.approx(explicit_none)

    def test_mid_position_is_noop(self):
        """MID multiplier=1.0 so score is unchanged from position=None."""
        player = {"form": 5.0, "ppg": 4.0, "xGI_per_90": 0.5}
        base = calculate_player_quality_score(player, VALUE_QUALITY_WEIGHTS)
        with_mid = calculate_player_quality_score(
            player, VALUE_QUALITY_WEIGHTS, position="MID",
        )
        assert with_mid == pytest.approx(base)

    def test_def_attenuates_by_0_85(self):
        player = {"form": 5.0, "ppg": 4.0, "xGI_per_90": 0.5}
        base = calculate_player_quality_score(
            player, VALUE_QUALITY_WEIGHTS.without_xgi(),
        )
        with_def = calculate_player_quality_score(
            player, VALUE_QUALITY_WEIGHTS.without_xgi(), position="DEF",
        )
        assert with_def == pytest.approx(base * 0.85)

    def test_gk_attenuates_by_0_7(self):
        player = {
            "form": 5.0, "ppg": 4.0,
            "gk_saves_per_90": 3.5, "gk_xgc_quality": 1.2, "gk_cs_rate": 0.4,
        }
        base = calculate_player_quality_score(player, VALUE_QUALITY_WEIGHTS.for_gk())
        with_gk = calculate_player_quality_score(
            player, VALUE_QUALITY_WEIGHTS.for_gk(), position="GK",
        )
        assert with_gk == pytest.approx(base * 0.7)

    def test_unknown_position_raises(self):
        """Unknown positions raise KeyError (strict lookup, no silent fallback)."""
        player = {"form": 5.0, "ppg": 4.0, "xGI_per_90": 0.5}
        with pytest.raises(KeyError):
            calculate_player_quality_score(
                player, VALUE_QUALITY_WEIGHTS, position="UNKNOWN",  # type: ignore[arg-type]
            )


class TestCeilingValidationBands:
    """Elite and edge-case players produce sensible normalised scores."""

    def _elite_mid(self):
        # Fernandes-tier: high form, strong xGI, nailed, in-form trajectory
        return make_player(
            id=401, web_name="EliteMID", team_id=1,
            position=PlayerPosition.MIDFIELDER,
            form=7.5, points_per_game=5.8, minutes=2000, total_points=180,
            now_cost=95, expected_goals=10.0, expected_assists=8.0,
        )

    def _elite_fwd(self):
        # Haaland-tier: xG monster
        return make_player(
            id=402, web_name="EliteFWD", team_id=2,
            position=PlayerPosition.FORWARD,
            form=7.0, points_per_game=6.2, minutes=1900, total_points=190,
            now_cost=150, expected_goals=15.0, expected_assists=4.0,
        )

    def _elite_def(self):
        return make_player(
            id=403, web_name="EliteDEF", team_id=3,
            position=PlayerPosition.DEFENDER,
            form=6.0, points_per_game=5.0, minutes=2000, total_points=140,
            now_cost=65, expected_goals=1.5, expected_assists=1.0,
        )

    def _elite_gk(self):
        return make_player(
            id=404, web_name="EliteGK", team_id=4,
            position=PlayerPosition.GOALKEEPER,
            form=5.5, points_per_game=4.8, minutes=2000, total_points=120,
            now_cost=55,
            saves_per_90=3.5, expected_goals_conceded=18.0, clean_sheets=10,
        )

    def _score(self, player, *, next_gw_id=20):
        from fpl_cli.services.player_scoring import compute_quality_value
        score, _ = compute_quality_value(
            player, us_match={}, next_gw_id=next_gw_id, team_short="LIV",
        )
        return score

    def test_elite_mid_in_band(self):
        assert 55 <= self._score(self._elite_mid()) <= 100

    def test_elite_fwd_in_band(self):
        assert 55 <= self._score(self._elite_fwd()) <= 100

    def test_elite_def_in_band(self):
        # DEF uses without_xgi() so the xGI family is zeroed. Elite DEFs
        # land in the lower half of the 0-100 scale by design. See todo
        # 006 for the display-ceiling inconsistency this exposes.
        assert 45 <= self._score(self._elite_def()) <= 85

    def test_elite_gk_in_band(self):
        assert 55 <= self._score(self._elite_gk()) <= 100

    def test_elite_fwd_outranks_elite_gk_on_raw_quality(self):
        """Load-bearing regression: Haaland-tier FWD raw > Raya-tier GK raw.

        This is the exact class of bug the stopgap plan fixes — before the
        position multiplier landed on the multi-GW path, elite GKs were
        out-ranking elite outfielders in the top-N raw_quality table.
        """
        from fpl_cli.services.player_scoring import compute_quality_value
        fwd_raw = compute_quality_value(
            self._elite_fwd(), us_match={}, next_gw_id=20, team_short="LIV", raw=True,
        )
        gk_raw = compute_quality_value(
            self._elite_gk(), us_match={}, next_gw_id=20, team_short="LIV", raw=True,
        )
        assert fwd_raw > gk_raw

    def test_low_minutes_backup_gk_not_nonsense(self):
        """Darlow-tier backup: 180 mins, ramp 0.4, still produces sane 0-100 score."""
        backup_gk = make_player(
            id=405, web_name="BackupGK", team_id=5,
            position=PlayerPosition.GOALKEEPER,
            form=3.0, points_per_game=2.5, minutes=180, total_points=12,
            now_cost=39,
            saves_per_90=3.0, expected_goals_conceded=2.5, clean_sheets=1,
        )
        score = self._score(backup_gk)
        assert 0 <= score <= 60


class TestPositionalDistributionGuard:
    """Top-N composition under compute_quality_value. Guards against GK dominance."""

    def _build_pool(self):
        """Synthetic pool spanning positions with realistic parameter ranges."""
        pool = []
        # Elite outfielders (MID + FWD) — should dominate top 15
        for i in range(6):
            pool.append(make_player(
                id=500 + i, web_name=f"ElMID{i}", team_id=i + 1,
                position=PlayerPosition.MIDFIELDER,
                form=6.5 + i * 0.2, points_per_game=5.0 + i * 0.1,
                minutes=1800, total_points=130 + i * 10,
                now_cost=80 + i * 5, expected_goals=8.0, expected_assists=6.0,
            ))
        for i in range(6):
            pool.append(make_player(
                id=510 + i, web_name=f"ElFWD{i}", team_id=(i + 6) + 1,
                position=PlayerPosition.FORWARD,
                form=6.0 + i * 0.2, points_per_game=5.0 + i * 0.1,
                minutes=1700, total_points=120 + i * 10,
                now_cost=75 + i * 5, expected_goals=10.0, expected_assists=3.0,
            ))
        # Solid DEFs
        for i in range(6):
            pool.append(make_player(
                id=520 + i, web_name=f"SoDEF{i}", team_id=i + 1,
                position=PlayerPosition.DEFENDER,
                form=5.0 + i * 0.2, points_per_game=4.5 + i * 0.1,
                minutes=1800, total_points=100 + i * 5,
                now_cost=55, expected_goals=1.5, expected_assists=1.0,
            ))
        # Elite GKs (the ones that were dominating top 10)
        for i in range(6):
            pool.append(make_player(
                id=530 + i, web_name=f"ElGK{i}", team_id=i + 5,
                position=PlayerPosition.GOALKEEPER,
                form=5.0 + i * 0.2, points_per_game=4.5 + i * 0.1,
                minutes=1800, total_points=100 + i * 5,
                now_cost=50,
                saves_per_90=3.5, expected_goals_conceded=16.0,
                clean_sheets=8 + i,
            ))
        return pool

    def test_top_15_has_at_most_three_gks(self):
        from fpl_cli.services.player_scoring import compute_quality_value
        pool = self._build_pool()
        scored = [
            (p, compute_quality_value(
                p, us_match={}, next_gw_id=20, team_short="LIV", raw=True,
            ))
            for p in pool
        ]
        scored.sort(key=lambda pr: pr[1], reverse=True)
        top15 = [p for p, _ in scored[:15]]
        gk_count = sum(1 for p in top15 if p.position_name == "GK")
        assert gk_count <= 3, (
            f"Expected ≤3 GKs in top 15 raw_quality, got {gk_count}. "
            f"Top 15 positions: {[p.position_name for p in top15]}"
        )
