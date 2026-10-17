"""Reading FPL's post-gameweek processing feed for `league-recap` and `status`."""

from __future__ import annotations

import asyncio
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

# The feed only ever adds a caveat, so it gets far less patience than the
# clients' own 30s: a stalled feed must not hold up the command it qualifies.
FEED_TIMEOUT_SECONDS = 5.0


async def fetch_gameweek_status(client: GameweekStatusClient) -> GameweekStatus | None:
    """The feed, or None when it cannot be read in time.

    Never raises for an unreachable, slow or reshaped feed: losing it must
    not cost the command it qualifies. A `pydantic.ValidationError` is a
    `ValueError`, as is a body that is not JSON.
    """
    try:
        return await asyncio.wait_for(client.get_gameweek_status(), FEED_TIMEOUT_SECONDS)
    except (httpx.HTTPError, ValueError, TimeoutError) as exc:
        logger.debug("Could not read FPL's gameweek status feed: %r", exc)
        return None


async def fetch_draft_gameweek_status() -> GameweekStatus | None:
    """The draft game's feed, from a client opened for it alone."""
    from fpl_cli.api.fpl_draft import FPLDraftClient

    async with FPLDraftClient() as client:
        return await fetch_gameweek_status(client)


def _all_of(values: Sequence[bool | None]) -> bool | None:
    """False as soon as one says no; True only when every one says yes."""
    if any(value is False for value in values):
        return False
    if not values or any(value is None for value in values):
        return None
    return True


def settled_flags(
    statuses: Sequence[GameweekStatus | None], gameweek: int,
) -> tuple[bool | None, bool | None]:
    """`(bonus_confirmed, league_tables_updated)` for `gameweek` across games.

    Each flag is False as soon as one game's feed says no, True only when
    every game's feed says yes, and None otherwise: some feed could not
    answer (unread, no day for this gameweek, an unrecognised state) and
    none said no. A user who plays both games asks "are my league tables up
    to date?", and one game still updating is a no.
    """
    return (
        _all_of([s.bonus_confirmed(gameweek) if s else None for s in statuses]),
        _all_of([s.league_tables_updated(gameweek) if s else None for s in statuses]),
    )
