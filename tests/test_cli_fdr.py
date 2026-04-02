"""Tests for fdr CLI command - draft squad path and JSON output."""

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from click.testing import CliRunner

from fpl_cli.cli.fdr import fdr_command
from tests.conftest import make_draft_player, make_fixture, make_team


def _make_draft_client(picks_data: dict, bootstrap_elements: list, current_gw: int = 25):
    """Build a minimal FPLDraftClient context-manager mock."""
    client = MagicMock()
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)
    client.get_game_state = AsyncMock(return_value={"current_event": current_gw})
    client.get_bootstrap_static = AsyncMock(return_value={"elements": bootstrap_elements})
    client.get_entry_picks = AsyncMock(return_value=picks_data)
    return client


@pytest.fixture
def mock_fixture_agent():
    """Fixture agent mock that returns minimal success data."""
    agent = MagicMock()
    agent.__aenter__ = AsyncMock(return_value=agent)
    agent.__aexit__ = AsyncMock(return_value=False)
    result = MagicMock()
    result.success = True
    result.data = {
        "current_gameweek": 25,
        "easy_fixture_runs": {"overall": [], "for_attackers": [], "for_defenders": []},
        "blank_gameweeks": {},
        "double_gameweeks": {},
        "squad_exposure": [],
    }
    agent.run = AsyncMock(return_value=result)
    return agent


def test_fdr_my_squad_draft_builds_context(mock_fixture_agent):
    """Draft squad dicts are built correctly from bootstrap elements."""
    player_a = make_draft_player(id=10, web_name="Salah", team=14, element_type=3)
    player_b = make_draft_player(id=11, web_name="Haaland", team=13, element_type=4)
    picks_data = {"picks": [{"element": 10}, {"element": 11}]}
    draft_client = _make_draft_client(picks_data, [player_a, player_b], current_gw=25)

    captured_context: list = []  # use list to capture from closure

    original_run = mock_fixture_agent.run

    async def capture_run(context=None):
        captured_context.append(context)
        return await original_run(context=context)

    mock_fixture_agent.run = AsyncMock(side_effect=capture_run)

    with (
        patch("fpl_cli.cli.fdr.is_custom_analysis_enabled", return_value=True),
        patch("fpl_cli.cli.fdr.load_settings", return_value={"fpl": {"draft_entry_id": 999}}),
        patch("fpl_cli.api.fpl_draft.FPLDraftClient", return_value=draft_client),
        patch("fpl_cli.agents.data.fixture.FixtureAgent", return_value=mock_fixture_agent),
        patch("fpl_cli.services.team_ratings.TeamRatingsService") as mock_ratings,
        patch("fpl_cli.services.fixture_predictions.FixturePredictionsService") as mock_preds,
    ):
        mock_ratings.return_value.get_staleness_warning.return_value = None
        mock_preds.return_value.get_predicted_blanks.return_value = []
        mock_preds.return_value.get_predicted_doubles.return_value = []

        runner = CliRunner()
        result = runner.invoke(fdr_command, ["--my-squad", "--draft"])

    assert result.exit_code == 0, result.output
    assert len(captured_context) == 1
    squad = captured_context[0]["squad"]
    assert len(squad) == 2
    assert {"team_id": 14, "element_type": 3, "web_name": "Salah"} in squad
    assert {"team_id": 13, "element_type": 4, "web_name": "Haaland"} in squad


def test_fdr_my_squad_draft_missing_entry_id(mock_fixture_agent):
    """Warning printed and context stays None when draft_entry_id not configured."""
    captured_context: list = []

    original_run = mock_fixture_agent.run

    async def capture_run(context=None):
        captured_context.append(context)
        return await original_run(context=context)

    mock_fixture_agent.run = AsyncMock(side_effect=capture_run)

    with (
        patch("fpl_cli.cli.fdr.is_custom_analysis_enabled", return_value=True),
        patch("fpl_cli.cli.fdr.load_settings", return_value={"fpl": {}}),
        patch("fpl_cli.agents.data.fixture.FixtureAgent", return_value=mock_fixture_agent),
        patch("fpl_cli.services.team_ratings.TeamRatingsService") as mock_ratings,
        patch("fpl_cli.services.fixture_predictions.FixturePredictionsService") as mock_preds,
    ):
        mock_ratings.return_value.get_staleness_warning.return_value = None
        mock_preds.return_value.get_predicted_blanks.return_value = []
        mock_preds.return_value.get_predicted_doubles.return_value = []

        runner = CliRunner()
        result = runner.invoke(fdr_command, ["--my-squad", "--draft"])

    assert result.exit_code == 0, result.output
    assert "draft_entry_id not configured" in result.output
    assert len(captured_context) == 1
    assert captured_context[0] is None


class TestFdrJsonOutput:
    """Tests for --format json on the agent path."""

    def test_json_happy_path(self, mock_fixture_agent):
        """--format json emits valid JSON with command, data, and metadata."""
        mock_fixture_agent.run = AsyncMock(return_value=MagicMock(
            success=True,
            data={
                "current_gameweek": 25,
                "easy_fixture_runs": {"overall": [{"short_name": "ARS", "average_fdr": 2.1}]},
                "blank_gameweeks": {},
                "double_gameweeks": {},
            },
        ))

        with (
            patch("fpl_cli.cli.fdr.is_custom_analysis_enabled", return_value=True),
            patch("fpl_cli.agents.data.fixture.FixtureAgent", return_value=mock_fixture_agent),
        ):
            runner = CliRunner()
            result = runner.invoke(fdr_command, ["--format", "json"])

        assert result.exit_code == 0, result.output
        parsed = json.loads(result.output)
        assert parsed["command"] == "fdr"
        assert "easy_fixture_runs" in parsed["data"]
        assert parsed["metadata"]["mode"] == "difference"
        assert parsed["metadata"]["format"] == "classic"
        assert parsed["metadata"]["gameweek"] == 25

    def test_json_includes_position_in_metadata(self, mock_fixture_agent):
        """Position filter appears in metadata."""
        mock_fixture_agent.run = AsyncMock(return_value=MagicMock(
            success=True,
            data={
                "current_gameweek": 25,
                "easy_fixture_runs": {"for_attackers": []},
            },
        ))

        with (
            patch("fpl_cli.cli.fdr.is_custom_analysis_enabled", return_value=True),
            patch("fpl_cli.agents.data.fixture.FixtureAgent", return_value=mock_fixture_agent),
        ):
            runner = CliRunner()
            result = runner.invoke(fdr_command, ["--format", "json", "--position", "atk"])

        assert result.exit_code == 0, result.output
        parsed = json.loads(result.output)
        assert parsed["metadata"]["position"] == "atk"

    def test_json_agent_failure_exits_nonzero(self, mock_fixture_agent):
        """Agent failure emits JSON error and exits with code 1."""
        mock_fixture_agent.run = AsyncMock(return_value=MagicMock(
            success=False,
            message="Something went wrong",
        ))

        with (
            patch("fpl_cli.cli.fdr.is_custom_analysis_enabled", return_value=True),
            patch("fpl_cli.agents.data.fixture.FixtureAgent", return_value=mock_fixture_agent),
        ):
            runner = CliRunner()
            result = runner.invoke(fdr_command, ["--format", "json"])

        assert result.exit_code == 1
        parsed = json.loads(result.output)
        assert parsed["command"] == "fdr"
        assert "Something went wrong" in parsed["error"]

    def test_table_output_unchanged(self, mock_fixture_agent):
        """Default table output is not affected by JSON support."""
        with (
            patch("fpl_cli.cli.fdr.is_custom_analysis_enabled", return_value=True),
            patch("fpl_cli.agents.data.fixture.FixtureAgent", return_value=mock_fixture_agent),
            patch("fpl_cli.services.team_ratings.TeamRatingsService") as mock_ratings,
            patch("fpl_cli.services.fixture_predictions.FixturePredictionsService") as mock_preds,
        ):
            mock_ratings.return_value.get_staleness_warning.return_value = None
            mock_preds.return_value.get_predicted_blanks.return_value = []
            mock_preds.return_value.get_predicted_doubles.return_value = []

            runner = CliRunner()
            result = runner.invoke(fdr_command, [])

        assert result.exit_code == 0, result.output
        assert "Fixture Analysis" in result.output


# ---------------------------------------------------------------------------
# Helpers for raw FDR (toggle off) path
# ---------------------------------------------------------------------------


def _make_fpl_client_for_raw_fdr(fixtures=None, teams=None, current_gw=25):
    """Build a minimal FPLClient mock for the raw FDR path."""
    client = MagicMock()
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)
    client.get_next_gameweek = AsyncMock(return_value={"id": current_gw})

    if teams is None:
        teams = [
            make_team(id=1, name="Arsenal", short_name="ARS"),
            make_team(id=2, name="Manchester City", short_name="MCI"),
            make_team(id=3, name="Liverpool", short_name="LIV"),
        ]
    client.get_teams = AsyncMock(return_value=teams)

    if fixtures is None:
        fixtures = [
            make_fixture(id=1, gameweek=25, home_team_id=1, away_team_id=2,
                         home_difficulty=2, away_difficulty=4),
            make_fixture(id=2, gameweek=26, home_team_id=3, away_team_id=1,
                         home_difficulty=3, away_difficulty=3),
            make_fixture(id=3, gameweek=25, home_team_id=3, away_team_id=2,
                         home_difficulty=4, away_difficulty=2),
        ]
    client.get_fixtures = AsyncMock(return_value=fixtures)
    return client


class TestFdrCustomAnalysisToggle:
    """Tests for custom_analysis toggle on fdr command."""

    def test_toggle_off_shows_raw_fdr(self):
        """When toggle off, default path shows raw FPL API FDR without ATK/DEF columns."""
        client = _make_fpl_client_for_raw_fdr()
        with (
            patch("fpl_cli.cli.fdr.is_custom_analysis_enabled", return_value=False),
            patch("fpl_cli.api.fpl.FPLClient", return_value=client),
            patch("fpl_cli.services.fixture_predictions.FixturePredictionsService") as mock_preds,
        ):
            mock_preds.return_value.get_predicted_blanks.return_value = []
            mock_preds.return_value.get_predicted_doubles.return_value = []

            runner = CliRunner()
            result = runner.invoke(fdr_command, [])

        assert result.exit_code == 0, result.output
        assert "FPL API Ratings" in result.output
        assert "ATK" not in result.output
        assert "DEF" not in result.output
        # Team names should appear
        assert "ARS" in result.output

    def test_toggle_off_blanks_still_works(self):
        """When toggle off, --blanks works normally (data-only path)."""
        client = _make_fpl_client_for_raw_fdr()
        with (
            patch("fpl_cli.cli.fdr.is_custom_analysis_enabled", return_value=False),
            patch("fpl_cli.api.fpl.FPLClient", return_value=client),
            patch("fpl_cli.services.fixture_predictions.FixturePredictionsService") as mock_preds,
        ):
            mock_preds.return_value.is_stale = False
            mock_preds.return_value.get_predicted_blanks.return_value = []
            mock_preds.return_value.get_predicted_doubles.return_value = []
            mock_preds.return_value.get_metadata.return_value = {"last_updated": "2026-04-01"}

            runner = CliRunner()
            result = runner.invoke(fdr_command, ["--blanks"])

        assert result.exit_code == 0, result.output
        assert "Blank/Double GW" in result.output

    def test_toggle_off_position_atk_shows_error(self):
        """When toggle off, --position atk shows custom analysis required message."""
        with patch("fpl_cli.cli.fdr.is_custom_analysis_enabled", return_value=False):
            runner = CliRunner()
            result = runner.invoke(fdr_command, ["--position", "atk"])

        assert result.exit_code != 0
        assert "custom analysis" in result.output.lower()
        assert "fpl init" in result.output

    def test_toggle_off_position_def_shows_error(self):
        """When toggle off, --position def shows custom analysis required message."""
        with patch("fpl_cli.cli.fdr.is_custom_analysis_enabled", return_value=False):
            runner = CliRunner()
            result = runner.invoke(fdr_command, ["--position", "def"])

        assert result.exit_code != 0
        assert "custom analysis" in result.output.lower()

    def test_toggle_on_full_output(self, mock_fixture_agent):
        """When toggle on, full Bayesian output (no regression)."""
        with (
            patch("fpl_cli.cli.fdr.is_custom_analysis_enabled", return_value=True),
            patch("fpl_cli.agents.data.fixture.FixtureAgent", return_value=mock_fixture_agent),
            patch("fpl_cli.services.team_ratings.TeamRatingsService") as mock_ratings,
            patch("fpl_cli.services.fixture_predictions.FixturePredictionsService") as mock_preds,
        ):
            mock_ratings.return_value.get_staleness_warning.return_value = None
            mock_preds.return_value.get_predicted_blanks.return_value = []
            mock_preds.return_value.get_predicted_doubles.return_value = []

            runner = CliRunner()
            result = runner.invoke(fdr_command, [])

        assert result.exit_code == 0, result.output
        assert "Fixture Analysis" in result.output

    def test_toggle_off_json_raw_fdr(self):
        """When toggle off, JSON output has raw FDR values."""
        client = _make_fpl_client_for_raw_fdr()
        with (
            patch("fpl_cli.cli.fdr.is_custom_analysis_enabled", return_value=False),
            patch("fpl_cli.api.fpl.FPLClient", return_value=client),
            patch("fpl_cli.services.fixture_predictions.FixturePredictionsService") as mock_preds,
        ):
            mock_preds.return_value.get_predicted_blanks.return_value = []
            mock_preds.return_value.get_predicted_doubles.return_value = []

            runner = CliRunner()
            result = runner.invoke(fdr_command, ["--format", "json"])

        assert result.exit_code == 0, result.output
        parsed = json.loads(result.output)
        assert parsed["command"] == "fdr"
        assert parsed["metadata"]["mode"] == "raw"
        assert parsed["metadata"]["custom_analysis"] is False
        assert "easy_fixture_runs" in parsed["data"]
