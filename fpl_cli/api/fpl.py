"""FPL API client for fetching data from the official Fantasy Premier League API."""

from __future__ import annotations

from typing import Any, cast

import httpx

from fpl_cli.models.fixture import Fixture
from fpl_cli.models.player import Player
from fpl_cli.models.team import Team

BASE_URL = "https://fantasy.premierleague.com/api"


class FPLClient:
    """Client for the official FPL API.

    The FPL API is unauthenticated for read operations and provides
    comprehensive data about players, teams, fixtures, and gameweeks.
    """

    def __init__(self, timeout: float = 30.0):
        """Initialize the FPL API client.

        Args:
            timeout: Request timeout in seconds.
        """
        self.timeout = timeout
        self._bootstrap_data: dict[str, Any] | None = None
        self._http = httpx.AsyncClient(base_url=BASE_URL, timeout=self.timeout)

    async def close(self) -> None:
        """Close the underlying HTTP client."""
        await self._http.aclose()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        await self.close()

    async def _get(self, endpoint: str) -> dict[str, Any]:
        """Make a GET request to the FPL API.

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
        """Get the bootstrap-static data (main data dump).

        This endpoint returns all static data including:
        - All players (elements)
        - All teams
        - All gameweeks (events)
        - Game settings
        - Player positions (element_types)

        Args:
            force_refresh: If True, fetch fresh data even if cached.

        Returns:
            Bootstrap static data.
        """
        if self._bootstrap_data is None or force_refresh:
            self._bootstrap_data = await self._get("bootstrap-static/")
        return self._bootstrap_data

    async def get_players(self) -> list[Player]:
        """Get all players in the game.

        Returns:
            List of Player objects.
        """
        data = await self.get_bootstrap_static()
        return [Player.model_validate(p) for p in data["elements"]]

    async def get_player(self, player_id: int) -> Player | None:
        """Get a specific player by ID.

        Args:
            player_id: The player's FPL ID.

        Returns:
            Player object or None if not found.
        """
        players = await self.get_players()
        for player in players:
            if player.id == player_id:
                return player
        return None

    async def get_teams(self) -> list[Team]:
        """Get all Premier League teams.

        Returns:
            List of Team objects.
        """
        data = await self.get_bootstrap_static()
        return [Team.model_validate(t) for t in data["teams"]]

    async def get_team(self, team_id: int) -> Team | None:
        """Get a specific team by ID.

        Args:
            team_id: The team's FPL ID.

        Returns:
            Team object or None if not found.
        """
        teams = await self.get_teams()
        for team in teams:
            if team.id == team_id:
                return team
        return None

    async def get_fixtures(self, gameweek: int | None = None) -> list[Fixture]:
        """Get fixtures, optionally filtered by gameweek.

        Args:
            gameweek: Optional gameweek number to filter by.

        Returns:
            List of Fixture objects.
        """
        endpoint = "fixtures/"
        if gameweek is not None:
            endpoint = f"fixtures/?event={gameweek}"
        data = await self._get(endpoint)
        return [Fixture.model_validate(f) for f in data]

    async def get_gameweeks(self) -> list[dict[str, Any]]:
        """Get all gameweeks (events).

        Returns:
            List of gameweek data.
        """
        data = await self.get_bootstrap_static()
        return data["events"]

    async def get_current_gameweek(self) -> dict[str, Any] | None:
        """Get the current gameweek.

        Returns:
            Current gameweek data or None if season hasn't started.
        """
        gameweeks = await self.get_gameweeks()
        for gw in gameweeks:
            if gw["is_current"]:
                return gw
        return None

    async def get_next_gameweek(self) -> dict[str, Any] | None:
        """Get the next gameweek.

        Returns:
            Next gameweek data or None if season is over.
        """
        gameweeks = await self.get_gameweeks()
        for gw in gameweeks:
            if gw["is_next"]:
                return gw
        return None

    async def get_player_detail(self, player_id: int) -> dict[str, Any]:
        """Get detailed player data including fixture history.

        Args:
            player_id: The player's FPL ID.

        Returns:
            Player detail including history and fixtures.
        """
        return await self._get(f"element-summary/{player_id}/")

    async def get_manager_entry(self, entry_id: int) -> dict[str, Any]:
        """Get a manager's team picks.

        Note: This endpoint may require authentication for some data.

        Args:
            entry_id: The manager's entry ID.

        Returns:
            Team data including picks.
        """
        return await self._get(f"entry/{entry_id}/")

    async def get_manager_history(self, entry_id: int) -> dict[str, Any]:
        """Get a manager's historical data.

        Args:
            entry_id: The manager's entry ID.

        Returns:
            Manager history including past seasons.
        """
        return await self._get(f"entry/{entry_id}/history/")

    async def get_manager_transfers(self, entry_id: int) -> list[dict[str, Any]]:
        """Get a manager's transfer history.

        Args:
            entry_id: The manager's entry ID.

        Returns:
            List of transfers with element_in, element_out, event (gameweek), etc.
        """
        return cast(list[dict[str, Any]], await self._get(f"entry/{entry_id}/transfers/"))

    async def get_manager_picks(self, entry_id: int, gameweek: int) -> dict[str, Any]:
        """Get a manager's picks for a specific gameweek.

        Args:
            entry_id: The manager's entry ID.
            gameweek: The gameweek number.

        Returns:
            Manager's picks for the gameweek.
        """
        return await self._get(f"entry/{entry_id}/event/{gameweek}/picks/")

    async def get_classic_league_standings(
        self,
        league_id: int,
        page: int = 1,
    ) -> dict[str, Any]:
        """Get classic league standings.

        Args:
            league_id: The league ID.
            page: Page number for pagination (default 1).

        Returns:
            League standings data including league info and standings list.
        """
        return await self._get(f"leagues-classic/{league_id}/standings/?page_standings={page}")

    async def get_dream_team(self, gameweek: int) -> dict[str, Any]:
        """Get the Dream Team (Team of the Week) for a specific gameweek.

        Args:
            gameweek: The gameweek number.

        Returns:
            Dream team data including team list and top player.
        """
        return await self._get(f"dream-team/{gameweek}/")

    async def get_gameweek_live(self, gameweek: int) -> dict[str, Any]:
        """Get live gameweek data including all player points.

        Args:
            gameweek: The gameweek number.

        Returns:
            Live event data with player points for the gameweek.
        """
        return await self._get(f"event/{gameweek}/live/")

    async def get_fdr(self) -> dict[int, list[dict[str, Any]]]:
        """Get fixture difficulty ratings for all teams.

        Returns:
            Dictionary mapping team ID to list of fixture difficulties.
        """
        fixtures = await self.get_fixtures()
        teams = await self.get_teams()

        fdr: dict[int, list[dict[str, Any]]] = {t.id: [] for t in teams}

        for fixture in fixtures:
            if fixture.gameweek is None:
                continue

            # Add fixture to home team's FDR
            fdr[fixture.home_team_id].append({
                "gameweek": fixture.gameweek,
                "opponent_id": fixture.away_team_id,
                "is_home": True,
                "difficulty": fixture.home_difficulty,
            })

            # Add fixture to away team's FDR
            fdr[fixture.away_team_id].append({
                "gameweek": fixture.gameweek,
                "opponent_id": fixture.home_team_id,
                "is_home": False,
                "difficulty": fixture.away_difficulty,
            })

        # Sort each team's fixtures by gameweek
        for team_id in fdr:
            fdr[team_id].sort(key=lambda x: x["gameweek"])

        return fdr
