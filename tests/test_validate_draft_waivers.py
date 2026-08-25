"""Tests for .agents/skills/gw-prep/scripts/validate_draft_waivers.py."""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

SCRIPT_PATH = (
    Path(__file__).parent.parent
    / ".agents/skills/gw-prep/scripts/validate_draft_waivers.py"
)
FIXTURE_DIR = Path(__file__).parent / "fixtures" / "validate_draft_waivers"


def _load_script() -> ModuleType:
    """Load validate_draft_waivers.py as a module (it's not a package).

    Its only import beyond the stdlib is `fpl_cli.utils.markdown`, resolved
    through the installed fpl-cli package, so no sys.path manipulation is
    needed to load it standalone.
    """
    spec = importlib.util.spec_from_file_location("validate_draft_waivers", SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_mod = _load_script()
_run = _mod._run


# ---- Shared fixture helpers --------------------------------------------------

_WAIVERS_BASE = {
    "command": "fpl waivers",
    "metadata": {"gameweek": 34},
    "data": {
        "top_targets": [
            {"player_name": "Lacroix", "position": "DEF", "team_short": "CRY"},
            {"player_name": "Pedro Porro", "position": "DEF", "team_short": "TOT"},
            {"player_name": "E.Le Fée", "position": "MID", "team_short": "SUN"},
            {"player_name": "Canvot", "position": "DEF", "team_short": "CRY"},
        ]
    },
}

# The full unowned roster: every ranked target plus a flagged returnee that the
# `minutes / 450` availability factor keeps out of the top 15 forever.
_WAIVERS_WITH_POOL = {
    "command": "fpl waivers",
    "metadata": {"gameweek": 34},
    "data": {
        "top_targets": _WAIVERS_BASE["data"]["top_targets"],
        "pool": [
            {"id": 101, "player_name": "Lacroix", "position": "DEF", "team_short": "CRY"},
            {"id": 102, "player_name": "Pedro Porro", "position": "DEF", "team_short": "TOT"},
            {"id": 103, "player_name": "E.Le Fée", "position": "MID", "team_short": "SUN"},
            {"id": 104, "player_name": "Canvot", "position": "DEF", "team_short": "CRY"},
            {"id": 105, "player_name": "Havertz", "position": "FWD", "team_short": "ARS"},
        ],
    },
}

_SQUAD_BASE = {
    "command": "fpl squad grid",
    "metadata": {"gameweek": 34},
    "data": [
        {"player": "Flekken", "position": "GK", "team": "BRE"},
        {"player": "Raya", "position": "GK", "team": "ARS"},
        {"player": "Hill", "position": "DEF", "team": "BOU"},
        {"player": "Konsa", "position": "DEF", "team": "AVL"},
        {"player": "Bassey", "position": "DEF", "team": "FUL"},
        {"player": "Gabriel", "position": "DEF", "team": "ARS"},
        {"player": "Walker-Peters", "position": "DEF", "team": "WHU"},
        {"player": "Scott", "position": "MID", "team": "BOU"},
        {"player": "Gross", "position": "MID", "team": "BHA"},
        {"player": "Saka", "position": "MID", "team": "ARS"},
        {"player": "Gibbs-White", "position": "MID", "team": "NFO"},
        {"player": "Mbeumo", "position": "MID", "team": "BRE"},
        {"player": "João Pedro", "position": "FWD", "team": "CHE"},
        {"player": "Watkins", "position": "FWD", "team": "AVL"},
        {"player": "Beto", "position": "FWD", "team": "EVE"},
    ],
}

_CLEAN_RECS = """\
## Draft

### Waiver Recommendations

| Priority | Drop | Claim | Position | Fixture Run | Rationale |
|----------|------|-------|----------|-------------|-----------|
| 1 | Hill (BOU) | Lacroix (CRY) | DEF | A LIV / H MUN | Straight upgrade. |
| 2 | Scott (BOU) | E.Le Fée (SUN) | MID | H NFO / A WOL | Blanker to starter. |
| 3 | (depth) | Canvot (CRY) | DEF | A LIV / H MUN | Depth only. |
| 4 | - | - | - | - | No further moves. |
"""


def _write_fixtures(
    tmp_path: Path,
    recs_content: str,
    waivers: dict | None = None,
    squad: dict | None = None,
) -> tuple[Path, Path, Path]:
    recs = tmp_path / "recs.md"
    recs.write_text(recs_content, encoding="utf-8")
    w = tmp_path / "waivers.json"
    w.write_text(json.dumps(waivers or _WAIVERS_BASE), encoding="utf-8")
    s = tmp_path / "squad.json"
    s.write_text(json.dumps(squad or _SQUAD_BASE), encoding="utf-8")
    return recs, w, s


def _parse(capsys) -> dict:
    return json.loads(capsys.readouterr().out)


# ---- Happy path -------------------------------------------------------------


def test_clean_file_ok_no_flags(tmp_path, capsys):
    recs, w, s = _write_fixtures(tmp_path, _CLEAN_RECS)
    _run(str(recs), str(w), str(s))
    data = _parse(capsys)
    assert data["ok"] is True
    assert data["flags"] == []
    assert data["warnings"] == []


# ---- Waiver-not-in-pool (non-blocking flag) ---------------------------------


def test_claim_not_in_pool_flagged(tmp_path, capsys):
    recs_content = """\
## Draft

### Waiver Recommendations

| Priority | Drop | Claim | Position | Fixture Run | Rationale |
|----------|------|-------|----------|-------------|-----------|
| 1 | Hill (BOU) | Thiago (BRE) | MID | A MUN | Pool miss. |
"""
    recs, w, s = _write_fixtures(tmp_path, recs_content)
    _run(str(recs), str(w), str(s))
    data = _parse(capsys)
    assert data["ok"] is False
    assert any(f["type"] == "waiver-not-in-pool" for f in data["flags"])
    flag = next(f for f in data["flags"] if f["type"] == "waiver-not-in-pool")
    assert "Thiago" in flag.get("claim", "")


# ---- Cross-position (blocking flag) -----------------------------------------


def test_cross_position_swap_flagged(tmp_path, capsys):
    recs_content = """\
## Draft

### Waiver Recommendations

| Priority | Drop | Claim | Position | Fixture Run | Rationale |
|----------|------|-------|----------|-------------|-----------|
| 1 | João Pedro (CHE) | Pedro Porro (TOT) | DEF | A WOL | Cross-pos. |
"""
    recs, w, s = _write_fixtures(tmp_path, recs_content)
    _run(str(recs), str(w), str(s))
    data = _parse(capsys)
    assert data["ok"] is False
    assert any(f["type"] == "cross-position-claim" for f in data["flags"])
    flag = next(f for f in data["flags"] if f["type"] == "cross-position-claim")
    assert flag["drop_position"] == "FWD"
    assert flag["claim_position"] == "DEF"


# ---- Placeholder rows skipped -----------------------------------------------


def test_dash_placeholder_skipped(tmp_path, capsys):
    recs_content = """\
## Draft

### Waiver Recommendations

| Priority | Drop | Claim | Position | Fixture Run | Rationale |
|----------|------|-------|----------|-------------|-----------|
| 1 | - | - | - | - | No moves. |
"""
    recs, w, s = _write_fixtures(tmp_path, recs_content)
    _run(str(recs), str(w), str(s))
    data = _parse(capsys)
    assert data["ok"] is True
    assert data["flags"] == []


def test_depth_placeholder_skipped(tmp_path, capsys):
    recs_content = """\
## Draft

### Waiver Recommendations

| Priority | Drop | Claim | Position | Fixture Run | Rationale |
|----------|------|-------|----------|-------------|-----------|
| 1 | (depth) | Canvot (CRY) | DEF | A LIV | Depth. |
"""
    recs, w, s = _write_fixtures(tmp_path, recs_content)
    _run(str(recs), str(w), str(s))
    data = _parse(capsys)
    assert data["ok"] is True
    assert data["flags"] == []


def test_bold_hold_placeholder_skipped(tmp_path, capsys):
    recs_content = """\
## Draft

### Waiver Recommendations

| Priority | Drop | Claim | Position | Fixture Run | Rationale |
|----------|------|-------|----------|-------------|-----------|
| 1 | **HOLD** | **HOLD** | - | - | Hold. |
"""
    recs, w, s = _write_fixtures(tmp_path, recs_content)
    _run(str(recs), str(w), str(s))
    data = _parse(capsys)
    assert data["ok"] is True
    assert data["flags"] == []


# ---- Name normalisation -----------------------------------------------------


def test_nfkd_accent_insensitive_match(tmp_path, capsys):
    """E.Le Fee (no accent in rec) matches E.Le Fée in pool after NFKD."""
    recs_content = """\
## Draft

### Waiver Recommendations

| Priority | Drop | Claim | Position | Fixture Run | Rationale |
|----------|------|-------|----------|-------------|-----------|
| 1 | Scott (BOU) | E.Le Fee (SUN) | MID | H NFO | Good. |
"""
    recs, w, s = _write_fixtures(tmp_path, recs_content)
    _run(str(recs), str(w), str(s))
    data = _parse(capsys)
    assert data["ok"] is True
    assert data["flags"] == []


def test_last_token_fallback_matches_full_pool_name(tmp_path, capsys):
    """'Porro (TOT)' in rec matches 'Pedro Porro' in pool via last-token fallback."""
    recs_content = """\
## Draft

### Waiver Recommendations

| Priority | Drop | Claim | Position | Fixture Run | Rationale |
|----------|------|-------|----------|-------------|-----------|
| 1 | Konsa (AVL) | Porro (TOT) | DEF | A WOL | Good. |
"""
    recs, w, s = _write_fixtures(tmp_path, recs_content)
    _run(str(recs), str(w), str(s))
    data = _parse(capsys)
    assert data["ok"] is True
    assert data["flags"] == []


def test_position_scoped_no_false_collision(tmp_path, capsys):
    """Pool has Son (MID) and Johnson (DEF); Claim Son position MID hits Son only."""
    waivers = {
        "command": "fpl waivers",
        "data": {
            "top_targets": [
                {"player_name": "Son", "position": "MID", "team_short": "TOT"},
                {"player_name": "Johnson", "position": "DEF", "team_short": "CRY"},
            ]
        },
    }
    squad = {
        "command": "fpl squad grid",
        "data": [
            {"player": "Raya", "position": "GK", "team": "ARS"},
            {"player": "Flekken", "position": "GK", "team": "BRE"},
            {"player": "Hill", "position": "DEF", "team": "BOU"},
            {"player": "Konsa", "position": "DEF", "team": "AVL"},
            {"player": "Bassey", "position": "DEF", "team": "FUL"},
            {"player": "Gabriel", "position": "DEF", "team": "ARS"},
            {"player": "Walker-Peters", "position": "DEF", "team": "WHU"},
            {"player": "Scott", "position": "MID", "team": "BOU"},
            {"player": "Gross", "position": "MID", "team": "BHA"},
            {"player": "Saka", "position": "MID", "team": "ARS"},
            {"player": "Gibbs-White", "position": "MID", "team": "NFO"},
            {"player": "Mbeumo", "position": "MID", "team": "BRE"},
            {"player": "Watkins", "position": "FWD", "team": "AVL"},
            {"player": "Beto", "position": "FWD", "team": "EVE"},
            {"player": "Wilson", "position": "FWD", "team": "NEW"},
        ],
    }
    recs_content = """\
## Draft

### Waiver Recommendations

| Priority | Drop | Claim | Position | Fixture Run | Rationale |
|----------|------|-------|----------|-------------|-----------|
| 1 | Scott (BOU) | Son (TOT) | MID | H WOL | Attack. |
"""
    recs, w, s = _write_fixtures(tmp_path, recs_content, waivers=waivers, squad=squad)
    _run(str(recs), str(w), str(s))
    data = _parse(capsys)
    assert data["ok"] is True
    assert data["flags"] == []


def test_dot_strip_normalisation(tmp_path, capsys):
    """Pool 'J.Pedro (FWD)' matches rec 'J Pedro (CHE)' after dot normalisation."""
    waivers = {
        "command": "fpl waivers",
        "data": {
            "top_targets": [
                {"player_name": "J.Pedro", "position": "FWD", "team_short": "CHE"},
            ]
        },
    }
    squad = {
        "command": "fpl squad grid",
        "data": [
            {"player": "Raya", "position": "GK", "team": "ARS"},
            {"player": "Flekken", "position": "GK", "team": "BRE"},
            {"player": "Hill", "position": "DEF", "team": "BOU"},
            {"player": "Konsa", "position": "DEF", "team": "AVL"},
            {"player": "Bassey", "position": "DEF", "team": "FUL"},
            {"player": "Gabriel", "position": "DEF", "team": "ARS"},
            {"player": "Walker-Peters", "position": "DEF", "team": "WHU"},
            {"player": "Scott", "position": "MID", "team": "BOU"},
            {"player": "Gross", "position": "MID", "team": "BHA"},
            {"player": "Saka", "position": "MID", "team": "ARS"},
            {"player": "Gibbs-White", "position": "MID", "team": "NFO"},
            {"player": "Mbeumo", "position": "MID", "team": "BRE"},
            {"player": "Watkins", "position": "FWD", "team": "AVL"},
            {"player": "Beto", "position": "FWD", "team": "EVE"},
            {"player": "Wilson", "position": "FWD", "team": "NEW"},
        ],
    }
    recs_content = """\
## Draft

### Waiver Recommendations

| Priority | Drop | Claim | Position | Fixture Run | Rationale |
|----------|------|-------|----------|-------------|-----------|
| 1 | Watkins (AVL) | J Pedro (CHE) | FWD | A WOL | Good. |
"""
    recs, w, s = _write_fixtures(tmp_path, recs_content, waivers=waivers, squad=squad)
    _run(str(recs), str(w), str(s))
    data = _parse(capsys)
    assert data["ok"] is True
    assert data["flags"] == []


def test_drop_not_resolvable_warns_and_skips_cross_pos(tmp_path, capsys):
    """Drop name not in squad → warning drop-not-resolvable; cross-pos check skipped for that row."""
    recs_content = """\
## Draft

### Waiver Recommendations

| Priority | Drop | Claim | Position | Fixture Run | Rationale |
|----------|------|-------|----------|-------------|-----------|
| 1 | XYZ Nonexistent (ABC) | Lacroix (CRY) | DEF | A LIV | Bad. |
"""
    recs, w, s = _write_fixtures(tmp_path, recs_content)
    _run(str(recs), str(w), str(s))
    data = _parse(capsys)
    # drop-not-resolvable is a warning, not a flag; no cross-pos flag raised
    assert not any(f["type"] == "cross-position-claim" for f in data["flags"])
    assert any(w["type"] == "drop-not-resolvable" for w in data["warnings"])


# ---- Structural warnings (section / table missing) -------------------------


def test_no_draft_section_warns_ok(tmp_path, capsys):
    """File with no ## Draft section → warning, ok: true."""
    recs_content = """\
## Classic

Some classic content only.
"""
    recs, w, s = _write_fixtures(tmp_path, recs_content)
    _run(str(recs), str(w), str(s))
    data = _parse(capsys)
    assert data["ok"] is True
    assert any(w["type"] == "draft-section-not-found" for w in data["warnings"])


def test_draft_section_no_waiver_table_warns(tmp_path, capsys):
    """## Draft section present but no waiver table → warning waiver-table-not-found."""
    recs_content = """\
## Draft

No waiver table here, just prose.
"""
    recs, w, s = _write_fixtures(tmp_path, recs_content)
    _run(str(recs), str(w), str(s))
    data = _parse(capsys)
    assert data["ok"] is True
    assert any(w["type"] == "waiver-table-not-found" for w in data["warnings"])


def test_draft_league_heading_variant(tmp_path, capsys):
    """## Draft League heading is parsed correctly."""
    recs_content = """\
## Draft League

### Waiver Recommendations

| Priority | Drop | Claim | Position | Fixture Run | Rationale |
|----------|------|-------|----------|-------------|-----------|
| 1 | Hill (BOU) | Lacroix (CRY) | DEF | A LIV | Good. |
| 2 | - | - | - | - | No moves. |
"""
    recs, w, s = _write_fixtures(tmp_path, recs_content)
    _run(str(recs), str(w), str(s))
    data = _parse(capsys)
    assert data["ok"] is True
    assert not any(w["type"] == "draft-section-not-found" for w in data["warnings"])


# ---- 6-column schema (GW34 format) ------------------------------------------


def test_six_column_schema_parsed_by_header_name(tmp_path, capsys):
    """GW34-style 6-column table (Priority|Drop|Claim|Position|Fixture Run|Rationale) parses correctly."""
    recs_content = """\
## Draft

### Waiver Recommendations

| Priority | Drop | Claim | Position | Fixture Run | Rationale |
|----------|------|-------|----------|-------------|-----------|
| 1 | Hill (BOU) | Lacroix (CRY) | DEF | A LIV / H MUN | Straight upgrade. |
| 2 | Scott (BOU) | E.Le Fée (SUN) | MID | H NFO | Blanker to starter. |
| 3 | (depth) | Canvot (CRY) | DEF | A LIV | Depth only. |
| 4 | - | - | - | - | No further moves. |
"""
    recs, w, s = _write_fixtures(tmp_path, recs_content)
    _run(str(recs), str(w), str(s))
    data = _parse(capsys)
    assert data["ok"] is True
    assert data["flags"] == []


# ---- Shape-check guard ------------------------------------------------------


def test_squad_grid_missing_position_key_warns_no_cross_pos_flags(tmp_path, capsys):
    """squad-grid rows missing 'position' → squad-grid-shape-unknown warning; no cross-pos flags; pool flags still work."""
    squad_no_pos = {
        "command": "fpl squad grid",
        "data": [
            {"player": "Hill", "team": "BOU"},
            {"player": "Konsa", "team": "AVL"},
        ],
    }
    recs_content = """\
## Draft

### Waiver Recommendations

| Priority | Drop | Claim | Position | Fixture Run | Rationale |
|----------|------|-------|----------|-------------|-----------|
| 1 | João Pedro (CHE) | Pedro Porro (TOT) | DEF | A WOL | Would be cross-pos. |
| 2 | Hill (BOU) | Thiago (BRE) | MID | A MUN | Not in pool. |
"""
    recs, w, s = _write_fixtures(tmp_path, recs_content, squad=squad_no_pos)
    _run(str(recs), str(w), str(s))
    data = _parse(capsys)
    # shape-check fires
    assert any(w["type"] == "squad-grid-shape-unknown" for w in data["warnings"])
    # cross-pos suppressed (no squad data)
    assert not any(f["type"] == "cross-position-claim" for f in data["flags"])
    # pool check still works: Thiago is not in pool
    assert any(f["type"] == "waiver-not-in-pool" for f in data["flags"])


# ---- Exit code and output contract ------------------------------------------


def test_always_exits_zero_even_with_blocking_flags(tmp_path, capsys):
    """Script exits 0 regardless of flag type; orchestrator decides blocking."""
    recs_content = """\
## Draft

### Waiver Recommendations

| Priority | Drop | Claim | Position | Fixture Run | Rationale |
|----------|------|-------|----------|-------------|-----------|
| 1 | João Pedro (CHE) | Pedro Porro (TOT) | DEF | A WOL | Cross-pos. |
"""
    recs, w, s = _write_fixtures(tmp_path, recs_content)
    # _run should not raise SystemExit
    _run(str(recs), str(w), str(s))
    data = _parse(capsys)
    assert any(f["type"] == "cross-position-claim" for f in data["flags"])


def test_output_schema(tmp_path, capsys):
    """JSON output always has ok, flags, warnings keys."""
    recs, w, s = _write_fixtures(tmp_path, _CLEAN_RECS)
    _run(str(recs), str(w), str(s))
    data = _parse(capsys)
    assert set(data.keys()) == {"ok", "flags", "warnings"}


# ---- Integration: subprocess ------------------------------------------------


def test_integration_subprocess_clean(tmp_path):
    """Run script as subprocess with clean fixtures; assert JSON stdout, exit 0."""
    import shutil

    recs = tmp_path / "recs.md"
    shutil.copy(FIXTURE_DIR / "recs_clean.md", recs)
    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPT_PATH),
            "--recommendations-file", str(recs),
            "--waivers-json", str(FIXTURE_DIR / "waivers.json"),
            "--squad-grid-json", str(FIXTURE_DIR / "squad_grid.json"),
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0
    data = json.loads(result.stdout)
    assert data["ok"] is True
    assert data["flags"] == []


def test_integration_subprocess_cross_pos(tmp_path):
    """Cross-position row → exit 0, ok: false, cross-position-claim flag in stdout."""
    recs = tmp_path / "recs.md"
    recs.write_text(
        "## Draft\n\n### Waiver Recommendations\n\n"
        "| Priority | Drop | Claim | Position | Fixture Run | Rationale |\n"
        "|----------|------|-------|----------|-------------|----------|\n"
        "| 1 | João Pedro (CHE) | Pedro Porro (TOT) | DEF | A WOL | Cross-pos. |\n",
        encoding="utf-8",
    )
    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPT_PATH),
            "--recommendations-file", str(recs),
            "--waivers-json", str(FIXTURE_DIR / "waivers.json"),
            "--squad-grid-json", str(FIXTURE_DIR / "squad_grid.json"),
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0
    data = json.loads(result.stdout)
    assert data["ok"] is False
    assert any(f["type"] == "cross-position-claim" for f in data["flags"])


def test_integration_subprocess_pool_miss(tmp_path):
    """Pool miss → exit 0, ok: false, waiver-not-in-pool flag."""
    recs = tmp_path / "recs.md"
    recs.write_text(
        "## Draft\n\n### Waiver Recommendations\n\n"
        "| Priority | Drop | Claim | Position | Fixture Run | Rationale |\n"
        "|----------|------|-------|----------|-------------|----------|\n"
        "| 1 | Hill (BOU) | Thiago (BRE) | MID | A MUN | Not in pool. |\n",
        encoding="utf-8",
    )
    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPT_PATH),
            "--recommendations-file", str(recs),
            "--waivers-json", str(FIXTURE_DIR / "waivers.json"),
            "--squad-grid-json", str(FIXTURE_DIR / "squad_grid.json"),
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0
    data = json.loads(result.stdout)
    assert data["ok"] is False
    assert any(f["type"] == "waiver-not-in-pool" for f in data["flags"])


# ---- Waivers shape guard ----------------------------------------------------


def test_waivers_missing_top_targets_warns_and_suppresses_pool_flags(tmp_path, capsys):
    """waivers JSON missing data.top_targets → waivers-json-shape-unknown warning;
    claim-in-pool check is suppressed so rows don't flood with false waiver-not-in-pool flags."""
    waivers_bad = {"command": "fpl waivers", "data": {}}  # no top_targets
    recs_content = """\
## Draft

### Waiver Recommendations

| Priority | Drop | Claim | Position | Fixture Run | Rationale |
|----------|------|-------|----------|-------------|-----------|
| 1 | Hill (BOU) | Lacroix (CRY) | DEF | A LIV | Real claim. |
| 2 | Scott (BOU) | E.Le Fée (SUN) | MID | H NFO | Real claim. |
"""
    recs, w, s = _write_fixtures(tmp_path, recs_content, waivers=waivers_bad)
    _run(str(recs), str(w), str(s))
    data = _parse(capsys)
    assert any(w["type"] == "waivers-json-shape-unknown" for w in data["warnings"])
    # no flood of pool-miss flags when shape is bad
    assert not any(f["type"] == "waiver-not-in-pool" for f in data["flags"])


def test_waivers_top_targets_wrong_type_warns(tmp_path, capsys):
    """top_targets not a list → shape warning, pool check suppressed."""
    waivers_bad = {"command": "fpl waivers", "data": {"top_targets": "nope"}}
    recs, w, s = _write_fixtures(tmp_path, _CLEAN_RECS, waivers=waivers_bad)
    _run(str(recs), str(w), str(s))
    data = _parse(capsys)
    assert any(w["type"] == "waivers-json-shape-unknown" for w in data["warnings"])


# ---- Full unowned pool ------------------------------------------------------

_STASH_RECS = """\
## Draft

### Waiver Recommendations

| Priority | Drop | Claim | Position | Fixture Run | Rationale |
|----------|------|-------|----------|-------------|-----------|
| 1 | Beto (EVE) | Havertz (ARS) | FWD | H BUR / A NEW | Stash the returnee. |
"""


def test_pool_only_claim_resolves_without_a_pool_miss(tmp_path, capsys):
    """A flagged returnee lives in `pool` and never in `top_targets` — R14."""
    recs, w, s = _write_fixtures(tmp_path, _STASH_RECS, waivers=_WAIVERS_WITH_POOL)
    _run(str(recs), str(w), str(s))
    data = _parse(capsys)
    assert data["ok"] is True
    assert data["flags"] == []
    assert data["warnings"] == []


def test_returning_soon_section_is_not_ingested_as_waiver_rows(tmp_path, capsys):
    """The template's Returning Soon table sits beside the waiver table under
    `## Draft`. Its rows name players who are not claims — a returnee still on
    watch, a rival-owned one — so ingesting them would flag phantom pool misses."""
    recs_content = """\
## Draft

### Waiver Recommendations

| Priority | Drop | Claim | Position | Fixture Run | Rationale |
|----------|------|-------|----------|-------------|-----------|
| 1 | Beto (EVE) | Havertz (ARS) | FWD | H BUR / A NEW | Stash the returnee. |

### Returning Soon

| Player | Team | Pos | Quality | Expected Return | Chance | Change | Verdict |
|--------|------|-----|---------|-----------------|--------|--------|---------|
| Havertz | ARS | FWD | history (stash) | 2026-09-13 | 25 | Date set | Stash |
| Chalobah | CHE | DEF | history | Unknown | 0 | New | Watch |
"""
    recs, w, s = _write_fixtures(tmp_path, recs_content, waivers=_WAIVERS_WITH_POOL)
    _run(str(recs), str(w), str(s))
    data = _parse(capsys)
    assert data["ok"] is True
    assert data["flags"] == []
    assert data["warnings"] == []


def test_claim_in_neither_pool_nor_top_targets_still_flagged(tmp_path, capsys):
    """Widening the pool must not blunt the check itself."""
    recs_content = """\
## Draft

### Waiver Recommendations

| Priority | Drop | Claim | Position | Fixture Run | Rationale |
|----------|------|-------|----------|-------------|-----------|
| 1 | Beto (EVE) | Nobody (BRE) | FWD | H BUR | Not rosterable. |
"""
    recs, w, s = _write_fixtures(tmp_path, recs_content, waivers=_WAIVERS_WITH_POOL)
    _run(str(recs), str(w), str(s))
    data = _parse(capsys)
    assert data["ok"] is False
    assert [f["type"] for f in data["flags"]] == ["waiver-not-in-pool"]


def test_waivers_json_without_pool_falls_back_to_top_targets(tmp_path, capsys):
    """An older cached waivers JSON validates instead of flagging every claim."""
    assert "pool" not in _WAIVERS_BASE["data"]
    recs, w, s = _write_fixtures(tmp_path, _CLEAN_RECS)
    _run(str(recs), str(w), str(s))
    data = _parse(capsys)
    assert data["ok"] is True
    assert data["flags"] == []


def test_pool_only_waivers_json_passes_the_shape_check(tmp_path, capsys):
    """`pool` alone satisfies the shape guard — `top_targets` is not required."""
    waivers = {
        "command": "fpl waivers",
        "data": {"pool": _WAIVERS_WITH_POOL["data"]["pool"]},
    }
    recs, w, s = _write_fixtures(tmp_path, _STASH_RECS, waivers=waivers)
    _run(str(recs), str(w), str(s))
    data = _parse(capsys)
    assert data["warnings"] == []
    assert data["flags"] == []


def test_malformed_pool_field_warns_and_suppresses_pool_flags(tmp_path, capsys):
    """A `pool` that is not a list is drift, not a licence to flag every claim."""
    waivers = {
        "command": "fpl waivers",
        "data": {"pool": "nope", "top_targets": _WAIVERS_BASE["data"]["top_targets"]},
    }
    recs, w, s = _write_fixtures(tmp_path, _STASH_RECS, waivers=waivers)
    _run(str(recs), str(w), str(s))
    data = _parse(capsys)
    assert any(x["type"] == "waivers-json-shape-unknown" for x in data["warnings"])
    assert not any(f["type"] == "waiver-not-in-pool" for f in data["flags"])


def test_empty_pool_falls_back_to_top_targets(tmp_path, capsys):
    """An empty roster beside a populated ranked list is inconsistent output;
    fall back rather than reporting every claim as a miss."""
    waivers = {
        "command": "fpl waivers",
        "data": {"pool": [], "top_targets": _WAIVERS_BASE["data"]["top_targets"]},
    }
    recs, w, s = _write_fixtures(tmp_path, _CLEAN_RECS, waivers=waivers)
    _run(str(recs), str(w), str(s))
    data = _parse(capsys)
    assert not any(f["type"] == "waiver-not-in-pool" for f in data["flags"])


def test_cross_position_swap_still_blocks_against_the_pool(tmp_path, capsys):
    """R11: position-for-position is unchanged by this widening."""
    recs_content = """\
## Draft

### Waiver Recommendations

| Priority | Drop | Claim | Position | Fixture Run | Rationale |
|----------|------|-------|----------|-------------|-----------|
| 1 | Hill (BOU) | Havertz (ARS) | FWD | H BUR | DEF out, FWD in. |
"""
    recs, w, s = _write_fixtures(tmp_path, recs_content, waivers=_WAIVERS_WITH_POOL)
    _run(str(recs), str(w), str(s))
    data = _parse(capsys)
    assert data["ok"] is False
    assert [f["type"] for f in data["flags"]] == ["cross-position-claim"]


# ---- Drop ambiguous last-token match ---------------------------------------


def test_drop_ambiguous_last_token_across_positions_warns(tmp_path, capsys):
    """Two squad players share a last-token at different positions → drop-ambiguous-match
    warning and cross-position check is skipped (no false flag)."""
    waivers = {
        "command": "fpl waivers",
        "data": {
            "top_targets": [
                {"player_name": "Lacroix", "position": "DEF", "team_short": "CRY"},
            ]
        },
    }
    squad = {
        "command": "fpl squad grid",
        "data": [
            {"player": "Raya", "position": "GK", "team": "ARS"},
            {"player": "Flekken", "position": "GK", "team": "BRE"},
            {"player": "Neco Williams", "position": "DEF", "team": "NFO"},
            {"player": "Konsa", "position": "DEF", "team": "AVL"},
            {"player": "Bassey", "position": "DEF", "team": "FUL"},
            {"player": "Gabriel", "position": "DEF", "team": "ARS"},
            {"player": "Walker-Peters", "position": "DEF", "team": "WHU"},
            {"player": "Scott", "position": "MID", "team": "BOU"},
            {"player": "Gross", "position": "MID", "team": "BHA"},
            {"player": "Saka", "position": "MID", "team": "ARS"},
            {"player": "Gibbs-White", "position": "MID", "team": "NFO"},
            {"player": "Mbeumo", "position": "MID", "team": "BRE"},
            {"player": "Harry Wilson", "position": "MID", "team": "FUL"},
            {"player": "Callum Wilson", "position": "FWD", "team": "NEW"},
            {"player": "Beto", "position": "FWD", "team": "EVE"},
        ],
    }
    # Drop "Wilson" is ambiguous: Harry Wilson (MID) and Callum Wilson (FWD)
    recs_content = """\
## Draft

### Waiver Recommendations

| Priority | Drop | Claim | Position | Fixture Run | Rationale |
|----------|------|-------|----------|-------------|-----------|
| 1 | Wilson (FUL) | Lacroix (CRY) | DEF | A LIV | Ambiguous drop. |
"""
    recs, w, s = _write_fixtures(tmp_path, recs_content, waivers=waivers, squad=squad)
    _run(str(recs), str(w), str(s))
    data = _parse(capsys)
    assert any(w["type"] == "drop-ambiguous-match" for w in data["warnings"])
    # cross-position check was skipped — no cross-position-claim flag raised
    assert not any(f["type"] == "cross-position-claim" for f in data["flags"])


# ---- Argparse contract ------------------------------------------------------


def test_missing_required_arg_exits_nonzero():
    """argparse rejects invocation missing a required arg and exits non-zero with usage on stderr."""
    result = subprocess.run(
        [sys.executable, str(SCRIPT_PATH), "--waivers-json", "/tmp/x.json"],
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert "required" in result.stderr.lower()


def test_three_stacked_tables_only_waiver_table_parsed(tmp_path, capsys):
    """Regression: gw35 had Waiver Recommendations + Starting XI + Bench tables stacked
    in the Draft section. The validator previously parsed all three as one table,
    keyed by the waiver headers, and emitted false-positive flags on the XI/Bench
    rows. Scope is now bounded to ### Waiver Recommendations.
    """
    recs_content = """\
## Draft League

### Waiver Recommendations

| Priority | Drop | Claim | Position | Outlook | This GW | Fixture Run | Rationale |
|----------|------|-------|----------|---------|---------|-------------|-----------|
| 1 | Hill (BOU) | Lacroix (CRY) | DEF | +8 | +14 | A LIV / H MUN | Straight upgrade. |
| 2 | Scott (BOU) | E.Le Fée (SUN) | MID | +6 | +16 | H NFO / A WOL | Blanker to starter. |

**Top claim:** Hill → Lacroix.

### Starting XI

| Pos | Player | Score | Opponent (pFDR) | Form | Rationale |
|-----|--------|-------|-----------------|------|-----------|
| GK | Flekken | 34 | BUR (2.5) | 4.0 | CS fixture |
| DEF | Lacroix | 49 | CRY (5.0) | 5.7 | New in (waiver) |
| MID | Scott | 61 | CRY (2.5) | 5.7 | New in (waiver) |
| FWD | João Pedro | 35 | NFO (3.0) | 0.7 | Hold |

#### Bench

| Bench Slot | Player | Score | Rationale |
|------------|--------|-------|-----------|
| GK | Raya | 28 | Backup |
| 1st sub | Watkins | 30 | FWD cover |
"""
    recs, w, s = _write_fixtures(tmp_path, recs_content)
    _run(str(recs), str(w), str(s))
    data = _parse(capsys)
    assert data["ok"] is True
    assert data["flags"] == []
    assert data["warnings"] == []


def test_nested_subheading_table_inside_waivers_does_not_leak_in(tmp_path, capsys):
    """A #### sub-heading nested inside ### Waiver Recommendations is drift, not
    more waiver rows -- its table must not be picked up as the live waiver
    table, even though it's a heading one level deeper than the boundary."""
    recs_content = """\
## Draft League

### Waiver Recommendations

No live waivers this week.

#### Historical Priority

| Priority | Drop | Claim | Position |
|----------|------|-------|----------|
| 1 | Hill (BOU) | Rogue Claim (YYY) | MID |

### Starting XI

| Pos | Player |
|-----|--------|
| GK | Flekken |
"""
    recs, w, s = _write_fixtures(tmp_path, recs_content)
    _run(str(recs), str(w), str(s))
    data = _parse(capsys)
    assert data["ok"] is True
    assert data["flags"] == []
    assert any(w["type"] == "waiver-table-not-found" for w in data["warnings"])


def test_no_waiver_subheading_falls_back_to_full_section(tmp_path, capsys):
    """If ### Waiver Recommendations is absent (older report layout), parsing
    should fall back to the full ## Draft section and still locate the table.
    """
    recs_content = """\
## Draft

| Priority | Drop | Claim | Position | Fixture Run | Rationale |
|----------|------|-------|----------|-------------|-----------|
| 1 | Hill (BOU) | Lacroix (CRY) | DEF | A LIV | Upgrade. |
"""
    recs, w, s = _write_fixtures(tmp_path, recs_content)
    _run(str(recs), str(w), str(s))
    data = _parse(capsys)
    assert data["ok"] is True
    assert data["flags"] == []


def test_help_flag_exits_zero_and_prints_usage():
    """--help exits 0 and prints a usage line (no reference to the dropped deferred-flags epilog)."""
    result = subprocess.run(
        [sys.executable, str(SCRIPT_PATH), "--help"],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0
    assert "usage:" in result.stdout.lower()
    assert "--check-drop-in-squad" not in result.stdout


# ---- Heading drift tolerance (issue #65) ------------------------------------


@pytest.mark.parametrize(
    "draft_heading",
    [
        "## Draft League (Provisional)",
        "## Draft League — provisional",
        "## draft league",
        "## Draft (Provisional)",
        "## **Draft League**",
    ],
)
def test_qualified_draft_heading_still_locates_the_section(
    tmp_path, capsys, draft_heading
):
    """A qualifier or case variant on the Draft heading must not silently skip
    the whole section — the same failure mode as issue #63, one script over."""
    recs, w, s = _write_fixtures(
        tmp_path, _CLEAN_RECS.replace("## Draft", draft_heading, 1)
    )
    _run(str(recs), str(w), str(s))
    data = _parse(capsys)
    assert not any(x["type"] == "draft-section-not-found" for x in data["warnings"])
    assert data["ok"] is True
    assert data["flags"] == []


def test_qualified_waiver_subheading_still_bounds_the_table(tmp_path, capsys):
    """A qualifier on ### Waiver Recommendations must keep the scope narrowed to
    the waiver table rather than falling back to the whole Draft section."""
    recs_content = """\
## Draft League

### Waiver Recommendations (GW34)

| Priority | Drop | Claim | Position | Fixture Run | Rationale |
|----------|------|-------|----------|-------------|-----------|
| 1 | Hill (BOU) | Lacroix (CRY) | DEF | A LIV | Straight upgrade. |

### Starting XI

| Pos | Player | Score | Rationale |
|-----|--------|-------|-----------|
| GK | Flekken | 34 | CS fixture |
| DEF | Lacroix | 49 | New in |
"""
    recs, w, s = _write_fixtures(tmp_path, recs_content)
    _run(str(recs), str(w), str(s))
    data = _parse(capsys)
    assert data["ok"] is True
    assert data["flags"] == []
    assert data["warnings"] == []


def test_draft_rankings_heading_is_not_the_draft_section(tmp_path, capsys):
    """'## Draft Rankings' shares a prefix but is a different heading."""
    recs_content = """\
## Draft Rankings

| Priority | Drop | Claim | Position | Fixture Run | Rationale |
|----------|------|-------|----------|-------------|-----------|
| 1 | Hill (BOU) | Nobody (XXX) | DEF | A LIV | Decoy. |
"""
    recs, w, s = _write_fixtures(tmp_path, recs_content)
    _run(str(recs), str(w), str(s))
    data = _parse(capsys)
    assert any(x["type"] == "draft-section-not-found" for x in data["warnings"])
    assert data["flags"] == []


# ---------------------------------------------------------------------------
# Heading constants stay in sync with the output template
# ---------------------------------------------------------------------------
#
# _HEADING_DRAFT / _HEADING_WAIVERS are a second source of truth for heading
# text that also lives in gw-prep's output template. Nothing else enforces
# the two stay in sync, so a template rename would otherwise merge cleanly
# and silently desync the validator from the template it's meant to validate
# against -- this test catches that by matching the constants against the
# template's real, current content.

GW_PREP_TEMPLATE_PATH = (
    Path(__file__).parent.parent / ".agents/skills/gw-prep/references/output-template.md"
)


def test_draft_and_waiver_headings_match_the_gw_prep_template():
    """The template illustrates its whole document shape inside one wrapping
    ```markdown fence (a real recommendations file has no such fence), so the
    check matches heading text and ordering directly rather than through the
    fence-aware find_section, which correctly treats that fenced content as
    non-headings for real parsing."""
    lines = GW_PREP_TEMPLATE_PATH.read_text(encoding="utf-8").split("\n")
    draft_idx = next(
        (i for i, line in enumerate(lines) if _mod._HEADING_DRAFT.matches(line)), None
    )
    assert draft_idx is not None, "template must have a '## Draft League' heading"
    waiver_idx = next(
        (
            i
            for i, line in enumerate(lines)
            if i > draft_idx and _mod._HEADING_WAIVERS.matches(line)
        ),
        None,
    )
    assert waiver_idx is not None, (
        "'### Waiver Recommendations' not found after '## Draft League' in the "
        "gw-prep output template -- _HEADING_WAIVERS has drifted from the template"
    )


def test_fenced_draft_heading_is_ignored(tmp_path, capsys):
    """A '## Draft' inside a fenced example block must not open the section."""
    recs_content = """\
## Notes

```markdown
## Draft

| Priority | Drop | Claim | Position | Fixture Run | Rationale |
|----------|------|-------|----------|-------------|-----------|
| 1 | Hill (BOU) | Nobody (XXX) | DEF | A LIV | Template example. |
```

## Draft

### Waiver Recommendations

| Priority | Drop | Claim | Position | Fixture Run | Rationale |
|----------|------|-------|----------|-------------|-----------|
| 1 | Hill (BOU) | Lacroix (CRY) | DEF | A LIV | Straight upgrade. |
"""
    recs, w, s = _write_fixtures(tmp_path, recs_content)
    _run(str(recs), str(w), str(s))
    data = _parse(capsys)
    assert data["ok"] is True
    assert data["flags"] == []
