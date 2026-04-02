"""Season detection and constants for FPL season transitions.

Centralises all season-specific values so the CLI works automatically
when a new season starts.  The season year is derived from the current
date using the July cutover (FPL typically opens mid-July for the
season starting in August).

Format conventions used by external data sources:
  - Understat: start year as string, e.g. "2025" for 2025/26
  - Vaastav:   hyphenated, e.g. "2025-26" for 2025/26
"""

from __future__ import annotations

from datetime import date

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


def vaastav_season(year: int | None = None) -> str:
    """Return the Vaastav season identifier (hyphenated format).

    >>> vaastav_season(2025)
    '2025-26'
    """
    y = year if year is not None else get_season_year()
    return f"{y}-{(y + 1) % 100:02d}"


def vaastav_season_range(year: int | None = None, count: int = 4) -> tuple[str, ...]:
    """Return a trailing window of Vaastav season identifiers.

    >>> vaastav_season_range(2025, count=4)
    ('2022-23', '2023-24', '2024-25', '2025-26')
    """
    y = year if year is not None else get_season_year()
    return tuple(vaastav_season(y - count + 1 + i) for i in range(count))
