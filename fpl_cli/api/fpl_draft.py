"""FPL Draft API client for draft league functionality."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx

from fpl_cli.models.player import POSITION_MAP

logger = logging.getLogger(__name__)

BASE_URL = "https://draft.premierleague.com/api"


class FPLDraftClient:
    """Client for the FPL Draft API.

    The Draft API provides access to draft league data including:
    - League standings and details
    - Available players (free agents)
    - Waiver wire transactions
    - Team squads
    """

    def __init__(self, timeout: float = 30.0):
        """Initialize the FPL Draft API client.

        Args:
            timeout: Request timeout in seconds.
        """
        self.timeout = timeout
        self._bootstrap_data: dict[str, Any] | None = None
        self._game_state: dict[str, Any] | None = None
        self._http = httpx.AsyncClient(base_url=BASE_URL, timeout=self.timeout)

    async def close(self) -> None:
        """Close the underlying HTTP client."""
        await self._http.aclose()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
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
    ) -> dict[int, int]:
        """Get accurate ownership by querying actual team squads.

        The element-status endpoint can return stale data. This method
        builds ownership by querying each team's actual squad.

        Fetches game_state once then parallelises all entry_picks calls.

        Args:
            league_id: The draft league ID.
            bootstrap_data: Optional pre-fetched bootstrap data.

        Returns:
            Dict mapping player ID to owner entry_id.
        """
        if bootstrap_data is None:
            bootstrap_data = await self.get_bootstrap_static()

        league_details = await self.get_league_details(league_id)
        game_data = await self.get_game_state()
        current_gw = game_data.get("current_event", 1)
        player_map = {p["id"]: p for p in bootstrap_data.get("elements", [])}

        entries = league_details.get("league_entries", [])
        entry_ids = [e["entry_id"] for e in entries]

        # Fetch all squads in parallel
        results = await asyncio.gather(
            *(self.get_entry_picks(eid, current_gw) for eid in entry_ids),
            return_exceptions=True,
        )

        ownership: dict[int, int] = {}
        for entry_id, picks_result in zip(entry_ids, results):
            if isinstance(picks_result, BaseException):
                logger.debug("Failed to fetch squad for team %s: %s", entry_id, picks_result)
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

    def parse_player(self, player_data: dict[str, Any]) -> dict[str, Any]:
        """Parse raw player data into a cleaner format.

        Args:
            player_data: Raw player data from the API.

        Returns:
            Cleaned player data.
        """
        return {
            "id": player_data.get("id"),
            "player_name": player_data.get("web_name", ""),
            "first_name": player_data.get("first_name", ""),
            "second_name": player_data.get("second_name", ""),
            "team_id": player_data.get("team"),
            "position": POSITION_MAP.get(player_data.get("element_type", 0), "???"),
            "total_points": player_data.get("total_points", 0),
            "ppg": float(player_data.get("points_per_game", 0)),
            "form": float(player_data.get("form", 0)),
            "status": player_data.get("status", "a"),
            "news": player_data.get("news", ""),
            "chance_of_playing": player_data.get("chance_of_playing_next_round"),
            "goals_scored": player_data.get("goals_scored", 0),
            "assists": player_data.get("assists", 0),
            "clean_sheets": player_data.get("clean_sheets", 0),
            "minutes": player_data.get("minutes", 0),
            "expected_goals": float(player_data.get("expected_goals", 0)),
            "expected_assists": float(player_data.get("expected_assists", 0)),
        }
