"""Understat client for fetching xG and other underlying statistics."""

from __future__ import annotations

import json
import re
from typing import Any

import httpx

from fpl_cli.season import get_season_year, understat_season
from fpl_cli.utils.text import strip_diacritics

BASE_URL = "https://understat.com"

# Map FPL team names to Understat team names
TEAM_NAME_MAP = {
    "Arsenal": "Arsenal",
    "Aston Villa": "Aston Villa",
    "Bournemouth": "Bournemouth",
    "Brentford": "Brentford",
    "Brighton": "Brighton",
    "Burnley": "Burnley",
    "Chelsea": "Chelsea",
    "Crystal Palace": "Crystal Palace",
    "Everton": "Everton",
    "Fulham": "Fulham",
    "Leeds": "Leeds",
    "Liverpool": "Liverpool",
    "Man City": "Manchester City",
    "Man Utd": "Manchester United",
    "Newcastle": "Newcastle United",
    "Nott'm Forest": "Nottingham Forest",
    "Spurs": "Tottenham",
    "Sunderland": "Sunderland",
    "West Ham": "West Ham",
    "Wolves": "Wolverhampton Wanderers",
}

# Map Understat position tokens to FPL positions
POSITION_MAP = {
    "F": "FWD",
    "S": "FWD",
    "M": "MID",
    "D": "DEF",
    "GK": "GK",
}


class UnderstatClient:
    """Client for fetching data from Understat.

    Understat provides detailed xG (expected goals) and xA (expected assists)
    data for players and teams in the top 5 European leagues.
    """

    def __init__(self, timeout: float = 30.0, season_year: int | None = None):
        """Initialize the Understat client.

        Args:
            timeout: Request timeout in seconds.
            season_year: Season start year (e.g. 2025 for 2025/26).
                Defaults to the current season derived from today's date.
        """
        self.timeout = timeout
        self.season_year = season_year if season_year is not None else get_season_year()
        self._league_cache: dict[str, list[dict[str, Any]]] = {}  # season -> players
        self._http = httpx.AsyncClient(base_url=BASE_URL, timeout=self.timeout)

    async def close(self) -> None:
        """Close the underlying HTTP client."""
        await self._http.aclose()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        await self.close()

    async def _get_api_json(self, endpoint: str, referer: str) -> Any:
        """Fetch JSON from Understat's XHR API.

        Args:
            endpoint: API endpoint path (e.g. "getLeagueData/EPL/2024").
            referer: Referer URL for the request.

        Returns:
            Parsed JSON response.
        """
        headers = {
            "X-Requested-With": "XMLHttpRequest",
            "Accept": "application/json",
            "Referer": f"{BASE_URL}/{referer}",
        }
        response = await self._http.get(f"/{endpoint}", headers=headers)
        response.raise_for_status()
        return response.json()

    async def _get_html(self, endpoint: str) -> str:
        """Fetch HTML from Understat.

        Args:
            endpoint: URL endpoint.

        Returns:
            HTML content.
        """
        response = await self._http.get(f"/{endpoint}")
        response.raise_for_status()
        return response.text

    def _extract_json_data(self, html: str, var_name: str) -> Any:
        """Extract JSON data embedded in HTML.

        Understat embeds data as JavaScript variables in the page.

        Args:
            html: HTML content.
            var_name: JavaScript variable name to extract.

        Returns:
            Parsed JSON data.
        """
        # Pattern matches: var varName = JSON.parse('...')
        pattern = rf"var\s+{var_name}\s*=\s*JSON\.parse\('([^']+)'\)"
        match = re.search(pattern, html)

        if not match:
            return None

        # The data is escaped, need to decode it
        encoded_data = match.group(1)
        decoded_data = encoded_data.encode().decode("unicode_escape")
        return json.loads(decoded_data)

    async def get_league_players(self, season: str | None = None) -> list[dict[str, Any]]:
        """Get all players in the Premier League with their stats.

        Uses Understat's JSON API endpoint.

        Args:
            season: Season year (e.g., "2024" for 2024/25). Defaults to current.

        Returns:
            List of player data with xG, xA, etc.
        """
        season = season or understat_season(self.season_year)

        if season in self._league_cache:
            return self._league_cache[season]

        data = await self._get_api_json(
            f"getLeagueData/EPL/{season}",
            referer=f"league/EPL/{season}",
        )
        players_data = data.get("players") if data else None

        if not players_data:
            return []

        parsed = [self._parse_player(p) for p in players_data]
        self._league_cache[season] = parsed
        return parsed

    async def get_player(self, player_id: int) -> dict[str, Any] | None:
        """Get detailed stats for a specific player.

        Uses Understat's JSON API endpoint.

        Args:
            player_id: Understat player ID.

        Returns:
            Player data with match-by-match xG, xA, shots, and situation groups.
        """
        try:
            data = await self._get_api_json(
                f"getPlayerData/{player_id}",
                referer=f"player/{player_id}",
            )
            return {
                "id": player_id,
                "matches": data.get("matches") or [],
                "shots": data.get("shots") or [],
                "groups": data.get("groups") or {},
            }
        except (httpx.HTTPStatusError, httpx.RequestError):
            return None

    async def get_team(self, team_name: str, season: str | None = None) -> dict[str, Any] | None:
        """Get team stats including match xG data.

        Uses Understat's JSON API endpoint rather than HTML scraping.

        Args:
            team_name: Team name (FPL format, will be mapped).
            season: Season year (start year, e.g. "2025" for 2025/26). Defaults to current.

        Returns:
            Team data with player stats and match records.
        """
        season = season or understat_season(self.season_year)

        # Map FPL team name to Understat format
        understat_name = TEAM_NAME_MAP.get(team_name, team_name)
        url_name = understat_name.replace(" ", "_")

        data = await self._get_team_json(url_name, season)
        if data is None:
            return None

        return {
            "team": team_name,
            "players": [self._parse_player(p) for p in (data.get("players") or [])],
            "matches": data.get("dates") or [],
        }

    async def _get_team_json(self, url_name: str, season: str) -> dict[str, Any] | None:
        """Fetch team data from Understat JSON API.

        Args:
            url_name: Team name with spaces replaced by underscores.
            season: Season start year.

        Returns:
            Parsed JSON response or None on error.
        """
        try:
            return await self._get_api_json(
                f"getTeamData/{url_name}/{season}",
                referer=f"team/{url_name}/{season}",
            )
        except (httpx.HTTPStatusError, httpx.RequestError):
            return None

    def _parse_player(self, data: dict[str, Any]) -> dict[str, Any]:
        """Parse raw player data into a cleaner format.

        Args:
            data: Raw player data from Understat.

        Returns:
            Cleaned player data.
        """
        minutes = int(data.get("time", 0))
        xg = float(data.get("xG", 0))
        xa = float(data.get("xA", 0))
        npxg = float(data.get("npxG", 0))
        xg_chain = float(data.get("xGChain", 0))
        xg_buildup = float(data.get("xGBuildup", 0))

        return {
            "id": int(data.get("id", 0)),
            "name": data.get("player_name", ""),
            "team": data.get("team_title", ""),
            "position": data.get("position", ""),
            "games": int(data.get("games", 0)),
            "minutes": minutes,
            "goals": int(data.get("goals", 0)),
            "assists": int(data.get("assists", 0)),
            "xG": xg,
            "xA": xa,
            "npxG": npxg,
            "xGChain": xg_chain,
            "xGBuildup": xg_buildup,
            "shots": int(data.get("shots", 0)),
            "key_passes": int(data.get("key_passes", 0)),
            "npg": int(data.get("npg", 0)),
            # Per-90 metrics
            "xG_per_90": self._per_90(xg, minutes),
            "xA_per_90": self._per_90(xa, minutes),
            "xGI_per_90": self._per_90(xg + xa, minutes),
            "npxG_per_90": self._per_90(npxg, minutes),
            "xGChain_per_90": self._per_90(xg_chain, minutes),
            "xGBuildup_per_90": self._per_90(xg_buildup, minutes),
            # Over/underperformance
            "goals_minus_xG": int(data.get("goals", 0)) - xg,
            "assists_minus_xA": int(data.get("assists", 0)) - xa,
            "penalty_xG": round(xg - npxg, 2),
            "penalty_xG_per_90": self._per_90(xg - npxg, minutes),
        }

    def _per_90(self, stat: float, minutes: int) -> float:
        """Calculate per-90-minute stat.

        Args:
            stat: Total stat value.
            minutes: Total minutes played.

        Returns:
            Stat per 90 minutes.
        """
        if minutes == 0:
            return 0.0
        return round((stat / minutes) * 90, 2)


def _normalise(text: str) -> str:
    """Strip diacritics and lowercase for cross-source name comparison."""
    return strip_diacritics(text).lower()


def match_fpl_to_understat(
    fpl_name: str,
    fpl_team: str,
    understat_players: list[dict[str, Any]],
    fpl_position: str | None = None,
    fpl_minutes: int | None = None,
) -> dict[str, Any] | None:
    """Match an FPL player to their Understat data using multi-signal scoring.

    Scores candidates on name match quality, position, and minutes played.
    Returns highest-confidence match above threshold, or None.
    """
    import logging

    logger = logging.getLogger(__name__)
    fpl_name_norm = _normalise(fpl_name)
    fpl_team_mapped = TEAM_NAME_MAP.get(fpl_team, fpl_team)

    # Extract initial and surname from "X.Surname" or "X. Surname" FPL web_name format
    fpl_surname = None
    fpl_initial = None
    if "." in fpl_name_norm and len(fpl_name_norm.split(".")[0]) <= 2:
        fpl_initial = fpl_name_norm.split(".")[0].strip()
        fpl_surname = fpl_name_norm.split(".")[-1].strip()

    best_match = None
    best_score = 0

    for player in understat_players:
        if player["team"] != fpl_team_mapped:
            continue

        score = 0
        understat_name = _normalise(player["name"])
        name_parts = understat_name.split()

        # Name scoring
        if fpl_name_norm == understat_name:
            score += 10  # Exact match
        elif fpl_name_norm in understat_name:
            score += 6  # Substring match
        elif fpl_name_norm in name_parts:
            score += 7  # Exact surname match
        elif fpl_surname and fpl_surname in name_parts:
            score += 7  # "M.Salah" -> "salah" in ["mohamed", "salah"]
            # Bonus if initial matches first name (e.g. "B" matches "Bernardo")
            if fpl_initial and len(name_parts) > 1:
                other_parts = [p for p in name_parts if p != fpl_surname]
                if any(p.startswith(fpl_initial) for p in other_parts):
                    score += 2
        else:
            continue  # No name match at all

        # Position bonus
        if fpl_position and player.get("position"):
            understat_positions = {
                POSITION_MAP.get(tok)
                for tok in player["position"].split()
                if tok in POSITION_MAP
            }
            if fpl_position in understat_positions:
                score += 2

        # Minutes proximity bonus
        if fpl_minutes and player.get("minutes"):
            ratio = min(fpl_minutes, player["minutes"]) / max(fpl_minutes, player["minutes"], 1)
            if ratio >= 0.8:
                score += 2
            elif ratio >= 0.5:
                score += 1

        if score > best_score:
            best_score = score
            best_match = player

    if best_score < 5:
        if best_match:
            logger.warning(
                "Low-confidence Understat match: %s -> %s (score=%d)",
                fpl_name, best_match["name"], best_score,
            )
        return None

    return best_match
