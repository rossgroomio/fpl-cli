"""Tests for `fpl differentials` JSON output."""
from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

from click.testing import CliRunner

from fpl_cli.cli import main


def _make_stats_result(success=True, data=None, message=""):
    result = MagicMock()
    result.success = success
    result.data = data or {
        "differentials": {
            "elite": [
                {
                    "player_name": "Isak",
                    "team_short": "NEW",
                    "position": "FWD",
                    "ownership": 3.2,
                    "xGI_per_90": 0.75,
                    "matchup_score": 7.5,
                    "next_opponent": "SOU",
                    "differential_score": 8.1,
                }
            ],
            "by_position": {
                "FWD": [{"player_name": "Isak", "ownership": 3.2}],
            },
        },
    }
    result.message = message
    result.errors = ["Stats error"] if not success else []
    return result


def _make_captain_result(success=True, data=None, message=""):
    result = MagicMock()
    result.success = success
    result.data = data or {
        "differential_picks": [
            {
                "player_name": "Isak",
                "team_short": "NEW",
                "ownership": 3.2,
                "fixtures": [{"opponent": "SOU", "is_home": True}],
                "captain_score": 7.0,
            }
        ],
    }
    result.message = message
    result.errors = ["Captain error"] if not success else []
    return result


def _mock_agent(result):
    agent = MagicMock()
    agent.run = AsyncMock(return_value=result)
    agent.__aenter__ = AsyncMock(return_value=agent)
    agent.__aexit__ = AsyncMock(return_value=False)
    return agent


def _mock_fpl_client(gameweek_id=30):
    client = MagicMock()
    client.get_next_gameweek = AsyncMock(return_value={"id": gameweek_id})
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)
    return client


def _run_differentials(
    args=None,
    stats_result=None,
    captain_result=None,
    fpl_client=None,
):
    runner = CliRunner()
    if stats_result is None:
        stats_result = _make_stats_result()
    if captain_result is None:
        captain_result = _make_captain_result()
    if fpl_client is None:
        fpl_client = _mock_fpl_client()

    mock_stats = _mock_agent(stats_result)
    mock_captain = _mock_agent(captain_result)

    with (
        patch("fpl_cli.agents.analysis.stats.StatsAgent", return_value=mock_stats),
        patch("fpl_cli.agents.analysis.captain.CaptainAgent", return_value=mock_captain),
        patch("fpl_cli.api.fpl.FPLClient", return_value=fpl_client),
        patch("fpl_cli.cli._context.load_settings", return_value={"custom_analysis": True}),
    ):
        return runner.invoke(main, ["differentials"] + (args or []))


class TestDifferentialsJsonFormat:
    def test_json_envelope_structure(self):
        result = _run_differentials(["--format", "json"])
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert data["command"] == "differentials"
        assert isinstance(data["data"], dict)
        assert isinstance(data["metadata"], dict)

    def test_json_contains_both_keys(self):
        result = _run_differentials(["--format", "json"])
        data = json.loads(result.output)
        assert "differentials" in data["data"]
        assert "differential_captains" in data["data"]

    def test_json_metadata_has_gameweek(self):
        result = _run_differentials(["--format", "json"])
        data = json.loads(result.output)
        assert data["metadata"]["gameweek"] == 30

    def test_json_stats_failure_exits_nonzero(self):
        stats_result = _make_stats_result(success=False, message="API timeout")
        result = _run_differentials(["--format", "json"], stats_result=stats_result)
        assert result.exit_code == 1

    def test_json_captain_failure_graceful(self):
        captain_result = _make_captain_result(success=False, message="Captain failed")
        result = _run_differentials(["--format", "json"], captain_result=captain_result)
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert "differentials" in data["data"]
        assert "differential_captains" not in data["data"]

    def test_table_output_unchanged(self):
        result = _run_differentials()
        assert result.exit_code == 0, result.output
        assert "Isak" in result.output
        assert "Differential Picks" in result.output

    def test_table_stats_failure_exits_nonzero(self):
        """Table-mode stats failure must exit nonzero, not just print and succeed (#47)."""
        stats_result = _make_stats_result(success=False, message="API timeout")
        result = _run_differentials(stats_result=stats_result)
        assert result.exit_code == 1
        assert "Agent failed" in result.output


class TestDifferentialsReliabilityRendering:
    def test_reliability_shown_as_percentage(self):
        stats_result = _make_stats_result()
        stats_result.data["differentials"]["elite"][0]["reliability"] = 0.72
        result = _run_differentials(stats_result=stats_result)
        assert result.exit_code == 0
        assert "72%" in result.output

    def test_reliability_none_shows_dash(self):
        result = _run_differentials()
        assert result.exit_code == 0
        assert "Avail" in result.output


_NOTICE = {
    "code": "early_season_prior_informed",
    "message": "Early-season notice: until GW10, differential_score blends ...",
}


def _with_notice():
    stats_result = _make_stats_result()
    stats_result.data = {**stats_result.data, "warnings": [_NOTICE]}
    return stats_result


class TestDifferentialsEarlySeasonNotice:
    """Before GW10 differential_score is prior-informed, and says so (#206).

    Only the agent knows whether the priors actually loaded, so it decides
    the notice and the command routes it to its reader's channel.
    """

    def test_json_moves_the_notice_into_metadata_warnings(self):
        result = _run_differentials(["--format", "json"], stats_result=_with_notice())
        data = json.loads(result.output)
        assert data["metadata"]["warnings"] == [_NOTICE]

    def test_json_keeps_the_gameweek_alongside_it(self):
        """The notice is added to metadata, not substituted for what was there."""
        result = _run_differentials(["--format", "json"], stats_result=_with_notice())
        assert "gameweek" in json.loads(result.output)["metadata"]

    def test_json_does_not_leave_the_notice_in_data(self):
        result = _run_differentials(["--format", "json"], stats_result=_with_notice())
        assert "warnings" not in json.loads(result.output)["data"]

    def test_table_mode_prints_the_notice_to_stderr(self):
        result = _run_differentials(stats_result=_with_notice())
        assert "Early-season notice" in result.stderr
        assert "Early-season notice" not in result.stdout

    def test_no_notice_after_the_cutoff(self):
        assert "Early-season notice" not in _run_differentials().stderr


class TestDifferentialsEmptyResult:
    """An empty analysis says which of the floor and the data caused it (#227)."""

    @staticmethod
    def _empty_result():
        return _make_stats_result(data={
            "differentials": {"elite": [], "by_position": {}},
            "window_label": "whole season",
            "gameweeks_played": 0,
            "min_minutes": 60,
            "qualified_players": 0,
            "empty_reason": {
                "code": "no_minutes_played",
                "message": "No player has recorded any minutes in whole season.",
            },
        })

    def test_table_mode_explains_the_empty_analysis(self):
        result = _run_differentials(stats_result=self._empty_result())
        assert result.exit_code == 0, result.output
        assert "No players to analyse" in result.output
        assert "No player has recorded any minutes" in result.output

    def test_json_payload_carries_the_reason(self):
        """The JSON payload is rebuilt from scratch, so this has to be carried across."""
        result = _run_differentials(["--format", "json"], stats_result=self._empty_result())
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)["data"]
        assert data["empty_reason"]["code"] == "no_minutes_played"

    def test_a_normal_run_carries_a_null_reason(self):
        result = _run_differentials(["--format", "json"])
        data = json.loads(result.output)["data"]
        assert data["empty_reason"] is None
