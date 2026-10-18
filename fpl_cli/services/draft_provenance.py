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

Where neither has them (a redraft was scheduled before the first snapshot was
taken, so the feed moved on first) the picks are lost, but who drafted whom is
not: each manager's squad at the draft's gameweek is the set they drafted,
corrected by undoing the moves made before that gameweek. The replay runs on
from there as it would from known picks and is held to the same two checks, so
the chains are as trustworthy; they just start from a draft step with no round
(``DraftStep.pick`` is None, ``DraftInForce.rounds_known`` False). Rounds
return on their own once a snapshot of the draft exists.

One helper for every draft command that says where a player came from: the
league recap's Standout Players section (#458), and the draft lines of ``fpl
player`` and ``fpl stats`` (#460), which also read the replay's free agents
(``DraftProvenance.released``) for the last exit of a player nobody holds.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Any, Literal

import httpx

from fpl_cli.api.fpl_draft import (
    DRAFT_TRADE_VETOED,
    is_accepted_transaction,
    is_processed_trade,
)
from fpl_cli.models.player import usable_code
from fpl_cli.services.league_history_drafts import (
    api_id,
    api_time,
    is_draft_lost,
    read_saved_choices,
)
from fpl_cli.services.league_history_drafts import (
    completed_drafts as recognised_completed_drafts,
)

if TYPE_CHECKING:
    from fpl_cli.api.fpl_draft import FPLDraftClient
    from fpl_cli.models.player import Player

# Read failures are logged at debug, never warning: fpl-cli configures no
# logging handlers, so a warning record reaches logging's lastResort handler
# and prints raw to stderr on top of the `problem` the CLI already shows.
logger = logging.getLogger(__name__)

# Link kinds in a player's chain. The first four brought him to an entry; a
# "release" is that entry letting him go to the free-agent pool.
StepKind = Literal["draft", "waiver", "free_agent", "trade", "release"]

# The transaction log's `kind` codes for the moves it records.
_TXN_KINDS: dict[str, StepKind] = {"w": "waiver", "f": "free_agent"}

# Problems named in a message before the rest are only counted.
_PROBLEMS_SHOWN = 2

# Squads read at once when a draft's picks have to be rebuilt from them.
_SQUAD_READS = 10


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
    # A draft step only: the pick, or None where the draft's picks are lost and
    # the entry is known from the squads alone: drafted by `entry`, in a round
    # nobody can now say.
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

    @property
    def drafted(self) -> DraftStep | None:
        """His pick in the draft in force, or None if nobody drafted him in it."""
        return _drafted(self.chain)


@dataclass(frozen=True)
class DraftFreeAgentHistory:
    """A player in nobody's squad as of the gameweek whom the history has held since the draft in force (#460).

    Drafted and let go, or signed and let go: ``chain`` runs oldest first as
    an owned player's does and always ends on the release that freed him. A
    player the history never touched (nobody drafted him in the draft in
    force, nobody has signed him since) has no history at all, here or among
    the owned, which is how a caller tells him apart. ``id`` and ``code`` as
    on ``DraftPlayerHistory``.
    """

    element: int
    id: int | None
    code: int | None
    chain: tuple[DraftStep, ...]

    @property
    def last_exit(self) -> DraftStep:
        """The release that put him back in the pool."""
        return self.chain[-1]

    @property
    def drafted(self) -> DraftStep | None:
        """His pick in the draft in force, or None if nobody drafted him in it."""
        return _drafted(self.chain)


def _drafted(chain: Sequence[DraftStep]) -> DraftStep | None:
    """The draft step a chain starts with, if it starts with one: only its first link can be."""
    return chain[0] if chain and chain[0].kind == "draft" else None


@dataclass(frozen=True)
class DraftInForce:
    """The draft whose picks started the chains: the latest completed one at the gameweek."""

    id: int
    gameweek: int
    # Whether the league had drafted more than once by then, so a label has to say which.
    redraft: bool
    # False when the draft's picks were lost and the chains start from the squads
    # at its gameweek: who drafted whom is known, the rounds are not.
    rounds_known: bool = True


@dataclass(frozen=True)
class DraftProvenance:
    """What the replay established for a league at one gameweek.

    ``players`` are the owned, by draft element id; ``released`` the players
    the history held and let go, whom nobody holds at the gameweek. Both are
    empty and ``problem`` says why whenever the history could not be shown to
    be complete.
    """

    gameweek: int
    draft: DraftInForce | None = None
    players: Mapping[int, DraftPlayerHistory] = field(default_factory=dict)
    released: Mapping[int, DraftFreeAgentHistory] = field(default_factory=dict)
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
    ``draft_squads`` are, for a held draft with picks in neither, every
    manager's squad at the draft's gameweek (draft element id to entry id),
    present only when all of them could be read; ``unread_squads`` the entries
    that could not, for the draft where some could not. ``drafts`` is None
    when the league's list of drafts could not be read, which is not the same
    as a league that has held none. ``choices_payload`` is the choices
    response itself, for a caller that also keeps it (the snapshot) to spare
    a second request; None unless it came back with a ``choices`` list.
    """

    drafts: Sequence[Mapping[str, Any]] | None
    choices: Sequence[Mapping[str, Any]] | None
    element_status: Sequence[Mapping[str, Any]] | None
    transactions: Sequence[Mapping[str, Any]] | None
    trades: Sequence[Mapping[str, Any]] | None
    saved_choices: Mapping[int, Sequence[Mapping[str, Any]]] = field(default_factory=dict)
    lost_drafts: frozenset[int] = frozenset()
    draft_squads: Mapping[int, Mapping[int, int]] = field(default_factory=dict)
    unread_squads: Mapping[int, tuple[int, ...]] = field(default_factory=dict)
    choices_payload: dict[str, Any] | None = None


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
        logger.debug("Could not read the draft league's %s: %s", what, e)
        return None
    if rows is None:
        logger.debug("The draft league's %s came back in a shape this client does not know", what)
    return rows


async def read_draft_logs(
    draft_client: FPLDraftClient,
    league_id: int,
    *,
    drafts: Sequence[Mapping[str, Any]] | None = None,
    transactions: Sequence[Mapping[str, Any]] | None = None,
    season: str | None = None,
    entry_ids: Sequence[int] | None = None,
    gameweek: int | None = None,
) -> DraftLogs:
    """Read what a replay needs: the draft's picks, the trades and the ownership list.

    *drafts* (``league.drafts`` from the league details) and *transactions*
    (the rows of ``get_league_transactions``) are what the league recap
    already holds; pass them to spare the requests, leave them out and they
    are read here. Never raises for a failed read: the log is None, the cause
    goes to the debug log, and ``replay_draft_history()`` says what the gap
    costs.

    Given a *season*, each held draft's saved picks are read from the
    ledger's snapshots (``league_history_drafts``) for the replay to fall back
    on when the feed has none for that draft. Without one, only the feed's
    picks are used.

    Given *entry_ids* (every manager's entry id), a draft with picks in
    neither the feed nor a snapshot has its managers' squads read at the
    draft's gameweek, one request each, for the replay to rebuild who drafted
    whom from. With a *gameweek* only the drafts a replay of it reads are
    asked about (the draft in force and the league's latest, which the whole
    history is checked through); without, every such draft. No squads are read
    when another log is missing, since the replay refuses without them.
    """
    async def _drafts() -> Sequence[Mapping[str, Any]] | None:
        try:
            details = await draft_client.get_league_details(league_id)
        except (httpx.HTTPError, ValueError) as e:
            logger.debug("Could not read the draft league's details: %s", e)
            return None
        league = details.get("league") if isinstance(details, Mapping) else None
        found = league.get("drafts") if isinstance(league, Mapping) else None
        return [d for d in found if isinstance(d, Mapping)] if isinstance(found, list) else None

    async def _choices() -> tuple[
        list[Mapping[str, Any]] | None, list[Mapping[str, Any]] | None, dict[str, Any] | None,
    ]:
        # One payload, two logs: the picks and the ownership list ride together.
        try:
            payload = await draft_client.get_draft_choices(league_id)
        except (httpx.HTTPError, ValueError) as e:
            logger.debug("Could not read the draft league's choices: %s", e)
            return None, None, None
        picks, owners = _rows(payload, "choices"), _rows(payload, "element_status")
        if picks is None or owners is None:
            logger.debug("The draft league's choices came back in a shape this client does not know")
        return picks, owners, payload if picks is not None and isinstance(payload, dict) else None

    async def _transactions() -> Sequence[Mapping[str, Any]] | None:
        return await _read_rows(
            "transactions", lambda: draft_client.get_league_transactions(league_id), "transactions",
        )

    async def _trades() -> Sequence[Mapping[str, Any]] | None:
        return await _read_rows("trades", lambda: draft_client.get_league_trades(league_id), "trades")

    (picks, owners, payload), known, found_txns, found_trades = await asyncio.gather(
        _choices(),
        _drafts() if drafts is None else _given([d for d in drafts if isinstance(d, Mapping)]),
        _transactions() if transactions is None else _given(transactions),
        _trades(),
    )
    held = _held_drafts(known or [])
    saved: dict[int, Sequence[Mapping[str, Any]]] = {}
    lost: set[int] = set()
    if season is not None:
        for draft in held:
            rows = read_saved_choices(season, league_id, draft["id"])
            if rows:
                saved[draft["id"]] = rows
            if is_draft_lost(season, league_id, draft["id"]):
                lost.add(draft["id"])
    squads: dict[int, Mapping[int, int]] = {}
    unread: dict[int, tuple[int, ...]] = {}
    # A replay with a log missing refuses before it looks at a squad, so
    # reading them would be a request per manager for nothing.
    complete = picks is not None and bool(owners) and found_txns is not None and found_trades is not None
    if entry_ids and held and complete:
        wanted = {held[-1]["id"]}
        if gameweek is None:
            wanted |= {d["id"] for d in held}
        elif (in_force := draft_in_force(known or [], gameweek)) is not None:
            wanted.add(in_force["id"])
        asked = [
            d for d in held
            if d["id"] in wanted
            and not (any(row.get("draft") == d["id"] for row in picks or []) or d["id"] in saved)
        ]
        gate = asyncio.Semaphore(_SQUAD_READS)
        reads = await asyncio.gather(*(_read_squads(draft_client, entry_ids, d["event"], gate) for d in asked))
        for draft, (squad, missing) in zip(asked, reads, strict=True):
            if squad is not None:
                squads[draft["id"]] = squad
            if missing:
                unread[draft["id"]] = missing
    return DraftLogs(
        drafts=known,
        choices=picks,
        element_status=owners,
        transactions=found_txns,
        trades=found_trades,
        saved_choices=saved,
        lost_drafts=frozenset(lost),
        draft_squads=squads,
        unread_squads=unread,
        choices_payload=payload,
    )


async def _read_squads(
    draft_client: FPLDraftClient, entry_ids: Sequence[int], gameweek: int, gate: asyncio.Semaphore,
) -> tuple[dict[int, int] | None, tuple[int, ...]]:
    """Every entry's squad at *gameweek* as draft element id to entry id, or
    None if any could not be read, and the entries that could not.

    A squad missing from the set would put its players in nobody's draft, so
    one unreadable squad spoils the whole set rather than leaving a hole.
    *gate* bounds the requests in flight across every draft read together.
    """
    async def one(entry: int) -> tuple[int, list[Mapping[str, Any]] | None]:
        async with gate:
            try:
                return entry, _rows(await draft_client.get_entry_picks(entry, gameweek), "picks")
            except (httpx.HTTPError, ValueError) as e:
                logger.debug("Could not read entry %s's GW%s squad: %s", entry, gameweek, e)
                return entry, None

    squad: dict[int, int] = {}
    missing: list[int] = []
    for entry, picks in await asyncio.gather(*(one(e) for e in entry_ids)):
        elements = [api_id(row.get("element")) for row in picks or []]
        # A player in two squads at once cannot be read either: nobody drafted him twice.
        if picks is None or None in elements or any(el in squad for el in elements if el is not None):
            missing.append(entry)
            continue
        squad.update({el: entry for el in elements if el is not None})
    return (None, tuple(missing)) if missing else (squad, ())


async def _given(rows: Sequence[Mapping[str, Any]]) -> Sequence[Mapping[str, Any]]:
    """Rows the caller already holds, as the awaitable the reads they stand in for are."""
    return rows


def _held_drafts(drafts: Sequence[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    """The drafts a league has held that a replay can place, earliest first.

    ``league_history_drafts.completed_drafts()`` picks the held ones; a replay
    also needs the gameweek a draft applies from and the time it was
    completed, to order every later move after it, so an entry without
    either is left out here (and reported as no draft in force).
    """
    held = [
        d for d in recognised_completed_drafts(drafts)
        if api_id(d.get("event")) is not None and api_time(d.get("draft_completed")) is not None
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
    # False when the chains start from squads, not from the draft's picks.
    rounds_known: bool = True

    def link(self, element: int, step: DraftStep) -> None:
        self.chains.setdefault(element, []).append(step)


def _no_picks_message(draft: Mapping[str, Any], logs: DraftLogs) -> str:
    """Why a draft has no picks to start from: neither source holds them."""
    message = f"neither FPL's choices list nor a saved snapshot holds the picks of the GW{draft['event']} draft"
    if any(
        (event := api_id(d.get("event"))) is not None and (ident := api_id(d.get("id"))) is not None
        and (event, ident) > (draft["event"], draft["id"])
        for d in logs.drafts or []
    ):
        message += "; the list serves a league's latest draft only, and this league has a later one scheduled or held"
    if draft["id"] in logs.lost_drafts:
        message += ", and a run already recorded that they were gone before any was saved"
    return message


_Move = tuple[tuple[datetime, int, int, int], Literal["transaction", "trade"], Mapping[str, Any]]


def _collect_moves(logs: DraftLogs, started: datetime, through: int | None, problems: list[str]) -> list[_Move]:
    """Every accepted transaction and processed trade after *started* up to
    gameweek *through*, oldest first, ordered by the time FPL recorded each
    (a transaction's ``added``, a trade's ``response_time``)."""
    # (when, trades after transactions at the same instant, index, row id).
    moves: list[_Move] = []
    for arrival, txn in enumerate(logs.transactions or []):
        if not is_accepted_transaction(txn):
            continue
        event = api_id(txn.get("event"))
        if event is None or (through is not None and event > through):
            continue
        at = api_time(txn.get("added"))
        if at is None:
            problems.append("an accepted transaction has no usable time")
            continue
        if at > started:
            ident = api_id(txn.get("id"))
            order = (at, 0, api_id(txn.get("index")) or 0, arrival if ident is None else ident)
            moves.append((order, "transaction", txn))
    for arrival, trade in enumerate(logs.trades or []):
        if not is_processed_trade(trade):
            continue
        event = api_id(trade.get("event"))
        if event is None or (through is not None and event > through):
            continue
        at = api_time(trade.get("response_time"))
        if at is None:
            problems.append("a processed trade has no usable time")
            continue
        if at > started:
            ident = api_id(trade.get("id"))
            moves.append(((at, 1, 0, arrival if ident is None else ident), "trade", trade))
    moves.sort(key=lambda move: move[0])
    return moves


def _start_from_picks(
    replay: _Replay, draft: Mapping[str, Any], picks: Sequence[Mapping[str, Any]], started: datetime,
) -> None:
    """Begin the replay from the draft's picks, each with its round and pick."""
    for row in picks:
        element, entry = api_id(row.get("element")), api_id(row.get("entry"))
        rnd, number = api_id(row.get("round")), api_id(row.get("pick"))
        if element is None or entry is None or rnd is None or number is None:
            replay.problems.append("a pick in the draft is missing its player, manager, round or position")
            continue
        if element in replay.owner:
            replay.problems.append("a player was picked twice in the same draft")
            continue
        replay.owner[element] = entry
        replay.link(element, DraftStep(
            kind="draft", entry=entry, gameweek=draft["event"], at=started,
            pick=DraftPick(
                draft_id=draft["id"], draft_gameweek=draft["event"], round=rnd, pick=number,
                overall=api_id(row.get("index")), auto=row.get("was_auto") is True,
            ),
        ))


def _last_touched(trade: Mapping[str, Any]) -> datetime | None:
    """The later of a trade's offer and response times, or None if neither is usable."""
    times = [t for t in (api_time(trade.get("offer_time")), api_time(trade.get("response_time"))) if t is not None]
    return max(times) if times else None


def _start_from_squads(
    replay: _Replay, draft: Mapping[str, Any], logs: DraftLogs, started: datetime, moves: Sequence[_Move],
) -> None:
    """Begin the replay from each manager's drafted set, rebuilt from their squads.

    For a draft whose picks neither the feed nor a snapshot holds. A squad at
    the draft's gameweek is what was drafted plus the moves made since the
    draft and before that gameweek, so undoing those, newest first, gives the
    set drafted: a player who arrived in one was not drafted by that manager,
    and the player who left in the same move was. Every undone move is checked
    against the squads as the forward replay checks every applied one.

    The result is only as good as the logs it undoes, and the two checks that
    vouch for a replay from known picks cannot catch a missing move here (the
    start is derived from the same logs), so what could hide one refuses
    instead: a pre-gameweek trade, made since the draft, in a state nobody has
    seen move players. (A trades log that could not be read refuses earlier,
    for every replay.)
    """
    event: int = draft["event"]
    gap = _no_picks_message(draft, logs)
    squad = logs.draft_squads.get(draft["id"])
    if squad is None:
        unread = logs.unread_squads.get(draft["id"], ())
        replay.problems.append(
            f"{gap}; the GW{event} squads of {len(unread)} manager{'s' if len(unread) != 1 else ''} "
            "could not be read to rebuild them" if unread else gap,
        )
        return
    for trade in logs.trades or []:
        trade_event = api_id(trade.get("event"))
        touched = _last_touched(trade)
        if (
            trade_event is not None and trade_event <= event
            and not is_processed_trade(trade) and trade.get("state") != DRAFT_TRADE_VETOED
            # One last touched before this draft was held belongs to an earlier
            # draft's era and cannot have moved anyone since; one with no usable
            # time cannot be placed, so it counts.
            and (touched is None or touched > started)
        ):
            replay.problems.append(
                f"{gap}; and a GW{trade_event} trade is in a state this client has not seen move players, "
                "so the moves made before it cannot all be undone",
            )
            return

    drafted = dict(squad)
    for _, what, row in reversed([m for m in moves if m[2]["event"] <= event]):
        if what == "transaction":
            _unapply_transaction(drafted, row, replay.problems)
        else:
            _unapply_trade(drafted, row, replay.problems)
    for element, entry in drafted.items():
        replay.owner[element] = entry
        replay.link(element, DraftStep(kind="draft", entry=entry, gameweek=event, at=started))
    replay.rounds_known = False


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

    It starts from the draft's picks, the feed's first and the snapshot's
    when the feed has none for this draft, and from the squads at its
    gameweek when neither does (``_start_from_squads``).
    """
    replay = _Replay()
    started = api_time(draft.get("draft_completed"))
    if started is None:
        replay.problems.append("the draft in force has no completion time")
        return replay

    # The feed first, since it is the draft's own record; the snapshot only
    # when the feed has none for this draft (empty, or serving another).
    picks = [row for row in logs.choices or [] if row.get("draft") == draft["id"]]
    if not picks:
        picks = [row for row in logs.saved_choices.get(draft["id"], []) if row.get("draft") == draft["id"]]
    moves = _collect_moves(logs, started, through, replay.problems)
    if picks:
        _start_from_picks(replay, draft, picks, started)
    else:
        _start_from_squads(replay, draft, logs, started, moves)
        if replay.problems:
            return replay

    for (at, *_), what, row in moves:
        if what == "transaction":
            _apply_transaction(replay, row, at)
        else:
            _apply_trade(replay, row, at)
    return replay


def _transaction_parts(txn: Mapping[str, Any]) -> tuple[int, int, int | None, StepKind] | None:
    """A transaction's entry, incoming and outgoing player and kind, or None if
    it is not a waiver or free-agent move this client reads."""
    entry, element_in = api_id(txn.get("entry")), api_id(txn.get("element_in"))
    element_out = api_id(txn.get("element_out"))
    code = txn.get("kind")
    kind = _TXN_KINDS.get(code) if isinstance(code, str) else None
    if kind is None or entry is None or element_in is None or (
        txn.get("element_out") is not None and element_out is None
    ):
        return None
    return entry, element_in, element_out, kind


def _trade_parts(trade: Mapping[str, Any]) -> tuple[int, int, list[tuple[int, int]]] | None:
    """A trade's offering and receiving entries and its (element_out, element_in)
    pairs, or None if any is missing."""
    offered, received = api_id(trade.get("offered_entry")), api_id(trade.get("received_entry"))
    items = trade.get("tradeitem_set")
    pairs: list[tuple[int, int]] = []
    for item in items if isinstance(items, list) else []:
        out = api_id(item.get("element_out")) if isinstance(item, Mapping) else None
        into = api_id(item.get("element_in")) if isinstance(item, Mapping) else None
        if out is None or into is None:
            return None
        pairs.append((out, into))
    return None if offered is None or received is None or not pairs else (offered, received, pairs)


def _apply_transaction(replay: _Replay, txn: Mapping[str, Any], at: datetime) -> None:
    """An accepted claim: ``element_in`` joins the entry's squad, ``element_out`` leaves it."""
    event: int = txn["event"]
    parts = _transaction_parts(txn)
    if parts is None:
        replay.problems.append(f"a GW{event} transaction is not a waiver or free-agent move this client reads")
        return
    entry, element_in, element_out, kind = parts
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
    event: int = trade["event"]
    parts = _trade_parts(trade)
    if parts is None:
        replay.problems.append(f"a GW{event} trade is missing its managers or players")
        return
    offered, received, pairs = parts
    # Checked whole before any of it is applied, as the league applies it.
    if any(replay.owner.get(out) != offered or replay.owner.get(into) != received for out, into in pairs):
        replay.problems.append(f"a GW{event} trade moved a player his giver did not hold")
        return
    for out, into in pairs:
        replay.owner[out], replay.owner[into] = received, offered
        replay.link(out, DraftStep(kind="trade", entry=received, gameweek=event, at=at, from_entry=offered))
        replay.link(into, DraftStep(kind="trade", entry=offered, gameweek=event, at=at, from_entry=received))


def _unapply_transaction(squads: dict[int, int], txn: Mapping[str, Any], problems: list[str]) -> None:
    """Undo an accepted claim on *squads* (element to entry): the player it brought
    in leaves the entry's squad and the one it released is back in it."""
    event: int = txn["event"]
    parts = _transaction_parts(txn)
    if parts is None:
        problems.append(f"a GW{event} transaction is not a waiver or free-agent move this client reads")
        return
    entry, element_in, element_out, kind = parts
    label = f"a GW{event} {kind.replace('_', ' ')}"
    if squads.get(element_in) != entry:
        problems.append(f"{label} brought in a player his entry's squad did not have")
    else:
        del squads[element_in]
    if element_out is not None:
        if element_out in squads:
            problems.append(f"{label} released a player who was still in a squad")
        else:
            squads[element_out] = entry


def _unapply_trade(squads: dict[int, int], trade: Mapping[str, Any], problems: list[str]) -> None:
    """Undo a processed trade on *squads*: each player goes back to the entry that gave him."""
    event: int = trade["event"]
    parts = _trade_parts(trade)
    if parts is None:
        problems.append(f"a GW{event} trade is missing its managers or players")
        return
    offered, received, pairs = parts
    if any(squads.get(out) != received or squads.get(into) != offered for out, into in pairs):
        problems.append(f"a GW{event} trade moved a player the squads do not show with its receiver")
        return
    for out, into in pairs:
        squads[out], squads[into] = offered, received


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
      ``element_status`` says everyone is. A move missing from the logs or
      one misordered shows up here. This check is on the whole history, so an
      earlier *gameweek* is held to it too.
    - **Against the squads of the gameweek** (*observed*: draft element id to
      owner entry id, as the recap reads them from each manager's picks).
      The replay stopped at the gameweek must put every one of those players
      with the same entry.

    Any disagreement, any move that does not fit the squads it applies to, or
    any log that could not be read (the trades included, since a trade that
    left no mark on ownership shows up in neither check) returns no players
    and a ``problem``,
    which a caller reports and then says nothing about where players came
    from. *main_players* is ``match_draft_to_main``'s output (draft element
    id to main-game player), which gives each history its main-game ``id``
    and ``code``.
    """
    if logs.drafts is None:
        return DraftProvenance(gameweek, problem="the league's list of drafts could not be read")
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
    if logs.trades is None:
        # Not left to the ownership check: trades that net out (a player sent
        # away and sent back) leave every owner where the draft put them and
        # pass both checks, yet the latest link of each is a trade.
        return DraftProvenance(gameweek, problem="the league's trades could not be read")
    if not logs.element_status:
        return DraftProvenance(gameweek, problem="FPL sent no ownership list to check the history against")

    whole = _replay(held[-1], logs, through=None)
    if whole.problems:
        return DraftProvenance(gameweek, problem=_problem_text(whole.problems))
    owners_now = {
        element: row.get("owner") for row in logs.element_status if (element := api_id(row.get("element"))) is not None
    }
    off = sum(1 for element, owner in owners_now.items() if whole.owner.get(element) != owner)
    off += sum(1 for element in whole.owner if element not in owners_now)
    if off:
        return DraftProvenance(
            gameweek,
            problem=(
                f"the draft, waiver and trade history ends with {off} "
                f"player{'s' if off != 1 else ''} owned differently from FPL's own ownership list"
                " (if a waiver run or trade has only just been processed, re-run once FPL has caught up)"
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
    # Every chain of a player nobody holds ends on his release: only a release
    # takes a player out of the replay's ownership.
    released = {
        element: DraftFreeAgentHistory(
            element=element,
            id=main.id if (main := (main_players or {}).get(element)) else None,
            code=usable_code(main.code) if main else None,
            chain=tuple(chain),
        )
        for element, chain in replay.chains.items()
        if element not in replay.owner
    }
    return DraftProvenance(
        gameweek,
        draft=DraftInForce(
            id=in_force["id"], gameweek=in_force["event"],
            redraft=sum(1 for d in held if d["event"] <= gameweek) > 1,
            rounds_known=replay.rounds_known,
        ),
        players=players,
        released=released,
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
    entry_ids: Sequence[int] | None = None,
) -> DraftProvenance:
    """Read a league's logs and replay them: ``read_draft_logs()`` then ``replay_draft_history()``.

    Costs two requests (the choices and the trades), plus the league details
    and the transaction log for whichever of *drafts* and *transactions* the
    caller does not already hold, and reads the saved picks under *season*
    when given. Given *entry_ids*, a draft whose picks neither the feed nor a
    snapshot holds is rebuilt from the managers' squads at its gameweek, one
    request each (``DraftInForce.rounds_known`` is then False). Never raises
    for a failed read.
    """
    logs = await read_draft_logs(
        draft_client, league_id, drafts=drafts, transactions=transactions, season=season,
        entry_ids=entry_ids, gameweek=gameweek,
    )
    return replay_draft_history(logs, gameweek, main_players=main_players, observed=observed)
