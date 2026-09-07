"""The FPL Mate output style's command index, held to the CLI it indexes (#134).

`.claude/output-styles/fpl-mate.md` carries the Data Grounding section: the
fast-path list telling FPL Mate which command to reach for. Nothing breaks
when it drifts, which is the problem. Every command stays discoverable through
`--help` and `.agents/TOOLS.md`, so a missing entry costs nothing visible -- it
just means the persona keeps reaching for the commands that existed when the
file was written and silently under-uses the newer analysis surfaces. That is
how `doctor`, `history`, `returnees`, `intel`, `preview` and the league-history
commands all landed without ever reaching the index.

So the README/TOOLS.md/architecture.md "must stay in sync" convention gets
enforced here rather than remembered: every registered command is named in the
style, and every command the style names still exists. Presence, not accuracy
-- prose can't be asserted, but a PR that adds a command and forgets this file
can be.
"""

import re
from pathlib import Path

import click
import pytest

from fpl_cli.cli import main

REPO_ROOT = Path(__file__).parent.parent
OUTPUT_STYLE = REPO_ROOT / ".claude" / "output-styles" / "fpl-mate.md"

# Account admin, not analysis. Clearing a stored FPL password is something the
# user does at a terminal once; there is no conversation in which the persona
# should be reaching for it, so it is the one command with no place in the
# index. `credentials set` earns its mention -- `squad sell-prices --refresh`
# cannot scrape without it.
UNINDEXED = frozenset({"credentials clear"})

# A backticked invocation, e.g. `fpl squad grid -n 6` -> "squad grid -n 6".
_INVOCATION = re.compile(r"`fpl ([^`]+)`")


def _command_paths(group, prefix=""):
    """Every command path the CLI registers, groups and subcommands alike.

    Walks `.commands` rather than `list_commands()` on purpose: the group
    filters the custom-analysis set out of the latter, and those seven
    commands are exactly the ones the style most needs to describe.
    """
    for name, command in group.commands.items():
        path = f"{prefix}{name}"
        yield path
        if isinstance(command, click.Group):
            yield from _command_paths(command, prefix=f"{path} ")


def _mentions(path, text):
    """Whether the style names `fpl <path>` as a command rather than a prefix.

    The trailing boundary is what stops `fpl league-recap` from answering for
    `fpl league`. A group is allowed to be named through one of its
    subcommands (`fpl credentials set` covers `credentials`): a reader who has
    the subcommand has the group.
    """
    return re.search(r"`fpl " + re.escape(path) + r"(?=[ `])", text) is not None


@pytest.fixture(scope="module")
def style_text():
    return OUTPUT_STYLE.read_text(encoding="utf-8")


@pytest.mark.parametrize("path", sorted(set(_command_paths(main)) - UNINDEXED))
def test_every_command_is_indexed(path, style_text):
    assert _mentions(path, style_text), (
        f"`fpl {path}` is registered on the CLI but never named in "
        f"{OUTPUT_STYLE.relative_to(REPO_ROOT)}. Add it to the Data Grounding "
        "section so FPL Mate reaches for it, or add it to UNINDEXED here with "
        "the reason it has no place in the index."
    )


def test_style_names_only_commands_that_exist(style_text):
    """The reverse direction: a renamed or removed command leaves a dead entry.

    Worse than a missing one -- the persona is told the command exists, calls
    it, and gets "No such command" from a file that claims to describe the CLI
    as shipped.
    """
    known = set(_command_paths(main))
    top_level = {path for path in known if " " not in path}

    unknown = []
    for span in _INVOCATION.findall(style_text):
        tokens = span.split()
        if not tokens or tokens[0].startswith("-"):
            continue  # `fpl --version`, not a command
        if tokens[0] not in top_level:
            unknown.append(span)
            continue
        command = main.commands[tokens[0]]
        subcommand_given = len(tokens) > 1 and not tokens[1].startswith("-")
        if isinstance(command, click.Group) and subcommand_given:
            if f"{tokens[0]} {tokens[1]}" not in known:
                unknown.append(span)

    assert not unknown, (
        f"{OUTPUT_STYLE.relative_to(REPO_ROOT)} names commands the CLI does "
        f"not register: {unknown}"
    )
