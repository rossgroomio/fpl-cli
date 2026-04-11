"""Tests for .agents/skills/gw-prep/scripts/extract_classic_squad.py."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import ModuleType

import pytest


def _load_script() -> ModuleType:
    """Load extract_classic_squad.py as a module (it's not a package)."""
    script_path = (
        Path(__file__).parent.parent
        / ".agents/skills/gw-prep/scripts/extract_classic_squad.py"
    )
    spec = importlib.util.spec_from_file_location("extract_classic_squad", script_path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_mod = _load_script()
_run = _mod._run


FIXTURE_PATH = Path(__file__).parent / "fixtures" / "classic_squad_fixture.md"


# -- Happy path --


def test_extraction_exits_zero_and_returns_valid_json(capsys):
    _run(str(FIXTURE_PATH))
    captured = capsys.readouterr()
    data = json.loads(captured.out)
    assert "block" in data
    assert "metadata" in data


def test_extracted_block_starts_with_demoted_heading(capsys):
    _run(str(FIXTURE_PATH))
    captured = capsys.readouterr()
    data = json.loads(captured.out)
    assert data["block"].startswith("### Classic Squad")


def test_all_sub_headings_present_and_demoted(capsys):
    _run(str(FIXTURE_PATH))
    captured = capsys.readouterr()
    data = json.loads(captured.out)
    block = data["block"]
    for sub in [
        "#### Constraints",
        "#### Starting XI",
        "#### Bench",
        "#### Budget",
        "#### Team Exposure",
        "#### Key Decisions",
        "#### Alternatives",
    ]:
        assert sub in block, f"Expected '{sub}' in extracted block"


def test_metadata_heading_demoted_and_had_draft_rankings(capsys):
    _run(str(FIXTURE_PATH))
    captured = capsys.readouterr()
    data = json.loads(captured.out)
    assert data["metadata"]["heading_demoted"] is True
    assert data["metadata"]["had_draft_rankings"] is True


def test_frontmatter_not_in_block(capsys):
    _run(str(FIXTURE_PATH))
    captured = capsys.readouterr()
    data = json.loads(captured.out)
    block = data["block"]
    assert "mode: Wildcard" not in block
    assert "gameweek: 32" not in block
    assert "budget: 99.5" not in block


def test_draft_rankings_not_in_block(capsys):
    _run(str(FIXTURE_PATH))
    captured = capsys.readouterr()
    data = json.loads(captured.out)
    block = data["block"]
    assert "## Draft Rankings" not in block
    # also not under any demoted heading
    assert "### Draft Rankings" not in block
    assert "Draft Rankings" not in block


# -- Edge cases --


def test_classic_only_no_draft_rankings(tmp_path, capsys):
    f = tmp_path / "classic_only.md"
    f.write_text(
        "---\nmode: Wildcard\ngameweek: 32\n---\n\n"
        "## Classic Squad\n\n"
        "### Constraints\nSome constraints.\n\n"
        "### Starting XI\nContent.\n\n"
        "### Bench\nContent.\n\n"
        "### Budget\nContent.\n\n"
        "### Team Exposure\nContent.\n\n"
        "### Key Decisions\nContent.\n\n"
        "### Alternatives\nContent.\n"
    )
    _run(str(f))
    captured = capsys.readouterr()
    data = json.loads(captured.out)
    assert data["metadata"]["had_draft_rankings"] is False


def test_h4_heading_demoted_to_h5(capsys):
    """Fixture has '#### Detail' inside the Classic Squad block; after demotion it becomes '##### Detail'."""
    _run(str(FIXTURE_PATH))
    captured = capsys.readouterr()
    data = json.loads(captured.out)
    assert "##### Detail" in data["block"]


def test_trailing_blank_lines_stripped(capsys):
    _run(str(FIXTURE_PATH))
    captured = capsys.readouterr()
    data = json.loads(captured.out)
    block = data["block"]
    assert block.rstrip("\n") == block


def test_fenced_code_block_hash_not_demoted(capsys):
    """'# python comment' inside a fenced code block should survive undemoted."""
    _run(str(FIXTURE_PATH))
    captured = capsys.readouterr()
    data = json.loads(captured.out)
    block = data["block"]
    assert "# python comment" in block
    assert "## python comment" not in block


# -- Error paths --


def test_nonexistent_file_exits_1(tmp_path, capsys):
    nonexistent = str(tmp_path / "does_not_exist.md")
    with pytest.raises(SystemExit, match="1"):
        _run(nonexistent)
    captured = capsys.readouterr()
    data = json.loads(captured.out)
    assert data["error"] is True
    assert any(
        "does_not_exist" in m or "not found" in m.lower() or "No such file" in m
        for m in data["messages"]
    )


def test_no_classic_squad_heading_exits_1(tmp_path, capsys):
    f = tmp_path / "no_heading.md"
    f.write_text("# Just a title\n\nSome content without the expected heading.\n")
    with pytest.raises(SystemExit, match="1"):
        _run(str(f))
    captured = capsys.readouterr()
    data = json.loads(captured.out)
    assert data["error"] is True
    assert any("Classic Squad" in m for m in data["messages"])


def test_empty_block_exits_1(tmp_path, capsys):
    f = tmp_path / "empty_block.md"
    f.write_text(
        "## Classic Squad\n\n"
        "## Draft Rankings\n\nSome draft content\n"
    )
    with pytest.raises(SystemExit, match="1"):
        _run(str(f))
    captured = capsys.readouterr()
    data = json.loads(captured.out)
    assert data["error"] is True


def test_h6_source_heading_exits_1(tmp_path, capsys):
    f = tmp_path / "h6_heading.md"
    f.write_text(
        "## Classic Squad\n\n"
        "### Sub-section\n\nSome content.\n\n"
        "###### Deep Heading\n\nContent under H6.\n"
    )
    with pytest.raises(SystemExit, match="1"):
        _run(str(f))
    captured = capsys.readouterr()
    data = json.loads(captured.out)
    assert data["error"] is True
    assert any(
        "H6" in m or "heading depth" in m.lower() or "exceed" in m.lower()
        for m in data["messages"]
    )
