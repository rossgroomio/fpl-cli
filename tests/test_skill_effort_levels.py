"""The product skills pin an effort level next to the model they already pin.

Each product skill's ``SKILL.md`` sets ``model`` in its frontmatter, and gw-prep
and squad-builder set one per sub-agent, so a run does not depend on whichever
model the session happened to be on. Effort was the missing half: nothing set
it, so it followed the session (issue #393). The levels are a judgement call --
classic analysis weighs transfers, hits, chips and captaincy against each other,
draft analysis and preview extraction do not -- and that judgement is written
down once, in the Model and effort table of ``.agents/README.md``; the skills'
own prose gives the reason and points there rather than restating a level.

This module holds the copies together: every product skill's frontmatter
carries a ``model`` and an ``effort`` from the allowed sets, every
``Agent tool parameters`` block carries both too, and each row of the README
table is held to the one block its Where cell names, so a level changed in one
place and not the other, a swapped pair, or a row dropped or misspelt fails
here rather than showing up as a quietly different gameweek run.
"""

import re
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).parent.parent
SKILLS_DIR = REPO_ROOT / ".agents" / "skills"
README = REPO_ROOT / ".agents" / "README.md"

# Each product skill's pins in document order: the frontmatter (labelled
# `orchestrator`), then every `Agent tool parameters` block as it appears. The
# README table's Where cell is `<skill> <label>`, or `<skill> <label> and
# <label>` where two blocks share a row, so a row is held to the one block it
# names rather than to the skill's set of distinct pairs.
SKILL_BLOCKS: dict[str, tuple[str, ...]] = {
    "gw-prep": ("orchestrator", "C1 classic analysis", "C2 draft analysis"),
    "squad-builder": ("orchestrator", "Phase C position research", "Phase D assembly"),
    "update-gw-prep": ("orchestrator", "sub-agent"),
    "preview-ingest": ("orchestrator", "per-team agents"),
}
PRODUCT_SKILLS = tuple(SKILL_BLOCKS)

# Per the Claude Code skills and sub-agents docs.
EFFORT_LEVELS = frozenset({"low", "medium", "high", "xhigh", "max"})
MODEL_ALIASES = frozenset({"haiku", "sonnet", "opus", "fable", "inherit"})

# A fenced block opening with `Agent tool parameters`, as every skill writes
# its sub-agent dispatch. Any indented `key: value` line inside counts, so an
# extra key (`run_in_background: true`) neither splits the block nor hides the
# effort line behind it.
FENCED_BLOCK = re.compile(r"^```\n(Agent tool parameters[^\n]*\n(?:.*\n)*?)```$", re.MULTILINE)
FENCED_PARAM = re.compile(r"^\s+(\w+): (.+?)\s*$", re.MULTILINE)

Pin = tuple[str | None, str | None]


def _skill_text(skill: str) -> str:
    return (SKILLS_DIR / skill / "SKILL.md").read_text(encoding="utf-8")


def _frontmatter(text: str) -> dict:
    assert text.startswith("---\n"), "SKILL.md must open with a frontmatter block"
    end = text.index("\n---\n", 4)
    return yaml.safe_load(text[4:end])


def _subagent_blocks(text: str) -> list[dict[str, str]]:
    return [dict(FENCED_PARAM.findall(m.group(1))) for m in FENCED_BLOCK.finditer(text)]


def _skill_pins(skill: str) -> dict[str, Pin]:
    """Each labelled block's (model, effort): the frontmatter, then the sub-agent blocks in order."""
    text = _skill_text(skill)
    labels = SKILL_BLOCKS[skill]
    blocks = _subagent_blocks(text)
    assert len(blocks) == len(labels) - 1, (
        f"{skill}: found {len(blocks)} `Agent tool parameters` blocks but SKILL_BLOCKS labels {len(labels) - 1};"
        " update SKILL_BLOCKS and the README table together if a dispatch was added or removed"
    )
    fm = _frontmatter(text)
    pins: dict[str, Pin] = {labels[0]: (fm.get("model"), fm.get("effort"))}
    for label, block in zip(labels[1:], blocks, strict=True):
        pins[label] = (block.get("model"), block.get("effort"))
    return pins


def _parse_readme_table(text: str) -> dict[str, dict[str, Pin]]:
    """Rows of the Model and effort table, keyed by skill and then by block label.

    Fails on a row it cannot place -- an unknown skill or label, a label named
    twice, a model or effort outside the allowed sets -- rather than dropping
    it, so a misspelt cell is a failure and not a silently shorter table.
    """
    section = text.split("## Model and effort\n", 1)[1].split("\n## ", 1)[0]
    rows = [line for line in section.splitlines() if line.startswith("|")]
    assert rows and rows[0].startswith("| Where |"), "Model and effort table not found under its heading"
    out: dict[str, dict[str, Pin]] = {skill: {} for skill in SKILL_BLOCKS}
    for row in rows[2:]:  # past the header and the separator
        cells = [cell.strip() for cell in row.strip().strip("|").split("|")]
        where, model, effort = cells[:3]
        skill, _, labels = where.partition(" ")
        assert skill in SKILL_BLOCKS, f"README row {where!r} names no product skill"
        assert model in MODEL_ALIASES, f"README row {where!r}: model {model!r} is not one of {sorted(MODEL_ALIASES)}"
        assert effort in EFFORT_LEVELS, f"README row {where!r}: effort {effort!r} is not one of {sorted(EFFORT_LEVELS)}"
        for label in labels.split(" and "):
            assert label in SKILL_BLOCKS[skill], f"README row {where!r}: {label!r} is not a block of {skill}"
            assert label not in out[skill], f"README names {skill} {label!r} twice"
            out[skill][label] = (model, effort)
    return out


def test_product_skills_were_found():
    # Guard the guard: a renamed skill would silently check nothing.
    for skill in PRODUCT_SKILLS:
        assert (SKILLS_DIR / skill / "SKILL.md").is_file(), skill


def test_the_block_parser_reads_only_agent_tool_fences():
    doc = (
        "```bash\nfpl status --format json\n```\n"
        "```\nAgent tool parameters (per agent):\n  subagent_type: general-purpose\n  model: opus\n"
        '  effort: medium\n  run_in_background: true\n  description: "x"\n```\n'
    )
    assert _subagent_blocks(doc) == [
        {
            "subagent_type": "general-purpose",
            "model": "opus",
            "effort": "medium",
            "run_in_background": "true",
            "description": '"x"',
        }
    ]


def test_the_table_parser_fails_on_a_row_it_cannot_place():
    header = "## Model and effort\n\n| Where | Model | Effort | Why |\n|---|---|---|---|\n"
    good = header + "| gw-prep orchestrator and C1 classic analysis | opus | high | because |\n\n## Next\n"
    assert _parse_readme_table(good)["gw-prep"] == {
        "orchestrator": ("opus", "high"),
        "C1 classic analysis": ("opus", "high"),
    }
    for bad_row in (
        "| gw-prep orchestrator | Opus | high | capitalised model |",
        "| gw-prep orchestrator | opus | highest | not a level |",
        "| gw-prep C3 | opus | high | no such block |",
        "| gw-prep orchestrator | opus | high | twice |\n| gw-prep orchestrator | opus | high | twice |",
    ):
        with pytest.raises(AssertionError):
            _parse_readme_table(header + bad_row + "\n\n## Next\n")


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
    _skill_pins(skill)  # asserts the block count against SKILL_BLOCKS
    for label, block in zip(SKILL_BLOCKS[skill][1:], _subagent_blocks(_skill_text(skill)), strict=True):
        assert block.get("subagent_type"), f"{skill} {label}: block without subagent_type: {block}"
        assert block.get("model") in MODEL_ALIASES, f"{skill} {label}: block model is {block.get('model')!r}"
        assert block.get("effort") in EFFORT_LEVELS, (
            f"{skill} {label}: block pins model {block.get('model')!r} but effort {block.get('effort')!r};"
            " every Agent tool block carries both (#393)"
        )


@pytest.mark.parametrize("skill", PRODUCT_SKILLS)
def test_each_readme_row_matches_the_block_it_names(skill: str):
    documented = _parse_readme_table(README.read_text(encoding="utf-8"))[skill]
    pinned = _skill_pins(skill)
    assert documented == pinned, (
        f"{skill}: .agents/README.md's Model and effort table says {documented} but the skill pins {pinned};"
        " change the level in the block and in its row together so the reasoning stays next to the setting"
    )
