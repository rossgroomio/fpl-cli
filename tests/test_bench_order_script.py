"""Tests for .agents/skills/gw-prep/scripts/bench_order.py wrapper script."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import ModuleType
from unittest.mock import AsyncMock, patch

import pytest

from fpl_cli.agents.base import AgentResult, AgentStatus
from tests.conftest import make_player


def _load_script() -> ModuleType:
    """Load bench_order.py as a module (it's not a package)."""
    script_path = Path(__file__).parent.parent / ".agents/skills/gw-prep/scripts/bench_order.py"
    spec = importlib.util.spec_from_file_location("bench_order_script", script_path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_mod = _load_script()
resolve_player = _mod.resolve_player
_run = _mod._run


# -- resolve_player tests --

@pytest.fixture
def players():
    return [
        make_player(id=10, web_name="Salah", first_name="Mohamed", second_name="Salah"),
        make_player(id=20, web_name="Saka", first_name="Bukayo", second_name="Saka"),
        make_player(id=30, web_name="Haaland", first_name="Erling", second_name="Haaland"),
        make_player(id=40, web_name="Martinez", first_name="Emiliano", second_name="Martinez"),
    ]


def test_resolve_exact_web_name(players):
    assert resolve_player("Salah", players).id == 10


def test_resolve_exact_full_name(players):
    assert resolve_player("Mohamed Salah", players).id == 10


def test_resolve_case_insensitive(players):
    assert resolve_player("salah", players).id == 10
    assert resolve_player("SAKA", players).id == 20


def test_resolve_substring_match(players):
    assert resolve_player("Mohamed", players).id == 10
    assert resolve_player("Bukayo", players).id == 20


def test_resolve_unresolvable(players):
    assert resolve_player("Nonexistent", players) is None


def test_resolve_prefers_exact_over_substring(players):
    # "Saka" should match web_name exactly, not substring of something else
    assert resolve_player("Saka", players).id == 20


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
    ]


async def test_run_happy_path(mock_players, capsys):
    expected_data = {"bench_order": [5, 2], "reasoning": "test"}

    mock_client = AsyncMock()
    mock_client.get_players = AsyncMock(return_value=mock_players)
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


async def test_run_unresolvable_player(mock_players, capsys):
    mock_client = AsyncMock()
    mock_client.get_players = AsyncMock(return_value=mock_players)
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


async def test_run_agent_failure(mock_players, capsys):
    mock_client = AsyncMock()
    mock_client.get_players = AsyncMock(return_value=mock_players)
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


async def test_run_unresolvable_bench_player(mock_players, capsys):
    mock_client = AsyncMock()
    mock_client.get_players = AsyncMock(return_value=mock_players)
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


async def test_run_agent_failure_empty_errors(mock_players, capsys):
    mock_client = AsyncMock()
    mock_client.get_players = AsyncMock(return_value=mock_players)
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
