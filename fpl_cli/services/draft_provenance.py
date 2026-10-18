"""How each owned draft player came to his manager, replayed from the league's own history (#458).

A draft league keeps three logs of who held whom: the draft's picks, the
transaction log of waiver and free-agent moves, and the trades. None of them
says who owns a player now, but replaying them in time order does, and FPL
also publishes the answer (``element_status``), so the replay is checked
against it: on the public leagues probed, 38 of 38 reproduced the league's
live ownership exactly, and stopped at an earlier gameweek reproduced every
manager's squad for that week (80 of 80).

That agreement is the whole basis for trusting a label. A replay that
disagrees with FPL's ownership list is missing a move or misordering one, so
every chain it produced is suspect, and ``DraftProvenance.problem`` says so
instead of handing back chains: callers fall back to saying nothing about
where a player came from, never to a label from a history known to be
incomplete. The same goes for logs that cannot be read, and for a draft whose
picks nobody has: FPL serves a league's latest draft only (see
``FPLDraftClient.get_draft_choices()``), so a held draft's picks are read from
the feed first and, when it has none for that draft, from the snapshot
``league-recap --draft`` saved while it did (``league_history_drafts``, #461).

One helper for every draft command that says where a player came from; the
league recap's Standout Players section is the first (#458), and ``fpl
player``, ``fpl stats`` and ``fpl waivers`` follow (#460).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Literal

import httpx

from fpl_cli.api.fpl_draft import is_accepted_transaction, is_processed_trade
from fpl_cli.models.player import usable_code
from fpl_cli.services.league_history_drafts import (
    completed_drafts as recognised_completed_drafts,
)
from fpl_cli.services.league_history_drafts import (
    lost_draft_path,
    read_saved_choices,
)

if TYPE_CHECKING:
    from fpl_cli.api.fpl_draft import FPLDraftClient
    from fpl_cli.models.player import Player

logger = logging.getLogger(__name__)

# Link kinds in a player's chain. The first four brought him to an entry; a
# "release" is that entry letting him go to the free-agent pool.
StepKind = Literal["draft", "waiver", "free_agent", "trade", "release"]

# The transaction log's `kind` codes for the moves it records.
_TXN_KINDS: dict[str, StepKind] = {"w": "waiver", "f": "free_agent"}

# Problems named in a message before the rest are only counted.
_PROBLEMS_SHOWN = 2


@dataclass(frozen=True)
class DraftPick:
    """The pick a draft step records."""

    draft_id: int
    draft_gameweek: int
    round: int
    pick: int
    # The pick's place in the whole draft, round after round; None where FPL sent none.
    overall: int | None
    # Made by the draft's auto-pick, not the manager, after the clock ran out.
    auto: bool


@dataclass(frozen=True)
class DraftStep:
    """One link in a player's chain of custody.

    ``entry`` is the entry the link brought him to, or for a "release" the one
    that let him go. ``gameweek`` is the gameweek the move first applied to
    (the draft's own for a pick); ``at`` is when FPL recorded it, which is
    what orders the chain.
    """

    kind: StepKind
    entry: int
    gameweek: int
    at: datetime
    # A trade only: the entry he came from.
    from_entry: int | None = None
    # A draft pick only.
    pick: DraftPick | None = None


@dataclass(frozen=True)
class DraftPlayerHistory:
    """An owned player, his owner as of the gameweek, and everything that put him there.

    ``element`` is the draft game's id; ``id`` and ``code`` are the main
    game's, through ``match_draft_to_main`` (never a name join), None for a
    player it could not match. ``chain`` runs oldest first from the draft in
    force, or from the first move that put him in a squad, and always ends on
    the link that brought him to ``owner``: a pick dropped and signed again by
    the same entry ends on the signing.
    """

    element: int
    id: int | None
    code: int | None
    owner: int
    chain: tuple[DraftStep, ...]

    @property
    def latest(self) -> DraftStep:
        """The most recent link: how he came to his owner."""
        return self.chain[-1]


@dataclass(frozen=True)
class DraftInForce:
    """The draft whose picks started the chains: the latest completed one at the gameweek."""

    id: int
    gameweek: int
    # Whether the league had drafted more than once by then, so a label has to say which.
    redraft: bool


@dataclass(frozen=True)
class DraftProvenance:
    """What the replay established for a league at one gameweek.

    ``players`` is empty and ``problem`` says why whenever the history could
    not be shown to be complete.
    """

    gameweek: int
    draft: DraftInForce | None = None
    players: Mapping[int, DraftPlayerHistory] = field(default_factory=dict)
    problem: str | None = None

    @property
    def available(self) -> bool:
        return self.problem is None


@dataclass(frozen=True)
class DraftLogs:
    """The three logs and the ownership list, as read; None for one that could not be.

    ``choices`` and ``element_status`` are the feed's, always today's.
    ``saved_choices`` are the pick rows of each held draft's snapshot, by the
    draft's id, read only for a draft the caller asked about; ``lost_drafts``
    the held drafts a run recorded as gone before any snapshot was taken.
    """

    drafts: Sequence[Mapping[str, Any]]
    choices: Sequence[Mapping[str, Any]] | None
    element_status: Sequence[Mapping[str, Any]] | None
    transactions: Sequence[Mapping[str, Any]] | None
    trades: Sequence[Mapping[str, Any]] | None
    saved_choices: Mapping[int, Sequence[Mapping[str, Any]]] = field(default_factory=dict)
    lost_drafts: frozenset[int] = frozenset()


def _rows(payload: object, key: str) -> list[Mapping[str, Any]] | None:
    """The list under *key* in an API payload, or None for any other shape."""
    if not isinstance(payload, Mapping):
        return None
    rows = payload.get(key)
    return [r for r in rows if isinstance(r, Mapping)] if isinstance(rows, list) else None


async def _read_rows(
    what: str, read: Callable[[], Awaitable[object]], key: str,
) -> list[Mapping[str, Any]] | None:
    """One log's rows, or None (and a warning) when the request or its payload failed."""
    try:
        rows = _rows(await read(), key)
    except (httpx.HTTPError, ValueError) as e:
        logger.warning("Could not read the draft league's %s: %s", what, e)
        return None
    if rows is None:
        logger.warning("The draft league's %s came back in a shape this client does not know", what)
    return rows


async def read_draft_logs(
    draft_client: FPLDraftClient,
    league_id: int,
    *,
    drafts: Sequence[Mapping[str, Any]] | None = None,
    transactions: Sequence[Mapping[str, Any]] | None = None,
    season: str | None = None,
) -> DraftLogs:
    """Read what a replay needs: the draft's picks, the trades and the ownership list.

    *drafts* (``league.drafts`` from the league details) and *transactions*
    (the rows of ``get_league_transactions``) are what the league recap
    already holds; pass them to spare the requests, leave them out and they
    are read here. Never raises for a failed read: the log is None, a
    warning goes to the log, and ``replay_draft_history()`` says what the gap
    costs.

    Given a *season*, each held draft's saved picks are read from the
    ledger's snapshots (``league_history_drafts``) for the replay to fall back
    on when the feed has none for that draft. Without one, only the feed's
    picks are used.
    """
    async def _drafts() -> Sequence[Mapping[str, Any]] | None:
        try:
            details = await draft_client.get_league_details(league_id)
        except (httpx.HTTPError, ValueError) as e:
            logger.warning("Could not read the draft league's details: %s", e)
            return None
        league = details.get("league") if isinstance(details, Mapping) else None
        found = league.get("drafts") if isinstance(league, Mapping) else None
        return [d for d in found if isinstance(d, Mapping)] if isinstance(found, list) else None

    async def _choices() -> tuple[list[Mapping[str, Any]] | None, list[Mapping[str, Any]] | None]:
        # One payload, two logs: the picks and the ownership list ride together.
        try:
            payload = await draft_client.get_draft_choices(league_id)
        except (httpx.HTTPError, ValueError) as e:
            logger.warning("Could not read the draft league's choices: %s", e)
            return None, None
        picks, owners = _rows(payload, "choices"), _rows(payload, "element_status")
        if picks is None or owners is None:
            logger.warning("The draft league's choices came back in a shape this client does not know")
        return picks, owners

    async def _transactions() -> Sequence[Mapping[str, Any]] | None:
        return await _read_rows(
            "transactions", lambda: draft_client.get_league_transactions(league_id), "transactions",
        )

    async def _trades() -> Sequence[Mapping[str, Any]] | None:
        return await _read_rows("trades", lambda: draft_client.get_league_trades(league_id), "trades")

    (picks, owners), found_drafts, found_txns, found_trades = await asyncio.gather(
        _choices(),
        _drafts() if drafts is None else _given(drafts),
        _transactions() if transactions is None else _given(transactions),
        _trades(),
    )
    known = found_drafts if found_drafts is not None else []
    saved: dict[int, Sequence[Mapping[str, Any]]] = {}
    lost: set[int] = set()
    if season is not None:
        for draft in _held_drafts(known):
            rows = read_saved_choices(season, league_id, draft["id"])
            if rows:
                saved[draft["id"]] = rows
            if lost_draft_path(season, league_id, draft["id"]).is_file():
                lost.add(draft["id"])
    return DraftLogs(
        drafts=known,
        choices=picks,
        element_status=owners,
        transactions=found_txns,
        trades=found_trades,
        saved_choices=saved,
        lost_drafts=frozenset(lost),
    )


async def _given(rows: Sequence[Mapping[str, Any]]) -> Sequence[Mapping[str, Any]]:
    """Rows the caller already holds, as the awaitable the reads they stand in for are."""
    return rows


def _when(value: object) -> datetime | None:
    """An API timestamp as an aware datetime, or None for anything else."""
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _int(value: object) -> int | None:
    """*value* if it is an int (a bool is not one), else None."""
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _held_drafts(drafts: Sequence[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    """The drafts a league has held that a replay can place, earliest first.

    ``league_history_drafts.completed_drafts()`` picks the held ones; a replay
    also needs the gameweek a draft applies from and the time it was
    completed, to order every later move after it, so an entry without
    either is left out here (and reported as no draft in force).
    """
    held = [
        d for d in recognised_completed_drafts(drafts)
        if _int(d.get("event")) is not None and _when(d.get("draft_completed")) is not None
    ]
    return sorted(held, key=lambda d: (d["event"], d["id"]))


def draft_in_force(drafts: Sequence[Mapping[str, Any]], gameweek: int) -> Mapping[str, Any] | None:
    """The latest completed draft whose gameweek is at or before *gameweek*, else None.

    A redraft takes over from the gameweek it is scheduled for, so a replay
    of an earlier gameweek reads the draft that was in force then.
    """
    in_force = [d for d in _held_drafts(drafts) if d["event"] <= gameweek]
    return in_force[-1] if in_force else None


@dataclass
class _Replay:
    """A replay's end state: who holds whom, the chains behind it, and what did not fit."""

    owner: dict[int, int] = field(default_factory=dict)
    chains: dict[int, list[DraftStep]] = field(default_factory=dict)
    problems: list[str] = field(default_factory=list)

    def link(self, element: int, step: DraftStep) -> None:
        self.chains.setdefault(element, []).append(step)


def _no_picks_message(draft: Mapping[str, Any], logs: DraftLogs) -> str:
    """Why a draft has no picks to start from: neither source holds them."""
    message = f"neither FPL's choices list nor a saved snapshot holds the picks of the GW{draft['event']} draft"
    if any(
        (event := _int(d.get("event"))) is not None and (ident := _int(d.get("id"))) is not None
        and (event, ident) > (draft["event"], draft["id"])
        for d in logs.drafts
    ):
        message += "; the list serves a league's latest draft only, and this league has a later one scheduled or held"
    if draft["id"] in logs.lost_drafts:
        message += ", and a run already recorded that they were gone before any was saved"
    return message


def _replay(
    draft: Mapping[str, Any],
    logs: DraftLogs,
    *,
    through: int | None,
) -> _Replay:
    """Replay *draft*, then every accepted move and processed trade after it, in time order.

    Moves are ordered by the time FPL recorded them (a transaction's
    ``added``, a trade's ``response_time``) and only those after the draft
    completed count, so a redraft starts every squad afresh. *through* stops
    the log at a gameweek, by the ``event`` each row carries, for a replay of
    an earlier week; None runs it to the end.

    Every move is checked against the state it applies to: a release by an
    entry that did not hold the player, a claim on one somebody holds, a trade
    of a player the giver did not have. None happens in a complete history,
    so each one is recorded as a problem.
    """
    replay = _Replay()
    started = _when(draft.get("draft_completed"))
    if started is None:
        replay.problems.append("the draft in force has no completion time")
        return replay

    draft_event: int = draft["event"]
    # The feed first, since it is the draft's own record; the snapshot only
    # when the feed has none for this draft (empty, or serving another).
    picks = [row for row in logs.choices or [] if row.get("draft") == draft["id"]]
    if not picks:
        picks = [row for row in logs.saved_choices.get(draft["id"], []) if row.get("draft") == draft["id"]]
    if not picks:
        replay.problems.append(_no_picks_message(draft, logs))
        return replay
    for row in picks:
        element, entry = _int(row.get("element")), _int(row.get("entry"))
        rnd, number = _int(row.get("round")), _int(row.get("pick"))
        if element is None or entry is None or rnd is None or number is None:
            replay.problems.append("a pick in the draft is missing its player, manager, round or position")
            continue
        if element in replay.owner:
            replay.problems.append("a player was picked twice in the same draft")
            continue
        replay.owner[element] = entry
        replay.link(element, DraftStep(
            kind="draft", entry=entry, gameweek=draft_event, at=started,
            pick=DraftPick(
                draft_id=draft["id"], draft_gameweek=draft_event, round=rnd, pick=number,
                overall=_int(row.get("index")), auto=row.get("was_auto") is True,
            ),
        ))

    # (when, trades after transactions at the same instant, index, row id), then the row.
    moves: list[tuple[tuple[datetime, int, int, int], Literal["transaction", "trade"], Mapping[str, Any]]] = []
    for arrival, txn in enumerate(logs.transactions or []):
        if not is_accepted_transaction(txn):
            continue
        event = _int(txn.get("event"))
        if event is None or (through is not None and event > through):
            continue
        at = _when(txn.get("added"))
        if at is None:
            replay.problems.append("an accepted transaction has no usable time")
            continue
        if at > started:
            ident = _int(txn.get("id"))
            order = (at, 0, _int(txn.get("index")) or 0, arrival if ident is None else ident)
            moves.append((order, "transaction", txn))
    for arrival, trade in enumerate(logs.trades or []):
        if not is_processed_trade(trade):
            continue
        event = _int(trade.get("event"))
        if event is None or (through is not None and event > through):
            continue
        at = _when(trade.get("response_time"))
        if at is None:
            replay.problems.append("a processed trade has no usable time")
            continue
        if at > started:
            ident = _int(trade.get("id"))
            moves.append(((at, 1, 0, arrival if ident is None else ident), "trade", trade))
    moves.sort(key=lambda move: move[0])

    for (at, *_), what, row in moves:
        if what == "transaction":
            _apply_transaction(replay, row, at)
        else:
            _apply_trade(replay, row, at)
    return replay


def _apply_transaction(replay: _Replay, txn: Mapping[str, Any], at: datetime) -> None:
    """An accepted claim: ``element_in`` joins the entry's squad, ``element_out`` leaves it."""
    entry, element_in = _int(txn.get("entry")), _int(txn.get("element_in"))
    element_out = _int(txn.get("element_out"))
    code = txn.get("kind")
    kind = _TXN_KINDS.get(code) if isinstance(code, str) else None
    event: int = txn["event"]
    if kind is None or entry is None or element_in is None or (
        txn.get("element_out") is not None and element_out is None
    ):
        replay.problems.append(f"a GW{event} transaction is not a waiver or free-agent move this client reads")
        return
    if element_out is not None:
        if replay.owner.get(element_out) != entry:
            replay.problems.append(f"a GW{event} {kind.replace('_', ' ')} released a player his entry did not hold")
        else:
            del replay.owner[element_out]
            replay.link(element_out, DraftStep(kind="release", entry=entry, gameweek=event, at=at))
    if element_in in replay.owner:
        replay.problems.append(f"a GW{event} {kind.replace('_', ' ')} claimed a player who was already held")
        return
    replay.owner[element_in] = entry
    replay.link(element_in, DraftStep(kind=kind, entry=entry, gameweek=event, at=at))


def _apply_trade(replay: _Replay, trade: Mapping[str, Any], at: datetime) -> None:
    """A processed trade: each item's ``element_out`` goes from the offering entry to the
    receiving one, its ``element_in`` the other way."""
    offered, received = _int(trade.get("offered_entry")), _int(trade.get("received_entry"))
    items = trade.get("tradeitem_set")
    event: int = trade["event"]
    pairs: list[tuple[int, int]] = []
    for item in items if isinstance(items, list) else []:
        out = _int(item.get("element_out")) if isinstance(item, Mapping) else None
        into = _int(item.get("element_in")) if isinstance(item, Mapping) else None
        if out is None or into is None:
            pairs = []
            break
        pairs.append((out, into))
    if offered is None or received is None or not pairs:
        replay.problems.append(f"a GW{event} trade is missing its managers or players")
        return
    # Checked whole before any of it is applied, as the league applies it.
    if any(replay.owner.get(out) != offered or replay.owner.get(into) != received for out, into in pairs):
        replay.problems.append(f"a GW{event} trade moved a player his giver did not hold")
        return
    for out, into in pairs:
        replay.owner[out], replay.owner[into] = received, offered
        replay.link(out, DraftStep(kind="trade", entry=received, gameweek=event, at=at, from_entry=offered))
        replay.link(into, DraftStep(kind="trade", entry=offered, gameweek=event, at=at, from_entry=received))


def _problem_text(problems: Sequence[str]) -> str:
    """The first few problems in a sentence, the rest counted."""
    shown = "; ".join(problems[:_PROBLEMS_SHOWN])
    rest = len(problems) - _PROBLEMS_SHOWN
    return f"{shown}; and {rest} more" if rest > 0 else shown


def replay_draft_history(
    logs: DraftLogs,
    gameweek: int,
    *,
    main_players: Mapping[int, Player] | None = None,
    observed: Mapping[int, int] | None = None,
) -> DraftProvenance:
    """Each owned player's owner as of *gameweek*, and the chain that put him there.

    Replays the draft in force at *gameweek* and every accepted transaction
    and processed trade after it, up to that gameweek, and checks the history
    twice before believing it:

    - **Against FPL's ownership list.** The league's whole history, to the
      latest completed draft and the latest move, must end exactly where
      ``element_status`` says everyone is. A move missing from the logs, one
      misordered, or a read that failed (a trade that happened but whose log
      did not come back) all show up here. This check is on the whole
      history, so an earlier *gameweek* is held to it too.
    - **Against the squads of the gameweek** (*observed*: draft element id to
      owner entry id, as the recap reads them from each manager's picks).
      The replay stopped at the gameweek must put every one of those players
      with the same entry.

    Any disagreement, any move that does not fit the squads it applies to, or
    any log that could not be read returns no players and a ``problem``,
    which a caller reports and then says nothing about where players came
    from. *main_players* is ``match_draft_to_main``'s output (draft element
    id to main-game player), which gives each history its main-game ``id``
    and ``code``.
    """
    held = _held_drafts(logs.drafts)
    if not held:
        return DraftProvenance(gameweek, problem="no draft had been held in the league")
    in_force = draft_in_force(logs.drafts, gameweek)
    if in_force is None:
        return DraftProvenance(gameweek, problem=f"no draft had been held by GW{gameweek}")
    if logs.choices is None:
        return DraftProvenance(gameweek, problem="the draft's choices could not be read")
    if logs.transactions is None:
        return DraftProvenance(gameweek, problem="the league's transactions could not be read")
    if not logs.element_status:
        return DraftProvenance(gameweek, problem="FPL sent no ownership list to check the history against")

    whole = _replay(held[-1], logs, through=None)
    if whole.problems:
        return DraftProvenance(gameweek, problem=_problem_text(whole.problems))
    owners_now = {
        element: row.get("owner") for row in logs.element_status if (element := _int(row.get("element"))) is not None
    }
    off = sum(1 for element, owner in owners_now.items() if whole.owner.get(element) != owner)
    off += sum(1 for element in whole.owner if element not in owners_now)
    if off:
        unread = " (the trades could not be read)" if logs.trades is None else ""
        return DraftProvenance(
            gameweek,
            problem=(
                f"the draft, waiver and trade history ends with {off} "
                f"player{'s' if off != 1 else ''} owned differently from FPL's own ownership list{unread}"
            ),
        )

    replay = _replay(in_force, logs, through=gameweek)
    if replay.problems:
        return DraftProvenance(gameweek, problem=_problem_text(replay.problems))
    if observed is not None:
        off = sum(1 for element, entry in observed.items() if replay.owner.get(element) != entry)
        if off:
            return DraftProvenance(
                gameweek,
                problem=(
                    f"the history stopped at GW{gameweek} puts {off} player{'s' if off != 1 else ''} "
                    "in a different squad from the one the gameweek recorded"
                ),
            )

    players = {
        element: DraftPlayerHistory(
            element=element,
            id=main.id if (main := (main_players or {}).get(element)) else None,
            code=usable_code(main.code) if main else None,
            owner=owner,
            chain=tuple(replay.chains[element]),
        )
        for element, owner in replay.owner.items()
    }
    return DraftProvenance(
        gameweek,
        draft=DraftInForce(
            id=in_force["id"], gameweek=in_force["event"],
            redraft=sum(1 for d in held if d["event"] <= gameweek) > 1,
        ),
        players=players,
    )


async def get_draft_provenance(
    draft_client: FPLDraftClient,
    league_id: int,
    gameweek: int,
    *,
    main_players: Mapping[int, Player] | None = None,
    observed: Mapping[int, int] | None = None,
    drafts: Sequence[Mapping[str, Any]] | None = None,
    transactions: Sequence[Mapping[str, Any]] | None = None,
    season: str | None = None,
) -> DraftProvenance:
    """Read a league's logs and replay them: ``read_draft_logs()`` then ``replay_draft_history()``.

    Costs two requests (the choices and the trades), plus the league details
    and the transaction log for whichever of *drafts* and *transactions* the
    caller does not already hold, and reads the saved picks under *season*
    when given. Never raises for a failed read.
    """
    logs = await read_draft_logs(
        draft_client, league_id, drafts=drafts, transactions=transactions, season=season,
    )
    return replay_draft_history(logs, gameweek, main_players=main_players, observed=observed)
