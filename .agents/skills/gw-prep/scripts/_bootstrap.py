"""Shared startup for gw-prep scripts that import the fpl-cli package.

Importing this module is itself the interpreter guard. Every script here
needs `fpl_cli` importable, and a standalone `fpl` on PATH (uv tool, pipx)
provides the command without putting the package on the system
interpreter's import path — so `fpl status` works and `python3
transfer_eval.py` does not. Reaching for the package here, first and alone,
turns that mistake into the same JSON error envelope the callers already
parse rather than a traceback from halfway down a script's import list.
"""

from __future__ import annotations

import json
import sys
from typing import IO, Any, NoReturn

_WRONG_INTERPRETER = (
    "Cannot import the 'fpl_cli' package on this interpreter ({executable}). "
    "These scripts run inside fpl-cli's own environment: activate its venv "
    "('source .venv/bin/activate') or invoke that venv's Python directly. A "
    "standalone 'fpl' on PATH (uv tool, pipx) provides the command but not the "
    "importable package."
)


def fail(messages: list[str]) -> NoReturn:
    """Emit the error envelope callers parse from stdout, then exit 1.

    Startup only -- a wrong interpreter, a bad `FPL_CLI_*` override -- so it
    always runs before a script has entered `json_output_mode()` and the
    default stream is the right one.
    """
    emit({"error": True, "messages": messages})
    sys.exit(1)


def emit(payload: dict[str, Any], stream: IO[str] | None = None) -> None:
    """Write a payload to the stream the caller parses.

    *stream* is the real stdout handle `json_output_mode()` yields, and the
    scripts run their agents inside that context so agent progress lines land
    on stderr rather than ahead of this JSON (#226). Inside it `sys.stdout`
    *is* stderr, so a payload written there would vanish from the stream the
    caller reads -- hence the explicit handle. Outside it, before any agent
    exists, the default is right.
    """
    json.dump(payload, stream if stream is not None else sys.stdout, indent=2)


def is_fpl_cli_missing(exc: ModuleNotFoundError) -> bool:
    """True when the fpl_cli package itself is absent from this interpreter.

    Only the top-level miss earns the envelope below. Python names the first
    component it could not find, so an absent package raises with
    name="fpl_cli" while a corrupt install missing one file raises with
    name="fpl_cli.paths" — that one is on the right interpreter, and telling
    its reader to activate a venv would send them somewhere useless. A
    missing dependency (name="pydantic") is the same kind of wrong. Both
    keep their traceback, which says more than this guard could.
    """
    return exc.name == "fpl_cli"


try:
    from fpl_cli.paths import (
        UserDirError,
        ensure_legacy_migration,
        ensure_user_dirs_valid,
        load_env_files,
    )
except ModuleNotFoundError as exc:
    if not is_fpl_cli_missing(exc):
        raise
    fail([_WRONG_INTERPRETER.format(executable=sys.executable), f"Import failed: {exc}"])


def bootstrap_user_dirs() -> None:
    """Run fpl-cli's legacy-file migration before any agent resolves paths.

    The CLI does this in its own entry point; scripts importing agents
    directly must do it themselves or a user with files in the legacy
    repo-root dirs silently gets empty settings and regenerated ratings.
    A bad FPL_CLI_* override surfaces here as a clean JSON error instead
    of a traceback mid-agent.

    `ensure_user_dirs_valid()` is the CLI's other startup step: without it, a
    relative FPL_CLI_DATA_DIR/FPL_CLI_CACHE_DIR resolves silently against
    this script's launching cwd instead of failing here, in an installed venv
    where the legacy dirs migration checks for never exist (#139 review).
    """
    try:
        load_env_files()
        ensure_legacy_migration()
        ensure_user_dirs_valid()
    except UserDirError as exc:
        fail([str(exc)])
