"""Tests verifying CLI warnings go to stderr via error_console."""

from __future__ import annotations

import sys
from unittest.mock import MagicMock, patch

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
