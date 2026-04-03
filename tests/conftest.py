"""Shared fixtures and mock data for FPL Agents tests."""

from datetime import datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from fpl_cli.models.fixture import Fixture
from fpl_cli.models.player import Player, PlayerPosition, PlayerStatus
from fpl_cli.models.team import Team
from fpl_cli.paths import user_config_dir, user_data_dir


@pytest.fixture(autouse=True)
def _clear_path_cache():
    """Prevent stale lru_cache values leaking between tests that alter FPL_CLI_CONFIG_DIR."""
    user_config_dir.cache_clear()
    user_data_dir.cache_clear()
    yield
    user_config_dir.cache_clear()
    user_data_dir.cache_clear()


# --- Draft-Specific Factories ---

def make_draft_player(
    id: int = 1,
    web_name: str = "TestPlayer",
    first_name: str = "Test",
    second_name: str = "Player",
    team: int = 1,
    element_type: int = 3,  # MID
    form: float = 5.0,
    points_per_game: float = 5.0,
    total_points: int = 50,
    minutes: int = 900,
    status: str = "a",
    expected_goals: float = 3.0,
    expected_assists: float = 2.0,
    goals_scored: int = 3,
    assists: int = 2,
    clean_sheets: int = 2,
    news: str = "",
    chance_of_playing_next_round: int | None = 100,
    **kwargs,
) -> dict[str, Any]:
    """Factory for raw draft API player data (dict format)."""
    return {
        "id": id,
        "web_name": web_name,
        "first_name": first_name,
        "second_name": second_name,
        "team": team,
        "element_type": element_type,
        "form": str(form),
        "points_per_game": str(points_per_game),
        "total_points": total_points,
        "minutes": minutes,
        "status": status,
        "expected_goals": str(expected_goals),
        "expected_assists": str(expected_assists),
        "goals_scored": goals_scored,
        "assists": assists,
        "clean_sheets": clean_sheets,
        "news": news,
        "chance_of_playing_next_round": chance_of_playing_next_round,
        **kwargs,
    }


def make_draft_team(
    id: int = 1,
    name: str = "Test FC",
    short_name: str = "TFC",
) -> dict[str, Any]:
    """Factory for draft API team data."""
    return {"id": id, "name": name, "short_name": short_name}


def make_draft_league_entry(
    id: int = 1,
    entry_id: int = 100,
    entry_name: str = "Test Team",
    player_first_name: str = "John",
    player_last_name: str = "Doe",
) -> dict[str, Any]:
    """Factory for draft league entry."""
    return {
        "id": id,
        "entry_id": entry_id,
        "entry_name": entry_name,
        "player_first_name": player_first_name,
        "player_last_name": player_last_name,
    }


def make_draft_standing(
    league_entry: int = 1,
    rank: int = 1,
    total: int = 500,
    event_total: int = 50,
) -> dict[str, Any]:
    """Factory for draft league standing."""
    return {
        "league_entry": league_entry,
        "rank": rank,
        "total": total,
        "event_total": event_total,
    }


# --- Sample Data Factories ---

def make_player(
    id: int = 1,
    code: int = 0,
    web_name: str = "TestPlayer",
    first_name: str = "Test",
    second_name: str = "Player",
    team_id: int = 1,
    position: PlayerPosition = PlayerPosition.MIDFIELDER,
    now_cost: int = 100,
    selected_by_percent: float = 10.0,
    status: PlayerStatus = PlayerStatus.AVAILABLE,
    total_points: int = 50,
    points_per_game: float = 5.0,
    form: float = 5.0,
    minutes: int = 900,
    goals_scored: int = 5,
    assists: int = 3,
    expected_goals: float = 4.5,
    expected_assists: float = 2.8,
    expected_goal_involvements: float = 7.3,
    **kwargs,
) -> Player:
    """Factory function to create Player instances for testing."""
    return Player(
        id=id,
        code=code,
        web_name=web_name,
        first_name=first_name,
        second_name=second_name,
        team=team_id,
        element_type=position.value,
        now_cost=now_cost,
        selected_by_percent=selected_by_percent,
        status=status,
        total_points=total_points,
        points_per_game=points_per_game,
        form=form,
        minutes=minutes,
        goals_scored=goals_scored,
        assists=assists,
        expected_goals=expected_goals,
        expected_assists=expected_assists,
        expected_goal_involvements=expected_goal_involvements,
        **kwargs,
    )


def make_team(
    id: int = 1,
    name: str = "Test FC",
    short_name: str = "TFC",
    code: int = 1,
    strength: int = 3,
    strength_overall_home: int = 1200,
    strength_overall_away: int = 1100,
    strength_attack_home: int = 1150,
    strength_attack_away: int = 1050,
    strength_defence_home: int = 1180,
    strength_defence_away: int = 1080,
    form: str = "WDWLW",
    position: int = 10,
    played: int = 20,
    win: int = 8,
    draw: int = 5,
    loss: int = 7,
    points: int = 29,
    **kwargs,
) -> Team:
    """Factory function to create Team instances for testing."""
    return Team(
        id=id,
        name=name,
        short_name=short_name,
        code=code,
        strength=strength,
        strength_overall_home=strength_overall_home,
        strength_overall_away=strength_overall_away,
        strength_attack_home=strength_attack_home,
        strength_attack_away=strength_attack_away,
        strength_defence_home=strength_defence_home,
        strength_defence_away=strength_defence_away,
        form=form,
        position=position,
        played=played,
        win=win,
        draw=draw,
        loss=loss,
        points=points,
        **kwargs,
    )


def make_fixture(
    id: int = 1,
    gameweek: int = 10,
    home_team_id: int = 1,
    away_team_id: int = 2,
    home_difficulty: int = 3,
    away_difficulty: int = 3,
    kickoff_time: datetime | None = None,
    finished: bool = False,
    started: bool = False,
    home_score: int | None = None,
    away_score: int | None = None,
    stats: list | None = None,
    **kwargs,
) -> Fixture:
    """Factory function to create Fixture instances for testing."""
    if kickoff_time is None:
        kickoff_time = datetime.now() + timedelta(days=1)
    return Fixture(
        id=id,
        event=gameweek,
        team_h=home_team_id,
        team_a=away_team_id,
        team_h_difficulty=home_difficulty,
        team_a_difficulty=away_difficulty,
        kickoff_time=kickoff_time,
        finished=finished,
        started=started,
        team_h_score=home_score,
        team_a_score=away_score,
        stats=stats or [],
        **kwargs,
    )


# --- Pytest Fixtures ---

@pytest.fixture
def sample_player() -> Player:
    """A sample player for testing."""
    return make_player(
        id=100,
        web_name="Salah",
        first_name="Mohamed",
        second_name="Salah",
        team_id=14,  # Liverpool
        position=PlayerPosition.MIDFIELDER,
        now_cost=130,
        selected_by_percent=45.5,
        total_points=120,
        points_per_game=6.5,
        form=7.2,
        minutes=1800,
        goals_scored=12,
        assists=8,
        expected_goals=10.5,
        expected_assists=7.2,
    )


@pytest.fixture
def sample_players() -> list[Player]:
    """A list of sample players for testing."""
    return [
        make_player(id=1, web_name="Haaland", team_id=13, position=PlayerPosition.FORWARD,
                    now_cost=150, goals_scored=20, assists=5, expected_goals=18.5,
                    expected_assists=4.2, minutes=2000, form=8.5, selected_by_percent=85.0),
        make_player(id=2, web_name="Salah", team_id=14, position=PlayerPosition.MIDFIELDER,
                    now_cost=130, goals_scored=12, assists=8, expected_goals=10.5,
                    expected_assists=7.2, minutes=1800, form=7.2, selected_by_percent=45.5),
        make_player(id=3, web_name="Saka", team_id=1, position=PlayerPosition.MIDFIELDER,
                    now_cost=95, goals_scored=8, assists=10, expected_goals=7.0,
                    expected_assists=9.5, minutes=1700, form=6.8, selected_by_percent=35.0),
        make_player(id=4, web_name="Gabriel", team_id=1, position=PlayerPosition.DEFENDER,
                    now_cost=55, goals_scored=3, assists=1, expected_goals=2.5,
                    expected_assists=0.8, minutes=1900, form=5.5, selected_by_percent=25.0,
                    clean_sheets=10),
        make_player(id=5, web_name="Raya", team_id=1, position=PlayerPosition.GOALKEEPER,
                    now_cost=55, goals_scored=0, assists=0, expected_goals=0.0,
                    expected_assists=0.0, minutes=2000, form=5.0, selected_by_percent=20.0,
                    clean_sheets=12),
        # Differential players
        make_player(id=6, web_name="Differential", team_id=5, position=PlayerPosition.MIDFIELDER,
                    now_cost=60, goals_scored=4, assists=5, expected_goals=5.5,
                    expected_assists=4.0, minutes=1500, form=6.0, selected_by_percent=2.5),
    ]


@pytest.fixture
def sample_team() -> Team:
    """A sample team for testing."""
    return make_team(
        id=1,
        name="Arsenal",
        short_name="ARS",
        code=3,
        strength=4,
        position=1,
        played=20,
        win=15,
        draw=3,
        loss=2,
        points=48,
        form="WWWDW",
    )


@pytest.fixture
def sample_teams() -> list[Team]:
    """A list of sample teams for testing."""
    return [
        make_team(id=1, name="Arsenal", short_name="ARS", position=1, points=48, form="WWWDW"),
        make_team(id=2, name="Manchester City", short_name="MCI", position=2, points=45, form="WDWWW"),
        make_team(id=3, name="Liverpool", short_name="LIV", position=3, points=42, form="WWDWL"),
        make_team(id=4, name="Aston Villa", short_name="AVL", position=4, points=40, form="WLWDW"),
        make_team(id=5, name="Tottenham", short_name="TOT", position=5, points=38, form="LDWWW"),
        make_team(id=6, name="Brighton", short_name="BHA", position=10, points=28, form="DLWDW"),
        make_team(id=7, name="Bournemouth", short_name="BOU", position=15, points=20, form="LLWDL"),
        make_team(id=8, name="Sheffield Utd", short_name="SHU", position=20, points=10, form="LLLLL"),
    ]


@pytest.fixture
def sample_fixture() -> Fixture:
    """A sample fixture for testing."""
    return make_fixture(
        id=100,
        gameweek=25,
        home_team_id=1,  # Arsenal
        away_team_id=2,  # Man City
        home_difficulty=4,
        away_difficulty=4,
    )


@pytest.fixture
def sample_fixtures() -> list[Fixture]:
    """A list of sample fixtures for testing."""
    base_time = datetime.now() + timedelta(days=7)
    return [
        # GW 25
        make_fixture(id=1, gameweek=25, home_team_id=1, away_team_id=8, home_difficulty=2, away_difficulty=5,
                     kickoff_time=base_time),
        make_fixture(id=2, gameweek=25, home_team_id=2, away_team_id=7, home_difficulty=2, away_difficulty=5,
                     kickoff_time=base_time + timedelta(hours=2)),
        make_fixture(id=3, gameweek=25, home_team_id=3, away_team_id=6, home_difficulty=3, away_difficulty=4,
                     kickoff_time=base_time + timedelta(hours=4)),
        # GW 26
        make_fixture(id=4, gameweek=26, home_team_id=8, away_team_id=1, home_difficulty=5, away_difficulty=2,
                     kickoff_time=base_time + timedelta(days=7)),
        make_fixture(id=5, gameweek=26, home_team_id=6, away_team_id=2, home_difficulty=4, away_difficulty=3,
                     kickoff_time=base_time + timedelta(days=7, hours=2)),
        # Completed fixture
        make_fixture(id=10, gameweek=24, home_team_id=1, away_team_id=3, home_difficulty=3, away_difficulty=4,
                     finished=True, home_score=2, away_score=1,
                     kickoff_time=base_time - timedelta(days=7),
                     stats=[
                         {"identifier": "goals_scored", "h": [{"element": 4, "value": 2}], "a": [{"element": 100, "value": 1}]},
                         {"identifier": "assists", "h": [{"element": 3, "value": 1}], "a": []},
                         {"identifier": "bonus", "h": [{"element": 4, "value": 3}, {"element": 3, "value": 2}], "a": [{"element": 100, "value": 1}]},
                     ]),
    ]


@pytest.fixture
def completed_fixtures() -> list[Fixture]:
    """A list of completed fixtures for form calculation testing."""
    base_time = datetime.now() - timedelta(days=1)
    fixtures = []
    # Create 6 completed fixtures for team 1 (Arsenal)
    for i in range(6):
        gw = 20 - i
        # Alternate home/away
        if i % 2 == 0:
            fixtures.append(make_fixture(
                id=100 + i, gameweek=gw, home_team_id=1, away_team_id=7 + i,
                home_difficulty=2, away_difficulty=4,
                finished=True, home_score=2, away_score=0,
                kickoff_time=base_time - timedelta(days=7 * i),
            ))
        else:
            fixtures.append(make_fixture(
                id=100 + i, gameweek=gw, home_team_id=7 + i, away_team_id=1,
                home_difficulty=4, away_difficulty=2,
                finished=True, home_score=1, away_score=1,
                kickoff_time=base_time - timedelta(days=7 * i),
            ))
    return fixtures


@pytest.fixture
def mock_bootstrap_data(sample_players, sample_teams) -> dict:
    """Mock bootstrap-static API response."""
    return {
        "elements": [
            {
                "id": p.id,
                "code": p.code,
                "web_name": p.web_name,
                "first_name": p.first_name,
                "second_name": p.second_name,
                "team": p.team_id,
                "element_type": p.position.value,
                "now_cost": p.now_cost,
                "selected_by_percent": str(p.selected_by_percent),
                "status": p.status.value,
                "total_points": p.total_points,
                "points_per_game": str(p.points_per_game),
                "form": str(p.form),
                "minutes": p.minutes,
                "goals_scored": p.goals_scored,
                "assists": p.assists,
                "expected_goals": str(p.expected_goals),
                "expected_assists": str(p.expected_assists),
                "expected_goal_involvements": str(p.expected_goal_involvements),
                "expected_goals_conceded": "0.0",
                "clean_sheets": p.clean_sheets,
                "goals_conceded": p.goals_conceded,
                "bonus": p.bonus,
                "bps": p.bps,
                "influence": str(p.influence),
                "creativity": str(p.creativity),
                "threat": str(p.threat),
                "ict_index": str(p.ict_index),
                "transfers_in_event": p.transfers_in_event,
                "transfers_out_event": p.transfers_out_event,
                "cost_change_event": p.cost_change_event,
                "cost_change_start": p.cost_change_start,
                "chance_of_playing_next_round": p.chance_of_playing_next_round,
                "news": p.news,
                "news_added": p.news_added,
                "starts": p.starts,
            }
            for p in sample_players
        ],
        "teams": [
            {
                "id": t.id,
                "name": t.name,
                "short_name": t.short_name,
                "code": t.code,
                "strength": t.strength,
                "strength_overall_home": t.strength_overall_home,
                "strength_overall_away": t.strength_overall_away,
                "strength_attack_home": t.strength_attack_home,
                "strength_attack_away": t.strength_attack_away,
                "strength_defence_home": t.strength_defence_home,
                "strength_defence_away": t.strength_defence_away,
                "form": t.form,
                "position": t.position,
                "played": t.played,
                "win": t.win,
                "draw": t.draw,
                "loss": t.loss,
                "points": t.points,
            }
            for t in sample_teams
        ],
        "events": [
            {"id": i, "is_current": i == 24, "is_next": i == 25, "deadline_time": "2024-02-10T11:00:00Z"}
            for i in range(1, 39)
        ],
    }


@pytest.fixture
def mock_fpl_client(mock_bootstrap_data, sample_fixtures):
    """Create a mock FPL client with pre-configured responses."""
    from fpl_cli.api.fpl import FPLClient

    client = FPLClient()
    client._bootstrap_data = mock_bootstrap_data
    client._get = AsyncMock()

    async def mock_get(endpoint):
        if endpoint == "bootstrap-static/":
            return mock_bootstrap_data
        elif endpoint.startswith("fixtures"):
            # Convert fixtures to dict format
            return [
                {
                    "id": f.id,
                    "event": f.gameweek,
                    "team_h": f.home_team_id,
                    "team_a": f.away_team_id,
                    "team_h_difficulty": f.home_difficulty,
                    "team_a_difficulty": f.away_difficulty,
                    "kickoff_time": f.kickoff_time.isoformat() if f.kickoff_time else None,
                    "finished": f.finished,
                    "started": f.started,
                    "team_h_score": f.home_score,
                    "team_a_score": f.away_score,
                    "stats": f.stats,
                }
                for f in sample_fixtures
            ]
        return {}

    client._get = AsyncMock(side_effect=mock_get)
    return client
