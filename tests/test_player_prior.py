"""Tests for player-level Bayesian prior generation."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from fpl_cli.api.historical_types import PlayerProfile, SeasonHistory
from fpl_cli.models.player import PlayerPosition
from fpl_cli.services.player_prior import (
    CUTOFF_GW,
    PRICE_CONFIDENCE_FACTOR,
    PlayerPrior,
    _compute_confidence,
    _extract_prev_season_pts_per_90,
    percentile_rank,
    _save_prior_cache,
    early_season_quality_warning,
    generate_player_prior,
    load_cached_priors,
    load_or_generate_player_priors,
    observation_weight_range,
)
from tests.conftest import make_player

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_season(
    code: int,
    season: str = "2024-25",
    total_points: int = 150,
    minutes: int = 2700,
) -> SeasonHistory:
    return SeasonHistory(
        element_code=code,
        season=season,
        total_points=total_points,
        minutes=minutes,
        starts=30,
        goals=10,
        assists=5,
        expected_goals=9.0,
        expected_assists=4.5,
        expected_goal_involvements=13.5,
        start_cost=80,
        end_cost=90,
        position="MID",
        web_name="TestPlayer",
        team_id=1,
    )


def _make_profile(
    code: int,
    seasons: list[SeasonHistory] | None = None,
    reliability: float | None = None,
) -> PlayerProfile:
    return PlayerProfile(
        element_code=code, web_name="TestPlayer", current_position="MID",
        seasons=seasons or [], reliability=reliability,
    )


# ---------------------------------------------------------------------------
# _extract_prev_season_pts_per_90
# ---------------------------------------------------------------------------


class TestExtractPrevSeasonPts:
    def test_matching_season_with_enough_minutes(self):
        sh = _make_season(100, season="2024-25", total_points=150, minutes=2700)
        profile = _make_profile(100, seasons=[sh])
        result = _extract_prev_season_pts_per_90(profile, "2024-25")
        assert result == pytest.approx(150 / 2700 * 90, rel=1e-3)

    def test_no_matching_season(self):
        sh = _make_season(100, season="2023-24", total_points=150, minutes=2700)
        profile = _make_profile(100, seasons=[sh])
        assert _extract_prev_season_pts_per_90(profile, "2024-25") is None

    def test_below_min_minutes(self):
        sh = _make_season(100, season="2024-25", total_points=30, minutes=400)
        profile = _make_profile(100, seasons=[sh])
        assert _extract_prev_season_pts_per_90(profile, "2024-25") is None


# ---------------------------------------------------------------------------
# percentile_rank
# ---------------------------------------------------------------------------


class TestPercentileRank:
    def test_middle_value(self):
        assert percentile_rank(3.0, [1.0, 2.0, 3.0, 4.0, 5.0]) == pytest.approx(0.5)

    def test_lowest_value(self):
        assert percentile_rank(1.0, [1.0, 2.0, 3.0, 4.0, 5.0]) == pytest.approx(0.1)

    def test_highest_value(self):
        assert percentile_rank(5.0, [1.0, 2.0, 3.0, 4.0, 5.0]) == pytest.approx(0.9)

    def test_single_value(self):
        assert percentile_rank(5.0, [5.0]) == 0.5

    def test_all_equal(self):
        assert percentile_rank(3.0, [3.0, 3.0, 3.0]) == 0.5


# ---------------------------------------------------------------------------
# _compute_confidence
# ---------------------------------------------------------------------------


class TestComputeConfidence:
    def test_gw3_75th_percentile(self):
        # base = 3/9 = 0.333, conf = 0.333 * 1.75 = 0.583
        result = _compute_confidence(3, 0.75)
        assert result == pytest.approx(0.583, abs=0.01)

    def test_gw3_50th_percentile(self):
        # base = 3/9 = 0.333, conf = 0.333 * 1.5 = 0.5
        result = _compute_confidence(3, 0.5)
        assert result == pytest.approx(0.5, abs=0.01)

    def test_at_cutoff(self):
        assert _compute_confidence(CUTOFF_GW, 0.5) == 1.0

    def test_beyond_cutoff(self):
        assert _compute_confidence(CUTOFF_GW + 5, 0.0) == 1.0

    def test_gw1_no_history(self):
        # base = 1/7 = 0.143, conf = 0.143 * 1.0 = 0.143
        result = _compute_confidence(1, 0.0)
        assert result == pytest.approx(1 / 7, rel=1e-3)

    def test_high_strength_can_cap_at_1(self):
        # base = 8/14 = 0.571, conf = 0.571 * 2.0 = 1.143 -> capped at 1.0
        result = _compute_confidence(8, 1.0)
        assert result == 1.0

    def test_gw0_treated_as_gw1(self):
        """Pre-season (GW 0) produces same confidence as GW 1, not zero."""
        assert _compute_confidence(0, 0.5) == _compute_confidence(1, 0.5)
        assert _compute_confidence(0, 0.0) > 0  # never zero


# ---------------------------------------------------------------------------
# generate_player_prior
# ---------------------------------------------------------------------------


class TestGeneratePlayerPrior:
    @patch("fpl_cli.services.player_prior._previous_season_label", return_value="2024-25")
    def test_player_with_history(self, _mock_season):
        """Player with qualifying history gets prior_strength from percentile rank."""
        profiles = {
            100: _make_profile(100, [_make_season(100, "2024-25", 180, 2700)]),  # 6.0 pts/90
            200: _make_profile(200, [_make_season(200, "2024-25", 90, 2700)]),   # 3.0 pts/90
            300: _make_profile(300, [_make_season(300, "2024-25", 135, 2700)]),  # 4.5 pts/90
        }
        players = [
            make_player(id=1, code=100, position=PlayerPosition.MIDFIELDER, now_cost=100),
            make_player(id=2, code=200, position=PlayerPosition.MIDFIELDER, now_cost=60),
            make_player(id=3, code=300, position=PlayerPosition.MIDFIELDER, now_cost=80),
        ]
        result = generate_player_prior(profiles, players, current_gw=3)

        # Player 1 (6.0 pts/90) is highest -> ~0.833 percentile
        assert result[1].source == "history"
        assert result[1].prior_strength > result[3].prior_strength > result[2].prior_strength

    @patch("fpl_cli.services.player_prior._previous_season_label", return_value="2024-25")
    def test_no_history_uses_price(self, _mock_season):
        """Player without qualifying history gets price-based prior_strength."""
        profiles = {}  # No vaastav data
        players = [
            make_player(id=1, code=100, position=PlayerPosition.MIDFIELDER, now_cost=120),
            make_player(id=2, code=200, position=PlayerPosition.MIDFIELDER, now_cost=45),
        ]
        result = generate_player_prior(profiles, players, current_gw=3)

        assert result[1].source == "price"
        assert result[2].source == "price"
        # Expensive player should have higher prior_strength
        assert result[1].prior_strength > result[2].prior_strength
        # Price-based capped at PRICE_CONFIDENCE_FACTOR
        assert result[1].prior_strength <= PRICE_CONFIDENCE_FACTOR

    @patch("fpl_cli.services.player_prior._previous_season_label", return_value="2024-25")
    def test_below_min_minutes_falls_to_price(self, _mock_season):
        """Player with < MIN_MINUTES last season uses price fallback."""
        profiles = {
            100: _make_profile(100, [_make_season(100, "2024-25", 30, 400)]),
        }
        players = [
            make_player(id=1, code=100, position=PlayerPosition.MIDFIELDER, now_cost=100),
        ]
        result = generate_player_prior(profiles, players, current_gw=3)
        assert result[1].source == "price"

    @patch("fpl_cli.services.player_prior._previous_season_label", return_value="2024-25")
    def test_cutoff_gw_confidence_is_1(self, _mock_season):
        """At cutoff GW, all players get confidence=1.0."""
        profiles = {
            100: _make_profile(100, [_make_season(100, "2024-25", 150, 2700)]),
        }
        players = [
            make_player(id=1, code=100, position=PlayerPosition.MIDFIELDER),
        ]
        result = generate_player_prior(profiles, players, current_gw=CUTOFF_GW)
        assert result[1].confidence == 1.0

    @patch("fpl_cli.services.player_prior._previous_season_label", return_value="2024-25")
    def test_empty_profiles_graceful(self, _mock_season):
        """Empty vaastav data -> all players get price-based priors."""
        players = [
            make_player(id=1, code=100, position=PlayerPosition.FORWARD, now_cost=100),
        ]
        result = generate_player_prior({}, players, current_gw=3)
        assert result[1].source == "price"
        assert result[1].confidence > 0

    @patch("fpl_cli.services.player_prior._previous_season_label", return_value="2024-25")
    def test_reliability_threaded_from_profile(self, _mock_season):
        """Player with history profile gets reliability from profile.reliability."""
        profiles = {100: _make_profile(100, [_make_season(100, "2024-25", 150, 2700)], reliability=0.85)}
        players = [make_player(id=1, code=100, position=PlayerPosition.MIDFIELDER, now_cost=100)]
        result = generate_player_prior(profiles, players, current_gw=3)
        assert result[1].reliability == pytest.approx(0.85)

    @patch("fpl_cli.services.player_prior._previous_season_label", return_value="2024-25")
    def test_no_profile_gets_none_reliability(self, _mock_season):
        """Player with no profile (price fallback) gets reliability=None."""
        profiles: dict = {}
        players = [make_player(id=1, code=100, position=PlayerPosition.MIDFIELDER, now_cost=100)]
        result = generate_player_prior(profiles, players, current_gw=3)
        assert result[1].reliability is None

    @patch("fpl_cli.services.player_prior._previous_season_label", return_value="2024-25")
    def test_zero_reliability_preserved_not_converted_to_none(self, _mock_season):
        """Profile with reliability=0.0 gets PlayerPrior.reliability==0.0, not None."""
        profiles = {100: _make_profile(100, [_make_season(100, "2024-25", 150, 2700)], reliability=0.0)}
        players = [make_player(id=1, code=100, position=PlayerPosition.MIDFIELDER, now_cost=100)]
        result = generate_player_prior(profiles, players, current_gw=3)
        assert result[1].reliability == 0.0
        assert result[1].reliability is not None

    @patch("fpl_cli.services.player_prior._previous_season_label", return_value="2024-25")
    def test_position_ranking_uses_current_fpl_position(self, _mock_season):
        """Percentile rank uses current FPL position, not historical vaastav position."""
        # Profile has MID position in history but player is now FWD
        sh = _make_season(100, "2024-25", 150, 2700)
        sh.position = "MID"
        profiles = {100: _make_profile(100, [sh])}
        players = [
            make_player(id=1, code=100, position=PlayerPosition.FORWARD, now_cost=100),
        ]
        result = generate_player_prior(profiles, players, current_gw=3)
        # Should still work - ranked against FWD peers (only one, so percentile=0.5)
        assert result[1].source == "history"


# ---------------------------------------------------------------------------
# Cache round-trip
# ---------------------------------------------------------------------------


class TestPriorCache:
    def test_save_and_load_roundtrip(self, tmp_path, monkeypatch):
        monkeypatch.setattr("fpl_cli.services.player_prior.prior_config_path", lambda: tmp_path / "prior.yaml")
        monkeypatch.setattr("fpl_cli.services.player_prior.season_label", lambda: "2025-26")

        priors = {
            1: PlayerPrior(prior_strength=0.75, confidence=0.58, source="history"),
            2: PlayerPrior(prior_strength=0.25, confidence=0.35, source="price"),
        }
        _save_prior_cache(priors, "2025-26", 3)
        loaded = load_cached_priors(3)

        assert loaded is not None
        assert loaded[1].prior_strength == 0.75
        assert loaded[1].confidence == 0.58
        assert loaded[1].source == "history"
        assert loaded[2].prior_strength == 0.25

    def test_stale_season_returns_none(self, tmp_path, monkeypatch):
        monkeypatch.setattr("fpl_cli.services.player_prior.prior_config_path", lambda: tmp_path / "prior.yaml")
        monkeypatch.setattr("fpl_cli.services.player_prior.season_label", lambda: "2025-26")

        priors = {1: PlayerPrior(prior_strength=0.5, confidence=0.5, source="history")}
        _save_prior_cache(priors, "2024-25", 3)  # Wrong season
        assert load_cached_priors(3) is None

    def test_stale_gw_returns_none(self, tmp_path, monkeypatch):
        monkeypatch.setattr("fpl_cli.services.player_prior.prior_config_path", lambda: tmp_path / "prior.yaml")
        monkeypatch.setattr("fpl_cli.services.player_prior.season_label", lambda: "2025-26")

        priors = {1: PlayerPrior(prior_strength=0.5, confidence=0.5, source="history")}
        _save_prior_cache(priors, "2025-26", 3)
        assert load_cached_priors(5) is None  # Different GW

    def test_missing_file_returns_none(self, tmp_path, monkeypatch):
        monkeypatch.setattr("fpl_cli.services.player_prior.prior_config_path", lambda: tmp_path / "nope.yaml")
        assert load_cached_priors(3) is None

    def test_reliability_cache_roundtrip(self, tmp_path, monkeypatch):
        """PlayerPrior.reliability survives save/load cycle."""
        monkeypatch.setattr("fpl_cli.services.player_prior.prior_config_path", lambda: tmp_path / "prior.yaml")
        monkeypatch.setattr("fpl_cli.services.player_prior.season_label", lambda: "2025-26")

        priors = {1: PlayerPrior(prior_strength=0.75, confidence=0.58, source="history", reliability=0.85)}
        _save_prior_cache(priors, "2025-26", 3)
        loaded = load_cached_priors(3)

        assert loaded is not None
        assert loaded[1].reliability == pytest.approx(0.85)

    def test_reliability_none_cache_roundtrip(self, tmp_path, monkeypatch):
        """PlayerPrior.reliability=None (no history) is preserved through cache."""
        monkeypatch.setattr("fpl_cli.services.player_prior.prior_config_path", lambda: tmp_path / "prior.yaml")
        monkeypatch.setattr("fpl_cli.services.player_prior.season_label", lambda: "2025-26")

        priors = {1: PlayerPrior(prior_strength=0.3, confidence=0.4, source="price", reliability=None)}
        _save_prior_cache(priors, "2025-26", 3)
        loaded = load_cached_priors(3)

        assert loaded is not None
        # None serialised as null in YAML; loaded back as None
        assert loaded[1].reliability is None

    def test_old_cache_without_reliability_loads_as_none(self, tmp_path, monkeypatch):
        """Cache files written before reliability field was added load without crashing."""
        monkeypatch.setattr("fpl_cli.services.player_prior.prior_config_path", lambda: tmp_path / "prior.yaml")
        monkeypatch.setattr("fpl_cli.services.player_prior.season_label", lambda: "2025-26")

        import yaml
        old_cache = {
            "metadata": {"season": "2025-26", "gameweek": 3},
            "priors": {1: {"prior_strength": 0.5, "confidence": 0.6, "source": "history"}},
        }
        with open(tmp_path / "prior.yaml", "w") as f:
            yaml.dump(old_cache, f)

        loaded = load_cached_priors(3)
        assert loaded is not None
        assert loaded[1].reliability is None


# ---------------------------------------------------------------------------
# observation_weight_range
# ---------------------------------------------------------------------------


class TestObservationWeightRange:
    """The band a blended quality score sits in, quoted by fpl stats --value."""

    def test_gw2_band(self):
        assert observation_weight_range(2) == pytest.approx((0.25, 0.5))

    def test_gw5_band(self):
        low, high = observation_weight_range(5)
        assert low == pytest.approx(5 / 11)
        assert high == pytest.approx(10 / 11)

    def test_saturates_at_the_cutoff(self):
        assert observation_weight_range(CUTOFF_GW) == (1.0, 1.0)


# ---------------------------------------------------------------------------
# load_or_generate_player_priors
# ---------------------------------------------------------------------------


def _provider(profiles=None, *, enter_error=None):
    provider = MagicMock()
    provider.get_all_player_histories = AsyncMock(return_value=profiles or {})
    provider.__aenter__ = AsyncMock(
        return_value=provider, side_effect=enter_error,
    )
    provider.__aexit__ = AsyncMock(return_value=False)
    return provider


class TestLoadOrGeneratePlayerPriors:
    """The one entry point for a command that scores against a prior."""

    async def test_returns_the_cache_when_current(self):
        cached = {1: PlayerPrior(prior_strength=0.7, confidence=0.5, source="history")}
        with (
            patch("fpl_cli.services.player_prior.load_cached_priors", return_value=cached),
            patch("fpl_cli.api.historical.make_historical_provider") as make_provider,
        ):
            result = await load_or_generate_player_priors([make_player(id=1)], 3)
        assert result == cached
        make_provider.assert_not_called()

    @patch("fpl_cli.services.player_prior._previous_season_label", return_value="2024-25")
    async def test_generates_and_caches_on_a_miss(self, _mock_season):
        players = [make_player(id=1, code=100, position=PlayerPosition.MIDFIELDER, now_cost=80)]
        profiles = {100: _make_profile(100, [_make_season(100, total_points=180, minutes=2700)])}
        with patch(
            "fpl_cli.api.historical.make_historical_provider",
            return_value=_provider(profiles),
        ):
            result = await load_or_generate_player_priors(players, 3)

        assert result is not None
        assert result[1].source == "history"
        reloaded = load_cached_priors(3)
        assert reloaded is not None
        assert reloaded[1] == result[1]

    async def test_unreachable_history_degrades_to_none(self):
        with patch(
            "fpl_cli.api.historical.make_historical_provider",
            return_value=_provider(enter_error=OSError("offline")),
        ):
            result = await load_or_generate_player_priors([make_player(id=1)], 3)
        assert result is None


# ---------------------------------------------------------------------------
# early_season_quality_warning
# ---------------------------------------------------------------------------


class TestEarlySeasonQualityWarning:
    """One notice for every command that shows a quality_score (PR #208 review)."""

    def test_blended_before_the_cutoff_is_prior_informed(self):
        warning = early_season_quality_warning(2, blended=True)
        assert warning is not None
        assert warning["code"] == "early_season_prior_informed"
        assert "25%-50%" in warning["message"]  # observation_weight_range(2)
        assert f"GW{CUTOFF_GW}" in warning["message"]
        assert "ep_next" in warning["message"]

    def test_blended_notice_ends_at_the_cutoff(self):
        assert early_season_quality_warning(CUTOFF_GW - 1, blended=True) is not None
        assert early_season_quality_warning(CUTOFF_GW, blended=True) is None

    def test_unblended_before_gw6_is_small_sample(self):
        warning = early_season_quality_warning(2, blended=False)
        assert warning is not None
        assert warning["code"] == "early_season_small_sample"
        assert "could not be loaded" in warning["message"]
        assert "ep_next" in warning["message"]

    def test_unblended_notice_ends_at_gw6(self):
        from fpl_cli.services.scoring import MINS_FACTOR_START_GW
        assert early_season_quality_warning(MINS_FACTOR_START_GW, blended=False) is not None
        assert early_season_quality_warning(MINS_FACTOR_START_GW + 1, blended=False) is None

    def test_names_the_score_the_caller_actually_shows(self):
        """A notice pointing at quality_score on a page that only has
        target_score sends the reader looking for a column that is not there.
        """
        blended = early_season_quality_warning(
            2, blended=True, score_names=("target_score",),
        )
        assert blended is not None
        assert "target_score" in blended["message"]
        assert "quality_score" not in blended["message"]

        unblended = early_season_quality_warning(
            2, blended=False, score_names=("waiver_score",),
        )
        assert unblended is not None
        assert "waiver_score is pure observation" in unblended["message"]

    def test_two_score_names_read_as_a_plural_sentence(self):
        """fpl transfer-eval shows one blended score from each family."""
        warning = early_season_quality_warning(
            2, blended=True, score_names=("quality_score", "target_score"),
        )
        assert warning is not None
        assert "quality_score and target_score blend this season's" in warning["message"]
        assert "as prior-informed estimates, not measurements" in warning["message"]

    def test_the_codes_do_not_vary_with_the_score_named(self):
        """One rule for a consumer keying on the code: the condition and the
        device are the same whichever family's score is on the page.
        """
        for names in (("quality_score",), ("waiver_score",), ("quality_score", "target_score")):
            assert early_season_quality_warning(
                2, blended=True, score_names=names,
            )["code"] == "early_season_prior_informed"
            assert early_season_quality_warning(
                2, blended=False, score_names=names,
            )["code"] == "early_season_small_sample"

    def test_the_two_notices_never_share_a_code(self):
        blended = early_season_quality_warning(3, blended=True)
        unblended = early_season_quality_warning(3, blended=False)
        assert blended is not None and unblended is not None
        assert blended["code"] != unblended["code"]
