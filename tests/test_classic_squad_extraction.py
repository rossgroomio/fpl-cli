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


# ---------------------------------------------------------------------------
# --from-recommendations mode
# ---------------------------------------------------------------------------

RECOMMENDATIONS_FIXTURE_PATH = Path(__file__).parent / "fixtures" / "recommendations_fixture.md"


def test_from_recommendations_returns_valid_json(capsys):
    _run(str(RECOMMENDATIONS_FIXTURE_PATH), from_recommendations=True)
    captured = capsys.readouterr()
    data = json.loads(captured.out)
    assert "block" in data
    assert "validation" in data
    assert data["metadata"]["parse_ok"] is True


def test_from_recommendations_block_starts_with_heading(capsys):
    _run(str(RECOMMENDATIONS_FIXTURE_PATH), from_recommendations=True)
    data = json.loads(capsys.readouterr().out)
    assert data["block"].startswith("### Classic Squad")


def test_from_recommendations_all_sub_headings_present(capsys):
    _run(str(RECOMMENDATIONS_FIXTURE_PATH), from_recommendations=True)
    data = json.loads(capsys.readouterr().out)
    present = data["validation"]["structural"]["sub_headings_present"]
    for name in ["Starting XI", "Bench", "Budget", "Team Exposure", "Key Decisions", "Alternatives"]:
        assert present[name] is True, f"Expected '{name}' sub-heading present"


def test_from_recommendations_row_counts(capsys):
    _run(str(RECOMMENDATIONS_FIXTURE_PATH), from_recommendations=True)
    data = json.loads(capsys.readouterr().out)
    s = data["validation"]["structural"]
    assert s["starting_xi_rows"] == 11
    assert s["bench_rows"] == 4


def test_from_recommendations_player_count(capsys):
    _run(str(RECOMMENDATIONS_FIXTURE_PATH), from_recommendations=True)
    data = json.loads(capsys.readouterr().out)
    assert data["validation"]["arithmetic"]["player_count"] == 15


def test_from_recommendations_captain_vice_named(capsys):
    _run(str(RECOMMENDATIONS_FIXTURE_PATH), from_recommendations=True)
    data = json.loads(capsys.readouterr().out)
    s = data["validation"]["structural"]
    assert s["captain_named"] is True
    assert s["vice_named"] is True


def test_from_recommendations_budget_within_cap(capsys):
    _run(str(RECOMMENDATIONS_FIXTURE_PATH), from_recommendations=True)
    data = json.loads(capsys.readouterr().out)
    a = data["validation"]["arithmetic"]
    assert a["budget_within_cap"] is True
    assert abs(a["budget_total_mlm"] - 99.8) < 0.01


def test_from_recommendations_max_per_team_ok(capsys):
    _run(str(RECOMMENDATIONS_FIXTURE_PATH), from_recommendations=True)
    data = json.loads(capsys.readouterr().out)
    assert data["validation"]["arithmetic"]["max_per_team_ok"] is True


# -- Structural failure cases --


def test_missing_bench_sub_heading(tmp_path, capsys):
    """Block with no #### Bench → sub_headings_present["Bench"] is False."""
    f = tmp_path / "no_bench.md"
    xi_rows = "\n".join(f"| FWD | Player{i} | Team | £5.0m | 5.0 | 5.0 | FIX | rati |" for i in range(11))
    f.write_text(
        "## Classic League\n\n"
        "### Classic Squad\n\n"
        "#### Constraints\n\nSome constraints.\n\n"
        "#### Starting XI\n\n"
        "| Pos | Player | Team | Price | Form | PPG | Fix | Rat |\n"
        "|-----|--------|------|-------|------|-----|-----|-----|\n"
        + xi_rows + "\n\n"
        "**Captain:** X | **Vice:** Y\n\n"
        "#### Budget\n\n"
        "| Position | Count | Spend |\n"
        "|----------|-------|-------|\n"
        "| **Total** | **15** | **£99.5m** |\n\n"
        "#### Team Exposure\n\n"
        "| Team | Count |\n"
        "|------|-------|\n\n"
        "#### Key Decisions\n\nSome decisions.\n\n"
        "#### Alternatives\n\nSome alternatives.\n\n"
        "### Momentum Alerts\n\nsome content\n"
    )
    _run(str(f), from_recommendations=True)
    data = json.loads(capsys.readouterr().out)
    assert data["validation"]["structural"]["sub_headings_present"]["Bench"] is False


def test_wrong_starting_xi_row_count(tmp_path, capsys):
    """#### Starting XI with only 10 player rows → starting_xi_rows == 10."""
    f = tmp_path / "short_xi.md"
    xi_rows = "\n".join(f"| FWD | Player{i} | Team | £5.0m | 5.0 | 5.0 | FIX | rati |" for i in range(10))
    bench_rows = "\n".join(f"| GK | Bench{i} | Team | GK | £4.0m | cover |" for i in range(4))
    f.write_text(
        "## Classic League\n\n"
        "### Classic Squad\n\n"
        "#### Constraints\n\nSome.\n\n"
        "#### Starting XI\n\n"
        "| Pos | Player | Team | Price | Form | PPG | Fix | Rat |\n"
        "|-----|--------|------|-------|------|-----|-----|-----|\n"
        + xi_rows + "\n\n"
        "**Captain:** X | **Vice:** Y\n\n"
        "#### Bench\n\n"
        "| Order | Player | Team | Pos | Price | Role |\n"
        "|-------|--------|------|-----|-------|------|\n"
        + bench_rows + "\n\n"
        "#### Budget\n\n| P | C | S |\n|---|---|---|\n| **Total** | **14** | **£99.5m** |\n\n"
        "#### Team Exposure\n\n#### Key Decisions\n\n#### Alternatives\n\n"
        "### Momentum Alerts\n\nsome content\n"
    )
    _run(str(f), from_recommendations=True)
    data = json.loads(capsys.readouterr().out)
    assert data["validation"]["structural"]["starting_xi_rows"] == 10


# -- Arithmetic failure cases --


def test_over_budget(tmp_path, capsys):
    """Budget Total of £100.5m → budget_within_cap is False."""
    f = tmp_path / "over_budget.md"
    xi_rows = "\n".join(f"| FWD | Player{i} | Team | £5.0m | 5.0 | 5.0 | FIX | rati |" for i in range(11))
    bench_rows = "\n".join(f"| GK | Bench{i} | Team | GK | £4.0m | cover |" for i in range(4))
    f.write_text(
        "## Classic League\n\n"
        "### Classic Squad\n\n"
        "#### Constraints\n\n#### Starting XI\n\n"
        "| Pos | Player | Team | Price | Form | PPG | Fix | Rat |\n"
        "|-----|--------|------|-------|------|-----|-----|-----|\n"
        + xi_rows + "\n\n**Captain:** X | **Vice:** Y\n\n"
        "#### Bench\n\n"
        "| Order | Player | Team | Pos | Price | Role |\n"
        "|-------|--------|------|-----|-------|------|\n"
        + bench_rows + "\n\n"
        "#### Budget\n\n"
        "| Position | Count | Spend |\n"
        "|----------|-------|-------|\n"
        "| **Total** | **15** | **£100.5m** |\n\n"
        "#### Team Exposure\n\n#### Key Decisions\n\n#### Alternatives\n\n"
        "### Momentum Alerts\n\nsome\n"
    )
    _run(str(f), from_recommendations=True)
    data = json.loads(capsys.readouterr().out)
    a = data["validation"]["arithmetic"]
    assert a["budget_within_cap"] is False
    assert abs(a["budget_total_mlm"] - 100.5) < 0.01


def test_four_per_team_violation(tmp_path, capsys):
    """4 players from same team → max_per_team_ok is False."""
    f = tmp_path / "four_per_team.md"
    f.write_text(
        "## Classic League\n\n"
        "### Classic Squad\n\n"
        "#### Constraints\n\n#### Starting XI\n\n#### Bench\n\n"
        "#### Budget\n\n| P | C | S |\n|---|---|---|\n| **Total** | **15** | **£99.5m** |\n\n"
        "#### Team Exposure\n\n"
        "| Team | Count | Players |\n"
        "|------|-------|--------|\n"
        "| Man City | 4 | Haaland, Gvardiol, Walker, Ederson |\n\n"
        "#### Key Decisions\n\n#### Alternatives\n\n"
        "### Momentum Alerts\n\nsome\n"
    )
    _run(str(f), from_recommendations=True)
    data = json.loads(capsys.readouterr().out)
    a = data["validation"]["arithmetic"]
    assert a["max_per_team_ok"] is False
    assert a["team_exposure"].get("Man City") == 4


def test_wrong_player_count(tmp_path, capsys):
    """14 players total → player_count != 15."""
    f = tmp_path / "short_squad.md"
    xi_rows = "\n".join(f"| FWD | Player{i} | Team | £5.0m | 5.0 | 5.0 | FIX | rati |" for i in range(10))
    bench_rows = "\n".join(f"| GK | Bench{i} | Team | GK | £4.0m | cover |" for i in range(4))
    f.write_text(
        "## Classic League\n\n"
        "### Classic Squad\n\n"
        "#### Constraints\n\n#### Starting XI\n\n"
        "| Pos | Player | Team | Price | Form | PPG | Fix | Rat |\n"
        "|-----|--------|------|-------|------|-----|-----|-----|\n"
        + xi_rows + "\n\n**Captain:** X | **Vice:** Y\n\n"
        "#### Bench\n\n"
        "| Order | Player | Team | Pos | Price | Role |\n"
        "|-------|--------|------|-----|-------|------|\n"
        + bench_rows + "\n\n"
        "#### Budget\n\n| P | C | S |\n|---|---|---|\n| **Total** | **14** | **£95.0m** |\n\n"
        "#### Team Exposure\n\n#### Key Decisions\n\n#### Alternatives\n\n"
        "### Momentum Alerts\n\nsome\n"
    )
    _run(str(f), from_recommendations=True)
    data = json.loads(capsys.readouterr().out)
    assert data["validation"]["arithmetic"]["player_count"] == 14


# -- Error paths --


def test_from_recommendations_file_not_found_exits_1(tmp_path, capsys):
    with pytest.raises(SystemExit, match="1"):
        _run(str(tmp_path / "missing.md"), from_recommendations=True)
    data = json.loads(capsys.readouterr().out)
    assert data["error"] is True


def test_from_recommendations_no_heading_exits_1(tmp_path, capsys):
    f = tmp_path / "no_heading.md"
    f.write_text("## Classic League\n\nNo classic squad section here.\n")
    with pytest.raises(SystemExit, match="1"):
        _run(str(f), from_recommendations=True)
    data = json.loads(capsys.readouterr().out)
    assert data["error"] is True
    assert any("Classic Squad" in m for m in data["messages"])


# -- Late-change note doesn't break validation --


def test_late_change_note_does_not_break_validation(tmp_path, capsys):
    """Trailing > Late change: blockquote after the block is not inside the ### Classic Squad
    slice (it follows the next ### heading boundary) — validation still passes cleanly."""
    f = tmp_path / "late_change.md"
    xi_rows = "\n".join(f"| FWD | Player{i} | Team | £5.0m | 5.0 | 5.0 | FIX | rati |" for i in range(11))
    bench_rows = "\n".join(f"| GK | Bench{i} | Team | GK | £4.0m | cover |" for i in range(4))
    f.write_text(
        "## Classic League\n\n"
        "### Classic Squad\n\n"
        "#### Constraints\n\nSome.\n\n"
        "#### Starting XI\n\n"
        "| Pos | Player | Team | Price | Form | PPG | Fix | Rat |\n"
        "|-----|--------|------|-------|------|-----|-----|-----|\n"
        + xi_rows + "\n\n"
        "**Captain:** X | **Vice:** Y\n\n"
        "#### Bench\n\n"
        "| Order | Player | Team | Pos | Price | Role |\n"
        "|-------|--------|------|-----|-------|------|\n"
        + bench_rows + "\n\n"
        "#### Budget\n\n| P | C | S |\n|---|---|---|\n| **Total** | **15** | **£99.5m** |\n\n"
        "#### Team Exposure\n\n| Team | Count |\n|------|-------|\n\n"
        "#### Key Decisions\n\nSome.\n\n"
        "#### Alternatives\n\nSome.\n\n"
        "> Late change: Palmer → Saka — ruled out Thursday presser\n\n"
        "### Momentum Alerts\n\nsome\n"
    )
    _run(str(f), from_recommendations=True)
    data = json.loads(capsys.readouterr().out)
    assert data["metadata"]["parse_ok"] is True
    assert data["validation"]["structural"]["starting_xi_rows"] == 11
    assert data["validation"]["structural"]["bench_rows"] == 4
    assert data["validation"]["arithmetic"]["budget_within_cap"] is True


# -- Read-only invariant --


def test_from_recommendations_does_not_mutate_file(tmp_path, capsys):
    """Running --from-recommendations must not change the file's mtime."""
    import os
    import time

    f = tmp_path / "readonly_check.md"
    f.write_text("## Classic League\n\n### Classic Squad\n\nSome content.\n")
    # Sleep briefly to ensure a write would produce a different mtime
    mtime_before = os.stat(f).st_mtime
    time.sleep(0.05)
    try:
        _run(str(f), from_recommendations=True)
    except SystemExit:
        pass  # exit 1 is fine (no Classic Squad heading body), just must not write
    capsys.readouterr()  # drain
    mtime_after = os.stat(f).st_mtime
    assert mtime_after == mtime_before, "Helper must not mutate the input file"
