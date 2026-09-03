"""Tests for .agents/skills/gw-prep/scripts/transfer_eval.py wrapper script."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import pytest

from fpl_cli.agents.base import AgentResult, AgentStatus
from fpl_cli.models.player import PlayerPosition
from tests.conftest import load_gw_prep_script, make_player, make_team

_mod = load_gw_prep_script("transfer_eval.py")
_run = _mod._run


@pytest.fixture
def mock_players():
    return [
        make_player(id=2, web_name="Salah", first_name="Mohamed", second_name="Salah",
                    team_id=1, position=PlayerPosition.MIDFIELDER),
        make_player(id=3, web_name="Palmer", first_name="Cole", second_name="Palmer",
                    team_id=4, position=PlayerPosition.MIDFIELDER),
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
        agent_name="transfer_eval", status=AgentStatus.SUCCESS, data=data, errors=[], message="",
    ))
    agent.__aenter__ = AsyncMock(return_value=agent)
    agent.__aexit__ = AsyncMock(return_value=False)
    return agent


async def test_run_ambiguous_out_player_errors(mock_players, mock_teams, capsys):
    """Two Hendersons must not silently resolve to whichever has the lower id."""
    with (
        patch.object(_mod, "FPLClient", return_value=_mock_client(mock_players, mock_teams)),
        pytest.raises(SystemExit, match="1"),
    ):
        await _run("Henderson", ["Salah"])

    output = json.loads(capsys.readouterr().out)
    assert output["error"] is True
    message = "\n".join(output["messages"])
    assert "Ambiguous OUT player" in message
    assert "Henderson (CRY)" in message
    assert "Henderson (CHE)" in message


async def test_run_ambiguous_in_player_errors(mock_players, mock_teams, capsys):
    with (
        patch.object(_mod, "FPLClient", return_value=_mock_client(mock_players, mock_teams)),
        pytest.raises(SystemExit, match="1"),
    ):
        await _run("Palmer", ["Henderson"])

    output = json.loads(capsys.readouterr().out)
    assert output["error"] is True
    assert any("Ambiguous IN player" in msg for msg in output["messages"])


async def test_run_team_disambiguator_resolves_shared_surname(mock_players, mock_teams, capsys):
    expected = {"out_player": {"id": 3}, "in_players": []}
    agent = _mock_agent(expected)

    with (
        patch.object(_mod, "FPLClient", return_value=_mock_client(mock_players, mock_teams)),
        patch.object(_mod, "TransferEvalAgent", return_value=agent),
    ):
        await _run("Palmer", ["Henderson (CHE)"])

    assert json.loads(capsys.readouterr().out) == expected
    ctx = agent.run.call_args[1]["context"]
    assert ctx["out_player_id"] == 3
    assert ctx["in_player_ids"] == [7]
