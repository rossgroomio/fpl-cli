"""Tests for HistoricalDataProvider composition layer."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from fpl_cli.api.historical import HistoricalDataProvider, make_historical_provider
from fpl_cli.api.historical_types import GwTrendProfile, PlayerProfile, SeasonHistory


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
