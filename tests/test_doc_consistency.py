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


# A bash-fenced code block, captured so the check below only looks at actual
# invocations -- not at prose that mentions `python3 scripts/foo.py` as a
# worked example of the failure mode it's warning about (see gw-prep/SKILL.md's
# `[YOUR_PYTHON]` explainer).
BASH_BLOCK = re.compile(r"```bash\n(.*?)```", re.DOTALL)
# The quoted script-path argument itself, e.g. "...scripts/normalise_entities.py".
# Matched separately from the interpreter token so a $FPL_CLI_DIR-built *interpreter*
# path (gw-prep/SKILL.md's own suggested `[YOUR_PYTHON]` substitution, e.g.
# `$FPL_CLI_DIR/.venv/bin/python`) isn't confused with a $FPL_CLI_DIR-built *script*
# path (the actual #221 bug).
SCRIPT_PATH_ARG = re.compile(r'"([^"]*scripts/[\w-]+\.py)"')
# A bare `python3` interpreter, anchored to the start of the line so a resolved,
# fully-qualified interpreter path like `/opt/venv/bin/python3` -- a legitimate
# `[YOUR_PYTHON]` substitution -- doesn't match on the "python3" substring alone.
BARE_PYTHON3_INTERPRETER = re.compile(r"^\s*python3\b")


def _script_invocation_lines(doc: Path) -> list[tuple[int, str]]:
    """(line number, line text) for every bash-fenced line invoking a scripts/*.py file."""
    text = doc.read_text(encoding="utf-8")
    lines = []
    for block in BASH_BLOCK.finditer(text):
        block_start_line = text[: block.start()].count("\n") + 1
        for offset, line in enumerate(block.group(1).splitlines()):
            if SCRIPT_PATH_ARG.search(line):
                lines.append((block_start_line + 1 + offset, line))
    return lines


class TestScriptInvocationConvention:
    """scripts/*.py invocations must use `[YOUR_PYTHON]`, never a bare `python3`,
    and must never build the script's own path from `$FPL_CLI_DIR`.

    #196 introduced `[YOUR_PYTHON]` (plus `${CLAUDE_SKILL_DIR}` / a resolved
    skills-dir placeholder) because a standalone `fpl` install (uv tool, pipx)
    puts the *command* on `PATH` but not `fpl_cli` on the system interpreter's
    import path, and leaves `$FPL_CLI_DIR` unset -- both forms fail for that
    install shape. #221 found three call sites the original sweep missed.
    """

    def test_the_scan_actually_finds_script_invocations(self):
        # Guard the guard: if the ```bash fence syntax changes (```sh, ```shell)
        # or every invocation moves to a doc outside PROSE_DOCS, the check below
        # passes vacuously on every doc and a bare `python3` could walk back in
        # unnoticed. Assert the corpus isn't empty so that failure is loud.
        total = sum(len(_script_invocation_lines(doc)) for doc in PROSE_DOCS)
        assert total > 0, (
            "no scripts/*.py invocation found inside a ```bash fence across any"
            " PROSE_DOCS doc -- either the fence syntax changed, the invocations"
            " moved to a doc outside PROSE_DOCS, or SCRIPT_PATH_ARG no longer"
            " matches; test_script_invocations_use_your_python_convention below"
            " is scanning nothing"
        )

    @pytest.mark.parametrize("doc", PROSE_DOCS, ids=lambda p: str(p.relative_to(REPO_ROOT)))
    def test_script_invocations_use_your_python_convention(self, doc: Path):
        offences = []
        for line_no, line in _script_invocation_lines(doc):
            path_match = SCRIPT_PATH_ARG.search(line)
            assert path_match is not None
            if path_match.group(1).startswith("$FPL_CLI_DIR"):
                offences.append(f"line {line_no}: script path built from $FPL_CLI_DIR: {line.strip()!r}")
            if BARE_PYTHON3_INTERPRETER.match(line):
                offences.append(f"line {line_no}: bare python3 interpreter: {line.strip()!r}")
        assert not offences, (
            f"{doc.relative_to(REPO_ROOT)} invokes a scripts/*.py script with a bare"
            f" `python3` interpreter or a `$FPL_CLI_DIR`-built script path; use"
            f" `[YOUR_PYTHON]` (see gw-prep/SKILL.md's Environment section) together"
            f" with `${{CLAUDE_SKILL_DIR}}` (within gw-prep) or `[YOUR_SKILLS_DIR]`"
            f" (cross-skill) instead:\n" + "\n".join(offences)
        )
