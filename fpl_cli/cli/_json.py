"""Shared JSON output infrastructure for CLI commands."""

from __future__ import annotations

import functools
import json
import sys
from contextlib import contextmanager
from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import IO, Any, Callable, Generator, NoReturn, TypeVar, cast

import click
import httpx
from rich.markup import escape as rich_escape

from fpl_cli.cli._context import ConfigError, error_console

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
    cause: BaseException | None = None,
) -> NoReturn:
    """Write a JSON error envelope to stdout and exit with code 1.

    Failure goes down the same stream as success (#141): a consumer that
    parses stdout gets either envelope, and one that only checks the exit
    code still sees 1. Prose stays on stderr.

    *cause* chains the original exception onto the `SystemExit`. It has to be
    raised here rather than by the caller: this function never returns, so a
    `raise ... from exc` after the call is unreachable and the chain is lost.
    """
    envelope: dict[str, Any] = {"command": command, "error": message}
    print(json.dumps(envelope, indent=2, default=_json_default), file=_stream(file))
    raise SystemExit(1) from cause


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

    Table-mode prose goes to stderr, every command alike (#162). It used to
    follow whichever stream each call site happened to pick, so `2>/dev/null`
    silenced the reason for exiting on some commands and `> out.txt` captured
    it on others -- a split no user could have predicted, because nothing
    described it. stdout carries the output a command was asked for; the
    explanation for producing none of it does not belong there.
    """
    if output_format == "json":
        emit_json_error(command, message, cause=cause)
    error_console.print(f"[red]{rich_escape(message)}[/red]")
    raise SystemExit(1) from cause


@contextmanager
def api_failure_boundary(command: str, output_format: str) -> Generator[None, None, None]:
    """Turn an unreachable upstream into an error envelope instead of a traceback.

    An outage is the likeliest way any of these commands fails, and it
    surfaces as an `httpx.HTTPError` raised deep in a client call -- far from
    the code that knows the command name and the output format. Left
    uncaught it reaches click as a traceback: stdout stays empty, so a JSON
    consumer gets no envelope at all and cannot tell an outage from a crash.

    Wrap the `asyncio.run()` call rather than each request, so a client added
    to an existing command inherits the handling. Only `httpx.HTTPError` is
    caught -- a bug in our own code still raises, where it can be seen.
    """
    try:
        yield
    except httpx.HTTPStatusError as exc:
        # Reached, not unreachable: say so. A command that lets a 404 through
        # here has a gap in its own handling, and reporting it as an outage
        # would send the reader looking at their network (#159 review).
        emit_failure(
            command,
            f"The FPL API returned {exc.response.status_code} for {exc.request.url.path}",
            output_format,
            cause=exc,
        )
    except httpx.HTTPError as exc:
        emit_failure(command, f"Could not reach the FPL API: {exc}", output_format, cause=exc)


def config_failure_boundary(func: F) -> F:
    """Report a malformed settings block as this command's failure envelope.

    The sibling of `api_failure_boundary`, for the other failure a command
    cannot see coming. Config is parsed lazily, deep in whichever command
    needs it, so a `ConfigError` is raised far from the code that knows the
    command name and the output format. Left to propagate it reaches click as
    a traceback: stdout stays empty, and a `--format json` consumer gets no
    envelope at all -- indistinguishable from a hang or a killed process
    (#170). One mistyped `fines:` rule took out `league-fines`, `status` and
    `league-recap` alike, because the settings are read on nearly every
    command.

    A decorator rather than a context manager, because there is no single
    seam to wrap the way `asyncio.run()` is one for an outage: `status`
    parses fines in two branches and `league-recap` reaches its parse through
    three helpers. Wrapping the callback covers the body wherever the parse
    happens, and both halves of the envelope are read off the click context,
    so a command adopting it cannot name itself one thing here and another in
    its `emit_json`.

    Failing rather than degrading is deliberate. A `fines:` block the parser
    cannot read is not the same as no fines configured, and `league-recap`
    treating it as such would stamp an empty `fine_rules_evaluated` into the
    append-only ledger -- the false acquittal issue #136 exists to prevent,
    and a season's worth of them cannot be undone.
    """
    @functools.wraps(func)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        try:
            return func(*args, **kwargs)
        except ConfigError as exc:
            ctx = click.get_current_context(silent=True)
            if ctx is None:  # called outside click, e.g. directly from a test
                emit_failure(func.__name__, str(exc), "table", cause=exc)
            emit_failure(
                ctx.command.name or func.__name__,
                str(exc),
                ctx.params.get("output_format", "table"),
                cause=exc,
            )

    return cast(F, wrapper)


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
