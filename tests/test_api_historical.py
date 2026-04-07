"""Tests for HistoricalDataProvider composition layer."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from fpl_cli.api.historical import HistoricalDataProvider, make_historical_provider
from fpl_cli.api.historical_types import GwTrendProfile, PlayerProfile, SeasonHistory, compute_reliability


@pytest.fixture(autouse=True)
def _reset_session_caches():
    """Clear all session-level caches between tests."""
    HistoricalDataProvider._session_profiles = None
    yield
    HistoricalDataProvider._session_profiles = None


def _make_season(code: int, season: str, pts: int = 150, minutes: int = 2700) -> SeasonHistory:
    return SeasonHistory(
        element_code=code, season=season, total_points=pts, minutes=minutes,
        starts=30, goals=10, assists=5, expected_goals=9.0, expected_assists=4.5,
        expected_goal_involvements=13.5, start_cost=80, end_cost=90,
        position="MID", web_name="TestPlayer", team_id=1,
    )


def _make_profile(code: int, seasons: list[SeasonHistory]) -> PlayerProfile:
    return PlayerProfile(
        element_code=code, web_name=seasons[-1].web_name if seasons else "???",
        current_position="MID", seasons=seasons,
        pts_per_90=[5.0] * len(seasons), pts_per_90_trend=0.0,
    )


def _make_trend(element: int = 100, **kwargs) -> GwTrendProfile:
    defaults = dict(
        web_name="Salah", position="MID", team_name="Liverpool",
        price_start=130, price_current=135, price_change=5,
        price_slope=1.0, price_acceleration=0.1, transfer_momentum=50000,
        gw_count=6, latest_gw=6, first_gw=1,
    )
    defaults.update(kwargs)
    return GwTrendProfile(element=element, **defaults)


def _mock_clients(vaastav_profiles=None, ci_profiles=None, ci_trends=None):
    """Create mock vaastav and core-insights clients."""
    vaastav = MagicMock()
    vaastav.get_all_player_histories = AsyncMock(return_value=vaastav_profiles or {})

    ci = MagicMock()
    ci.get_all_player_histories = AsyncMock(return_value=ci_profiles or {})
    ci.get_gw_trends = AsyncMock(return_value=ci_trends or {})
    ci._build_profile = MagicMock(side_effect=_make_profile)

    return vaastav, ci


def _make_sh(code: int, season: str, starts: int, minutes: int = 2700) -> SeasonHistory:
    return SeasonHistory(
        element_code=code, season=season, total_points=100, minutes=minutes,
        starts=starts, goals=0, assists=0, expected_goals=0.0, expected_assists=0.0,
        expected_goal_involvements=0.0, start_cost=80, end_cost=90,
        position="MID", web_name="Test", team_id=1,
    )


class TestComputeReliability:
    def test_three_seasons_weighted_average(self):
        """Standard 3-season case with default (3,2,1) weights."""
        seasons = [
            _make_sh(1, "2022-23", starts=25),
            _make_sh(1, "2023-24", starts=30),
            _make_sh(1, "2024-25", starts=35),
        ]
        # Oldest=25, middle=30, newest=35; weights oldest->newest = (1,2,3)
        # weighted = (25*1 + 30*2 + 35*3) / (38*6) = (25+60+105)/228 = 190/228
        result = compute_reliability(seasons)
        assert result == pytest.approx(190 / 228, rel=1e-4)

    def test_single_season_full_starts(self):
        seasons = [_make_sh(1, "2024-25", starts=38)]
        assert compute_reliability(seasons) == pytest.approx(1.0)

    def test_no_seasons_returns_none(self):
        assert compute_reliability([]) is None

    def test_current_season_normalised_denominator(self):
        """Current season at GW20 uses starts/20, not starts/38."""
        seasons = [_make_sh(1, "2025-26", starts=18)]
        result = compute_reliability(seasons, current_season="2025-26", current_gw=20)
        assert result == pytest.approx(18 / 20)

    def test_current_season_excluded_before_gw10(self):
        """Current season excluded when current_gw < 10."""
        seasons = [
            _make_sh(1, "2024-25", starts=35),
            _make_sh(1, "2025-26", starts=5),
        ]
        result = compute_reliability(seasons, current_season="2025-26", current_gw=5)
        # Only 2024-25 counts, single season weight = (1,) = 35/38
        assert result == pytest.approx(35 / 38)

    def test_four_seasons_uses_three_most_recent(self):
        """Oldest season dropped when 4+ seasons provided."""
        seasons = [
            _make_sh(1, "2021-22", starts=10),  # dropped
            _make_sh(1, "2022-23", starts=25),
            _make_sh(1, "2023-24", starts=30),
            _make_sh(1, "2024-25", starts=35),
        ]
        result_4 = compute_reliability(seasons)
        result_3 = compute_reliability(seasons[1:])
        assert result_4 == result_3

    def test_starts_exceeding_38_clamped_to_1(self):
        """DGW season: starts > 38 clamps to 1.0."""
        seasons = [_make_sh(1, "2024-25", starts=42)]
        assert compute_reliability(seasons) == 1.0

    def test_two_season_weights_truncated(self):
        """2-season player uses first 2 weights (3, 2); newest gets 3."""
        seasons = [
            _make_sh(1, "2023-24", starts=20),
            _make_sh(1, "2024-25", starts=30),
        ]
        # weights[:2] = (3,2); reversed = (2,3); oldest*2, newest*3; total weight=5
        result = compute_reliability(seasons)
        assert result == pytest.approx((20 * 2 + 30 * 3) / (38 * 5), rel=1e-4)

    def test_sub_only_player_contributes_zero(self):
        """Player with 0 starts but minutes >= 450 still contributes 0.0 for that season."""
        seasons = [_make_sh(1, "2024-25", starts=0, minutes=900)]
        result = compute_reliability(seasons)
        assert result == 0.0

    def test_injury_shortened_included_not_excluded(self):
        """Season with few starts/minutes IS included (not MIN_MINUTES filtered)."""
        seasons = [_make_sh(1, "2024-25", starts=5, minutes=200)]
        result = compute_reliability(seasons)
        assert result == pytest.approx(5 / 38, rel=1e-4)


class TestMergedHistories:
    async def test_4_season_merge(self):
        """Profiles from both sources merged into single profile per player."""
        vaastav_seasons = [
            _make_season(100, "2022-23"), _make_season(100, "2023-24"),
            _make_season(100, "2024-25"),
        ]
        ci_seasons = [_make_season(100, "2025-26")]

        vaastav, ci = _mock_clients(
            vaastav_profiles={100: _make_profile(100, vaastav_seasons)},
            ci_profiles={100: _make_profile(100, ci_seasons)},
        )
        provider = HistoricalDataProvider(vaastav, ci)
        profiles = await provider.get_all_player_histories()

        assert 100 in profiles
        assert len(profiles[100].seasons) == 4

    async def test_player_only_in_vaastav(self):
        """Player from historical seasons only (retired/transferred out)."""
        vaastav, ci = _mock_clients(
            vaastav_profiles={100: _make_profile(100, [_make_season(100, "2023-24")])},
            ci_profiles={},
        )
        provider = HistoricalDataProvider(vaastav, ci)
        profiles = await provider.get_all_player_histories()

        assert 100 in profiles
        assert len(profiles[100].seasons) == 1

    async def test_player_only_in_core_insights(self):
        """New player with no historical data."""
        vaastav, ci = _mock_clients(
            vaastav_profiles={},
            ci_profiles={200: _make_profile(200, [_make_season(200, "2025-26")])},
        )
        provider = HistoricalDataProvider(vaastav, ci)
        profiles = await provider.get_all_player_histories()

        assert 200 in profiles
        assert len(profiles[200].seasons) == 1

    async def test_no_duplicate_seasons(self):
        """Same element_code from both sources - seasons should not duplicate."""
        s1 = _make_season(100, "2024-25")
        s2 = _make_season(100, "2025-26")

        vaastav, ci = _mock_clients(
            vaastav_profiles={100: _make_profile(100, [s1])},
            ci_profiles={100: _make_profile(100, [s2])},
        )
        provider = HistoricalDataProvider(vaastav, ci)
        profiles = await provider.get_all_player_histories()

        season_labels = [s.season for s in profiles[100].seasons]
        assert sorted(season_labels) == ["2024-25", "2025-26"]

    async def test_session_cache(self):
        """Second call returns cached data."""
        vaastav, ci = _mock_clients(
            vaastav_profiles={100: _make_profile(100, [_make_season(100, "2024-25")])},
        )
        provider = HistoricalDataProvider(vaastav, ci)
        p1 = await provider.get_all_player_histories()
        p2 = await provider.get_all_player_histories()

        assert p1 is p2
        assert vaastav.get_all_player_histories.call_count == 1

    async def test_get_player_history(self):
        """Single player lookup delegates to merged data."""
        vaastav, ci = _mock_clients(
            vaastav_profiles={100: _make_profile(100, [_make_season(100, "2024-25")])},
        )
        provider = HistoricalDataProvider(vaastav, ci)
        profile = await provider.get_player_history(100)
        assert profile is not None
        assert profile.element_code == 100

    async def test_get_player_history_not_found(self):
        vaastav, ci = _mock_clients()
        provider = HistoricalDataProvider(vaastav, ci)
        assert await provider.get_player_history(99999) is None


class TestGwTrends:
    async def test_delegates_to_core_insights(self):
        """GW trends come from Core-Insights only."""
        trends = {100: _make_trend(100)}
        vaastav, ci = _mock_clients(ci_trends=trends)
        provider = HistoricalDataProvider(vaastav, ci)

        result = await provider.get_gw_trends()
        assert result == trends
        ci.get_gw_trends.assert_called_once_with(last_n=None)

    async def test_last_n_passed_through(self):
        vaastav, ci = _mock_clients(ci_trends={})
        provider = HistoricalDataProvider(vaastav, ci)
        await provider.get_gw_trends(last_n=5)
        ci.get_gw_trends.assert_called_once_with(last_n=5)


class TestContextManager:
    async def test_closes_both_fetchers(self):
        """Context manager closes both fetchers even if one raises."""
        with (
            patch("fpl_cli.api.vaastav.make_vaastav_fetcher") as mock_vf,
            patch("fpl_cli.api.core_insights.make_core_insights_fetcher") as mock_cf,
            patch("fpl_cli.api.vaastav.VaastavClient"),
            patch("fpl_cli.api.core_insights.CoreInsightsClient"),
        ):
            vaastav_fetcher = MagicMock()
            vaastav_fetcher.close = AsyncMock()
            ci_fetcher = MagicMock()
            ci_fetcher.close = AsyncMock()
            mock_vf.return_value = vaastav_fetcher
            mock_cf.return_value = ci_fetcher

            async with make_historical_provider():
                pass

            vaastav_fetcher.close.assert_called_once()
            ci_fetcher.close.assert_called_once()

    async def test_closes_second_fetcher_even_if_first_raises(self):
        """If first fetcher.close() raises, second still closes."""
        with (
            patch("fpl_cli.api.vaastav.make_vaastav_fetcher") as mock_vf,
            patch("fpl_cli.api.core_insights.make_core_insights_fetcher") as mock_cf,
            patch("fpl_cli.api.vaastav.VaastavClient"),
            patch("fpl_cli.api.core_insights.CoreInsightsClient"),
        ):
            vaastav_fetcher = MagicMock()
            vaastav_fetcher.close = AsyncMock()
            ci_fetcher = MagicMock()
            ci_fetcher.close = AsyncMock(side_effect=RuntimeError("close failed"))
            mock_vf.return_value = vaastav_fetcher
            mock_cf.return_value = ci_fetcher

            async with make_historical_provider():
                pass

            ci_fetcher.close.assert_called_once()
            vaastav_fetcher.close.assert_called_once()
