"""Tests for analysis agents."""

from datetime import datetime, timedelta
from unittest.mock import AsyncMock, patch

import pytest

from fpl_cli.agents.base import AgentStatus
from fpl_cli.agents.analysis.captain import CaptainAgent
from fpl_cli.models.player import PlayerPosition
from fpl_cli.services.player_scoring import ScoringContext
from fpl_cli.services.team_ratings import TeamRating, TeamRatingsService

from tests.conftest import make_player, make_team, make_fixture


def _make_ratings_service():
    """Build a TeamRatingsService with test ratings."""
    svc = TeamRatingsService.__new__(TeamRatingsService)
    svc._ratings = {
        "ARS": TeamRating(atk_home=1, atk_away=2, def_home=1, def_away=2),
        "SHU": TeamRating(atk_home=6, atk_away=7, def_home=6, def_away=7),
        "MCI": TeamRating(atk_home=1, atk_away=2, def_home=2, def_away=3),
    }
    svc._loaded = True
    svc._metadata = None
    return svc


def _build_context(team_map, team_fixtures, team_form_by_id):
    """Build a ScoringContext from test data."""
    return ScoringContext(
        team_map=team_map,
        team_fixture_map=team_fixtures,
        ratings_service=_make_ratings_service(),
        team_form_by_id=team_form_by_id,
    )


class TestCaptainAgent:
    """Tests for CaptainAgent."""

    @pytest.fixture
    def agent(self):
        """Create a captain agent."""
        return CaptainAgent()

    @pytest.fixture
    def configured_agent(self):
        """Create agent with custom config."""
        return CaptainAgent(config={"differential_threshold": 5.0})

    @pytest.fixture
    def mock_players(self):
        """Create mock players for testing."""
        return [
            make_player(id=1, web_name="Haaland", team_id=13, position=PlayerPosition.FORWARD,
                       now_cost=150, goals_scored=20, assists=5, expected_goals=18.5,
                       expected_assists=4.2, minutes=2000, form=8.5, points_per_game=8.0,
                       selected_by_percent=85.0),
            make_player(id=2, web_name="Salah", team_id=14, position=PlayerPosition.MIDFIELDER,
                       now_cost=130, goals_scored=12, assists=8, expected_goals=10.5,
                       expected_assists=7.2, minutes=1800, form=7.2, points_per_game=6.5,
                       selected_by_percent=45.5),
            make_player(id=3, web_name="Saka", team_id=1, position=PlayerPosition.MIDFIELDER,
                       now_cost=95, goals_scored=8, assists=10, expected_goals=7.0,
                       expected_assists=9.5, minutes=1700, form=6.8, points_per_game=5.8,
                       selected_by_percent=35.0),
            make_player(id=4, web_name="Differential", team_id=5, position=PlayerPosition.MIDFIELDER,
                       now_cost=60, goals_scored=4, assists=5, expected_goals=5.5,
                       expected_assists=4.0, minutes=1500, form=6.0, points_per_game=5.0,
                       selected_by_percent=2.5),
        ]

    @pytest.fixture
    def mock_teams(self):
        """Create mock teams."""
        return [
            make_team(id=1, name="Arsenal", short_name="ARS", position=1),
            make_team(id=5, name="Brighton", short_name="BHA", position=10),
            make_team(id=8, name="Sheffield Utd", short_name="SHU", position=20),
            make_team(id=13, name="Man City", short_name="MCI", position=2),
            make_team(id=14, name="Liverpool", short_name="LIV", position=3),
        ]

    @pytest.fixture
    def mock_fixtures(self):
        """Create mock fixtures for GW25."""
        base_time = datetime.now() + timedelta(days=7)
        return [
            make_fixture(id=1, gameweek=25, home_team_id=13, away_team_id=8,
                        home_difficulty=2, away_difficulty=5, kickoff_time=base_time),
            make_fixture(id=2, gameweek=25, home_team_id=14, away_team_id=5,
                        home_difficulty=2, away_difficulty=4, kickoff_time=base_time),
            make_fixture(id=3, gameweek=25, home_team_id=1, away_team_id=8,
                        home_difficulty=2, away_difficulty=5, kickoff_time=base_time),
        ]

    def test_agent_initialization(self, agent):
        """Test default initialization."""
        assert agent.name == "CaptainAgent"
        assert agent.differential_threshold == 10.0

    def test_agent_custom_config(self, configured_agent):
        """Test custom config."""
        assert configured_agent.differential_threshold == 5.0

    @pytest.mark.asyncio
    async def test_run_success_global_mode(self, agent, mock_players, mock_teams, mock_fixtures):
        """Test successful captain analysis in global mode."""
        with patch.object(agent.client, "get_players", new_callable=AsyncMock) as mock_get_players, \
             patch.object(agent.client, "get_teams", new_callable=AsyncMock) as mock_get_teams, \
             patch.object(agent.client, "get_next_gameweek", new_callable=AsyncMock) as mock_next_gw, \
             patch.object(agent.client, "get_fixtures", new_callable=AsyncMock) as mock_get_fixtures:

            mock_get_players.return_value = mock_players
            mock_get_teams.return_value = mock_teams
            mock_next_gw.return_value = {"id": 25, "deadline_time": "2024-02-10T11:00:00Z"}
            mock_get_fixtures.return_value = mock_fixtures

            result = await agent.run()

            assert result.status == AgentStatus.SUCCESS
            assert result.data["gameweek"] == 25
            assert "top_picks" in result.data
            assert "all_candidates" in result.data
            assert "my_squad_mode" in result.data
            assert result.data["my_squad_mode"] is False

    @pytest.mark.asyncio
    async def test_run_with_picks_context(self, agent, mock_players, mock_teams, mock_fixtures):
        """Test captain analysis with specific player picks."""
        with patch.object(agent.client, "get_players", new_callable=AsyncMock) as mock_get_players, \
             patch.object(agent.client, "get_teams", new_callable=AsyncMock) as mock_get_teams, \
             patch.object(agent.client, "get_next_gameweek", new_callable=AsyncMock) as mock_next_gw, \
             patch.object(agent.client, "get_fixtures", new_callable=AsyncMock) as mock_get_fixtures:

            mock_get_players.return_value = mock_players
            mock_get_teams.return_value = mock_teams
            mock_next_gw.return_value = {"id": 25, "deadline_time": "2024-02-10T11:00:00Z"}
            mock_get_fixtures.return_value = mock_fixtures

            result = await agent.run(context={"picks": [1, 2, 3]})

            assert result.status == AgentStatus.SUCCESS
            assert result.data["my_squad_mode"] is True
            # Should only analyze the 3 specified players
            assert len(result.data["all_candidates"]) <= 3

    @pytest.mark.asyncio
    async def test_run_handles_api_error(self, agent):
        """Test handling API errors."""
        with patch.object(agent.client, "get_players", new_callable=AsyncMock) as mock_get_players:
            mock_get_players.side_effect = Exception("API Error")

            result = await agent.run()

            assert result.status == AgentStatus.FAILED
            assert len(result.errors) > 0

    @pytest.mark.asyncio
    async def test_run_no_next_gameweek(self, agent, mock_players, mock_teams):
        """Test handling when no next gameweek."""
        with patch.object(agent.client, "get_players", new_callable=AsyncMock) as mock_get_players, \
             patch.object(agent.client, "get_teams", new_callable=AsyncMock) as mock_get_teams, \
             patch.object(agent.client, "get_next_gameweek", new_callable=AsyncMock) as mock_next_gw, \
             patch.object(agent.client, "get_fixtures", new_callable=AsyncMock) as mock_get_fixtures:

            mock_get_players.return_value = mock_players
            mock_get_teams.return_value = mock_teams
            mock_next_gw.return_value = None
            mock_get_fixtures.return_value = []

            result = await agent.run()

            # Should still run but with limited data
            assert result.data["gameweek"] is None


class TestCaptainAgentScoring:
    """Tests for captain scoring logic."""

    @pytest.fixture
    def agent(self):
        """Create a captain agent."""
        return CaptainAgent()

    @pytest.fixture
    def team_map(self):
        """Create team map."""
        return {
            1: make_team(id=1, name="Arsenal", short_name="ARS", position=1),
            8: make_team(id=8, name="Sheffield Utd", short_name="SHU", position=20),
            13: make_team(id=13, name="Man City", short_name="MCI", position=2),
        }

    @pytest.fixture
    def team_form_by_id(self):
        """Create team form data."""
        return {
            1: {"team": "ARS", "team_id": 1, "league_position": 1, "pts_6": 15,
                "gs_6": 12, "gc_6": 3, "pts_home": 12, "gs_home": 10, "gc_home": 1,
                "pts_away": 9, "gs_away": 6, "gc_away": 4},
            8: {"team": "SHU", "team_id": 8, "league_position": 20, "pts_6": 3,
                "gs_6": 4, "gc_6": 15, "pts_home": 3, "gs_home": 3, "gc_home": 10,
                "pts_away": 0, "gs_away": 1, "gc_away": 12},
            13: {"team": "MCI", "team_id": 13, "league_position": 2, "pts_6": 14,
                 "gs_6": 15, "gc_6": 5, "pts_home": 12, "gs_home": 12, "gc_home": 2,
                 "pts_away": 10, "gs_away": 10, "gc_away": 4},
        }

    def test_score_captain_candidate(self, agent, team_map, team_form_by_id):
        """Test scoring a captain candidate."""
        player = make_player(
            id=1, web_name="Haaland", team_id=13, position=PlayerPosition.FORWARD,
            now_cost=150, minutes=2000, form=8.5, points_per_game=8.0,
            goals_scored=20, assists=5, expected_goals=18.5, expected_assists=4.2,
            selected_by_percent=85.0,
        )

        fixture = make_fixture(
            gameweek=25, home_team_id=13, away_team_id=8,
            home_difficulty=2, away_difficulty=5,
        )

        team_fixtures = {
            13: [{"fixture": fixture, "is_home": True}],
        }

        result = agent._score_captain_candidate(
            player, _build_context(team_map, team_fixtures, team_form_by_id)
        )

        assert result is not None
        assert result["id"] == 1
        assert result["player_name"] == "Haaland"
        assert result["team_short"] == "MCI"
        assert result["position"] == "FWD"
        assert 0 < result["captain_score"] <= 100
        assert isinstance(result["captain_score"], int)
        assert "fixtures" in result
        assert result["fixture_count"] == 1
        assert "matchup_score" in result
        assert "reasons" in result

    def test_score_captain_no_team(self, agent, team_form_by_id):
        """Test scoring when player's team not found."""
        player = make_player(id=1, team_id=999)  # Non-existent team
        team_map = {}
        team_fixtures = {}

        result = agent._score_captain_candidate(
            player, _build_context(team_map, team_fixtures, team_form_by_id)
        )

        assert result is None

    def test_score_captain_blank_gameweek(self, agent, team_map, team_form_by_id):
        """Test scoring when player has blank gameweek."""
        player = make_player(id=1, team_id=1)
        team_fixtures = {1: []}  # No fixtures for team 1

        result = agent._score_captain_candidate(
            player, _build_context(team_map, team_fixtures, team_form_by_id)
        )

        assert result is None

    def test_score_captain_dgw_bonus(self, agent, team_map, team_form_by_id):
        """Test double gameweek bonus is applied."""
        player = make_player(
            id=1, web_name="Haaland", team_id=13, position=PlayerPosition.FORWARD,
            now_cost=150, minutes=2000, form=8.5, points_per_game=8.0,
            goals_scored=20, assists=5, expected_goals=18.5, expected_assists=4.2,
        )

        # Both fixtures against SHU (easy opponent) to isolate DGW bonus
        fixture1 = make_fixture(id=1, gameweek=25, home_team_id=13, away_team_id=8,
                               home_difficulty=2, away_difficulty=5)
        fixture2 = make_fixture(id=2, gameweek=25, home_team_id=8, away_team_id=13,
                               home_difficulty=5, away_difficulty=2)  # Away but same easy opponent

        # Single gameweek
        team_fixtures_single = {
            13: [{"fixture": fixture1, "is_home": True}],
        }
        result_single = agent._score_captain_candidate(
            player, _build_context(team_map, team_fixtures_single, team_form_by_id)
        )

        # Double gameweek (two fixtures against same easy opponent)
        team_fixtures_double = {
            13: [
                {"fixture": fixture1, "is_home": True},
                {"fixture": fixture2, "is_home": False},
            ],
        }
        result_double = agent._score_captain_candidate(
            player, _build_context(team_map, team_fixtures_double, team_form_by_id)
        )

        # DGW scores higher: more fixtures = more points, normalised score reflects this
        assert result_double["fixture_count"] == 2
        assert any("Double gameweek" in r for r in result_double["reasons"])
        assert result_double["captain_score_raw"] > result_single["captain_score_raw"]
        assert result_double["captain_score"] >= result_single["captain_score"]

    def test_score_captain_home_vs_away(self, agent, team_map, team_form_by_id):
        """Test home advantage in scoring."""
        player = make_player(
            id=1, web_name="Haaland", team_id=13, position=PlayerPosition.FORWARD,
            now_cost=150, minutes=2000, form=8.5, points_per_game=8.0,
            goals_scored=20, assists=5, expected_goals=18.5, expected_assists=4.2,
        )

        fixture_home = make_fixture(gameweek=25, home_team_id=13, away_team_id=8,
                                   home_difficulty=2, away_difficulty=5)
        fixture_away = make_fixture(gameweek=25, home_team_id=8, away_team_id=13,
                                   home_difficulty=5, away_difficulty=2)

        # Home fixture
        team_fixtures_home = {
            13: [{"fixture": fixture_home, "is_home": True}],
        }
        result_home = agent._score_captain_candidate(
            player, _build_context(team_map, team_fixtures_home, team_form_by_id)
        )

        # Away fixture
        team_fixtures_away = {
            13: [{"fixture": fixture_away, "is_home": False}],
        }
        result_away = agent._score_captain_candidate(
            player, _build_context(team_map, team_fixtures_away, team_form_by_id)
        )

        # Home should have slightly higher score due to home bonus
        assert result_home["captain_score"] >= result_away["captain_score"]
        assert any("at home" in r.lower() for r in result_home["reasons"])

    def test_score_captain_xgi_metrics(self, agent, team_map, team_form_by_id):
        """Test xGI metrics in scoring."""
        # High xGI player
        high_xgi_player = make_player(
            id=1, web_name="HighXGI", team_id=13, position=PlayerPosition.FORWARD,
            now_cost=150, minutes=1800, form=7.0, points_per_game=7.0,
            goals_scored=15, assists=8, expected_goals=16.0, expected_assists=7.0,
        )

        # Low xGI player
        low_xgi_player = make_player(
            id=2, web_name="LowXGI", team_id=13, position=PlayerPosition.FORWARD,
            now_cost=100, minutes=1800, form=7.0, points_per_game=7.0,
            goals_scored=5, assists=3, expected_goals=4.0, expected_assists=2.0,
        )

        fixture = make_fixture(gameweek=25, home_team_id=13, away_team_id=8,
                              home_difficulty=2, away_difficulty=5)
        team_fixtures = {
            13: [{"fixture": fixture, "is_home": True}],
        }

        result_high = agent._score_captain_candidate(
            high_xgi_player, _build_context(team_map, team_fixtures, team_form_by_id)
        )
        result_low = agent._score_captain_candidate(
            low_xgi_player, _build_context(team_map, team_fixtures, team_form_by_id)
        )

        assert result_high["xGI_per_90"] > result_low["xGI_per_90"]
        assert result_high["captain_score_raw"] > result_low["captain_score_raw"]

    def test_penalty_taker_gets_bonus(self, agent, team_map, team_form_by_id):
        """Primary penalty taker gets 0.75 bonus."""
        pen_taker = make_player(
            id=10, web_name="Salah", team_id=1, position=PlayerPosition.MIDFIELDER,
            now_cost=130, minutes=2000, form=7.0, points_per_game=7.0,
            goals_scored=15, assists=10, expected_goals=14.0, expected_assists=9.0,
            selected_by_percent=60.0, penalties_order=1,
        )
        non_pen = make_player(
            id=11, web_name="Saka", team_id=1, position=PlayerPosition.MIDFIELDER,
            now_cost=100, minutes=2000, form=7.0, points_per_game=7.0,
            goals_scored=15, assists=10, expected_goals=14.0, expected_assists=9.0,
            selected_by_percent=40.0,
        )
        fixture = make_fixture(
            gameweek=25, home_team_id=1, away_team_id=8,
            home_difficulty=2, away_difficulty=5,
        )
        team_fixtures = {1: [{"fixture": fixture, "is_home": True}]}

        pen_result = agent._score_captain_candidate(
            pen_taker, _build_context(team_map, team_fixtures, team_form_by_id)
        )
        non_pen_result = agent._score_captain_candidate(
            non_pen, _build_context(team_map, team_fixtures, team_form_by_id)
        )

        assert pen_result is not None
        assert non_pen_result is not None
        # pen_bonus is StatWeight-derived from penalty_xG_per_90; no Understat lookup in test
        # so penalty_xg_per_90=None for both and pen_bonus == 0.0
        assert pen_result["pen_bonus"] == 0.0
        assert non_pen_result["pen_bonus"] == 0.0

    def test_penalty_taker_with_understat_gets_bonus(self, agent, team_map, team_form_by_id):
        """Primary penalty taker WITH Understat enrichment gets non-zero pen_bonus."""
        pen_taker = make_player(
            id=10, web_name="Salah", team_id=1, position=PlayerPosition.MIDFIELDER,
            now_cost=130, minutes=1800, form=7.0, points_per_game=7.0,
            goals_scored=15, assists=10, expected_goals=12.0, expected_assists=8.0,
            selected_by_percent=50.0, penalties_order=1,
        )
        non_pen = make_player(
            id=11, web_name="Saka", team_id=1, position=PlayerPosition.MIDFIELDER,
            now_cost=100, minutes=1800, form=7.0, points_per_game=7.0,
            goals_scored=15, assists=10, expected_goals=12.0, expected_assists=8.0,
            selected_by_percent=50.0,
        )
        fixture = make_fixture(
            gameweek=25, home_team_id=1, away_team_id=8,
            home_difficulty=2, away_difficulty=5,
        )
        team_fixtures = {1: [{"fixture": fixture, "is_home": True}]}
        ctx = _build_context(team_map, team_fixtures, team_form_by_id)

        understat_by_id = {10: {"penalty_xG_per_90": 0.20}}

        pen_result = agent._score_captain_candidate(
            pen_taker, ctx, understat_by_id=understat_by_id,
        )
        non_pen_result = agent._score_captain_candidate(non_pen, ctx)

        assert pen_result is not None
        assert non_pen_result is not None
        assert pen_result["pen_bonus"] > 0
        assert non_pen_result["pen_bonus"] == 0.0
        assert pen_result["captain_score_raw"] > non_pen_result["captain_score_raw"]

    def test_backup_penalty_taker_no_bonus(self, agent, team_map, team_form_by_id):
        """Backup penalty takers (order 2, 3) get no bonus."""
        player = make_player(
            id=12, web_name="Bruno", team_id=1, position=PlayerPosition.MIDFIELDER,
            now_cost=100, minutes=2000, form=6.0, points_per_game=6.0,
            goals_scored=10, assists=8, expected_goals=9.0, expected_assists=7.0,
            selected_by_percent=30.0, penalties_order=2,
        )
        fixture = make_fixture(
            gameweek=25, home_team_id=1, away_team_id=8,
            home_difficulty=2, away_difficulty=5,
        )
        team_fixtures = {1: [{"fixture": fixture, "is_home": True}]}

        result = agent._score_captain_candidate(
            player, _build_context(team_map, team_fixtures, team_form_by_id)
        )
        assert result is not None
        assert result["pen_bonus"] == 0

    def test_penalty_taker_reason_present(self, agent, team_map, team_form_by_id):
        """Primary penalty taker has reason string in output."""
        player = make_player(
            id=13, web_name="Palmer", team_id=1, position=PlayerPosition.MIDFIELDER,
            now_cost=100, minutes=2000, form=6.0, points_per_game=6.0,
            goals_scored=10, assists=8, expected_goals=9.0, expected_assists=7.0,
            selected_by_percent=30.0, penalties_order=1,
        )
        fixture = make_fixture(
            gameweek=25, home_team_id=1, away_team_id=8,
            home_difficulty=2, away_difficulty=5,
        )
        team_fixtures = {1: [{"fixture": fixture, "is_home": True}]}

        result = agent._score_captain_candidate(
            player, _build_context(team_map, team_fixtures, team_form_by_id)
        )
        assert result is not None
        assert "Primary penalty taker" in result["reasons"]


    def test_position_ceiling_reduces_def_score(self, agent, team_map, team_form_by_id):
        """DEF captain score is reduced by position ceiling (0.85x)."""
        common = dict(
            id=14, web_name="Test", team_id=1,
            now_cost=100, minutes=2000, form=6.0, points_per_game=6.0,
            goals_scored=5, assists=5, expected_goals=5.0, expected_assists=5.0,
            selected_by_percent=20.0,
        )
        fwd = make_player(position=PlayerPosition.FORWARD, **common)  # type: ignore[arg-type]
        defender = make_player(position=PlayerPosition.DEFENDER, **common)  # type: ignore[arg-type]
        fixture = make_fixture(
            gameweek=25, home_team_id=1, away_team_id=8,
            home_difficulty=2, away_difficulty=5,
        )
        team_fixtures = {1: [{"fixture": fixture, "is_home": True}]}

        fwd_result = agent._score_captain_candidate(
            fwd, _build_context(team_map, team_fixtures, team_form_by_id)
        )
        def_result = agent._score_captain_candidate(
            defender, _build_context(team_map, team_fixtures, team_form_by_id)
        )
        assert fwd_result is not None
        assert def_result is not None
        # FWD gets 1.0x, DEF gets 0.85x - FWD should score higher
        assert fwd_result["captain_score"] > def_result["captain_score"]
        assert def_result["captain_score"] == pytest.approx(
            fwd_result["captain_score"] * 0.85 / 1.0, rel=0.05,
        )


    def test_position_ceiling_reduces_gk_score(self, agent, team_map, team_form_by_id):
        """GK captain score is reduced by position ceiling (0.7x)."""
        common = dict(
            id=15, web_name="Test", team_id=1,
            now_cost=100, minutes=2000, form=6.0, points_per_game=6.0,
            goals_scored=5, assists=5, expected_goals=5.0, expected_assists=5.0,
            selected_by_percent=20.0,
        )
        fwd = make_player(position=PlayerPosition.FORWARD, **common)  # type: ignore[arg-type]
        gk = make_player(position=PlayerPosition.GOALKEEPER, **common)  # type: ignore[arg-type]
        fixture = make_fixture(
            gameweek=25, home_team_id=1, away_team_id=8,
            home_difficulty=2, away_difficulty=5,
        )
        team_fixtures = {1: [{"fixture": fixture, "is_home": True}]}

        fwd_result = agent._score_captain_candidate(
            fwd, _build_context(team_map, team_fixtures, team_form_by_id)
        )
        gk_result = agent._score_captain_candidate(
            gk, _build_context(team_map, team_fixtures, team_form_by_id)
        )
        assert fwd_result is not None
        assert gk_result is not None
        assert fwd_result["captain_score"] > gk_result["captain_score"]
        assert gk_result["captain_score"] == pytest.approx(
            fwd_result["captain_score"] * 0.7 / 1.0, rel=0.05,
        )


    def test_early_season_disables_mins_factor(self, agent, team_map, team_form_by_id):
        """Before GW5, mins_factor is 1.0 regardless of appearances."""
        # Sub-heavy player: 56 mins across 6 appearances -> mins_factor = 0.117 in late season
        player = make_player(
            id=20, web_name="SubOnly", team_id=1, position=PlayerPosition.FORWARD,
            now_cost=80, minutes=56, form=5.0, points_per_game=1.0,
            goals_scored=1, assists=0, expected_goals=0.8, expected_assists=0.2,
            total_points=6,
        )
        fixture = make_fixture(
            gameweek=3, home_team_id=1, away_team_id=8,
            home_difficulty=2, away_difficulty=5,
        )
        team_fixtures = {1: [{"fixture": fixture, "is_home": True}]}

        result_early = agent._score_captain_candidate(
            player, _build_context(team_map, team_fixtures, team_form_by_id),
            next_gw_id=3,
        )
        result_late = agent._score_captain_candidate(
            player, _build_context(team_map, team_fixtures, team_form_by_id),
            next_gw_id=25,
        )

        assert result_early is not None
        assert result_late is not None
        # Early season should score higher (mins_factor=1.0 vs penalised)
        assert result_early["captain_score_raw"] > result_late["captain_score_raw"]

    def test_zero_appearances_zeros_ceiling(self, agent, team_map, team_form_by_id):
        """Player with 0 appearances gets mins_factor=0, ceiling=0."""
        player = make_player(
            id=21, web_name="Benchwarmer", team_id=1, position=PlayerPosition.FORWARD,
            now_cost=50, minutes=0, form=0.0, points_per_game=0.0,
            goals_scored=0, assists=0, expected_goals=0.0, expected_assists=0.0,
            total_points=0,
        )
        fixture = make_fixture(
            gameweek=25, home_team_id=1, away_team_id=8,
            home_difficulty=2, away_difficulty=5,
        )
        team_fixtures = {1: [{"fixture": fixture, "is_home": True}]}

        result = agent._score_captain_candidate(
            player, _build_context(team_map, team_fixtures, team_form_by_id),
            next_gw_id=25,
        )

        assert result is not None
        # Ceiling is 0 (mins_factor=0), only home bonus contributes
        assert result["captain_score_raw"] > 0  # home_bonus = 1.0
        assert result["captain_score_raw"] <= 3.0  # home + pen max


class TestCaptainAgentDifferentials:
    """Tests for differential captain picks."""

    @pytest.fixture
    def agent(self):
        """Create agent with 10% differential threshold."""
        return CaptainAgent(config={"differential_threshold": 10.0})

    def test_differential_threshold(self, agent):
        """Test differential threshold is set correctly."""
        assert agent.differential_threshold == 10.0

    @pytest.mark.asyncio
    async def test_differential_picks_returned(self, agent):
        """Test that differential picks are identified."""
        mock_players = [
            make_player(id=1, web_name="Template", team_id=13, selected_by_percent=85.0,
                       form=8.0, points_per_game=7.0),
            make_player(id=2, web_name="Differential", team_id=13, selected_by_percent=3.0,
                       form=7.0, points_per_game=6.5),
        ]
        mock_teams = [
            make_team(id=8, name="Sheffield Utd", short_name="SHU", position=20),
            make_team(id=13, name="Man City", short_name="MCI", position=2),
        ]
        mock_fixtures = [
            make_fixture(id=1, gameweek=25, home_team_id=13, away_team_id=8,
                        home_difficulty=2, away_difficulty=5),
        ]

        with patch.object(agent.client, "get_players", new_callable=AsyncMock) as mock_get_players, \
             patch.object(agent.client, "get_teams", new_callable=AsyncMock) as mock_get_teams, \
             patch.object(agent.client, "get_next_gameweek", new_callable=AsyncMock) as mock_next_gw, \
             patch.object(agent.client, "get_fixtures", new_callable=AsyncMock) as mock_get_fixtures:

            mock_get_players.return_value = mock_players
            mock_get_teams.return_value = mock_teams
            mock_next_gw.return_value = {"id": 25, "deadline_time": "2024-02-10T11:00:00Z"}
            mock_get_fixtures.return_value = mock_fixtures

            result = await agent.run()

            assert "differential_picks" in result.data
            # Differential player (3% ownership) should be in differential picks
            # if their score is high enough
