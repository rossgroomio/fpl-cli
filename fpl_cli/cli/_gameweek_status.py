"""Reading FPL's post-gameweek processing feed for `league-recap` and `status`."""

from __future__ import annotations

import logging
from collections.abc import Sequence
from typing import TYPE_CHECKING, Protocol

import httpx

if TYPE_CHECKING:
    from fpl_cli.models.gameweek_status import GameweekStatus

    class GameweekStatusClient(Protocol):
        """Either game's client: both serve the feed under the same method."""

        async def get_gameweek_status(self) -> GameweekStatus: ...

logger = logging.getLogger(__name__)


async def fetch_gameweek_status(client: GameweekStatusClient) -> GameweekStatus | None:
    """The feed, or None when it cannot be read.

    Never raises for an unreachable or reshaped feed: it only ever adds a
    caveat, so losing it must not cost the command it qualifies. A
    `pydantic.ValidationError` is a `ValueError`, as is a body that is not
    JSON.
    """
    try:
        return await client.get_gameweek_status()
    except (httpx.HTTPError, ValueError) as exc:
        logger.debug("Could not read FPL's gameweek status feed: %s", exc)
        return None


async def fetch_draft_gameweek_status() -> GameweekStatus | None:
    """The draft game's feed, from a client opened for it alone.

    Opening the client sits inside the degradation too, so a draft API that
    cannot be reached costs the caveat and nothing else.
    """
    from fpl_cli.api.fpl_draft import FPLDraftClient

    try:
        async with FPLDraftClient() as client:
            return await fetch_gameweek_status(client)
    except httpx.HTTPError as exc:
        logger.debug("Could not reach the draft API for its gameweek status feed: %s", exc)
        return None


def gameweek_still_processing(status: GameweekStatus | None, gameweek: int) -> str | None:
    """Why FPL has not finished processing `gameweek`, or None.

    None also when the feed could not be read or covers another gameweek:
    the feed moves on to the next gameweek at its deadline, so it says
    nothing about an earlier one, and an unread feed is not evidence of a
    stale table.
    """
    if status is None or status.gameweek != gameweek:
        return None
    if not status.bonus_confirmed:
        return "bonus is not yet confirmed for every match day"
    if not status.league_tables_updated:
        return "league tables are still updating"
    return None


def settled_flags(
    statuses: Sequence[GameweekStatus | None], gameweek: int,
) -> tuple[bool | None, bool | None]:
    """`(bonus_confirmed, league_tables_updated)` for `gameweek` across games.

    Each flag is True only when every game's feed says so, False as soon as
    one says otherwise, and None when no feed said either way for this
    gameweek (unread, or covering another one). A user who plays both games
    asks "are my league tables up to date?", and one game still updating
    is a no.
    """
    readings = [s for s in statuses if s is not None and s.gameweek == gameweek]
    unknown = len(readings) < len(statuses)

    def _combine(values: list[bool]) -> bool | None:
        if not all(values):
            return False
        return None if unknown or not values else True

    return (
        _combine([s.bonus_confirmed for s in readings]),
        _combine([s.league_tables_updated for s in readings]),
    )
