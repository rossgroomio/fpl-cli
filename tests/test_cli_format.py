"""Tests for format resolution and FormatAwareGroup sectioned help."""

from unittest.mock import patch

import click
from click.testing import CliRunner

from fpl_cli.cli._context import (
    CLIContext,
    Format,
    FormatAwareGroup,
    _argv_requests_json,
    _command_from_argv,
    resolve_format,
)


class TestResolveFormat:
    def test_both_ids_returns_both(self):
        settings = {"fpl": {"classic_entry_id": 123, "draft_league_id": 456}}
        assert resolve_format(settings) == Format.BOTH

    def test_classic_only_returns_classic(self):
        settings = {"fpl": {"classic_entry_id": 123}}
        assert resolve_format(settings) == Format.CLASSIC

    def test_draft_only_returns_draft(self):
        settings = {"fpl": {"draft_league_id": 456}}
        assert resolve_format(settings) == Format.DRAFT

    def test_no_ids_returns_none(self):
        assert resolve_format({}) is None
        assert resolve_format({"fpl": {}}) is None

    def test_empty_settings_returns_none(self):
        assert resolve_format({}) is None

    def test_falsy_id_ignored(self):
        settings = {"fpl": {"classic_entry_id": 0, "draft_league_id": 456}}
        assert resolve_format(settings) == Format.DRAFT

    def test_env_var_overrides_inference(self):
        settings = {"fpl": {"classic_entry_id": 123}}
        with patch.dict("os.environ", {"FPL_FORMAT": "draft"}):
            assert resolve_format(settings) == Format.DRAFT

    def test_env_var_case_insensitive(self):
        with patch.dict("os.environ", {"FPL_FORMAT": "BOTH"}):
            assert resolve_format({}) == Format.BOTH

    def test_env_var_invalid_warns_and_falls_through(self, capsys):
        settings = {"fpl": {"classic_entry_id": 123}}
        with patch.dict("os.environ", {"FPL_FORMAT": "invalid"}):
            assert resolve_format(settings) == Format.CLASSIC
        captured = capsys.readouterr()
        assert "FPL_FORMAT" in captured.err


def _make_group(fmt: Format | None) -> click.Group:
    """Build a minimal FormatAwareGroup with dummy commands per section."""
    group = FormatAwareGroup(name="fpl")

    @group.command("fixtures")
    def fixtures_cmd():
        """Show upcoming fixtures."""

    @group.command("captain")
    def captain_cmd():
        """Pick your captain."""

    @group.command("waivers")
    def waivers_cmd():
        """Show waiver recommendations."""

    group.context_settings = {"obj": CLIContext(format=fmt, settings={"custom_analysis": True})}
    return group


class TestFormatAwareGroupHelp:
    def _get_help(self, fmt: Format | None) -> str:
        group = _make_group(fmt)
        runner = CliRunner()
        result = runner.invoke(group, ["--help"], obj=CLIContext(format=fmt, settings={"custom_analysis": True}))
        return result.output

    def test_both_shows_all_sections(self):
        output = self._get_help(Format.BOTH)
        assert "General Commands" in output
        assert "Classic Commands" in output
        assert "Draft Commands" in output

    def test_classic_omits_draft_section(self):
        output = self._get_help(Format.CLASSIC)
        assert "General Commands" in output
        assert "Classic Commands" in output
        assert "Draft Commands" not in output

    def test_draft_omits_classic_section(self):
        output = self._get_help(Format.DRAFT)
        assert "General Commands" in output
        assert "Draft Commands" in output
        assert "Classic Commands" not in output

    def test_none_shows_all_sections(self):
        output = self._get_help(None)
        assert "General Commands" in output
        assert "Classic Commands" in output
        assert "Draft Commands" in output

    def test_hidden_command_not_in_help(self):
        group = _make_group(Format.BOTH)

        @group.command("secret", hidden=True)
        def secret_cmd():
            """Hidden command."""

        runner = CliRunner()
        obj = CLIContext(format=Format.BOTH, settings={"custom_analysis": True})
        result = runner.invoke(group, ["--help"], obj=obj)
        assert "secret" not in result.output

    def test_commands_still_invocable_when_hidden_from_help(self):
        """Soft filtering: captain is hidden in draft help but still invocable."""
        group = _make_group(Format.DRAFT)
        runner = CliRunner()
        obj = CLIContext(format=Format.DRAFT, settings={"custom_analysis": True})
        # captain shouldn't be in help
        help_result = runner.invoke(group, ["--help"], obj=obj)
        assert "captain" not in help_result.output
        # but it should still be invocable (get_command returns it)
        result = runner.invoke(group, ["captain"], obj=obj)
        assert result.exit_code == 0

    def test_no_branded_header_line(self):
        output = self._get_help(Format.BOTH)
        assert output.startswith("Usage:")


class TestArgvRequestsJson:
    """`FormatAwareGroup.main`'s `UserDirError` handler recovers `--format json`
    from raw argv, since it fires before click has parsed anything (#307).
    """

    def test_space_form(self):
        assert _argv_requests_json(["status", "--format", "json"]) is True

    def test_equals_form(self):
        assert _argv_requests_json(["status", "--format=json"]) is True

    def test_case_insensitive(self):
        assert _argv_requests_json(["status", "--format", "JSON"]) is True

    def test_table_format_is_false(self):
        assert _argv_requests_json(["status", "--format", "table"]) is False

    def test_no_format_flag_is_false(self):
        assert _argv_requests_json(["status"]) is False

    def test_empty_argv_is_false(self):
        assert _argv_requests_json([]) is False

    def test_dangling_format_flag_is_false(self):
        """`--format` with nothing after it -- click would reject this itself."""
        assert _argv_requests_json(["status", "--format"]) is False


class TestCommandFromArgv:
    def test_first_non_option_token(self):
        assert _command_from_argv(["status", "--format", "json"]) == "status"

    def test_flags_before_the_command_are_skipped(self):
        assert _command_from_argv(["--verbose", "chips", "sync"]) == "chips"

    def test_falls_back_to_fpl_when_nothing_but_flags(self):
        assert _command_from_argv(["--format", "json"]) == "fpl"

    def test_falls_back_to_fpl_on_empty_argv(self):
        assert _command_from_argv([]) == "fpl"


class TestBrandedVersion:
    def test_version_output_contains_branded_line(self):
        from fpl_cli.cli import main

        runner = CliRunner()
        result = runner.invoke(main, ["--version"])
        assert "⚽ fpl-cli v" in result.output
        # Verify a version number follows the prefix (not just the prefix alone)
        version_part = result.output.split("v", 1)[1].strip()
        assert any(c.isdigit() for c in version_part)

    def test_version_fallback_when_version_file_missing(self):
        import importlib
        import sys
        # Patch _version import to raise ImportError, then reload fpl_cli
        saved_version = sys.modules.pop("fpl_cli._version", None)
        sys.modules["fpl_cli._version"] = None  # forces ImportError on from-import
        saved_init = sys.modules.pop("fpl_cli", None)
        try:
            import fpl_cli

            importlib.reload(fpl_cli)
            assert fpl_cli.__version__ == "0.0.0+unknown"
        finally:
            # Restore original modules
            if saved_version is not None:
                sys.modules["fpl_cli._version"] = saved_version
            else:
                sys.modules.pop("fpl_cli._version", None)
            if saved_init is not None:
                sys.modules["fpl_cli"] = saved_init
                importlib.reload(saved_init)
