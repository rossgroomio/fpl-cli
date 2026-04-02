"""Tests for `fpl captain` JSON output."""
from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

from click.testing import CliRunner

from fpl_cli.cli import main


def _make_agent_result(success=True, data=None, message=""):
    """Create a mock AgentResult."""
    result = MagicMock()
    result.success = success
    result.data = data or {
        "gameweek": 30,
        "deadline": "2026-03-28 18:30",
        "my_squad_mode": True,
        "top_picks": [
            {
                "player_name": "Salah",
                "team_short": "LIV",
                "captain_score": 8.5,
                "attack_matchup": 7.2,
                "defence_matchup": 6.0,
                "form_differential": 0.5,
                "position_differential": 0.3,
                "avg_fdr": 2.0,
                "fixtures": [{"opponent": "ARS", "is_home": True}],
                "reasons": ["Top form", "Easy fixture"],
            }
        ],
    }
    result.message = message
    result.errors = ["Something went wrong"] if not success else []
    return result


def _run_captain(args=None, agent_result=None):
    runner = CliRunner()
    if agent_result is None:
        agent_result = _make_agent_result()

    mock_agent = MagicMock()
    mock_agent.run = AsyncMock(return_value=agent_result)
    mock_agent.__aenter__ = AsyncMock(return_value=mock_agent)
    mock_agent.__aexit__ = AsyncMock(return_value=False)

    with patch("fpl_cli.agents.analysis.captain.CaptainAgent", return_value=mock_agent), \
         patch("fpl_cli.cli._context.load_settings", return_value={"fpl": {"classic_entry_id": 123}, "custom_analysis": True}):
        return runner.invoke(main, ["captain"] + (args or []))


class TestCaptainJsonFormat:
    def test_json_output_is_valid(self):
        result = _run_captain(["--format", "json"])
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert data["command"] == "captain"
        assert isinstance(data["data"], dict)

    def test_json_contains_top_picks(self):
        result = _run_captain(["--format", "json"])
        data = json.loads(result.output)
        assert "top_picks" in data["data"]
        assert data["data"]["top_picks"][0]["player_name"] == "Salah"

    def test_json_metadata_has_gameweek(self):
        result = _run_captain(["--format", "json"])
        data = json.loads(result.output)
        assert data["metadata"]["gameweek"] == 30

    def test_json_agent_failure_exits_nonzero(self):
        agent_result = _make_agent_result(success=False, message="API timeout")
        result = _run_captain(["--format", "json"], agent_result=agent_result)
        assert result.exit_code == 1

    def test_table_output_unchanged(self):
        result = _run_captain()
        assert result.exit_code == 0, result.output
        assert "Salah" in result.output
        assert "Captain Picks" in result.output
