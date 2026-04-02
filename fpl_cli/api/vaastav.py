"""Vaastav Fantasy-Premier-League dataset client for historical player data."""
from __future__ import annotations

import asyncio
import csv
import io
import logging
from dataclasses import dataclass, field
from typing import ClassVar, TypedDict

import httpx

from fpl_cli.models.player import POSITION_MAP
from fpl_cli.season import vaastav_season_range

logger = logging.getLogger(__name__)

BASE_URL = "https://raw.githubusercontent.com/vaastav/Fantasy-Premier-League/master/data"
MOMENTUM_WINDOW = 5


@dataclass
class SeasonHistory:
    """One player, one season."""

    element_code: int
    season: str
    total_points: int
    minutes: int
    starts: int
    goals: int
    assists: int
    expected_goals: float
    expected_assists: float
    expected_goal_involvements: float
    start_cost: int
    end_cost: int
    position: str
    web_name: str
    team_id: int


@dataclass
class PlayerProfile:
    """One player across multiple seasons with computed signals."""

    element_code: int
    web_name: str
    current_position: str
    seasons: list[SeasonHistory] = field(default_factory=list)
    pts_per_90: list[float] = field(default_factory=list)
    pts_per_90_trend: float = 0.0
    cost_trajectory: float = 0.0
    xgi_per_90: list[float] = field(default_factory=list)
    xgi_per_90_trend: float | None = None
    minutes_per_start: list[float] = field(default_factory=list)


@dataclass
class GwTrendProfile:
    """One player's intra-season price and transfer trend signals."""

    element: int
    web_name: str
    position: str
    team_name: str
    price_start: int
    price_current: int
    price_change: int
    price_slope: float
    price_acceleration: float
    transfer_momentum: int
    gw_count: int
    latest_gw: int
    first_gw: int


class _GwRow(TypedDict):
    value: int
    transfers_balance: int
    web_name: str
    position: str
    team_name: str


class VaastavClient:
    """Client for the vaastav/Fantasy-Premier-League GitHub dataset."""

    BASE_URL = BASE_URL
    MIN_MINUTES = 450

    # Session-level cache: shared across all instances within a single CLI run.
    # Mirrors TeamRatingsService._refreshed_this_session pattern.
    _session_profiles: ClassVar[dict[int, PlayerProfile] | None] = None

    def __init__(
        self,
        seasons: tuple[str, ...] | None = None,
        timeout: float = 30.0,
    ):
        self.seasons = seasons if seasons is not None else vaastav_season_range()
        self.timeout = timeout
        self._season_data: dict[str, list[SeasonHistory]] | None = None
        self._gw_rows: dict[int, dict[int, _GwRow]] | None = None
        self._http = httpx.AsyncClient(base_url=BASE_URL, timeout=self.timeout)

    async def close(self) -> None:
        """Close the underlying HTTP client."""
        await self._http.aclose()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        await self.close()

    async def _fetch_csv(
        self, season: str,
    ) -> tuple[str, list[SeasonHistory]]:
        try:
            response = await self._http.get(f"/{season}/players_raw.csv")
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404:
                logger.warning("Vaastav data not available for season %s", season)
                return season, []
            raise
        return season, self._parse_csv(response.text, season)

    async def _fetch_season_data(self) -> dict[str, list[SeasonHistory]]:
        """Fetch and parse players_raw.csv for all configured seasons.

        Returns cached data on subsequent calls.
        """
        if self._season_data is not None:
            return self._season_data

        results = await asyncio.gather(
            *(self._fetch_csv(s) for s in self.seasons)
        )
        result = dict(results)
        self._season_data = result
        return result

    def _parse_csv(self, text: str, season: str) -> list[SeasonHistory]:
        """Parse a players_raw.csv into SeasonHistory objects."""
        reader = csv.DictReader(io.StringIO(text))
        histories: list[SeasonHistory] = []

        for row in reader:
            now_cost = int(row["now_cost"])
            cost_change = int(row["cost_change_start"])
            element_type = int(row["element_type"])

            histories.append(SeasonHistory(
                element_code=int(row["code"]),
                season=season,
                total_points=int(row["total_points"]),
                minutes=int(row["minutes"]),
                starts=int(row["starts"]),
                goals=int(row["goals_scored"]),
                assists=int(row["assists"]),
                expected_goals=float(row.get("expected_goals", 0) or 0),
                expected_assists=float(row.get("expected_assists", 0) or 0),
                expected_goal_involvements=float(
                    row.get("expected_goal_involvements", 0) or 0
                ),
                start_cost=now_cost - cost_change,
                end_cost=now_cost,
                position=POSITION_MAP.get(element_type, "???"),
                web_name=row["web_name"],
                team_id=int(row.get("team", 0)),
            ))

        return histories

    def _per_90(self, stat: float, minutes: int) -> float:
        if minutes == 0:
            return 0.0
        return round((stat / minutes) * 90, 2)

    def _compute_trend(self, values: list[float]) -> float:
        """Least-squares slope across season indices. Positive = improving."""
        n = len(values)
        if n <= 1:
            return 0.0
        xs = list(range(n))
        sum_x = sum(xs)
        sum_y = sum(values)
        sum_xy = sum(x * y for x, y in zip(xs, values))
        sum_x2 = sum(x * x for x in xs)
        denom = n * sum_x2 - sum_x * sum_x
        if denom == 0:
            return 0.0
        return round((n * sum_xy - sum_x * sum_y) / denom, 2)

    def _build_profile(
        self, element_code: int, seasons: list[SeasonHistory],
    ) -> PlayerProfile:
        """Build a PlayerProfile with computed signals from season data."""
        seasons.sort(key=lambda s: s.season)
        latest = seasons[-1]

        qualifying = [s for s in seasons if s.minutes >= self.MIN_MINUTES]

        pts_per_90 = [self._per_90(s.total_points, s.minutes) for s in qualifying]
        xgi_per_90_all = [
            (self._per_90(s.expected_goal_involvements, s.minutes), s)
            for s in qualifying
            if s.expected_goal_involvements > 0
        ]
        xgi_per_90 = [v for v, _ in xgi_per_90_all]
        minutes_per_start = [
            round(s.minutes / s.starts, 1) if s.starts > 0 else 0.0
            for s in qualifying
        ]

        cost_values = [s.end_cost for s in qualifying]

        return PlayerProfile(
            element_code=element_code,
            web_name=latest.web_name,
            current_position=latest.position,
            seasons=seasons,
            pts_per_90=pts_per_90,
            pts_per_90_trend=self._compute_trend(pts_per_90),
            cost_trajectory=self._compute_trend([float(c) for c in cost_values]),
            xgi_per_90=xgi_per_90,
            xgi_per_90_trend=(
                self._compute_trend(xgi_per_90) if len(xgi_per_90) >= 2 else None
            ),
            minutes_per_start=minutes_per_start,
        )

    async def get_player_history(self, element_code: int) -> PlayerProfile | None:
        """Get historical profile for a single player."""
        all_data = await self._fetch_season_data()
        player_seasons: list[SeasonHistory] = []

        for season_list in all_data.values():
            for sh in season_list:
                if sh.element_code == element_code:
                    player_seasons.append(sh)

        if not player_seasons:
            return None

        return self._build_profile(element_code, player_seasons)

    async def get_all_player_histories(self) -> dict[int, PlayerProfile]:
        """Get historical profiles for all players.

        Results are cached at the class level for the duration of the
        process (session-level caching), so multiple agents in a single
        CLI run share the same data without re-fetching from GitHub.
        """
        if VaastavClient._session_profiles is not None:
            return VaastavClient._session_profiles

        all_data = await self._fetch_season_data()
        by_code: dict[int, list[SeasonHistory]] = {}

        for season_list in all_data.values():
            for sh in season_list:
                by_code.setdefault(sh.element_code, []).append(sh)

        profiles = {
            code: self._build_profile(code, seasons)
            for code, seasons in by_code.items()
        }
        VaastavClient._session_profiles = profiles
        return profiles

    # --- Gameweek-level trend data (current season) ---

    async def get_gw_trends(
        self, last_n: int | None = None,
    ) -> dict[int, GwTrendProfile]:
        """Fetch current-season GW data, return per-player trend profiles.

        Uses merged_gw.csv from the latest season in self.seasons.
        Raw rows are cached; profiles are computed fresh per call.
        """
        if self._gw_rows is None:
            text = await self._fetch_gw_csv()
            self._gw_rows = self._parse_gw_rows(text)
        return self._compute_gw_profiles(self._gw_rows, last_n=last_n)

    async def _fetch_gw_csv(self) -> str:
        season = self.seasons[-1]
        resp = await self._http.get(f"/{season}/gws/merged_gw.csv")
        resp.raise_for_status()
        return resp.text

    def _parse_gw_rows(self, text: str) -> dict[int, dict[int, _GwRow]]:
        """Parse merged_gw.csv into grouped rows, deduplicating DGW fixtures."""
        reader = csv.DictReader(io.StringIO(text))

        by_player: dict[int, dict[int, _GwRow]] = {}
        for row in reader:
            try:
                element = int(row["element"])
                rnd = int(row["round"])
            except (ValueError, KeyError):
                continue

            player_gws = by_player.setdefault(element, {})

            if rnd in player_gws:
                continue
            try:
                player_gws[rnd] = {
                    "value": int(row["value"]),
                    "transfers_balance": int(row["transfers_balance"]),
                    "web_name": row.get("name", "???"),
                    "position": row.get("position", "???"),
                    "team_name": row.get("team", "???"),
                }
            except (ValueError, KeyError):
                continue

        return by_player

    def _compute_gw_profiles(
        self,
        by_player: dict[int, dict[int, _GwRow]],
        last_n: int | None = None,
    ) -> dict[int, GwTrendProfile]:
        """Compute per-player trend profiles from grouped GW rows."""
        profiles: dict[int, GwTrendProfile] = {}
        for element, gw_dict in by_player.items():
            if not gw_dict:
                continue
            sorted_rounds = sorted(gw_dict.keys())
            if last_n is not None:
                sorted_rounds = sorted_rounds[-last_n:]

            values = [float(gw_dict[r]["value"]) for r in sorted_rounds]
            balances = [gw_dict[r]["transfers_balance"] for r in sorted_rounds]
            latest_row = gw_dict[sorted_rounds[-1]]

            if last_n is not None:
                recent_balances = balances
            else:
                window = min(MOMENTUM_WINDOW, len(sorted_rounds))
                recent_balances = balances[-window:]

            profiles[element] = GwTrendProfile(
                element=element,
                web_name=latest_row["web_name"],
                position=latest_row["position"],
                team_name=latest_row["team_name"],
                price_start=int(values[0]),
                price_current=int(values[-1]),
                price_change=int(values[-1] - values[0]),
                price_slope=self._compute_trend(values),
                price_acceleration=self._compute_acceleration(values),
                transfer_momentum=sum(recent_balances),
                gw_count=len(sorted_rounds),
                latest_gw=sorted_rounds[-1],
                first_gw=sorted_rounds[0],
            )

        return profiles

    def _compute_acceleration(self, values: list[float]) -> float:
        """Quadratic regression coefficient. Positive = price curve bending upward."""
        n = len(values)
        if n < 4:
            return 0.0
        # Solve y = a*x^2 + b*x + c via normal equations (Cramer's rule on 3x3)
        sx = sx2 = sx3 = sx4 = sy = sxy = sx2y = 0.0
        for i, y in enumerate(values):
            x = float(i)
            x2 = x * x
            sx += x
            sx2 += x2
            sx3 += x2 * x
            sx4 += x2 * x2
            sy += y
            sxy += x * y
            sx2y += x2 * y
        det = (
            sx4 * (sx2 * n - sx * sx)
            - sx3 * (sx3 * n - sx * sx2)
            + sx2 * (sx3 * sx - sx2 * sx2)
        )
        if abs(det) < 1e-12:
            return 0.0
        det_a = (
            sx2y * (sx2 * n - sx * sx)
            - sx3 * (sxy * n - sx * sy)
            + sx2 * (sxy * sx - sx2 * sy)
        )
        return round(det_a / det, 2)
