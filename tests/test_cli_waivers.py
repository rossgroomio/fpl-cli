"""Tests for `fpl waivers` JSON output."""
from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

from click.testing import CliRunner

from fpl_cli.cli import main


def _make_agent_result(success=True, data=None, message=""):
    result = MagicMock()
    result.success = success
    result.data = data or {
        "waiver_position": 3,
        "total_waiver_teams": 8,
        "squad_weaknesses": [
            {"position": "FWD", "severity": "high", "reason": "No fit strikers"}
        ],
        "recommendations": [
            {
                "priority": 1,
                "target": {"name": "Haaland", "team": "MCI", "position": "FWD", "form": 8.5},
                "drop": {"name": "Wilson", "form": 2.1, "reason": "Injured"},
                "reasons": ["Top form", "Easy fixtures"],
                "exposure": {},
            }
        ],
        "targets_by_position": {
            "FWD": [{"player_name": "Haaland", "team_short": "MCI", "form": 8.5}],
        },
        "recent_releases": [
            {
                "player_name": "Calafiori",
                "team_short": "ARS",
                "form": 2.3,
                "gameweek": 31,
                "availability": "\u2713",
                "injury_news": "",
                "dropped_by": "John Doe",
            },
        ],
    }
    result.message = message
    result.errors = ["fail"] if not success else []
    return result


def _run_waivers(args=None, agent_result=None, has_league_id=True):
    runner = CliRunner()
    if agent_result is None:
        agent_result = _make_agent_result()

    mock_agent = MagicMock()
    mock_agent.run = AsyncMock(return_value=agent_result)
    mock_agent.__aenter__ = AsyncMock(return_value=mock_agent)
    mock_agent.__aexit__ = AsyncMock(return_value=False)

    settings = {"fpl": {"draft_league_id": 123, "draft_entry_id": 456}, "custom_analysis": True} if has_league_id else {"fpl": {}, "custom_analysis": True}

    with patch("fpl_cli.agents.action.waiver.WaiverAgent", return_value=mock_agent), \
         patch("fpl_cli.cli.waivers.load_settings", return_value=settings), \
         patch("fpl_cli.cli._context.load_settings", return_value=settings):
        return runner.invoke(main, ["waivers"] + (args or []))


class TestWaiversJsonFormat:
    def test_json_output_is_valid(self):
        result = _run_waivers(["--format", "json"])
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert data["command"] == "waivers"
        assert isinstance(data["data"], dict)

    def test_json_contains_recommendations(self):
        result = _run_waivers(["--format", "json"])
        data = json.loads(result.output)
        assert "recommendations" in data["data"]
        assert data["data"]["recommendations"][0]["target"]["name"] == "Haaland"

    def test_json_metadata_format_is_draft(self):
        result = _run_waivers(["--format", "json"])
        data = json.loads(result.output)
        assert data["metadata"]["format"] == "draft"

    def test_json_no_league_id_exits_nonzero(self):
        result = _run_waivers(["--format", "json"], has_league_id=False)
        assert result.exit_code == 1

    def test_json_agent_failure_exits_nonzero(self):
        agent_result = _make_agent_result(success=False, message="API timeout")
        result = _run_waivers(["--format", "json"], agent_result=agent_result)
        assert result.exit_code == 1

    def test_json_contains_recent_releases(self):
        result = _run_waivers(["--format", "json"])
        data = json.loads(result.output)
        assert "recent_releases" in data["data"]
        assert data["data"]["recent_releases"][0]["player_name"] == "Calafiori"
        assert data["data"]["recent_releases"][0]["dropped_by"] == "John Doe"

    def test_table_output_unchanged(self):
        result = _run_waivers()
        assert result.exit_code == 0
        assert "Haaland" in result.output

    def test_table_shows_recently_released(self):
        result = _run_waivers()
        assert result.exit_code == 0
        assert "Recently Released" in result.output
        assert "Calafiori" in result.output
        assert "GW31" in result.output
        assert "John Doe" in result.output

    def test_table_shows_injury_and_availability_for_released_player(self):
        agent_result = _make_agent_result()
        agent_result.data["recent_releases"] = [
            {
                "player_name": "Trossard",
                "team_short": "ARS",
                "form": 0.7,
                "gameweek": 31,
                "availability": "75%",
                "injury_news": "Hip injury - 75% chance of playing",
            },
        ]
        result = _run_waivers(agent_result=agent_result)
        assert result.exit_code == 0
        assert "Trossard" in result.output
        assert "(75%)" in result.output
        assert "Hip injury" in result.output

    def test_table_omits_recently_released_when_empty(self):
        agent_result = _make_agent_result()
        agent_result.data["recent_releases"] = []
        result = _run_waivers(agent_result=agent_result)
        assert result.exit_code == 0
        assert "Recently Released" not in result.output
