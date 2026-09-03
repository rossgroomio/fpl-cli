"""Tests for .agents/skills/gw-prep/scripts/bench_order.py wrapper script."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import pytest

from fpl_cli.agents.base import AgentResult, AgentStatus
from fpl_cli.models.player import PlayerPosition
from tests.conftest import load_gw_prep_script, make_player, make_team

_mod = load_gw_prep_script("bench_order.py")
_run = _mod._run

# Name resolution itself is `resolve_players_or_report` in the package,
# shared with the other two scripts and covered by
# tests/test_player_resolution.py.


# -- _run integration tests --

def _make_agent_result(*, success: bool, data: dict | None = None, errors: list[str] | None = None) -> AgentResult:
    return AgentResult(
        agent_name="bench_order",
        status=AgentStatus.SUCCESS if success else AgentStatus.FAILED,
        data=data or {},
        errors=errors or [],
        message="" if success else "Agent failed",
    )


@pytest.fixture
def mock_players():
    return [
        make_player(id=1, web_name="Raya", first_name="David", second_name="Raya"),
        make_player(id=2, web_name="Saliba", first_name="William", second_name="Saliba"),
        make_player(id=3, web_name="Salah", first_name="Mohamed", second_name="Salah"),
        make_player(id=4, web_name="Haaland", first_name="Erling", second_name="Haaland"),
        make_player(id=5, web_name="Mbeumo", first_name="Bryan", second_name="Mbeumo"),
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


async def test_run_happy_path(mock_players, mock_teams, capsys):
    expected_data = {"bench_order": [5, 2], "reasoning": "test"}

    mock_client = AsyncMock()
    mock_client.get_players = AsyncMock(return_value=mock_players)
    mock_client.get_teams = AsyncMock(return_value=mock_teams)
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)

    mock_agent = AsyncMock()
    mock_agent.run = AsyncMock(return_value=_make_agent_result(success=True, data=expected_data))
    mock_agent.__aenter__ = AsyncMock(return_value=mock_agent)
    mock_agent.__aexit__ = AsyncMock(return_value=False)

    with (
        patch.object(_mod, "FPLClient", return_value=mock_client),
        patch.object(_mod, "BenchOrderAgent", return_value=mock_agent),
    ):
        await _run(["Salah", "Haaland", "Raya"], ["Mbeumo", "Saliba"])

    captured = capsys.readouterr()
    output = json.loads(captured.out)
    assert output == expected_data

    # Verify correct IDs passed to agent
    mock_agent.run.assert_called_once()
    call_ctx = mock_agent.run.call_args[1]["context"]
    assert call_ctx["starting_xi"] == [3, 4, 1]
    assert call_ctx["bench"] == [5, 2]


async def test_run_unresolvable_player(mock_players, mock_teams, capsys):
    mock_client = AsyncMock()
    mock_client.get_players = AsyncMock(return_value=mock_players)
    mock_client.get_teams = AsyncMock(return_value=mock_teams)
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)

    with (
        patch.object(_mod, "FPLClient", return_value=mock_client),
        pytest.raises(SystemExit, match="1"),
    ):
        await _run(["Salah", "NonexistentPlayer"], ["Mbeumo"])

    captured = capsys.readouterr()
    output = json.loads(captured.out)
    assert output["error"] is True
    assert any("NonexistentPlayer" in msg for msg in output["messages"])


async def test_run_agent_failure(mock_players, mock_teams, capsys):
    mock_client = AsyncMock()
    mock_client.get_players = AsyncMock(return_value=mock_players)
    mock_client.get_teams = AsyncMock(return_value=mock_teams)
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)

    mock_agent = AsyncMock()
    mock_agent.run = AsyncMock(return_value=_make_agent_result(
        success=False, errors=["Something went wrong"],
    ))
    mock_agent.__aenter__ = AsyncMock(return_value=mock_agent)
    mock_agent.__aexit__ = AsyncMock(return_value=False)

    with (
        patch.object(_mod, "FPLClient", return_value=mock_client),
        patch.object(_mod, "BenchOrderAgent", return_value=mock_agent),
        pytest.raises(SystemExit, match="1"),
    ):
        await _run(["Salah"], ["Mbeumo"])

    captured = capsys.readouterr()
    output = json.loads(captured.out)
    assert output["error"] is True
    assert "Something went wrong" in output["messages"]


async def test_run_unresolvable_bench_player(mock_players, mock_teams, capsys):
    mock_client = AsyncMock()
    mock_client.get_players = AsyncMock(return_value=mock_players)
    mock_client.get_teams = AsyncMock(return_value=mock_teams)
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)

    with (
        patch.object(_mod, "FPLClient", return_value=mock_client),
        pytest.raises(SystemExit, match="1"),
    ):
        await _run(["Salah"], ["NonexistentBench"])

    captured = capsys.readouterr()
    output = json.loads(captured.out)
    assert output["error"] is True
    assert any("NonexistentBench" in msg for msg in output["messages"])


async def test_run_agent_failure_empty_errors(mock_players, mock_teams, capsys):
    mock_client = AsyncMock()
    mock_client.get_players = AsyncMock(return_value=mock_players)
    mock_client.get_teams = AsyncMock(return_value=mock_teams)
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)

    mock_agent = AsyncMock()
    mock_agent.run = AsyncMock(return_value=_make_agent_result(
        success=False, errors=[],
    ))
    mock_agent.__aenter__ = AsyncMock(return_value=mock_agent)
    mock_agent.__aexit__ = AsyncMock(return_value=False)

    with (
        patch.object(_mod, "FPLClient", return_value=mock_client),
        patch.object(_mod, "BenchOrderAgent", return_value=mock_agent),
        pytest.raises(SystemExit, match="1"),
    ):
        await _run(["Salah"], ["Mbeumo"])

    captured = capsys.readouterr()
    output = json.loads(captured.out)
    assert output["error"] is True
    assert "Agent failed" in output["messages"]


async def test_run_ambiguous_name_errors_instead_of_guessing(mock_players, mock_teams, capsys):
    """Two Hendersons must not silently resolve to whichever has the lower id."""
    mock_client = AsyncMock()
    mock_client.get_players = AsyncMock(return_value=mock_players)
    mock_client.get_teams = AsyncMock(return_value=mock_teams)
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)

    with (
        patch.object(_mod, "FPLClient", return_value=mock_client),
        pytest.raises(SystemExit, match="1"),
    ):
        await _run(["Salah"], ["Henderson"])

    captured = capsys.readouterr()
    output = json.loads(captured.out)
    assert output["error"] is True
    message = "\n".join(output["messages"])
    assert "Ambiguous bench player" in message
    assert "Henderson (CRY)" in message
    assert "Henderson (CHE)" in message


async def test_run_team_disambiguator_resolves_shared_surname(mock_players, mock_teams, capsys):
    expected_data = {"bench_order": [6], "reasoning": "test"}

    mock_client = AsyncMock()
    mock_client.get_players = AsyncMock(return_value=mock_players)
    mock_client.get_teams = AsyncMock(return_value=mock_teams)
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)

    mock_agent = AsyncMock()
    mock_agent.run = AsyncMock(return_value=_make_agent_result(success=True, data=expected_data))
    mock_agent.__aenter__ = AsyncMock(return_value=mock_agent)
    mock_agent.__aexit__ = AsyncMock(return_value=False)

    with (
        patch.object(_mod, "FPLClient", return_value=mock_client),
        patch.object(_mod, "BenchOrderAgent", return_value=mock_agent),
    ):
        await _run(["Salah"], ["Henderson (CRY)"])

    assert json.loads(capsys.readouterr().out) == expected_data
    assert mock_agent.run.call_args[1]["context"]["bench"] == [6]
