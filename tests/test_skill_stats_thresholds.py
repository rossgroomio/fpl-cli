"""Skill `fpl stats` queries must not hardcode a minutes floor.

`--min-minutes` is a filter, not a quality bar: after ``N-1`` completed
gameweeks no player can have logged more than ``(N - 1) * 90`` minutes, so a
literal floor is arithmetically unreachable for the opening weeks of every
season. `fpl stats` then returns a well-formed envelope carrying ``"data": []``
-- no error, no warning -- which a sub-agent reads as "nobody qualifies".

The skills therefore derive the floor from the gameweek (``min(cap, (N - 1) *
45)``) and pass it as a placeholder. This module is the tripwire: a literal
creeping back in is silent at review time and silent at runtime, and only shows
up as four empty data blocks in an August gameweek-prep run.
"""

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parent.parent

SKILL_DOCS = sorted(
    [
        *REPO_ROOT.glob(".agents/skills/*/SKILL.md"),
        *REPO_ROOT.glob(".agents/skills/*/references/*.md"),
    ]
)

# `--min-minutes 450`, but not `--min-minutes {mins_pos}`.
LITERAL_MIN_MINUTES = re.compile(r"--min-minutes\s+(\d+)")

# The placeholder form the skills are expected to use instead.
PLACEHOLDER_MIN_MINUTES = re.compile(r"--min-minutes\s+\{[a-z_]+\}")


def test_skill_docs_were_found():
    # Guard the guard: a stale glob would silently check nothing.
    assert any(p.name == "SKILL.md" for p in SKILL_DOCS)


@pytest.mark.parametrize("doc", SKILL_DOCS, ids=lambda p: str(p.relative_to(REPO_ROOT)))
def test_no_hardcoded_minutes_floor(doc: Path):
    text = doc.read_text(encoding="utf-8")
    offences = [
        f"line {text[: m.start()].count(chr(10)) + 1}: {m.group(0)!r}"
        for m in LITERAL_MIN_MINUTES.finditer(text)
    ]
    assert not offences, (
        f"{doc.relative_to(REPO_ROOT)} passes a fixed --min-minutes to `fpl stats`."
        " A literal floor is unreachable early in a season and empties the result"
        " silently; derive it from the gameweek instead, e.g."
        " `min(450, (N - 1) * 45)`, and pass it as a placeholder:\n" + "\n".join(offences)
    )


def test_the_skills_that_filter_on_minutes_use_the_placeholder():
    # The rule above is only meaningful while some skill still filters on
    # minutes -- if every call dropped the flag, the tripwire would pass vacuously.
    users = [d for d in SKILL_DOCS if PLACEHOLDER_MIN_MINUTES.search(d.read_text(encoding="utf-8"))]
    assert {d.parent.name for d in users} == {"gw-prep", "squad-builder"}
