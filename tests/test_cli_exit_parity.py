"""A failure exits the same way whichever format asked for it (#286).

The third contract over the same walk. `test_cli_json_contract` checks what
`--format json` puts on stdout; `test_cli_failure_streams` checks what table
mode puts on stderr. Neither compares the two, so a command could -- and three
did -- report the same refusal as an error envelope with exit 1 under
`--format json` and as a warning with exit 0 without it.

That divergence is invisible to both existing walks by construction: each
treats "did not exit 1" as nothing to assert, so the table half of `fpl
history`, `fpl chips timing` and `fpl waivers` sailed through the stream walk
while the JSON half was passing the envelope walk beside it. What broke was a
script: `fpl chips timing && post-to-slack` ran the second half on a command
that had produced no signals, and the same pipeline written with `--format
json` stopped. The exit code is the one part of a CLI's answer that does not
change with the format it was asked in.

Only the codes are compared here. Whether the reason reached the right stream
is the other two modules' question, already asked.
"""

from __future__ import annotations

import pytest
from click.testing import CliRunner

from fpl_cli.cli import main

# The same walk both other contracts drive, down the same outage: one list of
# commands, one `offline` fixture, three questions asked of them.
from tests.test_cli_json_contract import JSON_COMMANDS, REQUIRED_PARAMS


def _args_for(command: tuple[str, ...]) -> list[str]:
    return list(command) + REQUIRED_PARAMS.get(" ".join(command), [])


@pytest.mark.parametrize(
    "command", JSON_COMMANDS, ids=[" ".join(c) or "(root)" for c in JSON_COMMANDS],
)
def test_the_same_failure_exits_the_same_way_in_both_formats(command, offline):
    """One command, one unreachable upstream, two formats, one exit code.

    Both directions fail here, not just the one that bit: a table path that
    exits 0 where the envelope exits 1 hides a failure from `&&`, and a table
    path that exits 1 where the envelope exits 0 invents one. Equality is the
    whole assertion -- a command that legitimately exits 0 on both (nothing
    configured is not always a failure) is as passing as one that exits 1.
    """
    args = _args_for(command)
    table = CliRunner().invoke(main, args)
    envelope = CliRunner().invoke(main, args + ["--format", "json"])

    assert table.exit_code == envelope.exit_code, (
        f"`fpl {' '.join(args)}` exits {table.exit_code} in table mode and "
        f"{envelope.exit_code} under `--format json` on the same failure. "
        f"table stderr: {table.stderr[:160]!r}"
    )
