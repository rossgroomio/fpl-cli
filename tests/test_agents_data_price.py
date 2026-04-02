"""Tests for PriceAgent."""

import pytest
from unittest.mock import AsyncMock, patch

from fpl_cli.agents.data.price import PriceAgent
from fpl_cli.agents.base import AgentStatus
from fpl_cli.models.player import PlayerPosition

from tests.conftest import make_player, make_team


# --- Fixtures ---

@pytest.fixture
def mock_players():
    """Create mock players with price change data."""
    return [
        # Risers
        make_player(
            id=1, web_name="Riser1", team_id=1, position=PlayerPosition.MIDFIELDER,
            now_cost=105, cost_change_event=5, cost_change_start=10,
            transfers_in_event=100000, transfers_out_event=5000,
            form=7.0, selected_by_percent=25.0,
        ),
        make_player(
            id=2, web_name="Riser2", team_id=2, position=PlayerPosition.FORWARD,
            now_cost=120, cost_change_event=2, cost_change_start=20,
            transfers_in_event=80000, transfers_out_event=10000,
            form=8.0, selected_by_percent=35.0,
        ),
        # Fallers
        make_player(
            id=3, web_name="Faller1", team_id=1, position=PlayerPosition.DEFENDER,
            now_cost=55, cost_change_event=-3, cost_change_start=-10,
            transfers_in_event=5000, transfers_out_event=120000,
            form=2.0, selected_by_percent=15.0,
        ),
        make_player(
            id=4, web_name="Faller2", team_id=3, position=PlayerPosition.MIDFIELDER,
            now_cost=70, cost_change_event=-1, cost_change_start=-5,
            transfers_in_event=10000, transfers_out_event=80000,
            form=3.0, selected_by_percent=10.0,
        ),
        # No change
        make_player(
            id=5, web_name="Stable1", team_id=2, position=PlayerPosition.GOALKEEPER,
            now_cost=50, cost_change_event=0, cost_change_start=0,
            transfers_in_event=20000, transfers_out_event=20000,
            form=5.0, selected_by_percent=20.0,
        ),
    ]


@pytest.fixture
def mock_teams():
    """Create mock teams."""
    return [
        make_team(id=1, name="Arsenal", short_name="ARS"),
        make_team(id=2, name="Man City", short_name="MCI"),
        make_team(id=3, name="Liverpool", short_name="LIV"),
    ]


# --- TestPriceAgentInit ---

class TestPriceAgentInit:
    """Tests for PriceAgent initialization."""

    def test_agent_initialization(self):
        """Test default initialization."""
        agent = PriceAgent()
        assert agent.name == "PriceAgent"
        assert agent.transfer_threshold == 5.0

    def test_agent_custom_threshold(self):
        """Test initialization with custom threshold."""
        agent = PriceAgent(config={"transfer_threshold": 10.0})
        assert agent.transfer_threshold == 10.0


# --- TestPriceAgentRun ---

class TestPriceAgentRun:
    """Tests for PriceAgent run method."""

    @pytest.mark.asyncio
    async def test_run_success(self, mock_players, mock_teams):
        """Test successful price analysis."""
        agent = PriceAgent()

        with patch.object(agent.client, "get_players", new_callable=AsyncMock) as mock_get_players, \
             patch.object(agent.client, "get_teams", new_callable=AsyncMock) as mock_get_teams:

            mock_get_players.return_value = mock_players
            mock_get_teams.return_value = mock_teams

            result = await agent.run()

            assert result.status == AgentStatus.SUCCESS
            assert "risers_this_gw" in result.data
            assert "fallers_this_gw" in result.data
            assert "hot_transfers_in" in result.data
            assert "hot_transfers_out" in result.data
            assert "season_value_gains" in result.data
            assert "season_value_losses" in result.data
            assert "summary" in result.data

    @pytest.mark.asyncio
    async def test_run_handles_api_error(self):
        """Test run handles API errors gracefully."""
        agent = PriceAgent()

        with patch.object(agent.client, "get_players", new_callable=AsyncMock) as mock_get_players:
            mock_get_players.side_effect = Exception("API Error")

            result = await agent.run()

            assert result.status == AgentStatus.FAILED
            assert "API Error" in result.errors[0]

    @pytest.mark.asyncio
    async def test_run_summary_populated(self, mock_players, mock_teams):
        """Test summary is correctly populated."""
        agent = PriceAgent()

        with patch.object(agent.client, "get_players", new_callable=AsyncMock) as mock_get_players, \
             patch.object(agent.client, "get_teams", new_callable=AsyncMock) as mock_get_teams:

            mock_get_players.return_value = mock_players
            mock_get_teams.return_value = mock_teams

            result = await agent.run()

            summary = result.data["summary"]
            assert summary["total_risers"] == 2
            assert summary["total_fallers"] == 2
            assert summary["most_transferred_in"] is not None
            assert summary["most_transferred_out"] is not None


# --- TestPriceAgentFinders ---

class TestPriceAgentFinders:
    """Tests for finder methods."""

    def test_find_risers(self, mock_players, mock_teams):
        """Test _find_risers returns players with positive price change."""
        agent = PriceAgent()
        team_map = {t.id: t for t in mock_teams}

        risers = agent._find_risers(mock_players, team_map)

        assert len(risers) == 2
        # Should be sorted by change_this_gw descending
        assert risers[0]["name"] == "Riser1"  # +0.5m
        assert risers[1]["name"] == "Riser2"  # +0.2m

    def test_find_risers_sorted_by_change(self, mock_players, mock_teams):
        """Test risers are sorted by price change descending."""
        agent = PriceAgent()
        team_map = {t.id: t for t in mock_teams}

        risers = agent._find_risers(mock_players, team_map)

        # First riser has +5 (0.5m), second has +2 (0.2m)
        assert risers[0]["change_this_gw"] > risers[1]["change_this_gw"]

    def test_find_risers_empty_when_none(self, mock_teams):
        """Test _find_risers returns empty list when no risers."""
        agent = PriceAgent()
        team_map = {t.id: t for t in mock_teams}
        players = [
            make_player(id=1, team_id=1, cost_change_event=0),
            make_player(id=2, team_id=2, cost_change_event=-1),
        ]

        risers = agent._find_risers(players, team_map)

        assert len(risers) == 0

    def test_find_fallers(self, mock_players, mock_teams):
        """Test _find_fallers returns players with negative price change."""
        agent = PriceAgent()
        team_map = {t.id: t for t in mock_teams}

        fallers = agent._find_fallers(mock_players, team_map)

        assert len(fallers) == 2
        # Should be sorted by change_this_gw ascending (most negative first)
        assert fallers[0]["name"] == "Faller1"  # -0.3m
        assert fallers[1]["name"] == "Faller2"  # -0.1m

    def test_find_fallers_sorted_by_change(self, mock_players, mock_teams):
        """Test fallers are sorted by price change ascending."""
        agent = PriceAgent()
        team_map = {t.id: t for t in mock_teams}

        fallers = agent._find_fallers(mock_players, team_map)

        # First faller has -3 (-0.3m), second has -1 (-0.1m)
        assert fallers[0]["change_this_gw"] < fallers[1]["change_this_gw"]

    def test_find_hot_transfers_in(self, mock_players, mock_teams):
        """Test _find_hot_transfers_in returns top transferred in players."""
        agent = PriceAgent()
        team_map = {t.id: t for t in mock_teams}

        hot_in = agent._find_hot_transfers_in(mock_players, team_map)

        assert len(hot_in) == 5  # All players, limited by default
        # Should be sorted by transfers_in_event descending
        assert hot_in[0]["transfers_in"] == 100000
        assert "net_transfers" in hot_in[0]

    def test_find_hot_transfers_in_limit(self, mock_players, mock_teams):
        """Test _find_hot_transfers_in respects limit."""
        agent = PriceAgent()
        team_map = {t.id: t for t in mock_teams}

        hot_in = agent._find_hot_transfers_in(mock_players, team_map, limit=2)

        assert len(hot_in) == 2

    def test_find_hot_transfers_out(self, mock_players, mock_teams):
        """Test _find_hot_transfers_out returns top transferred out players."""
        agent = PriceAgent()
        team_map = {t.id: t for t in mock_teams}

        hot_out = agent._find_hot_transfers_out(mock_players, team_map)

        assert len(hot_out) == 5
        # Should be sorted by transfers_out_event descending
        assert hot_out[0]["transfers_out"] == 120000
        assert "net_transfers" in hot_out[0]

    def test_find_season_value_gains(self, mock_players, mock_teams):
        """Test _find_season_value_gains returns players with positive season change."""
        agent = PriceAgent()
        team_map = {t.id: t for t in mock_teams}

        gains = agent._find_season_value_gains(mock_players, team_map)

        assert len(gains) == 2
        # Should only include players with cost_change_start > 0
        names = [g["name"] for g in gains]
        assert "Riser1" in names
        assert "Riser2" in names
        # Should be sorted by season change descending
        assert gains[0]["change_this_season"] > gains[1]["change_this_season"]

    def test_find_season_value_gains_excludes_negative(self, mock_teams):
        """Test season gains excludes players with negative season change."""
        agent = PriceAgent()
        team_map = {t.id: t for t in mock_teams}
        players = [
            make_player(id=1, team_id=1, cost_change_start=10),
            make_player(id=2, team_id=2, cost_change_start=-5),
            make_player(id=3, team_id=1, cost_change_start=0),
        ]

        gains = agent._find_season_value_gains(players, team_map)

        assert len(gains) == 1
        assert gains[0]["change_this_season"] == 1.0  # 10 / 10

    def test_find_season_value_losses(self, mock_players, mock_teams):
        """Test _find_season_value_losses returns players with negative season change."""
        agent = PriceAgent()
        team_map = {t.id: t for t in mock_teams}

        losses = agent._find_season_value_losses(mock_players, team_map)

        assert len(losses) == 2
        # Should only include players with cost_change_start < 0
        names = [l["name"] for l in losses]
        assert "Faller1" in names
        assert "Faller2" in names
        # Should be sorted by season change ascending (most negative first)
        assert losses[0]["change_this_season"] < losses[1]["change_this_season"]

    def test_find_season_value_losses_excludes_positive(self, mock_teams):
        """Test season losses excludes players with positive season change."""
        agent = PriceAgent()
        team_map = {t.id: t for t in mock_teams}
        players = [
            make_player(id=1, team_id=1, cost_change_start=-10),
            make_player(id=2, team_id=2, cost_change_start=5),
            make_player(id=3, team_id=1, cost_change_start=0),
        ]

        losses = agent._find_season_value_losses(players, team_map)

        assert len(losses) == 1
        assert losses[0]["change_this_season"] == -1.0  # -10 / 10


# --- TestPriceAgentPlayerData ---

class TestPriceAgentPlayerData:
    """Tests for _player_price_data method."""

    def test_player_price_data_structure(self, mock_players, mock_teams):
        """Test _player_price_data returns correct structure."""
        agent = PriceAgent()
        team_map = {t.id: t for t in mock_teams}
        player = mock_players[0]

        data = agent._player_price_data(player, team_map)

        assert "id" in data
        assert "name" in data
        assert "team" in data
        assert "position" in data
        assert "current_price" in data
        assert "change_this_gw" in data
        assert "change_this_season" in data
        assert "ownership" in data
        assert "form" in data
        assert "total_points" in data

    def test_player_price_data_converts_to_millions(self, mock_players, mock_teams):
        """Test price changes are converted to millions."""
        agent = PriceAgent()
        team_map = {t.id: t for t in mock_teams}
        player = mock_players[0]  # cost_change_event=5, cost_change_start=10

        data = agent._player_price_data(player, team_map)

        assert data["change_this_gw"] == 0.5  # 5 / 10
        assert data["change_this_season"] == 1.0  # 10 / 10

    def test_player_price_data_handles_missing_team(self, mock_players):
        """Test _player_price_data handles missing team gracefully."""
        agent = PriceAgent()
        team_map = {}  # Empty team map
        player = mock_players[0]

        data = agent._player_price_data(player, team_map)

        assert data["team"] == "???"
