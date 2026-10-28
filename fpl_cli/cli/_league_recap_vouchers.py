"""The draft recap's Player of the Month voucher (issue #492).

`fpl_cli/services/league_history_vouchers.py` says who held a month's winner
and keeps the record of each announcement. This is the recap's side of it:
when to ask the Premier League's awards feed, which months a recap announces,
the lines every surface prints, and the warnings beside them.

A month is announced by the recap of the latest finished gameweek, never by a
replay of an older one: a replay run after the award was published would
otherwise claim it for a recap the league read weeks ago. That recap checks
every completed month of the season that has an award and no announcement, so
a late award or a skipped recap still produces the voucher, once, and it is
the only one that asks the feed, since a replay could announce nothing from
it. A month announced with its voucher withheld is announced again, settled,
by the first such recap whose ledger can say who held him.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx

from fpl_cli.api.premier_league import (
    BASE_URL,
    MONTH_WITHOUT_PLAYER_OF_THE_MONTH,
    PLAYER_OF_THE_MONTH,
    AwardsFeedError,
    MonthlyAward,
    award_month_key,
    award_name,
)
from fpl_cli.cli._context import error_console
from fpl_cli.cli._league_recap_data import RecapFineLine, forfeit_penalty
from fpl_cli.cli._league_recap_types import RecapFinePenalty, RecapVoucher
from fpl_cli.season import season_start_year
from fpl_cli.services.league_history_months import month_played_by_league
from fpl_cli.services.league_history_vouchers import (
    AwardRecord,
    AwardSource,
    PlayerOfTheMonth,
    PlayerOfTheMonthFold,
    award_record_path,
    calendar_month_key,
    known_to_fpl,
    month_holders,
    player_name,
    read_award_record,
    write_award_record,
)
from fpl_cli.utils.gameweek import format_gameweek_list
from fpl_cli.utils.text import join_and

if TYPE_CHECKING:
    from fpl_cli.cli._fines_config import FinesConfig, VoucherRule
    from fpl_cli.models.season_month import SeasonMonth
    from fpl_cli.services.league_history import LeagueHistoryStore

# Stable codes for the JSON payload's warnings, like every other recap code.
# A month that may have an award could not be checked: the feed was
# unreadable, named two winners, or named a player FPL does not know.
RECAP_WARNING_POTM_UNCHECKED = "player_of_the_month_unchecked"
# A month was announced but its record could not be written, so the next
# recap may announce it again.
RECAP_WARNING_POTM_UNRECORDED = "player_of_the_month_unrecorded"
# A month's record cannot be read, or the feed now names someone else.
RECAP_WARNING_POTM_RECORD_CONFLICT = "player_of_the_month_record_conflict"

# Fetches one season's monthly awards. Passed in, like the draft picks'
# `FetchDraftChoices`, so this module never opens a client of its own.
FetchMonthlyAwards = Callable[[], Awaitable[list[MonthlyAward]]]

# The awards feed's host, named in the warning a blocked request gets: a
# sandbox with restricted outbound access (Claude Code on the web) has to
# allow it.
AWARDS_HOST = httpx.URL(BASE_URL).host


@dataclass
class PlayerOfTheMonthCapture:
    """What one recap knows of the season's Players of the Month."""

    fold: PlayerOfTheMonthFold = field(default_factory=PlayerOfTheMonthFold)
    warnings: list[dict[str, str]] = field(default_factory=list)
    # Records this run wrote, reported under `--format json` too, where the
    # stderr notice is suppressed, so a run that commits the ledger sees them.
    saved: list[Path] = field(default_factory=list)


def _warn(warnings: list[dict[str, str]], code: str, message: str) -> None:
    """Record a warning and show it on stderr, as the ledger capture does."""
    warnings.append({"code": code, "message": message})
    error_console.print(f"[yellow]{message}[/yellow]")


def _feed_problem(exc: Exception) -> str:
    """Why the feed could not be read, as a parenthesis can carry it."""
    if isinstance(exc, httpx.HTTPStatusError):
        return f"HTTP {exc.response.status_code}"
    if isinstance(exc, httpx.TransportError):
        return (
            f"{type(exc).__name__}; where outbound hosts are restricted, as in Claude Code "
            f"on the web, {AWARDS_HOST} has to be allowed"
        )
    if isinstance(exc, AwardsFeedError):
        return f"{exc}, so its shape may have changed"
    if isinstance(exc, ValueError):
        return "the response is not JSON"
    return type(exc).__name__


def _unchecked(
    capture: PlayerOfTheMonthCapture, reason: str, *, detail: str | None = None, overridable: bool = True,
) -> None:
    """Say a month that may have an award could not be checked, on every
    surface. The Awards' "not checked" line, which goes to the league, gets
    `reason` alone; the warning, which is the user's, adds `detail`, the
    host, status or shape behind it. `overridable` is whether an override in
    settings.yaml would settle it."""
    capture.fold.unchecked.append(reason)
    advice = (
        "; if the feed stays wrong, set or correct the month's winner under the "
        "player-of-the-month voucher's `overrides` in settings.yaml" if overridable else ""
    )
    _warn(
        capture.warnings, RECAP_WARNING_POTM_UNCHECKED,
        f"Player of the Month not checked: {reason}{f' ({detail})' if detail else ''}. Nothing is "
        f"announced for it until a recap can check it{advice}.",
    )


def _record_announcement(
    capture: PlayerOfTheMonthCapture, store: LeagueHistoryStore, month: SeasonMonth, record: AwardRecord,
    *, settles: int | None = None,
) -> None:
    """Write the record of this recap's announcement, and say so. `settles`
    is the gameweek whose recap withheld the voucher this one settles."""
    path = award_record_path(store.season, store.league_id, record.month_key)
    error = write_award_record(store.season, store.league_id, record)
    if error is not None:
        _warn(
            capture.warnings, RECAP_WARNING_POTM_UNRECORDED,
            f"Could not record that this recap announced {month.label}'s Player of the Month at "
            f"{path}: {error}. The next recap may announce it again.",
        )
        return
    capture.saved.append(path)
    if settles is not None:
        said = (
            f"Recorded at {path} that this recap settles {month.label}'s Player of the Month "
            f"voucher, withheld by the GW{settles} recap."
        )
    elif record.withheld:
        said = (
            f"Recorded the announcement of {month.label}'s Player of the Month at {path}. Its "
            "voucher is withheld, so the first recap of the latest gameweek that can say who held "
            "him announces it again."
        )
    else:
        said = f"Recorded the announcement of {month.label}'s Player of the Month at {path}."
    error_console.print(f"[dim]{said}[/dim]")


async def capture_player_of_the_month(
    store: LeagueHistoryStore,
    months: Sequence[SeasonMonth],
    gameweek: int,
    *,
    voucher: VoucherRule,
    fetch_awards: FetchMonthlyAwards,
    may_announce: bool,
    league_start_gameweek: int = 1,
    bootstrap_names: Mapping[int, str] | None = None,
    partial_league: str | None = None,
    now: datetime | None = None,
) -> PlayerOfTheMonthCapture:
    """Every completed month's Player of the Month, announcing the new ones.

    `store` is the draft league's ledger, already holding this gameweek: the
    capture's own, so a damaged gameweek is reported once and each is read
    once. `may_announce` is whether this recap is the latest finished
    gameweek's, the one recap that announces a month nobody has, settles a
    withheld voucher, or asks the feed. `bootstrap_names` maps every FPL
    `code` in the main game's bootstrap to its `web_name`, which is both what
    the winner is called and the check that his code is real.
    `partial_league` is why the recap read only part of the league, as the
    monthly honours are handed it: a winner in no squad it read could be in
    one it did not.

    The feed is asked only when a completed month that has a Player of the
    Month (May has none) has neither a record nor an override. Never raises
    for an unreachable or reshaped feed or a file problem: each is a warning
    and the recap carries on (R4).
    """
    capture = PlayerOfTheMonthCapture()
    names = bootstrap_names or {}
    start_year = season_start_year(store.season)
    # May's award never comes, so waiting on it would send every recap from
    # the season's last gameweek on to the feed.
    awardless = award_month_key(start_year + 1, MONTH_WITHOUT_PLAYER_OF_THE_MONTH)
    completed: list[tuple[SeasonMonth, str]] = []
    for month in sorted(months, key=lambda month: month.first_gameweek):
        if not league_start_gameweek <= month.last_gameweek <= gameweek:
            continue
        key = calendar_month_key(month, start_year)
        if key is None:
            _unchecked(
                capture, f"FPL's month {month.name!r} is not a calendar month the award could name",
                overridable=False,
            )
            continue
        completed.append((month_played_by_league(month, league_start_gameweek), key))

    records: dict[str, AwardRecord] = {}
    unreadable: set[str] = set()
    for month, key in completed:
        record, problem = read_award_record(store.season, store.league_id, key)
        if problem is not None:
            unreadable.add(key)
            path = award_record_path(store.season, store.league_id, key)
            _warn(
                capture.warnings, RECAP_WARNING_POTM_RECORD_CONFLICT,
                f"The Player of the Month record for {month.label} at {path} {problem}. It is left "
                "as it is rather than written over, so the month is not announced again. Move it "
                f"aside (mv '{path}' '{path}.corrupt') and the next recap of the latest gameweek "
                "announces it afresh.",
            )
        elif record is not None:
            records[key] = record

    feed: dict[str, list[MonthlyAward]] | None = None
    if may_announce and any(
        key not in records and key not in unreadable and key not in voucher.overrides and key != awardless
        for _, key in completed
    ):
        try:
            awards = await fetch_awards()
        except (httpx.HTTPError, httpx.InvalidURL, OSError, ValueError) as exc:
            _unchecked(capture, "the Premier League's awards could not be read", detail=_feed_problem(exc))
        else:
            feed = {}
            for award in awards:
                if award.type == PLAYER_OF_THE_MONTH:
                    feed.setdefault(award.month_key, []).append(award)

    captured = set(store.captured_gameweeks())
    seen_at = now or datetime.now(tz=timezone.utc)
    for month, key in completed:
        if key in unreadable:
            continue
        record = records.get(key)
        override = voucher.overrides.get(key)
        served = feed.get(key, []) if feed is not None else []
        served_codes = sorted({award.code for award in served})
        if record is not None and record.source == "feed" and len(served_codes) == 1 and served_codes[0] != record.code:
            _warn(
                capture.warnings, RECAP_WARNING_POTM_RECORD_CONFLICT,
                f"The Premier League's feed now names player {served_codes[0]} as {month.label}'s "
                f"Player of the Month, where the GW{record.announced_gameweek} recap announced "
                f"player {record.code}. The announcement stands; if the feed is right, set the month "
                "under the player-of-the-month voucher's `overrides` in settings.yaml.",
            )

        source: AwardSource
        entry: Mapping[str, Any] | None
        if override is not None:
            code, source, entry = override, "override", None
        elif record is not None:
            code, source, entry = record.code, record.source, record.entry
        elif len(served_codes) > 1:
            _unchecked(capture, f"the Premier League names {len(served_codes)} Players of the Month for {month.label}")
            continue
        elif served:
            code, source, entry = served[0].code, "feed", served[0].entry
        else:
            # Not yet awarded, never asked (a replay, or May), or the feed
            # could not say, which `_unchecked` has already reported.
            continue

        holders = month_holders(store, month, code, captured=captured, partial_league=partial_league)
        # A code no record vouches for, a new winner or an override naming
        # another, must be one FPL or the month's squads know, so a typo can
        # neither announce a month nor settle a withheld one.
        if (record is None or record.code != code) and not known_to_fpl(code, holders, names):
            what = "override's player" if source == "override" else "Premier League's player id"
            _unchecked(capture, f"the winner for {month.label} matches no FPL player", detail=f"the {what} {code}")
            continue
        # Whether the month's last gameweek can say who held him. A month it
        # cannot is announced withheld, then announced again, settled, by the
        # first recap of the latest gameweek that can: an unknown row the
        # repair sweep refetches, or a gameweek backfilled, never costs the
        # voucher.
        settled = holders[-1].known
        announced = record.announced_gameweek if record is not None else None
        if may_announce and (record is None or (record.withheld and settled)):
            announced = gameweek
            _record_announcement(
                capture, store, month,
                AwardRecord(
                    month_key=key, code=code, source=source, announced_gameweek=gameweek,
                    first_seen_at=record.first_seen_at if record is not None else seen_at,
                    entry=entry, withheld=not settled,
                ),
                settles=record.announced_gameweek if record is not None else None,
            )
        capture.fold.months.append(PlayerOfTheMonth(
            month=month, month_key=key, code=code,
            name=player_name(
                code, holders, bootstrap_names=names,
                feed_name=award_name(entry.get("name")) if entry is not None else None,
            ),
            source=source, holders=holders, announced_gameweek=announced,
        ))
    return capture


# ---------------------------------------------------------------------------
# What each surface prints
# ---------------------------------------------------------------------------


def holding_line(potm: PlayerOfTheMonth) -> str:
    """Who held him over the month, for a month he changed hands in: "Held by
    Bob in GW3-4 and Alice in GW5, so the voucher goes to Alice", with
    "(GW4 not recorded)" for a gameweek that cannot say. Read off the ledger
    like the voucher itself, never a second source of who holds it."""
    runs = [f"{name or 'nobody'} in {format_gameweek_list(gameweeks)}" for name, gameweeks in potm.holding_runs()]
    line = f"Held by {join_and(runs)}"
    if unrecorded := potm.unrecorded_gameweeks:
        line += f" ({format_gameweek_list(unrecorded)} not recorded)"
    if (owner := potm.owner) is not None:
        line += f", so the voucher goes to {owner.manager_name}"
    return line


def player_of_the_month_line(potm: PlayerOfTheMonth) -> str:
    """One month's Player of the Month as the Awards print it, the console,
    the report and the editorial alike: "Groß for September (GW3-5), owned by
    Alice at the month's end", the holding line after it where he changed
    hands; "..., a free agent at the month's end, so there is no voucher"; or
    "Groß for September (GW3-5): the voucher is withheld, as no squad is
    recorded for Cy in GW5"."""
    head = f"{potm.name} for {potm.month.label}"
    if (owner := potm.owner) is not None:
        line = f"{head}, owned by {owner.manager_name} at the month's end"
    elif potm.unowned:
        line = f"{head}, a free agent at the month's end, so there is no voucher"
    else:
        return f"{head}: the voucher is withheld, as {potm.withheld_reason}"
    return f"{line}. {holding_line(potm)}" if potm.changed_hands else line


def player_of_the_month_unchecked_line(capture: PlayerOfTheMonthCapture) -> str | None:
    """What the Awards print when a month that may have an award could not be
    checked: "not checked, as the Premier League's awards could not be read".
    Never "no award": a month nobody could check is not an empty one. Read
    by the league, so the status or host behind it stays in the warning."""
    if not capture.fold.unchecked:
        return None
    return f"not checked, as {join_and(capture.fold.unchecked)}"


def recap_vouchers(capture: PlayerOfTheMonthCapture, gameweek: int, voucher: VoucherRule) -> list[RecapVoucher]:
    """The vouchers this gameweek's recap announces, one per month whose
    winner a manager held at its end, in month order."""
    vouchers: list[RecapVoucher] = []
    for potm in capture.fold.announced_at(gameweek):
        owner = potm.owner
        if owner is None or owner.manager_key is None or owner.manager_name is None:
            continue
        detail = f"{owner.manager_name} may hand out one fine to anyone they choose"
        if forfeit := voucher.forfeit:
            detail += f" ({forfeit})"
        vouchers.append(RecapVoucher(
            manager_key=owner.manager_key,
            manager_name=owner.manager_name,
            month=potm.month.name,
            player=potm.name,
            penalty=voucher.forfeit,
            lead=f"Player of the Month voucher ({potm.month.name})",
            detail=detail,
        ))
    return vouchers


def recap_voucher_lines(vouchers: Sequence[RecapVoucher]) -> list[RecapFineLine]:
    """The vouchers as Fines section lines, after the week's fines: the
    console and the saved report print the same ones."""
    return [RecapFineLine(lead=v["lead"], detail=v["detail"], grouped=False) for v in vouchers]


def voucher_penalties(
    vouchers: Sequence[RecapVoucher], fines_config: FinesConfig, penalties: Sequence[RecapFinePenalty],
) -> list[RecapFinePenalty]:
    """`penalties` with each voucher's forfeit added where it is not already
    there, with the league's phrasings for it, so the editorial names it by
    the same once-only rule as a fine's (#503)."""
    merged = list(penalties)
    named = {entry["penalty"] for entry in merged}
    for voucher in vouchers:
        forfeit = voucher["penalty"]
        if forfeit is None or forfeit in named:
            continue
        named.add(forfeit)
        merged.append(forfeit_penalty(fines_config, forfeit))
    return merged


def serialize_player_of_the_month(capture: PlayerOfTheMonthCapture, gameweek: int) -> dict[str, Any]:
    """Every completed month's Player of the Month, JSON-shaped, every week,
    like `monthly_honours`. `player` carries his FPL `code` and no `id`,
    and `announced` marks the months this recap announces."""
    def _holders(potm: PlayerOfTheMonth) -> list[dict[str, Any]]:
        return [
            {
                "gameweek": holder.gameweek,
                "manager_key": holder.manager_key,
                "manager_name": holder.manager_name,
                "known": holder.known,
                "gap": holder.gap,
            }
            for holder in potm.holders
        ]

    return {
        "unchecked": list(capture.fold.unchecked),
        "months": [
            {
                "month": potm.month_key,
                "name": potm.month.name,
                "first_gameweek": potm.month.first_gameweek,
                "last_gameweek": potm.month.last_gameweek,
                "player": {"code": potm.code, "name": potm.name},
                "source": potm.source,
                "owner": (
                    {"manager_key": owner.manager_key, "manager_name": owner.manager_name}
                    if (owner := potm.owner) is not None else None
                ),
                "voucher": potm.owner is not None,
                "withheld_reason": potm.withheld_reason,
                "holders": _holders(potm),
                "announced_gameweek": potm.announced_gameweek,
                "announced": potm.announced_gameweek == gameweek,
                "text": player_of_the_month_line(potm),
            }
            for potm in capture.fold.months
        ],
        "records_saved": [str(path) for path in capture.saved],
    }
