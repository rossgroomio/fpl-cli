"""Shared JSON output infrastructure for CLI commands."""

from __future__ import annotations

import json
import sys
from contextlib import contextmanager
from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import IO, Any, Callable, Generator, NoReturn, TypeVar

import click
from rich.markup import escape as rich_escape

from fpl_cli.cli._context import console

F = TypeVar("F", bound=Callable[..., Any])


def output_format_option(func: F) -> F:
    """Add --format table|json option to a command."""
    return click.option(
        "--format", "output_format",
        type=click.Choice(["table", "json"], case_sensitive=False),
        default="table",
        help="Output format (table or json for scripting)",
    )(func)


def _json_default(obj: object) -> Any:
    """Handle non-standard types in JSON serialisation.

    Converts datetime to ISO 8601, Decimal to float, Enum to value.
    Raises TypeError on unknown types to surface bugs early.
    """
    if isinstance(obj, datetime):
        return obj.isoformat()
    if isinstance(obj, Decimal):
        return float(obj)
    if isinstance(obj, Enum):
        return obj.value
    raise TypeError(f"Object of type {type(obj).__name__} is not JSON serializable")


# Real stdout while `json_output_mode()` is active. Both envelopes must land
# there: inside that context `sys.stdout` is stderr, so a call site that forgot
# `file=stdout` would otherwise put JSON on the prose stream (#141).
_real_stdout: IO[str] | None = None


def _stream(file: IO[str] | None) -> IO[str]:
    """Where an envelope goes: an explicit file, else the real stdout."""
    if file is not None:
        return file
    return _real_stdout if _real_stdout is not None else sys.stdout


def emit_json(
    command: str,
    data: Any,
    metadata: dict[str, Any] | None = None,
    *,
    file: IO[str] | None = None,
) -> None:
    """Write a JSON envelope to stdout (or given file).

    Uses print() not click.echo() - JSON is always UTF-8 and
    click.echo's encoding handling can mangle bytes.

    Inside `json_output_mode()` this still reaches the real stdout, whether
    or not the caller passes the handle that context yields.
    """
    envelope: dict[str, Any] = {"command": command}
    envelope["metadata"] = metadata or {}
    envelope["data"] = data
    print(json.dumps(envelope, indent=2, default=_json_default), file=_stream(file))


def emit_json_error(
    command: str,
    message: str,
    *,
    file: IO[str] | None = None,
) -> None:
    """Write a JSON error envelope to stdout and exit with code 1.

    Failure goes down the same stream as success (#141): a consumer that
    parses stdout gets either envelope, and one that only checks the exit
    code still sees 1. Prose stays on stderr.
    """
    envelope: dict[str, Any] = {"command": command, "error": message}
    print(json.dumps(envelope, indent=2, default=_json_default), file=_stream(file))
    raise SystemExit(1)


def emit_failure(
    command: str,
    message: str,
    output_format: str,
    *,
    cause: BaseException | None = None,
) -> NoReturn:
    """Report *message* on the channel the caller parses, then exit 1.

    JSON consumers read the `{command, error}` envelope; a terminal reads red
    prose. The two are mutually exclusive: prose printed under `--format json`
    would sit ahead of an envelope that never arrives and break the parse at
    byte 0 (#140).
    """
    if output_format == "json":
        emit_json_error(command, message)
    else:
        console.print(f"[red]{rich_escape(message)}[/red]")
    raise SystemExit(1) from cause


@contextmanager
def json_output_mode() -> Generator[IO[str], None, None]:
    """Redirect sys.stdout to stderr so JSON payload stays clean.

    Yields the real stdout for the caller to write JSON to.
    All console.print() calls (both CLI and agent consoles) go to
    stderr while inside this context, preventing JSON stream corruption.

    Only the outermost entry records the real stdout: on a nested entry
    `sys.stdout` is already stderr, so recording it there would hand the
    caller the prose stream -- #141 again, one level down.

    Safe in single-threaded asyncio.run() CLI.
    """
    global _real_stdout
    original_stdout = sys.stdout
    previous_real_stdout = _real_stdout
    real_stdout = previous_real_stdout if previous_real_stdout is not None else sys.stdout
    _real_stdout = real_stdout
    sys.stdout = sys.stderr
    try:
        yield real_stdout
    finally:
        sys.stdout = original_stdout
        _real_stdout = previous_real_stdout
