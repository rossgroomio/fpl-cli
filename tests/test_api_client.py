"""Tests for FPL API client."""

from unittest.mock import AsyncMock, patch

import pytest

from fpl_cli.api.fpl import FPLClient
from fpl_cli.models.fixture import Fixture
from fpl_cli.models.player import Player
from fpl_cli.models.team import Team


class TestFPLClient:
    """Tests for FPLClient."""

    @pytest.fixture
    def client(self):
        """Create a fresh FPL client for each test."""
        return FPLClient()

    @pytest.fixture
    def mock_bootstrap_response(self):
        """Sample bootstrap-static response."""
        return {
            "elements": [
                {
                    "id": 1,
                    "web_name": "Salah",
                    "first_name": "Mohamed",
                    "second_name": "Salah",
                    "team": 14,
                    "element_type": 3,
                    "now_cost": 130,
                    "selected_by_percent": "45.5",
                    "status": "a",
                    "total_points": 120,
                    "points_per_game": "6.5",
                    "form": "7.2",
                    "minutes": 1800,
                    "goals_scored": 12,
                    "assists": 8,
                    "expected_goals": "10.5",
                    "expected_assists": "7.2",
                    "expected_goal_involvements": "17.7",
                    "expected_goals_conceded": "0.0",
                    "clean_sheets": 5,
                    "goals_conceded": 10,
                    "bonus": 15,
                    "bps": 450,
                    "influence": "500.0",
                    "creativity": "400.0",
                    "threat": "600.0",
                    "ict_index": "150.0",
                    "transfers_in_event": 50000,
                    "transfers_out_event": 10000,
                    "cost_change_event": 1,
                    "cost_change_start": 5,
                    "chance_of_playing_next_round": 100,
                    "news": "",
                    "news_added": None,
                },
                {
                    "id": 2,
                    "web_name": "Haaland",
                    "first_name": "Erling",
                    "second_name": "Haaland",
                    "team": 13,
                    "element_type": 4,
                    "now_cost": 150,
                    "selected_by_percent": "85.0",
                    "status": "a",
                    "total_points": 150,
                    "points_per_game": "8.0",
                    "form": "8.5",
                    "minutes": 2000,
                    "goals_scored": 20,
                    "assists": 5,
                    "expected_goals": "18.5",
                    "expected_assists": "4.2",
                    "expected_goal_involvements": "22.7",
                    "expected_goals_conceded": "0.0",
                    "clean_sheets": 0,
                    "goals_conceded": 0,
                    "bonus": 20,
                    "bps": 500,
                    "influence": "600.0",
                    "creativity": "200.0",
                    "threat": "800.0",
                    "ict_index": "160.0",
                    "transfers_in_event": 100000,
                    "transfers_out_event": 5000,
                    "cost_change_event": 0,
                    "cost_change_start": 10,
                    "chance_of_playing_next_round": 100,
                    "news": "",
                    "news_added": None,
                },
            ],
            "teams": [
                {
                    "id": 13,
                    "name": "Manchester City",
                    "short_name": "MCI",
                    "code": 43,
                    "strength": 5,
                    "strength_overall_home": 1350,
                    "strength_overall_away": 1300,
                    "strength_attack_home": 1400,
                    "strength_attack_away": 1350,
                    "strength_defence_home": 1300,
                    "strength_defence_away": 1250,
                    "form": "WWWWW",
                    "position": 1,
                    "played": 20,
                    "win": 16,
                    "draw": 2,
                    "loss": 2,
                    "points": 50,
                },
                {
                    "id": 14,
                    "name": "Liverpool",
                    "short_name": "LIV",
                    "code": 14,
                    "strength": 5,
                    "strength_overall_home": 1320,
                    "strength_overall_away": 1280,
                    "strength_attack_home": 1350,
                    "strength_attack_away": 1320,
                    "strength_defence_home": 1290,
                    "strength_defence_away": 1240,
                    "form": "WDWWW",
                    "position": 2,
                    "played": 20,
                    "win": 14,
                    "draw": 4,
                    "loss": 2,
                    "points": 46,
                },
            ],
            "events": [
                {"id": 24, "is_current": True, "is_next": False, "deadline_time": "2024-02-03T11:00:00Z"},
                {"id": 25, "is_current": False, "is_next": True, "deadline_time": "2024-02-10T11:00:00Z"},
            ],
        }

    @pytest.fixture
    def mock_fixtures_response(self):
        """Sample fixtures response."""
        return [
            {
                "id": 1,
                "event": 25,
                "team_h": 13,
                "team_a": 14,
                "team_h_difficulty": 4,
                "team_a_difficulty": 4,
                "kickoff_time": "2024-02-10T16:30:00Z",
                "finished": False,
                "started": False,
                "team_h_score": None,
                "team_a_score": None,
                "stats": [],
            },
            {
                "id": 2,
                "event": 25,
                "team_h": 1,
                "team_a": 8,
                "team_h_difficulty": 2,
                "team_a_difficulty": 5,
                "kickoff_time": "2024-02-10T14:00:00Z",
                "finished": False,
                "started": False,
                "team_h_score": None,
                "team_a_score": None,
                "stats": [],
            },
        ]

    @pytest.mark.asyncio
    async def test_get_bootstrap_static(self, client, mock_bootstrap_response):
        """Test fetching bootstrap-static data."""
        with patch.object(client, "_get", new_callable=AsyncMock) as mock_get:
            mock_get.return_value = mock_bootstrap_response

            result = await client.get_bootstrap_static()

            mock_get.assert_called_once_with("bootstrap-static/")
            assert "elements" in result
            assert "teams" in result
            assert "events" in result

    @pytest.mark.asyncio
    async def test_get_bootstrap_static_caching(self, client, mock_bootstrap_response):
        """Test that bootstrap data is cached."""
        with patch.object(client, "_get", new_callable=AsyncMock) as mock_get:
            mock_get.return_value = mock_bootstrap_response

            # First call
            await client.get_bootstrap_static()
            # Second call should use cache
            await client.get_bootstrap_static()

            # Should only be called once due to caching
            mock_get.assert_called_once()

    @pytest.mark.asyncio
    async def test_get_bootstrap_static_force_refresh(self, client, mock_bootstrap_response):
        """Test force refresh bypasses cache."""
        with patch.object(client, "_get", new_callable=AsyncMock) as mock_get:
            mock_get.return_value = mock_bootstrap_response

            await client.get_bootstrap_static()
            await client.get_bootstrap_static(force_refresh=True)

            assert mock_get.call_count == 2

    @pytest.mark.asyncio
    async def test_get_players(self, client, mock_bootstrap_response):
        """Test fetching all players."""
        with patch.object(client, "_get", new_callable=AsyncMock) as mock_get:
            mock_get.return_value = mock_bootstrap_response

            players = await client.get_players()

            assert len(players) == 2
            assert all(isinstance(p, Player) for p in players)
            assert players[0].web_name == "Salah"
            assert players[1].web_name == "Haaland"

    @pytest.mark.asyncio
    async def test_get_player_by_id(self, client, mock_bootstrap_response):
        """Test fetching specific player by ID."""
        with patch.object(client, "_get", new_callable=AsyncMock) as mock_get:
            mock_get.return_value = mock_bootstrap_response

            player = await client.get_player(1)

            assert player is not None
            assert player.id == 1
            assert player.web_name == "Salah"

    @pytest.mark.asyncio
    async def test_get_player_not_found(self, client, mock_bootstrap_response):
        """Test fetching non-existent player returns None."""
        with patch.object(client, "_get", new_callable=AsyncMock) as mock_get:
            mock_get.return_value = mock_bootstrap_response

            player = await client.get_player(999)

            assert player is None

    @pytest.mark.asyncio
    async def test_get_teams(self, client, mock_bootstrap_response):
        """Test fetching all teams."""
        with patch.object(client, "_get", new_callable=AsyncMock) as mock_get:
            mock_get.return_value = mock_bootstrap_response

            teams = await client.get_teams()

            assert len(teams) == 2
            assert all(isinstance(t, Team) for t in teams)
            assert teams[0].short_name == "MCI"
            assert teams[1].short_name == "LIV"

    @pytest.mark.asyncio
    async def test_get_team_by_id(self, client, mock_bootstrap_response):
        """Test fetching specific team by ID."""
        with patch.object(client, "_get", new_callable=AsyncMock) as mock_get:
            mock_get.return_value = mock_bootstrap_response

            team = await client.get_team(14)

            assert team is not None
            assert team.id == 14
            assert team.name == "Liverpool"

    @pytest.mark.asyncio
    async def test_get_team_not_found(self, client, mock_bootstrap_response):
        """Test fetching non-existent team returns None."""
        with patch.object(client, "_get", new_callable=AsyncMock) as mock_get:
            mock_get.return_value = mock_bootstrap_response

            team = await client.get_team(999)

            assert team is None

    @pytest.mark.asyncio
    async def test_get_fixtures(self, client, mock_fixtures_response):
        """Test fetching all fixtures."""
        with patch.object(client, "_get", new_callable=AsyncMock) as mock_get:
            mock_get.return_value = mock_fixtures_response

            fixtures = await client.get_fixtures()

            mock_get.assert_called_once_with("fixtures/")
            assert len(fixtures) == 2
            assert all(isinstance(f, Fixture) for f in fixtures)

    @pytest.mark.asyncio
    async def test_get_fixtures_by_gameweek(self, client, mock_fixtures_response):
        """Test fetching fixtures for specific gameweek."""
        with patch.object(client, "_get", new_callable=AsyncMock) as mock_get:
            mock_get.return_value = mock_fixtures_response

            await client.get_fixtures(gameweek=25)

            mock_get.assert_called_once_with("fixtures/?event=25")

    @pytest.mark.asyncio
    async def test_get_gameweeks(self, client, mock_bootstrap_response):
        """Test fetching all gameweeks."""
        with patch.object(client, "_get", new_callable=AsyncMock) as mock_get:
            mock_get.return_value = mock_bootstrap_response

            gameweeks = await client.get_gameweeks()

            assert len(gameweeks) == 2
            assert gameweeks[0]["id"] == 24
            assert gameweeks[1]["id"] == 25

    @pytest.mark.asyncio
    async def test_get_current_gameweek(self, client, mock_bootstrap_response):
        """Test fetching current gameweek."""
        with patch.object(client, "_get", new_callable=AsyncMock) as mock_get:
            mock_get.return_value = mock_bootstrap_response

            current_gw = await client.get_current_gameweek()

            assert current_gw is not None
            assert current_gw["id"] == 24
            assert current_gw["is_current"] is True

    @pytest.mark.asyncio
    async def test_get_next_gameweek(self, client, mock_bootstrap_response):
        """Test fetching next gameweek."""
        with patch.object(client, "_get", new_callable=AsyncMock) as mock_get:
            mock_get.return_value = mock_bootstrap_response

            next_gw = await client.get_next_gameweek()

            assert next_gw is not None
            assert next_gw["id"] == 25
            assert next_gw["is_next"] is True

    @pytest.mark.asyncio
    async def test_get_next_gameweek_none(self, client):
        """Test no next gameweek at end of season."""
        data = {
            "elements": [],
            "teams": [],
            "events": [
                {"id": 38, "is_current": True, "is_next": False},
            ],
        }
        with patch.object(client, "_get", new_callable=AsyncMock) as mock_get:
            mock_get.return_value = data

            next_gw = await client.get_next_gameweek()

            assert next_gw is None

    @pytest.mark.asyncio
    async def test_get_player_detail(self, client):
        """Test fetching player detail."""
        mock_response = {
            "history": [
                {"round": 1, "total_points": 5},
                {"round": 2, "total_points": 8},
            ],
            "fixtures": [],
        }
        with patch.object(client, "_get", new_callable=AsyncMock) as mock_get:
            mock_get.return_value = mock_response

            summary = await client.get_player_detail(100)

            mock_get.assert_called_once_with("element-summary/100/")
            assert "history" in summary
            assert len(summary["history"]) == 2

    @pytest.mark.asyncio
    async def test_get_manager_entry(self, client):
        """Test fetching manager's team entry."""
        mock_response = {
            "id": 12345,
            "name": "Test Team",
            "summary_overall_points": 1000,
        }
        with patch.object(client, "_get", new_callable=AsyncMock) as mock_get:
            mock_get.return_value = mock_response

            team = await client.get_manager_entry(12345)

            mock_get.assert_called_once_with("entry/12345/")
            assert team["id"] == 12345

    @pytest.mark.asyncio
    async def test_get_manager_history(self, client):
        """Test fetching manager history."""
        mock_response = {
            "current": [{"event": 1, "points": 50}],
            "past": [],
        }
        with patch.object(client, "_get", new_callable=AsyncMock) as mock_get:
            mock_get.return_value = mock_response

            history = await client.get_manager_history(12345)

            mock_get.assert_called_once_with("entry/12345/history/")
            assert "current" in history

    @pytest.mark.asyncio
    async def test_get_manager_picks(self, client):
        """Test fetching manager's GW picks."""
        mock_response = {
            "picks": [
                {"element": 1, "position": 1, "is_captain": False},
                {"element": 2, "position": 2, "is_captain": True},
            ],
            "active_chip": None,
        }
        with patch.object(client, "_get", new_callable=AsyncMock) as mock_get:
            mock_get.return_value = mock_response

            picks = await client.get_manager_picks(12345, 25)

            mock_get.assert_called_once_with("entry/12345/event/25/picks/")
            assert len(picks["picks"]) == 2

    @pytest.mark.asyncio
    async def test_get_classic_league_standings(self, client):
        """Test fetching classic league standings."""
        mock_response = {
            "league": {"id": 1000, "name": "Test League"},
            "standings": {"results": []},
        }
        with patch.object(client, "_get", new_callable=AsyncMock) as mock_get:
            mock_get.return_value = mock_response

            standings = await client.get_classic_league_standings(1000)

            mock_get.assert_called_once_with("leagues-classic/1000/standings/?page_standings=1")
            assert "league" in standings

    @pytest.mark.asyncio
    async def test_get_classic_league_standings_pagination(self, client):
        """Test fetching classic league with page param."""
        mock_response = {"league": {}, "standings": {"results": []}}
        with patch.object(client, "_get", new_callable=AsyncMock) as mock_get:
            mock_get.return_value = mock_response

            await client.get_classic_league_standings(1000, page=3)

            mock_get.assert_called_once_with("leagues-classic/1000/standings/?page_standings=3")

    @pytest.mark.asyncio
    async def test_get_dream_team(self, client):
        """Test fetching dream team."""
        mock_response = {
            "team": [{"element": 1, "points": 15}],
            "top_player": {"id": 1, "points": 15},
        }
        with patch.object(client, "_get", new_callable=AsyncMock) as mock_get:
            mock_get.return_value = mock_response

            dream_team = await client.get_dream_team(25)

            mock_get.assert_called_once_with("dream-team/25/")
            assert "team" in dream_team

    @pytest.mark.asyncio
    async def test_get_gameweek_live(self, client):
        """Test fetching live gameweek data."""
        mock_response = {
            "elements": [
                {"id": 1, "stats": {"total_points": 10}},
            ],
        }
        with patch.object(client, "_get", new_callable=AsyncMock) as mock_get:
            mock_get.return_value = mock_response

            live = await client.get_gameweek_live(25)

            mock_get.assert_called_once_with("event/25/live/")
            assert "elements" in live

    @pytest.mark.asyncio
    async def test_get_fdr(self, client, mock_bootstrap_response, mock_fixtures_response):
        """Test FDR calculation."""
        # Combine bootstrap and fixtures data
        with patch.object(client, "get_fixtures", new_callable=AsyncMock) as mock_fixtures, \
             patch.object(client, "get_teams", new_callable=AsyncMock) as mock_teams:

            # Create team fixtures
            team_13 = Team(
                id=13, name="Man City", short_name="MCI", code=43,
                strength=5, strength_overall_home=1350, strength_overall_away=1300,
                strength_attack_home=1400, strength_attack_away=1350,
                strength_defence_home=1300, strength_defence_away=1250,
            )
            team_14 = Team(
                id=14, name="Liverpool", short_name="LIV", code=14,
                strength=5, strength_overall_home=1320, strength_overall_away=1280,
                strength_attack_home=1350, strength_attack_away=1320,
                strength_defence_home=1290, strength_defence_away=1240,
            )

            mock_teams.return_value = [team_13, team_14]

            # Create fixtures
            fixtures = [
                Fixture(
                    id=1, event=25, team_h=13, team_a=14,
                    team_h_difficulty=4, team_a_difficulty=4,
                ),
            ]
            mock_fixtures.return_value = fixtures

            fdr = await client.get_fdr()

            assert 13 in fdr
            assert 14 in fdr
            assert len(fdr[13]) == 1
            assert len(fdr[14]) == 1

            # Check FDR data structure
            assert fdr[13][0]["gameweek"] == 25
            assert fdr[13][0]["opponent_id"] == 14
            assert fdr[13][0]["is_home"] is True
            assert fdr[13][0]["difficulty"] == 4


class TestFPLClientClose:
    """Tests for FPLClient close and context manager."""

    @pytest.mark.asyncio
    async def test_close_delegates_to_aclose(self):
        client = FPLClient()
        client._http = AsyncMock()
        await client.close()
        client._http.aclose.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_context_manager_closes_client(self):
        client = FPLClient()
        client._http = AsyncMock()
        async with client:
            pass
        client._http.aclose.assert_awaited_once()


class TestFPLClientInit:
    """Tests for FPLClient initialization."""

    def test_default_timeout(self):
        """Test default timeout is set."""
        client = FPLClient()
        assert client.timeout == 30.0

    def test_custom_timeout(self):
        """Test custom timeout."""
        client = FPLClient(timeout=60.0)
        assert client.timeout == 60.0

    def test_initial_cache_empty(self):
        """Test bootstrap cache is initially empty."""
        client = FPLClient()
        assert client._bootstrap_data is None


