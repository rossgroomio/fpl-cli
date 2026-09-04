"""Tests for the squad CLI command group."""

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from click.testing import CliRunner

from fpl_cli.agents.base import AgentResult, AgentStatus
from fpl_cli.cli import main


@pytest.fixture
def runner():
    return CliRunner()


def _make_agent_result(is_draft=False):
    overview = {"total_points": 1500, "average_form": 5.5}
    if not is_draft:
        overview["team_value"] = 100.0
        overview["bank"] = 5.0

    return AgentResult(
        agent_name="SquadAnalyzerAgent",
        status=AgentStatus.SUCCESS,
        data={
            "squad_overview": overview,
            "position_analysis": {"GK": {"count": 2, "average_form": 4.0}},
            "injury_risks": [],
            "form_analysis": {
                "in_form": [{"name": "Salah", "team": "LIV", "form": 8.0}],
                "out_of_form": [{"name": "Bench", "team": "WHU", "form": 1.0}],
            },
            "recommendations": [],
        },
        message="OK",
    )


def _mock_fpl_client():
    client = MagicMock()
    client.get_players = AsyncMock(return_value=[])
    client.get_next_gameweek = AsyncMock(return_value={"id": 25})
    client.get_manager_picks = AsyncMock(return_value={"picks": [], "active_chip": None})
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)
    return client


def _mock_agent(result):
    agent = MagicMock()
    agent.run = AsyncMock(return_value=result)
    agent.__aenter__ = AsyncMock(return_value=agent)
    agent.__aexit__ = AsyncMock(return_value=False)
    return agent


def _patch_settings(settings):
    """Inject settings at both seams: the group's loader and the command's accessor."""
    return (
        patch("fpl_cli.cli.load_settings", return_value=settings),
        patch("fpl_cli.cli.squad.get_settings", return_value=settings),
    )


class TestSquadGroup:
    """Tests for `fpl squad` command group."""

    def test_squad_no_entry_id(self, runner):
        """Shows error when no classic entry ID configured."""
        p1, p2 = _patch_settings({})
        with p1, p2:
            result = runner.invoke(main, ["squad"])
        assert "classic_entry_id" in result.output

    def test_squad_no_draft_entry_id(self, runner):
        """Shows error when draft format but no draft_entry_id."""
        settings = {"fpl": {"draft_league_id": 42}}
        p1, p2 = _patch_settings(settings)
        with p1, p2:
            result = runner.invoke(main, ["squad"])
        assert "draft_entry_id" in result.output

    def test_squad_classic_success(self, runner):
        """Classic format shows squad health with value/bank."""
        settings = {"fpl": {"classic_entry_id": 12345}}
        agent_result = _make_agent_result(is_draft=False)
        p1, p2 = _patch_settings(settings)

        with p1, p2, \
             patch("fpl_cli.agents.analysis.squad_analyzer.SquadAnalyzerAgent", return_value=_mock_agent(agent_result)), \
             patch("fpl_cli.api.fpl.FPLClient", return_value=_mock_fpl_client()):
            result = runner.invoke(main, ["squad"])

        assert result.exit_code == 0
        assert "Squad Analysis" in result.output
        assert "Team Value" in result.output
        assert "Bank" in result.output

    def test_squad_draft_success(self, runner):
        """Draft format shows squad health without value/bank."""
        settings = {"fpl": {"draft_entry_id": 99, "draft_league_id": 42}}
        agent_result = _make_agent_result(is_draft=True)
        p1, p2 = _patch_settings(settings)

        with p1, p2, \
             patch("fpl_cli.agents.analysis.squad_analyzer.SquadAnalyzerAgent", return_value=_mock_agent(agent_result)), \
             patch("fpl_cli.api.fpl.FPLClient", return_value=_mock_fpl_client()), \
             patch("fpl_cli.agents.common.get_draft_squad_players", new_callable=AsyncMock, return_value=[]):
            result = runner.invoke(main, ["squad"])

        assert result.exit_code == 0
        assert "Squad Analysis" in result.output
        assert "Team Value" not in result.output
        assert "Bank" not in result.output

    def test_squad_draft_flag_in_both_mode(self, runner):
        """--draft flag selects draft squad when both formats configured."""
        settings = {"fpl": {"classic_entry_id": 123, "draft_entry_id": 99, "draft_league_id": 42}}
        agent_result = _make_agent_result(is_draft=True)
        p1, p2 = _patch_settings(settings)

        with p1, p2, \
             patch("fpl_cli.agents.analysis.squad_analyzer.SquadAnalyzerAgent", return_value=_mock_agent(agent_result)), \
             patch("fpl_cli.api.fpl.FPLClient", return_value=_mock_fpl_client()), \
             patch("fpl_cli.agents.common.get_draft_squad_players", new_callable=AsyncMock, return_value=[]):
            result = runner.invoke(main, ["squad", "--draft"])

        assert result.exit_code == 0
        assert "Squad Analysis" in result.output
        assert "Team Value" not in result.output

    def test_squad_subcommand_does_not_trigger_health(self, runner):
        """Invoking a subcommand should not run the health logic."""
        p1, p2 = _patch_settings({"fpl": {"classic_entry_id": 123}})
        with p1, p2:
            result = runner.invoke(main, ["squad", "nonexistent"])
        assert "Squad Analysis" not in result.output


class TestSquadJsonOutput:
    """Tests for `fpl squad --format json`."""

    def test_squad_json_happy_path(self, runner):
        """--format json emits valid JSON with correct structure."""
        settings = {"fpl": {"classic_entry_id": 12345}}
        agent_result = _make_agent_result(is_draft=False)
        p1, p2 = _patch_settings(settings)

        with p1, p2, \
             patch("fpl_cli.agents.analysis.squad_analyzer.SquadAnalyzerAgent", return_value=_mock_agent(agent_result)), \
             patch("fpl_cli.api.fpl.FPLClient", return_value=_mock_fpl_client()):
            result = runner.invoke(main, ["squad", "--format", "json"])

        assert result.exit_code == 0
        payload = json.loads(result.output)
        assert payload["command"] == "squad"
        assert "squad_overview" in payload["data"]

    def test_squad_json_metadata(self, runner):
        """JSON metadata includes gameweek and format."""
        settings = {"fpl": {"classic_entry_id": 12345}}
        agent_result = _make_agent_result(is_draft=False)
        p1, p2 = _patch_settings(settings)

        with p1, p2, \
             patch("fpl_cli.agents.analysis.squad_analyzer.SquadAnalyzerAgent", return_value=_mock_agent(agent_result)), \
             patch("fpl_cli.api.fpl.FPLClient", return_value=_mock_fpl_client()):
            result = runner.invoke(main, ["squad", "--format", "json"])

        payload = json.loads(result.output)
        assert payload["metadata"]["gameweek"] == 25
        assert payload["metadata"]["format"] == "classic"

    def test_squad_json_error(self, runner):
        """Agent failure emits JSON error."""
        settings = {"fpl": {"classic_entry_id": 12345}}
        fail_result = AgentResult(
            agent_name="SquadAnalyzerAgent",
            status=AgentStatus.FAILED,
            data={},
            message="something broke",
        )
        p1, p2 = _patch_settings(settings)

        with p1, p2, \
             patch("fpl_cli.agents.analysis.squad_analyzer.SquadAnalyzerAgent", return_value=_mock_agent(fail_result)), \
             patch("fpl_cli.api.fpl.FPLClient", return_value=_mock_fpl_client()):
            result = runner.invoke(main, ["squad", "--format", "json"])

        assert result.exit_code != 0
        payload = json.loads(result.output)
        assert payload["command"] == "squad"
        assert "error" in payload

    def test_squad_no_format_renders_rich(self, runner):
        """Default invocation (no --format) still renders Rich tables."""
        settings = {"fpl": {"classic_entry_id": 12345}}
        agent_result = _make_agent_result(is_draft=False)
        p1, p2 = _patch_settings(settings)

        with p1, p2, \
             patch("fpl_cli.agents.analysis.squad_analyzer.SquadAnalyzerAgent", return_value=_mock_agent(agent_result)), \
             patch("fpl_cli.api.fpl.FPLClient", return_value=_mock_fpl_client()):
            result = runner.invoke(main, ["squad"])

        assert result.exit_code == 0
        assert "Squad Analysis" in result.output
        # Should not be valid JSON
        with pytest.raises(json.JSONDecodeError):
            json.loads(result.output)


    def test_squad_table_agent_failure_exits_nonzero(self, runner):
        """Table-mode agent failure must exit nonzero, not just print and succeed (#47)."""
        settings = {"fpl": {"classic_entry_id": 12345}}
        fail_result = AgentResult(
            agent_name="SquadAnalyzerAgent",
            status=AgentStatus.FAILED,
            data={},
            message="something broke",
        )
        p1, p2 = _patch_settings(settings)

        with p1, p2, \
             patch("fpl_cli.agents.analysis.squad_analyzer.SquadAnalyzerAgent", return_value=_mock_agent(fail_result)), \
             patch("fpl_cli.api.fpl.FPLClient", return_value=_mock_fpl_client()):
            result = runner.invoke(main, ["squad"])

        assert result.exit_code != 0
        assert "Agent failed" in result.output


class TestSquadPreSeasonNoPicks:
    """Pre-season, the GW1 picks endpoint legitimately 404s until a squad is submitted (#47)."""

    def _mock_fpl_client_404(self):
        import httpx

        client = _mock_fpl_client()
        client.get_next_gameweek = AsyncMock(return_value={"id": 1})  # pre-season: next GW is 1
        request = httpx.Request("GET", "https://fantasy.premierleague.com/api/entry/12345/event/1/picks/")
        response = httpx.Response(404, request=request)
        client.get_manager_picks = AsyncMock(
            side_effect=httpx.HTTPStatusError("Not Found", request=request, response=response)
        )
        return client

    def test_table_mode_shows_friendly_message(self, runner):
        settings = {"fpl": {"classic_entry_id": 12345}}
        p1, p2 = _patch_settings(settings)

        with p1, p2, \
             patch("fpl_cli.api.fpl.FPLClient", return_value=self._mock_fpl_client_404()):
            result = runner.invoke(main, ["squad"])

        assert result.exit_code != 0
        assert "No squad submitted for GW1 yet" in result.output
        assert "Traceback" not in result.output

    def test_json_mode_emits_json_error(self, runner):
        settings = {"fpl": {"classic_entry_id": 12345}}
        p1, p2 = _patch_settings(settings)

        with p1, p2, \
             patch("fpl_cli.api.fpl.FPLClient", return_value=self._mock_fpl_client_404()):
            result = runner.invoke(main, ["squad", "--format", "json"])

        assert result.exit_code != 0
        payload = json.loads(result.output)
        assert payload["command"] == "squad"
        assert "No squad submitted for GW1 yet" in payload["error"]

    def test_non_404_error_still_propagates(self, runner):
        """A genuine (non-404) HTTP error should not be swallowed as 'no squad yet'."""
        import httpx

        settings = {"fpl": {"classic_entry_id": 12345}}
        client = _mock_fpl_client()
        request = httpx.Request("GET", "https://fantasy.premierleague.com/api/entry/12345/event/1/picks/")
        response = httpx.Response(500, request=request)
        client.get_manager_picks = AsyncMock(
            side_effect=httpx.HTTPStatusError("Server Error", request=request, response=response)
        )
        p1, p2 = _patch_settings(settings)

        with p1, p2, patch("fpl_cli.api.fpl.FPLClient", return_value=client):
            result = runner.invoke(main, ["squad"])

        assert result.exit_code != 0
        assert "No squad submitted" not in result.output

    def test_draft_table_mode_shows_friendly_message(self, runner):
        """`fpl squad --draft` before a draft squad has ever been picked (#47 review follow-up)."""
        import httpx

        settings = {"fpl": {"draft_entry_id": 99, "draft_league_id": 42}}
        client = _mock_fpl_client()
        client.get_next_gameweek = AsyncMock(return_value={"id": 1})  # pre-season: next GW is 1

        draft_client = MagicMock()
        draft_client.__aenter__ = AsyncMock(return_value=draft_client)
        draft_client.__aexit__ = AsyncMock(return_value=False)
        request = httpx.Request("GET", "https://draft.premierleague.com/api/entry/99/event/1")
        response = httpx.Response(404, request=request)
        draft_client.get_entry_picks = AsyncMock(
            side_effect=httpx.HTTPStatusError("Not Found", request=request, response=response)
        )
        draft_client.get_bootstrap_static = AsyncMock(return_value={"elements": []})

        p1, p2 = _patch_settings(settings)

        with p1, p2, \
             patch("fpl_cli.api.fpl.FPLClient", return_value=client), \
             patch("fpl_cli.api.fpl_draft.FPLDraftClient", return_value=draft_client):
            result = runner.invoke(main, ["squad", "--draft"])

        assert result.exit_code != 0
        assert "No squad submitted for GW1 yet" in result.output
        assert "Traceback" not in result.output


class TestTeamCommandRetired:
    """Verify `fpl team` no longer exists."""

    def test_team_command_not_registered(self, runner):
        """fpl team should produce an error."""
        result = runner.invoke(main, ["team"])
        assert result.exit_code != 0 or "No such command" in result.output
