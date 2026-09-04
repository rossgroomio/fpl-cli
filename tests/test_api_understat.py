"""Tests for Understat API client."""

import logging
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from fpl_cli.api.understat import (
    TEAM_NAME_MAP,
    UNDERSTAT_TEAM_UNMATCHED,
    UnderstatClient,
    match_fpl_to_understat,
    split_team_titles,
    understat_club_rows,
    understat_join_warnings,
    unmatched_understat_teams,
)
from fpl_cli.season import understat_season

# Derived from the same helper the client uses, so assertions follow the
# season rollover instead of pinning the season these tests were written in.
CURRENT_SEASON = understat_season()

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

            mock_get.assert_called_once_with(
                f"getLeagueData/EPL/{CURRENT_SEASON}", referer=f"league/EPL/{CURRENT_SEASON}"
            )
            assert len(result) == 1
            assert result[0]["name"] == "Mohamed Salah"

    @pytest.mark.asyncio
    async def test_get_league_players_custom_season(self, mock_league_api_response):
        """Test fetching players for custom season."""
        client = UnderstatClient()

        with patch.object(client, "_get_api_json", new_callable=AsyncMock) as mock_get:
            mock_get.return_value = mock_league_api_response

            await client.get_league_players(season="2023")

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

            mock_get.assert_called_once_with("Liverpool", CURRENT_SEASON)
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
            mock_get.assert_called_once_with("Manchester_City", CURRENT_SEASON)

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

    def test_match_wrong_team_falls_back_to_name(self, mock_understat_players):
        """A club that matches nothing falls through to the name-only pass (#234)."""
        result = match_fpl_to_understat(
            "Salah", "Man City", mock_understat_players  # Club Understat disagrees with
        )

        assert result is not None
        assert result["name"] == "Mohamed Salah"

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

    def test_match_transferred_player_comma_joined_team(self):
        """A mid-season mover's comma-joined team_title still joins (#94)."""
        players = [
            {
                "id": 8706,
                "name": "Eberechi Eze",
                "team": "Arsenal,Crystal Palace",
                "position": "M",
                "minutes": 1928,
            },
        ]
        result = match_fpl_to_understat(
            "Eze", "Arsenal", players, fpl_position="MID", fpl_minutes=1928
        )
        assert result is not None
        assert result["id"] == 8706

    def test_match_transferred_player_matches_former_club(self):
        """Either component of a comma-joined title resolves the same player."""
        players = [
            {
                "id": 8706,
                "name": "Eberechi Eze",
                "team": "Arsenal,Crystal Palace",
                "position": "M",
                "minutes": 1928,
            },
        ]
        result = match_fpl_to_understat(
            "Eze", "Crystal Palace", players, fpl_position="MID", fpl_minutes=1928
        )
        assert result is not None
        assert result["id"] == 8706

    def test_match_transferred_player_mapped_team_name(self):
        """The FPL→Understat name map still applies to a joined title."""
        players = [
            {
                "id": 1,
                "name": "Antoine Semenyo",
                "team": "Bournemouth,Manchester City",
                "position": "F M S",
                "minutes": 3220,
            },
        ]
        result = match_fpl_to_understat(
            "Semenyo", "Man City", players, fpl_position="MID", fpl_minutes=3220
        )
        assert result is not None
        assert result["id"] == 1

    def test_match_comma_title_does_not_match_unrelated_team(self):
        """Splitting must not turn the gate into a substring match.

        An unrelated club now falls through to the name-only pass (#234), so
        the gate is shown at the prefix tier, which that pass refuses.
        """
        players = [
            {
                "id": 1,
                "name": "Eberechi Eze",
                "team": "Arsenal,Crystal Palace",
                "position": "M",
                "minutes": 1928,
            },
        ]
        result = match_fpl_to_understat(
            "E.Eze", "Liverpool", players, fpl_position="MID", fpl_minutes=1928
        )
        assert result is None

        # The same abbreviated name resolves once a title names the club.
        assert match_fpl_to_understat(
            "E.Eze", "Arsenal", players, fpl_position="MID", fpl_minutes=1928
        ) is not None

    def test_match_mover_before_first_appearance_for_new_club(self):
        """A deadline-day mover carries only his old club's title (#234).

        Understat lists the clubs a player has actually appeared for, so until
        he features for the new one there is no comma-joined title for #151 to
        split — the FPL club is in neither component. The new club is carried
        by another row, which is what tells the fallback the club resolved.
        """
        players = [
            {
                "id": 4242,
                "name": "Marc Guiu",
                "team": "Chelsea",
                "position": "F S",
                "minutes": 25,
            },
            {"id": 7, "name": "Dan Ballard", "team": "Sunderland", "position": "D", "minutes": 900},
        ]
        result = match_fpl_to_understat(
            "Guiu", "Sunderland", players, fpl_position="FWD", fpl_minutes=25
        )
        assert result is not None
        assert result["id"] == 4242

    def test_unresolved_club_fails_as_a_block_not_player_by_player(self):
        """A club no Understat row carries never reaches the fallback.

        A TEAM_NAME_MAP gap or a roster Understat has yet to ingest fails every
        one of that club's players identically. Letting each of them scan the
        league by name would turn one legible warning into twenty players
        silently wearing a stranger's xG.
        """
        players = [
            {"id": 1, "name": "Marc Guiu", "team": "Chelsea", "position": "F S", "minutes": 25},
        ]
        result = match_fpl_to_understat(
            "Marc Guiu", "Coventry City", players, fpl_position="FWD", fpl_minutes=25
        )
        assert result is None

    def test_cross_club_fallback_rejects_prefix_only_names(self):
        """Without a club to agree, an abbreviated name is not enough."""
        players = [
            {"id": 1, "name": "Bernardo Silva", "team": "Manchester City", "position": "M", "minutes": 1600},
            {"id": 2, "name": "Harry Wilson", "team": "Fulham", "position": "M", "minutes": 1600},
        ]
        result = match_fpl_to_understat(
            "B. Silva", "Fulham", players, fpl_position="MID", fpl_minutes=1600
        )
        assert result is None

    def test_cross_club_fallback_refuses_ambiguous_namesakes(self):
        """Two equally-scoring namesakes elsewhere are declined, not guessed at."""
        players = [
            {"id": 1, "name": "Thiago Silva", "team": "Chelsea", "position": "D", "minutes": 900},
            {"id": 2, "name": "Bernardo Silva", "team": "Manchester City", "position": "D", "minutes": 900},
            {"id": 3, "name": "Harry Wilson", "team": "Fulham", "position": "M", "minutes": 900},
        ]
        result = match_fpl_to_understat(
            "Silva", "Fulham", players, fpl_position="DEF", fpl_minutes=900
        )
        assert result is None

    def test_cross_club_fallback_needs_minutes_to_corroborate(self):
        """A lone namesake whose season is the wrong length is not the player.

        The ambiguity guard cannot help when only one candidate carries the
        name, so minutes have to do the work: both sources count the same
        league's minutes, and a settled player at another club does not share
        a season length with the one being looked up.
        """
        players = [
            {"id": 1, "name": "Robert Sanchez", "team": "Brentford", "position": "M", "minutes": 200},
            {"id": 2, "name": "Cole Palmer", "team": "Chelsea", "position": "M", "minutes": 1500},
        ]
        result = match_fpl_to_understat(
            "Sanchez", "Chelsea", players, fpl_position="MID", fpl_minutes=1500
        )
        assert result is None

    def test_cross_club_fallback_separates_namesakes_on_minutes(self):
        """Two namesakes both clearing the minutes floor are split by closeness.

        Neither is filtered outright, so the tiebreak inside the name tier is
        what decides it — and the closer season must not be declined as
        ambiguous just because the other one also survived.
        """
        players = [
            {"id": 1, "name": "Bernardo Silva", "team": "Manchester City", "position": "M", "minutes": 1500},
            {"id": 2, "name": "Fabio Silva", "team": "Everton", "position": "M", "minutes": 850},
            {"id": 3, "name": "Harry Wilson", "team": "Fulham", "position": "M", "minutes": 900},
        ]
        result = match_fpl_to_understat(
            "Silva", "Fulham", players, fpl_position="MID", fpl_minutes=900
        )
        assert result is not None
        assert result["id"] == 2

    def test_club_match_wins_over_cross_club_namesake(self):
        """The gated pass runs first, so a club-mate beats an exact namesake."""
        players = [
            {"id": 1, "name": "Joao Silva", "team": "Fulham", "position": "M", "minutes": 400},
            {"id": 2, "name": "Silva", "team": "Everton", "position": "M", "minutes": 400},
        ]
        result = match_fpl_to_understat(
            "Silva", "Fulham", players, fpl_position="MID", fpl_minutes=400
        )
        assert result is not None
        assert result["id"] == 1

    def test_cross_club_fallback_still_needs_a_name_match(self):
        """Dropping the club gate does not lower the name bar."""
        players = [
            {"id": 1, "name": "Anderson", "team": "Everton", "position": "M", "minutes": 1000},
            {"id": 2, "name": "Micky van de Ven", "team": "Tottenham", "position": "D", "minutes": 1000},
        ]
        result = match_fpl_to_understat(
            "Son", "Spurs", players, fpl_position="MID", fpl_minutes=1000
        )
        assert result is None

    def test_exact_name_outranks_a_looser_name_carrying_bonuses(self):
        """Bonuses break ties inside a name tier, they do not promote across tiers.

        Summing them let an all-words match with a position bonus (8+2) equal
        an exact match with none (10), so whichever was scanned first won.
        """
        players = [
            {"id": 1, "name": "Bruno Fernandes Silva", "team": "Fulham", "position": "M", "minutes": 900},
            {"id": 2, "name": "Bruno Fernandes", "team": "Fulham", "position": "F", "minutes": 900},
        ]
        result = match_fpl_to_understat(
            "Bruno Fernandes", "Fulham", players, fpl_position="MID"
        )
        assert result is not None
        assert result["id"] == 2

    def test_exact_name_is_not_declined_as_ambiguous_against_a_weaker_tier(self):
        """The same tie must not read as ambiguity and decline a good match."""
        players = [
            {"id": 1, "name": "Bruno Fernandes Silva", "team": "Everton", "position": "M", "minutes": 900},
            {"id": 2, "name": "Bruno Fernandes", "team": "Chelsea", "position": "F", "minutes": 900},
            {"id": 3, "name": "Harry Wilson", "team": "Fulham", "position": "M", "minutes": 900},
        ]
        result = match_fpl_to_understat(
            "Bruno Fernandes", "Fulham", players, fpl_position="MID", fpl_minutes=900
        )
        assert result is not None
        assert result["id"] == 2

    def test_malformed_row_at_another_club_does_not_break_the_lookup(self):
        """The fallback scores rows the caller never asked about (#234).

        Before the fallback existed a row could only affect lookups for its own
        club, so an undocumented payload's bad row now has a wider blast radius
        than the two CLI callers that do not guard the call.
        """
        players = [
            {"id": 1, "team": "Everton", "position": "M", "minutes": 900},  # no name
            {"id": 2, "name": None, "team": "Everton", "minutes": "unknown"},
            {"id": 3, "name": "Harry Wilson", "team": "Fulham", "position": "M", "minutes": 900},
            {"id": 4, "name": "Marc Guiu", "team": "Chelsea", "position": "F S", "minutes": 25},
        ]
        result = match_fpl_to_understat(
            "Guiu", "Fulham", players, fpl_position="FWD", fpl_minutes=25
        )
        assert result is not None
        assert result["id"] == 4

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


class TestContractTripwires:
    """Upstream drift degrades with a warning, never silently (#97)."""

    @pytest.mark.asyncio
    async def test_empty_league_data_warns(self, caplog):
        # A missing/renamed "players" key and "no matches played yet" look
        # identical from here, so both announce the degradation.
        client = UnderstatClient()
        with patch.object(client, "_get_api_json", new_callable=AsyncMock) as mock_get:
            mock_get.return_value = {"teams": {}, "dates": []}
            with caplog.at_level(logging.WARNING):
                result = await client.get_league_players()

        assert result == []
        assert "contains no players" in caplog.text

    def test_unmatched_team_warns_once(self, caplog):
        # The join-drop tripwire: a team name the map fails to resolve fails
        # every one of its players identically (#94), so it warns at team
        # level — and only once per team, not once per player lookup.
        from fpl_cli.api import understat

        understat._unmatched_team_warned.clear()
        players = [
            {"name": "Someone", "team": "Really Fake FC", "position": "F M S", "minutes": 900},
        ]
        with caplog.at_level(logging.WARNING):
            first = match_fpl_to_understat("Player One", "Faketown", players)
            second = match_fpl_to_understat("Player Two", "Faketown", players)

        assert first is None and second is None
        assert caplog.text.count("No Understat players carry team") == 1
        assert "Faketown" in caplog.text
        assert "TEAM_NAME_MAP" in caplog.text
        understat._unmatched_team_warned.clear()

    def test_matched_team_does_not_warn(self, caplog):
        from fpl_cli.api import understat

        understat._unmatched_team_warned.clear()
        players = [
            {"name": "Saka", "team": "Arsenal", "position": "M S", "minutes": 900},
        ]
        with caplog.at_level(logging.WARNING):
            match = match_fpl_to_understat("Saka", "Arsenal", players)

        assert match is not None
        assert "TEAM_NAME_MAP may need updating" not in caplog.text

    def test_comma_joined_team_does_not_warn(self, caplog):
        # A transferred player's joined title resolves, so it must not read as
        # a TEAM_NAME_MAP failure (#94).
        from fpl_cli.api import understat

        understat._unmatched_team_warned.clear()
        players = [
            {"name": "Eberechi Eze", "team": "Arsenal,Crystal Palace", "position": "M", "minutes": 1928},
        ]
        with caplog.at_level(logging.WARNING):
            match = match_fpl_to_understat("Eze", "Arsenal", players)

        assert match is not None
        assert "TEAM_NAME_MAP may need updating" not in caplog.text

    def test_unresolved_club_warns_and_gets_no_fallback(self, caplog):
        # The name-only fallback (#234) matches across every club, so it must
        # not run for a club nothing carries: the warning would still fire, but
        # it would no longer mean "this club's players have no xG data" —
        # they would each be wearing whichever stranger's name matched.
        from fpl_cli.api import understat

        understat._unmatched_team_warned.clear()
        players = [
            {"name": "Marc Guiu", "team": "Chelsea", "position": "F S", "minutes": 25},
        ]
        with caplog.at_level(logging.WARNING):
            match = match_fpl_to_understat(
                "Marc Guiu", "Faketown", players, fpl_position="FWD", fpl_minutes=25
            )

        assert match is None
        assert "TEAM_NAME_MAP may need updating" in caplog.text
        understat._unmatched_team_warned.clear()

    def test_empty_understat_list_does_not_warn_per_team(self, caplog):
        # No Understat data at all is the league-level tripwire's job; the
        # team-level warning would misattribute it to TEAM_NAME_MAP.
        from fpl_cli.api import understat

        understat._unmatched_team_warned.clear()
        with caplog.at_level(logging.WARNING):
            match = match_fpl_to_understat("Saka", "Arsenal", [])

        assert match is None
        assert "TEAM_NAME_MAP" not in caplog.text


# --- TestUnderstatClubRows ---

class TestUnderstatClubRows:
    """The club gate the matcher and `fpl doctor --providers` share (#229)."""

    def test_maps_the_fpl_name_before_gating(self):
        players = [{"name": "Someone", "team": "Coventry"}]
        assert understat_club_rows("Coventry City", players) == players

    def test_unmapped_name_passes_through(self):
        # A club absent from TEAM_NAME_MAP still joins when both sources
        # already spell it the same way.
        players = [{"name": "Someone", "team": "Arsenal"}]
        assert understat_club_rows("Arsenal", players) == players

    def test_comma_joined_title_counts_for_both_clubs(self):
        players = [{"name": "Eberechi Eze", "team": "Arsenal,Crystal Palace"}]
        assert understat_club_rows("Arsenal", players) == players
        assert understat_club_rows("Crystal Palace", players) == players

    def test_club_with_no_rows_is_empty(self):
        players = [{"name": "Someone", "team": "Arsenal"}]
        assert understat_club_rows("Coventry City", players) == []

    def test_non_string_title_is_skipped(self):
        # The payload is undocumented; one malformed row must not raise.
        assert understat_club_rows("Arsenal", [{"name": "Someone", "team": None}]) == []

    def test_agrees_with_the_matcher_on_a_club_that_misses(self, caplog):
        # The point of sharing the gate: the probe's verdict and the runtime's
        # tripwire cannot disagree about the same club and the same payload.
        players = [{"name": "Saka", "team": "Arsenal", "position": "M S", "minutes": 900}]
        with caplog.at_level(logging.WARNING):
            match = match_fpl_to_understat("Some Player", "Coventry City", players)

        assert understat_club_rows("Coventry City", players) == []
        assert match is None
        assert unmatched_understat_teams() == ["Coventry City"]


# --- TestUnderstatJoinWarnings ---

class TestUnderstatJoinWarnings:
    """The tripwire has to reach `metadata.warnings`, not only stderr (#229)."""

    def test_historical_pool_does_not_warn(self, caplog):
        # A club promoted this season carries no rows in a past season's pool
        # because it was not in that league -- not because TEAM_NAME_MAP is
        # wrong. Warning about it is the false alarm #229 reported.
        players = [{"name": "Saka", "team": "Arsenal", "position": "M S", "minutes": 900}]
        with caplog.at_level(logging.WARNING):
            match = match_fpl_to_understat(
                "Some Player", "Coventry City", players, season_label="2024-25",
            )

        assert match is None
        assert "TEAM_NAME_MAP" not in caplog.text
        assert unmatched_understat_teams() == []
        assert understat_join_warnings() == []

    def test_live_pool_still_warns_after_the_same_club_missed_a_past_one(self, caplog):
        # Deduping on the club alone would let a past season's silent miss
        # swallow the live gap that actually matters.
        players = [{"name": "Saka", "team": "Arsenal", "position": "M S", "minutes": 900}]
        with caplog.at_level(logging.WARNING):
            match_fpl_to_understat(
                "Some Player", "Coventry City", players, season_label="2024-25",
            )
            match_fpl_to_understat("Some Player", "Coventry City", players)

        assert caplog.text.count("No Understat players carry team") == 1
        assert unmatched_understat_teams() == ["Coventry City"]

    def test_warning_entry_names_the_club_and_the_mapped_title(self):
        players = [{"name": "Saka", "team": "Arsenal", "position": "M S", "minutes": 900}]
        match_fpl_to_understat("Some Player", "Coventry City", players)

        warnings = understat_join_warnings()
        assert [w["code"] for w in warnings] == [UNDERSTAT_TEAM_UNMATCHED]
        assert "Coventry City" in warnings[0]["message"]
        assert "'Coventry'" in warnings[0]["message"]

    def test_one_entry_per_unmatched_club(self):
        players = [{"name": "Saka", "team": "Arsenal", "position": "M S", "minutes": 900}]
        match_fpl_to_understat("Some Player", "Coventry City", players)
        match_fpl_to_understat("Other Player", "Hull City", players)

        assert unmatched_understat_teams() == ["Coventry City", "Hull City"]
        assert len(understat_join_warnings()) == 2

    def test_resolved_club_reports_nothing(self):
        players = [{"name": "Saka", "team": "Arsenal", "position": "M S", "minutes": 900}]
        assert match_fpl_to_understat("Saka", "Arsenal", players) is not None
        assert understat_join_warnings() == []


# --- TestSplitTeamTitles ---

class TestSplitTeamTitles:
    """Understat comma-joins every club a player appeared for (#94)."""

    def test_single_club(self):
        assert split_team_titles("Arsenal") == ["Arsenal"]

    def test_joined_clubs(self):
        assert split_team_titles("Arsenal,Crystal Palace") == ["Arsenal", "Crystal Palace"]

    def test_strips_surrounding_whitespace(self):
        assert split_team_titles("Chelsea, Fulham") == ["Chelsea", "Fulham"]

    def test_empty_title(self):
        assert split_team_titles("") == [""]
