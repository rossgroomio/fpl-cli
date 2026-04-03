"""Canonical path constants for the fpl-cli project.

Three categories:
- SHIPPED_CONFIG_DIR / TEMPLATE_DIR: read-only data shipped inside the package
- user_config_dir() / user_data_dir(): writable dirs via platformdirs (lazy, cached)

Every module that needs config, data, or templates should import from here.
"""

from __future__ import annotations

import functools
import logging
import os
import shutil
from pathlib import Path

logger = logging.getLogger(__name__)

_PACKAGE_DIR = Path(__file__).resolve().parent

SHIPPED_CONFIG_DIR = _PACKAGE_DIR / "config"
TEMPLATE_DIR = _PACKAGE_DIR / "templates"

# Legacy repo-root paths for one-time migration
_LEGACY_CONFIG_DIR = _PACKAGE_DIR.parent / "config"
_LEGACY_DATA_DIR = _PACKAGE_DIR.parent / "data"

# Files that should migrate to user_config_dir
_USER_CONFIG_FILES = (
    "fixture_predictions.yaml",
    "team_managers.yaml",
    "team_ratings_overrides.yaml",
    "settings.yaml",
)

# Files that should migrate to user_data_dir
_USER_DATA_FILES = (
    "player_prior.yaml",
    "team_ratings.yaml",
    "team_ratings_prior.yaml",
    "chip_plan.json",
    "team_finances.json",
    "transfer_plan.json",
)


@functools.lru_cache(maxsize=1)
def user_config_dir() -> Path:
    """User-editable config directory (platformdirs). Respects FPL_CLI_CONFIG_DIR env var."""
    env = os.environ.get("FPL_CLI_CONFIG_DIR")
    if env:
        p = Path(env).expanduser().resolve()
    else:
        from platformdirs import user_config_path

        p = user_config_path("fpl-cli", appauthor=False, ensure_exists=True)
    p.mkdir(parents=True, exist_ok=True)
    if os.name != "nt":
        p.chmod(0o700)
    return p


@functools.lru_cache(maxsize=1)
def user_data_dir() -> Path:
    """Runtime cache directory (platformdirs)."""
    from platformdirs import user_data_path

    p = user_data_path("fpl-cli", appauthor=False, ensure_exists=True)
    p.mkdir(parents=True, exist_ok=True)
    if os.name != "nt":
        p.chmod(0o700)
    return p


def _migrate_legacy_files() -> None:
    """One-time migration of files from repo-root config/ and data/ to platformdirs."""
    for filename in _USER_CONFIG_FILES:
        src = _LEGACY_CONFIG_DIR / filename
        dst = user_config_dir() / filename
        if src.is_file() and not dst.exists():
            shutil.copy2(src, dst)
            logger.info("Migrated %s → %s", src, dst)

    for filename in _USER_DATA_FILES:
        src = _LEGACY_DATA_DIR / filename
        if not src.is_file():
            # Some data files lived in config/ (player_prior, team_ratings, team_ratings_prior)
            src = _LEGACY_CONFIG_DIR / filename
        dst = user_data_dir() / filename
        if src.is_file() and not dst.exists():
            shutil.copy2(src, dst)
            logger.info("Migrated %s → %s", src, dst)

    # Migrate debug/ subdirectory
    legacy_debug = _LEGACY_DATA_DIR / "debug"
    if legacy_debug.is_dir():
        dest_debug = user_data_dir() / "debug"
        if not dest_debug.exists():
            shutil.copytree(legacy_debug, dest_debug)
            logger.info("Migrated %s → %s", legacy_debug, dest_debug)


_migrate_legacy_files()
