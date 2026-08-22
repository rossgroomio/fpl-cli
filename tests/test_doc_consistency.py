"""The decay schedule and coverage threshold must not drift into prose.

`SECTION_DECAY` and `COVERAGE_THRESHOLD` live in
``fpl_cli/services/season_previews.py`` and are emitted live in every
``fpl intel --format json`` payload (``metadata.decay_schedule``,
``metadata.coverage``). One prose copy is allowed -- the tables in
``docs/command-reference.md`` -- and this module checks that copy against the
constants. Every other document, the agent skills especially, must reference
the payload fields instead of restating the numbers: a gameweek hardcoded in a
skill survives a retuning of the constant and then quietly instructs an agent
to discard live intel (or trust expired intel) at the wrong gameweek.
"""

import re
from pathlib import Path

import pytest

from fpl_cli.services.season_previews import COVERAGE_THRESHOLD, SECTION_DECAY

REPO_ROOT = Path(__file__).parent.parent
COMMAND_REFERENCE = REPO_ROOT / "docs" / "command-reference.md"

# The documents that must NOT restate the decay schedule or the threshold.
PROSE_DOCS = sorted(
    [
        *REPO_ROOT.glob(".agents/skills/*/SKILL.md"),
        *REPO_ROOT.glob(".agents/skills/*/references/*.md"),
        REPO_ROOT / ".agents" / "TOOLS.md",
        REPO_ROOT / "README.md",
    ]
)

# Phrasings that pin a decay gameweek or the coverage threshold in prose. Not
# exhaustive -- a tripwire, not a proof -- but each pattern has caught a real
# restatement in this repo's history.
FORBIDDEN_PATTERNS = [
    r"expire[sd]?\s+(?:at|by)\s+GW\d+",
    r"gone\s+by\s+GW\d+",
    r"(?:near-)?empty\s+past\s+GW\d+",
    r"GW\d+[^\n]{0,40}everything\s+expires",
    r"(?:injuries|XIs?|intel|narrative|transfers)\s+at\s+GW\d+",
    r"\d+%\+?\s+of\s+(?:the\s+)?teams",
    r"≥\s*\d+%",
]


class TestCommandReferenceDecayTable:
    """The one allowed prose copy must match the constants exactly."""

    def _table_rows(self) -> dict[str, tuple[int, int]]:
        text = COMMAND_REFERENCE.read_text(encoding="utf-8")
        rows = re.findall(r"^\| `(\w+)` \| GW(\d+) \| GW(\d+) \|", text, flags=re.MULTILINE)
        return {section: (int(full), int(expires)) for section, full, expires in rows}

    def test_every_section_is_documented(self):
        assert set(self._table_rows()) == set(SECTION_DECAY)

    def test_documented_gameweeks_match_the_code(self):
        assert self._table_rows() == SECTION_DECAY

    def test_documented_threshold_matches_the_code(self):
        text = COMMAND_REFERENCE.read_text(encoding="utf-8")
        match = re.search(r"\| `full` \| (\d+)%\+? of teams covered", text)
        assert match is not None, "coverage gate table row for `full` not found"
        assert int(match.group(1)) == int(COVERAGE_THRESHOLD * 100)


class TestNoRestatedPolicyInProse:
    """Skills and README must read the schedule from the payload, not restate it."""

    def test_prose_docs_were_found(self):
        # Guard the guard: if the glob goes stale the scan silently checks nothing.
        assert any(p.name == "SKILL.md" for p in PROSE_DOCS)
        assert any(p.name == "README.md" for p in PROSE_DOCS)

    @pytest.mark.parametrize("doc", PROSE_DOCS, ids=lambda p: str(p.relative_to(REPO_ROOT)))
    def test_no_hardcoded_decay_or_threshold(self, doc: Path):
        text = doc.read_text(encoding="utf-8")
        offences = [
            f"line {text[: m.start()].count(chr(10)) + 1}: {m.group(0)!r} (pattern {pattern!r})"
            for pattern in FORBIDDEN_PATTERNS
            for m in re.finditer(pattern, text, flags=re.IGNORECASE)
        ]
        assert not offences, (
            f"{doc.relative_to(REPO_ROOT)} restates the decay schedule or coverage"
            f" threshold; reference metadata.decay_schedule / metadata.sections_live /"
            f" metadata.coverage from the fpl intel payload instead:\n" + "\n".join(offences)
        )
