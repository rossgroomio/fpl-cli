"""The formation lists agents read, held to `VALID_FORMATIONS` (#353).

Three guidance files spell the valid formations out by hand: the gw-prep and
squad-builder rules tables and the FPL Mate output style. The constant is what
the ILP solver, `select_starting_xi` and `best_xi_selection` iterate, so it
moves on its own; the prose does not. #352 added 5-2-3 and the suite stayed
green with all three still naming seven shapes -- and it is the prose, not the
constant, that an assistant reasons from when it decides whether a 5-2-3 is
a legal XI. Ruling a shape out reads as ordinary analysis, so nothing flags it.

So the hand-written copies are checked here rather than remembered: every
enumeration in each file must match the constant exactly, shape for shape and
in the constant's order. A ninth formation, or a dropped one, fails this test
instead of going quietly stale.
"""

import re
from pathlib import Path

import pytest

from fpl_cli.services.scoring import VALID_FORMATIONS

REPO_ROOT = Path(__file__).parent.parent

GUIDANCE_FILES = [
    ".agents/skills/gw-prep/references/rules.md",
    ".agents/skills/squad-builder/references/rules.md",
    ".claude/output-styles/fpl-mate.md",
]

# Three or more comma-separated D-M-F shapes in a row: an enumeration of the
# valid set rather than a lone mention like "a 3-4-3 with one bench DEF".
_ENUMERATION = re.compile(r"\d-\d-\d(?:, \d-\d-\d){2,}")


def _expected() -> list[str]:
    return [f"{d}-{m}-{f}" for d, m, f in VALID_FORMATIONS]


@pytest.mark.parametrize("relative_path", GUIDANCE_FILES)
def test_guidance_lists_every_valid_formation(relative_path):
    text = (REPO_ROOT / relative_path).read_text(encoding="utf-8")

    listings = [match.split(", ") for match in _ENUMERATION.findall(text)]

    assert listings, (
        f"{relative_path} no longer enumerates the valid formations. If the "
        "list was removed on purpose, drop the file from GUIDANCE_FILES here."
    )
    for listing in listings:
        assert listing == _expected(), (
            f"{relative_path} lists {', '.join(listing)} but VALID_FORMATIONS "
            f"(fpl_cli/services/scoring/constants.py) is {', '.join(_expected())}. "
            "Update the prose to match, in the constant's order."
        )
