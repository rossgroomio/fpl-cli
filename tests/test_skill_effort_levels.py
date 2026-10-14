"""The product skills pin an effort level next to the model they already pin.

Each product skill's ``SKILL.md`` sets ``model`` in its frontmatter, and gw-prep
and squad-builder set one per sub-agent, so a run does not depend on whichever
model the session happened to be on. Effort was the missing half: nothing set
it, so it followed the session (issue #393). The levels are a judgement call --
classic analysis weighs transfers, hits, chips and captaincy against each other,
draft analysis and preview extraction do not -- and that judgement is written
down once, in the Model and effort table of ``.agents/README.md``.

This module holds the copies together: every product skill's frontmatter
carries a ``model`` and an ``effort`` from the allowed sets, every sub-agent
parameter block carries both too, and the README table names exactly the pairs
the skills use, so a level changed in one place and not the other fails here
rather than showing up as a quietly different gameweek run.
"""

import re
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).parent.parent
SKILLS_DIR = REPO_ROOT / ".agents" / "skills"
README = REPO_ROOT / ".agents" / "README.md"

# The user-facing skills (CLAUDE.md, Commits & Changelog): the ones a session
# runs against live data, where a stray session setting changes the output.
PRODUCT_SKILLS = ("gw-prep", "squad-builder", "update-gw-prep", "preview-ingest")

# How many sub-agent parameter blocks each skill dispatches. A parser that
# silently matched fewer would wave a missing effort line through.
SUBAGENT_BLOCKS = {"gw-prep": 2, "squad-builder": 2, "update-gw-prep": 1, "preview-ingest": 1}

# Per the Claude Code skills and sub-agents docs.
EFFORT_LEVELS = frozenset({"low", "medium", "high", "xhigh", "max"})
MODEL_ALIASES = frozenset({"haiku", "sonnet", "opus", "fable", "inherit"})

# gw-prep writes its Phase C parameters as a run of bullets ...
BULLET_PARAM = re.compile(r"^- \*\*(model|effort|subagent_type)\*\*: (\S+)$", re.MULTILINE)
BULLET_GROUP = re.compile(r"(?:^- \*\*(?:model|effort|subagent_type)\*\*: \S+\n)+", re.MULTILINE)
# ... the other three as a fenced `Agent tool parameters` block.
FENCED_BLOCK = re.compile(r"^```\n(Agent tool parameters[^\n]*\n(?:.*\n)*?)```$", re.MULTILINE)
FENCED_PARAM = re.compile(r"^\s+(model|effort|subagent_type|description): (.+)$", re.MULTILINE)

# `| gw-prep C1 classic analysis | opus | high | ... |` -- the first word of the
# Where cell is the skill, the next two cells are the pair.
README_ROW = re.compile(r"^\| ([^|]+?) \| (\w+) \| (\w+) \|", re.MULTILINE)


def _skill_text(skill: str) -> str:
    return (SKILLS_DIR / skill / "SKILL.md").read_text(encoding="utf-8")


def _frontmatter(text: str) -> dict:
    assert text.startswith("---\n"), "SKILL.md must open with a frontmatter block"
    end = text.index("\n---\n", 4)
    return yaml.safe_load(text[4:end])


def _subagent_blocks(text: str) -> list[dict[str, str]]:
    blocks = [dict(BULLET_PARAM.findall(m.group(0))) for m in BULLET_GROUP.finditer(text)]
    blocks += [dict(FENCED_PARAM.findall(m.group(1))) for m in FENCED_BLOCK.finditer(text)]
    return blocks


def _skill_pairs(skill: str) -> set[tuple[str, str]]:
    """Every (model, effort) pair a skill pins: its frontmatter plus each sub-agent block."""
    text = _skill_text(skill)
    fm = _frontmatter(text)
    pairs = {(fm.get("model"), fm.get("effort"))}
    pairs |= {(b.get("model"), b.get("effort")) for b in _subagent_blocks(text)}
    return pairs


def _readme_pairs() -> dict[str, set[tuple[str, str]]]:
    pairs: dict[str, set[tuple[str, str]]] = {skill: set() for skill in PRODUCT_SKILLS}
    for where, model, effort in README_ROW.findall(README.read_text(encoding="utf-8")):
        skill = where.split()[0]
        if skill in pairs and model in MODEL_ALIASES:
            pairs[skill].add((model, effort))
    return pairs


def test_product_skills_were_found():
    # Guard the guard: a renamed skill would silently check nothing.
    for skill in PRODUCT_SKILLS:
        assert (SKILLS_DIR / skill / "SKILL.md").is_file(), skill


def test_the_parsers_read_both_block_spellings():
    bullets = "### C1\n\n- **model**: opus\n- **effort**: high\n- **subagent_type**: general-purpose\n\nBranch on x.\n"
    fenced = '```\nAgent tool parameters (per agent):\n  subagent_type: general-purpose\n  model: opus\n  effort: medium\n  description: "x"\n```\n'
    other_fence = "```bash\nfpl status --format json\n```\n"
    blocks = _subagent_blocks(bullets + other_fence + fenced)
    assert blocks == [
        {"model": "opus", "effort": "high", "subagent_type": "general-purpose"},
        {"subagent_type": "general-purpose", "model": "opus", "effort": "medium", "description": '"x"'},
    ]


@pytest.mark.parametrize("skill", PRODUCT_SKILLS)
def test_frontmatter_pins_model_and_effort(skill: str):
    fm = _frontmatter(_skill_text(skill))
    assert fm.get("model") in MODEL_ALIASES, f"{skill}: frontmatter model is {fm.get('model')!r}"
    assert fm.get("effort") in EFFORT_LEVELS, (
        f"{skill}: frontmatter effort is {fm.get('effort')!r}; set one of {sorted(EFFORT_LEVELS)}"
        " next to `model:` so the run does not follow the session's effort (#393)"
    )


@pytest.mark.parametrize("skill", PRODUCT_SKILLS)
def test_every_subagent_block_pins_model_and_effort(skill: str):
    blocks = _subagent_blocks(_skill_text(skill))
    assert len(blocks) == SUBAGENT_BLOCKS[skill], (
        f"{skill}: found {len(blocks)} sub-agent parameter blocks, expected {SUBAGENT_BLOCKS[skill]};"
        " update SUBAGENT_BLOCKS if a dispatch was added or removed"
    )
    for block in blocks:
        assert block.get("subagent_type"), f"{skill}: block without subagent_type: {block}"
        assert block.get("model") in MODEL_ALIASES, f"{skill}: block model is {block.get('model')!r}: {block}"
        assert block.get("effort") in EFFORT_LEVELS, (
            f"{skill}: sub-agent block pins model {block.get('model')!r} but effort {block.get('effort')!r};"
            " every Agent tool block carries both (#393)"
        )


@pytest.mark.parametrize("skill", PRODUCT_SKILLS)
def test_readme_table_matches_the_skill(skill: str):
    expected = _skill_pairs(skill)
    documented = _readme_pairs()[skill]
    assert documented == expected, (
        f"{skill}: .agents/README.md's Model and effort table says {sorted(documented)} but the skill pins"
        f" {sorted(expected)}; change the level in both places so the reasoning stays next to the setting"
    )
