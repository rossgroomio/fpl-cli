"""Every `--format json` command keeps stdout machine-parseable.

`docs/command-reference.md` promises a consumer one thing: parse stdout and
you get an envelope either way -- `{command, metadata, data}` on success,
`{command, error}` on failure -- with every human-readable line on stderr.
Individual command tests assert that for the paths they happen to exercise,
which is how #140, #141 and #144 all reached a release: each was a path
nobody had a test for, in a command whose happy path was covered.

This module tests the contract instead of the commands. It walks the click
tree, so a new `--format json` command is covered the moment it is
registered, and drives every one of them down a failure path -- the FPL API
unreachable, no entry IDs configured -- because that is the side the bugs
were on. Prose printed before the envelope, or an early `return` that skips
it, breaks a consumer here rather than in someone's script.
"""

from __future__ import annotations

import json

import click
import httpx
import pytest
import yaml
from click.testing import CliRunner

from fpl_cli.cli import main

# Commands whose arguments are required. Without them click exits 2 before the
# command body runs, which tests click rather than the contract.
REQUIRED_ARGS: dict[str, list[str]] = {
    "player": ["Salah"],
    "intel show": ["Arsenal"],
    "intel resolve": ["Arsenal"],
}


def _json_commands(cmd: click.Command, path: tuple[str, ...] = ()) -> list[tuple[str, ...]]:
    """Every command path in the tree that accepts `--format json`.

    A group can carry the option itself (`invoke_without_command=True`, as
    `fpl squad` and `fpl chips` do), so groups are yielded as well as walked.
    """
    found: list[tuple[str, ...]] = []
    accepts_format = any(p.name == "output_format" for p in cmd.params)
    if isinstance(cmd, click.Group):
        if getattr(cmd, "invoke_without_command", False) and accepts_format:
            found.append(path)
        for name, sub in cmd.commands.items():
            found.extend(_json_commands(sub, path + (name,)))
    elif accepts_format:
        found.append(path)
    return found


JSON_COMMANDS = _json_commands(main)


def test_command_discovery_found_the_tree():
    """Guard the walker: a silent zero here would make every test below vacuous."""
    assert len(JSON_COMMANDS) > 15
    assert ("stats",) in JSON_COMMANDS
    assert ("squad", "grid") in JSON_COMMANDS
    assert ("squad",) in JSON_COMMANDS, "invoke_without_command groups must be covered"


@pytest.fixture(params=[True, False], ids=["custom-on", "custom-off"])
def offline(request, monkeypatch, tmp_path):
    """A configured install whose upstream APIs are all unreachable.

    Run twice, because `custom_analysis` picks between two different bodies
    for the same command -- `fdr` serves Bayesian ratings under one and raw
    API difficulty under the other, and only the second has the early return
    that skips the envelope. No entry IDs are set either way, so the commands
    that need one take their not-configured path. All of it is ordinary
    first-week state for a real user.
    """
    (tmp_path / "user-config" / "settings.yaml").write_text(
        yaml.safe_dump({"custom_analysis": request.param}), encoding="utf-8",
    )

    async def _unreachable(*args, **kwargs):
        raise httpx.ConnectError("connection refused")

    def _unreachable_sync(*args, **kwargs):
        raise httpx.ConnectError("connection refused")

    for module, attr in (
        ("fpl_cli.api.fpl", "FPLClient"),
        ("fpl_cli.api.fpl_draft", "FPLDraftClient"),
    ):
        mod = __import__(module, fromlist=[attr])
        monkeypatch.setattr(getattr(mod, attr), "_get", _unreachable, raising=False)
    monkeypatch.setattr(httpx.AsyncClient, "send", _unreachable)
    monkeypatch.setattr(httpx.Client, "send", _unreachable_sync)


@pytest.mark.parametrize(
    "command", JSON_COMMANDS, ids=[" ".join(c) or "(root)" for c in JSON_COMMANDS],
)
def test_stdout_is_json_or_empty_on_failure(command, offline):
    """stdout parses as JSON from byte 0, whatever the command did.

    Exit 2 is click's own parameter handling, which writes its usage message
    to stderr and never reaches the command body -- that is click's contract,
    not ours, so the only thing to assert is that it left stdout alone.
    """
    args = list(command) + REQUIRED_ARGS.get(" ".join(command), []) + ["--format", "json"]
    result = CliRunner().invoke(main, args)

    if result.exception is not None and not isinstance(result.exception, SystemExit):
        raise AssertionError(
            f"`fpl {' '.join(args)}` raised {result.exception!r} instead of reporting "
            f"the failure as an envelope"
        ) from result.exception

    if result.exit_code == 2:
        assert result.stdout == "", (
            f"`fpl {' '.join(args)}` wrote to stdout on a click usage error"
        )
        return

    assert result.stdout.strip(), (
        f"`fpl {' '.join(args)}` exited {result.exit_code} with nothing on stdout -- "
        f"a consumer gets no envelope to parse. stderr was: {result.stderr[:300]!r}"
    )
    try:
        envelope = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise AssertionError(
            f"`fpl {' '.join(args)}` stdout is not JSON ({exc}). "
            f"First 200 chars: {result.stdout[:200]!r}"
        ) from exc
    assert envelope["command"], "envelope must name the command that produced it"


@pytest.mark.parametrize(
    "command", JSON_COMMANDS, ids=[" ".join(c) or "(root)" for c in JSON_COMMANDS],
)
def test_exit_code_matches_envelope_kind(command, offline):
    """Exit 0 means data, exit 1 means an error, and nothing else means either.

    The exit code is the only signal a shell script checks before it bothers
    parsing, so an error envelope behind exit 0 (#144) is as broken as no
    envelope at all.
    """
    args = list(command) + REQUIRED_ARGS.get(" ".join(command), []) + ["--format", "json"]
    result = CliRunner().invoke(main, args)

    if result.exit_code == 2:
        return

    assert result.exit_code in (0, 1), (
        f"`fpl {' '.join(args)}` exited {result.exit_code}; the contract defines 0 and 1"
    )
    envelope = json.loads(result.stdout)
    if result.exit_code == 0:
        assert "error" not in envelope, "exit 0 must not carry an error envelope"
        assert "data" in envelope, "a success envelope carries data"
    else:
        assert "error" in envelope, (
            f"`fpl {' '.join(args)}` exited 1 without an error envelope: {envelope!r}"
        )
