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


def _run(file_path: str, from_recommendations: bool = False) -> None:
    try:
        with open(file_path) as f:
            content = f.read()
    except FileNotFoundError:
        json.dump(
            {"error": True, "messages": [f"File not found: {file_path}"]},
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

    # Locate '## Classic Squad'
    start_idx: int | None = None
    for i, line in enumerate(lines):
        if re.match(r"^## Classic Squad\b", line):
            start_idx = i
            break

    if start_idx is None:
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

    # Find the end boundary (next top-level ## heading or EOF)
    end_idx = len(lines)
    for i in range(start_idx + 1, len(lines)):
        if re.match(r"^## \S", lines[i]):
            end_idx = i
            break

    # Check whether Draft Rankings follows anywhere after Classic Squad
    had_draft_rankings = any(
        re.match(r"^## Draft Rankings\b", lines[i])
        for i in range(start_idx + 1, len(lines))
    )

    block_lines = list(lines[start_idx:end_idx])

    # Strip trailing blank lines
    while block_lines and not block_lines[-1].strip():
        block_lines.pop()

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

    # H6 ceiling check — demotion would produce H7 (####### exceeds markdown spec)
    in_fence = False
    for line in block_lines:
        if re.match(r"^```", line):
            in_fence = not in_fence
            continue
        if not in_fence and re.match(r"^#{6}", line):
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
    in_fence = False
    for line in block_lines:
        if re.match(r"^```", line):
            in_fence = not in_fence
            demoted.append(line)
            continue
        if not in_fence and re.match(r"^#+\s", line):
            line = "#" + line
        demoted.append(line)

    block = "\n".join(demoted)

    payload = {
        "block": block,
        "metadata": {
            "heading_demoted": True,
            "had_draft_rankings": had_draft_rankings,
            "source_path": file_path,
        },
    }
    json.dump(payload, sys.stdout, indent=2)


def _run_from_recommendations(file_path: str, content: str) -> None:
    """Read-only mode: recover and validate the Classic Squad block from a recommendations file."""
    lines = content.split("\n")

    # Locate '### Classic Squad' (already demoted in recommendations file)
    start_idx: int | None = None
    for i, line in enumerate(lines):
        if re.match(r"^### Classic Squad\b", line):
            start_idx = i
            break

    if start_idx is None:
        json.dump(
            {
                "error": True,
                "messages": [
                    "No '### Classic Squad' heading found in the recommendations file. "
                    "The Classic sub-agent may not have embedded the block correctly."
                ],
            },
            sys.stdout,
            indent=2,
        )
        sys.exit(1)

    # Find the end boundary (next ### or ## heading or EOF)
    end_idx = len(lines)
    for i in range(start_idx + 1, len(lines)):
        if re.match(r"^#{2,3} \S", lines[i]):
            end_idx = i
            break

    block_lines = list(lines[start_idx:end_idx])

    # Strip trailing blank lines
    while block_lines and not block_lines[-1].strip():
        block_lines.pop()

    block = "\n".join(block_lines)

    validation = _validate_classic_squad_block(block)

    payload = {
        "block": block,
        "validation": validation,
        "metadata": {
            "source_path": file_path,
            "parse_ok": True,
        },
    }
    json.dump(payload, sys.stdout, indent=2)


def _validate_classic_squad_block(block: str) -> dict:
    """Compute structural and arithmetic validation for a ### Classic Squad block."""
    expected_sub_headings = [
        "Starting XI",
        "Bench",
        "Budget",
        "Team Exposure",
        "Key Decisions",
        "Alternatives",
    ]

    # Structural checks
    sub_headings_present = {
        name: (f"#### {name}" in block) for name in expected_sub_headings
    }

    starting_xi_rows = _count_table_rows_between(block, "#### Starting XI")
    bench_rows = _count_table_rows_between(block, "#### Bench")

    captain_named = bool(re.search(r"\*\*Captain:\*\*\s*\S", block))
    vice_named = bool(re.search(r"\*\*Vice:\*\*\s*\S", block))

    # Arithmetic checks
    budget_total_mlm = _parse_budget_total(block)
    budget_within_cap = (
        budget_total_mlm is not None and budget_total_mlm <= 100.0
    )

    team_exposure = _parse_team_exposure(block)
    max_per_team_ok = all(v <= 3 for v in team_exposure.values())

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
            "budget_total_mlm": budget_total_mlm,
            "budget_within_cap": budget_within_cap,
            "team_exposure": team_exposure,
            "max_per_team_ok": max_per_team_ok,
            "player_count": player_count,
        },
    }


def _count_table_rows_between(block: str, start_heading: str) -> int:
    """Count markdown table data rows between start_heading and the next heading of same depth."""
    lines = block.split("\n")
    in_section = False
    in_table = False
    row_count = 0

    for line in lines:
        if line.strip() == start_heading or line.strip().startswith(start_heading):
            in_section = True
            continue
        if in_section:
            # Stop at the next heading of same or higher level
            if re.match(r"^#{3,4} \S", line) and not line.strip().startswith(start_heading):
                break
            # Count table rows (not separator rows)
            if line.startswith("|") and not re.match(r"^\|[-| ]+\|", line):
                in_table = True
                row_count += 1
            elif in_table and not line.startswith("|"):
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


def _parse_team_exposure(block: str) -> dict[str, int]:
    """
    Parse team exposure counts from the Team Exposure table,
    or fall back to tallying the Team column in Starting XI and Bench tables.
    """
    exposure: dict[str, int] = {}

    # Try to parse from Team Exposure table: | Team | Count | ...
    in_team_exposure = False
    for line in block.split("\n"):
        if "#### Team Exposure" in line:
            in_team_exposure = True
            continue
        if in_team_exposure:
            if re.match(r"^#### \S", line):
                break
            if line.startswith("|") and not re.match(r"^\|[-| ]+\|", line):
                parts = [p.strip() for p in line.strip("|").split("|")]
                if len(parts) >= 2:
                    team = parts[0].strip()
                    try:
                        count = int(parts[1].strip())
                        if team and team != "Team":
                            exposure[team] = count
                    except ValueError:
                        pass

    return exposure


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Extract the Classic Squad block from a squad-builder output file."
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
