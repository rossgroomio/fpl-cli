"""Unit tests for fpl_cli/paths.py."""

from __future__ import annotations

import os
from unittest.mock import MagicMock

import pytest

import fpl_cli.paths as paths_mod
from fpl_cli.paths import (
    SHIPPED_CONFIG_DIR,
    TEMPLATE_DIR,
    _migrate_legacy_files,
    user_config_dir,
    user_data_dir,
)


class TestShippedPaths:
    def test_shipped_config_dir_exists(self):
        assert SHIPPED_CONFIG_DIR.is_dir()

    def test_template_dir_exists(self):
        assert TEMPLATE_DIR.is_dir()


class TestUserConfigDir:
    def test_returns_platformdirs_path(self, tmp_path, monkeypatch):
        expected = tmp_path / "config"
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
    def test_sets_permissions(self, tmp_path, monkeypatch):
        custom = tmp_path / "perm_dir"
        monkeypatch.setenv("FPL_CLI_CONFIG_DIR", str(custom))
        user_config_dir()
        assert custom.stat().st_mode & 0o777 == 0o700


class TestUserDataDir:
    def test_returns_platformdirs_path(self, tmp_path, monkeypatch):
        expected = tmp_path / "data"
        monkeypatch.setattr("platformdirs.user_data_path", lambda *_args, **_kwargs: expected)
        result = user_data_dir()
        assert result == expected

    def test_creates_directory(self, tmp_path, monkeypatch):
        new_dir = tmp_path / "new_data"
        monkeypatch.setattr("platformdirs.user_data_path", lambda *_args, **_kwargs: new_dir)
        assert not new_dir.exists()
        user_data_dir()
        assert new_dir.is_dir()

    @pytest.mark.skipif(os.name == "nt", reason="chmod not applicable on Windows")
    def test_sets_permissions(self, tmp_path, monkeypatch):
        perm_dir = tmp_path / "perm_data"
        monkeypatch.setattr("platformdirs.user_data_path", lambda *_args, **_kwargs: perm_dir)
        user_data_dir()
        assert perm_dir.stat().st_mode & 0o777 == 0o700


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
        monkeypatch.setattr("platformdirs.user_data_path", lambda *_args, **_kwargs: dest_data)
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
        monkeypatch.setattr("platformdirs.user_data_path", lambda *_args, **_kwargs: tmp_path / "dest_data")

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
