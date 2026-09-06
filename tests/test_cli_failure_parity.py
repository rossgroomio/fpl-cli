"""A failure is answered the same way whichever format asked for it (#286).

The third contract over the same walk. `test_cli_json_contract` checks what
`--format json` puts on stdout; `test_cli_failure_streams` checks what table
mode puts on stderr. Neither compares the two, so the same refusal could be —
and repeatedly was — two different answers depending on which format the
caller happened to use.

That is invisible to both existing walks by construction: each treats "did not
exit 1" as nothing to assert, so the softened table half of `fpl history`,
`fpl chips timing` and `fpl waivers` sailed through the stream walk while the
strict JSON half passed the envelope walk beside it. What broke was a script:
`fpl chips timing && post-to-slack` posted on a command that had produced no
signals, and the same pipeline written with `--format json` stopped.

Two questions, one walk:

* **The exit code.** It is the one part of a CLI's answer that does not change
  with the format it was asked in.
* **The reason.** Under `--format json` the reason lives in the envelope and
  nowhere else — the consumer is told to script against `error` and ignore
  stderr — so an envelope carrying a category (`Failed to fetch historical
  data`) where the terminal gets the cause (`connection refused`) leaves the
  reader who cannot see the other stream as the only one in the dark.

A command that exits 0 in both formats is skipped by all three walks. That is
deliberate: an unconfigured install is not a failure everywhere, and the walks
have no way to tell a command that should have refused from one that correctly
did not. The exit-code test below is what keeps that skip from hiding a
divergence.
"""

from __future__ import annotations

import json

import pytest
from click.testing import CliRunner

from fpl_cli.cli import main

# The same walk both other contracts drive, down the same outage: one list of
# commands, one `offline` fixture, three questions asked of them.
from tests.test_cli_json_contract import JSON_COMMANDS, REQUIRED_PARAMS


def _args_for(command: tuple[str, ...]) -> list[str]:
    return list(command) + REQUIRED_PARAMS.get(" ".join(command), [])


def _squash(text: str) -> str:
    """Text with every space removed, for comparing across two renderings.

    Rich wraps table-mode prose to the terminal width and will break mid-token
    to do it, so `.../previews` comes back as `.../previe ws` next to the
    envelope's unwrapped copy of the same sentence. Whitespace is the one
    difference between the two that carries no meaning.
    """
    return "".join(text.split())


def _both_formats(command: tuple[str, ...]) -> tuple:
    args = _args_for(command)
    return (
        CliRunner().invoke(main, args),
        CliRunner().invoke(main, args + ["--format", "json"]),
    )


@pytest.mark.parametrize(
    "command", JSON_COMMANDS, ids=[" ".join(c) or "(root)" for c in JSON_COMMANDS],
)
def test_the_same_failure_exits_the_same_way_in_both_formats(command, offline):
    """One command, one unreachable upstream, two formats, one exit code.

    Both directions fail here, not just the one that bit: a table path that
    exits 0 where the envelope exits 1 hides a failure from `&&`, and a table
    path that exits 1 where the envelope exits 0 invents one. Equality is the
    whole assertion — a command that legitimately exits 0 on both is as
    passing as one that exits 1.
    """
    table, envelope = _both_formats(command)

    assert table.exit_code == envelope.exit_code, (
        f"`fpl {' '.join(_args_for(command))}` exits {table.exit_code} in table mode and "
        f"{envelope.exit_code} under `--format json` on the same failure. "
        f"table stderr: {table.stderr[:160]!r}"
    )


@pytest.mark.parametrize(
    "command", JSON_COMMANDS, ids=[" ".join(c) or "(root)" for c in JSON_COMMANDS],
)
def test_the_envelope_names_the_cause_the_terminal_was_given(command, offline):
    """The `error` string is the reason, not a category standing in for it.

    Containment rather than equality, because the two are not the same kind of
    text. Table mode may say more — an agent's progress lines precede its
    failure, and `handle_agent_failure` frames the message as `Agent failed:
    <message>` for a human. What it may not do is say *more than the envelope
    does about why*, which is what `fpl history` and `fpl price-history` did:
    a fixed `Failed to fetch ... data` in the envelope while the upstream's
    own words went to the stream a JSON consumer is told to ignore.
    """
    table, envelope = _both_formats(command)
    if table.exit_code != 1 or envelope.exit_code != 1:
        return

    error = json.loads(envelope.stdout)["error"]
    assert _squash(error) in _squash(table.stderr), (
        f"`fpl {' '.join(_args_for(command))}` tells a JSON consumer {error!r} "
        f"and a terminal something else: {table.stderr[:200]!r}"
    )
