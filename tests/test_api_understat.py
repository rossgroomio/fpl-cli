"""Tests for Understat API client."""

import pytest
from unittest.mock import AsyncMock, patch, MagicMock
import httpx

from fpl_cli.api.understat import UnderstatClient, match_fpl_to_understat, TEAM_NAME_MAP, POSITION_MAP


# --- Fixtures ---

@pytest.fixture
def mock_player_data():
    """Mock raw player data from Understat."""
    return {
        "id": "12345",
        "player_name": "Mohamed Salah",
        "team_title": "Liverpool",
        "position": "M F",
        "games": "20",
        "time": "1800",
        "goals": "15",
        "assists": "8",
        "xG": "12.5",
        "xA": "6.3",
        "npxG": "10.2",
        "xGChain": "18.5",
        "xGBuildup": "5.2",
        "shots": "60",
        "key_passes": "40",
        "npg": "13",
    }


@pytest.fixture
def mock_html_with_data():
    """Mock HTML containing embedded JSON data."""
    return """
    <html>
    <head></head>
    <body>
    <script>
    var playersData = JSON.parse('[{"id":"12345","player_name":"Mohamed Salah","team_title":"Liverpool","position":"M F","games":"20","time":"1800","goals":"15","assists":"8","xG":"12.5","xA":"6.3","npxG":"10.2","xGChain":"18.5","xGBuildup":"5.2","shots":"60","key_passes":"40","npg":"13"}]');
    </script>
    </body>
    </html>
    """


@pytest.fixture
def mock_league_api_response():
    """Mock JSON API response from getLeagueData endpoint."""
    return {
        "players": [
            {
                "id": "12345",
                "player_name": "Mohamed Salah",
                "team_title": "Liverpool",
                "position": "M F",
                "games": "20",
                "time": "1800",
                "goals": "15",
                "assists": "8",
                "xG": "12.5",
                "xA": "6.3",
                "npxG": "10.2",
                "xGChain": "18.5",
                "xGBuildup": "5.2",
                "shots": "60",
                "key_passes": "40",
                "npg": "13",
            }
        ],
        "teams": {},
        "dates": [],
    }


@pytest.fixture
def mock_understat_players():
    """Mock list of parsed Understat players."""
    return [
        {
            "id": 12345,
            "name": "Mohamed Salah",
            "team": "Liverpool",
            "position": "M F",
            "games": 20,
            "minutes": 1800,
            "goals": 15,
            "assists": 8,
            "xG": 12.5,
            "xA": 6.3,
            "xG_per_90": 0.63,
            "xA_per_90": 0.32,
            "xGI_per_90": 0.94,
            "goals_minus_xG": 2.5,
            "assists_minus_xA": 1.7,
        },
        {
            "id": 67890,
            "name": "Erling Haaland",
            "team": "Manchester City",
            "position": "F",
            "games": 20,
            "minutes": 1700,
            "goals": 25,
            "assists": 5,
            "xG": 22.0,
            "xA": 3.0,
            "xG_per_90": 1.16,
            "xA_per_90": 0.16,
            "xGI_per_90": 1.32,
            "goals_minus_xG": 3.0,
            "assists_minus_xA": 2.0,
        },
    ]


# --- TestUnderstatClientInit ---

class TestUnderstatClientInit:
    """Tests for UnderstatClient initialization."""

    def test_client_initialization(self):
        """Test default initialization."""
        client = UnderstatClient()
        assert client.timeout == 30.0
        assert isinstance(client.season_year, int)

    def test_client_custom_timeout(self):
        """Test custom timeout is applied."""
        client = UnderstatClient(timeout=60.0)
        assert client.timeout == 60.0

    def test_client_explicit_season_year(self):
        """Test explicit season_year is applied."""
        client = UnderstatClient(season_year=2024)
        assert client.season_year == 2024


# --- TestUnderstatClientExtract ---

class TestUnderstatClientExtract:
    """Tests for _extract_json_data method."""

    def test_extract_json_data(self, mock_html_with_data):
        """Test extracting JSON from HTML."""
        client = UnderstatClient()

        result = client._extract_json_data(mock_html_with_data, "playersData")

        assert result is not None
        assert isinstance(result, list)
        assert len(result) == 1
        assert result[0]["player_name"] == "Mohamed Salah"

    def test_extract_json_data_not_found(self):
        """Test extraction returns None when variable not found."""
        client = UnderstatClient()
        html = "<html><body><script>var otherData = {};</script></body></html>"

        result = client._extract_json_data(html, "playersData")

        assert result is None

    def test_extract_json_data_empty_html(self):
        """Test extraction from empty HTML."""
        client = UnderstatClient()

        result = client._extract_json_data("", "playersData")

        assert result is None


# --- TestUnderstatClientLeaguePlayers ---

class TestUnderstatClientLeaguePlayers:
    """Tests for get_league_players method."""

    @pytest.mark.asyncio
    async def test_get_league_players(self, mock_league_api_response):
        """Test fetching league players via JSON API."""
        client = UnderstatClient()

        with patch.object(client, "_get_api_json", new_callable=AsyncMock) as mock_get:
            mock_get.return_value = mock_league_api_response

            result = await client.get_league_players()

            mock_get.assert_called_once_with("getLeagueData/EPL/2025", referer="league/EPL/2025")
            assert len(result) == 1
            assert result[0]["name"] == "Mohamed Salah"

    @pytest.mark.asyncio
    async def test_get_league_players_custom_season(self, mock_league_api_response):
        """Test fetching players for custom season."""
        client = UnderstatClient()

        with patch.object(client, "_get_api_json", new_callable=AsyncMock) as mock_get:
            mock_get.return_value = mock_league_api_response

            result = await client.get_league_players(season="2023")

            mock_get.assert_called_once_with("getLeagueData/EPL/2023", referer="league/EPL/2023")

    @pytest.mark.asyncio
    async def test_get_league_players_empty(self):
        """Test empty result when no data found."""
        client = UnderstatClient()

        with patch.object(client, "_get_api_json", new_callable=AsyncMock) as mock_get:
            mock_get.return_value = {"players": [], "teams": {}, "dates": []}

            result = await client.get_league_players()

            assert result == []


# --- TestUnderstatClientPlayer ---

class TestUnderstatClientPlayer:
    """Tests for get_player method."""

    @pytest.mark.asyncio
    async def test_get_player(self):
        """Test fetching single player via JSON API."""
        client = UnderstatClient()
        mock_response = {
            "matches": [{"id": "1", "goals": "2"}],
            "shots": [{"id": "1", "xG": "0.5"}],
            "groups": {},
        }

        with patch.object(client, "_get_api_json", new_callable=AsyncMock) as mock_get:
            mock_get.return_value = mock_response

            result = await client.get_player(12345)

            mock_get.assert_called_once_with("getPlayerData/12345", referer="player/12345")
            assert result is not None
            assert result["id"] == 12345
            assert "matches" in result
            assert "shots" in result

    @pytest.mark.asyncio
    async def test_get_player_includes_groups(self):
        """Test get_player returns groupsData for situation profiles."""
        client = UnderstatClient()
        mock_response = {
            "matches": [{"id": "1", "goals": "2"}],
            "shots": [{"id": "1", "xG": "0.5"}],
            "groups": {
                "situation": {
                    "OpenPlay": {"xG": "5.0", "shots": "30"},
                    "FromCorner": {"xG": "1.2", "shots": "8"},
                },
            },
        }

        with patch.object(client, "_get_api_json", new_callable=AsyncMock) as mock_get:
            mock_get.return_value = mock_response

            result = await client.get_player(12345)

            assert result is not None
            assert "groups" in result
            assert "situation" in result["groups"]
            assert result["groups"]["situation"]["OpenPlay"]["xG"] == "5.0"

    @pytest.mark.asyncio
    async def test_get_player_not_found(self):
        """Test player not found returns None."""
        client = UnderstatClient()

        with patch.object(client, "_get_api_json", new_callable=AsyncMock) as mock_get:
            mock_get.side_effect = httpx.HTTPStatusError(
                "Not Found", request=MagicMock(), response=MagicMock(status_code=404)
            )

            result = await client.get_player(99999)

            assert result is None


# --- TestUnderstatClientTeam ---

class TestUnderstatClientTeam:
    """Tests for get_team method."""

    @pytest.fixture
    def mock_team_json(self):
        """Mock JSON response from Understat getTeamData API."""
        return {
            "players": [
                {
                    "id": "12345",
                    "player_name": "Mohamed Salah",
                    "team_title": "Liverpool",
                    "position": "M F",
                    "games": "20",
                    "time": "1800",
                    "goals": "15",
                    "assists": "8",
                    "xG": "12.5",
                    "xA": "6.3",
                    "npxG": "10.2",
                    "xGChain": "18.5",
                    "xGBuildup": "5.2",
                    "shots": "60",
                    "key_passes": "40",
                    "npg": "13",
                }
            ],
            "dates": [
                {"id": "1", "isResult": True, "side": "h", "xG": {"h": "2.5", "a": "0.8"}},
                {"id": "2", "isResult": True, "side": "a", "xG": {"h": "1.2", "a": "1.5"}},
            ],
            "statistics": {},
        }

    @pytest.mark.asyncio
    async def test_get_team(self, mock_team_json):
        """Test fetching team data via JSON API."""
        client = UnderstatClient()

        with patch.object(client, "_get_team_json", new_callable=AsyncMock) as mock_get:
            mock_get.return_value = mock_team_json

            result = await client.get_team("Liverpool")

            mock_get.assert_called_once_with("Liverpool", "2025")
            assert result is not None
            assert result["team"] == "Liverpool"
            assert "players" in result
            assert "matches" in result
            assert len(result["matches"]) == 2

    @pytest.mark.asyncio
    async def test_get_team_name_mapping(self, mock_team_json):
        """Test team name mapping from FPL to Understat format."""
        client = UnderstatClient()

        with patch.object(client, "_get_team_json", new_callable=AsyncMock) as mock_get:
            mock_get.return_value = mock_team_json

            await client.get_team("Man City")

            # FPL name mapped; spaces become underscores in the url_name arg
            mock_get.assert_called_once_with("Manchester_City", "2025")

    @pytest.mark.asyncio
    async def test_get_team_not_found(self):
        """Test team not found returns None when API returns None."""
        client = UnderstatClient()

        with patch.object(client, "_get_team_json", new_callable=AsyncMock) as mock_get:
            mock_get.return_value = None

            result = await client.get_team("Invalid Team")

            assert result is None


# --- TestUnderstatClientParsing ---

class TestUnderstatClientParsing:
    """Tests for _parse_player method."""

    def test_parse_player(self, mock_player_data):
        """Test parsing raw player data."""
        client = UnderstatClient()

        result = client._parse_player(mock_player_data)

        assert result["id"] == 12345
        assert result["name"] == "Mohamed Salah"
        assert result["team"] == "Liverpool"
        assert result["position"] == "M F"
        assert result["games"] == 20
        assert result["minutes"] == 1800
        assert result["goals"] == 15
        assert result["assists"] == 8
        assert result["xG"] == 12.5
        assert result["xA"] == 6.3

    def test_parse_player_calculates_per_90(self, mock_player_data):
        """Test per-90 calculations."""
        client = UnderstatClient()

        result = client._parse_player(mock_player_data)

        # xG per 90: 12.5 / 1800 * 90 = 0.625, rounded to 2 decimals
        assert result["xG_per_90"] == pytest.approx(0.62, abs=0.01)
        # xA per 90: 6.3 / 1800 * 90 = 0.315, rounded to 2 decimals
        assert result["xA_per_90"] == pytest.approx(0.32, abs=0.01)
        # xGI per 90: (12.5 + 6.3) / 1800 * 90 = 0.94
        assert result["xGI_per_90"] == pytest.approx(0.94, abs=0.01)

    def test_parse_player_over_underperformance(self, mock_player_data):
        """Test over/underperformance calculations."""
        client = UnderstatClient()

        result = client._parse_player(mock_player_data)

        # Goals - xG: 15 - 12.5 = 2.5
        assert result["goals_minus_xG"] == pytest.approx(2.5)
        # Assists - xA: 8 - 6.3 = 1.7
        assert result["assists_minus_xA"] == pytest.approx(1.7)


    def test_parse_player_extended_per_90(self, mock_player_data):
        """Test npxG, xGChain, xGBuildup per-90 calculations."""
        client = UnderstatClient()
        result = client._parse_player(mock_player_data)

        # npxG per 90: 10.2 / 1800 * 90 = 0.51
        assert result["npxG_per_90"] == pytest.approx(0.51, abs=0.01)
        # xGChain per 90: 18.5 / 1800 * 90 = 0.925
        assert result["xGChain_per_90"] == pytest.approx(0.93, abs=0.01)
        # xGBuildup per 90: 5.2 / 1800 * 90 = 0.26
        assert result["xGBuildup_per_90"] == pytest.approx(0.26, abs=0.01)

    def test_parse_player_penalty_xg_delta(self, mock_player_data):
        """Test penalty xG inflation metric."""
        client = UnderstatClient()
        result = client._parse_player(mock_player_data)

        # xG - npxG = 12.5 - 10.2 = 2.3
        assert result["penalty_xG"] == pytest.approx(2.3, abs=0.01)


class TestUnderstatClientPer90:
    """Tests for _per_90 method."""

    def test_per_90_calculation(self):
        """Test standard per-90 calculation."""
        client = UnderstatClient()

        result = client._per_90(10.0, 900)

        # 10 / 900 * 90 = 1.0
        assert result == 1.0

    def test_per_90_zero_minutes(self):
        """Test per-90 with zero minutes returns 0."""
        client = UnderstatClient()

        result = client._per_90(10.0, 0)

        assert result == 0.0

    def test_per_90_rounding(self):
        """Test per-90 rounds to 2 decimal places."""
        client = UnderstatClient()

        result = client._per_90(5.0, 900)

        # 5 / 900 * 90 = 0.5
        assert result == 0.5


# --- TestMatchFPLToUnderstat ---

class TestMatchFPLToUnderstat:
    """Tests for match_fpl_to_understat function."""

    def test_match_exact(self, mock_understat_players):
        """Test exact name match."""
        result = match_fpl_to_understat(
            "Mohamed Salah", "Liverpool", mock_understat_players
        )

        assert result is not None
        assert result["name"] == "Mohamed Salah"

    def test_match_partial(self, mock_understat_players):
        """Test partial name match (FPL name in Understat name)."""
        result = match_fpl_to_understat(
            "Salah", "Liverpool", mock_understat_players
        )

        assert result is not None
        assert result["name"] == "Mohamed Salah"

    def test_match_surname(self, mock_understat_players):
        """Test matching by surname only."""
        result = match_fpl_to_understat(
            "Haaland", "Man City", mock_understat_players
        )

        assert result is not None
        assert result["name"] == "Erling Haaland"

    def test_match_wrong_team(self, mock_understat_players):
        """Test no match when team doesn't match."""
        result = match_fpl_to_understat(
            "Salah", "Man City", mock_understat_players  # Wrong team
        )

        assert result is None

    def test_match_not_found(self, mock_understat_players):
        """Test no match when player not in list."""
        result = match_fpl_to_understat(
            "Unknown Player", "Liverpool", mock_understat_players
        )

        assert result is None

    def test_match_with_position_boost(self, mock_understat_players):
        """Test position match gives higher confidence."""
        result = match_fpl_to_understat(
            "Salah", "Liverpool", mock_understat_players,
            fpl_position="MID", fpl_minutes=1800,
        )
        assert result is not None
        assert result["name"] == "Mohamed Salah"

    def test_match_abbreviated_name_with_dot(self):
        """Test 'Bruno G.' matches 'Bruno Guimarães' via prefix matching."""
        players = [
            {"id": 1, "name": "Bruno Guimarães", "team": "Newcastle United", "position": "M", "minutes": 2000},
        ]
        result = match_fpl_to_understat(
            "Bruno G.", "Newcastle", players,
            fpl_position="MID", fpl_minutes=2000,
        )
        assert result is not None
        assert result["name"] == "Bruno Guimarães"

    def test_match_dot_initial_multi_word_surname(self):
        """Test 'E.Le Fee' matches 'Enzo Le Fee' via prefix + exact words."""
        players = [
            {"id": 1, "name": "Enzo Le Fee", "team": "Aston Villa", "position": "M", "minutes": 800},
        ]
        result = match_fpl_to_understat(
            "E.Le Fee", "Aston Villa", players,
            fpl_position="MID", fpl_minutes=800,
        )
        assert result is not None
        assert result["name"] == "Enzo Le Fee"

    def test_match_initial_dot_surname(self, mock_understat_players):
        """Test 'M.Salah' matches 'Mohamed Salah' via prefix matching."""
        result = match_fpl_to_understat(
            "M.Salah", "Liverpool", mock_understat_players,
            fpl_position="MID", fpl_minutes=1800,
        )
        assert result is not None
        assert result["name"] == "Mohamed Salah"

    def test_match_b_silva_prefers_bernardo(self):
        """Test 'B. Silva' prefers 'Bernardo Silva' over plain 'Silva'."""
        players = [
            {"id": 1, "name": "Silva", "team": "Manchester City", "position": "M", "minutes": 1500},
            {"id": 2, "name": "Bernardo Silva", "team": "Manchester City", "position": "M", "minutes": 1600},
        ]
        result = match_fpl_to_understat(
            "B. Silva", "Man City", players,
            fpl_position="MID", fpl_minutes=1600,
        )
        assert result is not None
        assert result["name"] == "Bernardo Silva"

    def test_match_hyphenated_name(self):
        """Test hyphenated names like 'Alexander-Arnold' match correctly."""
        players = [
            {"id": 1, "name": "Trent Alexander-Arnold", "team": "Liverpool", "position": "D", "minutes": 2500},
        ]
        result = match_fpl_to_understat(
            "Alexander-Arnold", "Liverpool", players,
            fpl_position="DEF", fpl_minutes=2500,
        )
        assert result is not None
        assert result["name"] == "Trent Alexander-Arnold"

    def test_match_no_false_substring(self):
        """Test 'Son' does not match 'Anderson' (word-level, not substring)."""
        players = [
            {"id": 1, "name": "Anderson", "team": "Liverpool", "position": "M", "minutes": 1000},
        ]
        result = match_fpl_to_understat(
            "Son", "Liverpool", players,
        )
        assert result is None

    def test_match_is_sync(self):
        """match_fpl_to_understat should be a sync function (no async)."""
        import inspect
        assert not inspect.iscoroutinefunction(match_fpl_to_understat)


# --- TestUnderstatClientCaching ---

class TestUnderstatClientCaching:
    """Tests for league player caching."""

    @pytest.mark.asyncio
    async def test_league_players_cached_on_second_call(self, mock_league_api_response):
        """Second call returns cached data without API fetch."""
        client = UnderstatClient()

        with patch.object(client, "_get_api_json", new_callable=AsyncMock) as mock_get:
            mock_get.return_value = mock_league_api_response

            result1 = await client.get_league_players()
            result2 = await client.get_league_players()

            mock_get.assert_called_once()  # Only one API call
            assert result1 == result2

    @pytest.mark.asyncio
    async def test_league_players_different_season_not_cached(self, mock_league_api_response):
        """Different season bypasses cache."""
        client = UnderstatClient()

        with patch.object(client, "_get_api_json", new_callable=AsyncMock) as mock_get:
            mock_get.return_value = mock_league_api_response

            await client.get_league_players(season="2024")
            await client.get_league_players(season="2025")

            assert mock_get.call_count == 2


# --- TestTeamNameMap ---

class TestTeamNameMap:
    """Tests for TEAM_NAME_MAP constant."""

    def test_team_name_map_contains_common_teams(self):
        """Test map contains common FPL team names."""
        assert "Man City" in TEAM_NAME_MAP
        assert TEAM_NAME_MAP["Man City"] == "Manchester City"

        assert "Man Utd" in TEAM_NAME_MAP
        assert TEAM_NAME_MAP["Man Utd"] == "Manchester United"

        assert "Spurs" in TEAM_NAME_MAP
        assert TEAM_NAME_MAP["Spurs"] == "Tottenham"

    def test_team_name_map_handles_same_names(self):
        """Test teams with same name in both systems."""
        assert "Liverpool" in TEAM_NAME_MAP
        assert TEAM_NAME_MAP["Liverpool"] == "Liverpool"

        assert "Arsenal" in TEAM_NAME_MAP
        assert TEAM_NAME_MAP["Arsenal"] == "Arsenal"
