"""Injury returnee radar: turning FPL availability news into return signals.

The FPL `news` field is the only return-timing signal that ships with the data
every run already fetches, so this module parses it directly. The grammar is
observed rather than contractual: a live bootstrap-static snapshot resolved to
exactly four shapes, and only two of them carry a date.

    {reason} - Expected back {D} {Mmm}      -> date
    Suspended until {D} {Mmm}               -> date
    {reason} - {NN}% chance of playing      -> no date (the percentage
                                               duplicates
                                               `chance_of_playing_next_round`)
    {reason} - Unknown return date          -> no date

Parse contract:

* Nothing here raises on bad input. Anything the two dated shapes do not match
  -- a new phrasing, a transfer note, an empty string -- yields a signal with
  no date, because date-unknown is the common case (roughly one flagged player
  in eight carries a date), not the error case.
* FPL states a day and a month with no year. The year is resolved against the
  season start year on the same July cutover `fpl_cli.season` uses, so a
  February return during an August-start season lands in the following calendar
  year.
* A resolved date is mapped to a gameweek by walking event deadlines, never by
  assuming a fixed number of weeks per gameweek -- the live schedule has a
  three-week break between GW5 and GW6. A date past the final deadline maps to
  no gameweek rather than being clamped onto GW38.
* A date that falls before the current gameweek's deadline while the player is
  still flagged has *lapsed*: `has_return_date` goes False and the signal reads
  as date-unknown, while `return_date` keeps the stated date for display and
  for week-over-week diffing. Decaying into the date-unknown bucket rather than
  inventing a new state means a failed return stays on the watchlist instead of
  advertising a return gameweek that has already been missed.

There is no cache here: every signal is derived from data the caller already
holds. Internal date maths is UTC throughout; formatting a date for a user is
the caller's job and goes through `fpl_cli.utils.time`.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Any

from fpl_cli.season import get_season_year
from fpl_cli.services.scoring.evaluation import read_player_field

# Identifies where a return date came from. U5's optional AI-search enrichment
# adds its own source alongside this one.
SOURCE_FPL_NEWS = "fpl-news"

# July cutover, matching `fpl_cli.season.get_season_year`: a month at or after
# July belongs to the season start year, an earlier month to the year after.
_CUTOVER_MONTH = 7

_MONTHS: dict[str, int] = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}

# The two measured shapes that carry a date. Each anchors on its own keyword
# phrase, so the `{NN}% chance of playing` shape cannot be read as a day.
_DATE_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\bexpected\s+back\s+(\d{1,2})\s+([a-z]+)", re.IGNORECASE),
    re.compile(r"\bsuspended\s+until\s+(\d{1,2})\s+([a-z]+)", re.IGNORECASE),
)


@dataclass(frozen=True)
class ReturnSignal:
    """One player's parsed availability news.

    `return_date` survives lapsing so a display can say "was due 5 Sep" and the
    week-over-week diff can tell a moved date apart from a newly stated one.
    `has_return_date` -- not `return_date is not None` -- is the check for
    whether a usable date exists.
    """

    news: str
    chance_of_playing: int | None = None
    return_date: date | None = None
    return_gameweek: int | None = None
    source: str | None = None
    news_age_days: int | None = None
    lapsed: bool = False

    @property
    def has_return_date(self) -> bool:
        """Whether a return date is both known and still ahead of us."""
        return self.return_date is not None and not self.lapsed


def parse_news_date(news: str) -> tuple[int, int] | None:
    """Extract a `(day, month)` pair from FPL news text, or None.

    Matches only the two measured date-bearing shapes. An unrecognised
    phrasing, an unknown month token or an empty string yields None.
    """
    if not news:
        return None
    for pattern in _DATE_PATTERNS:
        match = pattern.search(news)
        if match is None:
            continue
        month = _MONTHS.get(match.group(2)[:3].lower())
        if month is None:
            continue
        return int(match.group(1)), month
    return None


def resolve_return_date(day: int, month: int, season_year: int | None = None) -> date | None:
    """Resolve a bare day/month against the season, or None if impossible.

    FPL states no year. Months at or after the July cutover belong to the
    season start year, earlier months to the following calendar year, so a
    February return in the 2026-27 season resolves to February 2027.
    """
    year = season_year if season_year is not None else get_season_year()
    if month < _CUTOVER_MONTH:
        year += 1
    try:
        return date(year, month, day)
    except ValueError:
        # e.g. "Expected back 31 Feb" -- treated as date-unknown, not an error.
        return None


def gameweek_for_date(target: date, gameweeks: Sequence[Mapping[str, Any]]) -> int | None:
    """Return the gameweek a date falls in, walking event deadlines.

    The first gameweek whose deadline is on or after *target* wins, so a date
    in a multi-week break lands on the gameweek that follows it. A date past
    the final deadline returns None rather than being clamped -- a return
    beyond the fixture list on hand is unknown, not imminent.
    """
    best_gw: int | None = None
    best_deadline: date | None = None
    for event in gameweeks:
        deadline = _parse_utc(event.get("deadline_time"))
        gw_id = event.get("id")
        if deadline is None or not isinstance(gw_id, int):
            continue
        deadline_date = deadline.date()
        if deadline_date < target:
            continue
        if best_deadline is None or deadline_date < best_deadline:
            best_gw, best_deadline = gw_id, deadline_date
    return best_gw


def news_age_days(news_added: str | datetime | None, now: datetime | None = None) -> int | None:
    """Whole days since FPL last touched this player's news, or None.

    None covers both an absent stamp and an unparseable one. A stamp in the
    future (clock skew between the API and this machine) clamps to 0.
    """
    added = _parse_utc(news_added)
    if added is None:
        return None
    reference = _as_utc(now) if now is not None else datetime.now(timezone.utc)
    return max(0, (reference - added).days)


def build_return_signal(
    player: Any,
    *,
    gameweeks: Sequence[Mapping[str, Any]] = (),
    now: datetime | None = None,
    season_year: int | None = None,
) -> ReturnSignal:
    """Build the return signal for a player model or player-shaped mapping.

    Reads `news`, `news_added` and `chance_of_playing_next_round` through
    `read_player_field`, so both shapes are accepted. Never raises: an
    unparseable news string yields a date-unknown signal.
    """
    news = read_player_field(player, "news", "") or ""
    chance = read_player_field(player, "chance_of_playing_next_round")
    added = read_player_field(player, "news_added")

    parsed = parse_news_date(news)
    return_date = resolve_return_date(*parsed, season_year) if parsed else None

    lapsed = False
    return_gameweek: int | None = None
    if return_date is not None:
        current_deadline = _current_deadline_date(gameweeks, now)
        lapsed = current_deadline is not None and return_date < current_deadline
        if not lapsed:
            return_gameweek = gameweek_for_date(return_date, gameweeks)

    return ReturnSignal(
        news=news,
        chance_of_playing=chance,
        return_date=return_date,
        return_gameweek=return_gameweek,
        source=SOURCE_FPL_NEWS if return_date is not None else None,
        news_age_days=news_age_days(added, now),
        lapsed=lapsed,
    )


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _as_utc(value: datetime) -> datetime:
    """Normalise a datetime to UTC, reading a naive one as UTC (FPL convention)."""
    return value.astimezone(timezone.utc) if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _parse_utc(value: Any) -> datetime | None:
    """Coerce an FPL ISO timestamp to a UTC datetime, or None if unusable."""
    if isinstance(value, datetime):
        return _as_utc(value)
    if not isinstance(value, str) or not value:
        return None
    try:
        return _as_utc(datetime.fromisoformat(value.replace("Z", "+00:00")))
    except ValueError:
        return None


def _current_deadline_date(gameweeks: Sequence[Mapping[str, Any]], now: datetime | None) -> date | None:
    """The most recent deadline already passed, which is what a date must beat.

    Measuring against the deadline that has passed rather than the one coming
    up keeps a return stated for later this week off the lapsed pile: only a
    date the current gameweek has already left behind counts as failed.
    """
    reference = _as_utc(now) if now is not None else datetime.now(timezone.utc)
    latest: date | None = None
    for event in gameweeks:
        deadline = _parse_utc(event.get("deadline_time"))
        if deadline is None or deadline > reference:
            continue
        if latest is None or deadline.date() > latest:
            latest = deadline.date()
    return latest
