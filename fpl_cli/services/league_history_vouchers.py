"""The Player of the Month voucher, a draft league's house rule (issue #492).

When the Premier League names its Player of the Month, the manager who owns
him at the end of the month may hand out one fine, the league's configured
penalty, to anyone they choose. This module works out who that is from the
ledger, and keeps the record that has each month's award announced once.

Layout::

    <data_dir>/league_history/<season>/draft-<league_id>/awards/player-of-the-month-<YYYY>-<MM>.json

The two facts behave differently, so one is stored and the other derived:

- **The winner, and the recap that announced him, are stored.** The winner
  is external and the awards feed is the fragile part. The recap that
  announces a month writes one file for it, holding the feed's entry as
  served (or the override's code), when fpl-cli first saw it, the gameweek
  recapped, and whether that recap withheld the voucher. Re-running that
  gameweek's recap announces it again and no other recap does, so a late
  award or a skipped recap still produces the voucher, once, and a past
  month stays readable if the feed moves again. The one rewrite is a
  withheld month's: the first recap of the latest gameweek that can say who
  held him announces it again, settled, and records that, so a gap a later
  run fills (an unknown row the repair sweep refetches, a gameweek
  backfilled) never costs the voucher. It lives beside the ledger, not in
  the counters' disposable cache, because it cannot be rebuilt: the feed may
  stop serving it, and which recap announced it is a fact about a run, not
  about the gameweeks.
- **Who owned him is derived.** Every run reads it out of the ledger's squads
  for the month's gameweeks, joined on `code`, as the monthly honours are
  summed (`league_history_months.py`): a re-captured gameweek corrects the
  owner, the split line and the voucher, and the ledger's schema is left as
  it is.

Draft only: in draft exactly one manager can hold a player, so "the owner" is
a fact. In classic a popular winner is owned by half the league.

Three rules this module exists to enforce:

- **The owner is whoever held him for the month's last gameweek**, bench
  included: the squad the ledger records for the `stop_event` of FPL's month.
  A move after that gameweek's deadline counts towards the next month. FPL's
  months rather than the calendar's: the award covers the calendar month's
  matches, and October's phase also takes GW9's two November fixtures, but
  for ownership the phase is close enough and keeps one definition of a
  month across the recap.
- **Unknown is not nobody.** A gameweek says nobody held him only when every
  manager in it has a recorded squad, every player in those squads is known
  by code and the recap read the whole league: a missing or unknown row, a
  player the draft-to-main match never placed, or a member the standings
  never returned could be him. So the voucher is withheld, with the
  reason, when the month's last gameweek cannot say, and a gap earlier in the
  month only qualifies the line saying who held him when ("GW7 not
  recorded"). Finding him settles it, though: nobody else can have held him
  that gameweek, so a gap in someone else's row leaves him found.
- **A winner neither FPL nor the ledger knows is not checked.** A code that
  matches no player in the bootstrap or the month's squads is what the feed's
  ids drifting off FPL's codes, or a typo in an override, looks like, and
  reading it as "nobody held him" would announce a wrong answer. Such a month
  is not announced, so a later run with the fix announces it then.
"""

from __future__ import annotations

import json
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, cast

from fpl_cli.api.premier_league import award_month_key
from fpl_cli.models.league_history import CaptureStatus
from fpl_cli.services.league_history import LeagueHistoryError, LeagueHistoryStore, partition_dir
from fpl_cli.services.league_history_months import capped_managers
from fpl_cli.utils.files import atomic_write_text
from fpl_cli.utils.text import join_and

if TYPE_CHECKING:
    from fpl_cli.models.season_month import SeasonMonth

# Where a month's winner came from: the awards feed, or the league's override
# in `settings.yaml` (`fines.draft_vouchers[].overrides`).
AwardSource = Literal["feed", "override"]
AWARD_SOURCES: frozenset[str] = frozenset({"feed", "override"})

# FPL names its months in English (`phases`), so they are matched against
# these rather than `calendar.month_name`, which follows the locale.
_CALENDAR_MONTHS = {
    name: number for number, name in enumerate((
        "january", "february", "march", "april", "may", "june",
        "july", "august", "september", "october", "november", "december",
    ), start=1)
}
# The first calendar month of a season's first year: August to December
# belong to the year it starts in, January to July to the next.
_SEASON_OPENING_MONTH = 8


def calendar_month_key(month: SeasonMonth, season_start_year: int) -> str | None:
    """The calendar month ("2026-09") FPL's month is named after, which is
    the month the Premier League dates its award by; None for a name that is
    no month."""
    number = _CALENDAR_MONTHS.get(month.name.strip().lower())
    if number is None:
        return None
    year = season_start_year if number >= _SEASON_OPENING_MONTH else season_start_year + 1
    return award_month_key(year, number)


# ---------------------------------------------------------------------------
# The record of what was announced
# ---------------------------------------------------------------------------


def award_record_dir(season: str, league_id: int) -> Path:
    """Where one draft league's announced awards live for one season.

    Under the ledger partition, resolved per call like `partition_dir`, so an
    `FPL_CLI_DATA_DIR` set after import is honoured.
    """
    return partition_dir(season, "draft", league_id) / "awards"


def award_record_path(season: str, league_id: int, month_key: str) -> Path:
    """The record of one month's Player of the Month."""
    return award_record_dir(season, league_id) / f"player-of-the-month-{month_key}.json"


@dataclass(frozen=True)
class AwardRecord:
    """One month's Player of the Month as the recap that announced him wrote it."""

    month_key: str
    # The winner as announced: the feed's id, which is his FPL `code`, or the
    # override's.
    code: int
    source: AwardSource
    # The gameweek whose recap announced him: the one recap that shows him.
    announced_gameweek: int
    # When fpl-cli first saw him, an aware UTC time.
    first_seen_at: datetime
    # The feed's entry as served; None when the override named him.
    entry: Mapping[str, Any] | None = None
    # Whether that recap withheld the voucher, the month's last gameweek
    # unable to say who held him. Settled, and the record rewritten, by the
    # first recap of the latest gameweek that can.
    withheld: bool = False

    def to_json(self) -> str:
        return json.dumps({
            "month": self.month_key,
            "code": self.code,
            "source": self.source,
            "announced_gameweek": self.announced_gameweek,
            "first_seen_at": self.first_seen_at.isoformat(),
            "entry": dict(self.entry) if self.entry is not None else None,
            "withheld": self.withheld,
        }, ensure_ascii=False)


def _positive_int(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else None


def read_award_record(season: str, league_id: int, month_key: str) -> tuple[AwardRecord | None, str | None]:
    """One month's record, or what is wrong with its file.

    `(None, None)` when there is no file. Read without probing first, so a
    permission problem is a problem with the file rather than an exception
    for the caller. A file this cannot read is never a missing one: writing
    over it would announce the month a second time.
    """
    path = award_record_path(season, league_id, month_key)
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None, None
    except (OSError, ValueError) as exc:
        return None, f"could not be read ({exc})"
    try:
        stored = json.loads(text)
    except ValueError as exc:
        return None, f"is not valid JSON ({exc})"
    if not isinstance(stored, dict):
        return None, "holds no record"
    code = _positive_int(stored.get("code"))
    gameweek = _positive_int(stored.get("announced_gameweek"))
    source = stored.get("source")
    entry = stored.get("entry")
    withheld = stored.get("withheld", False)
    try:
        first_seen = datetime.fromisoformat(str(stored.get("first_seen_at")))
    except ValueError:
        first_seen = None
    # `source` is checked as a string before `in` hashes it: a list or a
    # mapping there would raise `TypeError` rather than read as damage.
    if (
        stored.get("month") != month_key or code is None or gameweek is None
        or not isinstance(source, str) or source not in AWARD_SOURCES or first_seen is None
        or not (entry is None or isinstance(entry, dict)) or not isinstance(withheld, bool)
    ):
        return None, "is not a record this can read"
    return AwardRecord(
        month_key=month_key, code=code, source=cast(AwardSource, source), announced_gameweek=gameweek,
        first_seen_at=first_seen, entry=entry, withheld=withheld,
    ), None


def write_award_record(season: str, league_id: int, record: AwardRecord) -> str | None:
    """Write one month's record; the error when it could not be written.

    Called only where `read_award_record` found no file, or found a withheld
    one to settle, and never to change any other: a month is announced once,
    and a withheld voucher once more.
    """
    try:
        atomic_write_text(award_record_path(season, league_id, record.month_key), record.to_json())
    except (OSError, ValueError) as exc:
        return str(exc)
    return None


# ---------------------------------------------------------------------------
# Who held him
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class GameweekHolder:
    """Who held the month's winner for one of its gameweeks."""

    gameweek: int
    # The manager whose squad held him, or None: nobody held him where `gap`
    # is None, and the gameweek cannot say where it is set.
    manager_key: int | None = None
    manager_name: str | None = None
    # Why the gameweek cannot say who held him, as a clause a sentence can
    # follow "as" with: "GW7 was never captured".
    gap: str | None = None
    # His name as that squad recorded it, where one held him.
    player_name: str | None = None

    @property
    def known(self) -> bool:
        """Whether the gameweek says who held him, nobody included."""
        return self.manager_key is not None or self.gap is None


def _squad_gap(gameweek: int, unrecorded: Sequence[str], unidentified: Sequence[str]) -> str:
    """"no squad is recorded for Alice in GW5", "a player in Bob's GW5 squad
    could not be identified", or both."""
    parts: list[str] = []
    if unrecorded:
        parts.append(f"no squad is recorded for {capped_managers(unrecorded)} in GW{gameweek}")
    if unidentified:
        if len(unidentified) == 1:
            parts.append(f"a player in {unidentified[0]}'s GW{gameweek} squad could not be identified")
        else:
            parts.append(
                f"players in the GW{gameweek} squads of {capped_managers(unidentified)} could not be identified",
            )
    return join_and(parts)


def gameweek_holder(
    store: LeagueHistoryStore, gameweek: int, code: int, *,
    captured: Collection[int], partial_league: str | None = None,
) -> GameweekHolder:
    """Who held the player with `code` for one gameweek, as the ledger says.

    `partial_league` is why the recap read only part of the league, as the
    clause the monthly honours withhold with: the store records no gameweek's
    read as partial, so where it is set no gameweek can say nobody held him,
    though finding him still settles it.

    Never raises for a store problem: an unreadable gameweek cannot say, and
    says why (R4).
    """
    if gameweek not in captured:
        return GameweekHolder(gameweek, gap=f"GW{gameweek} was never captured")
    try:
        rows = store.resolved_gameweek(gameweek)
    except LeagueHistoryError as exc:
        store.log_unreadable(gameweek, exc, context="so who held the Player of the Month then is unknown")
        return GameweekHolder(gameweek, gap=f"GW{gameweek} could not be read")
    if not rows:
        return GameweekHolder(gameweek, gap=f"GW{gameweek} was never captured")
    unrecorded: list[str] = []
    unidentified: list[str] = []
    for key, row in sorted(rows.items(), key=lambda item: (item[1].manager_name, item[0])):
        if row.capture_status is CaptureStatus.UNKNOWN or not row.squad:
            unrecorded.append(row.manager_name)
            continue
        held = next((player for player in row.squad if player.code == code), None)
        if held is not None:
            return GameweekHolder(gameweek, manager_key=key, manager_name=row.manager_name, player_name=held.name)
        if any(player.code is None for player in row.squad):
            unidentified.append(row.manager_name)
    gaps = [partial_league] if partial_league is not None else []
    if unrecorded or unidentified:
        gaps.append(_squad_gap(gameweek, unrecorded, unidentified))
    return GameweekHolder(gameweek, gap=join_and(gaps) if gaps else None)


@dataclass(frozen=True)
class PlayerOfTheMonth:
    """One month's Player of the Month and what the ledger says of him."""

    # FPL's month, from the league's first gameweek where it began partway
    # through, as the monthly honours count it.
    month: SeasonMonth
    month_key: str
    code: int
    name: str
    source: AwardSource
    # One per gameweek of `month`, in order.
    holders: tuple[GameweekHolder, ...]
    # The gameweek whose recap announced him, or None where none has.
    announced_gameweek: int | None = None

    @property
    def closing(self) -> GameweekHolder:
        """Who held him for the month's last gameweek."""
        return self.holders[-1]

    @property
    def owner(self) -> GameweekHolder | None:
        """The voucher's holder: whoever held him for the month's last
        gameweek, or None where nobody did or nobody can say."""
        return self.closing if self.closing.manager_key is not None else None

    @property
    def unowned(self) -> bool:
        """Nobody held him for the month's last gameweek, so nobody holds a voucher."""
        return self.closing.manager_key is None and self.closing.gap is None

    @property
    def withheld_reason(self) -> str | None:
        """Why the voucher is withheld: the month's last gameweek cannot say
        who held him."""
        return None if self.closing.known else self.closing.gap

    @property
    def changed_hands(self) -> bool:
        """Whether the gameweeks that can say name more than one holder,
        nobody counting as one."""
        return len({holder.manager_key for holder in self.holders if holder.known}) > 1

    @property
    def unrecorded_gameweeks(self) -> list[int]:
        """The month's gameweeks that cannot say who held him."""
        return [holder.gameweek for holder in self.holders if not holder.known]

    def holding_runs(self) -> list[tuple[str | None, list[int]]]:
        """Who held him over the month as (manager name, gameweeks) runs, None
        for nobody, a change of holder starting a run. A gameweek that cannot
        say is left out and splits nothing: Alice either side of it is one
        run. Named as in the run's last gameweek, so a rename partway through
        reads as one manager."""
        runs: list[tuple[int | None, str | None, list[int]]] = []
        for holder in self.holders:
            if not holder.known:
                continue
            if runs and runs[-1][0] == holder.manager_key:
                key, _, gameweeks = runs[-1]
                runs[-1] = (key, holder.manager_name, [*gameweeks, holder.gameweek])
            else:
                runs.append((holder.manager_key, holder.manager_name, [holder.gameweek]))
        return [(name, gameweeks) for _, name, gameweeks in runs]


def month_holders(
    store: LeagueHistoryStore, month: SeasonMonth, code: int, *,
    captured: Collection[int], partial_league: str | None = None,
) -> tuple[GameweekHolder, ...]:
    """Who held the player with `code` for each of the month's gameweeks."""
    return tuple(
        gameweek_holder(store, gameweek, code, captured=captured, partial_league=partial_league)
        for gameweek in month.gameweeks
    )


def known_to_fpl(code: int, holders: Sequence[GameweekHolder], known_codes: Collection[int]) -> bool:
    """Whether FPL's bootstrap or the month's squads know the player with
    `code`: the guard against a feed whose ids have drifted off FPL's codes,
    or an override's typo, reading as "nobody held him"."""
    return code in known_codes or any(holder.manager_key is not None for holder in holders)


def player_name(
    code: int, holders: Sequence[GameweekHolder], *,
    bootstrap_names: Mapping[int, str], feed_name: str | None = None,
) -> str:
    """The name every surface gives him: FPL's own (`web_name`), the one the
    recap's squads and the editorial's sections already use; else the
    month's squads' record of it, for a player who has since left the
    bootstrap; else the feed's."""
    if name := bootstrap_names.get(code):
        return name
    recorded = next((holder.player_name for holder in reversed(holders) if holder.player_name), None)
    return recorded or feed_name or f"Player {code}"


@dataclass
class PlayerOfTheMonthFold:
    """Every completed month's Player of the Month the recap knows of, and why
    any could not be checked, for one draft league."""

    # In gameweek order.
    months: list[PlayerOfTheMonth] = field(default_factory=list)
    # Why a month that may have an award could not be checked, one clause
    # each, as the league reads it ("the Premier League's awards could not
    # be read"): the host, status or shape behind it is the warnings'.
    unchecked: list[str] = field(default_factory=list)

    def announced_at(self, gameweek: int) -> list[PlayerOfTheMonth]:
        """The months this gameweek's recap announces."""
        return [potm for potm in self.months if potm.announced_gameweek == gameweek]
