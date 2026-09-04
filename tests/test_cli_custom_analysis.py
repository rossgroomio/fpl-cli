"""Tests for custom_analysis config toggle and EXPERIMENTAL frozenset."""

from unittest.mock import patch

import click
import pytest
from click.testing import CliRunner

from fpl_cli.cli._context import (
    CLASSIC_ONLY,
    EXPERIMENTAL,
    CLIContext,
    Format,
    FormatAwareGroup,
    custom_analysis_enabled,
    experimental_gate_message,
    get_settings,
    is_custom_analysis_enabled,
)
from fpl_cli.paths import user_config_dir


class TestIsCustomAnalysisEnabled:
    def test_returns_true_when_enabled(self):
        assert is_custom_analysis_enabled({"custom_analysis": True}) is True

    def test_returns_false_when_disabled(self):
        assert is_custom_analysis_enabled({"custom_analysis": False}) is False

    def test_returns_false_when_key_missing(self):
        assert is_custom_analysis_enabled({}) is False

    def test_returns_false_for_none_value(self):
        assert is_custom_analysis_enabled({"custom_analysis": None}) is False


class TestGetSettings:
    """The accessor a command with a Click context reads its settings through (#219)."""

    def test_returns_the_settings_the_group_resolved(self):
        settings = {"custom_analysis": True, "rolling_window": 7}
        ctx = click.Context(click.Command("stats"), obj=CLIContext(format=Format.BOTH, settings=settings))
        assert get_settings(ctx) is settings

    def test_returns_the_empty_dict_doctor_and_init_are_given(self):
        """`main()` hands those two an empty settings dict on purpose."""
        ctx = click.Context(click.Command("doctor"), obj=CLIContext(format=None, settings={}))
        assert get_settings(ctx) == {}

    def test_loads_settings_when_the_context_has_none(self):
        """A command invoked without the group still gets the shipped defaults.

        Falling back to `{}` would strip `rolling_window` and the thresholds
        along with the user's overrides, which is not the same thing at all.
        """
        ctx = click.Context(click.Command("stats"), obj=None)
        with patch("fpl_cli.cli._context.load_settings", return_value={"rolling_window": 9}) as loader:
            assert get_settings(ctx) == {"rolling_window": 9}
        loader.assert_called_once_with()

    def test_falls_back_for_an_unexpected_context_object(self):
        ctx = click.Context(click.Command("stats"), obj={"not": "a CLIContext"})
        with patch("fpl_cli.cli._context.load_settings", return_value={"custom_analysis": True}):
            assert get_settings(ctx) == {"custom_analysis": True}


class TestCustomAnalysisEnabled:
    """The ctx-level gate `fpl fdr`, `fpl fixtures` and `fpl xg` share (#219)."""

    @pytest.mark.parametrize("configured,expected", [(True, True), (False, False)])
    def test_reads_the_toggle_off_the_context(self, configured, expected):
        ctx = click.Context(
            click.Command("fixtures"),
            obj=CLIContext(format=Format.BOTH, settings={"custom_analysis": configured}),
        )
        assert custom_analysis_enabled(ctx) is expected

    def test_defaults_to_off_when_the_context_carries_no_toggle(self):
        ctx = click.Context(click.Command("fixtures"), obj=CLIContext(format=Format.BOTH, settings={}))
        assert custom_analysis_enabled(ctx) is False

    def test_loads_settings_when_the_context_has_none(self):
        ctx = click.Context(click.Command("fixtures"), obj=None)
        with patch("fpl_cli.cli._context.load_settings", return_value={"custom_analysis": True}):
            assert custom_analysis_enabled(ctx) is True


class TestGateFollowsTheGroupSettings:
    """The gate reads what `main()` resolved, with no per-command loading (#219).

    These invoke through the group with only `fpl_cli.cli.load_settings`
    patched -- the seam the group itself uses, and no per-command patch -- so a
    command that went back to resolving its own settings would fail here. Both
    answer the gate before touching the network.
    """

    @pytest.mark.parametrize("argv,refusal", [
        (["fdr", "--position", "atk"], "requires custom analysis"),
        (["stats", "--value", "--sort", "quality_per_m"], "requires custom analysis"),
    ])
    def test_command_is_refused_when_the_group_says_custom_analysis_is_off(self, argv, refusal):
        from fpl_cli.cli import main as cli_main

        runner = CliRunner()
        with patch("fpl_cli.cli.load_settings", return_value={"custom_analysis": False}):
            result = runner.invoke(cli_main, argv)

        assert result.exit_code == 1
        assert refusal in result.output

    @pytest.mark.parametrize("argv,refusal", [
        (["fdr", "--position", "atk"], "requires custom analysis"),
        (["stats", "--value", "--sort", "quality_per_m"], "requires custom analysis"),
    ])
    def test_command_is_allowed_past_the_gate_when_the_group_says_it_is_on(self, argv, refusal):
        """Past the gate the run dies on the blocked network, which is fine here."""
        from fpl_cli.cli import main as cli_main

        runner = CliRunner()
        with patch("fpl_cli.cli.load_settings", return_value={"custom_analysis": True}):
            result = runner.invoke(cli_main, argv)

        assert refusal not in result.output


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


class TestExperimentalGateMessage:
    """A gated command must name its gate, not suggest itself (#46)."""

    def test_message_names_the_command_the_toggle_and_the_file(self):
        message = experimental_gate_message("ratings")

        assert "ratings" in message
        assert "custom_analysis: true" in message
        # Naming the resolved file is the point: a surprise gate is usually a
        # config dir the user did not expect fpl-cli to be reading.
        assert str(user_config_dir() / "settings.yaml") in message

    def test_resolve_command_raises_informative_error_when_off(self):
        group = _make_group()
        ctx = click.Context(group, obj=CLIContext(format=Format.BOTH, settings={"custom_analysis": False}))

        with pytest.raises(click.UsageError) as exc_info:
            group.resolve_command(ctx, ["captain"])

        message = str(exc_info.value)
        assert "custom_analysis: true" in message
        assert "No such command" not in message

    def test_resolve_command_passes_through_when_on(self):
        group = _make_group()
        ctx = click.Context(group, obj=CLIContext(format=Format.BOTH, settings={"custom_analysis": True}))

        name, cmd, args = group.resolve_command(ctx, ["captain"])

        assert name == "captain"
        assert cmd is not None

    def test_genuinely_unknown_command_keeps_clicks_error(self):
        """The gate must not swallow click's normal not-found handling."""
        group = _make_group()
        ctx = click.Context(group, obj=CLIContext(format=Format.BOTH, settings={"custom_analysis": False}))

        with pytest.raises(click.UsageError) as exc_info:
            group.resolve_command(ctx, ["nonsense"])

        assert "No such command" in str(exc_info.value)

    def test_shell_completion_gets_none_not_an_exception(self):
        """Completion resolves with resilient_parsing and expects a None command."""
        group = _make_group()
        ctx = click.Context(
            group,
            obj=CLIContext(format=Format.BOTH, settings={"custom_analysis": False}),
            resilient_parsing=True,
        )

        name, cmd, args = group.resolve_command(ctx, ["captain"])

        assert cmd is None
        assert name is None

    def test_cli_does_not_suggest_the_command_it_just_refused(self):
        """The reported symptom: "No such command 'captain'. Did you mean 'captain'?"."""
        group = _make_group()
        settings = {"custom_analysis": False}

        result = CliRunner().invoke(
            group, ["captain"], obj=CLIContext(format=Format.BOTH, settings=settings)
        )

        assert result.exit_code != 0
        assert "Did you mean" not in result.output
        assert "custom_analysis: true" in result.output
