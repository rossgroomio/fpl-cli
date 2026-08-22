#!/usr/bin/env python3
"""Validate Draft waiver recommendations against the live waiver pool and squad-grid.

Reads a gw{N}-recommendations.md file, locates the ## Draft (or ## Draft League)
section, parses the waiver table, and cross-checks each row against:
  - waivers JSON: claim must be present in the pool (waiver-not-in-pool)
  - squad-grid JSON: drop position must match claim position (cross-position-claim)

Emits JSON to stdout: {"ok": bool, "flags": [...], "warnings": [...]}
Exit code is always 0; the orchestrator (Phase D1) interprets flag types for posture.

Usage:
    python3 validate_draft_waivers.py \\
        --recommendations-file path/to/gw34-recommendations.md \\
        --waivers-json /tmp/waivers.json \\
        --squad-grid-json /tmp/squad-grid.json
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import unicodedata
from typing import TypedDict

from _md_sections import HeadingMatcher, find_section, section_body

# ---------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------


class Flag(TypedDict, total=False):
    type: str
    row: int
    claim: str
    drop: str
    drop_position: str
    claim_position: str


class Warning(TypedDict, total=False):
    type: str
    name: str


class ValidatorResult(TypedDict):
    ok: bool
    flags: list[Flag]
    warnings: list[Warning]


class PoolEntry(TypedDict):
    player_name: str
    position: str
    team_short: str


class SquadEntry(TypedDict):
    player: str
    position: str
    team: str


# ---------------------------------------------------------------------------
# Normalisation
# ---------------------------------------------------------------------------

_APOSTROPHE_RE = re.compile(r"[‘’ʼʹ`]")
_HYPHEN_RE = re.compile(r"[–—‒‐‑]")
_TEAM_SUFFIX_RE = re.compile(r"\s*\([A-Z]{2,4}\)\s*$")
_MARKDOWN_WRAP_RE = re.compile(r"^[\*_`]+|[\*_`]+$")
_WHITESPACE_RE = re.compile(r"\s+")


def normalise(name: str) -> str:
    """Return a canonical key for name matching."""
    s = name.strip()
    # strip markdown bold/italic/code wrapping
    s = _MARKDOWN_WRAP_RE.sub("", s).strip()
    # strip trailing team suffix like "(CHE)" or "(TOT)"
    s = _TEAM_SUFFIX_RE.sub("", s).strip()
    # NFKD decompose then drop combining marks
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.category(c).startswith("M"))
    # fold apostrophe variants
    s = _APOSTROPHE_RE.sub("'", s)
    # fold hyphen/dash variants
    s = _HYPHEN_RE.sub("-", s)
    # fold dots to space (handles J.Pedro vs J Pedro, B.Fernandes vs B Fernandes)
    s = s.replace(".", " ")
    # casefold
    s = s.casefold()
    # collapse whitespace
    s = _WHITESPACE_RE.sub(" ", s).strip()
    return s


_PLACEHOLDER_VALUES = frozenset({"-", "—", "–", "", "hold", "(depth)", "tbd", "n/a"})


def _is_placeholder(raw: str) -> bool:
    key = normalise(raw)
    return key in _PLACEHOLDER_VALUES or (key.startswith("(") and key.endswith(")"))


# ---------------------------------------------------------------------------
# Pool / squad builders
# ---------------------------------------------------------------------------


def _shape_check_squad(data: dict) -> bool:
    """Return True if squad-grid JSON has the expected shape."""
    try:
        rows = data["data"]
        if not rows:
            return True
        sample = rows[0]
        return "player" in sample and "position" in sample
    except (KeyError, IndexError, TypeError):
        return False


def _shape_check_waivers(data: dict) -> bool:
    """Return True if waivers JSON has the expected shape."""
    try:
        targets = data["data"]["top_targets"]
        if not isinstance(targets, list):
            return False
        if not targets:
            return True
        sample = targets[0]
        return "player_name" in sample and "position" in sample
    except (KeyError, TypeError):
        return False


def build_pool(waivers_data: dict) -> dict[tuple[str, str], PoolEntry]:
    """Build {(position, normalised_name) -> entry} from waivers JSON."""
    pool: dict[tuple[str, str], PoolEntry] = {}
    for entry in waivers_data.get("data", {}).get("top_targets", []):
        pos = entry.get("position", "")
        key = (pos, normalise(entry.get("player_name", "")))
        pool[key] = entry
    return pool


def build_squad(squad_data: dict) -> dict[tuple[str, str], SquadEntry]:
    """Build {(position, normalised_name) -> entry} from squad-grid JSON."""
    squad: dict[tuple[str, str], SquadEntry] = {}
    for entry in squad_data.get("data", []):
        pos = entry.get("position", "")
        key = (pos, normalise(entry.get("player", "")))
        squad[key] = entry
    return squad


# ---------------------------------------------------------------------------
# Section / table parsing
# ---------------------------------------------------------------------------

# Shared with extract_classic_squad.py: both scripts locate a markdown section
# by heading in sub-agent-authored files, so both tolerate the same heading
# drift ("## Draft League (Provisional)") through the same matcher rather than
# each hard-coding an exact-match regex that a qualifier silently defeats.
_HEADING_DRAFT = HeadingMatcher("## Draft League", aliases=("Draft",))
_HEADING_WAIVERS = HeadingMatcher("### Waiver Recommendations")


def locate_draft_section(lines: list[str]) -> tuple[int, int] | None:
    """Return (start, end) line indices for the ## Draft / ## Draft League section."""
    return find_section(lines, _HEADING_DRAFT)


def locate_waiver_subsection(section_lines: list[str]) -> list[str]:
    """Return the slice of section_lines bounded by ### Waiver Recommendations.

    Bounds end at the next heading of the same or shallower depth. Falls back to
    the full section if the subheading is absent (older report formats).
    """
    body = section_body(section_lines, _HEADING_WAIVERS)
    return section_lines if body is None else body


def parse_waiver_table(
    section_lines: list[str],
) -> tuple[list[str], list[dict[str, str]]] | None:
    """Parse the first markdown table in the section.

    Returns (headers, rows) where rows are dicts keyed by header name,
    or None if no table found.
    """
    headers: list[str] | None = None
    rows: list[dict[str, str]] = []

    lines_iter = list(section_lines)
    for idx, line in enumerate(lines_iter):
        stripped = line.strip()
        if not stripped.startswith("|"):
            # End table at first non-pipe line after rows started (e.g. blank line
            # before the next ### subsection's table).
            if rows:
                break
            continue
        if headers is None:
            # first pipe-delimited line is the header row
            parts = [p.strip() for p in stripped.strip("|").split("|")]
            headers = parts
            continue
        # skip separator row
        if re.match(r"^\|[-| ]+\|", stripped):
            continue
        # Detect a new header row stacked directly after the current table:
        # current line is pipe-delimited and the next line is a separator. Break
        # before consuming it as a data row.
        if rows and idx + 1 < len(lines_iter):
            next_stripped = lines_iter[idx + 1].strip()
            if re.match(r"^\|[-| ]+\|", next_stripped):
                break
        parts = [p.strip() for p in stripped.strip("|").split("|")]
        row: dict[str, str] = {}
        for i, h in enumerate(headers):
            row[h] = parts[i] if i < len(parts) else ""
        rows.append(row)

    if headers is None:
        return None
    return headers, rows


# ---------------------------------------------------------------------------
# Name resolution
# ---------------------------------------------------------------------------


def _resolve_in_pool(
    name: str, position: str, pool: dict[tuple[str, str], PoolEntry]
) -> PoolEntry | None:
    """Exact match then last-token fallback, both position-scoped."""
    norm = normalise(name)
    # exact
    if (position, norm) in pool:
        return pool[(position, norm)]
    # last-token fallback: check if norm equals last token of any pool entry at this pos
    for (pos, pool_norm), entry in pool.items():
        if pos != position:
            continue
        if pool_norm.split()[-1] == norm:
            return entry
        # also handle: pool entry is short, rec file has full name
        if norm.split()[-1] == pool_norm:
            return entry
    return None


def _resolve_in_squad(
    name: str, squad: dict[tuple[str, str], SquadEntry]
) -> list[SquadEntry]:
    """Return all squad entries matching name (exact, else last-token fallback).

    Exact matches short-circuit the fallback. Callers inspect the list length and
    the set of positions to decide whether the match is unambiguous.
    """
    norm = normalise(name)
    exact = [entry for (_, squad_norm), entry in squad.items() if squad_norm == norm]
    if exact:
        return exact
    return [
        entry
        for (_, squad_norm), entry in squad.items()
        if squad_norm.split()[-1] == norm or norm.split()[-1] == squad_norm
    ]


# ---------------------------------------------------------------------------
# Core validation
# ---------------------------------------------------------------------------


def validate_rows(
    rows: list[dict[str, str]],
    pool: dict[tuple[str, str], PoolEntry],
    squad: dict[tuple[str, str], SquadEntry],
    squad_shape_ok: bool,
    waivers_shape_ok: bool = True,
) -> tuple[list[Flag], list[Warning]]:
    flags: list[Flag] = []
    warnings: list[Warning] = []

    for i, row in enumerate(rows, start=1):
        drop_raw = row.get("Drop", "")
        claim_raw = row.get("Claim", "")
        position = row.get("Position", "").strip()

        if _is_placeholder(drop_raw) or _is_placeholder(claim_raw):
            continue

        # Resolve claim in pool (position-scoped). Skip when waivers shape is unknown —
        # an empty pool would otherwise mark every real claim as a false positive.
        claim_entry: PoolEntry | None = None
        if waivers_shape_ok:
            claim_entry = _resolve_in_pool(claim_raw, position, pool)
            if claim_entry is None:
                flags.append(
                    Flag(
                        type="waiver-not-in-pool",
                        row=i,
                        claim=claim_raw,
                    )
                )

        # Resolve drop in squad (all positions, to find actual position)
        if not squad_shape_ok:
            continue
        drop_candidates = _resolve_in_squad(drop_raw, squad)
        if not drop_candidates:
            warnings.append(Warning(type="drop-not-resolvable", name=drop_raw))
            continue
        drop_positions = {e["position"] for e in drop_candidates}
        if len(drop_positions) > 1:
            # Ambiguous last-token match across positions — can't determine drop_pos safely
            warnings.append(Warning(type="drop-ambiguous-match", name=drop_raw))
            continue
        drop_entry = drop_candidates[0]

        # Cross-position check
        claim_pos = claim_entry["position"] if claim_entry else position
        drop_pos = drop_entry["position"]
        if drop_pos != claim_pos:
            flags.append(
                Flag(
                    type="cross-position-claim",
                    row=i,
                    drop=drop_raw,
                    drop_position=drop_pos,
                    claim=claim_raw,
                    claim_position=claim_pos,
                )
            )

    return flags, warnings


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def _run(recommendations_file: str, waivers_json: str, squad_grid_json: str) -> None:
    flags: list[Flag] = []
    warnings: list[Warning] = []

    # Load waivers
    with open(waivers_json, encoding="utf-8") as f:
        waivers_data = json.load(f)

    # Load squad-grid
    with open(squad_grid_json, encoding="utf-8") as f:
        squad_data = json.load(f)

    # Shape checks
    squad_shape_ok = _shape_check_squad(squad_data)
    if not squad_shape_ok:
        warnings.append(Warning(type="squad-grid-shape-unknown"))

    waivers_shape_ok = _shape_check_waivers(waivers_data)
    if not waivers_shape_ok:
        warnings.append(Warning(type="waivers-json-shape-unknown"))

    pool = build_pool(waivers_data) if waivers_shape_ok else {}
    squad = build_squad(squad_data) if squad_shape_ok else {}

    # Load recommendations
    with open(recommendations_file, encoding="utf-8") as f:
        content = f.read()
    lines = content.split("\n")

    # Locate draft section
    section_range = locate_draft_section(lines)
    if section_range is None:
        warnings.append(Warning(type="draft-section-not-found"))
        result: ValidatorResult = {"ok": True, "flags": [], "warnings": warnings}
        json.dump(result, sys.stdout)
        return

    start, end = section_range
    section_lines = lines[start:end]

    # Narrow to the ### Waiver Recommendations subsection so we don't ingest
    # rows from the Starting XI / Bench tables that follow it.
    waiver_lines = locate_waiver_subsection(section_lines)

    # Parse waiver table
    table = parse_waiver_table(waiver_lines)
    if table is None:
        warnings.append(Warning(type="waiver-table-not-found"))
        result = {"ok": True, "flags": [], "warnings": warnings}
        json.dump(result, sys.stdout)
        return

    headers, rows = table
    if "Drop" not in headers or "Claim" not in headers:
        warnings.append(Warning(type="drop-claim-columns-not-found"))
        result = {"ok": True, "flags": [], "warnings": warnings}
        json.dump(result, sys.stdout)
        return

    # Validate rows
    row_flags, row_warnings = validate_rows(
        rows, pool, squad, squad_shape_ok, waivers_shape_ok
    )
    flags.extend(row_flags)
    warnings.extend(row_warnings)

    ok = len(flags) == 0
    result = {"ok": ok, "flags": flags, "warnings": warnings}
    json.dump(result, sys.stdout)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Validate Draft waiver recommendations against the waiver pool and squad.",
    )
    parser.add_argument(
        "--recommendations-file",
        required=True,
        metavar="PATH",
        dest="recommendations_file",
        help="Path to gw{N}-recommendations.md.",
    )
    parser.add_argument(
        "--waivers-json",
        required=True,
        metavar="PATH",
        dest="waivers_json",
        help="Path to fpl waivers --format json output.",
    )
    parser.add_argument(
        "--squad-grid-json",
        required=True,
        metavar="PATH",
        dest="squad_grid_json",
        help="Path to fpl squad grid --format json output (draft squad).",
    )
    args = parser.parse_args()
    _run(args.recommendations_file, args.waivers_json, args.squad_grid_json)


if __name__ == "__main__":
    main()
