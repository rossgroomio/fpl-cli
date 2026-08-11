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
from fpl_cli.season import core_insights_season

# Derived from the same helper the client uses, so mocked URLs follow the
# season rollover instead of pinning the season these tests were written in.
CI_SEASON = core_insights_season()
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
    async def test_build_profile_sets_reliability(self, tmp_path):
        """reliability field is populated; current_gw tracked from max GW in data."""
        respx.get(f"{BASE}/{CI_SEASON}/players.csv").mock(
            return_value=Response(200, text=PLAYERS_CSV)
        )
        respx.get(f"{BASE}/{CI_SEASON}/playerstats.csv").mock(
            return_value=Response(200, text=PLAYERSTATS_CSV)
        )
        async with CoreInsightsClient(_make_fetcher(tmp_path)) as client:
            profiles = await client.get_all_player_histories()

        # current_gw = 10 (max GW in test data)
        assert client._current_gw == 10
        # Salah has starts=31 at GW10; normalised = 31/10 > 1.0 -> clamped to 1.0
        assert profiles[80201].reliability == 1.0
        # Keeper has starts=10 at GW10; normalised = 10/10 = 1.0 -> clamped to 1.0
        assert profiles[500000].reliability == 1.0

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


# --- Match-level CSV fixtures (per-GW structure) ---

# Per-GW fixtures mirror the Core-Insights layout:
#   By Tournament/Premier League/GW{n}/matches.csv
#   By Tournament/Premier League/GW{n}/playermatchstats.csv

_PL_PREFIX = f"{CI_SEASON}/By Tournament/Premier League"

GW1_MATCHES = (
    "match_id,gameweek,tournament,home_team,away_team,home_team_elo,away_team_elo\n"
    "m1,1,prem,14,13,1800.0,1750.0\n"  # Liverpool (14) home vs Man City (13)
)
GW1_STATS = (
    "player_id,match_id,minutes_played,xg,xa,penalties_scored,penalties_missed,"
    "total_shots,chances_created,touches_opposition_box,clearances,blocks,"
    "interceptions,tackles_won,recoveries,saves,xgot_faced,goals_prevented\n"
    "100,m1,90,0.60,0.30,1,0,4,2,8,0,0,0,0,0,0,0.0,0.0\n"
    "200,m1,90,0.80,0.10,0,0,5,1,10,0,0,0,0,0,0,0.0,0.0\n"
)

GW2_MATCHES = (
    "match_id,gameweek,tournament,home_team,away_team,home_team_elo,away_team_elo\n"
    "m2,2,prem,13,14,1760.0,1810.0\n"  # Man City home vs Liverpool
)
GW2_STATS = (
    "player_id,match_id,minutes_played,xg,xa,penalties_scored,penalties_missed,"
    "total_shots,chances_created,touches_opposition_box\n"
    "100,m2,45,0.20,0.15,0,0,2,1,3\n"
)

GW4_MATCHES = (
    "match_id,gameweek,tournament,home_team,away_team,home_team_elo,away_team_elo\n"
    "m4,4,prem,1,14,1550.0,1820.0\n"   # Weak team (1) home vs Liverpool
)
GW4_STATS = (
    "player_id,match_id,minutes_played,xg,xa,penalties_scored,penalties_missed,"
    "total_shots,chances_created,touches_opposition_box\n"
    "100,m4,90,0.50,0.20,0,1,3,2,6\n"
)

# current_gw for tests: 6 means we fetch GWs 1-5 (last 12 capped to available)
TEST_CURRENT_GW = 6


def _mock_gw(gw: int, matches_csv: str, stats_csv: str):
    """Mock both per-GW CSV endpoints for a single gameweek."""
    respx.get(f"{BASE}/{_PL_PREFIX}/GW{gw}/matches.csv").mock(
        return_value=Response(200, text=matches_csv)
    )
    respx.get(f"{BASE}/{_PL_PREFIX}/GW{gw}/playermatchstats.csv").mock(
        return_value=Response(200, text=stats_csv)
    )


def _mock_gw_404(gw: int):
    """Mock a gameweek where CSVs are not yet available."""
    respx.get(f"{BASE}/{_PL_PREFIX}/GW{gw}/matches.csv").mock(
        return_value=Response(404)
    )
    respx.get(f"{BASE}/{_PL_PREFIX}/GW{gw}/playermatchstats.csv").mock(
        return_value=Response(404)
    )


def _mock_players():
    respx.get(f"{BASE}/{CI_SEASON}/players.csv").mock(
        return_value=Response(200, text=PLAYERS_CSV)
    )


def _mock_standard_gws():
    """Mock GW1, GW2, GW4 with data; GW3, GW5 as 404 (not yet played)."""
    _mock_gw(1, GW1_MATCHES, GW1_STATS)
    _mock_gw(2, GW2_MATCHES, GW2_STATS)
    _mock_gw_404(3)
    _mock_gw(4, GW4_MATCHES, GW4_STATS)
    _mock_gw_404(5)


class TestMatchStats:
    @respx.mock
    async def test_happy_path_joins_across_gameweeks(self, tmp_path):
        """Per-GW CSVs joined correctly across multiple gameweeks."""
        _mock_players()
        _mock_standard_gws()

        async with CoreInsightsClient(_make_fetcher(tmp_path)) as client:
            result = await client.get_match_stats(TEST_CURRENT_GW)

        assert 100 in result
        salah_records = result[100]
        assert len(salah_records) == 3
        assert {r["gameweek"] for r in salah_records} == {1, 2, 4}

    @respx.mock
    async def test_opponent_elo_home_player(self, tmp_path):
        """Home player gets away team's Elo as opponent_elo; is_home=True."""
        _mock_players()
        _mock_standard_gws()

        async with CoreInsightsClient(_make_fetcher(tmp_path)) as client:
            result = await client.get_match_stats(TEST_CURRENT_GW)

        gw1 = next(r for r in result[100] if r["gameweek"] == 1)
        assert gw1["is_home"] is True
        assert gw1["opponent_elo"] == 1750.0  # away_team_elo for m1

    @respx.mock
    async def test_opponent_elo_away_player(self, tmp_path):
        """Away player gets home team's Elo as opponent_elo; is_home=False."""
        _mock_players()
        _mock_standard_gws()

        async with CoreInsightsClient(_make_fetcher(tmp_path)) as client:
            result = await client.get_match_stats(TEST_CURRENT_GW)

        gw2 = next(r for r in result[100] if r["gameweek"] == 2)
        assert gw2["is_home"] is False
        assert gw2["opponent_elo"] == 1760.0  # home_team_elo for m2

    @respx.mock
    async def test_npxg_fields_parsed(self, tmp_path):
        """penalties_scored, penalties_missed, xg, minutes_played parsed correctly."""
        _mock_players()
        _mock_standard_gws()

        async with CoreInsightsClient(_make_fetcher(tmp_path)) as client:
            result = await client.get_match_stats(TEST_CURRENT_GW)

        gw1 = next(r for r in result[100] if r["gameweek"] == 1)
        assert gw1["xg"] == 0.60
        assert gw1["penalties_scored"] == 1
        assert gw1["penalties_missed"] == 0
        assert gw1["minutes_played"] == 90

        gw4 = next(r for r in result[100] if r["gameweek"] == 4)
        assert gw4["penalties_scored"] == 0
        assert gw4["penalties_missed"] == 1

    @respx.mock
    async def test_extended_fields_parsed(self, tmp_path):
        """New extended fields (xa, shots, involvement, GK) parsed correctly."""
        _mock_players()
        _mock_standard_gws()

        async with CoreInsightsClient(_make_fetcher(tmp_path)) as client:
            result = await client.get_match_stats(TEST_CURRENT_GW)

        gw1 = next(r for r in result[100] if r["gameweek"] == 1)
        assert gw1["xa"] == 0.30
        assert gw1["total_shots"] == 4
        assert gw1["chances_created"] == 2
        assert gw1["touches_opposition_box"] == 8

    @respx.mock
    async def test_missing_xa_defaults_to_zero(self, tmp_path):
        """Row without xa column defaults to 0.0."""
        stats_no_xa = (
            "player_id,match_id,minutes_played,xg\n"
            "100,m1,90,0.50\n"
        )
        _mock_players()
        _mock_gw(1, GW1_MATCHES, stats_no_xa)

        async with CoreInsightsClient(_make_fetcher(tmp_path)) as client:
            result = await client.get_match_stats(2)

        gw1 = result[100][0]
        assert gw1["xa"] == 0.0
        assert gw1["total_shots"] == 0
        assert gw1["clearances"] == 0
        assert gw1["saves"] == 0

    @respx.mock
    async def test_missing_saves_empty_string_parses_as_zero(self, tmp_path):
        """Empty string for saves parses as 0."""
        stats_empty_saves = (
            "player_id,match_id,minutes_played,xg,saves\n"
            "100,m1,90,0.50,\n"
        )
        _mock_players()
        _mock_gw(1, GW1_MATCHES, stats_empty_saves)

        async with CoreInsightsClient(_make_fetcher(tmp_path)) as client:
            result = await client.get_match_stats(2)

        gw1 = result[100][0]
        assert gw1["saves"] == 0

    @respx.mock
    async def test_extended_fields_missing_columns_default(self, tmp_path):
        """When CSV lacks extended columns entirely, all default to 0."""
        _mock_players()
        _mock_gw(1, GW1_MATCHES, (
            "player_id,match_id,minutes_played,xg\n"
            "100,m1,90,0.50\n"
        ))

        async with CoreInsightsClient(_make_fetcher(tmp_path)) as client:
            result = await client.get_match_stats(2)

        gw1 = result[100][0]
        assert gw1["xa"] == 0.0
        assert gw1["tackles_won"] == 0
        assert gw1["xgot_faced"] == 0.0
        assert gw1["goals_prevented"] == 0.0
        assert gw1["recoveries"] == 0

    @respx.mock
    async def test_missing_penalties_field_defaults_to_zero(self, tmp_path):
        """Row without penalties_scored/missed defaults to 0."""
        stats_no_pen = (
            "player_id,match_id,minutes_played,xg\n"
            "100,m1,90,0.50\n"
        )
        _mock_players()
        _mock_gw(1, GW1_MATCHES, stats_no_pen)

        async with CoreInsightsClient(_make_fetcher(tmp_path)) as client:
            result = await client.get_match_stats(2)

        gw1 = result[100][0]
        assert gw1["penalties_scored"] == 0
        assert gw1["penalties_missed"] == 0

    @respx.mock
    async def test_orphaned_player_stat_row_skipped(self, tmp_path):
        """Player stat row with match_id not in matches.csv is silently dropped."""
        stats_orphan = (
            "player_id,match_id,minutes_played,xg,penalties_scored,penalties_missed\n"
            "100,orphan_id,90,0.5,0,0\n"
        )
        _mock_players()
        _mock_gw(1, GW1_MATCHES, stats_orphan)

        async with CoreInsightsClient(_make_fetcher(tmp_path)) as client:
            result = await client.get_match_stats(2)

        assert 100 not in result

    @respx.mock
    async def test_single_gw_404_skipped_others_still_parsed(self, tmp_path):
        """404 on one GW is tolerated; other GWs still parsed."""
        _mock_players()
        _mock_gw(1, GW1_MATCHES, GW1_STATS)
        _mock_gw_404(2)

        async with CoreInsightsClient(_make_fetcher(tmp_path)) as client:
            result = await client.get_match_stats(3)

        assert 100 in result
        assert len(result[100]) == 1
        assert result[100][0]["gameweek"] == 1

    @respx.mock
    async def test_all_gws_404_returns_empty(self, tmp_path):
        """All GWs 404 returns empty dict, no exception."""
        _mock_players()
        _mock_gw_404(1)
        _mock_gw_404(2)

        async with CoreInsightsClient(_make_fetcher(tmp_path)) as client:
            result = await client.get_match_stats(3)

        assert result == {}

    @respx.mock
    async def test_caching_no_second_fetch(self, tmp_path):
        """Second call returns cached result without additional HTTP requests."""
        _mock_players()
        route = respx.get(f"{BASE}/{_PL_PREFIX}/GW1/matches.csv").mock(
            return_value=Response(200, text=GW1_MATCHES)
        )
        respx.get(f"{BASE}/{_PL_PREFIX}/GW1/playermatchstats.csv").mock(
            return_value=Response(200, text=GW1_STATS)
        )

        async with CoreInsightsClient(_make_fetcher(tmp_path)) as client:
            first = await client.get_match_stats(2)
            second = await client.get_match_stats(2)

        assert first is second
        assert route.call_count == 1


class TestFactory:
    def test_make_core_insights_fetcher(self):
        """Factory creates fetcher with correct cache subdirectory."""
        with patch("fpl_cli.paths.user_cache_dir", return_value=Path("/fake/cache")):
            fetcher = make_core_insights_fetcher()
        assert str(fetcher.cache_dir).endswith("core-insights")
        assert fetcher.base_url == BASE_URL
