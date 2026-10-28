"""The Premier League's own data API, for its monthly player awards (issue #492).

FPL's API does not carry the Premier League's awards, so the recap's Player
of the Month voucher reads them where premierleague.com's awards page does:
the league's own data API, found in the site's JavaScript on 2026-10-22
(endpoint `awardsBySeason`) and first read live on 2026-10-28.

What that live response showed, and what the parser relies on:

- `GET /api/v1/competitions/8/seasons/{season}/awards`, where 8 is the
  Premier League and the season is its start year (2026 for 2026-27). It
  answers without the `Origin` header the older `footballapi.pulselive.com`
  reportedly needed.
- `player_awards` lists every player award of the season, the monthly ones
  and the end-of-season set alike. An entry's `type` names the award (`POTM`
  is Player of the Month), its `date` the month as "2026-9", unpadded, and
  its `id` the player, as a string.
- That `id` is FPL's `code`: every 2025-26 and 2026-27 winner still in the
  bootstrap matched his own (Groß 60307, João Pedro 475168), as the site's
  player pages suggested, since they use the Opta id FPL carries as
  `opta_code` ("p60307").
- May has no Player of the Month: the season's awards are made instead, and
  carry May's date.

The endpoint is undocumented and has moved before (public libraries still
call `footballapi.pulselive.com`, which the site no longer uses). So the keys
the parser reads are declared here, as the dataset clients declare their
columns (`contract.py`), and the `fpl doctor --providers` probe asserts
against the same constant. A Player of the Month entry it cannot read is
drift, raised rather than skipped: a skipped award would read as a month with
none, and a month nobody could check is not an empty one. The goal and save
of the month, which nothing reads, are skipped instead, so their drift never
costs the Player of the Month.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Self

import httpx

from fpl_cli.api.contract import missing_columns
from fpl_cli.season import season_start_year

BASE_URL = "https://sdp-prem-prod.premier-league-prod.pulselive.com/api/v1"

# The Premier League in the API's own numbering (`EN_PR: 8` in the site's
# filters).
COMPETITION_ID = 8

# The list of player awards in a season's response, and the keys the parser
# reads off each of its entries.
PLAYER_AWARDS_KEY = "player_awards"
AWARD_REQUIRED_KEYS: frozenset[str] = frozenset({"type", "date", "id"})

PLAYER_OF_THE_MONTH = "POTM"
# The monthly player awards, as the site's filters name them: the player, the
# goal and the save of the month. Every other type is an end-of-season award.
MONTHLY_PLAYER_AWARDS: frozenset[str] = frozenset({PLAYER_OF_THE_MONTH, "GOTM", "SOTM"})

# The calendar month with no Player of the Month: the season's awards are
# made in May instead, and carry its date.
MONTH_WITHOUT_PLAYER_OF_THE_MONTH = 5

_AWARD_MONTH_RE = re.compile(r"(\d{4})-(\d{1,2})")


class AwardsFeedError(ValueError):
    """The awards response is not the shape the parser reads.

    Drift rather than a season with no awards, so a caller says the awards
    could not be checked rather than that there were none.
    """


@dataclass(frozen=True)
class MonthlyAward:
    """One monthly award, as the feed names it."""

    type: str
    year: int
    # The calendar month, 1-12.
    month: int
    # The player's FPL `code`, which is the id the feed gives him.
    code: int
    # The feed's own name for him ("Pascal Groß"), or None where it gives none.
    name: str | None
    # The entry exactly as served, for a caller that keeps a copy.
    entry: Mapping[str, Any]

    @property
    def month_key(self) -> str:
        """The month as "2026-09"."""
        return award_month_key(self.year, self.month)


def award_month_key(year: int, month: int) -> str:
    """A calendar month as "2026-09", the form every surface keys one by."""
    return f"{year:04d}-{month:02d}"


def award_month(value: object) -> tuple[int, int] | None:
    """The (year, month) a month written as the feed dates an award names
    ("2026-9", or "2026-09"), or None if it names none."""
    match = _AWARD_MONTH_RE.fullmatch(value) if isinstance(value, str) else None
    if match is None:
        return None
    year, month = int(match.group(1)), int(match.group(2))
    return (year, month) if 1 <= month <= 12 else None


def _award_code(value: object) -> int | None:
    """The player id as a positive int: the feed sends a string of digits."""
    if isinstance(value, bool):
        return None
    if isinstance(value, str) and value.isascii() and value.isdigit():
        value = int(value)
    return value if isinstance(value, int) and value > 0 else None


def award_name(value: object) -> str | None:
    """`name.display`, else the first and last names, else None."""
    if not isinstance(value, Mapping):
        return None
    display = value.get("display")
    if isinstance(display, str) and display.strip():
        return display.strip()
    parts = [value.get(key) for key in ("first", "last")]
    joined = " ".join(part.strip() for part in parts if isinstance(part, str) and part.strip())
    return joined or None


def _monthly_award(entry: Mapping[str, Any], award_type: str) -> MonthlyAward | str:
    """One monthly entry as an award, or what stops it being read."""
    missing = missing_columns(list(entry), AWARD_REQUIRED_KEYS)
    if missing:
        return f"is missing {', '.join(sorted(missing))}"
    month = award_month(entry["date"])
    if month is None:
        return f"has a `date` naming no month: {entry['date']!r}"
    code = _award_code(entry["id"])
    if code is None:
        return f"has an `id` that is no player id: {entry['id']!r}"
    return MonthlyAward(
        type=award_type, year=month[0], month=month[1], code=code,
        name=award_name(entry.get("name")), entry=dict(entry),
    )


def parse_monthly_awards(payload: Any) -> list[MonthlyAward]:
    """Every monthly player award in one season's response, in the order served.

    The end-of-season awards are left out. A Player of the Month entry must
    say which month and which player, or the whole response is refused:
    there is no telling which month a skipped one would have filled. The goal
    and save of the month are skipped where they cannot be read, and so is an
    entry that does not say what it is, since neither can be a Player of the
    Month; a list in which no entry says what it is has changed shape, and is
    refused.

    Raises:
        AwardsFeedError: the response is not the shape described above.
    """
    if not isinstance(payload, Mapping) or not isinstance(payload.get(PLAYER_AWARDS_KEY), list):
        raise AwardsFeedError(f"the response carries no `{PLAYER_AWARDS_KEY}` list")
    entries = payload[PLAYER_AWARDS_KEY]
    typed = [
        (index, entry) for index, entry in enumerate(entries)
        if isinstance(entry, Mapping) and isinstance(entry.get("type"), str)
    ]
    if entries and not typed:
        raise AwardsFeedError(f"no `{PLAYER_AWARDS_KEY}` entry carries an award `type`")
    awards: list[MonthlyAward] = []
    for index, entry in typed:
        award_type = entry["type"]
        if award_type not in MONTHLY_PLAYER_AWARDS:
            continue
        award = _monthly_award(entry, award_type)
        if isinstance(award, MonthlyAward):
            awards.append(award)
        elif award_type == PLAYER_OF_THE_MONTH:
            raise AwardsFeedError(f"`{PLAYER_AWARDS_KEY}` entry {index + 1} ({award_type}) {award}")
    return awards


class PremierLeagueClient:
    """Client for the Premier League's own data API."""

    def __init__(self, timeout: float = 30.0) -> None:
        self.timeout = timeout
        self._http = httpx.AsyncClient(base_url=BASE_URL, timeout=self.timeout)

    async def close(self) -> None:
        """Close the underlying HTTP client."""
        await self._http.aclose()

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    async def get_awards(self, season: str) -> Any:
        """One season's awards response as served, for a caller that checks
        its shape itself (`fpl doctor --providers`).

        Args:
            season: The season label, "2026-27".

        Raises:
            httpx.HTTPError: the API could not be reached, or refused.
            json.JSONDecodeError: the response is not JSON.
        """
        response = await self._http.get(
            f"/competitions/{COMPETITION_ID}/seasons/{season_start_year(season)}/awards",
        )
        response.raise_for_status()
        return response.json()

    async def get_monthly_awards(self, season: str) -> list[MonthlyAward]:
        """One season's monthly player awards (`parse_monthly_awards`).

        Raises:
            httpx.HTTPError: the API could not be reached, or refused.
            ValueError: the response is not JSON (`json.JSONDecodeError`) or
                not the shape the parser reads (`AwardsFeedError`).
        """
        return parse_monthly_awards(await self.get_awards(season))
