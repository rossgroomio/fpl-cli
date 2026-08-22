#!/usr/bin/env python3
"""Extract the Classic Squad block from a squad-builder output file.

Reads a gw{N}-squad-builder.md file, locates the '## Classic Squad' section,
demotes all headings by one level (depth-aware), discards any following
'## Draft Rankings' section, and emits JSON on stdout.

Requires fpl-cli venv to be activated before running.

Usage:
    python3 extract_classic_squad.py --file path/to/gw32-squad-builder.md
    python3 extract_classic_squad.py --from-recommendations --file path/to/gw32-recommendations.md
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from typing import Literal, TypedDict

from _md_sections import (
    HeadingMatcher,
    fence_flags,
    find_section,
    has_heading,
    leaf_body,
    parse_heading,
)


class StructuralResult(TypedDict):
    sub_headings_present: dict[str, bool]
    starting_xi_rows: int
    bench_rows: int
    captain_named: bool
    vice_named: bool


class ArithmeticResult(TypedDict):
    budget_total_gbp_m: float | None
    budget_within_cap: bool
    team_exposure: dict[str, int]
    max_per_team_ok: bool
    player_count: int


class ValidationResult(TypedDict):
    structural: StructuralResult
    arithmetic: ArithmeticResult


class ExtractMetadata(TypedDict):
    heading_demoted: bool
    had_draft_rankings: bool
    source_path: str
    mode: Literal["extract"]


class FromRecommendationsMetadata(TypedDict):
    source_path: str
    block_extracted: bool
    mode: Literal["from-recommendations"]


# Headings this script locates. Sub-agents author these files from a
# verbatim-heading instruction but sometimes decorate the heading anyway, so
# every comparison goes through HeadingMatcher rather than a bespoke regex --
# see _md_sections for what counts as a tolerable decoration and what stays a
# different heading ("#### Bench Order" is not a qualified "#### Bench").
_HEADING_CLASSIC_SQUAD = HeadingMatcher("## Classic Squad")
_HEADING_DRAFT_RANKINGS = HeadingMatcher("## Draft Rankings")
_HEADING_EMBEDDED_CLASSIC_SQUAD = HeadingMatcher("### Classic Squad")
_HEADING_STARTING_XI = HeadingMatcher(
    "#### Starting XI", aliases=("XI", "Starting Eleven", "Starting 11")
)
_HEADING_BENCH = HeadingMatcher("#### Bench")
_HEADING_TEAM_EXPOSURE = HeadingMatcher("#### Team Exposure")

# Ordered as the sub-agent is told to emit them; drives sub_headings_present.
_SUB_HEADINGS: dict[str, HeadingMatcher] = {
    "Starting XI": _HEADING_STARTING_XI,
    "Bench": _HEADING_BENCH,
    "Budget": HeadingMatcher("#### Budget"),
    "Team Exposure": _HEADING_TEAM_EXPOSURE,
    "Key Decisions": HeadingMatcher("#### Key Decisions"),
    "Alternatives": HeadingMatcher("#### Alternatives"),
}

# Matches a heading's leading whitespace and `#` marker, so demotion can insert
# an extra `#` at the right position even when parse_heading recognised the
# heading through leading whitespace it tolerates.
_LEADING_HASHES_RE = re.compile(r"^(\s*)(#+)")


class ExtractPayload(TypedDict):
    block: str
    validation: None
    metadata: ExtractMetadata


class FromRecommendationsPayload(TypedDict):
    block: str
    validation: ValidationResult
    metadata: FromRecommendationsMetadata


def _run(file_path: str, from_recommendations: bool = False) -> None:
    try:
        with open(file_path, encoding="utf-8") as f:
            content = f.read()
    except FileNotFoundError:
        hint = (
            "If this is a recommendations file, re-run with --from-recommendations."
            if not from_recommendations
            else "If this is a squad-builder output file, re-run without --from-recommendations."
        )
        json.dump(
            {"error": True, "messages": [f"File not found: {file_path}. {hint}"]},
            sys.stdout,
            indent=2,
        )
        sys.exit(1)

    if from_recommendations:
        _run_from_recommendations(file_path, content)
    else:
        _run_extract(file_path, content)


def _run_extract(file_path: str, content: str) -> None:
    """Extract and demote the Classic Squad block from a squad-builder file."""
    lines = content.split("\n")

    section = find_section(lines, _HEADING_CLASSIC_SQUAD)

    if section is None:
        json.dump(
            {
                "error": True,
                "messages": [
                    "No '## Classic Squad' heading found in the file. "
                    "Ensure the file was produced by /squad-builder with classic mode."
                ],
            },
            sys.stdout,
            indent=2,
        )
        sys.exit(1)

    start_idx, end_idx = section

    # Check whether Draft Rankings follows anywhere after Classic Squad
    fenced = list(fence_flags(lines))
    had_draft_rankings = any(
        not fenced[i] and _HEADING_DRAFT_RANKINGS.matches(lines[i])
        for i in range(start_idx + 1, len(lines))
    )

    block_lines = list(lines[start_idx:end_idx])
    # A heading's start is always outside a fence (HeadingMatcher.matches() only
    # recognises unfenced lines), so the section boundary can't land mid-fence --
    # the whole-file fence state can be sliced rather than recomputed from
    # scratch over the extracted block.
    fenced_block = fenced[start_idx:end_idx]

    # Strip trailing blank lines
    while block_lines and not block_lines[-1].strip():
        block_lines.pop()
        fenced_block.pop()

    # Empty block guard (heading only, no body)
    if len(block_lines) <= 1:
        json.dump(
            {
                "error": True,
                "messages": [
                    "The '## Classic Squad' block is empty (heading present but no content). "
                    "Re-run /squad-builder to regenerate the output file."
                ],
            },
            sys.stdout,
            indent=2,
        )
        sys.exit(1)

    # H6 ceiling check — demotion would produce H7 (####### exceeds markdown spec).
    # Goes through parse_heading (tolerant of leading whitespace, like every
    # other heading comparison in this file) rather than a bespoke regex.
    for i, line in enumerate(block_lines):
        if fenced_block[i]:
            continue
        parsed = parse_heading(line)
        if parsed is not None and parsed[0] == 6:
            json.dump(
                {
                    "error": True,
                    "messages": [
                        "The Classic Squad block contains a heading at H6 depth (######). "
                        "Demotion would produce ####### which exceeds the markdown H6 ceiling. "
                        "Edit the source file to reduce heading depth before extracting."
                    ],
                },
                sys.stdout,
                indent=2,
            )
            sys.exit(1)

    # Demote headings one level (skip lines inside fenced code blocks)
    demoted: list[str] = []
    for i, line in enumerate(block_lines):
        if not fenced_block[i] and parse_heading(line) is not None:
            hashes = _LEADING_HASHES_RE.match(line)
            assert hashes is not None
            line = f"{hashes.group(1)}#{line[hashes.start(2):]}"
        demoted.append(line)

    block = "\n".join(demoted)

    payload: ExtractPayload = {
        "block": block,
        "validation": None,
        "metadata": {
            "heading_demoted": True,
            "had_draft_rankings": had_draft_rankings,
            "source_path": file_path,
            "mode": "extract",
        },
    }
    json.dump(payload, sys.stdout, indent=2)


def _run_from_recommendations(file_path: str, content: str) -> None:
    """Read-only mode: recover and validate the Classic Squad block from a recommendations file."""
    lines = content.split("\n")

    # Locate '### Classic Squad' (already demoted in recommendations file)
    section = find_section(lines, _HEADING_EMBEDDED_CLASSIC_SQUAD)

    if section is None:
        json.dump(
            {
                "error": True,
                "messages": [
                    "No '### Classic Squad' heading found in the recommendations file. "
                    "The Classic sub-agent may not have embedded the block correctly. "
                    "If this is a squad-builder output file, re-run without --from-recommendations."
                ],
            },
            sys.stdout,
            indent=2,
        )
        sys.exit(1)

    start_idx, end_idx = section
    block_lines = list(lines[start_idx:end_idx])

    # Strip trailing blank lines
    while block_lines and not block_lines[-1].strip():
        block_lines.pop()

    block = "\n".join(block_lines)

    validation = _validate_classic_squad_block(block)

    payload: FromRecommendationsPayload = {
        "block": block,
        "validation": validation,
        "metadata": {
            "source_path": file_path,
            "block_extracted": True,
            "mode": "from-recommendations",
        },
    }
    json.dump(payload, sys.stdout, indent=2)


def _validate_classic_squad_block(block: str) -> ValidationResult:
    """Compute structural and arithmetic validation for a ### Classic Squad block."""
    # Structural checks
    block_lines = block.split("\n")
    sub_headings_present = {
        name: has_heading(block_lines, matcher)
        for name, matcher in _SUB_HEADINGS.items()
    }

    starting_xi_rows = _count_table_rows_between(block, _HEADING_STARTING_XI)
    bench_rows = _count_table_rows_between(block, _HEADING_BENCH)

    captain_named = bool(re.search(r"\*\*Captain:\*\*\s*\S", block))
    vice_named = bool(re.search(r"\*\*Vice:\*\*\s*\S", block))

    # Arithmetic checks
    budget_total_gbp_m = _parse_budget_total(block)
    budget_within_cap = budget_total_gbp_m is not None and budget_total_gbp_m <= 100.0

    team_exposure = _parse_team_exposure(block)
    max_per_team_ok = bool(team_exposure) and all(v <= 3 for v in team_exposure.values())

    player_count = starting_xi_rows + bench_rows

    return {
        "structural": {
            "sub_headings_present": sub_headings_present,
            "starting_xi_rows": starting_xi_rows,
            "bench_rows": bench_rows,
            "captain_named": captain_named,
            "vice_named": vice_named,
        },
        "arithmetic": {
            "budget_total_gbp_m": budget_total_gbp_m,
            "budget_within_cap": budget_within_cap,
            "team_exposure": team_exposure,
            "max_per_team_ok": max_per_team_ok,
            "player_count": player_count,
        },
    }


_TABLE_SEPARATOR_RE = re.compile(r"^\|[-:| ]+\|")


def _lines_in_section(block: str, heading: str | HeadingMatcher) -> list[str]:
    """Return the body lines of `heading`'s section within `block`.

    `heading` is always one of the leaf sub-headings (a table, a short note),
    never a container for further sub-headings, so a nested heading found
    inside it is drift and must not be scanned as more of its data.
    """
    return leaf_body(block.split("\n"), heading) or []


def _count_table_rows_between(block: str, start_heading: str | HeadingMatcher) -> int:
    """Count markdown table data rows between start_heading and the next heading of same depth."""
    in_table = False
    row_count = 0

    for line in _lines_in_section(block, start_heading):
        if line.startswith("|"):
            if _TABLE_SEPARATOR_RE.match(line):
                pass
            elif not in_table:
                in_table = True
            else:
                row_count += 1
        elif in_table:
            in_table = False

    return row_count


def _parse_budget_total(block: str) -> float | None:
    """Parse the Total row from the Budget table. Returns float in £m or None on parse failure."""
    # Look for a row like: | **Total** | **15** | **£99.5m** | or | **Total** | **15** | **GBP99.5m** |
    match = re.search(
        r"\|\s*\*\*Total\*\*\s*\|[^|]*\|\s*\*\*[£GBP]*([\d.]+)m\*\*",
        block,
    )
    if match:
        try:
            return float(match.group(1))
        except ValueError:
            return None
    return None


def _tally_teams_from_squad_tables(block: str) -> dict[str, int]:
    """Tally team counts from the Team column in Starting XI and Bench tables."""
    tally: dict[str, int] = {}
    for section_heading in (_HEADING_STARTING_XI, _HEADING_BENCH):
        in_table = False
        team_col: int | None = None
        for line in _lines_in_section(block, section_heading):
            if line.startswith("|"):
                if _TABLE_SEPARATOR_RE.match(line):
                    pass  # separator
                elif not in_table:
                    headers = [h.strip().lower() for h in line.strip("|").split("|")]
                    team_col = next(
                        (i for i, h in enumerate(headers) if h == "team"), None
                    )
                    in_table = True
                else:
                    if team_col is not None:
                        parts = [p.strip() for p in line.strip("|").split("|")]
                        if len(parts) > team_col and parts[team_col]:
                            team = parts[team_col]
                            tally[team] = tally.get(team, 0) + 1
            elif in_table:
                in_table = False
    return tally


def _parse_team_exposure(block: str) -> dict[str, int]:
    """
    Parse team exposure counts from the Team Exposure table.
    Falls back to tallying the Team column in Starting XI and Bench tables
    when the dedicated table is absent or parses to an empty dict.
    """
    exposure: dict[str, int] = {}

    for line in _lines_in_section(block, _HEADING_TEAM_EXPOSURE):
        if line.startswith("|") and not _TABLE_SEPARATOR_RE.match(line):
            parts = [p.strip() for p in line.strip("|").split("|")]
            if len(parts) >= 2:
                team = parts[0].strip()
                try:
                    count = int(parts[1].strip().strip("*"))
                    team_clean = team.strip("*").strip()
                    if team_clean and team_clean.lower() not in ("team", "total"):
                        exposure[team_clean] = count
                except ValueError:
                    pass

    if not exposure:
        return _tally_teams_from_squad_tables(block)

    return exposure


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Extract the Classic Squad block from a squad-builder output file.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  python3 extract_classic_squad.py --file gw32-squad-builder.md\n"
            "  python3 extract_classic_squad.py --from-recommendations --file gw32-recommendations.md"
        ),
    )
    parser.add_argument(
        "--file",
        required=True,
        metavar="PATH",
        help="Path to the squad-builder output file (or recommendations file with --from-recommendations).",
    )
    parser.add_argument(
        "--from-recommendations",
        action="store_true",
        dest="from_recommendations",
        help=(
            "Read-only mode: recover and validate the ### Classic Squad block "
            "from a gw{N}-recommendations.md file instead of extracting from a squad-builder file."
        ),
    )
    args = parser.parse_args()
    _run(args.file, from_recommendations=args.from_recommendations)


if __name__ == "__main__":
    main()
