"""Tests for .agents/skills/gw-prep/scripts/starting_xi.py wrapper script."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType
from unittest.mock import AsyncMock, patch

import pytest

from fpl_cli.agents.base import AgentResult, AgentStatus
from fpl_cli.models.player import PlayerPosition
from tests.conftest import make_player, make_team


def _load_script() -> ModuleType:
    """Load starting_xi.py as a module (it's not a package).

    The scripts dir goes on sys.path while loading, matching a real
    `python starting_xi.py` run (sys.path[0] is the script's own dir), so
    the shared `_bootstrap` sibling module resolves.
    """
    scripts_dir = Path(__file__).parent.parent / ".agents/skills/gw-prep/scripts"
    script_path = scripts_dir / "starting_xi.py"
    sys.path.insert(0, str(scripts_dir))
    try:
        spec = importlib.util.spec_from_file_location("starting_xi_script", script_path)
        assert spec is not None and spec.loader is not None
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    finally:
        sys.path.remove(str(scripts_dir))
    return mod


_mod = _load_script()
_run = _mod._run


@pytest.fixture
def mock_players():
    return [
        make_player(id=1, web_name="Raya", first_name="David", second_name="Raya",
                    team_id=1, position=PlayerPosition.GOALKEEPER),
        make_player(id=2, web_name="Salah", first_name="Mohamed", second_name="Salah"),
        # Shared surname across two clubs - the issue #180 collision.
        make_player(id=6, web_name="Henderson", first_name="Dean", second_name="Henderson",
                    team_id=7, position=PlayerPosition.GOALKEEPER),
        make_player(id=7, web_name="Henderson", first_name="Jordan", second_name="Henderson",
                    team_id=4, position=PlayerPosition.MIDFIELDER),
    ]


@pytest.fixture
def mock_teams():
    return [
        make_team(id=1, name="Arsenal", short_name="ARS"),
        make_team(id=4, name="Chelsea", short_name="CHE"),
        make_team(id=7, name="Crystal Palace", short_name="CRY"),
    ]


def _mock_client(mock_players, mock_teams):
    client = AsyncMock()
    client.get_players = AsyncMock(return_value=mock_players)
    client.get_teams = AsyncMock(return_value=mock_teams)
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)
    return client


def _mock_agent(data: dict):
    agent = AsyncMock()
    agent.run = AsyncMock(return_value=AgentResult(
        agent_name="starting_xi", status=AgentStatus.SUCCESS, data=data, errors=[], message="",
    ))
    agent.__aenter__ = AsyncMock(return_value=agent)
    agent.__aexit__ = AsyncMock(return_value=False)
    return agent


async def test_run_ambiguous_name_errors_instead_of_guessing(mock_players, mock_teams, capsys):
    """Two Hendersons must not silently resolve to whichever has the lower id."""
    with (
        patch.object(_mod, "FPLClient", return_value=_mock_client(mock_players, mock_teams)),
        pytest.raises(SystemExit, match="1"),
    ):
        await _run(["Salah", "Henderson"])

    output = json.loads(capsys.readouterr().out)
    assert output["error"] is True
    message = "\n".join(output["messages"])
    assert "Ambiguous squad player" in message
    assert "Henderson (CRY)" in message
    assert "Henderson (CHE)" in message


async def test_run_team_disambiguator_resolves_shared_surname(mock_players, mock_teams, capsys):
    expected = {"starting_xi": [6], "bench": []}
    agent = _mock_agent(expected)

    with (
        patch.object(_mod, "FPLClient", return_value=_mock_client(mock_players, mock_teams)),
        patch.object(_mod, "StartingXIAgent", return_value=agent),
    ):
        await _run(["Salah", "Henderson (CRY)"])

    assert json.loads(capsys.readouterr().out) == expected
    assert agent.run.call_args[1]["context"]["squad"] == [2, 6]


async def test_run_unresolvable_player(mock_players, mock_teams, capsys):
    with (
        patch.object(_mod, "FPLClient", return_value=_mock_client(mock_players, mock_teams)),
        pytest.raises(SystemExit, match="1"),
    ):
        await _run(["Salah", "NonexistentPlayer"])

    output = json.loads(capsys.readouterr().out)
    assert output["error"] is True
    assert any("NonexistentPlayer" in msg for msg in output["messages"])
