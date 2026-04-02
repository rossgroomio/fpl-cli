"""Tests for data collection agents."""

from datetime import datetime, timedelta
from unittest.mock import AsyncMock, patch

import pytest

from fpl_cli.agents.base import AgentStatus
from fpl_cli.agents.data.fixture import FixtureAgent
from fpl_cli.agents.data.scout import ScoutAgent
from fpl_cli.agents.analysis.stats import StatsAgent
from fpl_cli.api.providers import LLMResponse, TokenUsage
from fpl_cli.services.team_form import calculate_team_form
from tests.conftest import make_fixture, make_player, make_team


class TestFixtureAgent:
    """Tests for FixtureAgent."""

    @pytest.fixture
    def agent(self):
        """Create a fixture agent."""
        return FixtureAgent()

    @pytest.fixture
    def configured_agent(self):
        """Create agent with custom config."""
        return FixtureAgent(config={"lookahead_gameweeks": 4})

    @pytest.fixture
    def mock_teams(self):
        """Create mock teams."""
        return [
            make_team(id=1, name="Arsenal", short_name="ARS", position=1),
            make_team(id=2, name="Man City", short_name="MCI", position=2),
            make_team(id=3, name="Liverpool", short_name="LIV", position=3),
            make_team(id=4, name="Chelsea", short_name="CHE", position=4),
        ]

    @pytest.fixture
    def mock_fixtures(self):
        """Create mock fixtures."""
        base_time = datetime.now()
        return [
            # GW 25 fixtures
            make_fixture(id=1, gameweek=25, home_team_id=1, away_team_id=2,
                        home_difficulty=4, away_difficulty=4, finished=False,
                        kickoff_time=base_time + timedelta(days=7)),
            make_fixture(id=2, gameweek=25, home_team_id=3, away_team_id=4,
                        home_difficulty=3, away_difficulty=3, finished=False,
                        kickoff_time=base_time + timedelta(days=7)),
            # GW 26 fixtures
            make_fixture(id=3, gameweek=26, home_team_id=2, away_team_id=3,
                        home_difficulty=4, away_difficulty=4, finished=False,
                        kickoff_time=base_time + timedelta(days=14)),
            make_fixture(id=4, gameweek=26, home_team_id=4, away_team_id=1,
                        home_difficulty=4, away_difficulty=3, finished=False,
                        kickoff_time=base_time + timedelta(days=14)),
            # Completed GW 24 fixture
            make_fixture(id=5, gameweek=24, home_team_id=1, away_team_id=3,
                        home_difficulty=3, away_difficulty=4, finished=True,
                        home_score=2, away_score=1,
                        kickoff_time=base_time - timedelta(days=7)),
        ]

    def test_agent_initialization(self, agent):
        """Test agent default initialization."""
        assert agent.name == "FixtureAgent"
        assert agent.lookahead_gameweeks == 6

    def test_agent_custom_lookahead(self, configured_agent):
        """Test agent with custom lookahead."""
        assert configured_agent.lookahead_gameweeks == 4

    @pytest.mark.asyncio
    async def test_gw_window_default(self, mock_teams, mock_fixtures):
        """Default: window starts at current_gw, ends at current_gw + lookahead."""
        agent = FixtureAgent()
        with patch.object(agent.client, "get_next_gameweek", new_callable=AsyncMock) as mock_next_gw, \
             patch.object(agent.client, "get_fixtures", new_callable=AsyncMock) as mock_get_fixtures, \
             patch.object(agent.client, "get_teams", new_callable=AsyncMock) as mock_get_teams:

            mock_next_gw.return_value = {"id": 25}
            mock_get_fixtures.return_value = mock_fixtures
            mock_get_teams.return_value = mock_teams

            result = await agent.run()

            assert result.status == AgentStatus.SUCCESS
            gws = {f["gameweek"] for f in result.data["fixtures"]}
            assert 25 in gws
            assert 26 in gws
            assert 24 not in gws  # past GW excluded

    @pytest.mark.asyncio
    async def test_gw_window_from_gw_only(self, mock_teams, mock_fixtures):
        """from_gw only: window is from_gw to from_gw + lookahead."""
        agent = FixtureAgent(config={"from_gw": 26})
        with patch.object(agent.client, "get_next_gameweek", new_callable=AsyncMock) as mock_next_gw, \
             patch.object(agent.client, "get_fixtures", new_callable=AsyncMock) as mock_get_fixtures, \
             patch.object(agent.client, "get_teams", new_callable=AsyncMock) as mock_get_teams:

            mock_next_gw.return_value = {"id": 25}
            mock_get_fixtures.return_value = mock_fixtures
            mock_get_teams.return_value = mock_teams

            result = await agent.run()

            assert result.status == AgentStatus.SUCCESS
            gws = {f["gameweek"] for f in result.data["fixtures"]}
            assert 25 not in gws  # before from_gw
            assert 26 in gws

    @pytest.mark.asyncio
    async def test_gw_window_to_gw_only(self, mock_teams, mock_fixtures):
        """to_gw only: window is current_gw to to_gw."""
        agent = FixtureAgent(config={"to_gw": 25})
        with patch.object(agent.client, "get_next_gameweek", new_callable=AsyncMock) as mock_next_gw, \
             patch.object(agent.client, "get_fixtures", new_callable=AsyncMock) as mock_get_fixtures, \
             patch.object(agent.client, "get_teams", new_callable=AsyncMock) as mock_get_teams:

            mock_next_gw.return_value = {"id": 25}
            mock_get_fixtures.return_value = mock_fixtures
            mock_get_teams.return_value = mock_teams

            result = await agent.run()

            assert result.status == AgentStatus.SUCCESS
            gws = {f["gameweek"] for f in result.data["fixtures"]}
            assert 25 in gws
            assert 26 not in gws  # beyond to_gw

    @pytest.mark.asyncio
    async def test_gw_window_explicit_range(self, mock_teams, mock_fixtures):
        """Both from_gw and to_gw: exact window, fixtures outside excluded."""
        agent = FixtureAgent(config={"from_gw": 26, "to_gw": 26})
        with patch.object(agent.client, "get_next_gameweek", new_callable=AsyncMock) as mock_next_gw, \
             patch.object(agent.client, "get_fixtures", new_callable=AsyncMock) as mock_get_fixtures, \
             patch.object(agent.client, "get_teams", new_callable=AsyncMock) as mock_get_teams:

            mock_next_gw.return_value = {"id": 25}
            mock_get_fixtures.return_value = mock_fixtures
            mock_get_teams.return_value = mock_teams

            result = await agent.run()

            assert result.status == AgentStatus.SUCCESS
            gws = {f["gameweek"] for f in result.data["fixtures"]}
            assert gws == {26}
            assert "GW26-26" in result.message

    @pytest.mark.asyncio
    async def test_run_success(self, agent, mock_teams, mock_fixtures):
        """Test successful fixture analysis run."""
        with patch.object(agent.client, "get_next_gameweek", new_callable=AsyncMock) as mock_next_gw, \
             patch.object(agent.client, "get_fixtures", new_callable=AsyncMock) as mock_get_fixtures, \
             patch.object(agent.client, "get_teams", new_callable=AsyncMock) as mock_get_teams:

            mock_next_gw.return_value = {"id": 25}
            mock_get_fixtures.return_value = mock_fixtures
            mock_get_teams.return_value = mock_teams

            result = await agent.run()

            assert result.status == AgentStatus.SUCCESS
            assert result.data["current_gameweek"] == 25
            assert "fixtures" in result.data
            assert "fdr_by_team" in result.data
            assert "team_form" in result.data

    @pytest.mark.asyncio
    async def test_run_no_next_gameweek(self, agent):
        """Test handling when no next gameweek (season ended)."""
        with patch.object(agent.client, "get_next_gameweek", new_callable=AsyncMock) as mock_next_gw:
            mock_next_gw.return_value = None

            result = await agent.run()

            assert result.status == AgentStatus.FAILED
            assert "Could not determine next gameweek" in result.message

    @pytest.mark.asyncio
    async def test_run_handles_api_error(self, agent):
        """Test handling API errors."""
        with patch.object(agent.client, "get_next_gameweek", new_callable=AsyncMock) as mock_next_gw:
            mock_next_gw.side_effect = Exception("API Error")

            result = await agent.run()

            assert result.status == AgentStatus.FAILED
            assert len(result.errors) > 0

    def test_group_by_gameweek(self, agent, mock_fixtures):
        """Test grouping fixtures by gameweek."""
        grouped = agent._group_by_gameweek(mock_fixtures)

        assert 24 in grouped
        assert 25 in grouped
        assert 26 in grouped
        assert len(grouped[25]) == 2
        assert len(grouped[26]) == 2

    def test_find_blank_gameweeks(self, agent, mock_fixtures, mock_teams):
        """Test finding teams with blank gameweeks."""
        # Create a scenario where team 4 doesn't play in GW 27
        fixtures_by_gw = {
            27: [
                make_fixture(gameweek=27, home_team_id=1, away_team_id=2),
                make_fixture(gameweek=27, home_team_id=3, away_team_id=5),  # Team 5 doesn't exist in mock_teams
            ],
        }
        # Add team 5 to make it complete
        teams = mock_teams + [make_team(id=5, name="Test", short_name="TST", position=5)]

        from fpl_cli.services.fixture_predictions import find_blank_gameweeks
        blank_gws = find_blank_gameweeks(fixtures_by_gw, teams, 27, 27)

        assert 27 in blank_gws
        # Team 4 should be blank
        blank_team_ids = [t["team_id"] for t in blank_gws[27]]
        assert 4 in blank_team_ids

    def test_find_double_gameweeks(self, agent, mock_teams):
        """Test finding teams with double gameweeks."""
        fixtures_by_gw = {
            27: [
                make_fixture(id=1, gameweek=27, home_team_id=1, away_team_id=2),
                make_fixture(id=2, gameweek=27, home_team_id=1, away_team_id=3),  # Team 1 plays twice
                make_fixture(id=3, gameweek=27, home_team_id=4, away_team_id=2),  # Team 2 plays twice
            ],
        }

        from fpl_cli.services.fixture_predictions import find_double_gameweeks
        double_gws = find_double_gameweeks(fixtures_by_gw, mock_teams)

        assert 27 in double_gws
        double_team_ids = [t["team_id"] for t in double_gws[27]]
        assert 1 in double_team_ids  # Arsenal has DGW
        assert 2 in double_team_ids  # Man City has DGW

    def test_analyze_fdr(self, agent, mock_teams, mock_fixtures):
        """Test FDR analysis."""
        team_map = {t.id: t for t in mock_teams}
        fdr = agent._analyze_fdr(mock_fixtures, team_map, 25, 26)

        assert "ARS" in fdr
        assert "MCI" in fdr
        assert fdr["ARS"]["team_name"] == "Arsenal"
        assert "average_fdr" in fdr["ARS"]
        assert "fixtures" in fdr["ARS"]

    def test_find_easy_runs(self, agent, mock_teams):
        """Test finding teams with easy fixture runs."""
        team_map = {t.id: t for t in mock_teams}
        fdr_analysis = {
            "ARS": {"team_name": "Arsenal", "average_fdr": 2.5, "average_fdr_atk": 2.3,
                   "average_fdr_def": 2.7, "fixture_count": 6,
                   "fixtures": [{"opponent": "SHU", "is_home": True, "fdr": 2}]},
            "MCI": {"team_name": "Man City", "average_fdr": 3.5, "average_fdr_atk": 3.2,
                   "average_fdr_def": 3.8, "fixture_count": 6,
                   "fixtures": [{"opponent": "LIV", "is_home": False, "fdr": 4}]},
        }

        easy_runs = agent._find_easy_runs(fdr_analysis, team_map)

        # Should return dict with overall, for_attackers, for_defenders
        assert "overall" in easy_runs
        assert "for_attackers" in easy_runs
        assert "for_defenders" in easy_runs
        assert len(easy_runs["overall"]) <= 10
        # Arsenal should be ranked higher (easier fixtures)
        assert easy_runs["overall"][0]["short_name"] == "ARS"
        assert easy_runs["for_attackers"][0]["short_name"] == "ARS"
        assert easy_runs["for_defenders"][0]["short_name"] == "ARS"

    def test_fixture_to_dict(self, agent, mock_teams):
        """Test fixture to dict conversion."""
        team_map = {t.id: t for t in mock_teams}
        fixture = make_fixture(
            id=1, gameweek=22, home_team_id=1, away_team_id=2,
            home_difficulty=3, away_difficulty=4,
        )

        fixture_dict = agent._fixture_to_dict(fixture, team_map)

        assert fixture_dict["id"] == 1
        assert fixture_dict["gameweek"] == 22
        assert fixture_dict["home_team"] == "ARS"
        assert fixture_dict["away_team"] == "MCI"
        # FDR comes from team ratings avg_overall_fdr (not FPL API difficulty)
        # ARS rated (2,1,1,1) -> avg_overall=1.25 -> away_fdr=6.75
        # MCI rated (2,3,2,2) -> avg_overall=2.25 -> home_fdr=5.75
        assert fixture_dict["home_fdr"] == 5.75
        assert fixture_dict["away_fdr"] == 6.75

    def test_fixture_to_dict_fdr_uses_opponent_rating(self, mock_teams):
        """FDR for home team is derived from away team's rating, and vice versa."""
        from unittest.mock import MagicMock

        from fpl_cli.services.team_ratings import TeamRating

        agent = FixtureAgent()
        mock_svc = MagicMock()

        # Strong away team (low rating) = hard fixture for home
        # Weak home team (high rating) = easy fixture for away
        def _get_rating(short):
            if short == "MCI":
                return TeamRating(atk_home=1, atk_away=1, def_home=1, def_away=1)  # avg=1.0
            if short == "ARS":
                return TeamRating(atk_home=6, atk_away=6, def_home=6, def_away=6)  # avg=6.0
            return None

        mock_svc.get_rating.side_effect = _get_rating
        agent.ratings_service = mock_svc

        team_map = {t.id: t for t in mock_teams}
        fixture = make_fixture(id=1, gameweek=22, home_team_id=1, away_team_id=2,
                               home_difficulty=3, away_difficulty=4)
        result = agent._fixture_to_dict(fixture, team_map)

        # Home FDR = opponent (MCI) avg_overall_fdr = 8 - 1.0 = 7.0 (very hard)
        assert result["home_fdr"] == 7.0
        # Away FDR = opponent (ARS) avg_overall_fdr = 8 - 6.0 = 2.0 (easy)
        assert result["away_fdr"] == 2.0


class TestFixtureAgentSquadExposure:
    """Tests for _analyze_squad_exposure."""

    @pytest.fixture
    def agent(self):
        return FixtureAgent()

    @pytest.fixture
    def teams(self):
        return [
            make_team(id=1, short_name="LIV"),
            make_team(id=2, short_name="MCI"),
            make_team(id=3, short_name="ARS"),
            make_team(id=4, short_name="CHE"),
        ]

    def _make_blank_pred(self, gw, team_names):
        from fpl_cli.services.fixture_predictions import BlankPrediction, Confidence
        return BlankPrediction(
            gameweek=gw,
            teams=team_names,
            confidence=Confidence.HIGH,
        )

    def _make_double_pred(self, gw, team_names):
        from fpl_cli.services.fixture_predictions import Confidence, DoublePrediction
        return DoublePrediction(
            gameweek=gw,
            teams=team_names,
            confidence=Confidence.HIGH,
        )

    def test_confirmed_blank_exposure(self, agent, teams):
        squad = [
            {"team_id": 1, "element_type": 3, "web_name": "Salah"},
            {"team_id": 1, "element_type": 2, "web_name": "TAA"},
            {"team_id": 2, "element_type": 3, "web_name": "Haaland"},
            {"team_id": 3, "element_type": 2, "web_name": "Saka"},
        ]
        blank_gws = {31: [{"team_id": 1}, {"team_id": 2}]}
        result = agent._analyze_squad_exposure(squad, blank_gws, {}, teams)

        assert len(result) == 1
        entry = result[0]
        assert entry["gw"] == 31
        assert entry["type"] == "blank"
        assert entry["affected"] == 3
        assert entry["total"] == 4
        assert entry["source"] == "confirmed"
        assert set(entry["players"]) == {"Salah", "TAA", "Haaland"}

    def test_confirmed_double_exposure(self, agent, teams):
        squad = [
            {"team_id": 2, "element_type": 4, "web_name": "Haaland"},
            {"team_id": 2, "element_type": 3, "web_name": "Foden"},
        ]
        double_gws = {33: [{"team_id": 2}]}
        result = agent._analyze_squad_exposure(squad, {}, double_gws, teams)

        assert len(result) == 1
        assert result[0]["type"] == "double"
        assert result[0]["affected"] == 2

    def test_position_capping_gk(self, agent, teams):
        """3 GKs affected -> only 1 projected starter."""
        squad = [
            {"team_id": 1, "element_type": 1, "web_name": "GK1"},
            {"team_id": 1, "element_type": 1, "web_name": "GK2"},
            {"team_id": 1, "element_type": 1, "web_name": "GK3"},
        ]
        blank_gws = {31: [{"team_id": 1}]}
        result = agent._analyze_squad_exposure(squad, blank_gws, {}, teams)

        assert result[0]["starters"] == 1

    def test_position_capping_full_squad(self, agent, teams):
        """Formation cap: 1 GK + 5 DEF + 5 MID + 3 FWD = 11 max."""
        squad = (
            [{"team_id": 1, "element_type": 1, "web_name": f"GK{i}"} for i in range(2)] +
            [{"team_id": 1, "element_type": 2, "web_name": f"DEF{i}"} for i in range(5)] +
            [{"team_id": 1, "element_type": 3, "web_name": f"MID{i}"} for i in range(5)] +
            [{"team_id": 1, "element_type": 4, "web_name": f"FWD{i}"} for i in range(3)]
        )
        blank_gws = {31: [{"team_id": 1}]}
        result = agent._analyze_squad_exposure(squad, blank_gws, {}, teams)

        assert result[0]["starters"] == 11

    def test_empty_squad_returns_empty(self, agent, teams):
        blank_gws = {31: [{"team_id": 1}]}
        result = agent._analyze_squad_exposure([], blank_gws, {}, teams)
        assert result == []

    def test_no_blanks_or_doubles_returns_empty(self, agent, teams):
        squad = [{"team_id": 1, "element_type": 3, "web_name": "Salah"}]
        result = agent._analyze_squad_exposure(squad, {}, {}, teams)
        assert result == []

    def test_no_squad_overlap_omitted(self, agent, teams):
        """GW with blank teams not in squad produces no entry."""
        squad = [{"team_id": 4, "element_type": 3, "web_name": "Palmer"}]
        blank_gws = {31: [{"team_id": 1}, {"team_id": 2}]}
        result = agent._analyze_squad_exposure(squad, blank_gws, {}, teams)
        assert result == []

    def test_predicted_blank_with_empty_teams_skipped(self, agent, teams):
        squad = [{"team_id": 1, "element_type": 3, "web_name": "Salah"}]
        pred = self._make_blank_pred(31, [])  # teams: []
        result = agent._analyze_squad_exposure(squad, {}, {}, teams, predicted_blanks=[pred])
        assert result == []

    def test_predicted_blank_matched_via_short_name(self, agent, teams):
        squad = [{"team_id": 1, "element_type": 3, "web_name": "Salah"}]
        pred = self._make_blank_pred(31, ["LIV"])
        result = agent._analyze_squad_exposure(squad, {}, {}, teams, predicted_blanks=[pred])

        assert len(result) == 1
        assert result[0]["source"] == "predicted"
        assert result[0]["players"] == ["Salah"]

    def test_predicted_suppressed_by_confirmed(self, agent, teams):
        """Predicted blank for same GW+type as confirmed is fully suppressed when teams overlap."""
        squad = [{"team_id": 1, "element_type": 3, "web_name": "Salah"}]
        blank_gws = {31: [{"team_id": 1}]}
        pred = self._make_blank_pred(31, ["LIV"])
        result = agent._analyze_squad_exposure(squad, blank_gws, {}, teams, predicted_blanks=[pred])

        # Only one entry (confirmed), not two
        assert len(result) == 1
        assert result[0]["source"] == "confirmed"

    def test_predicted_partial_overlap_keeps_unconfirmed_teams(self, agent, teams):
        """Predicted blank sharing a GW with confirmed keeps non-overlapping teams."""
        squad = [
            {"team_id": 2, "element_type": 3, "web_name": "Haaland"},
            {"team_id": 3, "element_type": 3, "web_name": "Saka"},
            {"team_id": 4, "element_type": 3, "web_name": "Palmer"},
        ]
        # GW34: MCI+ARS confirmed blank, MCI+CHE predicted blank
        blank_gws = {34: [{"team_id": 2}, {"team_id": 3}]}
        pred = self._make_blank_pred(34, ["MCI", "CHE"])
        result = agent._analyze_squad_exposure(
            squad, blank_gws, {}, teams, predicted_blanks=[pred],
        )

        # Two entries: confirmed (MCI+ARS) and predicted (CHE only - MCI subtracted)
        assert len(result) == 2
        confirmed = [r for r in result if r["source"] == "confirmed"][0]
        predicted = [r for r in result if r["source"] == "predicted"][0]
        assert set(confirmed["players"]) == {"Haaland", "Saka"}
        assert predicted["players"] == ["Palmer"]

    def test_results_sorted_by_gw(self, agent, teams):
        squad = [
            {"team_id": 1, "element_type": 3, "web_name": "Salah"},
            {"team_id": 2, "element_type": 3, "web_name": "Haaland"},
        ]
        blank_gws = {35: [{"team_id": 1}], 31: [{"team_id": 2}]}
        result = agent._analyze_squad_exposure(squad, blank_gws, {}, teams)

        assert result[0]["gw"] == 31
        assert result[1]["gw"] == 35


class TestFixtureAgentTeamForm:
    """Tests for team form calculation."""

    @pytest.fixture
    def agent(self):
        """Create a fixture agent."""
        return FixtureAgent()

    @pytest.fixture
    def teams(self):
        """Create test teams."""
        return [
            make_team(id=1, name="Arsenal", short_name="ARS", position=1),
            make_team(id=2, name="Man City", short_name="MCI", position=2),
        ]

    @pytest.fixture
    def completed_fixtures(self):
        """Create completed fixtures for form calculation."""
        base_time = datetime.now() - timedelta(days=1)
        fixtures = []
        # 6 completed home wins for team 1
        for i in range(6):
            fixtures.append(make_fixture(
                id=100 + i, gameweek=20 - i, home_team_id=1, away_team_id=2,
                home_difficulty=3, away_difficulty=3,
                finished=True, home_score=2, away_score=0,
                kickoff_time=base_time - timedelta(days=7 * i),
            ))
        # Add upcoming fixture
        fixtures.append(make_fixture(
            id=200, gameweek=25, home_team_id=1, away_team_id=2,
            home_difficulty=3, away_difficulty=3,
            finished=False, kickoff_time=datetime.now() + timedelta(days=7),
        ))
        return fixtures

    def test_calculate_team_form(self, agent, teams, completed_fixtures):
        """Test team form calculation."""
        form = calculate_team_form(completed_fixtures, teams)

        assert len(form) == 2
        # Find Arsenal's form
        arsenal_form = next(f for f in form if f["team"] == "ARS")
        assert arsenal_form["pts_6"] > 0
        assert "gs_6" in arsenal_form
        assert "gc_6" in arsenal_form
        assert "next_venue" in arsenal_form


class TestFixtureAgentMatchupScore:
    """Tests for matchup score calculation."""

    @pytest.fixture
    def agent(self):
        """Create a fixture agent."""
        return FixtureAgent()

    @pytest.fixture
    def team_form(self):
        """Sample team form data."""
        return {
            "team": "ARS",
            "team_id": 1,
            "league_position": 1,
            "pts_6": 15,
            "gs_6": 12,
            "gc_6": 3,
            "pts_home": 12,
            "gs_home": 10,
            "gc_home": 1,
            "pts_away": 9,
            "gs_away": 6,
            "gc_away": 4,
        }

    @pytest.fixture
    def opponent_form(self):
        """Sample opponent form data."""
        return {
            "team": "SHU",
            "team_id": 20,
            "league_position": 20,
            "pts_6": 3,
            "gs_6": 4,
            "gc_6": 15,
            "pts_home": 3,
            "gs_home": 3,
            "gc_home": 10,
            "pts_away": 0,
            "gs_away": 1,
            "gc_away": 12,
        }

    def test_calculate_matchup_score_forward(self, agent, team_form, opponent_form):
        """Test matchup calculation for forward."""
        result = agent.calculate_matchup_score(
            team_form, opponent_form, "FWD", is_home=True
        )

        assert "matchup_score" in result
        assert "attack_matchup" in result
        assert "defence_matchup" in result
        assert "form_differential" in result
        assert "position_differential" in result
        assert "reasoning" in result

        # Should be a favorable matchup (top team vs bottom)
        assert result["matchup_score"] > 5

    def test_calculate_matchup_score_defender(self, agent, team_form, opponent_form):
        """Test matchup calculation for defender."""
        result = agent.calculate_matchup_score(
            team_form, opponent_form, "DEF", is_home=True
        )

        # DEF weights defence more than attack
        assert result["matchup_score"] > 0

    def test_calculate_matchup_score_goalkeeper(self, agent, team_form, opponent_form):
        """Test matchup calculation for goalkeeper."""
        result = agent.calculate_matchup_score(
            team_form, opponent_form, "GK", is_home=True
        )

        # GK weights defence most heavily
        assert result["matchup_score"] > 0

    def test_calculate_matchup_score_away(self, agent, team_form, opponent_form):
        """Test matchup calculation when playing away."""
        home_result = agent.calculate_matchup_score(
            team_form, opponent_form, "MID", is_home=True
        )
        away_result = agent.calculate_matchup_score(
            team_form, opponent_form, "MID", is_home=False
        )

        # Both should be valid scores
        assert home_result["matchup_score"] > 0
        assert away_result["matchup_score"] > 0

    def test_position_weights_exist(self, agent):
        """Test position weights are defined."""
        assert "FWD" in agent.POSITION_WEIGHTS
        assert "MID" in agent.POSITION_WEIGHTS
        assert "DEF" in agent.POSITION_WEIGHTS
        assert "GK" in agent.POSITION_WEIGHTS

        # Check weights sum to 1.0
        for pos, weights in agent.POSITION_WEIGHTS.items():
            total = sum(weights.values())
            assert abs(total - 1.0) < 0.01, f"{pos} weights don't sum to 1.0"


class TestFixtureAgentPositionalFDR:
    """Tests for position-specific FDR calculations in FixtureAgent."""

    @pytest.fixture
    def agent(self):
        """Create a fixture agent."""
        return FixtureAgent()

    @pytest.fixture
    def temp_ratings_config(self, tmp_path):
        """Create temp ratings config with known values."""
        from datetime import datetime

        import yaml

        config_path = tmp_path / "team_ratings.yaml"
        config_data = {
            "metadata": {
                "last_updated": datetime.now().strftime("%Y-%m-%d"),
                "source": "test",
                "staleness_threshold_days": 30,
            },
            "ratings": {
                "LIV": {"atk_home": 1, "atk_away": 2, "def_home": 1, "def_away": 2},
                "SHU": {"atk_home": 6, "atk_away": 7, "def_home": 6, "def_away": 7},
                "ARS": {"atk_home": 2, "atk_away": 2, "def_home": 2, "def_away": 2},
                "MCI": {"atk_home": 1, "atk_away": 1, "def_home": 2, "def_away": 3},
            },
        }
        with open(config_path, "w", encoding="utf-8") as f:
            yaml.dump(config_data, f)
        return config_path

    def test_get_positional_fdr_forward(self, agent, temp_ratings_config):
        """Test positional FDR for forward."""
        from fpl_cli.services.team_ratings import TeamRatingsService

        agent.ratings_service = TeamRatingsService(config_path=temp_ratings_config)

        fdr = agent.get_positional_fdr("FWD", "LIV", "SHU", is_home=True, mode="opponent")

        # FWD: weak opponent defence (rating 7) → inverted to FDR 1 (easy)
        assert fdr == 1.0

    def test_get_positional_fdr_defender(self, agent, temp_ratings_config):
        """Test positional FDR for defender."""
        from fpl_cli.services.team_ratings import TeamRatingsService

        agent.ratings_service = TeamRatingsService(config_path=temp_ratings_config)

        fdr = agent.get_positional_fdr("DEF", "LIV", "SHU", is_home=True, mode="opponent")

        # DEF: weak opponent attack (rating 7) → inverted to FDR 1 (easy)
        assert fdr == 1.0

    def test_get_positional_fdr_uses_agent_mode(self, temp_ratings_config):
        """Test positional FDR uses agent's configured mode."""
        from fpl_cli.services.team_ratings import TeamRatingsService

        # Create agent with specific mode
        agent = FixtureAgent(config={"fdr_mode": "difference"})
        agent.ratings_service = TeamRatingsService(config_path=temp_ratings_config)

        fdr_default = agent.get_positional_fdr("FWD", "LIV", "SHU", is_home=True)

        # Should use difference mode ((8-opp_def) + team_off) / 2 = (1 + 1) / 2 = 1.0
        assert fdr_default == 1.0

    def test_get_fixture_fdr_by_position(self, agent, temp_ratings_config):
        """Test getting FDR for all position groups."""
        from fpl_cli.services.team_ratings import TeamRatingsService

        agent.ratings_service = TeamRatingsService(config_path=temp_ratings_config)

        fdr = agent.get_fixture_fdr_by_position("LIV", "SHU", is_home=True, mode="opponent")

        assert "ATK" in fdr
        assert "DEF" in fdr
        # ATK: weak opponent defence (rating 7) → inverted to FDR 1 (easy)
        assert fdr["ATK"] == 1.0
        # DEF: weak opponent attack (rating 7) → inverted to FDR 1 (easy)
        assert fdr["DEF"] == 1.0

    def test_get_fixture_fdr_by_position_rounds(self, agent, temp_ratings_config):
        """Test FDR values are rounded to 1 decimal."""
        from fpl_cli.services.team_ratings import TeamRatingsService

        agent.ratings_service = TeamRatingsService(config_path=temp_ratings_config)

        # Difference mode produces averages that may need rounding
        fdr = agent.get_fixture_fdr_by_position("LIV", "SHU", is_home=True, mode="difference")

        # Values should be rounded to 1 decimal place
        assert fdr["ATK"] == round(fdr["ATK"], 1)
        assert fdr["DEF"] == round(fdr["DEF"], 1)

    def test_get_fixture_fdr_asymmetric(self, agent, temp_ratings_config):
        """Test ATK and DEF FDR can differ for same fixture."""
        from fpl_cli.services.team_ratings import TeamRatingsService

        agent.ratings_service = TeamRatingsService(config_path=temp_ratings_config)

        # Using MCI (strong attack=1, weaker defence=3 away)
        fdr = agent.get_fixture_fdr_by_position("ARS", "MCI", is_home=True, mode="opponent")

        # ATK: MCI away defence rating 3 → inverted to FDR 5 (moderately hard)
        assert fdr["ATK"] == 5.0
        # DEF: MCI away attack rating 1 → inverted to FDR 7 (hardest)
        assert fdr["DEF"] == 7.0


class TestStatsAgent:
    """Tests for StatsAgent."""

    @pytest.fixture
    def agent(self):
        """Create a stats agent."""
        return StatsAgent()

    @pytest.fixture
    def configured_agent(self):
        """Create agent with custom config."""
        return StatsAgent(config={
            "gameweeks": 10,
            "differential_threshold": 3.0,
        })

    def test_agent_initialization(self, agent):
        """Test default initialization."""
        assert agent.name == "StatsAgent"
        assert agent.gameweeks == 6
        assert agent.min_minutes == 360  # 60 * 6
        assert agent.differential_threshold == 5.0

    def test_agent_custom_config(self, configured_agent):
        """Test custom config."""
        assert configured_agent.gameweeks == 10
        assert configured_agent.min_minutes == 600  # 60 * 10
        assert configured_agent.differential_threshold == 3.0

    def test_agent_whole_season_config(self):
        """Test config for whole season analysis."""
        agent = StatsAgent(config={"gameweeks": 0})
        assert agent.gameweeks == 0
        assert agent.min_minutes == 450  # Default for whole season

    def test_calculate_player_stats(self, agent):
        """Test player stats calculation."""
        player = make_player(
            id=1, web_name="Salah", team_id=14,
            minutes=900, goals_scored=6, assists=4,
            expected_goals=5.5, expected_assists=3.8,
            form=7.0, total_points=80, points_per_game=6.5,
        )
        team_map = {14: make_team(id=14, name="Liverpool", short_name="LIV")}

        stats = agent._calculate_player_stats(player, team_map)

        assert stats["id"] == 1
        assert stats["player_name"] == "Salah"
        assert stats["team_short"] == "LIV"
        assert stats["minutes"] == 900
        assert stats["goals"] == 6
        assert stats["assists"] == 4
        assert stats["GI"] == 10  # 6 + 4
        assert stats["xG"] == 5.5
        assert stats["xA"] == 3.8
        assert stats["xGI"] == 9.3  # 5.5 + 3.8
        # Per 90 calculations
        assert stats["xG_per_90"] == pytest.approx(0.55, rel=0.01)
        assert stats["xA_per_90"] == pytest.approx(0.38, rel=0.01)
        # Over/underperformance
        assert stats["goals_minus_xG"] == pytest.approx(0.5, rel=0.01)

    def test_find_underperformers(self, agent):
        """Test finding underperforming players."""
        players = [
            {"player_name": "Player1", "team_short": "ARS", "position": "MID", "price": 10.0,
             "GI": 4, "xGI": 8.0, "GI_minus_xGI": -4.0, "xGI_per_90": 0.8, "minutes": 900},
            {"player_name": "Player2", "team_short": "CHE", "position": "FWD", "price": 8.0,
             "GI": 6, "xGI": 5.0, "GI_minus_xGI": 1.0, "xGI_per_90": 0.5, "minutes": 900},
            {"player_name": "Player3", "team_short": "LIV", "position": "MID", "price": 9.0,
             "GI": 3, "xGI": 6.0, "GI_minus_xGI": -3.0, "xGI_per_90": 0.6, "minutes": 900},
        ]

        underperformers = agent._find_underperformers(players, threshold=-2.0)

        assert len(underperformers) == 2
        # Should be sorted by biggest underperformance
        assert underperformers[0]["player_name"] == "Player1"  # -4.0
        assert underperformers[1]["player_name"] == "Player3"  # -3.0

    def test_find_overperformers(self, agent):
        """Test finding overperforming players."""
        players = [
            {"player_name": "Player1", "team_short": "ARS", "position": "MID", "price": 10.0,
             "GI": 10, "xGI": 6.0, "GI_minus_xGI": 4.0, "xGI_per_90": 0.6, "minutes": 900},
            {"player_name": "Player2", "team_short": "CHE", "position": "FWD", "price": 8.0,
             "GI": 5, "xGI": 5.0, "GI_minus_xGI": 0.0, "xGI_per_90": 0.5, "minutes": 900},
            {"player_name": "Player3", "team_short": "LIV", "position": "MID", "price": 9.0,
             "GI": 9, "xGI": 5.5, "GI_minus_xGI": 3.5, "xGI_per_90": 0.55, "minutes": 900},
        ]

        overperformers = agent._find_overperformers(players, threshold=3.0)

        assert len(overperformers) == 2
        # Should be sorted by biggest overperformance
        assert overperformers[0]["player_name"] == "Player1"  # +4.0
        assert overperformers[1]["player_name"] == "Player3"  # +3.5

    def test_find_value_picks(self, agent):
        """Test finding value picks (high xGI, low ownership)."""
        players = [
            {"player_name": "High Owned", "team_short": "MCI", "position": "FWD", "price": 15.0,
             "ownership": 85.0, "xGI_per_90": 1.0, "xG": 10, "xA": 5,
             "goals": 12, "assists": 6, "minutes": 900},
            {"player_name": "Value Pick", "team_short": "BHA", "position": "MID", "price": 6.0,
             "ownership": 5.0, "xGI_per_90": 0.6, "xG": 4, "xA": 3,
             "goals": 3, "assists": 2, "minutes": 900},
            {"player_name": "Low xGI", "team_short": "SHU", "position": "MID", "price": 5.0,
             "ownership": 2.0, "xGI_per_90": 0.1, "xG": 0.5, "xA": 0.3,
             "goals": 0, "assists": 0, "minutes": 900},
        ]

        value_picks = agent._find_value_picks(players)

        assert len(value_picks) == 1
        assert value_picks[0]["player_name"] == "Value Pick"

    def test_get_top_xgi(self, agent):
        """Test getting top xGI players."""
        players = [
            {"player_name": "Player1", "team_short": "ARS", "position": "MID", "price": 10.0,
             "xGI_per_90": 0.8, "xG": 5, "xA": 3, "goals": 5, "assists": 3, "minutes": 900},
            {"player_name": "Player2", "team_short": "MCI", "position": "FWD", "price": 15.0,
             "xGI_per_90": 1.2, "xG": 10, "xA": 5, "goals": 12, "assists": 6, "minutes": 900},
            {"player_name": "Player3", "team_short": "LIV", "position": "MID", "price": 12.0,
             "xGI_per_90": 0.9, "xG": 6, "xA": 4, "goals": 7, "assists": 5, "minutes": 900},
        ]

        top_xgi = agent._get_top_xgi(players, limit=2)

        assert len(top_xgi) == 2
        assert top_xgi[0]["player_name"] == "Player2"  # Highest xGI/90
        assert top_xgi[1]["player_name"] == "Player3"

    def test_calculate_differential_score(self, agent):
        """Test differential score calculation."""
        player = {
            "xGI_per_90": 0.7,
            "form": 6.5,
            "ppg": 5.5,
            "ownership": 3.0,
            "GI_minus_xGI": -2.0,  # Underperforming
            "matchup_score": 7.0,
        }

        score = agent._calculate_differential_score(player)

        assert score > 0
        # Score should be relatively high given good stats and low ownership

    def test_find_differentials(self, agent):
        """Test finding differential players."""
        players = [
            {"id": 1, "player_name": "Template", "team_short": "MCI", "position": "FWD", "price": 15.0,
             "ownership": 85.0, "xGI_per_90": 1.0, "form": 8.0, "ppg": 7.0,
             "total_points": 140, "goals": 20, "assists": 5, "GI_minus_xGI": 2.0,
             "minutes": 1800, "matchup_score": 6.0, "next_opponent": "SHU(H)"},
            {"id": 2, "player_name": "Elite Diff", "team_short": "BHA", "position": "MID", "price": 6.0,
             "ownership": 2.0, "xGI_per_90": 0.6, "form": 6.0, "ppg": 5.0,
             "total_points": 80, "goals": 5, "assists": 5, "GI_minus_xGI": -1.5,
             "minutes": 1500, "matchup_score": 7.0, "next_opponent": "SHU(H)"},
            {"id": 3, "player_name": "Value Diff", "team_short": "NFO", "position": "DEF", "price": 4.5,
             "ownership": 8.0, "xGI_per_90": 0.3, "form": 5.0, "ppg": 4.5,
             "total_points": 70, "goals": 2, "assists": 3, "GI_minus_xGI": 0.5,
             "minutes": 1600, "matchup_score": 5.5, "next_opponent": "LIV(A)"},
        ]

        differentials = agent._find_differentials(players)

        assert "all" in differentials
        assert "by_position" in differentials
        assert "elite" in differentials
        assert "thresholds" in differentials

        # Template player should be excluded
        diff_names = [p["player_name"] for p in differentials["all"]]
        assert "Template" not in diff_names
        assert "Elite Diff" in diff_names
        assert "Value Diff" in diff_names

        # Check tier assignment
        elite_diff = next(p for p in differentials["all"] if p["player_name"] == "Elite Diff")
        assert elite_diff["tier"] == "elite"

        value_diff = next(p for p in differentials["all"] if p["player_name"] == "Value Diff")
        assert value_diff["tier"] == "value"

    def test_calculate_target_score(self, agent):
        """Test target score calculation (no ownership penalty)."""
        player = {
            "xGI_per_90": 0.8,
            "form": 7.0,
            "ppg": 6.0,
            "ownership": 50.0,  # High ownership shouldn't reduce score
            "GI_minus_xGI": -1.5,  # Underperforming bonus
            "matchup_score": 7.0,
        }

        score = agent._calculate_target_score(player)

        assert score > 0

    def test_find_targets(self, agent):
        """Test finding transfer targets."""
        players = [
            {"id": 1, "player_name": "Template", "team_short": "MCI", "position": "FWD", "price": 15.0,
             "ownership": 50.0, "xGI_per_90": 1.0, "form": 8.0, "ppg": 7.0,
             "total_points": 140, "goals": 20, "assists": 5, "GI_minus_xGI": 0.0,
             "minutes": 1800, "matchup_score": 6.0, "next_opponent": "SHU(H)"},
            {"id": 2, "player_name": "Popular", "team_short": "LIV", "position": "MID", "price": 12.0,
             "ownership": 20.0, "xGI_per_90": 0.8, "form": 7.0, "ppg": 6.0,
             "total_points": 100, "goals": 10, "assists": 8, "GI_minus_xGI": -1.0,
             "minutes": 1700, "matchup_score": 7.0, "next_opponent": "BOU(H)"},
            {"id": 3, "player_name": "Differential", "team_short": "BHA", "position": "MID", "price": 6.0,
             "ownership": 5.0, "xGI_per_90": 0.5, "form": 5.0, "ppg": 4.5,
             "total_points": 70, "goals": 4, "assists": 4, "GI_minus_xGI": 0.0,
             "minutes": 1500, "matchup_score": 5.0, "next_opponent": "MCI(A)"},
        ]

        targets = agent._find_targets(players)

        assert "all" in targets
        assert "by_tier" in targets
        assert "by_position" in targets

        # Check tier assignments
        template_target = next(p for p in targets["all"] if p["player_name"] == "Template")
        assert template_target["tier"] == "template"

        popular_target = next(p for p in targets["all"] if p["player_name"] == "Popular")
        assert popular_target["tier"] == "popular"

        diff_target = next(p for p in targets["all"] if p["player_name"] == "Differential")
        assert diff_target["tier"] == "differential"


class TestStatsAgentViewSelection:
    """Tests for StatsAgent view selection."""

    def test_default_views_all(self):
        """Default config computes all views."""
        from fpl_cli.agents.analysis.stats import RECOGNISED_VIEWS

        agent = StatsAgent()
        assert agent.views == RECOGNISED_VIEWS

    def test_custom_views_subset(self):
        """Passing views restricts to that subset."""
        agent = StatsAgent(config={"views": {"differentials"}})
        assert agent.views == frozenset({"differentials"})

    def test_multiple_views(self):
        """Multiple views accepted."""
        views = {"underperformers", "value_picks", "top_xgi_per_90"}
        agent = StatsAgent(config={"views": views})
        assert agent.views == frozenset(views)

    def test_empty_views_defaults_to_all(self):
        """Empty set defaults to all views (same as unset)."""
        from fpl_cli.agents.analysis.stats import RECOGNISED_VIEWS

        agent = StatsAgent(config={"views": set()})
        assert agent.views == RECOGNISED_VIEWS

    def test_invalid_view_raises(self):
        """Unrecognised view name raises ValueError."""
        with pytest.raises(ValueError, match="Unrecognised view"):
            StatsAgent(config={"views": {"invalid_name"}})

    def test_partial_invalid_raises(self):
        """Mix of valid and invalid view names raises ValueError."""
        with pytest.raises(ValueError, match="Unrecognised view"):
            StatsAgent(config={"views": {"differentials", "bogus"}})


class TestScoutAgent:
    """Tests for ScoutAgent."""

    @pytest.fixture
    def agent(self, monkeypatch):
        """Create a scout agent with research provider API key set."""
        monkeypatch.setenv("PERPLEXITY_API_KEY", "test-key")
        return ScoutAgent()

    @pytest.fixture
    def mock_research_response(self):
        """Sample research provider response."""
        content = """## BUY Targets

1. **Mohamed Salah** [1] - Excellent form with 3 goals in last 4 games
2. **Erling Haaland** [2] - Easy fixtures ahead against bottom-half teams

## SELL Recommendations

1. **Marcus Rashford** [3] - Poor underlying stats, overperforming xG

Sources:
1. https://example.com/fpl-analysis
2. https://example.com/fixtures"""
        return LLMResponse(
            content=content,
            model="sonar-pro",
            usage=TokenUsage(input_tokens=150, output_tokens=300),
            citations=["https://example.com/fpl-analysis", "https://example.com/fixtures"],
        )

    def test_agent_initialization(self, agent):
        """Test agent default initialization."""
        assert agent.name == "ScoutAgent"
        assert agent.client is not None

    async def test_run_missing_gameweek(self, agent):
        """Test run fails when gameweek not provided."""
        result = await agent.run(context=None)

        assert result.status == AgentStatus.FAILED
        assert "No gameweek specified" in result.message

    async def test_run_empty_context(self, agent):
        """Test run fails with empty context."""
        result = await agent.run(context={})

        assert result.status == AgentStatus.FAILED
        assert "No gameweek specified" in result.message

    async def test_run_api_not_configured(self, monkeypatch):
        """Test run fails when API key not configured."""
        monkeypatch.setenv("PERPLEXITY_API_KEY", "test-key")
        agent = ScoutAgent()
        agent.research_provider.api_key = None

        result = await agent.run(context={"gameweek": 25})

        assert result.status == AgentStatus.FAILED
        assert "not configured" in result.message
        assert len(result.errors) > 0
        assert "PERPLEXITY_API_KEY" in result.errors[0]

    async def test_run_success(self, agent, mock_research_response):
        """Test successful scout analysis run."""
        with patch.object(agent.research_provider, "query", new_callable=AsyncMock) as mock_query:
            mock_query.return_value = mock_research_response

            result = await agent.run(context={"gameweek": 25})

            assert result.status == AgentStatus.SUCCESS
            assert result.data["gameweek"] == 25
            assert "content_referenced" in result.data
            assert "content_clean" in result.data
            assert "citations" in result.data
            assert "model" in result.data

            # Verify content_referenced contains citations
            assert "[1]" in result.data["content_referenced"]
            assert "## BUY Targets" in result.data["content_referenced"]

            # Verify content_clean has citations removed
            assert "[1]" not in result.data["content_clean"]
            assert "Sources:" not in result.data["content_clean"]

            # Verify the query was called with correct prompts
            mock_query.assert_called_once()
            call_kwargs = mock_query.call_args[1]
            assert "Gameweek 25" in call_kwargs["prompt"]
            assert call_kwargs["system_prompt"] is not None

    async def test_run_empty_response(self, agent):
        """Test run handles empty API response."""
        with patch.object(agent.research_provider, "query", new_callable=AsyncMock) as mock_query:
            mock_query.return_value = LLMResponse(
                content="", model="sonar-pro",
                usage=TokenUsage(0, 0), citations=[],
            )

            result = await agent.run(context={"gameweek": 25})

            assert result.status == AgentStatus.FAILED
            assert "Empty response" in result.message

    async def test_run_api_error(self, agent):
        """Test run handles API errors gracefully."""
        with patch.object(agent.research_provider, "query", new_callable=AsyncMock) as mock_query:
            mock_query.side_effect = Exception("API rate limit exceeded")

            result = await agent.run(context={"gameweek": 25})

            assert result.status == AgentStatus.FAILED
            assert "Failed to fetch" in result.message
            assert len(result.errors) > 0
            assert "rate limit" in result.errors[0]

    async def test_run_verifies_prompt_content(self, agent, mock_research_response):
        """Test that the agent sends appropriate prompts."""
        with patch.object(agent.research_provider, "query", new_callable=AsyncMock) as mock_query:
            mock_query.return_value = mock_research_response

            await agent.run(context={"gameweek": 30})

            call_kwargs = mock_query.call_args[1]
            prompt = call_kwargs["prompt"]
            system_prompt = call_kwargs["system_prompt"]

            # Verify prompt contains expected elements
            assert "Gameweek 30" in prompt
            assert "BUY Signals" in prompt
            assert "SELL Signals" in prompt
            assert "research_focus" in prompt
            assert "output_format" in prompt

            # Verify system prompt sets up the expert context
            assert "fpl" in system_prompt.lower()
            assert "ALWAYS:" in system_prompt
            assert "NEVER:" in system_prompt

    async def test_run_data_structure(self, agent, mock_research_response):
        """Test the structure of returned data."""
        with patch.object(agent.research_provider, "query", new_callable=AsyncMock) as mock_query:
            mock_query.return_value = mock_research_response

            result = await agent.run(context={"gameweek": 25})

            # Verify all expected data fields are present
            assert "gameweek" in result.data
            assert "content_referenced" in result.data
            assert "content_clean" in result.data
            assert "citations" in result.data
            assert "model" in result.data
            assert "usage" in result.data

            # Verify types
            assert isinstance(result.data["gameweek"], int)
            assert isinstance(result.data["content_referenced"], str)
            assert isinstance(result.data["content_clean"], str)
            assert isinstance(result.data["citations"], list)

    async def test_run_cleans_citations_correctly(self, agent):
        """Test that citations are properly cleaned from content."""
        response_with_citations = LLMResponse(
            content="Player A [1] is great [2]. Player B [3][4] is also good.\n\nSources:\n1. https://a.com",
            model="sonar-pro",
            usage=TokenUsage(0, 0),
            citations=["https://a.com", "https://b.com"],
        )

        with patch.object(agent.research_provider, "query", new_callable=AsyncMock) as mock_query:
            mock_query.return_value = response_with_citations

            result = await agent.run(context={"gameweek": 25})

            # Referenced version should have citations
            assert "[1]" in result.data["content_referenced"]
            assert "[2]" in result.data["content_referenced"]

            # Clean version should not have citations
            assert "[1]" not in result.data["content_clean"]
            assert "[2]" not in result.data["content_clean"]
            assert "[3]" not in result.data["content_clean"]
            assert "Sources:" not in result.data["content_clean"]
