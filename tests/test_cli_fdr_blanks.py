"""Tests for fpl fdr --blanks flag."""

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from click.testing import CliRunner

from fpl_cli.cli import main


@pytest.fixture
def runner():
    return CliRunner()


@pytest.fixture
def mock_fpl_client():
    """Mock FPLClient with minimal fixture data."""
    client = AsyncMock()
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)
    client.get_next_gameweek = AsyncMock(return_value={"id": 30})

    # 2 teams, fixtures for GW30 only (team 1 plays, team 2 blanks)
    team1 = MagicMock(id=1, name="Arsenal", short_name="ARS")
    team2 = MagicMock(id=2, name="Man City", short_name="MCI")
    client.get_teams = AsyncMock(return_value=[team1, team2])

    fixture = MagicMock(
        gameweek=30, home_team_id=1, away_team_id=3,
        home_difficulty=2, away_difficulty=4,
    )
    client.get_fixtures = AsyncMock(return_value=[fixture])

    return client


class TestBlanksFlag:
    def test_blanks_shows_blank_gameweeks(self, runner, mock_fpl_client, tmp_path):
        config_path = tmp_path / "predictions.yaml"
        config_path.write_text(
            "metadata:\n  last_updated: '2026-03-19'\n"
            "predicted_blanks:\n"
            "- gameweek: 31\n  teams: [ARS, MCI]\n  reason: FA Cup\n  confidence: high\n"
            "predicted_doubles: []\n",
            encoding="utf-8",
        )

        with (
            patch("fpl_cli.api.fpl.FPLClient", return_value=mock_fpl_client),
            patch(
                "fpl_cli.services.fixture_predictions.CONFIG_FILE",
                config_path,
            ),
        ):
            result = runner.invoke(main, ["fdr", "--blanks"], catch_exceptions=False)

        assert result.exit_code == 0
        assert "Blank" in result.output
        assert "2026-03-19" in result.output

    def test_blanks_with_from_to_gw(self, runner, mock_fpl_client, tmp_path):
        config_path = tmp_path / "predictions.yaml"
        config_path.write_text(
            "metadata:\n  last_updated: '2026-03-19'\n"
            "predicted_blanks: []\npredicted_doubles: []\n",
            encoding="utf-8",
        )

        with (
            patch("fpl_cli.api.fpl.FPLClient", return_value=mock_fpl_client),
            patch(
                "fpl_cli.services.fixture_predictions.CONFIG_FILE",
                config_path,
            ),
        ):
            result = runner.invoke(
                main, ["fdr", "--blanks", "--from-gw", "33", "--to-gw", "38"],
                catch_exceptions=False,
            )

        assert result.exit_code == 0
        assert "GW33-38" in result.output

    def test_blanks_with_my_squad_raises_usage_error(self, runner):
        result = runner.invoke(main, ["fdr", "--blanks", "--my-squad"])
        assert result.exit_code != 0
        assert "--blanks cannot be combined with --my-squad" in result.output

    def test_blanks_with_mode_raises_usage_error(self, runner):
        result = runner.invoke(main, ["fdr", "--blanks", "--mode", "opponent"])
        assert result.exit_code != 0
        assert "--blanks cannot be combined with --mode" in result.output

    def test_blanks_with_position_raises_usage_error(self, runner):
        result = runner.invoke(main, ["fdr", "--blanks", "--position", "atk"])
        assert result.exit_code != 0
        assert "--blanks cannot be combined with --position" in result.output

    def test_blanks_filters_past_gw_predictions(self, runner, mock_fpl_client, tmp_path):
        """Predictions before current GW (30) should be filtered out."""
        config_path = tmp_path / "predictions.yaml"
        config_path.write_text(
            "metadata:\n  last_updated: '2026-03-19'\n"
            "predicted_blanks:\n"
            "- gameweek: 28\n  teams: [ARS]\n  reason: Past\n  confidence: high\n"
            "- gameweek: 31\n  teams: [MCI]\n  reason: Future\n  confidence: medium\n"
            "predicted_doubles: []\n",
            encoding="utf-8",
        )

        with (
            patch("fpl_cli.api.fpl.FPLClient", return_value=mock_fpl_client),
            patch(
                "fpl_cli.services.fixture_predictions.CONFIG_FILE",
                config_path,
            ),
        ):
            result = runner.invoke(main, ["fdr", "--blanks"], catch_exceptions=False)

        assert result.exit_code == 0
        # GW28 (past) should not appear, GW31 (future) should
        assert "GW28" not in result.output
        assert "GW31" in result.output

    def test_blanks_json_happy_path(self, runner, mock_fpl_client, tmp_path):
        """--blanks --format json emits valid JSON with blanks/doubles structure."""
        config_path = tmp_path / "predictions.yaml"
        config_path.write_text(
            "metadata:\n  last_updated: '2026-03-19'\n"
            "predicted_blanks:\n"
            "- gameweek: 31\n  teams: [ARS, MCI]\n  reason: FA Cup\n  confidence: high\n"
            "predicted_doubles:\n"
            "- gameweek: 33\n  teams: [LIV, CHE]\n  reason: Rescheduled\n  confidence: medium\n",
            encoding="utf-8",
        )

        with (
            patch("fpl_cli.api.fpl.FPLClient", return_value=mock_fpl_client),
            patch(
                "fpl_cli.services.fixture_predictions.CONFIG_FILE",
                config_path,
            ),
        ):
            result = runner.invoke(
                main, ["fdr", "--blanks", "--format", "json"],
                catch_exceptions=False,
            )

        assert result.exit_code == 0, result.output
        parsed = json.loads(result.output)
        assert parsed["command"] == "fdr"
        assert parsed["metadata"]["mode"] == "blanks"
        assert parsed["metadata"]["gameweek"] == 30
        data = parsed["data"]
        assert "confirmed_blanks" in data
        assert "predicted_blanks" in data
        assert "confirmed_doubles" in data
        assert "predicted_doubles" in data
        # Check predicted blanks content
        assert len(data["predicted_blanks"]) == 1
        assert data["predicted_blanks"][0]["gw"] == 31
        assert data["predicted_blanks"][0]["confidence"] == "high"
        # Check predicted doubles content
        assert len(data["predicted_doubles"]) == 1
        assert data["predicted_doubles"][0]["gw"] == 33

    def test_blanks_stale_predictions_shows_warning(self, runner, mock_fpl_client, tmp_path):
        """Stale predictions (previous season) show warning with 'unknown' date."""
        config_path = tmp_path / "predictions.yaml"
        config_path.write_text(
            "metadata:\n  last_updated: '2024-03-19'\n"
            "predicted_blanks:\n"
            "- gameweek: 31\n  teams: [ARS]\n  confidence: high\n"
            "predicted_doubles: []\n",
            encoding="utf-8",
        )

        with (
            patch("fpl_cli.api.fpl.FPLClient", return_value=mock_fpl_client),
            patch(
                "fpl_cli.services.fixture_predictions.CONFIG_FILE",
                config_path,
            ),
        ):
            result = runner.invoke(main, ["fdr", "--blanks"], catch_exceptions=False)

        assert result.exit_code == 0
        assert "may be stale" in result.output
        assert "unknown" in result.output

    def test_blanks_json_metadata_has_gw_range(self, runner, mock_fpl_client, tmp_path):
        """--blanks --format json metadata includes from_gw and to_gw."""
        config_path = tmp_path / "predictions.yaml"
        config_path.write_text(
            "metadata:\n  last_updated: '2026-03-19'\n"
            "predicted_blanks: []\npredicted_doubles: []\n",
            encoding="utf-8",
        )

        with (
            patch("fpl_cli.api.fpl.FPLClient", return_value=mock_fpl_client),
            patch(
                "fpl_cli.services.fixture_predictions.CONFIG_FILE",
                config_path,
            ),
        ):
            result = runner.invoke(
                main, ["fdr", "--blanks", "--format", "json", "--from-gw", "33", "--to-gw", "38"],
                catch_exceptions=False,
            )

        assert result.exit_code == 0, result.output
        parsed = json.loads(result.output)
        assert parsed["metadata"]["from_gw"] == 33
        assert parsed["metadata"]["to_gw"] == 38
