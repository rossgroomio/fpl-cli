"""Football-data.org API client for Premier League standings."""

from __future__ import annotations

import logging
import os
from typing import Any

import httpx

BASE_URL = "https://api.football-data.org/v4"
logger = logging.getLogger(__name__)


class FootballDataClient:
    """Client for the football-data.org API.

    Provides Premier League standings data including goal difference
    and form, which the FPL bootstrap-static endpoint does not supply.
    """

    def __init__(self, timeout: float = 30.0):
        self.timeout = timeout
        self.api_key = os.environ.get("FOOTBALL_DATA_API_KEY")
        self._http = httpx.AsyncClient(base_url=BASE_URL, timeout=self.timeout)

    async def close(self) -> None:
        """Close the underlying HTTP client."""
        await self._http.aclose()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        await self.close()

    @property
    def is_configured(self) -> bool:
        """Check if the API key is configured."""
        return bool(self.api_key)

    async def get_standings(self) -> list[dict[str, Any]]:
        """Fetch Premier League standings.

        Returns:
            List of dicts with keys: position, name, short_name, played,
            win, draw, loss, goal_difference, points, form.
            Empty list if not configured or on error.
        """
        if not self.api_key:
            logger.warning("FOOTBALL_DATA_API_KEY not set - skipping league table")
            return []

        headers = {"X-Auth-Token": self.api_key}

        try:
            response = await self._http.get(
                "/competitions/PL/standings",
                headers=headers,
            )
            response.raise_for_status()
            data = response.json()
        except httpx.HTTPError as e:
            logger.warning("Failed to fetch standings from football-data.org: %s", e)
            return []

        # Parse the TOTAL standings table
        standings = data.get("standings", [])
        total = next((s for s in standings if s.get("type") == "TOTAL"), None)
        if not total:
            logger.warning("No TOTAL standings found in football-data.org response")
            return []

        result = []
        for entry in total.get("table", []):
            team = entry.get("team", {})
            result.append({
                "position": entry.get("position"),
                "name": team.get("shortName", team.get("name", "")),
                "short_name": team.get("tla", ""),
                "played": entry.get("playedGames", 0),
                "win": entry.get("won", 0),
                "draw": entry.get("draw", 0),
                "loss": entry.get("lost", 0),
                "goal_difference": entry.get("goalDifference", 0),
                "points": entry.get("points", 0),
            })

        return result

    async def get_matches(
        self, competition: str = "PL", season: int | None = None
    ) -> list[dict[str, Any]]:
        """Fetch completed match results.

        Args:
            competition: Competition code ("PL" for Premier League, "ELC" for Championship).
            season: Starting year (e.g. 2025 for 2025/26). Omit for current season.

        Returns:
            List of dicts with home_team_tla, away_team_tla, home_score, away_score, matchday.
            Empty list if not configured or on error.
        """
        if not self.api_key:
            logger.warning("FOOTBALL_DATA_API_KEY not set - skipping match data")
            return []

        headers = {"X-Auth-Token": self.api_key}
        params: dict[str, Any] = {"status": "FINISHED"}
        if season is not None:
            params["season"] = season

        try:
            response = await self._http.get(
                f"/competitions/{competition}/matches",
                headers=headers,
                params=params,
            )
            response.raise_for_status()
            data = response.json()
        except httpx.HTTPError as e:
            logger.warning("Failed to fetch matches from football-data.org: %s", e)
            return []

        result = []
        for match in data.get("matches", []):
            home = match.get("homeTeam", {})
            away = match.get("awayTeam", {})
            score = match.get("score", {}).get("fullTime", {})
            if score.get("home") is None or score.get("away") is None:
                continue
            result.append({
                "home_team_tla": home.get("tla", ""),
                "away_team_tla": away.get("tla", ""),
                "home_score": score["home"],
                "away_score": score["away"],
                "matchday": match.get("matchday"),
            })

        return result
