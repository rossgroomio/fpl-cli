"""Tests for VaastavClient CSV fetching and parsing."""
from __future__ import annotations

import logging
from datetime import timedelta
from pathlib import Path
from unittest.mock import MagicMock

import pytest
import respx
from httpx import Response

from fpl_cli.api.dataset_fetcher import DatasetFetcher
from fpl_cli.api.historical_types import compute_acceleration, compute_trend
from fpl_cli.api.vaastav import BASE_URL, VaastavClient


@pytest.fixture(autouse=True)
def _reset_vaastav_session_cache():
    """Clear session-level cache between tests."""
    VaastavClient._session_profiles = {}
    yield
    VaastavClient._session_profiles = {}


def _make_fetcher(tmp_path: Path) -> DatasetFetcher:
    """DatasetFetcher configured for vaastav tests with tmp_path cache."""
    return DatasetFetcher(
        base_url=BASE_URL,
        cache_dir=tmp_path / "cache",
        ttl=timedelta(hours=4),
    )


def _stub_fetcher() -> MagicMock:
    """MagicMock fetcher for tests that never call get()."""
    return MagicMock(spec=DatasetFetcher)


# Minimal CSV that mirrors players_raw.csv columns we use
# `team` is the season-local id, `team_code` the stable club code the parser
# reads; they differ here so a test can tell which column was used.
SAMPLE_CSV = (
    "code,web_name,element_type,team,team_code,total_points,minutes,starts,"
    "goals_scored,assists,expected_goals,expected_assists,"
    "expected_goal_involvements,now_cost,cost_change_start\n"
    "80201,Salah,3,11,14,265,2800,31,19,13,17.5,10.2,27.7,130,5\n"
    "206325,Haaland,4,12,43,220,2500,28,25,5,22.0,3.5,25.5,150,10\n"
)

SAMPLE_CSV_SEASON2 = (
    "code,web_name,element_type,team,team_code,total_points,minutes,starts,"
    "goals_scored,assists,expected_goals,expected_assists,"
    "expected_goal_involvements,now_cost,cost_change_start\n"
    "80201,Salah,3,11,14,230,2600,29,15,11,14.0,9.0,23.0,125,0\n"
    "206325,Haaland,4,12,43,200,2200,25,22,3,20.0,2.5,22.5,140,5\n"
)

BASE = VaastavClient.BASE_URL


class TestVaastavClientParsing:
    @respx.mock
    async def test_fetch_season_data_parses_csv(self, tmp_path):
        """CSV rows are parsed into SeasonHistory dataclasses."""
        respx.get(f"{BASE}/2024-25/players_raw.csv").mock(
            return_value=Response(200, text=SAMPLE_CSV)
        )
        async with VaastavClient(_make_fetcher(tmp_path), seasons=("2024-25",)) as client:
            data = await client._fetch_season_data()

        assert "2024-25" in data
        rows = data["2024-25"]
        assert len(rows) == 2
        salah = [r for r in rows if r.element_code == 80201][0]
        assert salah.web_name == "Salah"
        assert salah.total_points == 265
        assert salah.minutes == 2800
        assert salah.starts == 31
        assert salah.goals == 19
        assert salah.assists == 13
        assert salah.expected_goals == 17.5
        assert salah.start_cost == 125  # 130 - 5
        assert salah.end_cost == 130
        assert salah.position == "MID"
        assert salah.season == "2024-25"
        assert salah.team_id == 14  # the club code, as Core-Insights carries it, not `team`

    @respx.mock
    async def test_position_mapping(self, tmp_path):
        """element_type integers map to position strings."""
        csv = (
            "code,web_name,element_type,team,team_code,total_points,minutes,starts,"
            "goals_scored,assists,expected_goals,expected_assists,"
            "expected_goal_involvements,now_cost,cost_change_start\n"
            "1,GK,1,1,3,100,1800,20,0,0,0,0,0,45,0\n"
            "2,DEF,2,1,3,80,1600,18,2,1,1.5,0.8,2.3,50,0\n"
            "3,MID,3,1,3,120,2000,22,5,8,4.0,7.0,11.0,70,0\n"
            "4,FWD,4,1,3,150,2200,24,15,3,13.0,2.5,15.5,90,0\n"
        )
        respx.get(f"{BASE}/2024-25/players_raw.csv").mock(
            return_value=Response(200, text=csv)
        )
        async with VaastavClient(_make_fetcher(tmp_path), seasons=("2024-25",)) as client:
            data = await client._fetch_season_data()
        positions = {r.element_code: r.position for r in data["2024-25"]}
        assert positions == {1: "GK", 2: "DEF", 3: "MID", 4: "FWD"}

    @respx.mock
    async def test_in_memory_cache(self, tmp_path):
        """Second call uses cached data, no extra HTTP request."""
        route = respx.get(f"{BASE}/2024-25/players_raw.csv").mock(
            return_value=Response(200, text=SAMPLE_CSV)
        )
        async with VaastavClient(_make_fetcher(tmp_path), seasons=("2024-25",)) as client:
            await client._fetch_season_data()
            await client._fetch_season_data()
        assert route.call_count == 1


class TestSignalComputation:
    def test_per_90(self):
        """Per-90 calculation matches UnderstatClient convention."""
        client = VaastavClient(_stub_fetcher())
        assert client._per_90(10.0, 900) == 1.0
        assert client._per_90(5.0, 1800) == 0.25
        assert client._per_90(0.0, 900) == 0.0
        assert client._per_90(10.0, 0) == 0.0

    def test_compute_trend_three_points(self):
        """Least-squares trend with 3 data points."""
        assert compute_trend([4.0, 5.0, 6.0]) == pytest.approx(1.0)

    def test_compute_trend_two_points(self):
        """Trend with 2 data points is just the difference."""
        assert compute_trend([4.0, 6.0]) == pytest.approx(2.0)

    def test_compute_trend_one_point(self):
        """Single data point has no trend."""
        assert compute_trend([4.0]) == 0.0

    def test_compute_trend_empty(self):
        """Empty list has no trend."""
        assert compute_trend([]) == 0.0

    def test_compute_trend_declining(self):
        """Declining values produce negative trend."""
        assert compute_trend([6.0, 4.0, 2.0]) == pytest.approx(-2.0)

    @respx.mock
    async def test_build_profile_computes_signals(self, tmp_path):
        """PlayerProfile has correct computed signals."""
        respx.get(f"{BASE}/2023-24/players_raw.csv").mock(
            return_value=Response(200, text=SAMPLE_CSV_SEASON2)
        )
        respx.get(f"{BASE}/2024-25/players_raw.csv").mock(
            return_value=Response(200, text=SAMPLE_CSV)
        )
        async with VaastavClient(_make_fetcher(tmp_path), seasons=("2023-24", "2024-25")) as client:
            profile = await client.get_player_history(80201)

        assert profile is not None
        assert profile.element_code == 80201
        assert profile.web_name == "Salah"
        assert len(profile.seasons) == 2
        assert len(profile.pts_per_90) == 2
        # Season 1: 230pts / (2600/90) = 7.96, Season 2: 265pts / (2800/90) = 8.52
        assert profile.pts_per_90[0] == pytest.approx(7.96, abs=0.01)
        assert profile.pts_per_90[1] == pytest.approx(8.52, abs=0.01)
        assert profile.pts_per_90_trend > 0  # improving
        assert profile.xgi_per_90_trend is not None  # has xG data both seasons
        assert len(profile.minutes_per_start) == 2

    @respx.mock
    async def test_build_profile_sets_reliability(self, tmp_path):
        """reliability field is populated from starts data (no MIN_MINUTES filter)."""
        respx.get(f"{BASE}/2023-24/players_raw.csv").mock(
            return_value=Response(200, text=SAMPLE_CSV_SEASON2)
        )
        respx.get(f"{BASE}/2024-25/players_raw.csv").mock(
            return_value=Response(200, text=SAMPLE_CSV)
        )
        async with VaastavClient(_make_fetcher(tmp_path), seasons=("2023-24", "2024-25")) as client:
            profile = await client.get_player_history(80201)

        assert profile is not None
        # Salah: 2023-24 starts=29, 2024-25 starts=31; weights[:2]=(3,2); reversed=(2,3)
        # oldest*2 + newest*3 / (38*5) = (29*2 + 31*3) / (38*5) = (58+93)/190
        assert profile.reliability == pytest.approx((29 * 2 + 31 * 3) / (38 * 5), rel=1e-3)

    @respx.mock
    async def test_reliability_includes_injury_shortened_season(self, tmp_path):
        """Low-minute season counts toward reliability (not MIN_MINUTES filtered)."""
        low_minutes_csv = (
            "code,web_name,element_type,team,team_code,total_points,minutes,starts,"
            "goals_scored,assists,expected_goals,expected_assists,"
            "expected_goal_involvements,now_cost,cost_change_start\n"
            "80201,Salah,3,11,14,20,300,4,1,0,0.8,0.2,1.0,130,0\n"
        )
        respx.get(f"{BASE}/2023-24/players_raw.csv").mock(
            return_value=Response(200, text=low_minutes_csv)
        )
        respx.get(f"{BASE}/2024-25/players_raw.csv").mock(
            return_value=Response(200, text=SAMPLE_CSV)
        )
        async with VaastavClient(_make_fetcher(tmp_path), seasons=("2023-24", "2024-25")) as client:
            profile = await client.get_player_history(80201)

        assert profile is not None
        # 2 seasons in data, even though 2023-24 has <450 min
        assert len(profile.seasons) == 2
        # Both count for reliability: injury season (4 starts) IS included
        assert profile.reliability is not None
        assert profile.reliability == pytest.approx((4 * 2 + 31 * 3) / (38 * 5), rel=1e-3)

    @respx.mock
    async def test_season_below_450_minutes_excluded_from_trend(self, tmp_path):
        """Seasons with <450 minutes are excluded from signal computation."""
        low_minutes_csv = (
            "code,web_name,element_type,team,team_code,total_points,minutes,starts,"
            "goals_scored,assists,expected_goals,expected_assists,"
            "expected_goal_involvements,now_cost,cost_change_start\n"
            "80201,Salah,3,11,14,20,300,4,1,0,0.8,0.2,1.0,130,0\n"
        )
        respx.get(f"{BASE}/2023-24/players_raw.csv").mock(
            return_value=Response(200, text=low_minutes_csv)
        )
        respx.get(f"{BASE}/2024-25/players_raw.csv").mock(
            return_value=Response(200, text=SAMPLE_CSV)
        )
        async with VaastavClient(_make_fetcher(tmp_path), seasons=("2023-24", "2024-25")) as client:
            profile = await client.get_player_history(80201)

        assert profile is not None
        assert len(profile.seasons) == 2  # both seasons shown
        assert len(profile.pts_per_90) == 1  # only 1 qualifying season

    @respx.mock
    async def test_xgi_trend_none_with_insufficient_data(self, tmp_path):
        """xgi_per_90_trend is None when <2 seasons have xG data."""
        no_xg_csv = (
            "code,web_name,element_type,team,team_code,total_points,minutes,starts,"
            "goals_scored,assists,expected_goals,expected_assists,"
            "expected_goal_involvements,now_cost,cost_change_start\n"
            "80201,Salah,3,11,14,200,2500,28,10,8,0,0,0,120,0\n"
        )
        respx.get(f"{BASE}/2023-24/players_raw.csv").mock(
            return_value=Response(200, text=no_xg_csv)
        )
        respx.get(f"{BASE}/2024-25/players_raw.csv").mock(
            return_value=Response(200, text=SAMPLE_CSV)
        )
        async with VaastavClient(_make_fetcher(tmp_path), seasons=("2023-24", "2024-25")) as client:
            profile = await client.get_player_history(80201)

        assert profile is not None
        assert len(profile.xgi_per_90) == 1
        assert profile.xgi_per_90_trend is None

    @respx.mock
    async def test_player_not_found(self, tmp_path):
        """Unknown element_code returns None."""
        respx.get(f"{BASE}/2024-25/players_raw.csv").mock(
            return_value=Response(200, text=SAMPLE_CSV)
        )
        async with VaastavClient(_make_fetcher(tmp_path), seasons=("2024-25",)) as client:
            profile = await client.get_player_history(99999)
        assert profile is None

    @respx.mock
    async def test_get_all_player_histories(self, tmp_path):
        """Batch returns profiles keyed by element_code."""
        respx.get(f"{BASE}/2024-25/players_raw.csv").mock(
            return_value=Response(200, text=SAMPLE_CSV)
        )
        async with VaastavClient(_make_fetcher(tmp_path), seasons=("2024-25",)) as client:
            profiles = await client.get_all_player_histories()

        assert 80201 in profiles  # Salah
        assert 206325 in profiles  # Haaland
        assert profiles[80201].web_name == "Salah"
        assert profiles[206325].current_position == "FWD"

    @respx.mock
    async def test_session_cache_is_keyed_by_the_season_window(self, tmp_path):
        # A client on one window running first must not answer for a client
        # on another with the seasons it happened to load.
        respx.get(f"{BASE}/2023-24/players_raw.csv").mock(
            return_value=Response(200, text=SAMPLE_CSV_SEASON2)
        )
        respx.get(f"{BASE}/2024-25/players_raw.csv").mock(
            return_value=Response(200, text=SAMPLE_CSV)
        )
        async with VaastavClient(_make_fetcher(tmp_path), seasons=("2024-25",)) as one:
            one_profiles = await one.get_all_player_histories()
        async with VaastavClient(
            _make_fetcher(tmp_path), seasons=("2023-24", "2024-25")
        ) as two:
            two_profiles = await two.get_all_player_histories()

        assert [s.season for s in one_profiles[80201].seasons] == ["2024-25"]
        assert [s.season for s in two_profiles[80201].seasons] == ["2023-24", "2024-25"]
        assert await two.get_all_player_histories() is two_profiles


# --- Gameweek-level trend data ---

# Columns matching merged_gw.csv (subset we use)
_GW_HEADER = (
    "name,position,team,element,round,value,transfers_balance,"
    "transfers_in,transfers_out,total_points,minutes,fixture,xP\n"
)

SAMPLE_GW_CSV = _GW_HEADER + (
    "Salah,MID,Liverpool,100,1,130,50000,80000,30000,12,90,1,8.5\n"
    "Salah,MID,Liverpool,100,2,131,40000,70000,30000,8,90,2,7.0\n"
    "Salah,MID,Liverpool,100,3,132,30000,60000,30000,5,90,3,6.5\n"
    "Salah,MID,Liverpool,100,4,133,20000,55000,35000,10,90,4,7.5\n"
    "Salah,MID,Liverpool,100,5,134,35000,65000,30000,7,90,5,7.0\n"
    "Salah,MID,Liverpool,100,6,135,45000,75000,30000,9,90,6,8.0\n"
    "Haaland,FWD,Manchester City,200,1,150,60000,90000,30000,15,90,1,9.0\n"
    "Haaland,FWD,Manchester City,200,2,150,30000,50000,20000,6,90,2,7.5\n"
    "Haaland,FWD,Manchester City,200,3,149,-10000,20000,30000,2,90,3,6.0\n"
    "Haaland,FWD,Manchester City,200,4,148,-20000,15000,35000,4,90,4,5.5\n"
    "Haaland,FWD,Manchester City,200,5,148,-5000,25000,30000,8,90,5,7.0\n"
    "Haaland,FWD,Manchester City,200,6,147,-15000,10000,25000,3,90,6,5.0\n"
)

# DGW: Salah has two fixtures in round 3
SAMPLE_DGW_CSV = _GW_HEADER + (
    "Salah,MID,Liverpool,100,1,130,50000,80000,30000,12,90,1,8.5\n"
    "Salah,MID,Liverpool,100,2,131,40000,70000,30000,8,90,2,7.0\n"
    "Salah,MID,Liverpool,100,3,132,30000,60000,30000,5,45,3,6.5\n"
    "Salah,MID,Liverpool,100,3,132,30000,60000,30000,7,90,4,6.5\n"
    "Salah,MID,Liverpool,100,4,133,20000,55000,35000,10,90,5,7.5\n"
)

# Mid-season joiner: only has GW3-6
SAMPLE_JOINER_CSV = _GW_HEADER + (
    "NewGuy,FWD,West Ham,300,3,55,10000,15000,5000,4,90,3,5.0\n"
    "NewGuy,FWD,West Ham,300,4,56,15000,20000,5000,6,90,4,5.5\n"
    "NewGuy,FWD,West Ham,300,5,57,20000,25000,5000,8,90,5,6.0\n"
    "NewGuy,FWD,West Ham,300,6,58,25000,30000,5000,5,90,6,5.5\n"
)


class TestGwTrendParsing:
    @respx.mock
    async def test_basic_parsing(self, tmp_path):
        """merged_gw.csv rows are parsed into GwTrendProfile objects."""
        respx.get(f"{BASE}/2025-26/gws/merged_gw.csv").mock(
            return_value=Response(200, text=SAMPLE_GW_CSV)
        )
        async with VaastavClient(_make_fetcher(tmp_path), seasons=("2025-26",)) as client:
            trends = await client.get_gw_trends()

        assert len(trends) == 2
        assert 100 in trends  # Salah
        assert 200 in trends  # Haaland

        salah = trends[100]
        assert salah.web_name == "Salah"
        assert salah.position == "MID"
        assert salah.team_name == "Liverpool"
        assert salah.element == 100
        assert salah.price_start == 130
        assert salah.price_current == 135
        assert salah.price_change == 5
        assert salah.gw_count == 6
        assert salah.latest_gw == 6
        assert salah.first_gw == 1

    @respx.mock
    async def test_haaland_falling_price(self, tmp_path):
        """Player with falling price has negative price_change and slope."""
        respx.get(f"{BASE}/2025-26/gws/merged_gw.csv").mock(
            return_value=Response(200, text=SAMPLE_GW_CSV)
        )
        async with VaastavClient(_make_fetcher(tmp_path), seasons=("2025-26",)) as client:
            trends = await client.get_gw_trends()

        haaland = trends[200]
        assert haaland.price_start == 150
        assert haaland.price_current == 147
        assert haaland.price_change == -3
        assert haaland.price_slope < 0

    @respx.mock
    async def test_dgw_deduplication(self, tmp_path):
        """DGW rows are deduplicated - only one entry per round per player."""
        respx.get(f"{BASE}/2025-26/gws/merged_gw.csv").mock(
            return_value=Response(200, text=SAMPLE_DGW_CSV)
        )
        async with VaastavClient(_make_fetcher(tmp_path), seasons=("2025-26",)) as client:
            trends = await client.get_gw_trends()

        salah = trends[100]
        assert salah.gw_count == 4  # rounds 1,2,3,4 (not 5 rows)
        assert salah.price_start == 130
        assert salah.price_current == 133
        assert salah.latest_gw == 4

    @respx.mock
    async def test_mid_season_joiner(self, tmp_path):
        """Player joining mid-season uses first available GW as baseline."""
        respx.get(f"{BASE}/2025-26/gws/merged_gw.csv").mock(
            return_value=Response(200, text=SAMPLE_JOINER_CSV)
        )
        async with VaastavClient(_make_fetcher(tmp_path), seasons=("2025-26",)) as client:
            trends = await client.get_gw_trends()

        newguy = trends[300]
        assert newguy.price_start == 55  # GW3 value
        assert newguy.price_current == 58  # GW6 value
        assert newguy.price_change == 3
        assert newguy.gw_count == 4
        assert newguy.latest_gw == 6
        assert newguy.first_gw == 3

    @respx.mock
    async def test_in_memory_cache(self, tmp_path):
        """Second call returns cached data without extra HTTP request."""
        route = respx.get(f"{BASE}/2025-26/gws/merged_gw.csv").mock(
            return_value=Response(200, text=SAMPLE_GW_CSV)
        )
        async with VaastavClient(_make_fetcher(tmp_path), seasons=("2025-26",)) as client:
            await client.get_gw_trends()
            await client.get_gw_trends()
        assert route.call_count == 1

    @respx.mock
    async def test_empty_csv(self, tmp_path):
        """Empty CSV (header only) returns empty dict."""
        respx.get(f"{BASE}/2025-26/gws/merged_gw.csv").mock(
            return_value=Response(200, text=_GW_HEADER)
        )
        async with VaastavClient(_make_fetcher(tmp_path), seasons=("2025-26",)) as client:
            trends = await client.get_gw_trends()
        assert trends == {}


class TestGwTrendComputation:
    @respx.mock
    async def test_transfer_momentum_uses_recent_window(self, tmp_path):
        """Transfer momentum sums transfers_balance over last MOMENTUM_WINDOW GWs."""
        respx.get(f"{BASE}/2025-26/gws/merged_gw.csv").mock(
            return_value=Response(200, text=SAMPLE_GW_CSV)
        )
        async with VaastavClient(_make_fetcher(tmp_path), seasons=("2025-26",)) as client:
            trends = await client.get_gw_trends()

        salah = trends[100]
        # MOMENTUM_WINDOW=5, last 5 GWs: 40000+30000+20000+35000+45000 = 170000
        assert salah.transfer_momentum == 170000

    @respx.mock
    async def test_transfer_momentum_clamped_to_gw_count(self, tmp_path):
        """When fewer GWs than MOMENTUM_WINDOW, uses all available."""
        respx.get(f"{BASE}/2025-26/gws/merged_gw.csv").mock(
            return_value=Response(200, text=SAMPLE_JOINER_CSV)
        )
        async with VaastavClient(_make_fetcher(tmp_path), seasons=("2025-26",)) as client:
            trends = await client.get_gw_trends()

        newguy = trends[300]
        # Only 4 GWs, window clamped to 4: 10000+15000+20000+25000 = 70000
        assert newguy.transfer_momentum == 70000

    @respx.mock
    async def test_price_acceleration_rising(self, tmp_path):
        """Salah's price rises steadily - acceleration should be near zero."""
        respx.get(f"{BASE}/2025-26/gws/merged_gw.csv").mock(
            return_value=Response(200, text=SAMPLE_GW_CSV)
        )
        async with VaastavClient(_make_fetcher(tmp_path), seasons=("2025-26",)) as client:
            trends = await client.get_gw_trends()

        salah = trends[100]
        # Steady +1 per GW: acceleration ~0
        assert abs(salah.price_acceleration) < 0.5

    @respx.mock
    async def test_price_slope_positive_for_rising_player(self, tmp_path):
        """Salah's price slope is positive."""
        respx.get(f"{BASE}/2025-26/gws/merged_gw.csv").mock(
            return_value=Response(200, text=SAMPLE_GW_CSV)
        )
        async with VaastavClient(_make_fetcher(tmp_path), seasons=("2025-26",)) as client:
            trends = await client.get_gw_trends()
        assert trends[100].price_slope > 0

    def test_compute_acceleration_needs_4_points(self):
        """Acceleration returns 0 with fewer than 4 data points."""
        assert compute_acceleration([1.0, 2.0, 3.0]) == 0.0
        assert compute_acceleration([1.0, 2.0]) == 0.0
        assert compute_acceleration([]) == 0.0

    def test_compute_acceleration_detects_speedup(self):
        """Acceleration is positive when rate of change increases."""
        # Early: flat (100,100). Recent: rising (100,102,104,106)
        values = [100.0, 100.0, 100.0, 102.0, 104.0, 106.0]
        accel = compute_acceleration(values)
        assert accel > 0

    def test_compute_acceleration_detects_slowdown(self):
        """Acceleration is negative when rate of change decreases."""
        # Early: rising fast (100,104,108). Recent: flat (108,108,108)
        values = [100.0, 104.0, 108.0, 108.0, 108.0, 108.0]
        accel = compute_acceleration(values)
        assert accel < 0

    def test_compute_acceleration_quadratic_input(self):
        """Clearly quadratic input produces a meaningfully positive coefficient."""
        values = [100.0, 100.0, 101.0, 103.0, 106.0, 110.0]
        accel = compute_acceleration(values)
        assert accel > 0.1

    def test_compute_acceleration_constant_values(self):
        """Constant values return near-zero, not an error."""
        accel = compute_acceleration([100.0, 100.0, 100.0, 100.0])
        assert accel == pytest.approx(0, abs=0.1)


class TestGwTrendWindowing:
    @respx.mock
    async def test_last_n_slices_to_recent_gws(self, tmp_path):
        """last_n=4 on 6-GW data returns last 4 GWs only."""
        respx.get(f"{BASE}/2025-26/gws/merged_gw.csv").mock(
            return_value=Response(200, text=SAMPLE_GW_CSV)
        )
        async with VaastavClient(_make_fetcher(tmp_path), seasons=("2025-26",)) as client:
            trends = await client.get_gw_trends(last_n=4)

        salah = trends[100]
        assert salah.gw_count == 4
        assert salah.first_gw == 3
        assert salah.latest_gw == 6
        assert salah.price_start == 132  # GW3 value
        assert salah.price_current == 135  # GW6 value
        assert salah.price_change == 3

    @respx.mock
    async def test_last_n_momentum_uses_full_window(self, tmp_path):
        """When windowed, momentum sums all balances in the window."""
        respx.get(f"{BASE}/2025-26/gws/merged_gw.csv").mock(
            return_value=Response(200, text=SAMPLE_GW_CSV)
        )
        async with VaastavClient(_make_fetcher(tmp_path), seasons=("2025-26",)) as client:
            trends = await client.get_gw_trends(last_n=4)

        salah = trends[100]
        # GW3-6 balances: 30000+20000+35000+45000 = 130000
        assert salah.transfer_momentum == 130000

    @respx.mock
    async def test_last_n_larger_than_available_clamps(self, tmp_path):
        """last_n larger than available GWs uses all available."""
        respx.get(f"{BASE}/2025-26/gws/merged_gw.csv").mock(
            return_value=Response(200, text=SAMPLE_JOINER_CSV)
        )
        async with VaastavClient(_make_fetcher(tmp_path), seasons=("2025-26",)) as client:
            trends = await client.get_gw_trends(last_n=10)

        newguy = trends[300]
        assert newguy.gw_count == 4  # Only 4 GWs available
        assert newguy.first_gw == 3

    @respx.mock
    async def test_different_last_n_reuses_cached_rows(self, tmp_path):
        """Two calls with different last_n values make only one HTTP request."""
        route = respx.get(f"{BASE}/2025-26/gws/merged_gw.csv").mock(
            return_value=Response(200, text=SAMPLE_GW_CSV)
        )
        async with VaastavClient(_make_fetcher(tmp_path), seasons=("2025-26",)) as client:
            full = await client.get_gw_trends()
            windowed = await client.get_gw_trends(last_n=4)

        assert route.call_count == 1
        assert full[100].gw_count == 6
        assert windowed[100].gw_count == 4

    @respx.mock
    async def test_no_last_n_preserves_momentum_window(self, tmp_path):
        """Without last_n, momentum uses the hardcoded MOMENTUM_WINDOW."""
        respx.get(f"{BASE}/2025-26/gws/merged_gw.csv").mock(
            return_value=Response(200, text=SAMPLE_GW_CSV)
        )
        async with VaastavClient(_make_fetcher(tmp_path), seasons=("2025-26",)) as client:
            trends = await client.get_gw_trends()

        salah = trends[100]
        # MOMENTUM_WINDOW=5, last 5 GWs: 40000+30000+20000+35000+45000 = 170000
        assert salah.transfer_momentum == 170000


class TestContractTripwires:
    """Upstream schema drift degrades with a warning, never silently (#97)."""

    @respx.mock
    async def test_players_raw_missing_column_warns_and_degrades(self, tmp_path, caplog):
        # A renamed required column used to raise KeyError mid-row; it now
        # degrades to an empty season and says which column drifted.
        drifted = SAMPLE_CSV.replace("now_cost", "cost_now")
        respx.get(f"{BASE}/2024-25/players_raw.csv").mock(
            return_value=Response(200, text=drifted)
        )
        with caplog.at_level(logging.WARNING):
            async with VaastavClient(_make_fetcher(tmp_path), seasons=("2024-25",)) as client:
                data = await client._fetch_season_data()

        assert data["2024-25"] == []
        assert "players_raw.csv" in caplog.text
        assert "now_cost" in caplog.text

    @respx.mock
    async def test_players_raw_missing_team_code_warns_and_degrades(self, tmp_path, caplog):
        # team_code is what makes team_id mean the club code across both
        # sources, so its loss must be as loud as any other column's rather
        # than every season quietly reading team_id=0.
        drifted = SAMPLE_CSV.replace("team_code", "club_code")
        respx.get(f"{BASE}/2024-25/players_raw.csv").mock(
            return_value=Response(200, text=drifted)
        )
        with caplog.at_level(logging.WARNING):
            async with VaastavClient(_make_fetcher(tmp_path), seasons=("2024-25",)) as client:
                data = await client._fetch_season_data()

        assert data["2024-25"] == []
        assert "team_code" in caplog.text

    @respx.mock
    async def test_merged_gw_missing_column_warns_and_degrades(self, tmp_path, caplog):
        drifted = SAMPLE_GW_CSV.replace("transfers_balance", "net_transfers")
        respx.get(f"{BASE}/2025-26/gws/merged_gw.csv").mock(
            return_value=Response(200, text=drifted)
        )
        with caplog.at_level(logging.WARNING):
            async with VaastavClient(_make_fetcher(tmp_path), seasons=("2025-26",)) as client:
                trends = await client.get_gw_trends()

        assert trends == {}
        assert "merged_gw.csv" in caplog.text
        assert "transfers_balance" in caplog.text

    @respx.mock
    async def test_merged_gw_value_drift_warns(self, tmp_path, caplog):
        # element/round still parse here, so this exercises the second
        # conversion: a player must not enter by_player until a row fully
        # parses, or the empty-but-truthy buckets mask the tripwire.
        drifted = _GW_HEADER + (
            "Salah,MID,Liverpool,100,1,130.5,50000.0,80000,30000,12,90,1,8.5\n"
            "Haaland,FWD,Manchester City,200,1,150.5,60000.0,90000,30000,15,90,1,9.0\n"
        )
        respx.get(f"{BASE}/2025-26/gws/merged_gw.csv").mock(
            return_value=Response(200, text=drifted)
        )
        with caplog.at_level(logging.WARNING):
            async with VaastavClient(_make_fetcher(tmp_path), seasons=("2025-26",)) as client:
                trends = await client.get_gw_trends()

        assert trends == {}
        assert "none could be parsed" in caplog.text

    @respx.mock
    async def test_merged_gw_unparseable_rows_warn(self, tmp_path, caplog):
        # The header survives but every value fails conversion: the file is
        # non-empty yet parses to nothing, which must not pass silently.
        garbled = _GW_HEADER + (
            "Salah,MID,Liverpool,not-a-number,1,130,50000,80000,30000,12,90,1,8.5\n"
            "Haaland,FWD,Manchester City,not-a-number,1,150,60000,90000,30000,15,90,1,9.0\n"
        )
        respx.get(f"{BASE}/2025-26/gws/merged_gw.csv").mock(
            return_value=Response(200, text=garbled)
        )
        with caplog.at_level(logging.WARNING):
            async with VaastavClient(_make_fetcher(tmp_path), seasons=("2025-26",)) as client:
                trends = await client.get_gw_trends()

        assert trends == {}
        assert "none could be parsed" in caplog.text

    @respx.mock
    async def test_healthy_csv_emits_no_contract_warning(self, tmp_path, caplog):
        respx.get(f"{BASE}/2024-25/players_raw.csv").mock(
            return_value=Response(200, text=SAMPLE_CSV)
        )
        with caplog.at_level(logging.WARNING):
            async with VaastavClient(_make_fetcher(tmp_path), seasons=("2024-25",)) as client:
                data = await client._fetch_season_data()

        assert len(data["2024-25"]) == 2
        assert "missing expected column" not in caplog.text
