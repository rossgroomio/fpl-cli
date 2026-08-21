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
    assert data["metadata"]["block_extracted"] is True


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
    assert abs(a["budget_total_gbp_m"] - 99.8) < 0.01


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
    assert abs(a["budget_total_gbp_m"] - 100.5) < 0.01


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
        "### Momentum Alerts\n\nsome\n\n"
        "> Late change: Palmer → Saka — ruled out Thursday presser\n"
    )
    _run(str(f), from_recommendations=True)
    data = json.loads(capsys.readouterr().out)
    assert data["metadata"]["block_extracted"] is True
    assert data["validation"]["structural"]["starting_xi_rows"] == 11
    assert data["validation"]["structural"]["bench_rows"] == 4
    assert data["validation"]["arithmetic"]["budget_within_cap"] is True
    assert "> Late change" not in data["block"], "blockquote outside ### boundary must not be in block"


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


# ---------------------------------------------------------------------------
# New tests: team-exposure fallback, schema contract, edge cases
# ---------------------------------------------------------------------------


def test_from_recommendations_team_exposure_fallback_from_xi_bench(tmp_path, capsys):
    """Empty Team Exposure table + 4 Man City rows in Starting XI → max_per_team_ok is False."""
    xi_rows = (
        "| FWD | Player0 | Man City | £5.0m | 5.0 | 5.0 | FIX | rati |\n"
        "| FWD | Player1 | Man City | £5.0m | 5.0 | 5.0 | FIX | rati |\n"
        "| FWD | Player2 | Man City | £5.0m | 5.0 | 5.0 | FIX | rati |\n"
        "| FWD | Player3 | Man City | £5.0m | 5.0 | 5.0 | FIX | rati |\n"
        + "\n".join(
            f"| FWD | Player{i} | Arsenal | £5.0m | 5.0 | 5.0 | FIX | rati |"
            for i in range(4, 11)
        )
    )
    bench_rows_str = "\n".join(
        f"| GK | Bench{i} | Chelsea | GK | £4.0m | cover |" for i in range(4)
    )
    content = (
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
        + bench_rows_str + "\n\n"
        "#### Budget\n\n| P | C | S |\n|---|---|---|\n| **Total** | **15** | **£99.5m** |\n\n"
        "#### Team Exposure\n\n"
        "| Team | Count | Players | GW Fixture |\n"
        "|------|-------|---------|------------|\n\n"
        "#### Key Decisions\n\nSome.\n\n"
        "#### Alternatives\n\nSome.\n\n"
        "### Momentum Alerts\n\nsome\n"
    )
    f = tmp_path / "four_team_fallback.md"
    f.write_text(content, encoding="utf-8")
    _run(str(f), from_recommendations=True)
    data = json.loads(capsys.readouterr().out)
    a = data["validation"]["arithmetic"]
    assert a["max_per_team_ok"] is False
    assert a["team_exposure"].get("Man City") == 4
    assert a["team_exposure"].get("Arsenal") == 7
    assert a["team_exposure"].get("Chelsea") == 4


def test_from_recommendations_team_exposure_column_drift(tmp_path, capsys):
    """Team Exposure with | Team | Players | Count | column order (Count not in col 1) → fallback tally catches 4-from-one-club."""
    xi_rows = (
        "\n".join(
            f"| FWD | Player{i} | Liverpool | £5.0m | 5.0 | 5.0 | FIX | rati |"
            for i in range(4)
        )
        + "\n"
        + "\n".join(
            f"| FWD | Player{i} | Arsenal | £5.0m | 5.0 | 5.0 | FIX | rati |"
            for i in range(4, 11)
        )
    )
    bench_rows_str = "\n".join(
        f"| GK | Bench{i} | Chelsea | GK | £4.0m | cover |" for i in range(4)
    )
    content = (
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
        + bench_rows_str + "\n\n"
        "#### Budget\n\n| P | C | S |\n|---|---|---|\n| **Total** | **15** | **£99.5m** |\n\n"
        "#### Team Exposure\n\n"
        "| Team | Players | Count |\n"
        "|------|---------|-------|\n"
        "| Liverpool | Salah, Trent, ... | 4 |\n\n"
        "#### Key Decisions\n\nSome.\n\n"
        "#### Alternatives\n\nSome.\n\n"
        "### Momentum Alerts\n\nsome\n"
    )
    f = tmp_path / "column_drift.md"
    f.write_text(content, encoding="utf-8")
    _run(str(f), from_recommendations=True)
    data = json.loads(capsys.readouterr().out)
    a = data["validation"]["arithmetic"]
    # Column drift means primary parse reads "Players" col as count → parse fails (int("Salah, Trent, ...") throws)
    # Fallback tally from XI/Bench should catch Liverpool count = 4
    assert a["max_per_team_ok"] is False
    assert a["team_exposure"].get("Liverpool") == 4
    # Prove the fallback path was taken: Arsenal (7) and Chelsea (4) must also appear
    # (primary parse would only produce Liverpool if it read col 2 correctly)
    assert a["team_exposure"].get("Arsenal") == 7
    assert a["team_exposure"].get("Chelsea") == 4


def test_from_recommendations_team_exposure_total_row_ignored(tmp_path, capsys):
    """| Total | 15 | All players | must not be stored as exposure['Total'] = 15."""
    content = (
        "## Classic League\n\n"
        "### Classic Squad\n\n"
        "#### Constraints\n\n#### Starting XI\n\n#### Bench\n\n"
        "#### Budget\n\n| P | C | S |\n|---|---|---|\n| **Total** | **15** | **£99.5m** |\n\n"
        "#### Team Exposure\n\n"
        "| Team | Count | Players | GW Fixture |\n"
        "|------|-------|---------|------------|\n"
        "| Arsenal | 3 | Salah, Trent, X |\n"
        "| Total | 15 | All players |\n\n"
        "#### Key Decisions\n\n#### Alternatives\n\n"
        "### Momentum Alerts\n\nsome\n"
    )
    f = tmp_path / "total_row.md"
    f.write_text(content, encoding="utf-8")
    _run(str(f), from_recommendations=True)
    data = json.loads(capsys.readouterr().out)
    a = data["validation"]["arithmetic"]
    assert "Total" not in a["team_exposure"]
    assert a["team_exposure"].get("Arsenal") == 3


def test_budget_total_gbp_prefix(tmp_path, capsys):
    """| **Total** | **15** | **GBP99.5m** | returns budget_total_gbp_m == 99.5."""
    content = (
        "## Classic League\n\n"
        "### Classic Squad\n\n"
        "#### Constraints\n\n#### Starting XI\n\n#### Bench\n\n"
        "#### Budget\n\n"
        "| Position | Count | Spend |\n"
        "|----------|-------|-------|\n"
        "| **Total** | **15** | **GBP99.5m** |\n\n"
        "#### Team Exposure\n\n#### Key Decisions\n\n#### Alternatives\n\n"
        "### Momentum Alerts\n\nsome\n"
    )
    f = tmp_path / "gbp_prefix.md"
    f.write_text(content, encoding="utf-8")
    _run(str(f), from_recommendations=True)
    data = json.loads(capsys.readouterr().out)
    a = data["validation"]["arithmetic"]
    assert a["budget_total_gbp_m"] is not None
    assert abs(a["budget_total_gbp_m"] - 99.5) < 0.01


def test_budget_cap_boundary_exactly_100(tmp_path, capsys):
    """£100.0m → budget_within_cap is True (boundary is <=, not <)."""
    content = (
        "## Classic League\n\n"
        "### Classic Squad\n\n"
        "#### Constraints\n\n#### Starting XI\n\n#### Bench\n\n"
        "#### Budget\n\n"
        "| Position | Count | Spend |\n"
        "|----------|-------|-------|\n"
        "| **Total** | **15** | **£100.0m** |\n\n"
        "#### Team Exposure\n\n#### Key Decisions\n\n#### Alternatives\n\n"
        "### Momentum Alerts\n\nsome\n"
    )
    f = tmp_path / "exactly_100.md"
    f.write_text(content, encoding="utf-8")
    _run(str(f), from_recommendations=True)
    data = json.loads(capsys.readouterr().out)
    a = data["validation"]["arithmetic"]
    assert abs(a["budget_total_gbp_m"] - 100.0) < 0.001
    assert a["budget_within_cap"] is True


def test_starting_xi_over_count(tmp_path, capsys):
    """12 rows in Starting XI → starting_xi_rows == 12."""
    xi_rows = "\n".join(
        f"| FWD | Player{i} | Team | £5.0m | 5.0 | 5.0 | FIX | rati |" for i in range(12)
    )
    bench_rows_str = "\n".join(
        f"| GK | Bench{i} | Team | GK | £4.0m | cover |" for i in range(4)
    )
    content = (
        "## Classic League\n\n"
        "### Classic Squad\n\n"
        "#### Constraints\n\n#### Starting XI\n\n"
        "| Pos | Player | Team | Price | Form | PPG | Fix | Rat |\n"
        "|-----|--------|------|-------|------|-----|-----|-----|\n"
        + xi_rows + "\n\n"
        "**Captain:** X | **Vice:** Y\n\n"
        "#### Bench\n\n"
        "| Order | Player | Team | Pos | Price | Role |\n"
        "|-------|--------|------|-----|-------|------|\n"
        + bench_rows_str + "\n\n"
        "#### Budget\n\n| P | C | S |\n|---|---|---|\n| **Total** | **16** | **£99.5m** |\n\n"
        "#### Team Exposure\n\n#### Key Decisions\n\n#### Alternatives\n\n"
        "### Momentum Alerts\n\nsome\n"
    )
    f = tmp_path / "over_xi.md"
    f.write_text(content, encoding="utf-8")
    _run(str(f), from_recommendations=True)
    data = json.loads(capsys.readouterr().out)
    assert data["validation"]["structural"]["starting_xi_rows"] == 12


def test_h6_inside_fenced_block_allowed(tmp_path, capsys):
    """###### inside a fenced code block must not trigger the H6 ceiling guard."""
    content = (
        "## Classic Squad\n\n"
        "### Sub-section\n\n"
        "Some content.\n\n"
        "```python\n"
        "###### this is a comment, not a heading\n"
        "x = 1\n"
        "```\n\n"
        "More content.\n"
    )
    f = tmp_path / "h6_in_fence.md"
    f.write_text(content, encoding="utf-8")
    # Should NOT exit 1 — the H6 is inside a fenced block
    _run(str(f))
    data = json.loads(capsys.readouterr().out)
    assert "block" in data
    assert data.get("error") is not True


def test_schema_contract_extract_mode(capsys):
    """Extract mode emits {block, validation, metadata} with validation=None and mode='extract'."""
    _run(str(FIXTURE_PATH))
    data = json.loads(capsys.readouterr().out)
    assert set(data.keys()) == {"block", "validation", "metadata"}
    assert data["validation"] is None
    assert data["metadata"]["mode"] == "extract"


def test_schema_contract_from_recommendations_mode(capsys):
    """From-recommendations mode emits exact schema at all levels."""
    _run(str(RECOMMENDATIONS_FIXTURE_PATH), from_recommendations=True)
    data = json.loads(capsys.readouterr().out)
    assert set(data.keys()) == {"block", "validation", "metadata"}
    assert data["metadata"]["mode"] == "from-recommendations"
    assert data["metadata"]["block_extracted"] is True
    assert set(data["validation"].keys()) == {"structural", "arithmetic"}
    assert set(data["validation"]["structural"].keys()) == {
        "sub_headings_present", "starting_xi_rows", "bench_rows",
        "captain_named", "vice_named",
    }
    assert set(data["validation"]["arithmetic"].keys()) == {
        "budget_total_gbp_m", "budget_within_cap", "team_exposure",
        "max_per_team_ok", "player_count",
    }


def test_late_change_blockquote_in_block(tmp_path, capsys):
    """A trailing > Late change: blockquote BEFORE the next ### heading is captured in block."""
    xi_rows = "\n".join(
        f"| FWD | Player{i} | Team | £5.0m | 5.0 | 5.0 | FIX | rati |" for i in range(11)
    )
    bench_rows_str = "\n".join(
        f"| GK | Bench{i} | Team | GK | £4.0m | cover |" for i in range(4)
    )
    content = (
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
        + bench_rows_str + "\n\n"
        "#### Budget\n\n| P | C | S |\n|---|---|---|\n| **Total** | **15** | **£99.5m** |\n\n"
        "#### Team Exposure\n\n| Team | Count |\n|------|-------|\n\n"
        "#### Key Decisions\n\nSome.\n\n"
        "#### Alternatives\n\nSome.\n\n"
        "> Late change: Palmer → Saka — ruled out Thursday presser\n\n"
        "### Momentum Alerts\n\nsome\n"
    )
    f = tmp_path / "late_change_in_block.md"
    f.write_text(content, encoding="utf-8")
    _run(str(f), from_recommendations=True)
    data = json.loads(capsys.readouterr().out)
    assert "> Late change: Palmer → Saka" in data["block"]


# -- Qualified headings (issue #63) --

_count_table_rows_between = _mod._count_table_rows_between


def _recommendations_content(xi_heading: str = "#### Starting XI", bench_heading: str = "#### Bench") -> str:
    """A well-formed 15-player Classic Squad block with configurable XI/Bench headings."""
    xi_rows = "\n".join(
        f"| FWD | Player{i} | TM{i % 5} | £5.0m | 5.0 | 5.0 | FIX | rati |" for i in range(11)
    )
    bench_rows_str = "\n".join(
        f"| GK | Bench{i} | TM{i} | GK | £4.0m | cover |" for i in range(4)
    )
    return (
        "## Classic League\n\n"
        "### Classic Squad\n\n"
        "#### Constraints\n\nSome.\n\n"
        f"{xi_heading}\n\n"
        "| Pos | Player | Team | Price | Form | PPG | Fix | Rat |\n"
        "|-----|--------|------|-------|------|-----|-----|-----|\n"
        + xi_rows + "\n\n"
        "**Captain:** X | **Vice:** Y\n\n"
        f"{bench_heading}\n\n"
        "| Order | Player | Team | Pos | Price | Role |\n"
        "|-------|--------|------|-----|-------|------|\n"
        + bench_rows_str + "\n\n"
        "#### Budget\n\n| P | C | S |\n|---|---|---|\n| **Total** | **15** | **£99.5m** |\n\n"
        "#### Key Decisions\n\nSome.\n\n"
        "#### Alternatives\n\nSome.\n\n"
        "### Momentum Alerts\n\nsome\n"
    )


@pytest.mark.parametrize(
    "heading",
    [
        "#### Starting XI",
        "#### Starting XI (3-4-3)",
        "#### Starting XI (Formation: 3-4-3)",
        "#### Starting XI: 3-4-3",
        "#### Starting XI — 3-4-3",
        "#### Starting XI 3-4-3",
    ],
)
def test_count_rows_tolerates_qualified_heading(heading):
    """A formation suffix on the heading must not zero the row count."""
    block = (
        f"### Classic Squad\n{heading}\n\n"
        "| Pos | Player | Team |\n"
        "|-----|--------|------|\n"
        "| GK | Raya | ARS |\n"
        "| DEF | Gabriel | ARS |\n"
        "| FWD | Haaland | MCI |\n"
    )
    assert _count_table_rows_between(block, "#### Starting XI") == 3


def test_count_rows_does_not_match_longer_word_heading():
    """'#### Bench Order' is a different heading, not a qualified '#### Bench'."""
    block = (
        "### Classic Squad\n#### Bench Order\n\n"
        "| Order | Player |\n"
        "|-------|--------|\n"
        "| 1st | Justin |\n"
        "| 2nd | Rogers |\n"
    )
    assert _count_table_rows_between(block, "#### Bench") == 0


def test_count_rows_bench_unaffected_by_following_bench_order():
    """With both headings present, '#### Bench' counts only its own table."""
    block = (
        "### Classic Squad\n#### Bench\n\n"
        "| Order | Player |\n"
        "|-------|--------|\n"
        "| GK | Darlow |\n"
        "| 1st | Justin |\n\n"
        "#### Bench Order\n\n"
        "| Order | Player |\n"
        "|-------|--------|\n"
        "| 1st | Someone |\n"
    )
    assert _count_table_rows_between(block, "#### Bench") == 2


def test_qualified_xi_heading_validates_as_well_formed(tmp_path, capsys):
    """A block whose XI heading carries a formation must not trip Phase E."""
    f = tmp_path / "qualified_xi.md"
    f.write_text(
        _recommendations_content(xi_heading="#### Starting XI (Formation: 3-4-3)"),
        encoding="utf-8",
    )
    _run(str(f), from_recommendations=True)
    data = json.loads(capsys.readouterr().out)
    structural = data["validation"]["structural"]
    assert structural["sub_headings_present"]["Starting XI"] is True
    assert structural["starting_xi_rows"] == 11
    assert structural["bench_rows"] == 4
    assert data["validation"]["arithmetic"]["player_count"] == 15


def test_qualified_bench_heading_validates_as_well_formed(tmp_path, capsys):
    """A qualifier on the Bench heading is tolerated too."""
    f = tmp_path / "qualified_bench.md"
    f.write_text(
        _recommendations_content(bench_heading="#### Bench (GK first)"), encoding="utf-8"
    )
    _run(str(f), from_recommendations=True)
    data = json.loads(capsys.readouterr().out)
    structural = data["validation"]["structural"]
    assert structural["sub_headings_present"]["Bench"] is True
    assert structural["bench_rows"] == 4
    assert data["validation"]["arithmetic"]["player_count"] == 15


def test_bench_order_alone_does_not_satisfy_bench_sub_heading(tmp_path, capsys):
    """'#### Bench Order' must not be read as the missing '#### Bench'."""
    f = tmp_path / "bench_order_only.md"
    f.write_text(
        _recommendations_content(bench_heading="#### Bench Order"), encoding="utf-8"
    )
    _run(str(f), from_recommendations=True)
    data = json.loads(capsys.readouterr().out)
    assert data["validation"]["structural"]["sub_headings_present"]["Bench"] is False


def test_qualified_headings_keep_team_exposure_fallback_working(tmp_path, capsys):
    """The Team column tally falls back correctly through qualified XI/Bench headings."""
    f = tmp_path / "qualified_exposure.md"
    f.write_text(
        _recommendations_content(
            xi_heading="#### Starting XI (3-4-3)", bench_heading="#### Bench (GK first)"
        ),
        encoding="utf-8",
    )
    _run(str(f), from_recommendations=True)
    data = json.loads(capsys.readouterr().out)
    arithmetic = data["validation"]["arithmetic"]
    assert sum(arithmetic["team_exposure"].values()) == 15
    assert arithmetic["team_exposure"]["TM0"] == 4  # 3 in the XI + 1 on the bench


def test_qualified_team_exposure_heading_is_parsed(tmp_path, capsys):
    """A qualifier on '#### Team Exposure' must not empty the exposure table."""
    block = _recommendations_content().replace(
        "#### Key Decisions",
        "#### Team Exposure (max 3)\n\n"
        "| Team | Count |\n|------|-------|\n| ARS | 3 |\n| MCI | 2 |\n\n"
        "#### Key Decisions",
    )
    f = tmp_path / "qualified_exposure_table.md"
    f.write_text(block, encoding="utf-8")
    _run(str(f), from_recommendations=True)
    data = json.loads(capsys.readouterr().out)
    arithmetic = data["validation"]["arithmetic"]
    assert arithmetic["team_exposure"] == {"ARS": 3, "MCI": 2}
    assert arithmetic["max_per_team_ok"] is True
    assert data["validation"]["structural"]["sub_headings_present"]["Team Exposure"] is True


# -- Review-fix regressions (heading/table-parsing bugs found in PR 64 review) --


def test_count_rows_does_not_match_glued_compound_word_heading():
    """'#### Bench-Warmers' is a different heading, not a punctuation-qualified
    '#### Bench' — a qualifier glued with no separating space must not continue
    into a word character, or a hyphenated compound heading would be silently
    swallowed as 'Bench' with an ignored suffix."""
    block = (
        "### Classic Squad\n#### Bench-Warmers\n\n"
        "| Order | Player |\n"
        "|-------|--------|\n"
        "| 1st | Justin |\n"
        "| 2nd | Rogers |\n"
    )
    assert _count_table_rows_between(block, "#### Bench") == 0


def test_glued_word_qualifier_on_team_exposure_heading_falls_back(tmp_path, capsys):
    """A heading corrupted with a glued word continuation (no space) must not be
    read as '#### Team Exposure' — the validator must fall back to the XI/Bench
    tally rather than silently parsing an unrelated decoy table."""
    block = _recommendations_content().replace(
        "#### Key Decisions",
        "#### Team Exposure.Deprecated\n\n"
        "| Team | Count |\n|------|-------|\n| ARS | 1 |\n\n"
        "#### Key Decisions",
    )
    f = tmp_path / "glued_qualifier_exposure.md"
    f.write_text(block, encoding="utf-8")
    _run(str(f), from_recommendations=True)
    data = json.loads(capsys.readouterr().out)
    arithmetic = data["validation"]["arithmetic"]
    structural = data["validation"]["structural"]
    assert structural["sub_headings_present"]["Team Exposure"] is False
    assert arithmetic["team_exposure"] != {"ARS": 1}
    assert sum(arithmetic["team_exposure"].values()) == 15


def test_repeated_team_exposure_heading_merges_not_truncates(tmp_path, capsys):
    """Two '#### Team Exposure' headings must merge into one section, not have the
    second one silently discarded — otherwise a real per-team violation reported
    only in the second table would go undetected."""
    content = (
        "## Classic League\n\n"
        "### Classic Squad\n\n"
        "#### Constraints\n\n#### Starting XI\n\n#### Bench\n\n"
        "#### Budget\n\n| P | C | S |\n|---|---|---|\n| **Total** | **15** | **£99.5m** |\n\n"
        "#### Team Exposure\n\n"
        "| Team | Count |\n"
        "|------|-------|\n"
        "| Arsenal | 2 |\n\n"
        "#### Team Exposure\n\n"
        "| Team | Count |\n"
        "|------|-------|\n"
        "| Man City | 4 |\n\n"
        "#### Key Decisions\n\n#### Alternatives\n\n"
        "### Momentum Alerts\n\nsome\n"
    )
    f = tmp_path / "repeated_team_exposure.md"
    f.write_text(content, encoding="utf-8")
    _run(str(f), from_recommendations=True)
    data = json.loads(capsys.readouterr().out)
    a = data["validation"]["arithmetic"]
    assert a["team_exposure"].get("Man City") == 4
    assert a["max_per_team_ok"] is False


def test_count_rows_gfm_colon_aligned_separator_not_counted_as_row():
    """A GFM alignment-colon separator ('|:----|:-----|') must be recognised as
    the header/body divider, not miscounted as an extra data row."""
    block = (
        "### Classic Squad\n#### Starting XI\n\n"
        "| Pos | Player | Team |\n"
        "|:----|:-------|:-----|\n"
        "| GK | Raya | ARS |\n"
        "| DEF | Gabriel | ARS |\n"
        "| FWD | Haaland | MCI |\n"
    )
    assert _count_table_rows_between(block, "#### Starting XI") == 3


def test_indented_next_heading_still_terminates_section():
    """A next heading indented with leading whitespace must still close the
    current section rather than letting its table bleed into the previous count."""
    block = (
        "### Classic Squad\n#### Bench\n\n"
        "| Order | Player |\n"
        "|-------|--------|\n"
        "| GK | Darlow |\n"
        "| 1st | Justin |\n\n"
        "  #### Budget\n\n"
        "| P | C |\n"
        "|---|---|\n"
        "| **Total** | **15** |\n"
    )
    assert _count_table_rows_between(block, "#### Bench") == 2
