"""Unit tests for fpl_cli/paths.py."""

from __future__ import annotations

import ast
import os
from pathlib import Path
from unittest.mock import MagicMock

import pytest

import fpl_cli.paths as paths_mod
from fpl_cli.paths import (
    SHIPPED_CONFIG_DIR,
    TEMPLATE_DIR,
    UserDirError,
    _migrate_legacy_files,
    user_cache_dir,
    user_config_dir,
    user_data_dir,
)

RESOLVERS = (
    ("FPL_CLI_CONFIG_DIR", user_config_dir),
    ("FPL_CLI_DATA_DIR", user_data_dir),
    ("FPL_CLI_CACHE_DIR", user_cache_dir),
)


class TestShippedPaths:
    def test_shipped_config_dir_exists(self):
        assert SHIPPED_CONFIG_DIR.is_dir()

    def test_template_dir_exists(self):
        assert TEMPLATE_DIR.is_dir()


class TestUserConfigDir:
    def test_returns_platformdirs_path(self, tmp_path, monkeypatch):
        expected = tmp_path / "config"
        monkeypatch.delenv("FPL_CLI_CONFIG_DIR", raising=False)
        monkeypatch.setattr("platformdirs.user_config_path", lambda *_args, **_kwargs: expected)
        result = user_config_dir()
        assert result == expected

    def test_respects_env_var(self, tmp_path, monkeypatch):
        custom = tmp_path / "custom_config"
        monkeypatch.setenv("FPL_CLI_CONFIG_DIR", str(custom))
        result = user_config_dir()
        assert result == custom

    def test_creates_directory(self, tmp_path, monkeypatch):
        custom = tmp_path / "new_dir"
        monkeypatch.setenv("FPL_CLI_CONFIG_DIR", str(custom))
        assert not custom.exists()
        user_config_dir()
        assert custom.is_dir()

    @pytest.mark.skipif(os.name == "nt", reason="chmod not applicable on Windows")
    def test_sets_permissions_on_dir_it_creates(self, tmp_path, monkeypatch):
        custom = tmp_path / "perm_dir"
        monkeypatch.setenv("FPL_CLI_CONFIG_DIR", str(custom))
        user_config_dir()
        assert custom.stat().st_mode & 0o777 == 0o700


class TestUserDataDir:
    def test_returns_platformdirs_path(self, tmp_path, monkeypatch):
        expected = tmp_path / "data"
        monkeypatch.delenv("FPL_CLI_DATA_DIR", raising=False)
        monkeypatch.setattr("platformdirs.user_data_path", lambda *_args, **_kwargs: expected)
        result = user_data_dir()
        assert result == expected

    def test_respects_env_var(self, tmp_path, monkeypatch):
        custom = tmp_path / "custom_data"
        monkeypatch.setenv("FPL_CLI_DATA_DIR", str(custom))
        result = user_data_dir()
        assert result == custom

    def test_creates_directory(self, tmp_path, monkeypatch):
        custom = tmp_path / "new_data"
        monkeypatch.setenv("FPL_CLI_DATA_DIR", str(custom))
        assert not custom.exists()
        user_data_dir()
        assert custom.is_dir()

    @pytest.mark.skipif(os.name == "nt", reason="chmod not applicable on Windows")
    def test_sets_permissions_on_dir_it_creates(self, tmp_path, monkeypatch):
        custom = tmp_path / "perm_data"
        monkeypatch.setenv("FPL_CLI_DATA_DIR", str(custom))
        user_data_dir()
        assert custom.stat().st_mode & 0o777 == 0o700


class TestUserCacheDir:
    def test_returns_platformdirs_path(self, tmp_path, monkeypatch):
        expected = tmp_path / "cache"
        monkeypatch.delenv("FPL_CLI_CACHE_DIR", raising=False)
        monkeypatch.setattr("platformdirs.user_cache_path", lambda *_args, **_kwargs: expected)
        result = user_cache_dir()
        assert result == expected

    def test_respects_env_var(self, tmp_path, monkeypatch):
        custom = tmp_path / "custom_cache"
        monkeypatch.setenv("FPL_CLI_CACHE_DIR", str(custom))
        result = user_cache_dir()
        assert result == custom

    def test_creates_directory(self, tmp_path, monkeypatch):
        custom = tmp_path / "new_cache"
        monkeypatch.setenv("FPL_CLI_CACHE_DIR", str(custom))
        assert not custom.exists()
        user_cache_dir()
        assert custom.is_dir()

    @pytest.mark.skipif(os.name == "nt", reason="chmod not applicable on Windows")
    def test_sets_permissions_on_dir_it_creates(self, tmp_path, monkeypatch):
        custom = tmp_path / "perm_cache"
        monkeypatch.setenv("FPL_CLI_CACHE_DIR", str(custom))
        user_cache_dir()
        assert custom.stat().st_mode & 0o777 == 0o700


@pytest.mark.skipif(os.name == "nt", reason="chmod not applicable on Windows")
class TestExistingDirPermissions:
    """An operator-supplied directory keeps the mode its owner gave it."""

    @pytest.mark.parametrize(("env_var", "resolver"), RESOLVERS)
    def test_existing_dir_mode_is_left_alone(self, env_var, resolver, tmp_path, monkeypatch):
        shared = tmp_path / "shared-workspace"
        shared.mkdir()
        shared.chmod(0o755)
        (shared / "someone_elses_file").write_text("keep me", encoding="utf-8")
        monkeypatch.setenv(env_var, str(shared))

        resolver()

        assert shared.stat().st_mode & 0o777 == 0o755


class TestUnusableOverride:
    """A bad FPL_CLI_* value is reported, not raised as a bare OSError."""

    @pytest.mark.parametrize(("env_var", "resolver"), RESOLVERS)
    def test_raises_user_dir_error(self, env_var, resolver, tmp_path, monkeypatch):
        not_a_dir = tmp_path / "regular_file"
        not_a_dir.write_text("", encoding="utf-8")
        monkeypatch.setenv(env_var, str(not_a_dir / "nested"))

        with pytest.raises(UserDirError) as exc_info:
            resolver()

        message = str(exc_info.value)
        assert env_var in message
        assert "writable directory" in message


class TestRelativeOverrideRejected:
    """A relative FPL_CLI_* value would resolve against the cwd (#46)."""

    @pytest.mark.parametrize(("env_var", "resolver"), RESOLVERS)
    def test_relative_value_is_rejected(self, env_var, resolver, monkeypatch):
        monkeypatch.setenv(env_var, "./config")

        with pytest.raises(UserDirError) as exc_info:
            resolver()

        message = str(exc_info.value)
        assert env_var in message
        assert "relative path" in message
        # Names the absolute equivalent so the fix is a copy-paste away.
        assert str(Path("./config").resolve()) in message

    @pytest.mark.parametrize(("env_var", "resolver"), RESOLVERS)
    def test_bare_name_is_rejected(self, env_var, resolver, monkeypatch):
        """'config' with no leading './' is relative too."""
        monkeypatch.setenv(env_var, "config")

        with pytest.raises(UserDirError):
            resolver()

    def test_outcome_does_not_depend_on_cwd(self, tmp_path, monkeypatch):
        """One env value, one outcome -- whichever directory fpl was run from."""
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        monkeypatch.setenv("FPL_CLI_CONFIG_DIR", "./config")

        for cwd in (tmp_path, elsewhere):
            monkeypatch.chdir(cwd)
            user_config_dir.cache_clear()
            with pytest.raises(UserDirError):
                user_config_dir()

    def test_tilde_value_is_still_accepted(self, tmp_path, monkeypatch):
        """~ expands to an absolute path, so it is not caught by the check."""
        home = tmp_path / "home"
        home.mkdir()
        monkeypatch.setenv("HOME", str(home))
        monkeypatch.setenv("FPL_CLI_CONFIG_DIR", "~/vault-config")

        assert user_config_dir() == (home / "vault-config").resolve()

    def test_absolute_value_is_still_accepted(self, tmp_path, monkeypatch):
        custom = tmp_path / "abs_config"
        monkeypatch.setenv("FPL_CLI_CONFIG_DIR", str(custom))

        assert user_config_dir() == custom


class TestRelativeOverrideInCLI:
    """The reported symptom: a command that exists reports itself as missing."""

    def test_command_reports_relative_config_dir(self, monkeypatch):
        from click.testing import CliRunner

        from fpl_cli.cli import main

        monkeypatch.setenv("FPL_CLI_CONFIG_DIR", "./config")
        user_config_dir.cache_clear()

        result = CliRunner().invoke(main, ["ratings"], catch_exceptions=False)

        assert result.exit_code == 1
        assert "FPL_CLI_CONFIG_DIR" in result.output
        assert "relative path" in result.output
        # The old failure mode: "No such command 'ratings'. Did you mean 'ratings'?"
        assert "Did you mean" not in result.output
        assert "Traceback" not in result.output


class TestLazyResolution:
    """Resolution happens per call, so a late override still lands."""

    @pytest.mark.parametrize(("env_var", "resolver"), RESOLVERS)
    def test_override_set_after_import_is_honoured(self, env_var, resolver, tmp_path, monkeypatch):
        first = resolver()
        moved = tmp_path / "moved"
        monkeypatch.setenv(env_var, str(moved))
        resolver.cache_clear()

        assert resolver() == moved
        assert resolver() != first

    def test_data_dir_from_dotenv_is_honoured(self, tmp_path):
        """FPL_CLI_DATA_DIR set in the config dir's .env must reach the resolvers.

        Regression guard: importing the CLI used to freeze the data dir before
        load_dotenv ran, so the .env route silently did nothing.
        """
        import subprocess
        import sys

        config_dir = tmp_path / "config"
        config_dir.mkdir()
        data_dir = tmp_path / "persistent-data"
        (config_dir / ".env").write_text(f"FPL_CLI_DATA_DIR={data_dir}\n", encoding="utf-8")

        env = {k: v for k, v in os.environ.items() if not k.startswith("FPL_CLI_")}
        env["FPL_CLI_CONFIG_DIR"] = str(config_dir)
        probe = (
            "import fpl_cli.cli\n"
            "from fpl_cli.paths import user_data_dir\n"
            "print(user_data_dir())\n"
        )
        result = subprocess.run(
            [sys.executable, "-c", probe],
            capture_output=True, text=True, env=env, cwd=tmp_path, check=True,
        )

        assert result.stdout.strip() == str(data_dir.resolve())


class TestUnusableOverrideInCLI:
    """The CLI turns an unusable override into an error message, not a traceback."""

    def test_command_reports_bad_data_dir(self, tmp_path, monkeypatch):
        from click.testing import CliRunner

        from fpl_cli.cli import main

        blocker = tmp_path / "regular_file"
        blocker.write_text("", encoding="utf-8")
        monkeypatch.setenv("FPL_CLI_DATA_DIR", str(blocker / "nested"))
        user_data_dir.cache_clear()

        result = CliRunner().invoke(main, ["chips"], catch_exceptions=False)

        assert result.exit_code == 1
        assert "FPL_CLI_DATA_DIR" in result.output
        assert "Traceback" not in result.output


class TestNoImportTimeResolution:
    """Guards the convention that keeps the FPL_CLI_* overrides working."""

    RESOLVER_NAMES = {"user_config_dir", "user_data_dir", "user_cache_dir"}

    def _module_level_calls(self, node: ast.AST) -> list[str]:
        """Names of resolvers called outside any function, method, or lambda body."""
        found: list[str] = []
        for child in ast.iter_child_nodes(node):
            # Function bodies run at call time, which is exactly what we want.
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
                continue
            if (
                isinstance(child, ast.Call)
                and isinstance(child.func, ast.Name)
                and child.func.id in self.RESOLVER_NAMES
            ):
                found.append(child.func.id)
            found.extend(self._module_level_calls(child))
        return found

    def test_package_never_resolves_a_user_dir_at_import_time(self):
        """A module-level constant would freeze the override before .env loads.

        Class bodies count too: a ClassVar default is evaluated at import.
        """
        package_root = Path(paths_mod.__file__).parent
        offenders = []
        for source in sorted(package_root.rglob("*.py")):
            if source == Path(paths_mod.__file__):
                continue  # paths.py defines them; its own calls are inside functions.
            tree = ast.parse(source.read_text(encoding="utf-8"))
            for name in self._module_level_calls(tree):
                offenders.append(f"{source.relative_to(package_root)}: {name}()")

        assert offenders == [], (
            "Resolve user dirs at point of use, not at import time: " + ", ".join(offenders)
        )


class TestMigrateLegacyFiles:
    def _setup(self, tmp_path, monkeypatch):
        """Create legacy/dest dirs and patch module-level path constants."""
        legacy_cfg = tmp_path / "legacy_config"
        legacy_data = tmp_path / "legacy_data"
        dest_cfg = tmp_path / "dest_config"
        dest_data = tmp_path / "dest_data"
        legacy_cfg.mkdir()
        legacy_data.mkdir()
        monkeypatch.setattr(paths_mod, "_LEGACY_CONFIG_DIR", legacy_cfg)
        monkeypatch.setattr(paths_mod, "_LEGACY_DATA_DIR", legacy_data)
        monkeypatch.setenv("FPL_CLI_CONFIG_DIR", str(dest_cfg))
        monkeypatch.setenv("FPL_CLI_DATA_DIR", str(dest_data))
        return legacy_cfg, legacy_data, dest_cfg, dest_data

    def test_copies_config_files(self, tmp_path, monkeypatch):
        legacy_cfg, _, dest_cfg, _ = self._setup(tmp_path, monkeypatch)
        (legacy_cfg / "settings.yaml").write_text("key: value")

        _migrate_legacy_files()

        assert (dest_cfg / "settings.yaml").read_text() == "key: value"

    def test_skips_existing_destination(self, tmp_path, monkeypatch):
        legacy_cfg, _, dest_cfg, _ = self._setup(tmp_path, monkeypatch)
        dest_cfg.mkdir()
        (legacy_cfg / "settings.yaml").write_text("new_value")
        (dest_cfg / "settings.yaml").write_text("existing_value")

        _migrate_legacy_files()

        assert (dest_cfg / "settings.yaml").read_text() == "existing_value"

    def test_handles_missing_legacy_dirs(self, tmp_path, monkeypatch):
        """Migration must not raise when legacy dirs don't exist."""
        monkeypatch.setattr(paths_mod, "_LEGACY_CONFIG_DIR", tmp_path / "nonexistent_config")
        monkeypatch.setattr(paths_mod, "_LEGACY_DATA_DIR", tmp_path / "nonexistent_data")
        monkeypatch.setenv("FPL_CLI_CONFIG_DIR", str(tmp_path / "dest_config"))
        monkeypatch.setenv("FPL_CLI_DATA_DIR", str(tmp_path / "dest_data"))

        _migrate_legacy_files()  # must not raise

    def test_data_files_fall_back_to_config_dir(self, tmp_path, monkeypatch):
        """Data files that lived in config/ (not data/) still migrate to user_data_dir."""
        legacy_cfg, legacy_data, _, dest_data = self._setup(tmp_path, monkeypatch)
        # team_ratings.yaml in config/, absent from data/
        (legacy_cfg / "team_ratings.yaml").write_text("ratings: {}")

        _migrate_legacy_files()

        assert (dest_data / "team_ratings.yaml").read_text() == "ratings: {}"

    def test_migrates_debug_directory(self, tmp_path, monkeypatch):
        _, legacy_data, _, dest_data = self._setup(tmp_path, monkeypatch)
        legacy_debug = legacy_data / "debug"
        legacy_debug.mkdir()
        (legacy_debug / "trace.json").write_text('{"ok": true}')

        _migrate_legacy_files()

        assert (dest_data / "debug" / "trace.json").read_text() == '{"ok": true}'

    def test_copy_error_is_handled(self, tmp_path, monkeypatch):
        """shutil.copy2 raising OSError must not propagate; a warning must be logged."""
        import shutil

        legacy_cfg, _, _, _ = self._setup(tmp_path, monkeypatch)
        (legacy_cfg / "settings.yaml").write_text("key: value")

        mock_logger = MagicMock()
        monkeypatch.setattr(paths_mod, "logger", mock_logger)
        monkeypatch.setattr(shutil, "copy2", MagicMock(side_effect=OSError("disk full")))

        _migrate_legacy_files()  # must not raise

        mock_logger.warning.assert_called()
