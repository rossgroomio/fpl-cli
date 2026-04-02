"""Tests for `fpl targets` JSON output."""
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
        "targets": {
            "all": [
                {
                    "player_name": "Salah",
                    "team_short": "LIV",
                    "position": "MID",
                    "ownership": 45.0,
                    "xGI_per_90": 0.85,
                    "matchup_score": 7.5,
                    "next_opponent": "ARS",
                    "target_score": 9.2,
                }
            ],
            "by_tier": {
                "template": [],
                "popular": [],
                "differential": [],
            },
        },
        "window_label": "whole season",
    }
    result.message = message
    result.errors = ["Something went wrong"] if not success else []
    return result


def _run_targets(args=None, agent_result=None):
    runner = CliRunner()
    if agent_result is None:
        agent_result = _make_agent_result()

    mock_agent = MagicMock()
    mock_agent.run = AsyncMock(return_value=agent_result)
    mock_agent.__aenter__ = AsyncMock(return_value=mock_agent)
    mock_agent.__aexit__ = AsyncMock(return_value=False)

    with patch("fpl_cli.agents.analysis.stats.StatsAgent", return_value=mock_agent), \
         patch("fpl_cli.cli._context.load_settings", return_value={"custom_analysis": True}):
        return runner.invoke(main, ["targets"] + (args or []))


class TestTargetsJsonFormat:
    def test_json_output_is_valid(self):
        result = _run_targets(["--format", "json"])
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert data["command"] == "targets"
        assert isinstance(data["data"], dict)

    def test_json_contains_targets(self):
        result = _run_targets(["--format", "json"])
        data = json.loads(result.output)
        assert "targets" in data["data"]
        assert data["data"]["targets"]["all"][0]["player_name"] == "Salah"

    def test_json_metadata_is_empty(self):
        result = _run_targets(["--format", "json"])
        data = json.loads(result.output)
        assert data["metadata"] == {}

    def test_json_agent_failure_exits_nonzero(self):
        agent_result = _make_agent_result(success=False, message="API timeout")
        result = _run_targets(["--format", "json"], agent_result=agent_result)
        assert result.exit_code == 1

    def test_table_output_unchanged(self):
        result = _run_targets()
        assert result.exit_code == 0, result.output
        assert "Salah" in result.output
        assert "Transfer Targets" in result.output
