"""An explicitly-set config dir with no settings.yaml must say so (#46).

A missing settings.yaml is indistinguishable from a deliberately minimal one,
so the CLI silently ran on defaults -- which is how a mis-set
FPL_CLI_CONFIG_DIR presented as commands vanishing rather than as a
misconfiguration. The default platformdirs location stays quiet: no
settings.yaml there is just the normal pre-`fpl init` state.
"""

from __future__ import annotations

from click.testing import CliRunner

from fpl_cli.cli import main
from fpl_cli.cli._context import load_settings
from fpl_cli.paths import user_config_dir

_WARNING_MARKER = "has no settings.yaml"


class TestMissingSettingsWarning:
    def test_warns_when_explicit_config_dir_has_no_settings(self, tmp_path, monkeypatch, capsys):
        config_dir = tmp_path / "vault"
        monkeypatch.setenv("FPL_CLI_CONFIG_DIR", str(config_dir))
        user_config_dir.cache_clear()

        load_settings()

        stderr = capsys.readouterr().err
        assert "FPL_CLI_CONFIG_DIR" in stderr
        assert _WARNING_MARKER in stderr
        assert str(config_dir) in stderr

    def test_warning_goes_to_stderr_not_stdout(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setenv("FPL_CLI_CONFIG_DIR", str(tmp_path / "vault"))
        user_config_dir.cache_clear()

        load_settings()

        captured = capsys.readouterr()
        assert _WARNING_MARKER not in captured.out

    def test_warns_only_once_per_invocation(self, tmp_path, monkeypatch, capsys):
        """load_settings runs several times per command; one warning is enough."""
        monkeypatch.setenv("FPL_CLI_CONFIG_DIR", str(tmp_path / "vault"))
        user_config_dir.cache_clear()

        load_settings()
        load_settings()
        load_settings()

        assert capsys.readouterr().err.count(_WARNING_MARKER) == 1

    def test_silent_when_settings_file_exists(self, tmp_path, monkeypatch, capsys):
        config_dir = tmp_path / "vault"
        config_dir.mkdir()
        (config_dir / "settings.yaml").write_text("custom_analysis: true\n", encoding="utf-8")
        monkeypatch.setenv("FPL_CLI_CONFIG_DIR", str(config_dir))
        user_config_dir.cache_clear()

        settings = load_settings()

        assert capsys.readouterr().err == ""
        assert settings["custom_analysis"] is True

    def test_silent_when_override_is_unset(self, tmp_path, monkeypatch, capsys):
        """No settings.yaml in the platformdirs default is normal, not a mistake."""
        monkeypatch.delenv("FPL_CLI_CONFIG_DIR", raising=False)
        monkeypatch.setattr(
            "platformdirs.user_config_path", lambda *_args, **_kwargs: tmp_path / "platform"
        )
        user_config_dir.cache_clear()

        load_settings()

        assert capsys.readouterr().err == ""

    def test_defaults_still_load_after_warning(self, tmp_path, monkeypatch):
        """The warning is advisory: shipped defaults must still apply."""
        monkeypatch.setenv("FPL_CLI_CONFIG_DIR", str(tmp_path / "vault"))
        user_config_dir.cache_clear()

        settings = load_settings()

        assert settings["custom_analysis"] is False


class TestMissingSettingsWarningInCLI:
    def test_command_run_reports_the_empty_config_dir(self, tmp_path, monkeypatch):
        config_dir = tmp_path / "vault"
        monkeypatch.setenv("FPL_CLI_CONFIG_DIR", str(config_dir))
        user_config_dir.cache_clear()

        result = CliRunner().invoke(main, ["ratings"])

        # A gated command from an empty config dir now explains both halves:
        # which directory was read, and which toggle is off.
        assert _WARNING_MARKER in result.stderr
        assert "custom_analysis: true" in result.output
