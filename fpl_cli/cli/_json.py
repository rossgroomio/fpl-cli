"""Shared JSON output infrastructure for CLI commands."""

from __future__ import annotations

import json
import sys
from contextlib import contextmanager
from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import IO, Any, Callable, Generator, TypeVar

import click

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
    """
    envelope: dict[str, Any] = {"command": command}
    envelope["metadata"] = metadata or {}
    envelope["data"] = data
    print(json.dumps(envelope, indent=2, default=_json_default), file=file)


def emit_json_error(
    command: str,
    message: str,
    *,
    file: IO[str] | None = None,
) -> None:
    """Write a JSON error to stderr and exit with code 1.

    If file is provided, writes there instead of stderr.
    """
    envelope: dict[str, Any] = {"command": command, "error": message}
    target = file if file is not None else sys.stderr
    print(json.dumps(envelope, indent=2, default=_json_default), file=target)
    raise SystemExit(1)


@contextmanager
def json_output_mode() -> Generator[IO[str], None, None]:
    """Redirect sys.stdout to stderr so JSON payload stays clean.

    Yields the original stdout for the caller to write JSON to.
    All console.print() calls (both CLI and agent consoles) go to
    stderr while inside this context, preventing JSON stream corruption.

    Safe in single-threaded asyncio.run() CLI.
    """
    original_stdout = sys.stdout
    sys.stdout = sys.stderr
    try:
        yield original_stdout
    finally:
        sys.stdout = original_stdout
