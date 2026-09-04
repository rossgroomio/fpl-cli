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

    def test_json_metadata_carries_only_the_warnings_slot(self):
        """metadata.warnings is where every prior-blended score's early-season
        notice lives, so a consumer reads one place whichever command
        produced the score (#206).
        """
        result = _run_targets(["--format", "json"])
        data = json.loads(result.output)
        assert data["metadata"] == {"warnings": []}

    def test_json_agent_failure_exits_nonzero(self):
        agent_result = _make_agent_result(success=False, message="API timeout")
        result = _run_targets(["--format", "json"], agent_result=agent_result)
        assert result.exit_code == 1

    def test_table_output_unchanged(self):
        result = _run_targets()
        assert result.exit_code == 0, result.output
        assert "Salah" in result.output
        assert "Transfer Targets" in result.output

    def test_table_agent_failure_exits_nonzero(self):
        """Table-mode agent failure must exit nonzero, not just print and succeed (#47)."""
        agent_result = _make_agent_result(success=False, message="API timeout")
        result = _run_targets(agent_result=agent_result)
        assert result.exit_code == 1
        assert "Agent failed" in result.output


class TestTargetsReliabilityRendering:
    def test_reliability_shown_as_percentage(self):
        agent_result = _make_agent_result()
        agent_result.data["targets"]["all"][0]["reliability"] = 0.85
        result = _run_targets(agent_result=agent_result)
        assert result.exit_code == 0
        assert "85%" in result.output

    def test_reliability_none_shows_dash(self):
        result = _run_targets()
        assert result.exit_code == 0
        assert "Avail" in result.output


_NOTICE = {
    "code": "early_season_prior_informed",
    "message": "Early-season notice: until GW10, target_score blends ...",
}


def _with_notice():
    agent_result = _make_agent_result()
    agent_result.data = {**agent_result.data, "warnings": [_NOTICE]}
    return agent_result


class TestTargetsEarlySeasonNotice:
    """Before GW10 target_score is prior-informed, and says so (#206).

    The agent decides whether the blend actually ran — only it knows whether
    the priors loaded — so the notice travels in the result and the command
    routes it to whichever channel its reader is on.
    """

    def test_json_moves_the_notice_into_metadata_warnings(self):
        result = _run_targets(["--format", "json"], agent_result=_with_notice())
        data = json.loads(result.output)
        assert data["metadata"]["warnings"] == [_NOTICE]

    def test_json_does_not_leave_the_notice_in_data(self):
        """One home for it, or a consumer has to check two places."""
        result = _run_targets(["--format", "json"], agent_result=_with_notice())
        data = json.loads(result.output)
        assert "warnings" not in data["data"]

    def test_table_mode_prints_the_notice_to_stderr(self):
        """Prose to stderr, so a shell pipeline reading the table is unaffected."""
        result = _run_targets(agent_result=_with_notice())
        assert "Early-season notice" in result.stderr
        assert "Early-season notice" not in result.stdout

    def test_no_notice_after_the_cutoff(self):
        """The agent returns an empty list mid-season; nothing is printed."""
        result = _run_targets()
        assert "Early-season notice" not in result.stderr
