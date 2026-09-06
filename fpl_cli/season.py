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

from collections.abc import Mapping, Sequence
from datetime import date, datetime
from pathlib import Path
from typing import Any

# -- Constants ---------------------------------------------------------------

TOTAL_GAMEWEEKS: int = 38
"""Number of gameweeks in a Premier League season (unchanged since 1995)."""

CHIP_SPLIT_GW: int = TOTAL_GAMEWEEKS // 2
"""Gameweek boundary for chip availability (each chip once per half)."""

PROMOTED_CLUBS_PER_SEASON: int = 3
"""Clubs promoted into the Premier League each season (three up, three down since 1995-96)."""

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


def season_year_from_gameweeks(gameweeks: Sequence[Mapping[str, Any]]) -> int | None:
    """Derive the season start year from GW1's deadline, not the clock (#91).

    `get_season_year()`'s July cutover assumes a season always finishes
    before 1 July. 2019-20 didn't -- it ran to July 2020, COVID-delayed --
    so the clock stamped its final gameweeks with the *following* season's
    label. GW1's deadline doesn't have that problem: whenever the season
    ends, it started when it started.

    Returns None when `gameweeks` has no GW1 or GW1 carries no parseable
    `deadline_time` -- pre-season, before fixtures are released, or a caller
    handed an unrelated payload -- so the caller can fall back to
    `get_season_year()` rather than receive a guess.

    >>> season_year_from_gameweeks([{"id": 1, "deadline_time": "2019-08-09T18:00:00Z"}])
    2019
    """
    gw1 = next((gw for gw in gameweeks if gw.get("id") == 1), None)
    deadline = gw1.get("deadline_time") if gw1 else None
    if not isinstance(deadline, str):
        return None
    try:
        return datetime.fromisoformat(deadline.replace("Z", "+00:00")).year
    except ValueError:
        return None


def resolve_season_year(
    gameweeks: Sequence[Mapping[str, Any]], today: date | None = None,
) -> int:
    """The season year a bootstrap-static payload names, favouring GW1 over the clock.

    `FPLClient.get_season_year()`'s policy, factored out so it is testable
    without an event loop or a mocked client (#91 review). Three cases:

    - No GW1, or an unparseable deadline (pre-season, before fixtures are
      released): the clock is all there is.
    - GW1 exists and at least one gameweek in the payload is not yet
      finished (the season is live): GW1's own year, always -- this is the
      case the derivation exists for, and a season overrunning the July
      cutover (2019-20, into July 2020) must not be second-guessed against
      a clock that has since rolled into the following season.
    - Every gameweek in the payload is finished (the season shown is over)
      *and* the clock's year is newer: the clock. `bootstrap-static` keeps
      serving a just-finished season's events, untouched, through the close
      season until the next one's fixtures are released -- GW1's year is
      then stale, naming a season that has already ended, and trusting it
      would silently misfile the very first `fpl status`/`review`/
      `league-recap` runs of the new season (the same mislabelling #91 is
      about, at the other end of the year).

    >>> resolve_season_year([{"id": 1, "deadline_time": "2019-08-09T18:00:00Z"}])
    2019
    """
    clock_year = get_season_year(today)
    gw1_year = season_year_from_gameweeks(gameweeks)
    if gw1_year is None:
        return clock_year
    season_concluded = bool(gameweeks) and all(gw.get("finished") for gw in gameweeks)
    if season_concluded and clock_year > gw1_year:
        return clock_year
    return gw1_year


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


def previous_season_year(today: date | None = None) -> int:
    """Start year of the newest *completed* season.

    One expression, in one place, because "the season before this one" is
    asked from several directions: the player prior reads last season's
    pts/90, the calibration script defaults to calibrating against it, and
    `fpl doctor` measures the frozen quality anchors against it. Each had
    re-typed `get_season_year() - 1` locally.

    The July cutover makes this exact rather than approximate: from the
    cutover onwards the season in progress is the current one, so the one
    before it has finished and nothing newer can have been measured.

    >>> previous_season_year(date(2026, 9, 1))
    2025
    """
    return get_season_year(today) - 1


def previous_season_label(today: date | None = None) -> str:
    """Hyphenated label of the newest completed season.

    >>> previous_season_label(date(2026, 9, 1))
    '2025-26'
    """
    return season_label(previous_season_year(today))


def season_label_range(year: int | None = None, count: int = 4) -> tuple[str, ...]:
    """Return a trailing window of season identifiers in hyphenated format.

    >>> season_label_range(2025, count=4)
    ('2022-23', '2023-24', '2024-25', '2025-26')
    """
    y = year if year is not None else get_season_year()
    return tuple(season_label(y - count + 1 + i) for i in range(count))


# -- Directory partitioning --------------------------------------------------

def is_season_label(name: str) -> bool:
    """Whether `name` is a season label for *some* season, not necessarily now.

    Exact rather than shape-matching: the leading year is parsed and the label
    rebuilt, so `2026-27` passes and `2026-28` does not. Used to tell a
    directory a user pointed at last season apart from an ordinary directory
    that merely contains digits.

    Non-string input answers False rather than raising: the annotation says
    `str`, but the callers that lean on this are reading names off disk or out
    of a generated block, and `season_start_year` promises them a ValueError
    for anything that is not a label. An AttributeError out of `.partition`
    would break that promise in the one place it matters -- a health check
    reporting the bad value instead of dying on it.

    >>> is_season_label("2026-27"), is_season_label("2026-28"), is_season_label("reports")
    (True, False, False)
    """
    if not isinstance(name, str):
        return False
    year, _, _ = name.partition("-")
    if not (len(year) == 4 and year.isdigit()):
        return False
    return season_label(int(year)) == name


def season_start_year(label: str) -> int:
    """Start year of a hyphenated season label: ``"2025-26"`` -> 2025.

    The inverse of `season_label`, for callers handed a label (a source
    window, a partition directory) that need the year to build another
    source's identifier from. Anything that is not a season label -- the
    Core-Insights ``2025-2026`` form included -- is a ValueError rather than
    a guessed year.

    >>> season_start_year("2025-26")
    2025
    """
    if not is_season_label(label):
        raise ValueError(f"not a season label: {label!r}")
    return int(label[:4])


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
    Only the *current* label short-circuits. A base ending in some other
    season's label is partitioned normally, giving `2025-26/2026-27`: nesting
    is ugly but visible and lossless, where treating a stale directory as
    already-partitioned would file this season's reports under last season's
    name -- the mislabelling #85 is about. `resolve_output_dir` warns when it
    sees that shape rather than letting it pass silently.

    Defaults to `season_label()` -- today's date on a fixed July cutover --
    when `season` is omitted. That mislabels a season that overruns the
    cutover (2019-20, delayed into July 2020 by COVID): its late gameweeks
    would collide with that season's own. A caller that can reach
    bootstrap-static should derive the label from GW1's deadline instead
    (`season_year_from_gameweeks()`) and pass it explicitly -- as `review`,
    `league-recap` and `preview` do -- rather than rely on the default (#91).

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
