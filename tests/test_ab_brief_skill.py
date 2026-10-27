"""The A/B handback contract between the ab-brief skill and the instructions every session loads.

An A/B of a league-recap prompt runs in a separate fpl-workspace session, which
posts its results as one comment on the fpl-cli PR. Both sessions post as the
same GitHub identity, and a session watching a PR is told to skip echoes of its
own comments, so the fpl-cli session recognises the handback by its marker
alone. The marker is written in three places: the skill that defines it, the
brief template that tells the vault session to write it, and CLAUDE.md /
AGENTS.md, which tell every fpl-cli session (including one whose checkout has
no copy of the skill yet loaded) that a comment carrying it is a result to act
on. If they drift apart, the handback lands and nobody acts on it.
"""

import re
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).parent.parent
SKILLS_DIR = REPO_ROOT / ".agents" / "skills"
SKILL = SKILLS_DIR / "ab-brief" / "SKILL.md"
TEMPLATE = SKILLS_DIR / "ab-brief" / "references" / "brief-template.md"
INSTRUCTIONS = (REPO_ROOT / "CLAUDE.md", REPO_ROOT / "AGENTS.md")

# The marker's opening and its fields, in order, as each copy writes it.
MARKER = re.compile(r"<!-- ab-handback pr=[^\n]+? head=[^\n]+? round=[^\n]+? -->")
MARKER_PREFIX = "<!-- ab-handback pr="


def _frontmatter(path: Path) -> dict:
    text = path.read_text(encoding="utf-8")
    assert text.startswith("---\n"), f"{path}: SKILL.md must open with a frontmatter block"
    return yaml.safe_load(text[4 : text.index("\n---\n", 4)])


@pytest.mark.parametrize("skill", sorted(p.parent.name for p in SKILLS_DIR.glob("*/SKILL.md")))
def test_every_skill_frontmatter_parses(skill: str):
    # An unquoted colon inside a folded value makes the YAML invalid, and the
    # skill is then listed with no description, so nothing ever triggers it.
    fm = _frontmatter(SKILLS_DIR / skill / "SKILL.md")
    assert fm["name"] == skill
    assert isinstance(fm.get("description"), str) and fm["description"].strip()


def test_claude_md_and_agents_md_are_the_same_instructions():
    claude, agents = (p.read_text(encoding="utf-8") for p in INSTRUCTIONS)
    assert claude == agents, "CLAUDE.md and AGENTS.md must change together"


@pytest.mark.parametrize("path", [SKILL, TEMPLATE], ids=lambda p: p.name)
def test_the_skill_and_its_template_write_the_full_marker(path: Path):
    assert MARKER.search(path.read_text(encoding="utf-8")), f"{path.name} lost the ab-handback marker"


@pytest.mark.parametrize("path", INSTRUCTIONS, ids=lambda p: p.name)
def test_the_instructions_name_the_marker_and_the_skill(path: Path):
    text = path.read_text(encoding="utf-8")
    assert MARKER_PREFIX in text, f"{path.name} must name the ab-handback marker so a session acts on it"
    assert "`ab-brief`" in text
