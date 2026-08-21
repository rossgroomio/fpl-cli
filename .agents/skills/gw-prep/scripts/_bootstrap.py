"""Shared startup for gw-prep scripts that import fpl-cli agents directly."""

from __future__ import annotations

import json
import sys

from fpl_cli.paths import UserDirError, ensure_legacy_migration, load_env_files


def bootstrap_user_dirs() -> None:
    """Run fpl-cli's legacy-file migration before any agent resolves paths.

    The CLI does this in its own entry point; scripts importing agents
    directly must do it themselves or a user with files in the legacy
    repo-root dirs silently gets empty settings and regenerated ratings.
    A bad FPL_CLI_* override surfaces here as a clean JSON error instead
    of a traceback mid-agent.
    """
    try:
        load_env_files()
        ensure_legacy_migration()
    except UserDirError as exc:
        json.dump({"error": True, "messages": [str(exc)]}, sys.stdout, indent=2)
        sys.exit(1)
