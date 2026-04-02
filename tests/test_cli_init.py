"""Tests for fpl init command."""

from unittest.mock import MagicMock, patch

import httpx
import pytest
from click.testing import CliRunner
from ruamel.yaml import YAML

from fpl_cli.cli.init import init_command

_DRAFT_LEAGUE_RESPONSE = {"entry": {"league_set": [456]}}


@pytest.fixture
def settings_file(tmp_path):
    """Provide a temp settings file path and patch SETTINGS_FILE to use it."""
    path = tmp_path / "settings.yaml"
    env_path = tmp_path / ".env"
    with patch("fpl_cli.cli.init._settings_file", return_value=path), \
         patch("fpl_cli.cli.init._env_file", return_value=env_path), \
         patch("fpl_cli.cli.init._keyring_available", return_value=False):
        yield path


def _mock_draft_response(json_data=_DRAFT_LEAGUE_RESPONSE, status_code=200):
    """Create a mock httpx.Response."""
    resp = MagicMock(spec=httpx.Response)
    resp.status_code = status_code
    resp.json.return_value = json_data
    resp.raise_for_status.return_value = None
    if status_code >= 400:
        resp.raise_for_status.side_effect = httpx.HTTPStatusError(
            "error", request=MagicMock(), response=resp
        )
    return resp


class TestInitCreateNew:
    def test_creates_classic_config(self, settings_file):
        runner = CliRunner()
        result = runner.invoke(init_command, input="classic\nN\n123456\n234567\nN\nN\nN\nN\n")
        assert result.exit_code == 0

        yaml = YAML(typ="rt")
        data = yaml.load(settings_file.read_text(encoding="utf-8"))
        assert data["fpl"]["classic_entry_id"] == 123456
        assert data["fpl"]["classic_league_id"] == 234567
        assert "draft_league_id" not in data["fpl"]
        assert "fines" not in data

    @patch("fpl_cli.cli.init.httpx.get", return_value=_mock_draft_response())
    def test_creates_draft_config(self, _mock_get, settings_file):
        runner = CliRunner()
        # Only entry ID needed - league ID auto-derived
        result = runner.invoke(init_command, input="draft\n78901\nN\nN\nN\nN\n")
        assert result.exit_code == 0
        assert "Found draft league: 456" in result.output

        yaml = YAML(typ="rt")
        data = yaml.load(settings_file.read_text(encoding="utf-8"))
        assert data["fpl"]["draft_entry_id"] == 78901
        assert data["fpl"]["draft_league_id"] == 456
        assert "classic_entry_id" not in data["fpl"]

    @patch("fpl_cli.cli.init.httpx.get", return_value=_mock_draft_response())
    def test_creates_both_config(self, _mock_get, settings_file):
        runner = CliRunner()
        result = runner.invoke(init_command, input="both\nN\n123456\n234567\n78901\nN\nN\nN\nN\n")
        assert result.exit_code == 0

        yaml = YAML(typ="rt")
        data = yaml.load(settings_file.read_text(encoding="utf-8"))
        assert data["fpl"]["classic_entry_id"] == 123456
        assert data["fpl"]["draft_league_id"] == 456


class TestInitCustomAnalysisTier:
    def test_opt_in_sets_custom_analysis_true(self, settings_file):
        runner = CliRunner()
        # classic, net pts N, IDs, custom analysis Y then Y, AI N, League N, Fines N
        result = runner.invoke(init_command, input="classic\nN\n123\n234\nY\nY\nN\nN\nN\n")
        assert result.exit_code == 0, result.output

        yaml = YAML(typ="rt")
        data = yaml.load(settings_file.read_text(encoding="utf-8"))
        assert data["custom_analysis"] is True

    def test_opt_out_sets_custom_analysis_false(self, settings_file):
        runner = CliRunner()
        # custom analysis Y (enter tier) then N (disable)
        result = runner.invoke(init_command, input="classic\nN\n123\n234\nY\nN\nN\nN\nN\n")
        assert result.exit_code == 0, result.output

        yaml = YAML(typ="rt")
        data = yaml.load(settings_file.read_text(encoding="utf-8"))
        assert data["custom_analysis"] is False

    def test_skip_tier_preserves_existing_value(self, settings_file):
        yaml = YAML(typ="rt")
        existing = {"fpl": {"classic_entry_id": 123, "classic_league_id": 234}, "custom_analysis": True}
        yaml.dump(existing, settings_file)

        runner = CliRunner()
        # Skip custom analysis tier (N at the reconfigure prompt)
        result = runner.invoke(init_command, input="classic\nN\n\n\nN\nN\nN\nN\n")
        assert result.exit_code == 0, result.output

        data = yaml.load(settings_file.read_text(encoding="utf-8"))
        assert data["custom_analysis"] is True

    def test_summary_table_shows_custom_analysis(self, settings_file):
        runner = CliRunner()
        result = runner.invoke(init_command, input="classic\nN\n123\n234\nN\nN\nN\nN\n")
        assert result.exit_code == 0, result.output
        assert "Custom Analysis" in result.output

    def test_rerun_shows_current_state(self, settings_file):
        yaml = YAML(typ="rt")
        existing = {"fpl": {"classic_entry_id": 123, "classic_league_id": 234}, "custom_analysis": True}
        yaml.dump(existing, settings_file)

        runner = CliRunner()
        result = runner.invoke(init_command, input="classic\nN\n\n\nN\nN\nN\nN\n")
        assert result.exit_code == 0, result.output
        assert "enabled" in result.output

class TestDraftLeagueAutoDerivation:
    @patch("fpl_cli.cli.init.httpx.get")
    def test_falls_back_to_prompt_on_api_error(self, mock_get, settings_file):
        mock_get.side_effect = httpx.ConnectError("network error")
        runner = CliRunner()
        # Provide manual league ID after auto-detect fails
        result = runner.invoke(init_command, input="draft\n78901\n456\nN\nN\nN\nN\n")
        assert result.exit_code == 0
        assert "Could not auto-detect" in result.output

        yaml = YAML(typ="rt")
        data = yaml.load(settings_file.read_text(encoding="utf-8"))
        assert data["fpl"]["draft_league_id"] == 456

    @patch("fpl_cli.cli.init.httpx.get", return_value=_mock_draft_response(status_code=404))
    def test_falls_back_to_prompt_on_bad_entry_id(self, _mock_get, settings_file):
        runner = CliRunner()
        result = runner.invoke(init_command, input="draft\n99999\n456\nN\nN\nN\nN\n")
        assert result.exit_code == 0
        assert "Could not auto-detect" in result.output

    @patch("fpl_cli.cli.init.httpx.get", return_value=_mock_draft_response({"entry": {"league_set": []}}))
    def test_falls_back_to_prompt_on_empty_league_set(self, _mock_get, settings_file):
        runner = CliRunner()
        result = runner.invoke(init_command, input="draft\n78901\n456\nN\nN\nN\nN\n")
        assert result.exit_code == 0
        assert "Could not auto-detect" in result.output


class TestInitUpdateExisting:
    def test_preserves_non_fpl_sections(self, settings_file):
        yaml = YAML(typ="rt")
        existing = {"fpl": {"classic_entry_id": 123}, "thresholds": {"transfer_xg_threshold": 0.15}}
        yaml.dump(existing, settings_file)

        runner = CliRunner()
        result = runner.invoke(init_command, input="classic\nN\n999\n888\nN\nN\nN\nN\n")
        assert result.exit_code == 0

        data = yaml.load(settings_file.read_text(encoding="utf-8"))
        assert data["fpl"]["classic_entry_id"] == 999
        assert data["thresholds"]["transfer_xg_threshold"] == 0.15

    def test_prefills_existing_values(self, settings_file):
        yaml = YAML(typ="rt")
        existing = {"fpl": {"classic_entry_id": 123456, "classic_league_id": 234567}}
        yaml.dump(existing, settings_file)

        runner = CliRunner()
        # Accept defaults by pressing enter twice
        result = runner.invoke(init_command, input="classic\nN\n\n\nN\nN\nN\nN\n")
        assert result.exit_code == 0

        data = yaml.load(settings_file.read_text(encoding="utf-8"))
        assert data["fpl"]["classic_entry_id"] == 123456
        assert data["fpl"]["classic_league_id"] == 234567

    @patch("fpl_cli.cli.init.httpx.get", return_value=_mock_draft_response())
    def test_format_change_draft_to_both(self, _mock_get, settings_file):
        yaml = YAML(typ="rt")
        existing = {"fpl": {"draft_league_id": 456, "draft_entry_id": 78901}}
        yaml.dump(existing, settings_file)

        runner = CliRunner()
        # Classic fields new, draft entry prefilled (accept default), league auto-derived
        result = runner.invoke(init_command, input="both\nN\n123456\n234567\n\nN\nN\nN\nN\n")
        assert result.exit_code == 0

        data = yaml.load(settings_file.read_text(encoding="utf-8"))
        assert data["fpl"]["classic_entry_id"] == 123456
        assert data["fpl"]["draft_league_id"] == 456
