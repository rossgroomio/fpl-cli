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
- One entry leaves the fixture's two clubs, which does not say which side he
  was on. Today's club being *neither* is proof that he has moved, and
  `element-summary/{id}/` is then worth one call to place him exactly.
- No entries (his club blanked) leaves nothing to intersect and no answer.

That leaves one case this deliberately does **not** answer: a single fixture
whose pair contains today's club. It is tempting to read as "he has not
moved", and it usually is -- but a player who moved from one of those two
clubs to the other is the same shape and the pair cannot tell them apart. So
it is recorded as no answer rather than as today's club, which gives the
precedence ladder the callers rely on:

1. A club derived here is exact, and supersedes whatever a row already holds.
2. A club a prior row recorded beats an assumption, so the identity carry
   keeps it (issue #169's guarantee, unchanged).
3. Today's club is the fallback, as it was before any of this.

Reading the ambiguous case as an answer would invert 1 and 2 and let today's
club overwrite a correctly recorded one -- the very bug this exists to fix.

Club only. Name and position are not derivable from fixtures at all, so they
stay carry-forward-only and a first capture still records today's.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol

import httpx

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping, Sequence

    from fpl_cli.models.fixture import Fixture
    from fpl_cli.models.player import Player
    from fpl_cli.models.team import Team

logger = logging.getLogger(__name__)

# This module's own permit, deliberately tighter than the recap's
# `_PICKS_CONCURRENCY` of 10: a drift is rare by construction, so this bounds
# a short burst rather than a stream. The two pools are separate and never
# run at the same time -- `resolve()` is awaited to completion before the
# picks phase begins.
_DETAIL_CONCURRENCY = 5


class PlayerDetailClient(Protocol):
    """The one FPLClient method the moved-player lookup calls."""

    async def get_player_detail(self, player_id: int, /) -> dict[str, Any]: ...


@dataclass(frozen=True)
class GameweekClubs:
    """What one gameweek's own data says about where its players were.

    Both fields come out of a single pass over the live payload, which is why
    they travel together: `had_fixture` and the club stamping ask overlapping
    questions of the same `explain` entries, and scanning ~700 elements twice
    per gameweek is wasted work on a whole-season backfill.
    """

    # Player id to the club he was at, for the players the gameweek could
    # place *exactly*. Absent means no answer, not "today's club".
    clubs: Mapping[int, int]
    # Every player whose club had a fixture at all -- what `had_fixture`
    # needs, and a strict superset of `clubs`.
    with_fixture: frozenset[int]


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

    Every `explain` fixture id is kept, including one this gameweek's fixture
    list does not carry, so that `frozenset(result)` is exactly the answer
    `resolve_players_with_fixture` gives and the two cannot drift apart.
    `narrow_clubs` is where an id with no known pair drops out.

    A player whose club had no fixture is absent from the mapping: there is
    nothing to intersect for him and no club to recover.
    """
    if not fixtures or not all(f.finished for f in fixtures):
        return None
    elements = live_data.get("elements") or []
    by_player: dict[int, frozenset[int]] = {}
    for element in elements:
        player_id = element.get("id")
        if player_id is None:
            continue
        played = frozenset(
            fixture_id
            for entry in element.get("explain") or ()
            if (fixture_id := entry.get("fixture")) is not None
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

    One club for a double gameweek, two for a single. A fixture id with no
    pair in this gameweek is skipped rather than treated as a constraint, and
    a player left with no pair at all drops out entirely.
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
    """Split the candidates into the clubs settled exactly and the open ones.

    Settled only where the intersection is a single club, which a double
    gameweek gives outright. A single fixture leaves two, and the pair alone
    never says which side he was on:

    - Today's club is *neither* of them, so he has certainly moved. Returned
      as open, which is what buys him an `element-summary` lookup -- a
      handful a season rather than one per player.
    - Today's club is one of them. Read as an answer this would say "he has
      not moved", but a player who moved between those exact two clubs looks
      identical, and the wrong answer would be unflaggable by construction
      since it always equals today's club. So it is neither settled nor
      opened: no answer, and the recorded club (then today's) stands. See the
      precedence ladder in the module docstring.
    """
    settled: dict[int, int] = {}
    moved: set[int] = set()
    for player_id, clubs in candidates.items():
        if len(clubs) == 1:
            settled[player_id] = next(iter(clubs))
            continue
        club_now = clubs_now.get(player_id)
        if club_now is not None and club_now not in clubs:
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


def derived_player_codes(
    players: Iterable[Player], clubs: Mapping[int, int] | None,
) -> list[int]:
    """The codes of players this gameweek placed exactly.

    Presence in `clubs` is the whole test, deliberately: every club in there
    was derived from the gameweek's own fixtures, so it outranks a recorded
    one whether or not it happens to match today's bootstrap. Testing
    "differs from today's club" instead would drop the case this is for -- a
    player who left and came back, whose gameweek club is right, whose
    recorded club is a stale capture-time stamp, and whose two clubs agree
    today. That row would keep the stale club, silently.

    Keyed on `code` rather than the seasonal id because that is what a ledger
    row carries. A player the gameweek could not place exactly is absent, so
    the identity carry keeps what the row already recorded (issue #177).
    """
    if not clubs:
        return []
    return sorted({
        player.code
        for player in players
        if player.code and player.id in clubs
    })


class GameweekClubResolver:
    """Resolves gameweek clubs, reusing one player-detail fetch across a run.

    A backfill replays every gameweek in the season and the same handful of
    players have moved for all of them, so the fetch is cached per player
    rather than per gameweek: `element-summary/{id}/` returns the whole
    season's history in one response, which answers for every gameweek the
    run goes on to replay.

    Only a *permanent* failure is cached. One resolver serves a whole
    backfill, so caching a timeout or a 5xx would let one blip on the first
    gameweek that needs a player silently cost him every later one; a 4xx
    says the endpoint will keep answering the same way and is worth
    remembering.
    """

    def __init__(self, client: PlayerDetailClient) -> None:
        self._client = client
        self._detail: dict[int, Mapping[str, Any] | None] = {}

    async def resolve(
        self,
        live_data: dict[str, Any],
        fixtures: Sequence[Fixture],
        clubs_now: Mapping[int, int],
    ) -> GameweekClubs | None:
        """What one gameweek says about its own clubs. None when it cannot say."""
        player_fixtures = resolve_player_fixtures(live_data, fixtures)
        if player_fixtures is None:
            return None
        candidates = narrow_clubs(player_fixtures, fixtures)
        settled, moved = settle_clubs(candidates, clubs_now)
        if moved:
            settled.update(await self._place_moved(moved, player_fixtures, fixtures))
        return GameweekClubs(clubs=settled, with_fixture=frozenset(player_fixtures))

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
            try:
                club = club_from_player_detail(
                    detail, player_fixtures[player_id], fixtures,
                )
            except Exception as exc:  # noqa: BLE001 — best-effort enrichment
                # Guarded for the same reason the fetch is: one unexpected
                # `history` shape must cost that player his club, not abort
                # every other lookup in the batch and the recap with it.
                logger.debug("Could not read a club off player %s's history: %s", player_id, exc)
                return player_id, None
            return player_id, club

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
                detail: Mapping[str, Any] = await self._client.get_player_detail(player_id)
            except Exception as exc:  # noqa: BLE001 — best-effort enrichment
                logger.debug("Could not fetch player detail for %s: %s", player_id, exc)
                if _is_permanent(exc):
                    self._detail[player_id] = None
                return None
        self._detail[player_id] = detail
        return detail


def _is_permanent(exc: BaseException) -> bool:
    """Whether re-requesting this player later would fail the same way.

    A 4xx is the endpoint's settled answer; a timeout, a connection error or
    a 5xx is the moment, and the next gameweek deserves a fresh attempt.
    Anything unrecognised is treated as transient, so an unfamiliar failure
    costs a retry rather than the rest of the run.
    """
    return (
        isinstance(exc, httpx.HTTPStatusError)
        and 400 <= exc.response.status_code < 500
    )
