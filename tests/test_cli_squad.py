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
    # The entry resolves: a picks 404 on this client means "no squad yet", not
    # a wrong classic_entry_id (#228).
    client.get_manager_entry = AsyncMock(return_value={"id": 12345, "name": "Team"})
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


class TestSquadFormatPinning:
    """`--classic` is the counterpart to `--draft`, so a caller can say which
    roster it wants instead of taking whatever the configured IDs resolve to
    (#228)."""

    DRAFT_ONLY = {"fpl": {"draft_entry_id": 99, "draft_league_id": 42}}

    def test_draft_only_config_still_auto_selects(self, runner):
        """Unadorned `fpl squad` keeps working for a draft-only manager."""
        p1, p2 = _patch_settings(self.DRAFT_ONLY)
        with p1, p2, \
             patch("fpl_cli.agents.analysis.squad_analyzer.SquadAnalyzerAgent",
                   return_value=_mock_agent(_make_agent_result(is_draft=True))), \
             patch("fpl_cli.api.fpl.FPLClient", return_value=_mock_fpl_client()), \
             patch("fpl_cli.agents.common.get_draft_squad_players", new_callable=AsyncMock, return_value=[]):
            result = runner.invoke(main, ["squad", "--format", "json"])

        assert result.exit_code == 0
        assert json.loads(result.stdout)["metadata"]["format"] == "draft"

    def test_classic_flag_errors_instead_of_returning_the_draft_squad(self, runner):
        """The failure #228 names: asking for classic and getting another league."""
        p1, p2 = _patch_settings(self.DRAFT_ONLY)
        with p1, p2:
            result = runner.invoke(main, ["squad", "--classic", "--format", "json"])

        assert result.exit_code == 1
        payload = json.loads(result.stdout)
        assert payload["command"] == "squad"
        assert "classic_entry_id is not set" in payload["error"]

    def test_classic_flag_pins_the_format_when_both_are_configured(self, runner):
        settings = {"fpl": {"classic_entry_id": 123, "draft_entry_id": 99, "draft_league_id": 42}}
        p1, p2 = _patch_settings(settings)
        with p1, p2, \
             patch("fpl_cli.agents.analysis.squad_analyzer.SquadAnalyzerAgent",
                   return_value=_mock_agent(_make_agent_result())), \
             patch("fpl_cli.api.fpl.FPLClient", return_value=_mock_fpl_client()):
            result = runner.invoke(main, ["squad", "--classic", "--format", "json"])

        assert result.exit_code == 0
        assert json.loads(result.stdout)["metadata"]["format"] == "classic"

    def test_both_flags_is_an_error_envelope(self, runner):
        p1, p2 = _patch_settings({"fpl": {"classic_entry_id": 123}})
        with p1, p2:
            result = runner.invoke(main, ["squad", "--classic", "--draft", "--format", "json"])

        assert result.exit_code == 1
        assert "mutually exclusive" in json.loads(result.stdout)["error"]

    def test_group_level_classic_flag_reaches_the_subcommand(self, runner):
        """`fpl squad --classic grid` is the same request as `fpl squad grid
        --classic`; the group callback returns before resolving the format, so
        the flag used to be parsed and then dropped (#259 review)."""
        p1, p2 = _patch_settings(self.DRAFT_ONLY)
        with p1, p2:
            result = runner.invoke(main, ["squad", "--classic", "grid", "--format", "json"])

        assert result.exit_code == 1
        payload = json.loads(result.stdout)
        assert payload["command"] == "plan-grid"
        assert "classic_entry_id is not set" in payload["error"]

    def test_group_level_draft_flag_reaches_the_subcommand(self, runner):
        """The mirror case: `--draft` before `grid` on a classic-only config
        must reach the subcommand and be reported, not be dropped."""
        p1, p2 = _patch_settings({"fpl": {"classic_entry_id": 123}})
        with p1, p2:
            result = runner.invoke(main, ["squad", "--draft", "grid", "--format", "json"])

        assert result.exit_code == 1
        assert "draft_entry_id is not set" in json.loads(result.stdout)["error"]

    def test_contradictory_flags_before_a_subcommand_error_in_the_subcommands_format(self, runner):
        """The group's own `--format` is not the subcommand's, so the check has
        to happen where the reader's format is known -- reporting it from the
        group put prose on stderr and nothing on stdout (#259 review)."""
        p1, p2 = _patch_settings({"fpl": {"classic_entry_id": 123}})
        with p1, p2:
            result = runner.invoke(main, ["squad", "--classic", "--draft", "grid", "--format", "json"])

        assert result.exit_code == 1
        payload = json.loads(result.stdout)
        assert payload["command"] == "plan-grid"
        assert "mutually exclusive" in payload["error"]

    def test_grid_classic_flag_errors_on_a_draft_only_config(self, runner):
        p1, p2 = _patch_settings(self.DRAFT_ONLY)
        with p1, p2:
            result = runner.invoke(main, ["squad", "grid", "--classic", "--format", "json"])

        assert result.exit_code == 1
        payload = json.loads(result.stdout)
        assert payload["command"] == "plan-grid"
        assert "classic_entry_id is not set" in payload["error"]

    def test_table_heading_names_the_format(self, runner):
        """Table mode has no `metadata.format` to read, so the panel says it."""
        p1, p2 = _patch_settings(self.DRAFT_ONLY)
        with p1, p2, \
             patch("fpl_cli.agents.analysis.squad_analyzer.SquadAnalyzerAgent",
                   return_value=_mock_agent(_make_agent_result(is_draft=True))), \
             patch("fpl_cli.api.fpl.FPLClient", return_value=_mock_fpl_client()), \
             patch("fpl_cli.agents.common.get_draft_squad_players", new_callable=AsyncMock, return_value=[]):
            result = runner.invoke(main, ["squad"])

        assert result.exit_code == 0
        assert "Squad Analysis (Draft)" in result.output


class TestSquadEntryDoesNotExist:
    """A 404 on the picks endpoint is two different problems (#228)."""

    def _client(self, *, entry_exists: bool):
        import httpx

        client = _mock_fpl_client()
        client.get_next_gameweek = AsyncMock(return_value={"id": 3})
        picks_request = httpx.Request(
            "GET", "https://fantasy.premierleague.com/api/entry/999999999/event/2/picks/"
        )
        client.get_manager_picks = AsyncMock(side_effect=httpx.HTTPStatusError(
            "Not Found", request=picks_request,
            response=httpx.Response(404, request=picks_request),
        ))
        if not entry_exists:
            entry_request = httpx.Request(
                "GET", "https://fantasy.premierleague.com/api/entry/999999999/"
            )
            client.get_manager_entry = AsyncMock(side_effect=httpx.HTTPStatusError(
                "Not Found", request=entry_request,
                response=httpx.Response(404, request=entry_request),
            ))
        return client

    def test_nonexistent_entry_is_not_reported_as_pre_deadline(self, runner):
        settings = {"fpl": {"classic_entry_id": 999999999}}
        p1, p2 = _patch_settings(settings)
        with p1, p2, patch("fpl_cli.api.fpl.FPLClient", return_value=self._client(entry_exists=False)):
            result = runner.invoke(main, ["squad", "--format", "json"])

        assert result.exit_code == 1
        error = json.loads(result.stdout)["error"]
        assert "No FPL entry 999999999 exists" in error
        assert "reissued" in error
        assert "No squad submitted" not in error

    def test_live_entry_still_reads_as_no_squad_yet(self, runner):
        settings = {"fpl": {"classic_entry_id": 12345}}
        p1, p2 = _patch_settings(settings)
        with p1, p2, patch("fpl_cli.api.fpl.FPLClient", return_value=self._client(entry_exists=True)):
            result = runner.invoke(main, ["squad", "--format", "json"])

        assert result.exit_code == 1
        assert "No squad submitted for GW2 yet" in json.loads(result.stdout)["error"]

    def test_unreachable_entry_endpoint_does_not_condemn_the_id(self, runner):
        """A 503 on `entry/<id>/` proves nothing, so the wording must not change."""
        import httpx

        settings = {"fpl": {"classic_entry_id": 12345}}
        client = self._client(entry_exists=True)
        request = httpx.Request("GET", "https://fantasy.premierleague.com/api/entry/12345/")
        client.get_manager_entry = AsyncMock(side_effect=httpx.HTTPStatusError(
            "Service Unavailable", request=request,
            response=httpx.Response(503, request=request),
        ))
        p1, p2 = _patch_settings(settings)
        with p1, p2, patch("fpl_cli.api.fpl.FPLClient", return_value=client):
            result = runner.invoke(main, ["squad", "--format", "json"])

        assert result.exit_code == 1
        error = json.loads(result.stdout)["error"]
        assert "No squad submitted for GW2 yet" in error
        assert "does not exist" not in error


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


class TestGridSharesTheDiagnosis:
    """`squad grid` must not be a third wording of the same 404 (#259 review)."""

    SETTINGS = {"fpl": {"classic_entry_id": 999999999}}

    def _client(self, *, entry_exists: bool):
        import httpx

        client = _mock_fpl_client()
        client.get_next_gameweek = AsyncMock(return_value={"id": 3})
        client.get_current_gameweek = AsyncMock(return_value={"id": 2})
        client.get_teams = AsyncMock(return_value=[])
        client.get_fixtures = AsyncMock(return_value=[])

        def _not_found(path):
            request = httpx.Request("GET", f"https://fantasy.premierleague.com/api{path}")
            return httpx.HTTPStatusError(
                "Not Found", request=request, response=httpx.Response(404, request=request)
            )

        client.get_manager_picks = AsyncMock(side_effect=_not_found("/entry/999999999/event/3/picks/"))
        if not entry_exists:
            client.get_manager_entry = AsyncMock(side_effect=_not_found("/entry/999999999/"))
        return client

    def _run(self, runner, client):
        p1, p2 = _patch_settings(self.SETTINGS)
        ratings = MagicMock()
        ratings.ensure_fresh = AsyncMock(return_value=None)
        with p1, p2, \
             patch("fpl_cli.cli._plan_grid.get_settings", return_value=self.SETTINGS), \
             patch("fpl_cli.api.fpl.FPLClient", return_value=client), \
             patch("fpl_cli.services.team_ratings.TeamRatingsService", return_value=ratings):
            return runner.invoke(main, ["squad", "grid", "--format", "json"])

    def test_stale_entry_gets_the_same_message_as_fpl_squad(self, runner):
        result = self._run(runner, self._client(entry_exists=False))

        assert result.exit_code == 1
        error = json.loads(result.stdout)["error"]
        assert "No FPL entry 999999999 exists" in error
        assert "Could not fetch squad" not in error

    def test_live_entry_still_reads_as_no_squad_yet(self, runner):
        result = self._run(runner, self._client(entry_exists=True))

        assert result.exit_code == 1
        assert "No squad submitted for GW" in json.loads(result.stdout)["error"]
