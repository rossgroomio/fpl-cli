"""Tests for `fpl credentials` command group."""

from __future__ import annotations

from unittest.mock import patch

from click.testing import CliRunner

from fpl_cli.cli import main


class TestCredentialsSet:
    def test_stores_email_and_password(self):
        runner = CliRunner()
        with patch("keyring.set_password") as mock_set:
            result = runner.invoke(main, ["credentials", "set"], input="user@example.com\nsecret\n")
        assert result.exit_code == 0
        mock_set.assert_any_call("fpl-cli", "email", "user@example.com")
        mock_set.assert_any_call("fpl-cli", "password", "secret")

    def test_success_message_displayed(self):
        runner = CliRunner()
        with patch("keyring.set_password"):
            result = runner.invoke(main, ["credentials", "set"], input="a@b.com\npw\n")
        assert result.exit_code == 0
        assert "Credentials saved" in result.output


class TestCredentialsClear:
    def test_removes_both_credentials(self):
        runner = CliRunner()
        with patch("keyring.delete_password") as mock_delete:
            result = runner.invoke(main, ["credentials", "clear"])
        assert result.exit_code == 0
        assert mock_delete.call_count == 2
        mock_delete.assert_any_call("fpl-cli", "email")
        mock_delete.assert_any_call("fpl-cli", "password")
        assert "Removed" in result.output

    def test_no_credentials_found(self):
        from keyring.errors import PasswordDeleteError

        runner = CliRunner()
        with patch("keyring.delete_password", side_effect=PasswordDeleteError()):
            result = runner.invoke(main, ["credentials", "clear"])
        assert result.exit_code == 0
        assert "No credentials" in result.output
