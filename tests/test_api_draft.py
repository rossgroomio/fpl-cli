"""Tests for FPL Draft API client."""

import pytest
from unittest.mock import AsyncMock, patch, MagicMock

from fpl_cli.api.fpl_draft import FPLDraftClient

from tests.conftest import (
    make_draft_player,
    make_draft_team,
    make_draft_league_entry,
    make_draft_standing,
)


# --- Fixtures ---

@pytest.fixture
def mock_draft_bootstrap():
    """Mock draft bootstrap-static response."""
    return {
        "elements": [
            make_draft_player(id=1, web_name="Salah", team=14, element_type=3, form=7.0, total_points=120),
            make_draft_player(id=2, web_name="Haaland", team=13, element_type=4, form=8.0, total_points=150),
            make_draft_player(id=3, web_name="Saka", team=1, element_type=3, form=6.0, total_points=90),
            make_draft_player(id=4, web_name="Gabriel", team=1, element_type=2, form=5.0, total_points=70),
            make_draft_player(id=5, web_name="Unavailable", team=1, element_type=3, status="u", total_points=0),
        ],
        "teams": [
            make_draft_team(id=1, name="Arsenal", short_name="ARS"),
            make_draft_team(id=13, name="Man City", short_name="MCI"),
            make_draft_team(id=14, name="Liverpool", short_name="LIV"),
        ],
    }


@pytest.fixture
def mock_league_details():
    """Mock draft league details response."""
    return {
        "league": {"id": 12345, "name": "Test Draft League"},
        "league_entries": [
            make_draft_league_entry(id=1, entry_id=100, entry_name="Team A"),
            make_draft_league_entry(id=2, entry_id=101, entry_name="Team B"),
            make_draft_league_entry(id=3, entry_id=102, entry_name="Team C"),
        ],
        "standings": [
            make_draft_standing(league_entry=1, rank=1, total=500, event_total=60),
            make_draft_standing(league_entry=2, rank=2, total=450, event_total=55),
            make_draft_standing(league_entry=3, rank=3, total=400, event_total=50),
        ],
    }


@pytest.fixture
def mock_element_status():
    """Mock element-status response."""
    return {
        "element_status": [
            {"element": 1, "owner": 100},  # Salah owned by team 100
            {"element": 2, "owner": 101},  # Haaland owned by team 101
            {"element": 3, "owner": None},  # Saka available
            {"element": 4, "owner": None},  # Gabriel available
            {"element": 5, "owner": None},  # Unavailable player
        ]
    }


@pytest.fixture
def mock_game_data():
    """Mock game data response."""
    return {
        "current_event": 25,
        "next_event": 26,
    }


@pytest.fixture
def mock_entry_picks():
    """Mock entry picks response."""
    return {
        "picks": [
            {"element": 1, "position": 1},
            {"element": 3, "position": 2},
        ]
    }


@pytest.fixture
def mock_transactions():
    """Mock transactions response."""
    return {
        "transactions": [
            {"element_in": 3, "element_out": 10, "entry": 100, "event": 25, "kind": "w", "result": "a"},
            {"element_in": 4, "element_out": 11, "entry": 101, "event": 24, "kind": "w", "result": "a"},
            {"element_in": 12, "element_out": 5, "entry": 102, "event": 23, "kind": "w", "result": "a"},
        ]
    }


# --- TestFPLDraftClient ---

class TestFPLDraftClientInit:
    """Tests for FPLDraftClient initialization."""

    def test_default_timeout(self):
        """Test default timeout is 30 seconds."""
        client = FPLDraftClient()
        assert client.timeout == 30.0

    def test_custom_timeout(self):
        """Test custom timeout is applied."""
        client = FPLDraftClient(timeout=60.0)
        assert client.timeout == 60.0

    def test_initial_cache_state(self):
        """Test initial cache is None."""
        client = FPLDraftClient()
        assert client._bootstrap_data is None


class TestFPLDraftClientBootstrap:
    """Tests for bootstrap-static endpoint."""

    @pytest.mark.asyncio
    async def test_get_bootstrap_static(self, mock_draft_bootstrap):
        """Test fetching bootstrap-static data."""
        client = FPLDraftClient()

        with patch.object(client, "_get", new_callable=AsyncMock) as mock_get:
            mock_get.return_value = mock_draft_bootstrap

            result = await client.get_bootstrap_static()

            mock_get.assert_called_once_with("bootstrap-static")
            assert "elements" in result
            assert "teams" in result
            assert len(result["elements"]) == 5

    @pytest.mark.asyncio
    async def test_get_bootstrap_static_caching(self, mock_draft_bootstrap):
        """Test bootstrap data is cached."""
        client = FPLDraftClient()

        with patch.object(client, "_get", new_callable=AsyncMock) as mock_get:
            mock_get.return_value = mock_draft_bootstrap

            # First call
            result1 = await client.get_bootstrap_static()
            # Second call should use cache
            result2 = await client.get_bootstrap_static()

            mock_get.assert_called_once()  # Only called once
            assert result1 == result2

    @pytest.mark.asyncio
    async def test_get_bootstrap_static_force_refresh(self, mock_draft_bootstrap):
        """Test force refresh bypasses cache."""
        client = FPLDraftClient()
        client._bootstrap_data = {"old": "data"}

        with patch.object(client, "_get", new_callable=AsyncMock) as mock_get:
            mock_get.return_value = mock_draft_bootstrap

            result = await client.get_bootstrap_static(force_refresh=True)

            mock_get.assert_called_once()
            assert result == mock_draft_bootstrap


class TestFPLDraftClientLeague:
    """Tests for league-related endpoints."""

    @pytest.mark.asyncio
    async def test_get_league_details(self, mock_league_details):
        """Test fetching league details."""
        client = FPLDraftClient()

        with patch.object(client, "_get", new_callable=AsyncMock) as mock_get:
            mock_get.return_value = mock_league_details

            result = await client.get_league_details(12345)

            mock_get.assert_called_once_with("league/12345/details")
            assert result["league"]["name"] == "Test Draft League"
            assert len(result["league_entries"]) == 3

    @pytest.mark.asyncio
    async def test_get_league_ownership_status(self, mock_element_status):
        """Test fetching ownership status."""
        client = FPLDraftClient()

        with patch.object(client, "_get", new_callable=AsyncMock) as mock_get:
            mock_get.return_value = mock_element_status

            result = await client.get_league_ownership_status(12345)

            mock_get.assert_called_once_with("league/12345/element-status")
            assert len(result["element_status"]) == 5

    @pytest.mark.asyncio
    async def test_get_league_transactions(self, mock_transactions):
        """Test fetching league transactions."""
        client = FPLDraftClient()

        with patch.object(client, "_get", new_callable=AsyncMock) as mock_get:
            mock_get.return_value = mock_transactions

            result = await client.get_league_transactions(12345)

            mock_get.assert_called_once_with("draft/league/12345/transactions")
            assert len(result["transactions"]) == 3


class TestFPLDraftClientEntry:
    """Tests for entry-related endpoints."""

    @pytest.mark.asyncio
    async def test_get_entry_profile(self):
        """Test fetching entry profile."""
        client = FPLDraftClient()
        mock_response = {"entry": {"id": 100, "name": "Test Team"}}

        with patch.object(client, "_get", new_callable=AsyncMock) as mock_get:
            mock_get.return_value = mock_response

            result = await client.get_entry_profile(100)

            mock_get.assert_called_once_with("entry/100/public")
            assert result["entry"]["id"] == 100

    @pytest.mark.asyncio
    async def test_get_entry_picks(self, mock_entry_picks):
        """Test fetching entry picks for gameweek."""
        client = FPLDraftClient()

        with patch.object(client, "_get", new_callable=AsyncMock) as mock_get:
            mock_get.return_value = mock_entry_picks

            result = await client.get_entry_picks(100, 25)

            mock_get.assert_called_once_with("entry/100/event/25")
            assert len(result["picks"]) == 2


class TestFPLDraftClientGameData:
    """Tests for game data endpoint."""

    @pytest.mark.asyncio
    async def test_get_game_state(self, mock_game_data):
        """Test fetching game state."""
        client = FPLDraftClient()

        with patch.object(client, "_get", new_callable=AsyncMock) as mock_get:
            mock_get.return_value = mock_game_data

            result = await client.get_game_state()

            mock_get.assert_called_once_with("game")
            assert result["current_event"] == 25


class TestFPLDraftClientSquad:
    """Tests for get_squad."""

    @pytest.mark.asyncio
    async def test_get_squad(self, mock_draft_bootstrap, mock_game_data, mock_entry_picks):
        """Test fetching enriched team squad."""
        client = FPLDraftClient()

        with patch.object(client, "_get", new_callable=AsyncMock) as mock_get:
            async def side_effect(endpoint):
                if endpoint == "bootstrap-static":
                    return mock_draft_bootstrap
                elif endpoint == "game":
                    return mock_game_data
                elif endpoint == "entry/100/event/25":
                    return mock_entry_picks
                return {}

            mock_get.side_effect = side_effect

            result = await client.get_squad(100)

            assert len(result) == 2
            assert result[0]["id"] == 1  # Salah
            assert result[1]["id"] == 3  # Saka


class TestFPLDraftClientOwnership:
    """Tests for ownership-related methods."""

    @pytest.mark.asyncio
    async def test_get_league_ownership(
        self, mock_draft_bootstrap, mock_league_details, mock_game_data, mock_entry_picks
    ):
        """Test building ownership from actual squads."""
        client = FPLDraftClient()

        with patch.object(client, "_get", new_callable=AsyncMock) as mock_get:
            async def side_effect(endpoint):
                if endpoint == "bootstrap-static":
                    return mock_draft_bootstrap
                elif endpoint == "league/12345/details":
                    return mock_league_details
                elif endpoint == "game":
                    return mock_game_data
                elif endpoint.startswith("entry/") and "/event/" in endpoint:
                    return mock_entry_picks
                return {}

            mock_get.side_effect = side_effect

            result = await client.get_league_ownership(12345, mock_draft_bootstrap)

            # Each entry has Salah (1) and Saka (3), so they should be owned
            assert 1 in result or 3 in result

    @pytest.mark.asyncio
    async def test_get_available_players(
        self, mock_draft_bootstrap, mock_league_details, mock_game_data, mock_entry_picks
    ):
        """Test getting available players."""
        client = FPLDraftClient()

        with patch.object(client, "_get", new_callable=AsyncMock) as mock_get:
            async def side_effect(endpoint):
                if endpoint == "bootstrap-static":
                    return mock_draft_bootstrap
                elif endpoint == "league/12345/details":
                    return mock_league_details
                elif endpoint == "game":
                    return mock_game_data
                elif endpoint.startswith("entry/") and "/event/" in endpoint:
                    return mock_entry_picks
                return {}

            mock_get.side_effect = side_effect

            result = await client.get_available_players(12345, mock_draft_bootstrap)

            # Players not owned and not unavailable with 0 points should be available
            available_ids = [p["id"] for p in result]
            # Unavailable player (id=5) with status='u' and 0 points should be excluded
            assert 5 not in available_ids

    @pytest.mark.asyncio
    async def test_get_available_players_excludes_unavailable_zero_points(self, mock_draft_bootstrap):
        """Test that unavailable players with 0 points are excluded."""
        client = FPLDraftClient()

        # Mock empty ownership
        with patch.object(client, "get_league_ownership", new_callable=AsyncMock) as mock_ownership:
            mock_ownership.return_value = {}

            result = await client.get_available_players(12345, mock_draft_bootstrap)

            # Player 5 has status='u' and total_points=0, should be excluded
            available_ids = [p["id"] for p in result]
            assert 5 not in available_ids
            # Other players should be available
            assert 1 in available_ids
            assert 2 in available_ids


class TestFPLDraftClientWaiverOrder:
    """Tests for waiver order."""

    @pytest.mark.asyncio
    async def test_get_waiver_order(self, mock_league_details):
        """Test getting waiver order (reverse of standings)."""
        client = FPLDraftClient()

        with patch.object(client, "_get", new_callable=AsyncMock) as mock_get:
            mock_get.return_value = mock_league_details

            result = await client.get_waiver_order(12345)

            # Should be sorted by rank descending (worst first)
            assert result[0]["rank"] == 3
            assert result[-1]["rank"] == 1


class TestFPLDraftClientReleases:
    """Tests for recent releases."""

    @pytest.mark.asyncio
    async def test_get_recent_releases(
        self, mock_draft_bootstrap, mock_game_data, mock_element_status, mock_transactions
    ):
        """Test getting recently released players."""
        client = FPLDraftClient()

        with patch.object(client, "_get", new_callable=AsyncMock) as mock_get:
            async def side_effect(endpoint):
                if endpoint == "bootstrap-static":
                    return mock_draft_bootstrap
                elif endpoint == "game":
                    return mock_game_data
                elif endpoint == "league/12345/element-status":
                    return mock_element_status
                elif endpoint == "draft/league/12345/transactions":
                    return mock_transactions
                return {}

            mock_get.side_effect = side_effect

            result = await client.get_recent_releases(12345, mock_draft_bootstrap)

            # Transactions that dropped players should create releases
            # element_out values in transactions: 10, 11, 5
            # Player 5 exists in bootstrap (Unavailable), but 10 and 11 don't
            assert isinstance(result, list)

    @pytest.mark.asyncio
    async def test_get_recent_releases_filters_by_gameweek(
        self, mock_draft_bootstrap, mock_game_data, mock_element_status
    ):
        """Test that old releases are filtered out."""
        client = FPLDraftClient()

        # Current GW is 25, max_gameweeks_back=4 means only GW 22+ included
        mock_old_transactions = {
            "transactions": [
                {"element_in": 3, "element_out": 10, "entry": 100, "event": 20, "kind": "w"},  # Too old
            ]
        }

        with patch.object(client, "_get", new_callable=AsyncMock) as mock_get:
            async def side_effect(endpoint):
                if endpoint == "bootstrap-static":
                    return mock_draft_bootstrap
                elif endpoint == "game":
                    return mock_game_data
                elif endpoint == "league/12345/element-status":
                    return mock_element_status
                elif endpoint == "draft/league/12345/transactions":
                    return mock_old_transactions
                return {}

            mock_get.side_effect = side_effect

            result = await client.get_recent_releases(12345, mock_draft_bootstrap, max_gameweeks_back=4)

            # GW 20 is outside the window (25 - 4 = 21 minimum)
            assert len(result) == 0


class TestFPLDraftClientParsePlayer:
    """Tests for parse_player method."""

    def test_parse_player(self):
        """Test parsing raw player data."""
        client = FPLDraftClient()
        raw_data = make_draft_player(
            id=1,
            web_name="Salah",
            first_name="Mohamed",
            second_name="Salah",
            team=14,
            element_type=3,
            form=7.5,
            points_per_game=6.2,
        )

        result = client.parse_player(raw_data)

        assert result["id"] == 1
        assert result["player_name"] == "Salah"
        assert result["team_id"] == 14
        assert result["position"] == "MID"
        assert result["form"] == 7.5
        assert result["ppg"] == 6.2

    def test_parse_player_handles_missing_fields(self):
        """Test parse_player with minimal data."""
        client = FPLDraftClient()
        raw_data = {"id": 1}

        result = client.parse_player(raw_data)

        assert result["id"] == 1
        assert result["player_name"] == ""
        assert result["total_points"] == 0
        assert result["form"] == 0.0

    def test_parse_player_converts_string_fields(self):
        """Test that string fields are converted to appropriate types."""
        client = FPLDraftClient()
        raw_data = {
            "id": 1,
            "form": "7.5",
            "points_per_game": "6.2",
            "expected_goals": "10.5",
            "expected_assists": "5.3",
        }

        result = client.parse_player(raw_data)

        assert result["form"] == 7.5
        assert result["ppg"] == 6.2
        assert result["expected_goals"] == 10.5
        assert result["expected_assists"] == 5.3
