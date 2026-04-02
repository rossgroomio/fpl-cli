"""Tests for player-level Bayesian prior generation."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from fpl_cli.api.vaastav import PlayerProfile, SeasonHistory
from fpl_cli.models.player import PlayerPosition
from fpl_cli.services.player_prior import (
    CUTOFF_GW,
    PRICE_CONFIDENCE_FACTOR,
    PlayerPrior,
    _compute_confidence,
    _extract_prev_season_pts_per_90,
    _percentile_rank,
    _save_prior_cache,
    generate_player_prior,
    load_cached_priors,
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
) -> PlayerProfile:
    return PlayerProfile(element_code=code, web_name="TestPlayer", current_position="MID", seasons=seasons or [])


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
# _percentile_rank
# ---------------------------------------------------------------------------


class TestPercentileRank:
    def test_middle_value(self):
        assert _percentile_rank(3.0, [1.0, 2.0, 3.0, 4.0, 5.0]) == pytest.approx(0.5)

    def test_lowest_value(self):
        assert _percentile_rank(1.0, [1.0, 2.0, 3.0, 4.0, 5.0]) == pytest.approx(0.1)

    def test_highest_value(self):
        assert _percentile_rank(5.0, [1.0, 2.0, 3.0, 4.0, 5.0]) == pytest.approx(0.9)

    def test_single_value(self):
        assert _percentile_rank(5.0, [5.0]) == 0.5

    def test_all_equal(self):
        assert _percentile_rank(3.0, [3.0, 3.0, 3.0]) == 0.5


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
        monkeypatch.setattr("fpl_cli.services.player_prior.PRIOR_CONFIG_PATH", tmp_path / "prior.yaml")
        monkeypatch.setattr("fpl_cli.services.player_prior.vaastav_season", lambda: "2025-26")

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
        monkeypatch.setattr("fpl_cli.services.player_prior.PRIOR_CONFIG_PATH", tmp_path / "prior.yaml")
        monkeypatch.setattr("fpl_cli.services.player_prior.vaastav_season", lambda: "2025-26")

        priors = {1: PlayerPrior(prior_strength=0.5, confidence=0.5, source="history")}
        _save_prior_cache(priors, "2024-25", 3)  # Wrong season
        assert load_cached_priors(3) is None

    def test_stale_gw_returns_none(self, tmp_path, monkeypatch):
        monkeypatch.setattr("fpl_cli.services.player_prior.PRIOR_CONFIG_PATH", tmp_path / "prior.yaml")
        monkeypatch.setattr("fpl_cli.services.player_prior.vaastav_season", lambda: "2025-26")

        priors = {1: PlayerPrior(prior_strength=0.5, confidence=0.5, source="history")}
        _save_prior_cache(priors, "2025-26", 3)
        assert load_cached_priors(5) is None  # Different GW

    def test_missing_file_returns_none(self, tmp_path, monkeypatch):
        monkeypatch.setattr("fpl_cli.services.player_prior.PRIOR_CONFIG_PATH", tmp_path / "nope.yaml")
        assert load_cached_priors(3) is None
