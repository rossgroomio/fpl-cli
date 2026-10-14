"""Parse gw{N}-recommendations.md into structured decisions.

The file is written by the gw-prep skill, so its shape is whatever
`.agents/skills/gw-prep/references/output-template.md` says it is -- and,
on a wildcard or free hit week, the Classic Squad block that template embeds
from squad-builder's own. That template moved from one heading per move
(`##### Recommended Transfer (1): In <- Out`) to markdown tables, and a parser
still matching the headings extracted nothing at all: every move came back
unadvised, including the ones followed to the letter (#354).

So tables are found by their column headers rather than by the heading above
them, and a column by its name rather than its position -- the transfer table
is Out-then-In while the old headings were In-then-Out, and that flip is the
easy one to get backwards. The heading format is still read as a fallback:
files written before the template change are still on disk, and a review of
an old gameweek reads them.

Names keep their initial and hand their club code over separately. Stripping
the initial made `B.Fernandes` and `J.Fernandes` one player, and five of the
pairs it merges on a current bootstrap are the same club twice, so the club
code alone could not have separated them again.
"""

from __future__ import annotations

import re
from collections.abc import Collection, Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from fpl_cli.utils.markdown import HeadingMatcher, fence_flags, parse_heading, section_body

# A club code is exactly three capitals in FPL ("ARS", "NFO"). Anything shorter
# is a captaincy marker ("(C)", "(VC)"), and a position code is three capitals
# too, so those are named and skipped: the waiver template's cells read
# "Nyoni (LIV, MID)".
_TEAM_CODE_RE = re.compile(r"[A-Z]{3}")
_POSITION_CODES = frozenset({"GKP", "DEF", "MID", "FWD"})
_TRAILING_PAREN_RE = re.compile(r"\s*\(([^()]*)\)\s*$")
_EMPHASIS_RE = re.compile(r"^[*_`]+|[*_`]+$")
# The lineup template appends "⚡ Override: {reason}" to a player it forced in
# or out; the reason is prose, never part of the name.
_OVERRIDE_RE = re.compile(r"\s*⚡.*$")

# A cell standing in for "no move here": the template's own empty rows, and
# what a writer fills them with when it recommends nothing for that slot.
_PLACEHOLDERS = frozenset({
    "", "-", "--", "—", "–", "...", "…", "n/a", "na", "tbd", "none", "nobody",
    "roll", "hold", "no transfer", "no claim",
})

_SEPARATOR_RE = re.compile(r"^\|?\s*:?-+:?\s*(?:\|\s*:?-+:?\s*)*\|?$")
_CELL_SPLIT_RE = re.compile(r"(?<!\\)\|")

# Column names, lowercased. Several per column because the draft section's
# free-agent table, written once the waiver run has processed, heads its
# columns "# | Drop | Pick up" rather than "Priority | Drop | Claim".
_TRANSFER_OUT = frozenset({"out", "transfer out", "player out", "sell"})
_TRANSFER_IN = frozenset({"in", "transfer in", "player in", "buy"})
_WAIVER_DROP = frozenset({"drop", "out", "release"})
_WAIVER_CLAIM = frozenset({"claim", "pick up", "pick-up", "pickup", "add", "in", "target"})
_PRIORITY = frozenset({"priority", "#", "p", "rank"})
_PLAYER = frozenset({"player", "name"})
_TEAM = frozenset({"team", "club"})

# The tables of a wildcard or free hit squad. Squad-builder writes them at
# `###` and gw-prep embeds them a level down, and a re-derived squad follows
# no fixed depth, so each depth gets a matcher. `Bench Order` is a different
# heading, deliberately: it is the transfer week's bench, not a squad's.
_STARTING_XI_HEADINGS = tuple(
    HeadingMatcher(f"{'#' * depth} Starting XI", aliases=("XI", "Starting Eleven", "Starting 11"))
    for depth in (3, 4, 5)
)
_BENCH_HEADINGS = tuple(HeadingMatcher(f"{'#' * depth} Bench") for depth in (3, 4, 5))
_HEADING_CLASSIC_SQUAD = HeadingMatcher("### Classic Squad")
_HEADING_TRANSFER_RECS = HeadingMatcher("### Transfer Recommendations")

_SQUAD_MODES = frozenset({"wildcard", "freehit"})

_CAPTAIN_RE = re.compile(
    r"^\s*(?:[-*+]\s+)?\*\*(?:Recommended\s+)?Captain\s*:?\s*\*\*\s*:?\s*(?P<value>.+)$",
    re.IGNORECASE | re.MULTILINE,
)
_VICE_LABEL_RE = re.compile(
    r"^\s*\*\*(?:Recommended\s+)?Vice(?:[\s-]*captain)?\s*:?\s*\*\*\s*:?\s*",
    re.IGNORECASE,
)
# What separates a captain's name from the rationale after it. Spaced on both
# sides, so a hyphenated surname ("Gibbs-White") is never cut.
_RATIONALE_SPLIT_RE = re.compile(r"\s+(?:--|—|–|-)\s+")
_MODE_LINE_RE = re.compile(r"\*\*Mode:\*\*\s*([^|\n]+)", re.IGNORECASE)

# The heading format the template replaced. Kept for files written before it.
_LEGACY_TRANSFER_RE = re.compile(
    r"^#{4,5}\s+(?:Recommended )?Transfer[s]?\s*(?:\(\d+\)|\d+)?:\s*(.+?)\s*(?:<-|←)\s*(.+)",
    re.MULTILINE,
)
_LEGACY_WAIVER_RE = re.compile(
    r"^#{4,5}\s+Priority\s+(\d+):\s*(.+?)\s*(?:<-|←)\s*(.+)",
    re.MULTILINE,
)
_LEGACY_ROLL_RE = re.compile(
    r"##### Recommended Transfer.*:\s*Roll|No transfers? (?:this gameweek|recommended)",
    re.IGNORECASE,
)
_ROLL_WORD_RE = re.compile(r"\broll(?:ing|ed)?\b", re.IGNORECASE)


@dataclass(frozen=True)
class _Table:
    headers: list[str]
    rows: list[list[str]]
    heading: str | None


def _split_name_team(cell: str) -> tuple[str, str | None]:
    """A player cell as (name, club code), the initial left on the name.

    Cells carry a club suffix ("Tzolis (ARS)", "Nyoni (LIV, MID)"), sometimes a
    qualifier ("Haaland (if fit)") or a captaincy marker ("Salah (C)"), and in
    the lineup tables an override note. The club code is worth keeping: it is
    what separates `Munoz` (LIV) from `Muñoz` (NFO) once the accent is folded.
    """
    text = _OVERRIDE_RE.sub("", cell).strip()
    text = _EMPHASIS_RE.sub("", text).strip()
    team: str | None = None
    while (match := _TRAILING_PAREN_RE.search(text)) is not None:
        for token in re.split(r"[\s,/;]+", match.group(1)):
            if team is None and _TEAM_CODE_RE.fullmatch(token) and token not in _POSITION_CODES:
                team = token
        text = text[: match.start()].strip()
    return _EMPHASIS_RE.sub("", text).strip(), team


def _is_placeholder(name: str) -> bool:
    return name.strip().lower() in _PLACEHOLDERS


def _cells(line: str) -> list[str]:
    inner = line.strip()
    if inner.startswith("|"):
        inner = inner[1:]
    if inner.endswith("|") and not inner.endswith("\\|"):
        inner = inner[:-1]
    return [cell.strip().replace("\\|", "|") for cell in _CELL_SPLIT_RE.split(inner)]


def _header_key(cell: str) -> str:
    return " ".join(_EMPHASIS_RE.sub("", cell).split()).lower()


def _is_separator(line: str) -> bool:
    return "-" in line and _SEPARATOR_RE.match(line.strip()) is not None


def _iter_tables(lines: Sequence[str]) -> Iterator[_Table]:
    """Every markdown table in `lines`, with the heading it sits under.

    Fence-aware, so a table quoted inside a code block is not read as one.
    A header row is a pipe row followed by a separator row, which is also what
    ends a table stacked directly under another.
    """
    fenced = list(fence_flags(lines))
    heading: str | None = None
    i = 0
    while i < len(lines):
        line = lines[i].strip()
        if fenced[i]:
            i += 1
            continue
        if parse_heading(line) is not None:
            heading = line
            i += 1
            continue
        if line.startswith("|") and i + 1 < len(lines) and _is_separator(lines[i + 1]):
            headers = [_header_key(cell) for cell in _cells(line)]
            rows: list[list[str]] = []
            j = i + 2
            while j < len(lines) and not fenced[j] and lines[j].strip().startswith("|"):
                if j + 1 < len(lines) and _is_separator(lines[j + 1]):
                    break
                rows.append(_cells(lines[j]))
                j += 1
            yield _Table(headers=headers, rows=rows, heading=heading)
            i = j
            continue
        i += 1


def _column(headers: Sequence[str], names: Collection[str]) -> int | None:
    return next((i for i, header in enumerate(headers) if header in names), None)


def _cell(row: Sequence[str], index: int) -> str:
    return row[index] if index < len(row) else ""


def parse_recommendations(path: Path) -> dict[str, Any] | None:
    """Parse a recommendations markdown file. Returns None if file missing."""
    if not path.exists():
        return None

    text = path.read_text(encoding="utf-8")
    frontmatter = _parse_frontmatter(text)
    sections = _get_section(text)
    mode = _parse_mode(text, frontmatter)

    result: dict[str, Any] = {
        "gameweek": _parse_gameweek(frontmatter),
        "classic": {
            "mode": mode,
            "captain": None,
            "captain_team": None,
            "vice_captain": None,
            "vice_captain_team": None,
            "transfers": [],
            "roll_transfer": False,
            "squad": [],
        },
        "draft": {
            "waivers": [],
        },
    }

    _parse_captain(text, result)
    # A wildcard or free hit week recommends a squad, not moves: the template
    # suppresses the transfer table, so there is nothing to pair in and out.
    result["classic"]["squad"] = _parse_squad(sections["classic"], mode)
    if not result["classic"]["squad"]:
        _parse_transfers(sections["classic"], result)
    _parse_waivers(sections["draft"], result)

    return result


def _parse_frontmatter(text: str) -> dict[str, Any]:
    fm_match = re.match(r"^---\s*\n(.*?)\n---", text, re.DOTALL)
    if not fm_match:
        return {}
    try:
        data = yaml.safe_load(fm_match.group(1))
    except yaml.YAMLError:
        return {}
    return data if isinstance(data, dict) else {}


def _parse_gameweek(frontmatter: dict[str, Any]) -> int | None:
    """Extract gameweek from YAML frontmatter."""
    try:
        return int(frontmatter["gameweek"])
    except (KeyError, TypeError, ValueError):
        return None


def _normalise_mode(raw: object) -> str | None:
    key = re.sub(r"[\s_-]+", "", str(raw)).lower()
    return key if key in {"wildcard", "freehit", "benchboost", "transfer"} else None


def _parse_mode(text: str, frontmatter: dict[str, Any]) -> str | None:
    """The chip the file was written for: `wildcard`, `freehit`, `benchboost`,
    `transfer`, or None when the file does not say.

    The frontmatter `mode` comes first. Failing that, the Classic Squad block's
    own `**Mode:** Wildcard` line -- but not the file's opening `**Mode:**`
    line, which reads "transfer" or "squad-builder" and so never names a chip.
    """
    mode = _normalise_mode(frontmatter.get("mode", ""))
    if mode is not None:
        return mode
    for match in _MODE_LINE_RE.finditer(text):
        mode = _normalise_mode(_EMPHASIS_RE.sub("", match.group(1).strip()))
        if mode in _SQUAD_MODES:
            return mode
    return None


def _parse_captain(text: str, result: dict[str, Any]) -> None:
    """Extract captain and vice captain.

    Two lines carry them. The transfer-mode template writes
    `**Recommended captain:** {player} -- {rationale}` and names no vice; the
    squad-builder block a wildcard week embeds, and files older than the
    table template, write `**Captain:** X | **Vice:** Y`.
    """
    match = _CAPTAIN_RE.search(text)
    if not match:
        return
    captain_part, _, vice_part = match.group("value").partition("|")
    captain, captain_team = _split_name_team(_RATIONALE_SPLIT_RE.split(captain_part, maxsplit=1)[0])
    if captain and not _is_placeholder(captain):
        result["classic"]["captain"] = captain
        result["classic"]["captain_team"] = captain_team
    vice_label = _VICE_LABEL_RE.match(vice_part)
    if vice_label:
        vice, vice_team = _split_name_team(
            _RATIONALE_SPLIT_RE.split(vice_part[vice_label.end():], maxsplit=1)[0]
        )
        if vice and not _is_placeholder(vice):
            result["classic"]["vice_captain"] = vice
            result["classic"]["vice_captain_team"] = vice_team


def _get_section(text: str) -> dict[str, str]:
    """Split text into classic/draft sections."""
    sections: dict[str, str] = {"classic": "", "draft": ""}
    classic_start = re.search(r"^## Classic\b", text, re.MULTILINE)
    draft_start = re.search(r"^## Draft\b", text, re.MULTILINE)

    if classic_start and draft_start:
        sections["classic"] = text[classic_start.start():draft_start.start()]
        sections["draft"] = text[draft_start.start():]
    elif classic_start:
        sections["classic"] = text[classic_start.start():]
    elif draft_start:
        sections["draft"] = text[draft_start.start():]

    return sections


def _parse_squad(classic: str, mode: str | None) -> list[dict[str, Any]]:
    """The recommended 15 on a wildcard or free hit week, else [].

    Read from the Starting XI and Bench tables, and never from a Bench table
    alone. A file that names no mode is
    read only when it carries the embedded `### Classic Squad` block, so an
    older transfer-week file is never mistaken for a squad recommendation.
    """
    if not classic:
        return []
    lines = classic.split("\n")
    if mode not in _SQUAD_MODES and not (
        mode is None and any(_HEADING_CLASSIC_SQUAD.matches(line) for line in lines)
    ):
        return []

    squad: list[dict[str, Any]] = []
    seen: set[tuple[str, str | None]] = set()
    has_starting_xi = False
    for table in _iter_tables(lines):
        if table.heading is None:
            continue
        is_starting_xi = any(m.matches(table.heading) for m in _STARTING_XI_HEADINGS)
        if not is_starting_xi and not any(m.matches(table.heading) for m in _BENCH_HEADINGS):
            continue
        player_col = _column(table.headers, _PLAYER)
        if player_col is None:
            continue
        has_starting_xi = has_starting_xi or is_starting_xi
        team_col = _column(table.headers, _TEAM)
        for row in table.rows:
            name, team = _split_name_team(_cell(row, player_col))
            if _is_placeholder(name):
                continue
            if team_col is not None:
                team_cell = _cell(row, team_col).strip().upper()
                if _TEAM_CODE_RE.fullmatch(team_cell):
                    team = team_cell
            if (name, team) in seen:
                continue
            seen.add((name, team))
            squad.append({"name": name, "team": team})
    # A bench on its own is not a squad recommendation.
    return squad if has_starting_xi else []


def _parse_transfers(classic: str, result: dict[str, Any]) -> None:
    """Extract transfer recommendations from Classic section."""
    if not classic:
        return
    lines = classic.split("\n")
    transfers: list[dict[str, Any]] = result["classic"]["transfers"]

    for table in _iter_tables(lines):
        out_col = _column(table.headers, _TRANSFER_OUT)
        in_col = _column(table.headers, _TRANSFER_IN)
        if out_col is None or in_col is None:
            continue
        for row in table.rows:
            player_out, out_team = _split_name_team(_cell(row, out_col))
            player_in, in_team = _split_name_team(_cell(row, in_col))
            if _is_placeholder(player_out) or _is_placeholder(player_in):
                continue
            transfers.append({"in": player_in, "in_team": in_team, "out": player_out, "out_team": out_team})

    if not transfers:
        for match in _LEGACY_TRANSFER_RE.finditer(classic):
            player_in, in_team = _split_name_team(match.group(1))
            player_out, out_team = _split_name_team(match.group(2))
            if player_in and player_out:
                transfers.append({"in": player_in, "in_team": in_team, "out": player_out, "out_team": out_team})

    if not transfers:
        result["classic"]["roll_transfer"] = _recommends_roll(classic, lines)


def _recommends_roll(classic: str, lines: list[str]) -> bool:
    """True when a section with no moves in it says to roll the transfer.

    The roll word is looked for only inside Transfer Recommendations: Chip
    Timing talks about rolling a chip to a later week, which is not this.
    """
    if _LEGACY_ROLL_RE.search(classic):
        return True
    body = section_body(lines, _HEADING_TRANSFER_RECS) or []
    return any(_ROLL_WORD_RE.search(line) for line in body)


def _priority(cell: str) -> int | None:
    match = re.search(r"\d+", cell)
    return int(match.group()) if match else None


def _parse_waivers(draft: str, result: dict[str, Any]) -> None:
    """Extract waiver recommendations from Draft section.

    Both draft tables are read: the waiver table (`Priority | Drop | Claim`)
    and the free-agent one a file written after the waiver run carries instead
    (`# | Drop | Pick up`). Drop comes first in both.
    """
    if not draft:
        return
    waivers: list[dict[str, Any]] = result["draft"]["waivers"]

    for table in _iter_tables(draft.split("\n")):
        drop_col = _column(table.headers, _WAIVER_DROP)
        claim_col = _column(table.headers, _WAIVER_CLAIM)
        if drop_col is None or claim_col is None:
            continue
        priority_col = _column(table.headers, _PRIORITY)
        for row in table.rows:
            player_out, out_team = _split_name_team(_cell(row, drop_col))
            player_in, in_team = _split_name_team(_cell(row, claim_col))
            if _is_placeholder(player_out) or _is_placeholder(player_in):
                continue
            priority = _priority(_cell(row, priority_col)) if priority_col is not None else None
            waivers.append({
                "priority": priority if priority is not None else len(waivers) + 1,
                "in": player_in,
                "in_team": in_team,
                "out": player_out,
                "out_team": out_team,
            })

    if waivers:
        return
    for match in _LEGACY_WAIVER_RE.finditer(draft):
        player_in, in_team = _split_name_team(match.group(2))
        player_out, out_team = _split_name_team(match.group(3))
        if player_in and player_out:
            waivers.append({
                "priority": int(match.group(1)),
                "in": player_in,
                "in_team": in_team,
                "out": player_out,
                "out_team": out_team,
            })
