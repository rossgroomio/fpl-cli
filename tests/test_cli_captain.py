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


def _run_captain(args=None, agent_result=None, settings=None):
    runner = CliRunner()
    if agent_result is None:
        agent_result = _make_agent_result()
    if settings is None:
        settings = {"fpl": {"classic_entry_id": 123}, "custom_analysis": True}

    mock_agent = MagicMock()
    mock_agent.run = AsyncMock(return_value=agent_result)
    mock_agent.__aenter__ = AsyncMock(return_value=mock_agent)
    mock_agent.__aexit__ = AsyncMock(return_value=False)

    # Both seams: the group's loader supplies the command's settings, the
    # `_context` one opens the experimental gate while click is still parsing.
    with patch("fpl_cli.agents.analysis.captain.CaptainAgent", return_value=mock_agent), \
         patch("fpl_cli.cli.load_settings", return_value=settings), \
         patch("fpl_cli.cli._context.load_settings", return_value=settings):
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

    def test_table_agent_failure_exits_nonzero(self):
        """Table-mode agent failure must exit nonzero, not just print and succeed (#47)."""
        agent_result = _make_agent_result(success=False, message="API timeout")
        result = _run_captain(agent_result=agent_result)
        assert result.exit_code == 1
        assert "Agent failed" in result.output


class TestCaptainRequiresAnEntryId:
    """Without `classic_entry_id`, `fpl captain` used to answer with the global
    top 30 and exit 0 -- a plausible list that never touched the squad the
    caller asked about (#228)."""

    NO_ID = {"fpl": {"classic_league_id": 42}, "custom_analysis": True}

    def test_missing_id_is_an_error_not_a_global_list(self):
        result = _run_captain(settings=self.NO_ID)
        assert result.exit_code == 1
        assert "classic_entry_id is not set" in result.output

    def test_error_names_the_global_flag_as_the_alternative(self):
        result = _run_captain(settings=self.NO_ID)
        assert "--global" in result.output

    def test_missing_id_emits_the_json_envelope(self):
        result = _run_captain(["--format", "json"], settings=self.NO_ID)
        assert result.exit_code == 1
        payload = json.loads(result.stdout)
        assert payload["command"] == "captain"
        assert "classic_entry_id is not set" in payload["error"]

    def test_global_flag_needs_no_id(self):
        result = _run_captain(["--global", "--format", "json"], settings=self.NO_ID)
        assert result.exit_code == 0, result.output
        assert json.loads(result.stdout)["command"] == "captain"


class TestCaptainJsonMetadata:
    def test_metadata_reports_my_squad_mode(self):
        result = _run_captain(["--format", "json"])
        assert json.loads(result.stdout)["metadata"]["my_squad_mode"] is True

    def test_metadata_reports_a_global_list_as_such(self):
        data = dict(_make_agent_result().data)
        data["my_squad_mode"] = False
        result = _run_captain(["--format", "json"], agent_result=_make_agent_result(data=data))
        assert json.loads(result.stdout)["metadata"]["my_squad_mode"] is False

    def test_agent_warnings_move_to_metadata(self):
        data = dict(_make_agent_result().data)
        data["warnings"] = [{"code": "captain_global_fallback", "message": "not yours"}]
        result = _run_captain(["--format", "json"], agent_result=_make_agent_result(data=data))
        payload = json.loads(result.stdout)
        assert payload["metadata"]["warnings"][0]["code"] == "captain_global_fallback"
        assert "warnings" not in payload["data"]

    def test_agent_warnings_reach_stderr_in_table_mode(self):
        data = dict(_make_agent_result().data)
        data["warnings"] = [{"code": "captain_global_fallback", "message": "no squad to rank"}]
        result = _run_captain(agent_result=_make_agent_result(data=data))
        assert "no squad to rank" in result.stderr
