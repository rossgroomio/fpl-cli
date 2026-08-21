"""Canonical path constants for the fpl-cli project.

Three categories:
- SHIPPED_CONFIG_DIR / TEMPLATE_DIR: read-only data shipped inside the package
- user_config_dir() / user_data_dir(): writable dirs via platformdirs (lazy, cached)
- user_cache_dir(): cache dir via platformdirs (lazy, cached, disposable)

Every writable dir honours an env var override (FPL_CLI_CONFIG_DIR,
FPL_CLI_DATA_DIR, FPL_CLI_CACHE_DIR) so ephemeral environments (e.g. Claude
Code on the web) can redirect them to a persistent workspace. Overrides must
be absolute: a relative one resolves against the current working directory,
which makes the CLI read a different directory per invocation, so it is
rejected with an actionable error rather than honoured.

Resolution is lazy and cached: nothing here touches the filesystem at import
time, so an override set after import (notably from the `.env` the CLI loads)
is still honoured. Modules must therefore call these functions where the path
is used rather than binding the result to a module-level constant.

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
)


class UserDirError(RuntimeError):
    """An FPL_CLI_* directory override points somewhere unusable."""


def _resolve_user_dir(env_var: str, platformdirs_func: str) -> Path:
    """Resolve one writable dir: env override if set, else platformdirs.

    Args:
        env_var: Override variable name (e.g. "FPL_CLI_DATA_DIR").
        platformdirs_func: Name of the platformdirs function to fall back on.
            Looked up on the module at call time so tests can patch it.

    Raises:
        UserDirError: The override is relative, or the resolved directory
            could not be created.
    """
    env = os.environ.get(env_var)
    if env:
        path = Path(env).expanduser()
        if not path.is_absolute():
            # Resolving a relative override against the cwd would give a
            # different directory per invocation, so config silently loads
            # only when fpl is run from one place (#46). No stable anchor
            # exists for a CLI, so say so rather than guess one.
            raise UserDirError(
                f"{env_var} is set to {env!r}, which is a relative path. It would be "
                f"resolved against the current working directory, so fpl-cli would read "
                f"a different directory depending on where you ran it from. "
                f"Use an absolute path (from here that is {Path(env).expanduser().resolve()}), "
                f"or unset it to use the default location."
            )
        path = path.resolve()
        # A directory the user pointed us at may be shared with other tools, so
        # its mode is theirs to set. Only lock down one we create ourselves.
        restrict = not path.exists()
    else:
        import platformdirs

        path = getattr(platformdirs, platformdirs_func)("fpl-cli", appauthor=False, ensure_exists=True)
        restrict = True

    try:
        path.mkdir(parents=True, exist_ok=True)
        if restrict and os.name != "nt":
            path.chmod(0o700)
    except OSError as exc:
        if env:
            raise UserDirError(
                f"{env_var} is set to {env!r}, which cannot be used as a directory: {exc}. "
                f"Point it at a writable directory, or unset it to use the default location."
            ) from exc
        raise UserDirError(f"Could not create the fpl-cli directory {path}: {exc}") from exc

    return path


@functools.lru_cache(maxsize=1)
def user_config_dir() -> Path:
    """User-editable config directory (platformdirs). Respects FPL_CLI_CONFIG_DIR env var.

    Cached after first call. Tests that change FPL_CLI_CONFIG_DIR must call
    user_config_dir.cache_clear() first (handled by the autouse fixture in conftest.py).
    """
    return _resolve_user_dir("FPL_CLI_CONFIG_DIR", "user_config_path")


@functools.lru_cache(maxsize=1)
def user_cache_dir() -> Path:
    """Disposable cache directory (platformdirs). Respects FPL_CLI_CACHE_DIR env var.

    Cached after first call. Tests that change FPL_CLI_CACHE_DIR must call
    user_cache_dir.cache_clear() first (handled by the autouse fixture in conftest.py).
    """
    return _resolve_user_dir("FPL_CLI_CACHE_DIR", "user_cache_path")


@functools.lru_cache(maxsize=1)
def user_data_dir() -> Path:
    """Generated-data directory (platformdirs). Respects FPL_CLI_DATA_DIR env var.

    Cached after first call. Tests that change FPL_CLI_DATA_DIR must call
    user_data_dir.cache_clear() first (handled by the autouse fixture in conftest.py).
    """
    return _resolve_user_dir("FPL_CLI_DATA_DIR", "user_data_path")


def _migrate_legacy_files() -> None:
    """One-time migration of files from repo-root config/ and data/ to platformdirs."""
    try:
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
    except UserDirError:
        # An unusable FPL_CLI_* override is a config error the caller reports
        # with an actionable message; swallowing it here would duplicate it.
        raise
    except Exception as exc:  # noqa: BLE001 — migration is best-effort; must not break CLI startup
        # No traceback: fpl-cli configures no logging handlers, so logging's
        # lastResort handler would dump it raw into the middle of CLI output.
        logger.warning("Legacy file migration failed: %s", exc)


_migration_done = False


def ensure_legacy_migration() -> None:
    """Run the legacy-file migration once per process.

    Called by the CLI entry point rather than at import time: resolving the
    user dirs during import would freeze them before the CLI has loaded the
    `.env` that may set FPL_CLI_DATA_DIR / FPL_CLI_CACHE_DIR.
    """
    global _migration_done
    if _migration_done:
        return
    _migration_done = True
    _migrate_legacy_files()
