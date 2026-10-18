"""FPL Draft API client for draft league functionality."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Iterable, Mapping, Sequence
from typing import TYPE_CHECKING, Any, Self

import httpx

from fpl_cli.models.gameweek_status import GameweekStatus
from fpl_cli.models.player import POSITION_MAP, Player, derive_appearances, usable_code
from fpl_cli.utils.text import strip_diacritics

if TYPE_CHECKING:
    from fpl_cli.models.team import Team

logger = logging.getLogger(__name__)

BASE_URL = "https://draft.premierleague.com/api"

# The Draft API's transaction `result` codes. Every row the league processed
# carries one, and the two denial codes say which side of the claim was
# already gone -- not who took it (issues #329, #342).
#
# `a`  -- the league processed the claim into a completed move.
# `di` -- denied on the *incoming* side: the player was gone when this claim
#         processed. Usually a rival won him, which is a genuine competitive
#         loss. But the engine processes claims globally in `index` order and
#         a manager's own earlier accepted claim removes him exactly as a
#         rival's does, so a manager who wins a player and also listed him
#         lower down gets a `di` for the player he just won.
# `do` -- denied on the *outgoing* side: the player nominated as the drop had
#         already been used as the drop in an earlier accepted claim by the
#         same manager. It cannot occur without that earlier accepted claim,
#         so a `do` row never stands alone as a manager's only activity.
#
# Both denials therefore cover a self-cascade as well as a real defeat, which
# is why the code alone cannot say whether a manager lost anything: managers
# submit several claims sharing one drop slot, or naming one target against
# several drops, expecting only one to land. `resolve_lost_claims()` answers
# that from the manager's whole gameweek; a single row cannot.
DRAFT_TXN_ACCEPTED = "a"
DRAFT_TXN_DENIED_PLAYER_GONE = "di"
DRAFT_TXN_DROP_ALREADY_USED = "do"

# The Draft API's trade `state` codes, read off the public trades feed (#458).
#
# `p` -- processed: the players changed squads. Every one of 115 trades across
#        32 public leagues was `p`, and each matched the squads before and
#        after it. Within an item, `element_out` leaves `offered_entry` for
#        `received_entry` and `element_in` goes the other way.
# `v` -- vetoed: nothing moved.
#
# Any other code is not a move this client has seen happen, so
# `is_processed_trade()` is false for it: a trade that did complete under some
# other code shows up as a replay that disagrees with the league's ownership
# list, which degrades the provenance it feeds rather than mislabelling.
DRAFT_TRADE_PROCESSED = "p"
DRAFT_TRADE_VETOED = "v"


def is_accepted_transaction(txn: Mapping[str, Any]) -> bool:
    """Whether the league turned this transaction row into a completed move.

    The only rows that changed a squad, so the only ones a move ledger, a net
    points swing or a "who dropped whom" question may be built from.
    """
    return txn.get("result") == DRAFT_TXN_ACCEPTED


def is_processed_trade(trade: Mapping[str, Any]) -> bool:
    """Whether this trade went through and moved its players between squads.

    The trade counterpart of `is_accepted_transaction()`: a vetoed trade, or
    one still awaiting its response, changed nothing.
    """
    return trade.get("state") == DRAFT_TRADE_PROCESSED


def is_denied_on_the_incoming_player(txn: Mapping[str, Any]) -> bool:
    """Whether this row was denied because the incoming player was gone.

    True for `di` alone. A `do` row is deliberately excluded: counting it as
    a failed attempt would misreport the manager whose *winning* claim caused
    it, which is the misattribution this classification exists to prevent.

    Row-local, and so not the same question as "did this manager lose the
    player" -- his own winning claim makes him gone too. Callers wanting that
    want `resolve_lost_claims()`.
    """
    return txn.get("result") == DRAFT_TXN_DENIED_PLAYER_GONE


def draft_claim_priority_rank(priority: int | None) -> tuple[int, int]:
    """Sort key placing a numbered priority before none at all, 1 first."""
    return (1, 0) if priority is None else (0, priority)


def _processing_position(txn: Mapping[str, Any], arrival: int) -> tuple[int, int, int]:
    """Where one row sits in the order the league processed the gameweek.

    Waivers run first, as a single batch the engine works through in `index`
    order; free agency opens once that batch is done and its rows carry no
    index at all. `arrival` is the row's place in the feed, which orders the
    rows an index cannot separate.
    """
    index = txn.get("index")
    return (0, index, arrival) if isinstance(index, int) else (1, 0, arrival)


def resolve_lost_claims(txns: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    """The claims one manager genuinely lost, from all their rows for one
    gameweek -- the question no single row can answer (issue #342).

    Two things a row-local read gets wrong, both consequences of managers
    submitting conditional chains rather than independent claims:

    - A `di` for a player this manager had *already taken* is his own cascade,
      not a defeat. He listed the same target twice with different drops, one
      landed, and every later claim for him was then denied on the incoming
      side. Reporting it inverts the outcome for the manager who won the race.
    - A player he claimed several times and lost was wanted once, so he is
      returned once, at the best priority he spent on him. Otherwise one
      target across three drops reads as three separate defeats.

    "Already taken" is a question about order, not merely about owning him by
    the end of the gameweek: only a claim that landed *before* the denial can
    have caused it. Free agency opens after the whole waiver batch, so a
    manager who loses a waiver to a rival and signs the same player as a free
    agent once the rival drops him still lost that waiver, and the loss is
    still recorded -- clearing it on the later pickup would erase a real
    defeat and contradict the case `contested_draft_claims` documents.

    Both rules are the ones that function already states and works to; this
    brings the stored claims into line with them, so the two layers cannot
    disagree about what a manager lost. Order follows the feed, by the row
    kept for each player.
    """
    # The earliest point at which a claim of this manager's took each player.
    taken_at: dict[Any, tuple[int, int, int]] = {}
    for arrival, txn in enumerate(txns):
        if not is_accepted_transaction(txn):
            continue
        player = txn.get("element_in")
        at = _processing_position(txn, arrival)
        if player not in taken_at or at < taken_at[player]:
            taken_at[player] = at

    best: dict[Any, dict[str, Any]] = {}
    for arrival, txn in enumerate(txns):
        player = txn.get("element_in")
        if player is None or not is_denied_on_the_incoming_player(txn):
            continue
        taken = taken_at.get(player)
        if taken is not None and taken < _processing_position(txn, arrival):
            continue
        known = best.get(player)
        if known is None or draft_claim_priority_rank(txn.get("priority")) < draft_claim_priority_rank(
            known.get("priority"),
        ):
            best[player] = txn
    return list(best.values())


DRAFT_SQUADS_UNREAD = "draft_squads_unread"


def unread_squads_warning(unread: Mapping[int, str]) -> dict[str, str] | None:
    """The `metadata.warnings` entry for rival squads an ownership read missed, else None.

    *unread* is `FPLDraftClient.unread_squads` after `get_league_ownership`
    (or `get_available_players`, which calls it): entry id to entry name.
    One builder for every consumer of the ownership map -- `fpl waivers`,
    whose pool offers such a player as a claim that fails at the deadline,
    and the draft columns of `fpl player` and `fpl stats`, which label them
    "Available" -- so the three cannot describe the same gap differently.
    """
    if not unread:
        return None
    names = ", ".join(sorted(unread.values()))
    plural = len(unread) > 1
    return {
        "code": DRAFT_SQUADS_UNREAD,
        "message": (
            f"The squad{'s' if plural else ''} of {len(unread)} league "
            f"entr{'ies' if plural else 'y'} ({names}) could not be read, so "
            f"{'their' if plural else 'its'} players may be listed as available "
            "and a claim on one would fail. Re-run; if it persists, the draft API "
            "may have changed."
        ),
    }


class FPLDraftClient:
    """Client for the FPL Draft API.

    The Draft API provides access to draft league data including:
    - League standings and details
    - Available players (free agents)
    - Waiver wire transactions
    - Team squads
    """

    def __init__(self, timeout: float = 30.0) -> None:
        """Initialize the FPL Draft API client.

        Args:
            timeout: Request timeout in seconds.
        """
        self.timeout = timeout
        self._bootstrap_data: dict[str, Any] | None = None
        self._game_state: dict[str, Any] | None = None
        self._http = httpx.AsyncClient(base_url=BASE_URL, timeout=self.timeout)
        # League entries whose squad the last `get_league_ownership` could
        # not read, as entry id -> entry name. Their players are absent
        # from the ownership map and so read as available; a caller
        # ranking claims must say so rather than offer them (#438).
        self.unread_squads: dict[int, str] = {}

    async def close(self) -> None:
        """Close the underlying HTTP client."""
        await self._http.aclose()

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    async def _get(self, endpoint: str) -> Any:
        """Make a GET request to the FPL Draft API.

        Args:
            endpoint: API endpoint (without base URL).

        Returns:
            JSON response data.

        Raises:
            httpx.HTTPError: If the request fails.
        """
        response = await self._http.get(f"/{endpoint}")
        response.raise_for_status()
        return response.json()

    async def get_bootstrap_static(self, force_refresh: bool = False) -> dict[str, Any]:
        """Get the bootstrap-static data for draft.

        Contains all static data including players, teams, and settings.

        Args:
            force_refresh: If True, fetch fresh data even if cached.

        Returns:
            Bootstrap static data.
        """
        if self._bootstrap_data is None or force_refresh:
            self._bootstrap_data = await self._get("bootstrap-static")
        if self._bootstrap_data is None:
            raise ValueError("bootstrap-static returned no data")
        return self._bootstrap_data

    async def get_league_details(self, league_id: int) -> dict[str, Any]:
        """Get details for a specific draft league.

        Args:
            league_id: The draft league ID.

        Returns:
            League details including settings and standings.
        """
        return await self._get(f"league/{league_id}/details")

    async def get_league_ownership_status(self, league_id: int) -> dict[str, Any]:
        """Get the ownership status of all players in a league.

        Shows which players are owned and by whom.

        Args:
            league_id: The draft league ID.

        Returns:
            Element status data.
        """
        return await self._get(f"league/{league_id}/element-status")

    async def get_league_transactions(self, league_id: int) -> dict[str, Any]:
        """Get recent transactions in a league.

        Includes waivers, free agent pickups, and trades.

        Args:
            league_id: The draft league ID.

        Returns:
            Transaction history.
        """
        return await self._get(f"draft/league/{league_id}/transactions")

    async def get_draft_choices(self, league_id: int) -> dict[str, Any]:
        """Get the picks of the league's latest draft.

        One `choices` row per pick, each naming the `draft` it belongs to,
        beside `element_status` (every player's owner *today*) and `idle`.
        Only the latest draft is served: a draft scheduled but not yet held
        returns no rows, and an earlier held draft's picks have no route at
        all, so a redraft erases them once it is scheduled (#461). The path
        takes the league id; a draft id reads some other league's draft.

        Args:
            league_id: The draft league ID.

        Returns:
            The choices response, as served.
        """
        return await self._get(f"draft/{league_id}/choices")

    async def get_league_trades(self, league_id: int) -> dict[str, Any]:
        """Get the trades managers in a league have made, whatever became of them.

        Each trade carries ``event``, ``offered_entry``, ``received_entry``,
        ``state`` (see ``is_processed_trade()``), ``offer_time``,
        ``response_time`` and a ``tradeitem_set`` of ``element_in`` /
        ``element_out`` pairs read from the offering entry's side: its
        ``element_out`` goes to the receiving entry and its ``element_in``
        comes the other way.

        Args:
            league_id: The draft league ID.

        Returns:
            ``{"trades": [...]}``.
        """
        return await self._get(f"draft/league/{league_id}/trades")

    async def get_entry_profile(self, entry_id: int) -> dict[str, Any]:
        """Get a draft entry's public profile.

        Args:
            entry_id: The team/entry ID.

        Returns:
            Entry profile data.
        """
        return await self._get(f"entry/{entry_id}/public")

    async def get_entry_picks(self, entry_id: int, gameweek: int) -> dict[str, Any]:
        """Get a team's picks for a specific gameweek.

        Args:
            entry_id: The team/entry ID.
            gameweek: The gameweek number.

        Returns:
            Team picks for the gameweek.
        """
        return await self._get(f"entry/{entry_id}/event/{gameweek}")

    async def get_game_state(self, force_refresh: bool = False) -> dict[str, Any]:
        """Get current game state data (cached after first fetch).

        Returns:
            Game data including current gameweek info.
        """
        if self._game_state is None or force_refresh:
            data: dict[str, Any] = await self._get("game")
            self._game_state = data
        return self._game_state

    async def get_gameweek_status(self) -> GameweekStatus:
        """Get where FPL has got to in processing the latest gameweek.

        The draft game's own copy of the main game's feed, reporting when the
        draft league tables were refreshed, which happens separately from the
        classic ones.

        Returns:
            The processing state of the gameweek the feed currently covers.
        """
        return GameweekStatus.model_validate(await self._get("pl/event-status"))

    async def get_squad(
        self,
        entry_id: int,
        bootstrap_data: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        """Get a team's current squad with enriched player data.

        Args:
            entry_id: The team's entry ID.
            bootstrap_data: Optional pre-fetched bootstrap data.

        Returns:
            List of players in the team's squad.
        """
        if bootstrap_data is None:
            bootstrap_data = await self.get_bootstrap_static()

        game_data = await self.get_game_state()
        current_gw = game_data.get("current_event", 1)

        picks_data = await self.get_entry_picks(entry_id, current_gw)
        player_ids = [p.get("element") for p in picks_data.get("picks", [])]

        player_map = {p["id"]: p for p in bootstrap_data.get("elements", [])}
        squad = [player_map[pid] for pid in player_ids if pid in player_map]

        return squad

    async def get_league_ownership(
        self,
        league_id: int,
        bootstrap_data: dict[str, Any] | None = None,
        league_details: dict[str, Any] | None = None,
    ) -> dict[int, int]:
        """Get accurate ownership by querying actual team squads.

        The element-status endpoint can return stale data. This method
        builds ownership by querying each team's actual squad.

        Fetches game_state once then parallelises all entry_picks calls. A
        squad whose fetch fails is left out of the map and recorded on
        ``unread_squads``; every caller reads that record and names it with
        ``unread_squads_warning``, since the players of a squad this could
        not read look exactly like free agents (#438).

        Args:
            league_id: The draft league ID.
            bootstrap_data: Optional pre-fetched bootstrap data.
            league_details: Optional pre-fetched league details. Unlike
                bootstrap-static and game state, get_league_details is not
                memoised, so a caller that already holds it should pass it
                rather than pay for a second fetch.

        Returns:
            Dict mapping player ID to owner entry_id.
        """
        if bootstrap_data is None:
            bootstrap_data = await self.get_bootstrap_static()

        if league_details is None:
            league_details = await self.get_league_details(league_id)
        game_data = await self.get_game_state()
        current_gw = game_data.get("current_event", 1)
        player_map = {p["id"]: p for p in bootstrap_data.get("elements", [])}

        entries = league_details.get("league_entries", [])
        entry_ids = [e["entry_id"] for e in entries]
        entry_names = {
            e["entry_id"]: str(e.get("entry_name") or e["entry_id"]) for e in entries
        }

        # Fetch all squads in parallel
        results = await asyncio.gather(
            *(self.get_entry_picks(eid, current_gw) for eid in entry_ids),
            return_exceptions=True,
        )

        ownership: dict[int, int] = {}
        self.unread_squads = {}
        for entry_id, picks_result in zip(entry_ids, results):
            if isinstance(picks_result, BaseException):
                # Recorded, not just logged: a squad this could not read
                # leaves every one of its players looking unowned, and the
                # waiver list built on that offers them as claims (#438).
                self.unread_squads[entry_id] = entry_names[entry_id]
                logger.warning(
                    "Could not read the squad of %s (entry %s): %s — its players may "
                    "read as available",
                    entry_names[entry_id],
                    entry_id,
                    picks_result,
                )
                continue
            picks_data: dict[str, Any] = picks_result
            pick_ids = [p.get("element") for p in picks_data.get("picks", [])]
            for pid in pick_ids:
                if pid in player_map:
                    ownership[pid] = entry_id

        return ownership

    async def get_available_players(
        self,
        league_id: int,
        bootstrap_data: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        """Get all available (unowned) players in a league.

        Only includes players who are:
        - Not owned by any team in the league
        - Not 'u' (unavailable) status with 0 points (left league, never played)

        Players with 'n' (not available) status are included as these are
        typically active players who transferred clubs mid-season.

        Args:
            league_id: The draft league ID.
            bootstrap_data: Optional pre-fetched bootstrap data.

        Returns:
            List of available player data.
        """
        if bootstrap_data is None:
            bootstrap_data = await self.get_bootstrap_static()

        # Build ownership from actual squads (element-status can be stale)
        ownership = await self.get_league_ownership(league_id, bootstrap_data)
        owned_ids = set(ownership.keys())

        # Filter to unowned players
        # Exclude 'u' (unavailable) players with 0 points - these are players who
        # left the league or never played and cannot be picked up
        # Include 'n' (not available) players - these are typically mid-season
        # transfers who are still active
        all_players = bootstrap_data.get("elements", [])
        available = [
            p for p in all_players
            if p["id"] not in owned_ids
            and not (p.get("status") == "u" and p.get("total_points", 0) == 0)
        ]

        return available

    async def get_waiver_order(self, league_id: int) -> list[dict[str, Any]]:
        """Get the current waiver priority order.

        Args:
            league_id: The draft league ID.

        Returns:
            List of teams in waiver order.
        """
        details = await self.get_league_details(league_id)
        standings = details.get("standings", [])

        # Waiver order is typically reverse of standings
        # (worst team gets first pick)
        return sorted(standings, key=lambda x: x.get("rank", 0), reverse=True)

    async def get_recent_releases(
        self,
        league_id: int,
        bootstrap_data: dict[str, Any] | None = None,
        max_gameweeks_back: int = 4,
    ) -> list[dict[str, Any]]:
        """Get players recently released (dropped) who are still available.

        Only returns players who:
        - Were dropped in recent gameweeks
        - Are still available (not picked up by another team)
        - Appear only once (most recent release)

        Args:
            league_id: The draft league ID.
            bootstrap_data: Optional pre-fetched bootstrap data.
            max_gameweeks_back: How many gameweeks back to look (default 4).

        Returns:
            List of recently released players who are still available.
        """
        if bootstrap_data is None:
            bootstrap_data = await self.get_bootstrap_static()

        # Get current gameweek
        game_data = await self.get_game_state()
        current_gw = game_data.get("current_event", 1)
        min_gameweek = max(1, current_gw - max_gameweeks_back)

        # Get element status to check who's still available
        element_status = await self.get_league_ownership_status(league_id)
        owned_ids = set()
        for element in element_status.get("element_status", []):
            if element.get("owner") is not None:
                owned_ids.add(element["element"])

        transactions = await self.get_league_transactions(league_id)
        player_map = {p["id"]: p for p in bootstrap_data.get("elements", [])}

        # Track releases, keeping only the most recent per player
        releases_by_player: dict[int, dict[str, Any]] = {}

        for txn in transactions.get("transactions", []):
            # A denied claim names the player it would have dropped, but the
            # drop never happened -- only an accepted row released anybody.
            if not is_accepted_transaction(txn):
                continue

            element_out = txn.get("element_out")
            gameweek = txn.get("event", 0)

            # Skip if no player was dropped, or too old
            if not element_out or gameweek < min_gameweek:
                continue

            # Skip if player was picked up by someone else
            if element_out in owned_ids:
                continue

            player = player_map.get(element_out)
            if not player:
                continue

            # Only keep the most recent release for each player
            if element_out not in releases_by_player or gameweek > releases_by_player[element_out]["gameweek"]:
                releases_by_player[element_out] = {
                    "player": player,
                    "dropped_by": txn.get("entry"),
                    "gameweek": gameweek,
                    "transaction_type": txn.get("kind"),
                }

        # Sort by gameweek (most recent first)
        releases = sorted(
            releases_by_player.values(),
            key=lambda x: x["gameweek"],
            reverse=True,
        )

        return releases

    def parse_player(
        self,
        player_data: dict[str, Any],
        team_id_map: Mapping[int, int],
    ) -> dict[str, Any]:
        """Parse raw player data into a cleaner format.

        Args:
            player_data: Raw player data from the API.
            team_id_map: Draft team id to main-game team id, from
                `match_draft_teams_to_main`. Required rather than defaulted,
                so no caller can emit a draft club id by forgetting it.

        Returns:
            Cleaned player data. `team_id` is the **main-game** team id, or
            None for a club the main game's team list does not carry.
        """
        # `or 0`: a null total must not crash the derivation below and fail
        # the whole pool, which `get_available_players` already tolerates.
        total_points = player_data.get("total_points") or 0
        ppg = float(player_data.get("points_per_game", 0))
        minutes = player_data.get("minutes") or 0
        return {
            "id": player_data.get("id"),
            # The draft game's own element id above, and the cross-game key
            # beside it: every row this feeds (the waiver pool and ranked
            # targets among them) is read by consumers that also hold main-game
            # ids, and the two numberings diverge for mid-season additions
            # (#209, #418). `code` is what a join across the boundary uses.
            "code": usable_code(player_data.get("code")),
            "player_name": player_data.get("web_name", ""),
            "first_name": player_data.get("first_name", ""),
            "second_name": player_data.get("second_name", ""),
            # Translated here, once, because everything downstream (fixtures,
            # form, FDR, matchup) is keyed by the main game's team id (#423).
            "team_id": draft_player_team_id(player_data, team_id_map),
            "position": POSITION_MAP.get(player_data.get("element_type", 0), "???"),
            "total_points": total_points,
            "ppg": ppg,
            # The bootstrap publishes no appearance count, and the waiver
            # score's minutes factor reads a missing one as "never played",
            # zeroing every per-90 term and the matchup bonus from GW6 (#471).
            # Derived the way the main game's `Player.appearances` is.
            "appearances": derive_appearances(total_points, ppg, minutes),
            "form": float(player_data.get("form", 0)),
            "status": player_data.get("status", "a"),
            "news": player_data.get("news", ""),
            "chance_of_playing": player_data.get("chance_of_playing_next_round"),
            "goals_scored": player_data.get("goals_scored", 0),
            "assists": player_data.get("assists", 0),
            "clean_sheets": player_data.get("clean_sheets", 0),
            "minutes": minutes,
            "expected_goals": float(player_data.get("expected_goals", 0)),
            "expected_assists": float(player_data.get("expected_assists", 0)),
        }


def match_draft_teams_to_main(
    draft_teams: Iterable[Mapping[str, Any]],
    main_teams: Iterable[Team],
) -> dict[int, int]:
    """Map draft team ids onto the main-game team id of the same club.

    Each game publishes its own `teams` list and numbers it 1-20 for the
    season. The two numberings agree today, but nothing ties them together:
    a draft relaunch out of step with the main game at a season change would
    leave every draft club id naming a different main-game club, and every
    read of one against main-game fixtures, form or team data would pick
    that club silently. Both lists carry `code`, the club's permanent
    identifier, so `code` is the join (#423). It is the club-level
    counterpart of `match_draft_to_main`.

    A club whose code is missing from either side is absent, so a lookup
    finds no club rather than whichever one happens to share the number.
    """
    main_by_code: dict[int, int] = {}
    for team in main_teams:
        code = usable_code(team.code)
        if code is not None:
            main_by_code[code] = team.id

    matched: dict[int, int] = {}
    for team in draft_teams:
        draft_id = team.get("id")
        code = usable_code(team.get("code"))
        if not isinstance(draft_id, int) or code is None:
            continue
        main_id = main_by_code.get(code)
        if main_id is not None:
            matched[draft_id] = main_id
    return matched


def draft_player_team_id(
    player: Mapping[str, Any],
    team_id_map: Mapping[int, int],
    *,
    main_player: Player | None = None,
) -> int | None:
    """The main-game team id of a raw draft player's club, or None.

    The matched main-game player answers first when there is one: his
    `team_id` is already in the main game's numbering and is the club the
    rest of his row (live points, fixtures) is read against, so it holds
    even when the draft list carries a club the main game has dropped.
    Otherwise the player's `team`, the draft game's own team id, goes
    through *team_id_map* (`match_draft_teams_to_main`). None means neither
    can say: the raw id is never passed through, because it would name
    whichever main-game club shares the number (#423).
    """
    if main_player is not None:
        return main_player.team_id
    team = player.get("team")
    return team_id_map.get(team) if isinstance(team, int) else None


def untranslated_draft_teams(
    draft_teams: Iterable[Mapping[str, Any]],
    team_id_map: Mapping[int, int],
) -> list[str]:
    """Short names of the draft clubs *team_id_map* has no main-game id for.

    What a draft path warns with: a player at one of these clubs reads with
    no club, which is right but otherwise indistinguishable from a quiet one.
    """
    return sorted(
        str(team.get("short_name") or team.get("id"))
        for team in draft_teams
        if team.get("id") not in team_id_map
    )


def match_draft_to_main(
    draft_elements: Sequence[dict[str, Any]],
    main_players: Sequence[Player],
    *,
    team_id_map: Mapping[int, int],
) -> dict[int, Player]:
    """Map draft element IDs onto the main-game player they represent.

    The join is `code`, the stable cross-season identifier both bootstraps
    carry on every element. `web_name` is not stable: the main game renamed
    Savinho to Sávio mid-season and the draft game kept the old spelling, so
    a `(web_name, team)` join dropped a player the two APIs agreed on in
    every other field (#168). Names survive only as a fallback for an element
    with no code, folded for diacritics and paired with the team so two
    players sharing a surname stay apart.

    The fallback compares main-game clubs, so the element's draft team goes
    through *team_id_map* (`match_draft_teams_to_main`) first; required, so
    no caller pairs on a draft team id read as a main-game one by leaving it
    out (#423). An element whose club does not translate goes unmatched.

    Returns only the draft IDs that matched; a missing key is a genuine miss.
    """
    by_code = {p.code: p for p in main_players if p.code}
    # Built on first use: every live element carries a code, so most calls
    # never reach the fallback and need not fold ~700 names for it
    by_name_team: dict[tuple[str, int], Player] | None = None

    matched: dict[int, Player] = {}
    for dp in draft_elements:
        draft_id = dp.get("id")
        if draft_id is None:
            continue
        code = dp.get("code")
        main_player = by_code.get(code) if code else None
        if main_player is None:
            name, team = dp.get("web_name"), draft_player_team_id(dp, team_id_map)
            if name and team is not None:
                if by_name_team is None:
                    by_name_team = {
                        (strip_diacritics(p.web_name).lower(), p.team_id): p for p in main_players
                    }
                main_player = by_name_team.get((strip_diacritics(name).lower(), team))
        if main_player is not None:
            matched[draft_id] = main_player
    return matched
