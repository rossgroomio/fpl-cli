"""Tests verifying CLI warnings go to stderr via error_console."""

from __future__ import annotations

import sys
from unittest.mock import AsyncMock, MagicMock, patch

from click.testing import CliRunner

from fpl_cli.cli import main
from fpl_cli.cli._context import console, error_console


class TestErrorConsoleConfiguration:
    def test_error_console_targets_stderr(self):
        # error_console must write to stderr, not stdout
        assert error_console.file is sys.stderr

    def test_console_targets_stdout(self):
        assert console.file is not sys.stderr


class TestWarningsOnStderr:
    def test_warning_routed_via_error_console_not_console(self, monkeypatch):
        """Warning must call error_console.print, not console.print."""
        from keyring.errors import PasswordDeleteError

        import fpl_cli.cli.credentials as creds_mod

        mock_error = MagicMock()
        monkeypatch.setattr(creds_mod, "error_console", mock_error)

        runner = CliRunner()
        with patch("keyring.delete_password", side_effect=PasswordDeleteError()):
            result = runner.invoke(main, ["credentials", "clear"])

        assert result.exit_code == 0
        mock_error.print.assert_called_once_with("[yellow]No credentials found in keyring[/yellow]")

    def test_warning_not_on_stdout(self, monkeypatch):
        """Warning text must not appear in stdout."""
        from keyring.errors import PasswordDeleteError

        import fpl_cli.cli.credentials as creds_mod

        monkeypatch.setattr(creds_mod, "error_console", MagicMock())

        runner = CliRunner()
        with patch("keyring.delete_password", side_effect=PasswordDeleteError()):
            result = runner.invoke(main, ["credentials", "clear"])

        assert "No credentials" not in result.output

    def test_data_output_on_stdout(self):
        """Success message must appear on stdout, not just stderr."""
        runner = CliRunner()
        with patch("keyring.delete_password"):
            result = runner.invoke(main, ["credentials", "clear"])
        assert result.exit_code == 0
        assert "Removed" in result.output

    def test_stats_no_match_warning_on_stderr(self, monkeypatch):
        """'No players match' must route via error_console, not stdout."""
        import fpl_cli.cli.stats as stats_mod

        mock_error = MagicMock()
        monkeypatch.setattr(stats_mod, "error_console", mock_error)

        mock_client = AsyncMock()
        mock_client.get_players = AsyncMock(return_value=[])
        mock_client.get_teams = AsyncMock(return_value=[])
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        runner = CliRunner()
        with patch("fpl_cli.api.fpl.FPLClient", return_value=mock_client):
            result = runner.invoke(main, ["stats"])

        assert result.exit_code == 0
        mock_error.print.assert_called_once_with("[yellow]No players match the given filters.[/yellow]")

    def test_fdr_missing_draft_entry_id_warning_on_stderr(self, monkeypatch):
        """Missing draft_entry_id must route warning via error_console."""
        import fpl_cli.cli.fdr as fdr_mod

        mock_error = MagicMock()
        monkeypatch.setattr(fdr_mod, "error_console", mock_error)
        monkeypatch.setattr(fdr_mod, "get_settings", lambda _ctx: {})
        # custom_on=False routes to raw FPL API path (early return) before my_squad check
        monkeypatch.setattr(fdr_mod, "custom_analysis_enabled", lambda _ctx: True)

        mock_result = MagicMock()
        mock_result.success = False
        mock_result.message = "mocked"
        mock_result.errors = []

        mock_agent = AsyncMock()
        mock_agent.run = AsyncMock(return_value=mock_result)
        mock_agent.__aenter__ = AsyncMock(return_value=mock_agent)
        mock_agent.__aexit__ = AsyncMock(return_value=False)

        runner = CliRunner()
        with patch("fpl_cli.agents.data.fixture.FixtureAgent", return_value=mock_agent):
            result = runner.invoke(main, ["fdr", "--my-squad", "--draft"])

        # Agent failure (#47): mocked result.success=False must exit nonzero, not swallow the failure.
        assert result.exit_code == 1
        mock_error.print.assert_any_call("[yellow]draft_entry_id not configured[/yellow]")

    def test_chips_timing_missing_classic_entry_id_reports_on_stderr(self, monkeypatch):
        """Missing classic_entry_id is a failure, on stderr, with exit 1.

        It used to be a yellow warning printed through this module's own
        `error_console` and followed by exit 0, while `--format json` called
        the same condition an error and exited 1 (#286). It now goes through
        `require_entry_id`, so the stream is asserted directly rather than by
        patching the console: the prose is `emit_failure`'s, printed from
        `_json`, and a patch here would no longer see it.
        """
        import fpl_cli.cli.chips as chips_mod

        monkeypatch.setattr(chips_mod, "get_settings", lambda _ctx: {})

        runner = CliRunner()
        result = runner.invoke(main, ["chips", "timing"])

        assert result.exit_code == 1
        assert "classic_entry_id is not set" in " ".join(result.stderr.split())
        assert result.stdout == ""
