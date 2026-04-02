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
