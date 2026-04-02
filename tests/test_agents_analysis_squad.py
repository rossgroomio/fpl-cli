"""Tests for analysis agents (SquadAnalyzerAgent, BenchOrderAgent)."""

from unittest.mock import AsyncMock, patch

import pytest

from fpl_cli.agents.analysis.bench_order import BenchOrderAgent
from fpl_cli.agents.analysis.squad_analyzer import SquadAnalyzerAgent
from fpl_cli.agents.base import AgentStatus
from fpl_cli.models.player import PlayerPosition, PlayerStatus
from fpl_cli.services.player_scoring import ScoringContext
from fpl_cli.services.team_ratings import TeamRatingsService
from tests.conftest import make_fixture, make_player, make_team


def _bench_context(team_map, team_fixtures=None):
    """Build a minimal ScoringContext for bench scoring tests."""
    svc = TeamRatingsService.__new__(TeamRatingsService)
    svc._ratings = {}
    svc._loaded = True
    svc._metadata = None
    return ScoringContext(
        team_map=team_map,
        team_fixture_map=team_fixtures or {},
        ratings_service=svc,
    )

# --- Fixtures ---

@pytest.fixture
def mock_players():
    """Create mock players for team analysis."""
    return [
        make_player(
            id=1, web_name="GK1", team_id=1, position=PlayerPosition.GOALKEEPER,
            now_cost=55, form=5.0, total_points=80, status=PlayerStatus.AVAILABLE,
        ),
        make_player(
            id=2, web_name="DEF1", team_id=1, position=PlayerPosition.DEFENDER,
            now_cost=60, form=6.0, total_points=90, status=PlayerStatus.AVAILABLE,
        ),
        make_player(
            id=3, web_name="DEF2", team_id=2, position=PlayerPosition.DEFENDER,
            now_cost=55, form=4.0, total_points=70, status=PlayerStatus.AVAILABLE,
        ),
        make_player(
            id=4, web_name="DEF3", team_id=3, position=PlayerPosition.DEFENDER,
            now_cost=45, form=3.0, total_points=50, status=PlayerStatus.DOUBTFUL,
            chance_of_playing_next_round=50, news="Knock",
        ),
        make_player(
            id=5, web_name="MID1", team_id=1, position=PlayerPosition.MIDFIELDER,
            now_cost=100, form=8.0, total_points=150, status=PlayerStatus.AVAILABLE,
        ),
        make_player(
            id=6, web_name="MID2", team_id=2, position=PlayerPosition.MIDFIELDER,
            now_cost=80, form=6.0, total_points=100, status=PlayerStatus.AVAILABLE,
        ),
        make_player(
            id=7, web_name="MID3", team_id=4, position=PlayerPosition.MIDFIELDER,
            now_cost=70, form=2.0, total_points=60, status=PlayerStatus.AVAILABLE,
        ),
        make_player(
            id=8, web_name="FWD1", team_id=5, position=PlayerPosition.FORWARD,
            now_cost=120, form=9.0, total_points=180, status=PlayerStatus.AVAILABLE,
        ),
        make_player(
            id=9, web_name="FWD2", team_id=6, position=PlayerPosition.FORWARD,
            now_cost=75, form=5.0, total_points=80, status=PlayerStatus.AVAILABLE,
        ),
        # Bench players
        make_player(
            id=10, web_name="BenchGK", team_id=7, position=PlayerPosition.GOALKEEPER,
            now_cost=40, form=3.0, total_points=30, status=PlayerStatus.AVAILABLE,
        ),
        make_player(
            id=11, web_name="BenchDEF", team_id=8, position=PlayerPosition.DEFENDER,
            now_cost=40, form=4.0, total_points=40, status=PlayerStatus.AVAILABLE,
        ),
    ]


@pytest.fixture
def mock_teams():
    """Create mock teams."""
    return [
        make_team(id=1, name="Arsenal", short_name="ARS"),
        make_team(id=2, name="Man City", short_name="MCI"),
        make_team(id=3, name="Liverpool", short_name="LIV"),
        make_team(id=4, name="Chelsea", short_name="CHE"),
        make_team(id=5, name="Tottenham", short_name="TOT"),
        make_team(id=6, name="Newcastle", short_name="NEW"),
        make_team(id=7, name="Brighton", short_name="BHA"),
        make_team(id=8, name="West Ham", short_name="WHU"),
    ]


@pytest.fixture
def mock_fixtures():
    """Create mock fixtures for next gameweek."""
    return [
        make_fixture(id=1, gameweek=25, home_team_id=1, away_team_id=8, home_difficulty=2, away_difficulty=4),
        make_fixture(id=2, gameweek=25, home_team_id=2, away_team_id=7, home_difficulty=2, away_difficulty=5),
        make_fixture(id=3, gameweek=25, home_team_id=3, away_team_id=6, home_difficulty=3, away_difficulty=4),
        make_fixture(id=4, gameweek=25, home_team_id=4, away_team_id=5, home_difficulty=3, away_difficulty=3),
    ]


# ==============================================================================
# SQUAD ANALYZER AGENT TESTS
# ==============================================================================

class TestSquadAnalyzerAgentInit:
    """Tests for SquadAnalyzerAgent initialization."""

    def test_agent_initialization(self):
        """Test default initialization."""
        agent = SquadAnalyzerAgent()
        assert agent.name == "SquadAnalyzerAgent"
        assert agent.entry_id is None

    def test_agent_initialization_with_config(self):
        """Test initialization with config."""
        config = {"entry_id": 12345}
        agent = SquadAnalyzerAgent(config=config)
        assert agent.entry_id == 12345


class TestSquadAnalyzerAgentRun:
    """Tests for SquadAnalyzerAgent run method."""

    @pytest.mark.asyncio
    async def test_run_missing_entry_id_and_picks(self):
        """Test run fails without entry_id or picks."""
        agent = SquadAnalyzerAgent()
        result = await agent.run()

        assert result.status == AgentStatus.FAILED
        assert "No entry_id or picks" in result.message

    @pytest.mark.asyncio
    async def test_run_success_with_picks(self, mock_players, mock_teams):
        """Test successful analysis with picks in context."""
        agent = SquadAnalyzerAgent()
        picks = [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11]  # Player IDs

        with patch.object(agent.client, "get_players", new_callable=AsyncMock) as mock_get_players, \
             patch.object(agent.client, "get_teams", new_callable=AsyncMock) as mock_get_teams:

            mock_get_players.return_value = mock_players
            mock_get_teams.return_value = mock_teams

            result = await agent.run(context={"picks": picks})

            assert result.status == AgentStatus.SUCCESS
            assert "squad_overview" in result.data
            assert "position_analysis" in result.data
            assert "injury_risks" in result.data
            assert "recommendations" in result.data

    @pytest.mark.asyncio
    async def test_run_handles_api_error(self):
        """Test run handles API errors gracefully."""
        agent = SquadAnalyzerAgent()

        with patch.object(agent.client, "get_players", new_callable=AsyncMock) as mock_get_players:
            mock_get_players.side_effect = Exception("API Error")

            result = await agent.run(context={"picks": [1, 2, 3]})

            assert result.status == AgentStatus.FAILED
            assert "API Error" in result.errors[0]


class TestSquadAnalyzerAgentAnalysis:
    """Tests for analysis methods."""

    def test_analyze_squad_overview(self, mock_players, mock_teams):
        """Test _analyze_squad_overview returns correct data for classic format."""
        agent = SquadAnalyzerAgent()
        team_map = {t.id: t for t in mock_teams}
        team_players = mock_players[:9]

        result = agent._analyze_squad_overview(team_players, team_map, fmt="classic")

        assert "total_points" in result
        assert "team_value" in result
        assert "average_form" in result
        assert "position_counts" in result
        assert "team_coverage" in result

    def test_analyze_squad_overview_with_manager_entry(self, mock_players, mock_teams):
        """Test _analyze_squad_overview with manager entry data."""
        agent = SquadAnalyzerAgent()
        team_map = {t.id: t for t in mock_teams}
        team_players = mock_players[:9]
        manager_entry = {
            "summary_overall_points": 1500,
            "last_deadline_value": 1000,
            "last_deadline_bank": 50,
        }

        result = agent._analyze_squad_overview(team_players, team_map, manager_entry, fmt="classic")

        assert result["total_points"] == 1500
        assert result["team_value"] == 100.0
        assert result["bank"] == 5.0

    def test_analyze_squad_overview_draft_omits_value(self, mock_players, mock_teams):
        """Test _analyze_squad_overview omits value/bank for draft format."""
        agent = SquadAnalyzerAgent()
        team_map = {t.id: t for t in mock_teams}
        team_players = mock_players[:9]

        result = agent._analyze_squad_overview(team_players, team_map, fmt="draft")

        assert "total_points" in result
        assert "average_form" in result
        assert "team_value" not in result
        assert "bank" not in result

    def test_analyze_positions(self, mock_players, mock_teams):
        """Test _analyze_positions groups by position."""
        agent = SquadAnalyzerAgent()
        team_map = {t.id: t for t in mock_teams}
        team_players = mock_players[:9]

        result = agent._analyze_positions(team_players, team_map)

        assert "GK" in result
        assert "DEF" in result
        assert "MID" in result
        assert "FWD" in result
        assert result["GK"]["count"] == 1
        assert result["DEF"]["count"] == 3
        assert result["MID"]["count"] == 3
        assert result["FWD"]["count"] == 2

    def test_analyze_positions_sorted_by_form(self, mock_players, mock_teams):
        """Test players in each position are sorted by form."""
        agent = SquadAnalyzerAgent()
        team_map = {t.id: t for t in mock_teams}
        team_players = mock_players[:9]

        result = agent._analyze_positions(team_players, team_map)

        # Check DEF position is sorted by form descending
        def_players = result["DEF"]["players"]
        forms = [p["form"] for p in def_players]
        assert forms == sorted(forms, reverse=True)

    def test_analyze_injury_risks(self, mock_players, mock_teams):
        """Test _analyze_injury_risks identifies risky players."""
        agent = SquadAnalyzerAgent()
        team_map = {t.id: t for t in mock_teams}
        team_players = mock_players[:9]  # Includes DEF3 with doubtful status

        result = agent._analyze_injury_risks(team_players, team_map)

        assert len(result) == 1  # DEF3 is doubtful
        assert result[0]["name"] == "DEF3"
        assert result[0]["status"] == "d"
        assert result[0]["chance_of_playing"] == 50

    def test_analyze_injury_risks_excludes_available(self, mock_teams):
        """Test available players are not flagged."""
        agent = SquadAnalyzerAgent()
        team_map = {t.id: t for t in mock_teams}
        available_players = [
            make_player(id=1, team_id=1, status=PlayerStatus.AVAILABLE),
            make_player(id=2, team_id=2, status=PlayerStatus.AVAILABLE),
        ]

        result = agent._analyze_injury_risks(available_players, team_map)

        assert len(result) == 0

    def test_analyze_form(self, mock_players, mock_teams):
        """Test _analyze_form identifies in-form and out-of-form players."""
        agent = SquadAnalyzerAgent()
        team_map = {t.id: t for t in mock_teams}
        team_players = mock_players[:9]

        result = agent._analyze_form(team_players, team_map)

        assert "in_form" in result
        assert "out_of_form" in result
        assert len(result["in_form"]) <= 5
        assert len(result["out_of_form"]) <= 5
        # First in_form should have highest form
        assert result["in_form"][0]["form"] >= result["in_form"][-1]["form"]

class TestSquadAnalyzerAgentRecommendations:
    """Tests for recommendation generation."""

    def test_generate_recommendations_injury_risk(self, mock_players, mock_teams):
        """Test recommendations include injury concerns."""
        agent = SquadAnalyzerAgent()
        team_map = {t.id: t for t in mock_teams}
        team_players = mock_players[:9]
        position_analysis = agent._analyze_positions(team_players, team_map)
        injury_risks = [
            {"name": "DEF3", "team": "LIV", "chance_of_playing": 25, "status": "d"},
        ]

        recommendations = agent._generate_recommendations(
            team_players, position_analysis, injury_risks
        )

        injury_recs = [r for r in recommendations if r["type"] == "injury_risk"]
        assert len(injury_recs) > 0

    def test_generate_recommendations_weak_position(self, mock_teams):
        """Test recommendations include weak position concerns."""
        agent = SquadAnalyzerAgent()

        # Create players with low form MID
        players = [
            make_player(id=1, team_id=1, position=PlayerPosition.MIDFIELDER, form=2.0),
            make_player(id=2, team_id=2, position=PlayerPosition.MIDFIELDER, form=2.5),
        ]
        position_analysis = {"MID": {"average_form": 2.25, "count": 2, "players": []}}

        recommendations = agent._generate_recommendations(
            players, position_analysis, []
        )

        weak_pos_recs = [r for r in recommendations if r["type"] == "weak_position"]
        assert len(weak_pos_recs) > 0

    def test_generate_recommendations_premium_underperforming(self, mock_teams):
        """Test recommendations flag underperforming premiums."""
        agent = SquadAnalyzerAgent()

        # Create expensive player with low form
        players = [
            make_player(id=1, team_id=1, position=PlayerPosition.FORWARD, now_cost=120, form=2.0),
        ]
        position_analysis = {"FWD": {"average_form": 2.0, "count": 1, "players": []}}

        recommendations = agent._generate_recommendations(
            players, position_analysis, []
        )

        premium_recs = [r for r in recommendations if r["type"] == "premium_underperforming"]
        assert len(premium_recs) > 0

    def test_generate_recommendations_sorted_by_priority(self, mock_players, mock_teams):
        """Test recommendations are sorted by priority."""
        agent = SquadAnalyzerAgent()
        team_map = {t.id: t for t in mock_teams}
        team_players = mock_players[:9]
        position_analysis = agent._analyze_positions(team_players, team_map)
        injury_risks = [
            {"name": "DEF3", "team": "LIV", "chance_of_playing": 25, "status": "d"},
        ]

        recommendations = agent._generate_recommendations(
            team_players, position_analysis, injury_risks
        )

        # High priority should come before medium
        priorities = [r["priority"] for r in recommendations]
        for i in range(len(priorities) - 1):
            if priorities[i] == "medium":
                assert priorities[i + 1] != "high"

    def test_generate_recommendations_limits_to_10(self):
        """Test recommendations are limited to 10."""
        agent = SquadAnalyzerAgent()

        # Create many underperforming premiums
        players = [
            make_player(id=i, team_id=1, position=PlayerPosition.FORWARD, now_cost=120, form=2.0)
            for i in range(15)
        ]
        position_analysis = {"FWD": {"average_form": 2.0, "count": 15, "players": []}}

        recommendations = agent._generate_recommendations(
            players, position_analysis, []
        )

        assert len(recommendations) <= 10

    def test_generate_recommendations_team_limit_warning(self):
        """Test recommendations flag teams at 3-player limit."""
        agent = SquadAnalyzerAgent()

        # Create squad with 3 Arsenal players
        players = [
            make_player(id=1, team_id=1, position=PlayerPosition.DEFENDER, form=5.0),
            make_player(id=2, team_id=1, position=PlayerPosition.MIDFIELDER, form=6.0),
            make_player(id=3, team_id=1, position=PlayerPosition.FORWARD, form=7.0),
            make_player(id=4, team_id=2, position=PlayerPosition.DEFENDER, form=5.0),
        ]
        position_analysis = {}
        squad_overview = {
            "team_coverage": {"ARS": 3, "MCI": 1},
            "players_by_team": {"ARS": ["DEF1", "MID1", "FWD1"], "MCI": ["DEF2"]},
        }

        recommendations = agent._generate_recommendations(
            players, position_analysis, [],
            squad_overview=squad_overview,
        )

        team_limit_recs = [r for r in recommendations if r["type"] == "team_limit"]
        assert len(team_limit_recs) == 1
        assert "ARS" in team_limit_recs[0]["message"]
        assert "3" in team_limit_recs[0]["message"]

    def test_generate_recommendations_draft_skips_price_and_team_limit(self):
        """Test draft format skips price-based and team-limit recs."""
        agent = SquadAnalyzerAgent()

        players = [
            make_player(id=1, team_id=1, position=PlayerPosition.FORWARD, now_cost=120, form=2.0),
        ]
        position_analysis = {"FWD": {"average_form": 2.0, "count": 1, "players": []}}
        squad_overview = {
            "team_coverage": {"ARS": 3},
            "players_by_team": {"ARS": ["P1", "P2", "P3"]},
        }

        recommendations = agent._generate_recommendations(
            players, position_analysis, [],
            squad_overview=squad_overview, fmt="draft",
        )

        assert not any(r["type"] == "premium_underperforming" for r in recommendations)
        assert not any(r["type"] == "team_limit" for r in recommendations)

    def test_generate_recommendations_no_warning_under_limit(self):
        """Test no team limit warning when under 3 players."""
        agent = SquadAnalyzerAgent()

        players = [
            make_player(id=1, team_id=1, position=PlayerPosition.DEFENDER, form=5.0),
            make_player(id=2, team_id=1, position=PlayerPosition.MIDFIELDER, form=6.0),
            make_player(id=3, team_id=2, position=PlayerPosition.FORWARD, form=7.0),
        ]
        position_analysis = {}
        squad_overview = {
            "team_coverage": {"ARS": 2, "MCI": 1},
            "players_by_team": {"ARS": ["DEF1", "MID1"], "MCI": ["FWD1"]},
        }

        recommendations = agent._generate_recommendations(
            players, position_analysis, [],
            squad_overview=squad_overview,
        )

        team_limit_recs = [r for r in recommendations if r["type"] == "team_limit"]
        assert len(team_limit_recs) == 0

    def test_mid_price_underperformer_flagged(self):
        """Mid-price player with poor value_season is flagged."""
        agent = SquadAnalyzerAgent()
        players = [make_player(
            id=1, web_name="Flop", now_cost=65, minutes=900,
            form=4.0, value_season=1.5, position=PlayerPosition.MIDFIELDER,
        )]
        recs = agent._generate_recommendations(
            players, {}, [], {"team_coverage": {}, "players_by_team": {}}, "classic",
        )
        mid_price_recs = [r for r in recs if r["type"] == "mid_price_underperforming"]
        assert len(mid_price_recs) == 1
        assert mid_price_recs[0]["priority"] == "low"
        assert "Flop" in mid_price_recs[0]["message"]
        assert "1.5" in mid_price_recs[0]["message"]

    def test_mid_price_low_minutes_not_flagged(self):
        """Player with < 450 minutes is not flagged as mid-price underperformer."""
        agent = SquadAnalyzerAgent()
        players = [make_player(
            id=2, web_name="NewSign", now_cost=60, minutes=200,
            form=2.0, value_season=0.5, position=PlayerPosition.FORWARD,
        )]
        recs = agent._generate_recommendations(
            players, {}, [], {"team_coverage": {}, "players_by_team": {}}, "classic",
        )
        mid_price_recs = [r for r in recs if r["type"] == "mid_price_underperforming"]
        assert len(mid_price_recs) == 0

    def test_mid_price_draft_format_excluded(self):
        """Draft format does not flag mid-price underperformers."""
        agent = SquadAnalyzerAgent()
        players = [make_player(
            id=3, web_name="Flop", now_cost=65, minutes=900,
            form=4.0, value_season=1.5, position=PlayerPosition.MIDFIELDER,
        )]
        recs = agent._generate_recommendations(
            players, {}, [], {"team_coverage": {}, "players_by_team": {}}, "draft",
        )
        mid_price_recs = [r for r in recs if r["type"] == "mid_price_underperforming"]
        assert len(mid_price_recs) == 0

    def test_budget_player_not_flagged(self):
        """Player below £5.0m is not flagged as mid-price underperformer."""
        agent = SquadAnalyzerAgent()
        players = [make_player(
            id=4, web_name="Cheap", now_cost=45, minutes=900,
            form=2.0, value_season=1.0, position=PlayerPosition.DEFENDER,
        )]
        recs = agent._generate_recommendations(
            players, {}, [], {"team_coverage": {}, "players_by_team": {}}, "classic",
        )
        mid_price_recs = [r for r in recs if r["type"] == "mid_price_underperforming"]
        assert len(mid_price_recs) == 0

    def test_premium_player_not_flagged_as_mid_price(self):
        """Player at £8.0m is caught by premium check, not mid-price."""
        agent = SquadAnalyzerAgent()
        players = [make_player(
            id=5, web_name="Premium", now_cost=80, minutes=900,
            form=2.0, value_season=1.0, position=PlayerPosition.MIDFIELDER,
        )]
        recs = agent._generate_recommendations(
            players, {}, [], {"team_coverage": {}, "players_by_team": {}}, "classic",
        )
        mid_price_recs = [r for r in recs if r["type"] == "mid_price_underperforming"]
        premium_recs = [r for r in recs if r["type"] == "premium_underperforming"]
        assert len(mid_price_recs) == 0
        assert len(premium_recs) == 1


# ==============================================================================
# BENCH ORDER AGENT TESTS
# ==============================================================================

class TestBenchOrderAgentInit:
    """Tests for BenchOrderAgent initialization."""

    def test_agent_initialization(self):
        """Test default initialization."""
        agent = BenchOrderAgent()
        assert agent.name == "BenchOrderAgent"


class TestBenchOrderAgentRun:
    """Tests for BenchOrderAgent run method."""

    @pytest.mark.asyncio
    async def test_run_missing_bench(self):
        """Test run fails without bench players."""
        agent = BenchOrderAgent()
        result = await agent.run()

        assert result.status == AgentStatus.FAILED
        assert "No bench players" in result.message

    @pytest.mark.asyncio
    async def test_run_success(self, mock_players, mock_teams, mock_fixtures):
        """Test successful bench order optimization."""
        agent = BenchOrderAgent()
        bench_ids = [10, 11]  # BenchGK, BenchDEF
        starting_ids = [1, 2, 3, 4, 5, 6, 7, 8, 9]

        with patch.object(agent.client, "get_players", new_callable=AsyncMock) as mock_get_players, \
             patch.object(agent.client, "get_teams", new_callable=AsyncMock) as mock_get_teams, \
             patch.object(agent.client, "get_next_gameweek", new_callable=AsyncMock) as mock_next_gw, \
             patch.object(agent.client, "get_fixtures", new_callable=AsyncMock) as mock_get_fixtures:

            mock_get_players.return_value = mock_players
            mock_get_teams.return_value = mock_teams
            mock_next_gw.return_value = {"id": 25}
            mock_get_fixtures.return_value = mock_fixtures

            result = await agent.run(context={"bench": bench_ids, "starting_xi": starting_ids})

            assert result.status == AgentStatus.SUCCESS
            assert "optimal_order" in result.data
            assert "availability_risks" in result.data
            assert "formation_context" in result.data
            assert "warnings" in result.data
            assert "current_order" not in result.data
            assert "order_changed" not in result.data


class TestBenchOrderAgentScoring:
    """Tests for bench scoring methods."""

    @pytest.fixture()
    def agent(self):
        return BenchOrderAgent()

    @pytest.fixture()
    def team_map(self, mock_teams):
        return {t.id: t for t in mock_teams}

    def test_analyze_availability_risk(self, mock_players, mock_teams):
        """Test _analyze_availability_risk identifies risky starters."""
        agent = BenchOrderAgent()
        team_map = {t.id: t for t in mock_teams}
        starters = [mock_players[3]]  # DEF3 with 50% chance

        risks = agent._analyze_availability_risk(starters, team_map)

        assert len(risks) == 1
        assert risks[0]["name"] == "DEF3"
        assert risks[0]["risk_level"] >= 1

    def test_analyze_availability_risk_by_chance(self, mock_teams):
        """Test availability risk levels by chance of playing."""
        agent = BenchOrderAgent()
        team_map = {t.id: t for t in mock_teams}

        # 25% chance = risk level 3
        player_25 = make_player(id=1, team_id=1, status=PlayerStatus.DOUBTFUL, chance_of_playing_next_round=25)
        risks = agent._analyze_availability_risk([player_25], team_map)
        assert risks[0]["risk_level"] == 3

        # 50% chance = risk level 2
        player_50 = make_player(id=2, team_id=1, status=PlayerStatus.DOUBTFUL, chance_of_playing_next_round=50)
        risks = agent._analyze_availability_risk([player_50], team_map)
        assert risks[0]["risk_level"] == 2

        # 75% chance = risk level 1
        player_75 = make_player(id=3, team_id=1, status=PlayerStatus.DOUBTFUL, chance_of_playing_next_round=75)
        risks = agent._analyze_availability_risk([player_75], team_map)
        assert risks[0]["risk_level"] == 1

    def test_score_bench_player_ppg(self, mock_players, mock_teams, mock_fixtures):
        """Test PPG contributes to bench priority score."""
        agent = BenchOrderAgent()
        team_map = {t.id: t for t in mock_teams}
        team_fixtures = {f.home_team_id: [{"fixture": f, "is_home": True}] for f in mock_fixtures}

        player = mock_players[10]  # BenchGK
        score_data = agent._score_bench_player(player, _bench_context(team_map, team_fixtures), [], next_gw_id=20)

        assert "priority_score" in score_data
        assert "reasons" in score_data

    def test_score_bench_player_form(self, mock_teams, mock_fixtures):
        """Test form contributes to bench priority score."""
        agent = BenchOrderAgent()
        team_map = {t.id: t for t in mock_teams}
        team_fixtures = {1: [{"fixture": mock_fixtures[0], "is_home": True}]}

        # High form player (needs fixture for core to produce non-zero score)
        high_form = make_player(id=1, team_id=1, form=7.0, minutes=900)
        score_high = agent._score_bench_player(high_form, _bench_context(team_map, team_fixtures), [], next_gw_id=20)

        # Low form player
        low_form = make_player(id=2, team_id=1, form=2.0, minutes=900)
        score_low = agent._score_bench_player(low_form, _bench_context(team_map, team_fixtures), [], next_gw_id=20)

        assert score_high["priority_score"] > score_low["priority_score"]

    def test_score_bench_player_matchup_bonus(self, mock_teams, mock_fixtures):
        """Test fixtures affect priority score via matchup contribution in core."""
        agent = BenchOrderAgent()
        team_map = {t.id: t for t in mock_teams}
        player = make_player(id=1, team_id=1, form=5.0, minutes=900)

        # With fixture: matchup contributes to core score
        fixture_ctx = _bench_context(team_map, {1: [{"fixture": mock_fixtures[0], "is_home": True}]})
        score_with = agent._score_bench_player(player, fixture_ctx, [], next_gw_id=20)

        # Without fixture: core returns 0.0
        no_fixture_ctx = _bench_context(team_map)
        score_without = agent._score_bench_player(player, no_fixture_ctx, [], next_gw_id=20)

        assert score_with["priority_score"] > score_without["priority_score"]

    def test_score_bench_player_no_fixture_no_matchup_bonus(self, mock_teams):
        """Player with no fixtures gets no matchup bonus."""
        agent = BenchOrderAgent()
        team_map = {t.id: t for t in mock_teams}
        player = make_player(id=1, team_id=1, form=5.0, minutes=900)

        score_no_fix = agent._score_bench_player(player, _bench_context(team_map), [], next_gw_id=20)
        # No fixtures -> no matchup bonus, no matchup reasons
        assert not any("matchup" in r.lower() for r in score_no_fix["reasons"])

    def test_score_bench_player_dgw(self, mock_teams, mock_fixtures):
        """Test double gameweek boosts priority score (implicit via matchup sum + xGI scaling)."""
        agent = BenchOrderAgent()
        team_map = {t.id: t for t in mock_teams}

        player = make_player(id=1, team_id=1, form=5.0, minutes=900)

        # SGW: one fixture
        sgw_fixtures = {1: [{"fixture": mock_fixtures[0], "is_home": True}]}
        sgw_data = agent._score_bench_player(player, _bench_context(team_map, sgw_fixtures), [], next_gw_id=20)

        # DGW: two fixtures
        dgw_fixtures = {1: [{"fixture": mock_fixtures[0], "is_home": True}, {"fixture": mock_fixtures[1], "is_home": False}]}
        dgw_data = agent._score_bench_player(player, _bench_context(team_map, dgw_fixtures), [], next_gw_id=20)

        assert dgw_data["priority_score"] > sgw_data["priority_score"]

    def test_score_bench_player_covers_risky_starter(self, mock_teams):
        """Test covering risky starter boosts priority score."""
        agent = BenchOrderAgent()
        team_map = {t.id: t for t in mock_teams}

        rotation_risks = [
            {"position": "DEF", "risk_level": 2, "name": "RiskyDEF"},
        ]
        player = make_player(id=1, team_id=1, position=PlayerPosition.DEFENDER, form=5.0, minutes=900)

        score_data = agent._score_bench_player(player, _bench_context(team_map), rotation_risks, next_gw_id=20)

        assert any("Covers risky starter" in r for r in score_data["reasons"])

    def test_score_bench_player_availability_doubt(self, mock_teams):
        """Test availability doubt reduces priority score."""
        agent = BenchOrderAgent()
        team_map = {t.id: t for t in mock_teams}

        player = make_player(
            id=1, team_id=1, form=5.0, minutes=900,
            status=PlayerStatus.DOUBTFUL, chance_of_playing_next_round=25,
        )

        score_data = agent._score_bench_player(player, _bench_context(team_map), [], next_gw_id=20)

        assert any("Doubt" in r for r in score_data["reasons"])

    def test_goalkeeper_always_last(self, mock_teams):
        """Test GK is always last in bench order."""
        agent = BenchOrderAgent()
        team_map = {t.id: t for t in mock_teams}

        # Create bench with GK having high score
        bench = [
            make_player(id=1, team_id=1, position=PlayerPosition.GOALKEEPER, form=9.0, minutes=1800),
            make_player(id=2, team_id=2, position=PlayerPosition.DEFENDER, form=2.0, minutes=500),
            make_player(id=3, team_id=3, position=PlayerPosition.MIDFIELDER, form=3.0, minutes=600),
        ]

        # Score each
        scored = []
        for p in bench:
            scored.append(agent._score_bench_player(p, _bench_context(team_map), [], next_gw_id=20))

        # Split by position
        outfield = [p for p in scored if p["position"] != "GK"]
        goalkeepers = [p for p in scored if p["position"] == "GK"]

        outfield.sort(key=lambda x: x["priority_score_raw"], reverse=True)
        optimal = outfield[:3] + goalkeepers

        # GK should be last even though they have highest form
        assert optimal[-1]["position"] == "GK"

    def test_primary_penalty_taker_bonus(self, agent, team_map):
        """Primary penalty taker gets +0.5 bonus."""
        player = make_player(
            id=20, web_name="PenTaker", team_id=1,
            position=PlayerPosition.MIDFIELDER,
            now_cost=80, minutes=1000, form=4.0, points_per_game=4.0,
            penalties_order=1,
        )
        result = agent._score_bench_player(player, _bench_context(team_map), [], next_gw_id=20)
        assert result["priority_score"] >= 0.5
        assert "Primary penalty taker" in result["reasons"]

    def test_corner_taker_bonus(self, agent, team_map):
        """Corner/FK taker gets +0.25 bonus."""
        player = make_player(
            id=21, web_name="CornerKing", team_id=1,
            position=PlayerPosition.MIDFIELDER,
            now_cost=70, minutes=1000, form=4.0, points_per_game=4.0,
            corners_and_indirect_freekicks_order=1,
        )
        result = agent._score_bench_player(player, _bench_context(team_map), [], next_gw_id=20)
        assert result["priority_score"] >= 0.25
        assert "Set-piece taker" in result["reasons"]

    def test_no_set_piece_duty_no_bonus(self, agent, team_map):
        """Player without set-piece duties gets no set-piece bonus."""
        player = make_player(
            id=22, web_name="NoDuty", team_id=1,
            position=PlayerPosition.MIDFIELDER,
            now_cost=70, minutes=1000, form=4.0, points_per_game=4.0,
        )
        result = agent._score_bench_player(player, _bench_context(team_map), [], next_gw_id=20)
        assert "Primary penalty taker" not in result["reasons"]
        assert "Set-piece taker" not in result["reasons"]

    def test_pen_taker_beats_non_pen_at_same_base(self, agent, team_map):
        """Primary pen taker scores higher than identical player without duty."""
        kwargs = dict(
            team_id=1, position=PlayerPosition.MIDFIELDER,
            now_cost=70, minutes=1000, form=4.0, points_per_game=4.0,
        )
        pen_taker = make_player(id=23, web_name="PenA", penalties_order=1, **kwargs)
        no_duty = make_player(id=24, web_name="NoA", **kwargs)

        pen_result = agent._score_bench_player(pen_taker, _bench_context(team_map), [], next_gw_id=20)
        no_result = agent._score_bench_player(no_duty, _bench_context(team_map), [], next_gw_id=20)
        assert pen_result["priority_score_raw"] > no_result["priority_score_raw"]


def _make_squad(
    formation: tuple[int, int, int],
    bench_positions: list[PlayerPosition],
    bench_overrides: dict[int, dict] | None = None,
) -> tuple[list, list]:
    """Build XI + bench from formation tuple (DEF, MID, FWD).

    Args:
        formation: (DEF count, MID count, FWD count) for the starting XI.
        bench_positions: Outfield positions for bench slots 2-4.
        bench_overrides: Optional {bench_index: kwargs} to override make_player defaults
            for specific bench players (index 0 = GK, 1-3 = outfield).
    """
    pid = 1
    xi = [make_player(id=pid, position=PlayerPosition.GOALKEEPER)]
    pid += 1
    for count, pos in zip(formation, [PlayerPosition.DEFENDER, PlayerPosition.MIDFIELDER, PlayerPosition.FORWARD]):
        for _ in range(count):
            xi.append(make_player(id=pid, position=pos))
            pid += 1
    bench = [make_player(id=pid, position=PlayerPosition.GOALKEEPER)]
    pid += 1
    for pos in bench_positions:
        overrides = (bench_overrides or {}).get(len(bench), {})
        bench.append(make_player(id=pid, position=pos, **overrides))
        pid += 1
    return xi, bench


class TestBenchOrderFormationContext:
    """Tests for formation context analysis."""

    @pytest.fixture()
    def agent(self):
        return BenchOrderAgent()

    def test_343_one_bench_def_sole_coverage(self, agent):
        """3-4-3 with one bench DEF: DEF is sole coverage for constrained position."""
        xi, bench = _make_squad((3, 4, 3), [PlayerPosition.DEFENDER, PlayerPosition.MIDFIELDER, PlayerPosition.FORWARD],
                                bench_overrides={1: {"web_name": "BenchDEF"}})

        ctx = agent._analyze_formation_context(xi, bench)

        assert "DEF" in ctx["constrained_positions"]
        assert len(ctx["sole_coverage"]) == 1
        assert ctx["sole_coverage"][0]["name"] == "BenchDEF"
        assert ctx["coverage_gaps"] == []

    def test_433_def_above_minimum(self, agent):
        """4-3-3 formation: DEF above minimum, not constrained."""
        xi, bench = _make_squad((4, 3, 3), [PlayerPosition.DEFENDER, PlayerPosition.MIDFIELDER, PlayerPosition.FORWARD])

        ctx = agent._analyze_formation_context(xi, bench)

        assert "DEF" not in ctx["constrained_positions"]

    def test_343_no_bench_def_coverage_gap(self, agent):
        """3-4-3 with zero bench DEFs: coverage gap generated."""
        xi, bench = _make_squad((3, 4, 3), [PlayerPosition.MIDFIELDER, PlayerPosition.MIDFIELDER, PlayerPosition.FORWARD])

        ctx = agent._analyze_formation_context(xi, bench)

        assert "DEF" in ctx["constrained_positions"]
        assert "DEF" in ctx["coverage_gaps"]
        assert ctx["sole_coverage"] == []

    def test_352_mixed_constraints(self, agent):
        """3-5-2: DEF constrained (3=min), FWD not (2>1 min)."""
        xi, bench = _make_squad((3, 5, 2), [PlayerPosition.DEFENDER, PlayerPosition.MIDFIELDER, PlayerPosition.FORWARD],
                                bench_overrides={1: {"web_name": "BenchDEF"}})

        ctx = agent._analyze_formation_context(xi, bench)

        assert "DEF" in ctx["constrained_positions"]
        assert "FWD" not in ctx["constrained_positions"]
        assert len(ctx["sole_coverage"]) == 1
        assert ctx["sole_coverage"][0]["name"] == "BenchDEF"

    def test_541_mid_at_minimum(self, agent):
        """5-4-1: MID not constrained (4>2 min), FWD constrained (1=min)."""
        xi, bench = _make_squad((5, 4, 1), [PlayerPosition.DEFENDER, PlayerPosition.MIDFIELDER, PlayerPosition.FORWARD],
                                bench_overrides={3: {"web_name": "BenchFWD"}})

        ctx = agent._analyze_formation_context(xi, bench)

        assert "FWD" in ctx["constrained_positions"]
        assert "MID" not in ctx["constrained_positions"]
        assert len(ctx["sole_coverage"]) == 1
        assert ctx["sole_coverage"][0]["name"] == "BenchFWD"

    def test_532_mid_at_minimum_constrained(self, agent):
        """5-2-3: MID constrained at minimum (2=min)."""
        xi, bench = _make_squad((5, 2, 3), [PlayerPosition.DEFENDER, PlayerPosition.MIDFIELDER, PlayerPosition.FORWARD])

        ctx = agent._analyze_formation_context(xi, bench)

        assert "MID" in ctx["constrained_positions"]

    def test_gk_excluded_from_formation_context(self, agent):
        """GK bench players are excluded from formation context (separate auto-sub slot)."""
        xi, bench = _make_squad((3, 4, 3), [PlayerPosition.DEFENDER, PlayerPosition.MIDFIELDER, PlayerPosition.FORWARD])

        ctx = agent._analyze_formation_context(xi, bench)

        for entry in ctx["sole_coverage"]:
            assert entry["position"] != "GK"
        assert "GK" not in ctx["coverage_gaps"]

    def test_empty_bench_constrained_positions_become_gaps(self, agent):
        """Empty outfield bench: constrained positions become coverage gaps."""
        xi, _ = _make_squad((3, 4, 3), [])
        bench = [make_player(id=99, position=PlayerPosition.GOALKEEPER)]

        ctx = agent._analyze_formation_context(xi, bench)

        # 3-4-3: DEF at minimum (3=3), MID above (4>2), FWD above (3>1)
        assert "DEF" in ctx["coverage_gaps"]
        assert "MID" not in ctx["coverage_gaps"]
        assert "FWD" not in ctx["coverage_gaps"]
        assert ctx["sole_coverage"] == []

    def test_multiple_coverage_gaps(self, agent):
        """Multiple positions at minimum with no bench cover produce multiple gaps."""
        # 3-2-5 is not a valid FPL formation but tests the logic
        xi, bench = _make_squad((3, 2, 5), [PlayerPosition.FORWARD, PlayerPosition.FORWARD, PlayerPosition.FORWARD])

        ctx = agent._analyze_formation_context(xi, bench)

        assert "DEF" in ctx["coverage_gaps"]
        assert "MID" in ctx["coverage_gaps"]
        assert len(ctx["coverage_gaps"]) == 2

    def test_no_warnings_when_no_coverage_gaps(self, agent):
        """No warnings when bench covers all constrained positions."""
        xi, bench = _make_squad((3, 4, 3), [PlayerPosition.DEFENDER, PlayerPosition.MIDFIELDER, PlayerPosition.FORWARD])

        ctx = agent._analyze_formation_context(xi, bench)

        assert ctx["coverage_gaps"] == []
