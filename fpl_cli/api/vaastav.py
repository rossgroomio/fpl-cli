"""Vaastav Fantasy-Premier-League dataset client for historical player data."""
from __future__ import annotations

import asyncio
import csv
import io
import logging
from datetime import timedelta
from typing import ClassVar

import httpx

from fpl_cli.api.contract import header_covers, warn_all_rows_skipped
from fpl_cli.api.dataset_fetcher import DatasetFetcher
from fpl_cli.api.historical_types import (
    MOMENTUM_WINDOW,
    GwTrendProfile,
    PlayerProfile,
    SeasonHistory,
    _GwRow,
    compute_acceleration,
    compute_reliability,
    compute_trend,
)
from fpl_cli.models.player import POSITION_MAP
from fpl_cli.season import season_label_range

logger = logging.getLogger(__name__)

BASE_URL = "https://raw.githubusercontent.com/vaastav/Fantasy-Premier-League/master/data"
DEFAULT_TTL = timedelta(hours=4)

# Columns the parsers below index directly (`row[...]`). The header checks
# and the `fpl doctor --providers` probe both assert against these constants,
# so the declared contract cannot drift from what the parsers consume.
# Optional columns read via `row.get(...)` are deliberately not listed.
PLAYERS_RAW_REQUIRED_COLUMNS: frozenset[str] = frozenset({
    "code", "web_name", "element_type", "team_code", "now_cost", "cost_change_start",
    "total_points", "minutes", "starts", "goals_scored", "assists",
})
MERGED_GW_REQUIRED_COLUMNS: frozenset[str] = frozenset({
    "element", "round", "value", "transfers_balance",
})

# Re-export shared types for backward compatibility
__all__ = [
    "MOMENTUM_WINDOW",
    "SeasonHistory",
    "PlayerProfile",
    "GwTrendProfile",
    "_GwRow",
    "compute_trend",
    "compute_acceleration",
]


def make_vaastav_fetcher(ttl: timedelta = DEFAULT_TTL) -> DatasetFetcher:
    """Create a DatasetFetcher configured for the vaastav GitHub dataset."""
    from fpl_cli.paths import user_cache_dir

    return DatasetFetcher(
        base_url=BASE_URL,
        cache_dir=user_cache_dir() / "datasets" / "vaastav",
        ttl=ttl,
    )


class VaastavClient:
    """Client for the vaastav/Fantasy-Premier-League GitHub dataset."""

    BASE_URL = BASE_URL
    MIN_MINUTES = 450

    # Session-level cache, shared across all instances within a single CLI run
    # (mirrors TeamRatingsService._refreshed_this_session) and keyed by the
    # season window: the default window is not the one make_historical_provider
    # hands in, so the key is what stops a client built elsewhere answering
    # for the provider with the wrong seasons.
    _session_profiles: ClassVar[dict[tuple[str, ...], dict[int, PlayerProfile]]] = {}

    # Historical seasons are effectively immutable after the season ends.
    HISTORICAL_TTL = timedelta(days=30)

    def __init__(
        self,
        fetcher: DatasetFetcher,
        seasons: tuple[str, ...] | None = None,
    ):
        self.fetcher = fetcher
        self.seasons = seasons if seasons is not None else season_label_range()
        self._season_data: dict[str, list[SeasonHistory]] | None = None
        self._gw_rows: dict[int, dict[int, _GwRow]] | None = None

    async def close(self) -> None:
        """Close the underlying fetcher."""
        await self.fetcher.close()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        await self.close()

    def _is_historical(self, season: str) -> bool:
        """A season is historical if it's not the latest configured season."""
        return season != self.seasons[-1]

    async def _fetch_csv(
        self, season: str,
    ) -> tuple[str, list[SeasonHistory]]:
        ttl = self.HISTORICAL_TTL if self._is_historical(season) else None
        try:
            text = await self.fetcher.get(f"{season}/players_raw.csv", ttl=ttl)
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404:
                logger.warning("Vaastav data not available for season %s", season)
                return season, []
            raise
        return season, self._parse_csv(text, season)

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
        """Parse a players_raw.csv into SeasonHistory objects.

        A header that no longer covers the required columns degrades to an
        empty season with a warning naming the columns, rather than the
        KeyError a renamed column used to raise mid-row.
        """
        reader = csv.DictReader(io.StringIO(text))
        if not header_covers(
            f"vaastav {season} players_raw.csv",
            reader.fieldnames,
            PLAYERS_RAW_REQUIRED_COLUMNS,
            degraded=f"historical profiles will not include {season}",
        ):
            return []
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
                # The stable club code, as Core-Insights carries it, so a
                # profile's seasons agree on what team_id means whichever
                # source served them. `team` is the season-local id, which
                # means nothing outside that season's own bootstrap.
                team_id=int(row["team_code"]),
            ))

        return histories

    def _per_90(self, stat: float, minutes: int) -> float:
        if minutes == 0:
            return 0.0
        return round((stat / minutes) * 90, 2)

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
            pts_per_90_trend=compute_trend(pts_per_90),
            cost_trajectory=compute_trend([float(c) for c in cost_values]),
            xgi_per_90=xgi_per_90,
            xgi_per_90_trend=(
                compute_trend(xgi_per_90) if len(xgi_per_90) >= 2 else None
            ),
            minutes_per_start=minutes_per_start,
            reliability=compute_reliability(seasons),
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
        process (session-level caching, per season window), so multiple
        agents in a single CLI run share the same data without re-fetching
        from GitHub.
        """
        cached = VaastavClient._session_profiles.get(self.seasons)
        if cached is not None:
            return cached

        all_data = await self._fetch_season_data()
        by_code: dict[int, list[SeasonHistory]] = {}

        for season_list in all_data.values():
            for sh in season_list:
                by_code.setdefault(sh.element_code, []).append(sh)

        profiles = {
            code: self._build_profile(code, seasons)
            for code, seasons in by_code.items()
        }
        VaastavClient._session_profiles[self.seasons] = profiles
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
        return await self.fetcher.get(f"{season}/gws/merged_gw.csv")

    def _parse_gw_rows(self, text: str) -> dict[int, dict[int, _GwRow]]:
        """Parse merged_gw.csv into grouped rows, deduplicating DGW fixtures."""
        degraded = "price-trend and transfer-momentum signals are unavailable"
        reader = csv.DictReader(io.StringIO(text))
        if not header_covers(
            "vaastav merged_gw.csv",
            reader.fieldnames,
            MERGED_GW_REQUIRED_COLUMNS,
            degraded=degraded,
        ):
            return {}

        by_player: dict[int, dict[int, _GwRow]] = {}
        row_count = 0
        for row in reader:
            row_count += 1
            try:
                element = int(row["element"])
                rnd = int(row["round"])
            except (ValueError, KeyError):
                continue

            # DGW dedup reads without inserting: a player must only enter
            # by_player once a row fully parses, or a file whose every value
            # fails conversion would fill it with empty-but-truthy buckets
            # and slip past the all-rows-skipped tripwire below.
            if rnd in by_player.get(element, {}):
                continue
            try:
                gw_row: _GwRow = {
                    "value": int(row["value"]),
                    "transfers_balance": int(row["transfers_balance"]),
                    "web_name": row.get("name", "???"),
                    "position": row.get("position", "???"),
                    "team_name": row.get("team", "???"),
                }
            except (ValueError, KeyError):
                continue
            by_player.setdefault(element, {})[rnd] = gw_row

        if row_count and not by_player:
            warn_all_rows_skipped("vaastav merged_gw.csv", row_count, degraded=degraded)
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
                price_slope=compute_trend(values),
                price_acceleration=compute_acceleration(values),
                transfer_momentum=sum(recent_balances),
                gw_count=len(sorted_rounds),
                latest_gw=sorted_rounds[-1],
                first_gw=sorted_rounds[0],
            )

        return profiles
