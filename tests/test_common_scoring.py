"""Characterization tests for shared player quality scoring."""

from math import inf

import pytest

from fpl_cli.services.player_scoring import QualityWeights, StatWeight, calculate_player_quality_score
from fpl_cli.services.player_scoring import (
    DIFFERENTIAL_QUALITY_WEIGHTS as DIFFERENTIAL_WEIGHTS,
    TARGET_QUALITY_WEIGHTS as TARGET_WEIGHTS,
    WAIVER_QUALITY_WEIGHTS as WAIVER_WEIGHTS,
)


@pytest.fixture
def mid_player_with_npxg():
    return {
        "position": "MID",
        "npxG_per_90": 0.35,
        "xGChain_per_90": 0.55,
        "xGI_per_90": 0.45,
        "form": 6.0,
        "ppg": 5.5,
    }


@pytest.fixture
def mid_player_without_npxg():
    return {
        "position": "MID",
        "npxG_per_90": None,
        "xGChain_per_90": None,
        "xGI_per_90": 0.45,
        "form": 6.0,
        "ppg": 5.5,
    }


class TestStatWeight:
    def test_default_cap_is_inf(self):
        w = StatWeight(5)
        assert w.multiplier == 5
        assert w.cap == inf

    def test_explicit_cap(self):
        w = StatWeight(10, 8)
        assert w.multiplier == 10
        assert w.cap == 8

    def test_frozen(self):
        w = StatWeight(5)
        with pytest.raises(AttributeError):
            w.multiplier = 10  # type: ignore[misc]


class TestQualityWeights:
    def test_without_xgi_zeroes_xgi_fields(self):
        w = WAIVER_WEIGHTS.without_xgi()
        assert w.npxg.multiplier == 0
        assert w.npxg.cap == 0
        assert w.xg_chain.multiplier == 0
        assert w.xg_chain.cap == 0
        assert w.xgi_fallback.multiplier == 0
        assert w.xgi_fallback.cap == 0

    def test_without_xgi_preserves_form_ppg(self):
        w = WAIVER_WEIGHTS.without_xgi()
        assert w.form == WAIVER_WEIGHTS.form
        assert w.ppg == WAIVER_WEIGHTS.ppg

    def test_without_xgi_returns_new_instance(self):
        original = WAIVER_WEIGHTS
        zeroed = original.without_xgi()
        assert zeroed is not original
        # Original unchanged
        assert original.npxg.multiplier == 5

    def test_frozen(self):
        with pytest.raises(AttributeError):
            WAIVER_WEIGHTS.form = StatWeight(99, 99)  # type: ignore[misc]


class TestCalculatePlayerQualityScore:
    def test_npxg_path_with_waiver_weights(self, mid_player_with_npxg):
        """npxG=0.35*5=1.75, xGChain=0.55*2=1.1, form=6*1.3=7(cap7), ppg=5.5*0.6=3.3 → 13.15"""
        score = calculate_player_quality_score(mid_player_with_npxg, WAIVER_WEIGHTS)
        assert score == pytest.approx(13.15)

    def test_npxg_path_with_target_weights(self, mid_player_with_npxg):
        """npxG=0.35*10=3.5(cap8), xGChain=0.55*2=1.1(cap3), form=6*1=5(cap5), ppg=5.5*0.5=2.75(cap4) → 12.35"""
        score = calculate_player_quality_score(mid_player_with_npxg, TARGET_WEIGHTS)
        assert score == pytest.approx(12.35)

    def test_npxg_path_with_differential_weights(self, mid_player_with_npxg):
        """npxG=3.5, xGChain=1.1, form=6*1.3=7(cap7), ppg=5.5*0.5=2.75(cap4) → 14.35"""
        score = calculate_player_quality_score(mid_player_with_npxg, DIFFERENTIAL_WEIGHTS)
        assert score == pytest.approx(14.35)

    def test_xgi_fallback_when_npxg_missing(self, mid_player_without_npxg):
        """No npxG → xGI=0.45*5=2.25, form=6*1.3=7(cap7), ppg=5.5*0.6=3.3 → 12.55"""
        score = calculate_player_quality_score(mid_player_without_npxg, WAIVER_WEIGHTS)
        assert score == pytest.approx(12.55)

    def test_xgi_fallback_with_target_weights(self, mid_player_without_npxg):
        """No npxG → xGI=0.45*10=4.5(cap10), form=6*1=5(cap5), ppg=5.5*0.5=2.75(cap4) → 12.25"""
        score = calculate_player_quality_score(mid_player_without_npxg, TARGET_WEIGHTS)
        assert score == pytest.approx(12.25)

    def test_without_xgi_only_form_and_ppg(self, mid_player_with_npxg):
        """without_xgi: form=6*1.3=7(cap7), ppg=5.5*0.6=3.3 → 10.3"""
        weights = WAIVER_WEIGHTS.without_xgi()
        score = calculate_player_quality_score(mid_player_with_npxg, weights)
        assert score == pytest.approx(10.3)

    def test_without_xgi_target_weights(self, mid_player_with_npxg):
        """without_xgi: form=6*1=5(cap5), ppg=5.5*0.5=2.75 → 7.75"""
        weights = TARGET_WEIGHTS.without_xgi()
        score = calculate_player_quality_score(mid_player_with_npxg, weights)
        assert score == pytest.approx(7.75)

    def test_cap_enforced(self):
        """High npxG should be capped."""
        player = {"npxG_per_90": 1.0, "xGChain_per_90": 0.0, "form": 0, "ppg": 0}
        score = calculate_player_quality_score(player, TARGET_WEIGHTS)
        # min(1.0 * 10, 8) = 8.0
        assert score == pytest.approx(8.0)

    def test_uncapped_passes_through(self):
        """Waiver npxG has no cap (inf)."""
        player = {"npxG_per_90": 1.0, "xGChain_per_90": 0.0, "form": 0, "ppg": 0}
        score = calculate_player_quality_score(player, WAIVER_WEIGHTS)
        # min(1.0 * 5, inf) = 5.0
        assert score == pytest.approx(5.0)

    def test_zero_form_and_ppg(self):
        player = {"npxG_per_90": None, "xGI_per_90": 0, "form": 0, "ppg": 0}
        score = calculate_player_quality_score(player, TARGET_WEIGHTS)
        assert score == 0.0

    def test_missing_keys_default_to_zero(self):
        """Minimal dict with no stats keys."""
        score = calculate_player_quality_score({}, TARGET_WEIGHTS)
        assert score == 0.0

    def test_xg_chain_none_treated_as_zero(self):
        """xGChain_per_90 can be None from Understat - should not crash."""
        player = {"npxG_per_90": 0.5, "xGChain_per_90": None, "form": 0, "ppg": 0}
        score = calculate_player_quality_score(player, TARGET_WEIGHTS)
        # min(0.5 * 10, 8) = 5.0, xGChain None→0
        assert score == pytest.approx(5.0)

    def test_dc_per_90_adds_to_def_score(self):
        """DC/90 contributes to score when using without_xgi weights."""
        defender = {"form": 4.0, "ppg": 3.0, "dc_per_90": 5.0}
        weights = WAIVER_WEIGHTS.without_xgi()
        score = calculate_player_quality_score(defender, weights)
        # form=4*1.3=5.2(cap7), ppg=3*0.6=1.8, dc=min(5*0.5, 2)=2.0 → 9.0
        assert score == pytest.approx(9.0)

    def test_dc_per_90_capped(self):
        """DC/90 contribution is capped at 2."""
        defender = {"form": 0, "ppg": 0, "dc_per_90": 10.0}
        weights = WAIVER_WEIGHTS.without_xgi()
        score = calculate_player_quality_score(defender, weights)
        # min(10 * 0.5, 2) = 2.0
        assert score == pytest.approx(2.0)

    def test_dc_per_90_ignored_for_mid_fwd(self):
        """DC/90 does not affect MID/FWD scoring (base weights are zero)."""
        mid_with_dc = {"npxG_per_90": 0.35, "xGChain_per_90": 0.55,
                       "form": 6.0, "ppg": 5.5, "dc_per_90": 5.0}
        mid_without_dc = {"npxG_per_90": 0.35, "xGChain_per_90": 0.55,
                          "form": 6.0, "ppg": 5.5}
        assert (calculate_player_quality_score(mid_with_dc, WAIVER_WEIGHTS)
                == calculate_player_quality_score(mid_without_dc, WAIVER_WEIGHTS))

    def test_dc_per_90_zero_contributes_nothing(self):
        """DEF with dc_per_90=0 gets same score as without the key."""
        defender_zero = {"form": 4.0, "ppg": 3.0, "dc_per_90": 0.0}
        defender_missing = {"form": 4.0, "ppg": 3.0}
        weights = WAIVER_WEIGHTS.without_xgi()
        assert (calculate_player_quality_score(defender_zero, weights)
                == calculate_player_quality_score(defender_missing, weights))

    def test_without_xgi_sets_dc_per_90_weight(self):
        """without_xgi() activates dc_per_90 weight for DEF/GK scoring."""
        w = WAIVER_WEIGHTS.without_xgi()
        assert w.dc_per_90.multiplier == 0.5
        assert w.dc_per_90.cap == 2

    def test_without_xgi_zeroes_penalty_xg(self):
        """without_xgi() zeroes penalty_xg even when source weights have it non-zero."""
        w = TARGET_WEIGHTS.without_xgi()
        assert w.penalty_xg.multiplier == 0
        assert w.penalty_xg.cap == 0


class TestMinsFactor:
    def test_mins_factor_halves_per90_not_form_ppg(self):
        """mins_factor=0.5 halves per-90 components but not form/ppg."""
        player = {"npxG_per_90": 0.5, "xGChain_per_90": 0.5, "form": 4.0, "ppg": 4.0}
        full = calculate_player_quality_score(player, TARGET_WEIGHTS, mins_factor=1.0)
        half = calculate_player_quality_score(player, TARGET_WEIGHTS, mins_factor=0.5)
        # per90 part: min(0.5*10,8)=5 + min(0.5*2,3)=1 = 6
        # form: min(4*1,5)=4, ppg: min(4*0.5,4)=2
        # full: 6*1.0 + 4 + 2 = 12.0
        # half: 6*0.5 + 4 + 2 = 9.0
        assert full == pytest.approx(12.0)
        assert half == pytest.approx(9.0)

    def test_mins_factor_default_preserves_existing_behavior(self, mid_player_with_npxg):
        """Default mins_factor=1.0 produces same result as before."""
        explicit = calculate_player_quality_score(mid_player_with_npxg, WAIVER_WEIGHTS, mins_factor=1.0)
        implicit = calculate_player_quality_score(mid_player_with_npxg, WAIVER_WEIGHTS)
        assert explicit == implicit

    def test_mins_factor_zero_zeroes_per90_preserves_form_ppg(self):
        """mins_factor=0.0 (zero appearances) zeroes per-90 but keeps form/ppg."""
        player = {"npxG_per_90": 0.5, "xGChain_per_90": 0.5, "form": 4.0, "ppg": 4.0}
        score = calculate_player_quality_score(player, TARGET_WEIGHTS, mins_factor=0.0)
        # per90 = 6.0 * 0.0 = 0.0, form=4, ppg=2 -> 6.0
        assert score == pytest.approx(6.0)

    def test_waiver_weights_unaffected_by_default(self):
        """Waiver weights with default mins_factor produce unchanged baseline scores."""
        player = {"npxG_per_90": 0.35, "xGChain_per_90": 0.55, "form": 6.0, "ppg": 5.5}
        # npxG=0.35*5=1.75, xGChain=0.55*2=1.1, form=6*1.3=7(cap7), ppg=5.5*0.6=3.3 → 13.15
        score = calculate_player_quality_score(player, WAIVER_WEIGHTS)
        assert score == pytest.approx(13.15)

    def test_waiver_combined_mins_factor_scales_per90_only(self):
        """Waiver's combined_mins_factor dampens per-90 stats but not form/ppg.

        Player with low availability (200 mins) and moderate per-appearance:
        availability = 200/450 ≈ 0.444, per_appearance = min(200/(4*80), 1.0) = 0.625
        combined = 0.444 * 0.625 ≈ 0.278

        per90: npxG=0.35*5=1.75, xGChain=0.55*2=1.1 → 2.85 * 0.278 ≈ 0.792
        form: 6.0*1.3=7.0(cap7, unscaled), ppg: 5.5*0.6=3.3 (unscaled)
        total ≈ 11.092
        """
        player = {"npxG_per_90": 0.35, "xGChain_per_90": 0.55, "form": 6.0, "ppg": 5.5}
        availability = min(200 / 450, 1.0)
        per_appearance = min(200 / (4 * 80), 1.0)
        combined = availability * per_appearance
        score = calculate_player_quality_score(player, WAIVER_WEIGHTS, mins_factor=combined)
        # per90 damped, form/ppg preserved
        assert score == pytest.approx(2.85 * combined + 7.0 + 3.3, abs=0.01)
        # Confirm this is higher than old approach (entire baseline * combined)
        old_full_baseline = 13.15
        old_score = old_full_baseline * combined
        assert score > old_score


class TestPenaltyXgComponent:
    def test_penalty_contributes_to_target_score(self):
        """MID with penalty_xG_per_90 gets penalty bonus with target weights."""
        player = {"npxG_per_90": 0.3, "xGChain_per_90": 0.5, "form": 3.0, "ppg": 4.0,
                  "penalty_xG_per_90": 0.15}
        score = calculate_player_quality_score(player, TARGET_WEIGHTS)
        # npxG: min(0.3*10,8)=3, xGCh: min(0.5*2,3)=1, pen: min(0.15*8,3)=1.2
        # per90 = 5.2 * 1.0 = 5.2
        # form: min(3*1,5)=3, ppg: min(4*0.5,4)=2
        assert score == pytest.approx(10.2)

    def test_penalty_none_contributes_zero(self):
        """penalty_xG_per_90=None (no Understat data) contributes zero."""
        player = {"npxG_per_90": 0.3, "xGChain_per_90": 0.5, "form": 3.0, "ppg": 4.0,
                  "penalty_xG_per_90": None}
        score_with_none = calculate_player_quality_score(player, TARGET_WEIGHTS)
        player_no_pen = {"npxG_per_90": 0.3, "xGChain_per_90": 0.5, "form": 3.0, "ppg": 4.0}
        score_without = calculate_player_quality_score(player_no_pen, TARGET_WEIGHTS)
        assert score_with_none == score_without

    def test_penalty_zero_weight_no_contribution(self):
        """Waiver weights (penalty_xg=StatWeight(8,3)) include penalty data."""
        player = {"npxG_per_90": 0.35, "xGChain_per_90": 0.55, "form": 6.0, "ppg": 5.5,
                  "penalty_xG_per_90": 0.20}
        score = calculate_player_quality_score(player, WAIVER_WEIGHTS)
        # npxG=1.75, xGChain=1.1, pen=0.20*8=1.6, form=7(cap7), ppg=3.3 → 14.75
        assert score == pytest.approx(14.75)

    def test_penalty_capped(self):
        """Penalty contribution is capped at 3 for target weights."""
        player = {"npxG_per_90": 0, "xGChain_per_90": 0, "form": 0, "ppg": 0,
                  "penalty_xG_per_90": 1.0}
        score = calculate_player_quality_score(player, TARGET_WEIGHTS)
        # min(1.0 * 8, 3) = 3.0
        assert score == pytest.approx(3.0)

    def test_penalty_with_mins_factor(self):
        """Penalty per-90 is scaled by mins_factor like other per-90 stats."""
        player = {"npxG_per_90": 0.5, "xGChain_per_90": 0, "form": 0, "ppg": 0,
                  "penalty_xG_per_90": 0.2}
        full = calculate_player_quality_score(player, TARGET_WEIGHTS, mins_factor=1.0)
        half = calculate_player_quality_score(player, TARGET_WEIGHTS, mins_factor=0.5)
        # per90: npxG=min(0.5*10,8)=5 + pen=min(0.2*8,3)=1.6 = 6.6
        # full: 6.6 * 1.0 = 6.6, half: 6.6 * 0.5 = 3.3
        assert full == pytest.approx(6.6)
        assert half == pytest.approx(3.3)

    def test_penalty_with_xgi_fallback_no_npxg(self):
        """Penalty component works alongside xGI fallback when npxG is unavailable."""
        player = {"npxG_per_90": None, "xGI_per_90": 0.5, "form": 2.0, "ppg": 3.0,
                  "penalty_xG_per_90": 0.15}
        score = calculate_player_quality_score(player, TARGET_WEIGHTS)
        # xGI fallback: min(0.5*10,10)=5, pen: min(0.15*8,3)=1.2
        # per90 = 6.2 * 1.0, form=min(2*1,5)=2, ppg=min(3*0.5,4)=1.5
        assert score == pytest.approx(9.7)

    def test_def_gets_no_penalty(self):
        """DEF/GK via without_xgi() gets no penalty component."""
        defender = {"form": 4.0, "ppg": 3.0, "dc_per_90": 2.0, "penalty_xG_per_90": 0.15}
        weights = TARGET_WEIGHTS.without_xgi()
        score = calculate_player_quality_score(defender, weights)
        # form=min(4*1,5)=4, ppg=min(3*0.5,4)=1.5, dc=min(2*0.5,2)=1 → 6.5
        assert score == pytest.approx(6.5)
