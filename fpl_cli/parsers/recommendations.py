"""Parse gw{N}-recommendations.md into structured decisions."""

from __future__ import annotations

import re
from pathlib import Path

import yaml


def _strip_parenthetical(name: str) -> str:
    """Remove trailing parenthetical like '(if confirmed fit)'."""
    return re.sub(r"\s*\(.*?\)\s*$", "", name).strip()


def _clean_name(name: str) -> str:
    """Normalise a player name for matching."""
    name = _strip_parenthetical(name)
    # Strip leading initial pattern like "L."
    name = re.sub(r"^[A-Z]\.\s*", "", name)
    return name.strip()


def parse_recommendations(path: Path) -> dict | None:
    """Parse a recommendations markdown file. Returns None if file missing."""
    if not path.exists():
        return None

    text = path.read_text(encoding="utf-8")

    result: dict = {
        "gameweek": _parse_gameweek(text),
        "classic": {
            "captain": None,
            "vice_captain": None,
            "transfers": [],
            "roll_transfer": False,
        },
        "draft": {
            "waivers": [],
        },
    }

    _parse_captain(text, result)
    _parse_transfers(text, result)
    _parse_waivers(text, result)

    return result


def _parse_gameweek(text: str) -> int | None:
    """Extract gameweek from YAML frontmatter."""
    fm_match = re.match(r"^---\s*\n(.*?)\n---", text, re.DOTALL)
    if not fm_match:
        return None
    try:
        data = yaml.safe_load(fm_match.group(1))
        return int(data.get("gameweek")) if data else None
    except (yaml.YAMLError, TypeError, ValueError):
        return None


def _parse_captain(text: str, result: dict) -> None:
    """Extract captain and vice captain."""
    m = re.search(
        r"\*\*Captain:\*\*\s+(.+?)\s*\|\s*\*\*Vice:\*\*\s+(.+)",
        text,
    )
    if m:
        result["classic"]["captain"] = _clean_name(m.group(1))
        result["classic"]["vice_captain"] = _clean_name(m.group(2))


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


def _parse_transfers(text: str, result: dict) -> None:
    """Extract transfer recommendations from Classic section."""
    sections = _get_section(text)
    classic = sections["classic"]
    if not classic:
        return

    # Detect roll / no transfers
    if re.search(r"##### Recommended Transfer.*:\s*Roll", classic, re.IGNORECASE):
        result["classic"]["roll_transfer"] = True
        return
    if re.search(r"No transfers? (?:this gameweek|recommended)", classic, re.IGNORECASE):
        result["classic"]["roll_transfer"] = True
        return

    # Pattern: heading line with IN <- OUT
    # Matches both "##### Recommended Transfer (1): Iwobi <- Miley"
    # and "##### Transfer 1: Rice <- Saka"
    transfer_pattern = re.compile(
        r"^#{4,5}\s+(?:Recommended )?Transfer[s]?\s*(?:\(\d+\)|\d+)?:\s*(.+?)\s*(?:<-|←)\s*(.+)",
        re.MULTILINE,
    )
    for m in transfer_pattern.finditer(classic):
        player_in = _clean_name(m.group(1).strip())
        player_out = _clean_name(m.group(2).strip())
        if player_in and player_out:
            result["classic"]["transfers"].append({"in": player_in, "out": player_out})


def _parse_waivers(text: str, result: dict) -> None:
    """Extract waiver recommendations from Draft section."""
    sections = _get_section(text)
    draft = sections["draft"]
    if not draft:
        return

    if re.search(r"No waivers? recommended", draft, re.IGNORECASE):
        return

    # Pattern: "##### Priority 1: Nyoni (LIV, MID) ← Wirtz"
    waiver_pattern = re.compile(
        r"^#{4,5}\s+Priority\s+(\d+):\s*(.+?)\s*(?:<-|←)\s*(.+)",
        re.MULTILINE,
    )
    for m in waiver_pattern.finditer(draft):
        priority = int(m.group(1))
        # IN player may have team/position suffix like "(LIV, MID)"
        player_in = _clean_name(re.sub(r"\s*\([^)]*\)\s*$", "", m.group(2).strip()))
        player_out = _clean_name(m.group(3).strip())
        if player_in and player_out:
            result["draft"]["waivers"].append({
                "priority": priority,
                "in": player_in,
                "out": player_out,
            })
