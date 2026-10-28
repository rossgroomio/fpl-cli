"""Manager and Dud of the Month, folded out of the league-history ledger (issue #425).

FPL divides the season into months (`SeasonMonth`, read off the bootstrap's
`phases`), and a league's page has a table for each. This module gives every
completed month its Manager of the Month, the month's top scorer, and its Dud
of the Month, the lowest, and collects them into the season's honours list.

Derived, never stored, the way the season fines tally is
(`league_history_fines.py`): every run sums the ledger's rows over each
month's gameweeks afresh. A gameweek re-captured or repaired since takes its
month's awards with it rather than leaving a stored award to disagree with
the ledger, a change to how a month is scored applies to the whole season at
once, and because the ledger is season-partitioned and outlives the rollover,
a past season's honours stay computable from its own partition. The sweep
reads the gameweeks the fines tally and the notes pack have usually read
already, off the same memoized store.

Three rules this module exists to enforce:

- **A month is scored the way FPL's own month table scores it.** Each
  gameweek's points net of its transfer-cost hit, except the month's first
  gameweek, whose hit FPL's month tables do not charge at all, to that month
  or the one before, whatever its size. Checked on 2026-10-28 against the top
  100 of the public Overall league's September and October tables (33 totals
  that included a hit) and three classic mini-leagues' own tables for August
  to October (31 more): every one fits that rule, and no other rule fits them
  all. It leaves a month table short of the season table by exactly that
  hit, so it reads as a quirk of FPL's tables rather than a rule anyone
  chose. It is mirrored because the month tables are what a league checks
  these awards against, and the recap's classic cross-check against them
  (`league_month_table_disagrees`) is what would flag FPL changing it, and
  withholds the month's awards when it does (`MonthlyHonours.withholding`).
  This is neither the season win and loss counts' measure (each gameweek net
  of its hit, `league_history_counters._net_gw_points`) nor the weekly
  awards' (`use_net_points`). Draft has no hits, so a draft month is a plain
  sum.
- **A gap withholds both awards.** A month where any manager has no score for
  one of its gameweeks -- an unknown capture row (R19), a gameweek never
  captured or unreadable, no row at all -- names nobody, and says why. So
  does a classic row with no hit recorded past the month's first gameweek,
  which would otherwise read as no hit taken. Summing what is there would
  make the manager with the missing points the Dud by default, and a missing
  score could have won the month: an un-ruled gameweek is not an innocent
  one, the rule the fines tally and the streaks already follow.
- **A month's table is the league as the month closed.** Its managers are the
  ones with a row in its last gameweek, as FPL's own month table lists the
  league's members: one who left partway through is not in it, and one who
  joined partway through has a gap in it. A month the league began partway
  through counts from the league's first gameweek, as FPL's table for that
  league does (checked on a league begun at GW2, whose August is GW2 alone),
  and that gameweek is the month's first for the hit rule. No league checked
  had a hit there, so that part is unverified and the cross-check is its
  guard. A month that ended before the league began is left out altogether.
  A league the recap reads only in part (classic standings past one page of
  50) has every month withheld: the members it never read are a gap the
  store cannot see, and the Dud is almost always one of them.

Ties share either award, competition-ranked (`competition_rank`) as the
league table is (1, 2, 2, 4).
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING

from fpl_cli.constants import DETAIL_CAP
from fpl_cli.models.league_history import CaptureStatus, LeagueFormat, LeagueHistoryRow
from fpl_cli.services.league_history import LeagueHistoryError, LeagueHistoryStore
from fpl_cli.utils.gameweek import format_gameweek_list
from fpl_cli.utils.ranking import competition_rank
from fpl_cli.utils.text import join_and

if TYPE_CHECKING:
    from collections.abc import Collection, Mapping, Sequence

    from fpl_cli.models.season_month import SeasonMonth

# The places Manager of the Month names: the winner and the rest of the top
# three.
_PODIUM = 3


# ---------------------------------------------------------------------------
# Shapes
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MonthTotal:
    """One manager's points for one month, and their place on it."""

    manager_key: int
    # As the month closed: an honours list is a record of who won each month
    # under the name they had then, not a running total like the fines.
    manager_name: str
    points: int
    # Competition-ranked, highest first: managers level on points share the
    # place and the next total skips the ones they took (1, 2, 2, 4).
    rank: int


@dataclass(frozen=True)
class MonthAwards:
    """One completed month's Manager and Dud of the Month, or why it has none."""

    month: SeasonMonth
    # Every manager in the month's table, best first. Empty when withheld.
    totals: list[MonthTotal] = field(default_factory=list)
    # Why neither award is given, as a clause a surface can follow a colon
    # with ("GW8 was never captured"). None when both are.
    withheld_reason: str | None = None

    @property
    def awarded(self) -> bool:
        return self.withheld_reason is None

    @property
    def managers_of_the_month(self) -> list[MonthTotal]:
        """Everyone on the month's top total."""
        return [total for total in self.totals if total.rank == 1]

    @property
    def top_three(self) -> list[MonthTotal]:
        """Everyone placed in the top three, so a tie on third names them all."""
        return [total for total in self.totals if total.rank <= _PODIUM]

    @property
    def duds_of_the_month(self) -> list[MonthTotal]:
        """Everyone on the month's lowest total, or nobody when that is
        everyone: a league level on points would put the same managers under
        both headings, so the Dud is dropped and the Manager of the Month
        kept, as for Best and Worst Captain (issue #145)."""
        if not self.totals:
            return []
        lowest = min(total.points for total in self.totals)
        duds = [total for total in self.totals if total.points == lowest]
        return [] if len(duds) == len(self.totals) else duds


@dataclass(frozen=True)
class MonthlyHonours:
    """Every month completed so far, each with its awards or why it has none,
    for one (season, format, league) partition."""

    season: str
    fpl_format: LeagueFormat
    league_id: int
    through_gameweek: int
    # In gameweek order, a month left out only when it ended before the
    # league began.
    months: list[MonthAwards] = field(default_factory=list)

    def ending_at(self, gameweek: int) -> MonthAwards | None:
        """The month this gameweek closed, whose awards are this week's news."""
        return next((awards for awards in self.months if awards.month.last_gameweek == gameweek), None)

    def withholding(self, month: SeasonMonth, reason: str) -> MonthlyHonours:
        """This list with `month`'s awards withheld for `reason`: what the
        recap's cross-check does when FPL's own table for the month
        disagrees with the ledger's totals, so every surface reading the
        list after it says the same."""
        return replace(self, months=[
            MonthAwards(month=awards.month, withheld_reason=reason) if awards.month.id == month.id else awards
            for awards in self.months
        ])


# ---------------------------------------------------------------------------
# Scoring one row
# ---------------------------------------------------------------------------


def _month_points(row: LeagueHistoryRow | None, *, opens_month: bool) -> int | str:
    """A row's points towards its month, or what it lacks for them.

    `gross_points` less the hit, but not in the month's first gameweek, whose
    hit FPL's month table never charges. "score" when the row records none:
    an unknown row's figure is never read, since R19's row carries the league
    table's number for a manager the capture never reached, which is not a
    score this module can vouch for. "transfer cost" when a classic row past
    the month's first gameweek has no hit recorded: classic records one every
    gameweek, zero included, so None there is a hit nobody read (the coarse
    tier's `event_transfers_cost` absent) rather than none taken, and it could
    have cost the month. Draft has no hits, so its None is a plain zero.
    """
    if row is None or row.capture_status is CaptureStatus.UNKNOWN or row.gross_points is None:
        return "score"
    if opens_month:
        return row.gross_points
    if row.transfer_cost is None:
        return "transfer cost" if row.fpl_format == "classic" else row.gross_points
    return row.gross_points - row.transfer_cost


# ---------------------------------------------------------------------------
# Why a month is withheld
# ---------------------------------------------------------------------------


def _store_gap_reason(uncaptured: list[int], unreadable: list[int]) -> str:
    """"GW8 was never captured", "GW3-4 were never captured and GW5 could not be read"."""
    parts: list[str] = []
    if uncaptured:
        verb = "was" if len(uncaptured) == 1 else "were"
        parts.append(f"{format_gameweek_list(uncaptured)} {verb} never captured")
    if unreadable:
        parts.append(f"{format_gameweek_list(unreadable)} could not be read")
    return join_and(parts)


def _score_gap_reason(missing: Mapping[str, Mapping[int, list[int]]], names: Mapping[int, str]) -> str:
    """"no score is recorded for Alice in GW4 and Bob in GW3-4", with "no
    transfer cost is recorded for ..." beside it when a classic hit is what
    a row lacks, the managers past `DETAIL_CAP` in each counted rather than
    named."""
    clauses: list[str] = []
    for lacking, gaps in sorted(missing.items()):
        ordered = sorted(gaps.items(), key=lambda item: (names[item[0]], item[0]))
        named = [f"{names[key]} in {format_gameweek_list(gameweeks)}" for key, gameweeks in ordered[:DETAIL_CAP]]
        omitted = len(ordered) - len(named)
        if omitted:
            named.append(f"{omitted} other manager{'s' if omitted != 1 else ''}")
        clauses.append(f"no {lacking} is recorded for {join_and(named)}")
    return join_and(clauses)


# ---------------------------------------------------------------------------
# One month
# ---------------------------------------------------------------------------


def _ranked(totals: Mapping[int, int], names: Mapping[int, str]) -> list[MonthTotal]:
    """Best first, a tie ordered by name so the list reads the same every run."""
    ordered = sorted(totals, key=lambda key: (-totals[key], names[key], key))
    return [
        MonthTotal(
            manager_key=key,
            manager_name=names[key],
            points=totals[key],
            rank=competition_rank(totals[key], totals.values()),
        )
        for key in ordered
    ]


def month_played_by_league(month: SeasonMonth, league_start_gameweek: int) -> SeasonMonth:
    """The part of the month the league played: all of it, or from its first
    gameweek when it began partway through, as FPL's own table for the league
    counts it (a league begun at GW2 has an August of GW2 alone). The Player
    of the Month voucher reads the month the same way (#492)."""
    if month.first_gameweek >= league_start_gameweek:
        return month
    return month.model_copy(update={"first_gameweek": league_start_gameweek})


def _month_awards(
    store: LeagueHistoryStore, month: SeasonMonth, *, captured: Collection[int],
) -> MonthAwards:
    resolved: dict[int, dict[int, LeagueHistoryRow]] = {}
    uncaptured: list[int] = []
    unreadable: list[int] = []
    for gameweek in month.gameweeks:
        if gameweek not in captured:
            uncaptured.append(gameweek)
            continue
        try:
            rows = store.resolved_gameweek(gameweek)
        except LeagueHistoryError as exc:
            store.log_unreadable(gameweek, exc, context="so its month's awards are withheld")
            unreadable.append(gameweek)
            continue
        if not rows:
            uncaptured.append(gameweek)
            continue
        resolved[gameweek] = rows
    if uncaptured or unreadable:
        return MonthAwards(month=month, withheld_reason=_store_gap_reason(uncaptured, unreadable))

    closing = resolved[month.last_gameweek]
    names = {key: row.manager_name for key, row in closing.items()}
    totals: dict[int, int] = {}
    # What each manager lacks, by gameweek, under what it is ("score").
    missing: dict[str, dict[int, list[int]]] = {}
    for key in closing:
        points = 0
        for gameweek in month.gameweeks:
            scored = _month_points(
                resolved[gameweek].get(key), opens_month=gameweek == month.first_gameweek,
            )
            if isinstance(scored, str):
                missing.setdefault(scored, {}).setdefault(key, []).append(gameweek)
            else:
                points += scored
        totals[key] = points
    if missing:
        return MonthAwards(month=month, withheld_reason=_score_gap_reason(missing, names))
    return MonthAwards(month=month, totals=_ranked(totals, names))


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def build_monthly_honours(
    store: LeagueHistoryStore,
    months: Sequence[SeasonMonth],
    through_gameweek: int,
    *,
    league_start_gameweek: int = 1,
    partial_league: str | None = None,
) -> MonthlyHonours:
    """Every month `through_gameweek` has completed, with its awards or why
    it has none, folded out of one partition's ledger.

    `league_start_gameweek` is the league's own first scored gameweek, which
    the recap knows and the store does not: a month that ended before it is
    left out, and one it fell inside starts there.

    `partial_league` is why the recap read only part of the league, as a
    reason clause ("the recap reads only the first 50 of the league's
    members"), and withholds every month with it. The store records no
    gameweek's read as partial, so a league read in part now has its earlier
    months withheld too, any from before it outgrew one page included.

    Never raises for a store problem: an unreadable gameweek withholds its
    month and is named in the reason, as R4 asks of every ledger reader.
    """
    completed = sorted(
        (
            month_played_by_league(month, league_start_gameweek) for month in months
            if league_start_gameweek <= month.last_gameweek <= through_gameweek
        ),
        key=lambda month: month.first_gameweek,
    )
    if partial_league is not None:
        awards = [MonthAwards(month=month, withheld_reason=partial_league) for month in completed]
    else:
        captured = set(store.captured_gameweeks())
        awards = [_month_awards(store, month, captured=captured) for month in completed]
    return MonthlyHonours(
        season=store.season,
        fpl_format=store.fpl_format,
        league_id=store.league_id,
        through_gameweek=through_gameweek,
        months=awards,
    )
