"""Shared fixtures and mock data for FPL Agents tests."""

import importlib.util
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import ModuleType
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
import yaml

from fpl_cli.agents.base import Agent, AgentResult, AgentStatus
from fpl_cli.models.fixture import Fixture
from fpl_cli.models.player import Player, PlayerPosition, PlayerStatus
from fpl_cli.models.team import Team
from fpl_cli.paths import user_cache_dir, user_config_dir, user_data_dir


@pytest.fixture
def stub_scoring_network_seams():
    """Stub every fetch prepare_scoring_data can make past a patched client.

    Tests patch an agent's client methods, but prepare_scoring_data reaches
    further: include_understat scrapes understat.com, include_prior pulls the
    historical datasets on a prior-cache miss, and include_match_data fetches
    the Core-Insights CSVs — none of them through the patched client.
    include_history stays on the client but calls get_player_detail, which
    run tests don't patch. `fpl stats --value` also calls fetch_match_records
    directly. This fixture is the single home for those patches (#53): test
    modules opt in with a one-line autouse wrapper, so closing a new seam in
    prepare_scoring_data happens here once instead of drifting per-file.

    Seams are patched on scoring.data_prep, where prepare_scoring_data
    resolves the names at call time. fetch_match_records is patched on the
    scoring package root as well, because `fpl stats --value` imports it
    from there inside the command body — a second, live lookup path that
    the data_prep patch does not cover. Other consumers bind their names at
    module-import time (agents.common, cli.player), so a root patch applied
    mid-test would not reach them; patch those at their own call site.
    """
    from fpl_cli.api.fpl import FPLClient

    with (
        patch(
            "fpl_cli.services.scoring.data_prep.build_understat_by_player_id",
            new_callable=AsyncMock,
            return_value={},
        ),
        patch(
            "fpl_cli.services.scoring.data_prep.fetch_match_records",
            new_callable=AsyncMock,
            return_value=None,
        ),
        patch(
            "fpl_cli.services.scoring.fetch_match_records",
            new_callable=AsyncMock,
            return_value=None,
        ),
        patch("fpl_cli.services.player_prior.load_cached_priors", return_value={}),
        patch.object(
            FPLClient,
            "get_player_detail",
            new_callable=AsyncMock,
            return_value={"history": []},
        ),
    ):
        yield


@pytest.fixture(params=[True, False], ids=["custom-on", "custom-off"])
def offline(request, monkeypatch, tmp_path):
    """A configured install whose upstream APIs are all unreachable.

    The shared driver for the two output contracts: `test_cli_json_contract`
    walks every `--format json` command down this failure and checks the
    envelope on stdout, `test_cli_failure_streams` walks the same commands in
    table mode and checks the prose on stderr. It lives here rather than in
    either module because a copy in the second one had already dropped the
    two client-level patches (#251 review), and a contract that skips a
    command whose outage stopped being an outage fails silently -- both walks
    treat "did not exit 1" as nothing to assert.

    Both toggles, because `custom_analysis` picks between two different
    bodies for the same command: `fdr` serves Bayesian ratings under one and
    raw API difficulty under the other, and only the second has the early
    return that skips the envelope. No entry IDs are set either way, so the
    commands that need one take their not-configured path. All of it is
    ordinary first-week state for a real user.
    """
    (tmp_path / "user-config" / "settings.yaml").write_text(
        yaml.safe_dump({"custom_analysis": request.param}), encoding="utf-8",
    )

    async def _unreachable(*args, **kwargs):
        raise httpx.ConnectError("connection refused")

    def _unreachable_sync(*args, **kwargs):
        raise httpx.ConnectError("connection refused")

    for module, attr in (
        ("fpl_cli.api.fpl", "FPLClient"),
        ("fpl_cli.api.fpl_draft", "FPLDraftClient"),
    ):
        mod = __import__(module, fromlist=[attr])
        monkeypatch.setattr(getattr(mod, attr), "_get", _unreachable, raising=False)
    monkeypatch.setattr(httpx.AsyncClient, "send", _unreachable)
    monkeypatch.setattr(httpx.Client, "send", _unreachable_sync)


@pytest.fixture
def malformed_fines(offline, tmp_path):
    """`offline`, plus the one hand-edit slip that used to take out three commands.

    A `below-threshold` rule that lost its `threshold:` (#170). The `fines:`
    block is read on nearly every command, so a parse that raised past click
    was never `league-fines`-shaped: `fpl status` and `fpl league-recap` died
    the same way, with a traceback on stderr and zero bytes on stdout.

    Layered over `offline` because the two walks that use it need the rest of
    the install to behave as it does there. Entry and league ids are set,
    unlike in `offline`, so a command reading the block reaches it rather
    than stopping at its not-configured path -- `league-fines` needs its
    league id, and being the one command that touches no network is what
    keeps the walk from going vacuous with the API down.
    """
    settings_file = tmp_path / "user-config" / "settings.yaml"
    settings = yaml.safe_load(settings_file.read_text(encoding="utf-8"))
    settings["fpl"] = {
        "classic_entry_id": 123,
        "classic_league_id": 456,
        "draft_entry_id": 789,
        "draft_league_id": 321,
    }
    bad_rule = [{"type": "below-threshold", "penalty": "Pint on video"}]
    settings["fines"] = {"classic": bad_rule, "draft": bad_rule}
    settings_file.write_text(yaml.safe_dump(settings), encoding="utf-8")


@pytest.fixture(autouse=True)
def _isolated_user_dirs(tmp_path, monkeypatch):
    """Per-test user dirs and fresh resolver caches.

    Redirects the config/data/cache env overrides to temp dirs so tests never
    read or write the real user locations (or a vault in cloud environments
    where FPL_CLI_* vars are set), and clears the lru_caches so the
    redirection takes effect. Tests that need different values call
    monkeypatch.setenv/delenv themselves, which overrides this fixture.

    This only holds because every consumer resolves its path through
    user_config_dir()/user_data_dir()/user_cache_dir() at the point of use.
    A module-level constant that calls one of them at import time is bound
    during collection, before this fixture runs, and would write to the real
    user location -- see fpl_cli/paths.py for why they are all functions.
    """
    from fpl_cli.cli import _context

    user_config_dir.cache_clear()
    user_data_dir.cache_clear()
    user_cache_dir.cache_clear()
    _context._warned_missing_settings.clear()
    monkeypatch.setenv("FPL_CLI_CONFIG_DIR", str(tmp_path / "user-config"))
    monkeypatch.setenv("FPL_CLI_DATA_DIR", str(tmp_path / "user-data"))
    monkeypatch.setenv("FPL_CLI_CACHE_DIR", str(tmp_path / "user-cache"))
    # The missing-settings warning (#46) is real behaviour for an explicitly-set
    # config dir, but every test gets an empty one -- so silence it by default
    # and let the tests that assert on it delete this file.
    (tmp_path / "user-config").mkdir(parents=True, exist_ok=True)
    (tmp_path / "user-config" / "settings.yaml").write_text("", encoding="utf-8")
    yield
    user_config_dir.cache_clear()
    user_data_dir.cache_clear()
    user_cache_dir.cache_clear()
    _context._warned_missing_settings.clear()


@pytest.fixture(autouse=True)
def _reset_understat_join_warnings():
    """A clean Understat join-drop record per test.

    The tripwire's "warn once per club, not once per player" record is
    process-global, so without this a club one test drops leaks into the next
    test's `metadata.warnings` -- or, worse, suppresses the warning a test is
    asserting on, depending on collection order.
    """
    from fpl_cli.api.understat import reset_understat_join_warnings

    reset_understat_join_warnings()
    yield
    reset_understat_join_warnings()


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


# --- Agent factory ---

class LoggingAgent(Agent):
    """A real agent that logs its progress the way every shipped one does.

    `make_agent()` below is a MagicMock, so it prints nothing. That is why a
    suite full of it could not see #226: the prose that broke a consumer's
    parse came from `Agent.log`, which a mock never reaches. Any test
    asserting on which stream a consumer reads needs an agent that logs.
    """

    name = "LoggingTestAgent"

    def __init__(
        self,
        data: dict[str, Any] | None = None,
        *,
        success: bool = True,
        message: str = "",
    ):
        super().__init__()
        self._data = data or {}
        self._success = success
        self._message = message
        self.last_context: dict[str, Any] | None = None

    async def run(self, context: dict[str, Any] | None = None) -> AgentResult:
        self.last_context = context
        self.log("Working...")
        if not self._success:
            self.log_error(self._message or "Agent failed")
            return self._create_result(
                AgentStatus.FAILED,
                message=self._message or "Agent failed",
                errors=[self._message] if self._message else [],
            )
        self.log_success("Done")
        return self._create_result(AgentStatus.SUCCESS, data=self._data)


def make_logging_agent(
    data: dict[str, Any] | None = None,
    *,
    success: bool = True,
    message: str = "",
) -> LoggingAgent:
    """`make_agent()`'s counterpart for tests about output streams.

    Patch it in where the mock would go: it is a real `Agent`, so `async
    with` and `run()` behave the same, and the two progress lines land
    wherever the code under test lets them.
    """
    return LoggingAgent(data, success=success, message=message)



def make_agent(
    data: dict[str, Any] | None = None,
    *,
    success: bool = True,
    message: str = "",
) -> MagicMock:
    """Async-context-manager stand-in for any `agents/base.py:Agent`.

    Every CLI test that patches an agent needs the same shape: an object
    usable with `async with`, whose `run()` awaits to a result carrying
    `success` / `data` / `message`. It is the base class's contract rather
    than any one agent's, which is why this is not FixtureAgent-specific --
    the copy in `test_cli_preview.py` already stood in for the stats, price
    and scout agents too.

    Built here so the shape lives in one place: rebuilt per test file, a
    change to how the CLI enters or awaits an agent has to be chased through
    each copy, and the copies that get missed keep passing on a stale mock
    while the real regression goes uncaught (#214 review).

    The returned mock stays mutable in the ways the tests rely on:
    `agent.run.return_value.data[key] = ...` to add a field, or
    `agent.run = AsyncMock(...)` to replace the result outright.
    """
    agent = MagicMock()
    agent.__aenter__ = AsyncMock(return_value=agent)
    agent.__aexit__ = AsyncMock(return_value=False)
    agent.run = AsyncMock(
        return_value=MagicMock(success=success, data=data or {}, message=message)
    )
    return agent


# --- League history ledger factory ---

def make_history_row(
    season: str = "2026-27",
    fpl_format: str = "classic",
    league_id: int = 1,
    gameweek: int = 1,
    manager_key: int = 1,
    capture_status: str = "ok",
    tier: str = "detailed",
    captured_at: datetime | None = None,
    manager_name: str = "Alice",
    **kwargs,
):
    """Factory for LeagueHistoryRow instances for testing.

    Defaults produce a minimal valid detailed row; pass any model field
    through kwargs to vary it. `captured_at` defaults to a fixed instant so
    two rows built without one compare equal on content.
    """
    from fpl_cli.models.league_history import LeagueHistoryRow

    return LeagueHistoryRow(
        season=season,
        fpl_format=fpl_format,
        league_id=league_id,
        gameweek=gameweek,
        manager_key=manager_key,
        capture_status=capture_status,
        tier=tier,
        captured_at=captured_at or datetime(2026, 8, 22, 12, 0, tzinfo=timezone.utc),
        manager_name=manager_name,
        **kwargs,
    )


def load_gw_prep_script(filename: str) -> ModuleType:
    """Load a gw-prep helper script as a module (they are not a package).

    The scripts dir goes on sys.path for the load, matching a real
    `python <script>` run where sys.path[0] is the script's own dir — which
    is how each script's `_bootstrap` sibling, the wrong-interpreter guard,
    resolves. Every script suite loads through here so a change to those
    mechanics is one edit rather than six.
    """
    scripts_dir = Path(__file__).parent.parent / ".agents/skills/gw-prep/scripts"
    script_path = scripts_dir / filename
    sys.path.insert(0, str(scripts_dir))
    try:
        spec = importlib.util.spec_from_file_location(script_path.stem, script_path)
        assert spec is not None and spec.loader is not None
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    finally:
        sys.path.remove(str(scripts_dir))
    return mod
