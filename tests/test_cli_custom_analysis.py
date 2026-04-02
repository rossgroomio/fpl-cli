"""Tests for custom_analysis config toggle and EXPERIMENTAL frozenset."""

from unittest.mock import patch

import click
from click.testing import CliRunner

from fpl_cli.cli._context import (
    CLASSIC_ONLY,
    EXPERIMENTAL,
    CLIContext,
    Format,
    FormatAwareGroup,
    is_custom_analysis_enabled,
)


class TestIsCustomAnalysisEnabled:
    def test_returns_true_when_enabled(self):
        assert is_custom_analysis_enabled({"custom_analysis": True}) is True

    def test_returns_false_when_disabled(self):
        assert is_custom_analysis_enabled({"custom_analysis": False}) is False

    def test_returns_false_when_key_missing(self):
        assert is_custom_analysis_enabled({}) is False

    def test_returns_false_for_none_value(self):
        assert is_custom_analysis_enabled({"custom_analysis": None}) is False


class TestExperimentalFrozenset:
    def test_contains_expected_commands(self):
        expected = {"captain", "targets", "differentials", "waivers",
                    "allocate", "transfer-eval", "ratings"}
        assert EXPERIMENTAL == expected

    def test_does_not_contain_data_only_commands(self):
        data_only = {"fixtures", "league", "league-recap", "sell-prices",
                     "chips", "status", "review", "init"}
        assert EXPERIMENTAL.isdisjoint(data_only)

    def test_does_not_contain_mixed_commands(self):
        mixed = {"xg", "stats", "fdr", "preview"}
        assert EXPERIMENTAL.isdisjoint(mixed)


def _make_group() -> FormatAwareGroup:
    """Build a minimal FormatAwareGroup with dummy commands for testing."""
    group = FormatAwareGroup(name="fpl")

    @group.command("fixtures")
    def fixtures_cmd():
        """Show upcoming fixtures."""

    @group.command("captain")
    def captain_cmd():
        """Pick your captain."""

    @group.command("targets")
    def targets_cmd():
        """Show transfer targets."""

    @group.command("waivers")
    def waivers_cmd():
        """Show waiver recommendations."""

    @group.command("allocate")
    def allocate_cmd():
        """Allocate squad budget."""

    @group.command("league")
    def league_cmd():
        """Show league standings."""

    return group


class TestFormatAwareGroupExperimental:
    """Tests for list_commands/get_command gating of experimental commands."""

    def test_list_commands_includes_experimental_when_on(self):
        group = _make_group()
        settings = {"custom_analysis": True}
        ctx = click.Context(group, obj=CLIContext(format=Format.BOTH, settings=settings))
        commands = group.list_commands(ctx)
        assert "captain" in commands
        assert "targets" in commands
        assert "waivers" in commands
        assert "allocate" in commands

    def test_list_commands_excludes_experimental_when_off(self):
        group = _make_group()
        settings = {"custom_analysis": False}
        ctx = click.Context(group, obj=CLIContext(format=Format.BOTH, settings=settings))
        commands = group.list_commands(ctx)
        for name in EXPERIMENTAL:
            assert name not in commands

    def test_get_command_returns_none_when_off(self):
        group = _make_group()
        settings = {"custom_analysis": False}
        ctx = click.Context(group, obj=CLIContext(format=Format.BOTH, settings=settings))
        assert group.get_command(ctx, "captain") is None

    def test_get_command_returns_command_when_on(self):
        group = _make_group()
        settings = {"custom_analysis": True}
        ctx = click.Context(group, obj=CLIContext(format=Format.BOTH, settings=settings))
        assert group.get_command(ctx, "captain") is not None

    def test_non_experimental_always_available(self):
        group = _make_group()
        for enabled in (True, False):
            settings = {"custom_analysis": enabled}
            ctx = click.Context(group, obj=CLIContext(format=Format.BOTH, settings=settings))
            commands = group.list_commands(ctx)
            assert "fixtures" in commands
            assert "league" in commands

    def test_classic_only_and_experimental_overlap(self):
        """Commands in both CLASSIC_ONLY and EXPERIMENTAL are hidden when toggle is off."""
        overlap = CLASSIC_ONLY & EXPERIMENTAL
        assert len(overlap) > 0, "precondition: overlap exists"
        group = _make_group()
        settings = {"custom_analysis": False}
        ctx = click.Context(group, obj=CLIContext(format=Format.BOTH, settings=settings))
        commands = group.list_commands(ctx)
        for name in overlap:
            assert name not in commands

    @patch("fpl_cli.cli._context.load_settings", return_value={"custom_analysis": False})
    def test_list_commands_uses_load_settings_when_no_ctx_obj(self, mock_load):
        """When ctx.obj is None (e.g. --help before callback), load_settings is used."""
        group = _make_group()
        ctx = click.Context(group)  # obj defaults to None
        commands = group.list_commands(ctx)
        assert "captain" not in commands
        assert "fixtures" in commands
        mock_load.assert_called()

    @patch("fpl_cli.cli._context.load_settings", return_value={"custom_analysis": True})
    def test_list_commands_uses_load_settings_when_on(self, mock_load):
        group = _make_group()
        ctx = click.Context(group)
        commands = group.list_commands(ctx)
        assert "captain" in commands

    def test_invoke_experimental_command_when_off_shows_error(self):
        group = _make_group()
        runner = CliRunner()
        settings = {"custom_analysis": False}
        result = runner.invoke(group, ["captain"], obj=CLIContext(format=Format.BOTH, settings=settings))
        assert result.exit_code != 0
