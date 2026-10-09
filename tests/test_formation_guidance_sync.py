"""The formation lists agents read, held to `VALID_FORMATIONS` (#353).

Three guidance files spell the valid formations out by hand: the gw-prep and
squad-builder rules tables and the FPL Mate output style. The constant is what
the ILP solver, `select_starting_xi` and `best_xi_selection` iterate, so it
moves on its own; the prose does not. #352 added 5-2-3 and the suite stayed
green with all three still naming seven shapes -- and it is the prose, not the
constant, that an assistant reasons from when it decides whether a 5-2-3 is
a legal XI. Ruling a shape out reads as ordinary analysis, so nothing flags it.

So the hand-written copies are checked here rather than remembered. Every
markdown file is swept, not a fixed few, so a list added to a new skill
reference is held to the constant the day it lands. Detection is by block
rather than by separator: a paragraph or table that names at least half the
valid shapes is an enumeration of them, however it wraps lines or joins them.
The set is what matters to a reader, so order is not -- the constant's own
order is the solver's tiebreak and may change for reasons that have nothing to
do with the prose. (`VALID_FORMATIONS` against `FORMATION_LIMITS` is pinned in
`tests/test_scoring.py`, which is what stops the constant itself going stale.)
"""

import re
from pathlib import Path

import pytest

from fpl_cli.services.scoring import VALID_FORMATIONS

REPO_ROOT = Path(__file__).parent.parent

# Where a hand-written list could live: the agent skills and their references,
# the output style, the docs, and the root-level READMEs and project
# instructions. CHANGELOG.md is history -- it is allowed to name a seven-shape
# world.
SCANNED_DOCS = sorted(
    [
        *REPO_ROOT.glob(".agents/**/*.md"),
        *REPO_ROOT.glob(".claude/output-styles/*.md"),
        *REPO_ROOT.glob("docs/**/*.md"),
        *(p for p in REPO_ROOT.glob("*.md") if p.name != "CHANGELOG.md"),
    ]
)

# The files known to carry a list today. If one stops enumerating -- the list
# was reworded past the detector, or removed on purpose -- the guard below
# fails rather than letting the sweep quietly check nothing.
KNOWN_LISTINGS = [
    ".agents/skills/gw-prep/references/rules.md",
    ".agents/skills/squad-builder/references/rules.md",
    ".claude/output-styles/fpl-mate.md",
]

# A D-M-F shape that is not part of a longer digit-hyphen run, so a date such
# as 2026-10-09 never reads as one.
_SHAPE = re.compile(r"(?<![\d-])\d-\d-\d(?![\d-])")
_BLOCK_BREAK = re.compile(r"\n\s*\n")


def _expected() -> set[str]:
    return {f"{d}-{m}-{f}" for d, m, f in VALID_FORMATIONS}


def _enumerations(text: str) -> list[set[str]]:
    """Each block of `text` that lists the valid formations, as its set of shapes.

    A block naming at least half the valid shapes is an enumeration. Below
    that it is a passing mention ("a 3-4-3 with one bench DEF") and is left
    alone; a list that has itself shrunk below the threshold is no longer
    guidance an agent could mistake for the full set.
    """
    expected = _expected()
    found = []
    for block in _BLOCK_BREAK.split(text):
        shapes = set(_SHAPE.findall(block))
        if len(shapes) * 2 >= len(expected):
            found.append(shapes)
    return found


def _relative(path: Path) -> str:
    return str(path.relative_to(REPO_ROOT))


def test_scanned_docs_were_found():
    # Guard the guard: a stale glob would sweep nothing and pass.
    scanned = {_relative(p) for p in SCANNED_DOCS}
    assert set(KNOWN_LISTINGS) <= scanned


@pytest.mark.parametrize("relative_path", KNOWN_LISTINGS)
def test_known_listing_is_still_detected(relative_path):
    text = (REPO_ROOT / relative_path).read_text(encoding="utf-8")

    assert _enumerations(text), (
        f"{relative_path} no longer enumerates the valid formations in a form "
        "this check recognises. If the list was removed on purpose, drop the "
        "file from KNOWN_LISTINGS here."
    )


def test_every_listing_matches_valid_formations():
    expected = _expected()
    stale = []
    for path in SCANNED_DOCS:
        for shapes in _enumerations(path.read_text(encoding="utf-8")):
            if shapes != expected:
                missing = sorted(expected - shapes)
                extra = sorted(shapes - expected)
                stale.append(f"{_relative(path)}: missing {missing}, unknown {extra}")

    assert not stale, (
        "Hand-written formation lists disagree with VALID_FORMATIONS "
        "(fpl_cli/services/scoring/constants.py): " + "; ".join(stale)
    )
