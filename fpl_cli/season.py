"""Season detection and constants for FPL season transitions.

Centralises all season-specific values so the CLI works automatically
when a new season starts.  The season year is derived from the current
date using the July cutover (FPL typically opens mid-July for the
season starting in August).

Format conventions used by external data sources:
  - Season label (generic): hyphenated, e.g. "2025-26" for 2025/26
  - Understat:              start year as string, e.g. "2025" for 2025/26
  - FPL-Core-Insights:      both years in full, e.g. "2025-2026" for 2025/26
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

# -- Constants ---------------------------------------------------------------

TOTAL_GAMEWEEKS: int = 38
"""Number of gameweeks in a Premier League season (unchanged since 1995)."""

CHIP_SPLIT_GW: int = TOTAL_GAMEWEEKS // 2
"""Gameweek boundary for chip availability (each chip once per half)."""

# July is the cutover month: month >= 7 means the current calendar year
# is the season start year.  This matches the existing pattern in
# fpl_cli/services/team_ratings_prior.py:150.
_CUTOVER_MONTH: int = 7


# -- Season year -------------------------------------------------------------

def get_season_year(today: date | None = None) -> int:
    """Derive the current FPL season start year from the date.

    Uses a July cutover: months >= 7 resolve to the current calendar year,
    earlier months resolve to the previous year.

    Examples:
        March 2026 -> 2025  (2025/26 season)
        July  2026 -> 2026  (2026/27 season)
        Jan   2027 -> 2026  (2026/27 season)
    """
    d = today or date.today()
    return d.year if d.month >= _CUTOVER_MONTH else d.year - 1


# -- Format helpers ----------------------------------------------------------

def understat_season(year: int | None = None) -> str:
    """Return the Understat season identifier (start year as string).

    >>> understat_season(2025)
    '2025'
    """
    return str(year if year is not None else get_season_year())


def core_insights_season(year: int | None = None) -> str:
    """Return the FPL-Core-Insights season identifier (both years in full).

    This is the path segment used by the dataset, distinct from the
    hyphenated short form returned by season_label().

    >>> core_insights_season(2025)
    '2025-2026'
    """
    y = year if year is not None else get_season_year()
    return f"{y}-{y + 1}"


def season_label(year: int | None = None) -> str:
    """Return the season identifier in hyphenated format.

    >>> season_label(2025)
    '2025-26'
    """
    y = year if year is not None else get_season_year()
    return f"{y}-{(y + 1) % 100:02d}"


def season_label_range(year: int | None = None, count: int = 4) -> tuple[str, ...]:
    """Return a trailing window of season identifiers in hyphenated format.

    >>> season_label_range(2025, count=4)
    ('2022-23', '2023-24', '2024-25', '2025-26')
    """
    y = year if year is not None else get_season_year()
    return tuple(season_label(y - count + 1 + i) for i in range(count))


# -- Directory partitioning --------------------------------------------------

def season_partition(base: Path, season: str | None = None) -> Path:
    """Return `base` partitioned by season, e.g. `01_Reports/2026-27`.

    Generated reports are named by gameweek alone, so without a season segment
    the 2026-27 GW21 report would land on the path 2025-26's GW21 report
    already occupies and destroy it -- an unconditional `write_text` with no
    existence check (#85). Partitioning by season makes that collision
    structurally impossible rather than merely warned about, and mirrors how
    the league-history ledger keys its own directories
    (`fpl_cli/services/league_history.py`).

    Appending is idempotent: a base whose final segment is already the season
    label is returned unchanged, so a user who has pointed
    `reports.output_dir` at a season directory by hand -- or a caller that
    passes an already-partitioned path back in -- does not get `2026-27/2026-27`.

    >>> season_partition(Path("01_Reports"), season="2026-27").as_posix()
    '01_Reports/2026-27'
    >>> season_partition(Path("01_Reports/2026-27"), season="2026-27").as_posix()
    '01_Reports/2026-27'
    """
    label = season or season_label()
    return base if base.name == label else base / label


# -- Backward-compatible aliases ---------------------------------------------

def vaastav_season(year: int | None = None) -> str:
    """Alias for season_label (backward compatibility)."""
    return season_label(year)


def vaastav_season_range(year: int | None = None, count: int = 4) -> tuple[str, ...]:
    """Alias for season_label_range (backward compatibility)."""
    return season_label_range(year, count)
