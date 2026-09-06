"""Which club a player was on the books at, in a gameweek already played.

The bootstrap only knows where a player is *today*, so anything replaying a
past gameweek stamps today's club onto a squad that had someone else's
(issue #169). `_league_recap_history._carry_recorded_identity` covers that
wherever a detailed row was already recorded, by keeping what the gameweek
remembered. Two cases have nothing to remember -- a first capture, and a
coarse row being upgraded, which stores headline numbers and no squad at all
-- and the second is the documented `--backfill-detail` workflow, so a
mid-season first run writes today's clubs across the whole season (issue #177).

This module answers the same question from the gameweek instead of from a
prior row, so it needs neither. FPL's live payload carries an `explain` entry
per club fixture for every player on that club's books at the time, and each
entry names its fixture; the gameweek's own fixtures give that fixture's two
clubs. A player turns out for one club, so his club that gameweek is in the
intersection of his fixtures' club pairs:

- Two entries (a double) intersect to exactly one club. Settled outright.
- One entry leaves the fixture's two clubs. Today's club being one of them is
  taken as "he has not moved"; today's club being *neither* is proof that he
  has, and `element-summary/{id}/` is then worth one call to say where he was.
- No entries (his club blanked) leaves nothing to intersect and no answer.

Club only. Name and position are not derivable from fixtures at all, so they
stay carry-forward-only and a first capture still records today's.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping, Sequence

    from fpl_cli.models.fixture import Fixture
    from fpl_cli.models.player import Player
    from fpl_cli.models.team import Team

logger = logging.getLogger(__name__)

# The same permit the recap's own per-manager fetches take out. A drift is
# rare by construction, so this bounds a burst rather than a stream.
_DETAIL_CONCURRENCY = 5


class PlayerDetailClient(Protocol):
    """The one FPLClient method the moved-player lookup calls."""

    async def get_player_detail(self, player_id: int, /) -> dict[str, Any]: ...


def resolve_player_fixtures(
    live_data: dict[str, Any], fixtures: Sequence[Fixture],
) -> dict[int, frozenset[int]] | None:
    """Per player, the fixtures the gameweek recorded for the club he was at.

    Read off the same `explain` signal as `resolve_players_with_fixture`, and
    declining on the same terms: an unstarted gameweek returns no elements,
    a part-played one has no `explain` yet for a fixture still to kick off,
    and a finished gameweek where nobody has one is a payload that has not
    populated rather than a league-wide blank. None in all of those, so the
    caller falls back to today's clubs exactly as before.

    A player whose club had no fixture is absent from the mapping: there is
    nothing to intersect for him and no club to recover.
    """
    if not fixtures or not all(f.finished for f in fixtures):
        return None
    known = {f.id for f in fixtures}
    elements = live_data.get("elements") or []
    by_player: dict[int, frozenset[int]] = {}
    for element in elements:
        player_id = element.get("id")
        if player_id is None:
            continue
        played = frozenset(
            fixture_id
            for entry in element.get("explain") or ()
            if (fixture_id := entry.get("fixture")) in known
        )
        if played:
            by_player[player_id] = played
    return by_player or None


def _club_pairs(fixtures: Sequence[Fixture]) -> dict[int, frozenset[int]]:
    """Each fixture's two clubs, keyed by fixture id."""
    return {f.id: frozenset((f.home_team_id, f.away_team_id)) for f in fixtures}


def narrow_clubs(
    player_fixtures: Mapping[int, frozenset[int]], fixtures: Sequence[Fixture],
) -> dict[int, frozenset[int]]:
    """Per player, the clubs his own fixtures narrow him down to.

    One club for a double gameweek, two for a single. Never empty: every
    fixture id here came from `_club_pairs`'s own gameweek, and a player's
    entries all belong to one club, so the intersection always holds it.
    """
    pairs = _club_pairs(fixtures)
    candidates: dict[int, frozenset[int]] = {}
    for player_id, fixture_ids in player_fixtures.items():
        clubs: frozenset[int] | None = None
        for fixture_id in fixture_ids:
            pair = pairs.get(fixture_id)
            if pair is None:
                continue
            clubs = pair if clubs is None else (clubs & pair)
        if clubs:
            candidates[player_id] = clubs
    return candidates


def settle_clubs(
    candidates: Mapping[int, frozenset[int]], clubs_now: Mapping[int, int],
) -> tuple[dict[int, int], frozenset[int]]:
    """Split the candidates into the clubs settled and the players still open.

    Settled outright where the intersection is a single club, and settled as
    today's club where today's club is one of the two a single fixture leaves
    -- a player at one of the clubs whose fixture it was has, on the evidence
    the gameweek carries, not moved.

    Left open is the one case worth paying for: a player whose current club is
    absent from his own fixture's pair has definitely moved, and the pair
    alone does not say which of the two he moved from.
    """
    settled: dict[int, int] = {}
    moved: set[int] = set()
    for player_id, clubs in candidates.items():
        if len(clubs) == 1:
            settled[player_id] = next(iter(clubs))
            continue
        club_now = clubs_now.get(player_id)
        if club_now is None:
            continue
        if club_now in clubs:
            settled[player_id] = club_now
        else:
            moved.add(player_id)
    return settled, frozenset(moved)


def club_from_player_detail(
    detail: Mapping[str, Any],
    fixture_ids: frozenset[int],
    fixtures: Sequence[Fixture],
) -> int | None:
    """The club a moved player turned out for, read off his season history.

    `element-summary/{id}/` keeps a row per fixture the player was registered
    for, including the ones at a club he has since left, and each row carries
    the fixture id and `was_home`. With the fixture's two clubs that is exact
    -- the side he was on names the club, where the pair alone could only
    offer both.

    None where the history says nothing about these fixtures, which leaves
    the caller where it started rather than guessing between the two.
    """
    pairs = _club_pairs(fixtures)
    for row in detail.get("history") or ():
        fixture_id = row.get("fixture")
        if fixture_id not in fixture_ids or fixture_id not in pairs:
            continue
        was_home = row.get("was_home")
        if was_home is None:
            continue
        fixture = next(f for f in fixtures if f.id == fixture_id)
        return fixture.home_team_id if was_home else fixture.away_team_id
    return None


def gameweek_club(
    player_id: int | None,
    team_id: int | None,
    *,
    clubs: Mapping[int, int] | None,
    teams: Mapping[int, Team],
) -> Team | None:
    """The club this player was at in the gameweek being read.

    Mirrors `had_fixture`: prefers the gameweek's own answer and falls back to
    today's club whenever the gameweek declined to give one, the player's club
    blanked, or he has no main-game id to look up (a draft player the main
    game never matched). Resolves to the `Team` itself so a call site swaps
    one lookup for another rather than growing a second step.
    """
    resolved = team_id
    if clubs is not None and player_id is not None:
        resolved = clubs.get(player_id, team_id)
    return teams.get(resolved) if resolved is not None else None


def overruled_player_codes(
    players: Iterable[Player], clubs: Mapping[int, int] | None,
) -> list[int]:
    """The codes of players the gameweek placed somewhere other than today.

    Keyed on `code` rather than the seasonal id because that is what a ledger
    row carries, and this is what tells the identity carry that a replayed
    club was derived from the gameweek rather than restamped from today's
    bootstrap -- so a stale club already on disk is superseded rather than
    carried forward over the correct one (issue #177).
    """
    if not clubs:
        return []
    return sorted({
        player.code
        for player in players
        if player.code
        and (club := clubs.get(player.id)) is not None
        and club != player.team_id
    })


class GameweekClubResolver:
    """Resolves gameweek clubs, reusing one player-detail fetch across a run.

    A backfill replays every gameweek in the season and the same handful of
    players have moved for all of them, so the fetch is cached per player
    rather than per gameweek: `element-summary/{id}/` returns the whole
    season's history in one response, which answers for every gameweek the
    run goes on to replay. A failed fetch is cached too -- an endpoint that
    404s for one player would otherwise be retried once per gameweek.
    """

    def __init__(self, client: PlayerDetailClient) -> None:
        self._client = client
        self._detail: dict[int, Mapping[str, Any] | None] = {}

    async def resolve(
        self,
        live_data: dict[str, Any],
        fixtures: Sequence[Fixture],
        clubs_now: Mapping[int, int],
    ) -> dict[int, int] | None:
        """Player id to the club he was at, for one gameweek. None when unanswerable."""
        player_fixtures = resolve_player_fixtures(live_data, fixtures)
        if player_fixtures is None:
            return None
        candidates = narrow_clubs(player_fixtures, fixtures)
        settled, moved = settle_clubs(candidates, clubs_now)
        if moved:
            settled.update(await self._place_moved(moved, player_fixtures, fixtures))
        return settled

    async def _place_moved(
        self,
        moved: frozenset[int],
        player_fixtures: Mapping[int, frozenset[int]],
        fixtures: Sequence[Fixture],
    ) -> dict[int, int]:
        sem = asyncio.Semaphore(_DETAIL_CONCURRENCY)

        async def _one(player_id: int) -> tuple[int, int | None]:
            detail = await self._fetch(player_id, sem)
            if detail is None:
                return player_id, None
            return player_id, club_from_player_detail(
                detail, player_fixtures[player_id], fixtures,
            )

        placed: dict[int, int] = {}
        for player_id, club in await asyncio.gather(*(_one(p) for p in sorted(moved))):
            if club is not None:
                placed[player_id] = club
            else:
                logger.debug(
                    "Player %s changed club since this gameweek but his own history "
                    "did not place him; falling back to his current club.", player_id,
                )
        return placed

    async def _fetch(
        self, player_id: int, sem: asyncio.Semaphore,
    ) -> Mapping[str, Any] | None:
        if player_id in self._detail:
            return self._detail[player_id]
        async with sem:
            try:
                detail: Mapping[str, Any] | None = await self._client.get_player_detail(player_id)
            except Exception as exc:  # noqa: BLE001 — best-effort enrichment
                logger.debug("Could not fetch player detail for %s: %s", player_id, exc)
                detail = None
        self._detail[player_id] = detail
        return detail
