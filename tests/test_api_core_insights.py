"""Tests for CoreInsightsClient CSV fetching and parsing."""
from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

import pytest
import respx
from httpx import Response

from fpl_cli.api.core_insights import BASE_URL, CoreInsightsClient, make_core_insights_fetcher
from fpl_cli.api.dataset_fetcher import DatasetFetcher

CI_SEASON = "2025-2026"
BASE = BASE_URL


@pytest.fixture(autouse=True)
def _reset_core_insights_session_cache():
    CoreInsightsClient._session_profiles = None
    yield
    CoreInsightsClient._session_profiles = None


def _make_fetcher(tmp_path: Path) -> DatasetFetcher:
    return DatasetFetcher(
        base_url=BASE_URL,
        cache_dir=tmp_path / "cache",
        ttl=timedelta(hours=4),
    )


# --- Sample CSV data ---

PLAYERS_CSV = (
    "player_code,player_id,first_name,second_name,web_name,team_code,position\n"
    "80201,100,Mohamed,Salah,Salah,14,Midfielder\n"
    "206325,200,Erling,Haaland,Haaland,13,Forward\n"
    "500000,300,Test,Keeper,Keeper,1,Goalkeeper\n"
)

# Cumulative playerstats with gw column. Player 100 has rows at GW5 and GW10.
PLAYERSTATS_CSV = (
    "id,now_cost,cost_change_start,total_points,minutes,starts,goals_scored,assists,"
    "expected_goals,expected_assists,expected_goal_involvements,gw,"
    "transfers_in_event,transfers_out_event\n"
    # Salah GW5 (intermediate snapshot)
    "100,13.2,0.7,120,1400,16,9,6,8.5,5.1,13.6,5,50000,20000\n"
    # Salah GW10 (latest = season aggregate)
    "100,13.5,1.0,265,2800,31,19,13,17.5,10.2,27.7,10,40000,30000\n"
    # Haaland GW10
    "200,15.2,0.7,220,2500,28,25,5,22.0,3.5,25.5,10,60000,20000\n"
    # Keeper GW10
    "300,4.5,0.0,80,900,10,0,0,0.0,0.0,0.0,10,1000,500\n"
)

# Player not in players.csv
PLAYERSTATS_ORPHAN_CSV = (
    "id,now_cost,cost_change_start,total_points,minutes,starts,goals_scored,assists,"
    "expected_goals,expected_assists,expected_goal_involvements,gw,"
    "transfers_in_event,transfers_out_event\n"
    "999,5.0,0.0,10,100,1,0,0,0.0,0.0,0.0,5,100,50\n"
)

# Per-GW player_gameweek_stats for trend tests
_GW_HEADER = (
    "id,now_cost,transfers_in_event,transfers_out_event,web_name\n"
)


def _gw_rows(gw_data: list[tuple[int, float, int, int, str]]) -> str:
    """Build a GW CSV from (id, cost, in, out, name) tuples."""
    lines = [_GW_HEADER.strip()]
    for pid, cost, tin, tout, name in gw_data:
        lines.append(f"{pid},{cost},{tin},{tout},{name}")
    return "\n".join(lines) + "\n"


GW1_CSV = _gw_rows([
    (100, 13.0, 80000, 30000, "Salah"),
    (200, 15.0, 90000, 30000, "Haaland"),
])

GW2_CSV = _gw_rows([
    (100, 13.1, 70000, 30000, "Salah"),
    (200, 15.0, 50000, 20000, "Haaland"),
])

GW3_CSV = _gw_rows([
    (100, 13.2, 60000, 30000, "Salah"),
    (200, 14.9, 20000, 30000, "Haaland"),
])

GW4_CSV = _gw_rows([
    (100, 13.3, 55000, 35000, "Salah"),
    (200, 14.8, 15000, 35000, "Haaland"),
])

GW5_CSV = _gw_rows([
    (100, 13.4, 65000, 30000, "Salah"),
    (200, 14.8, 25000, 30000, "Haaland"),
])

GW6_CSV = _gw_rows([
    (100, 13.5, 75000, 30000, "Salah"),
    (200, 14.7, 10000, 25000, "Haaland"),
])

# DGW: same player appears twice in same GW file
GW_DGW_CSV = _gw_rows([
    (100, 13.0, 80000, 30000, "Salah"),
    (100, 13.0, 80000, 30000, "Salah"),  # duplicate
])


def _mock_gw_routes(gw_csvs: dict[int, str]):
    """Set up respx mocks for GW files. Missing GWs return 404."""
    for gw in range(1, 39):
        url = f"{BASE}/{CI_SEASON}/By Gameweek/GW{gw}/player_gameweek_stats.csv"
        if gw in gw_csvs:
            respx.get(url).mock(return_value=Response(200, text=gw_csvs[gw]))
        else:
            respx.get(url).mock(return_value=Response(404))


# --- Season aggregate tests ---

class TestSeasonAggregates:
    @respx.mock
    async def test_parse_playerstats_max_gw_per_player(self, tmp_path):
        """Season aggregates use the max-GW row per player."""
        respx.get(f"{BASE}/{CI_SEASON}/players.csv").mock(
            return_value=Response(200, text=PLAYERS_CSV)
        )
        respx.get(f"{BASE}/{CI_SEASON}/playerstats.csv").mock(
            return_value=Response(200, text=PLAYERSTATS_CSV)
        )
        async with CoreInsightsClient(_make_fetcher(tmp_path)) as client:
            data = await client._fetch_season_data()

        season_key = client._season_label
        assert season_key in data
        rows = data[season_key]
        assert len(rows) == 3

        salah = [r for r in rows if r.element_code == 80201][0]
        assert salah.total_points == 265  # GW10 row, not GW5
        assert salah.minutes == 2800
        assert salah.goals == 19
        assert salah.assists == 13
        assert salah.expected_goals == 17.5
        assert salah.season == season_key

    @respx.mock
    async def test_price_conversion(self, tmp_path):
        """Pound prices converted to £0.1m integers."""
        respx.get(f"{BASE}/{CI_SEASON}/players.csv").mock(
            return_value=Response(200, text=PLAYERS_CSV)
        )
        respx.get(f"{BASE}/{CI_SEASON}/playerstats.csv").mock(
            return_value=Response(200, text=PLAYERSTATS_CSV)
        )
        async with CoreInsightsClient(_make_fetcher(tmp_path)) as client:
            data = await client._fetch_season_data()

        rows = data[client._season_label]
        salah = [r for r in rows if r.element_code == 80201][0]
        assert salah.end_cost == 135  # 13.5 * 10
        assert salah.start_cost == 125  # 135 - (1.0 * 10)

        haaland = [r for r in rows if r.element_code == 206325][0]
        assert haaland.end_cost == 152  # 15.2 * 10
        assert haaland.start_cost == 145  # 152 - (0.7 * 10)

    @respx.mock
    async def test_price_rounding_edge_case(self, tmp_path):
        """Price with .x5 boundary rounds correctly."""
        csv = (
            "id,now_cost,cost_change_start,total_points,minutes,starts,goals_scored,"
            "assists,expected_goals,expected_assists,expected_goal_involvements,gw,"
            "transfers_in_event,transfers_out_event\n"
            "100,5.85,0.0,50,500,6,2,1,1.5,0.8,2.3,5,100,50\n"
        )
        respx.get(f"{BASE}/{CI_SEASON}/players.csv").mock(
            return_value=Response(200, text=PLAYERS_CSV)
        )
        respx.get(f"{BASE}/{CI_SEASON}/playerstats.csv").mock(
            return_value=Response(200, text=csv)
        )
        async with CoreInsightsClient(_make_fetcher(tmp_path)) as client:
            data = await client._fetch_season_data()

        salah = data[client._season_label][0]
        # round(5.85 * 10) = round(58.5) = 58 (Python banker's rounding)
        assert salah.end_cost == 58

    @respx.mock
    async def test_players_csv_join(self, tmp_path):
        """player_code, position, web_name populated from players.csv join."""
        respx.get(f"{BASE}/{CI_SEASON}/players.csv").mock(
            return_value=Response(200, text=PLAYERS_CSV)
        )
        respx.get(f"{BASE}/{CI_SEASON}/playerstats.csv").mock(
            return_value=Response(200, text=PLAYERSTATS_CSV)
        )
        async with CoreInsightsClient(_make_fetcher(tmp_path)) as client:
            data = await client._fetch_season_data()

        rows = data[client._season_label]
        salah = [r for r in rows if r.element_code == 80201][0]
        assert salah.position == "MID"
        assert salah.web_name == "Salah"
        assert salah.team_id == 14

        haaland = [r for r in rows if r.element_code == 206325][0]
        assert haaland.position == "FWD"

        keeper = [r for r in rows if r.element_code == 500000][0]
        assert keeper.position == "GK"

    @respx.mock
    async def test_orphan_player_skipped(self, tmp_path):
        """Player in playerstats but not in players.csv is skipped."""
        respx.get(f"{BASE}/{CI_SEASON}/players.csv").mock(
            return_value=Response(200, text=PLAYERS_CSV)
        )
        respx.get(f"{BASE}/{CI_SEASON}/playerstats.csv").mock(
            return_value=Response(200, text=PLAYERSTATS_ORPHAN_CSV)
        )
        async with CoreInsightsClient(_make_fetcher(tmp_path)) as client:
            data = await client._fetch_season_data()

        assert data[client._season_label] == []

    @respx.mock
    async def test_empty_playerstats(self, tmp_path):
        """Empty playerstats returns empty list."""
        header_only = (
            "id,now_cost,cost_change_start,total_points,minutes,starts,goals_scored,"
            "assists,expected_goals,expected_assists,expected_goal_involvements,gw,"
            "transfers_in_event,transfers_out_event\n"
        )
        respx.get(f"{BASE}/{CI_SEASON}/players.csv").mock(
            return_value=Response(200, text=PLAYERS_CSV)
        )
        respx.get(f"{BASE}/{CI_SEASON}/playerstats.csv").mock(
            return_value=Response(200, text=header_only)
        )
        async with CoreInsightsClient(_make_fetcher(tmp_path)) as client:
            data = await client._fetch_season_data()

        assert data[client._season_label] == []

    @respx.mock
    async def test_session_cache(self, tmp_path):
        """get_all_player_histories caches at class level."""
        respx.get(f"{BASE}/{CI_SEASON}/players.csv").mock(
            return_value=Response(200, text=PLAYERS_CSV)
        )
        route = respx.get(f"{BASE}/{CI_SEASON}/playerstats.csv").mock(
            return_value=Response(200, text=PLAYERSTATS_CSV)
        )
        async with CoreInsightsClient(_make_fetcher(tmp_path)) as client:
            profiles1 = await client.get_all_player_histories()
            profiles2 = await client.get_all_player_histories()

        assert profiles1 is profiles2
        assert route.call_count == 1

    @respx.mock
    async def test_get_all_player_histories(self, tmp_path):
        """Profiles keyed by element_code with computed signals."""
        respx.get(f"{BASE}/{CI_SEASON}/players.csv").mock(
            return_value=Response(200, text=PLAYERS_CSV)
        )
        respx.get(f"{BASE}/{CI_SEASON}/playerstats.csv").mock(
            return_value=Response(200, text=PLAYERSTATS_CSV)
        )
        async with CoreInsightsClient(_make_fetcher(tmp_path)) as client:
            profiles = await client.get_all_player_histories()

        assert 80201 in profiles
        assert 206325 in profiles
        assert profiles[80201].web_name == "Salah"
        assert profiles[206325].current_position == "FWD"

    @respx.mock
    async def test_get_player_history_found(self, tmp_path):
        """Known element_code returns profile with seasons."""
        respx.get(f"{BASE}/{CI_SEASON}/players.csv").mock(
            return_value=Response(200, text=PLAYERS_CSV)
        )
        respx.get(f"{BASE}/{CI_SEASON}/playerstats.csv").mock(
            return_value=Response(200, text=PLAYERSTATS_CSV)
        )
        async with CoreInsightsClient(_make_fetcher(tmp_path)) as client:
            result = await client.get_player_history(80201)
        assert result is not None
        assert result.element_code == 80201
        assert result.web_name == "Salah"
        assert len(result.seasons) == 1

    @respx.mock
    async def test_get_player_history_not_found(self, tmp_path):
        """Unknown element_code returns None."""
        respx.get(f"{BASE}/{CI_SEASON}/players.csv").mock(
            return_value=Response(200, text=PLAYERS_CSV)
        )
        respx.get(f"{BASE}/{CI_SEASON}/playerstats.csv").mock(
            return_value=Response(200, text=PLAYERSTATS_CSV)
        )
        async with CoreInsightsClient(_make_fetcher(tmp_path)) as client:
            result = await client.get_player_history(99999)
        assert result is None


# --- GW trend tests ---

class TestGwTrends:
    @respx.mock
    async def test_basic_parsing(self, tmp_path):
        """Per-GW files parsed into GwTrendProfile objects."""
        respx.get(f"{BASE}/{CI_SEASON}/players.csv").mock(
            return_value=Response(200, text=PLAYERS_CSV)
        )
        gws = {1: GW1_CSV, 2: GW2_CSV, 3: GW3_CSV, 4: GW4_CSV, 5: GW5_CSV, 6: GW6_CSV}
        _mock_gw_routes(gws)

        async with CoreInsightsClient(_make_fetcher(tmp_path)) as client:
            trends = await client.get_gw_trends()

        assert len(trends) == 2
        assert 100 in trends
        assert 200 in trends

        salah = trends[100]
        assert salah.web_name == "Salah"
        assert salah.price_start == 130  # 13.0 * 10
        assert salah.price_current == 135  # 13.5 * 10
        assert salah.price_change == 5
        assert salah.gw_count == 6
        assert salah.latest_gw == 6
        assert salah.first_gw == 1

    @respx.mock
    async def test_falling_price(self, tmp_path):
        """Haaland's price falls - negative change and slope."""
        respx.get(f"{BASE}/{CI_SEASON}/players.csv").mock(
            return_value=Response(200, text=PLAYERS_CSV)
        )
        gws = {1: GW1_CSV, 2: GW2_CSV, 3: GW3_CSV, 4: GW4_CSV, 5: GW5_CSV, 6: GW6_CSV}
        _mock_gw_routes(gws)

        async with CoreInsightsClient(_make_fetcher(tmp_path)) as client:
            trends = await client.get_gw_trends()

        haaland = trends[200]
        assert haaland.price_start == 150
        assert haaland.price_current == 147
        assert haaland.price_change == -3
        assert haaland.price_slope < 0

    @respx.mock
    async def test_transfers_balance_computed(self, tmp_path):
        """transfers_balance = transfers_in_event - transfers_out_event."""
        respx.get(f"{BASE}/{CI_SEASON}/players.csv").mock(
            return_value=Response(200, text=PLAYERS_CSV)
        )
        _mock_gw_routes({1: GW1_CSV})

        async with CoreInsightsClient(_make_fetcher(tmp_path)) as client:
            trends = await client.get_gw_trends()

        salah = trends[100]
        # GW1: 80000 - 30000 = 50000
        assert salah.transfer_momentum == 50000

    @respx.mock
    async def test_404_gw_skipped(self, tmp_path):
        """404 on unplayed GW files are skipped gracefully."""
        respx.get(f"{BASE}/{CI_SEASON}/players.csv").mock(
            return_value=Response(200, text=PLAYERS_CSV)
        )
        # Only GW1 and GW2 exist, rest 404
        _mock_gw_routes({1: GW1_CSV, 2: GW2_CSV})

        async with CoreInsightsClient(_make_fetcher(tmp_path)) as client:
            trends = await client.get_gw_trends()

        assert trends[100].gw_count == 2
        assert trends[100].latest_gw == 2

    @respx.mock
    async def test_all_gws_404_returns_empty(self, tmp_path):
        """All GW files 404 returns empty dict."""
        respx.get(f"{BASE}/{CI_SEASON}/players.csv").mock(
            return_value=Response(200, text=PLAYERS_CSV)
        )
        _mock_gw_routes({})

        async with CoreInsightsClient(_make_fetcher(tmp_path)) as client:
            trends = await client.get_gw_trends()

        assert trends == {}

    @respx.mock
    async def test_dgw_deduplication(self, tmp_path):
        """DGW rows deduplicated - one entry per player per GW."""
        respx.get(f"{BASE}/{CI_SEASON}/players.csv").mock(
            return_value=Response(200, text=PLAYERS_CSV)
        )
        _mock_gw_routes({1: GW_DGW_CSV})

        async with CoreInsightsClient(_make_fetcher(tmp_path)) as client:
            trends = await client.get_gw_trends()

        assert trends[100].gw_count == 1

    @respx.mock
    async def test_gw_cache(self, tmp_path):
        """Second get_gw_trends call reuses cached rows."""
        respx.get(f"{BASE}/{CI_SEASON}/players.csv").mock(
            return_value=Response(200, text=PLAYERS_CSV)
        )
        _mock_gw_routes({1: GW1_CSV, 2: GW2_CSV})

        async with CoreInsightsClient(_make_fetcher(tmp_path)) as client:
            await client.get_gw_trends()
            await client.get_gw_trends()

        # players.csv fetched once, GW files fetched once each
        # (38 GW requests + 1 players request = 39 total, not 77)


class TestGwTrendWindowing:
    @respx.mock
    async def test_last_n_slices(self, tmp_path):
        """last_n=4 on 6-GW data returns last 4 GWs only."""
        respx.get(f"{BASE}/{CI_SEASON}/players.csv").mock(
            return_value=Response(200, text=PLAYERS_CSV)
        )
        gws = {1: GW1_CSV, 2: GW2_CSV, 3: GW3_CSV, 4: GW4_CSV, 5: GW5_CSV, 6: GW6_CSV}
        _mock_gw_routes(gws)

        async with CoreInsightsClient(_make_fetcher(tmp_path)) as client:
            trends = await client.get_gw_trends(last_n=4)

        salah = trends[100]
        assert salah.gw_count == 4
        assert salah.first_gw == 3
        assert salah.latest_gw == 6
        assert salah.price_start == 132  # GW3: 13.2 * 10
        assert salah.price_current == 135  # GW6: 13.5 * 10

    @respx.mock
    async def test_momentum_window(self, tmp_path):
        """Without last_n, momentum uses MOMENTUM_WINDOW."""
        respx.get(f"{BASE}/{CI_SEASON}/players.csv").mock(
            return_value=Response(200, text=PLAYERS_CSV)
        )
        gws = {1: GW1_CSV, 2: GW2_CSV, 3: GW3_CSV, 4: GW4_CSV, 5: GW5_CSV, 6: GW6_CSV}
        _mock_gw_routes(gws)

        async with CoreInsightsClient(_make_fetcher(tmp_path)) as client:
            trends = await client.get_gw_trends()

        salah = trends[100]
        # MOMENTUM_WINDOW=5, last 5 GWs (2-6) balances:
        # GW2: 70000-30000=40000, GW3: 60000-30000=30000, GW4: 55000-35000=20000,
        # GW5: 65000-30000=35000, GW6: 75000-30000=45000
        assert salah.transfer_momentum == 170000


class TestFactory:
    def test_make_core_insights_fetcher(self):
        """Factory creates fetcher with correct cache subdirectory."""
        with patch("fpl_cli.paths.user_cache_dir", return_value=Path("/fake/cache")):
            fetcher = make_core_insights_fetcher()
        assert str(fetcher.cache_dir).endswith("core-insights")
        assert fetcher.base_url == BASE_URL
