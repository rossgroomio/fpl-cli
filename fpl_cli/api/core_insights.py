"""FPL-Core-Insights dataset client for historical player data (2025-26+).

Provides season aggregates and GW-level trend data for the current season,
sourced from olbauday/FPL-Core-Insights which updates 3x daily.
"""
from __future__ import annotations

import asyncio
import csv
import io
import logging
from datetime import timedelta
from typing import ClassVar

import httpx

from fpl_cli.api.dataset_fetcher import DatasetFetcher
from fpl_cli.api.historical_types import (
    MOMENTUM_WINDOW,
    GwTrendProfile,
    PlayerProfile,
    SeasonHistory,
    _GwRow,
    compute_acceleration,
    compute_trend,
)
from fpl_cli.season import get_season_year, season_label

logger = logging.getLogger(__name__)

BASE_URL = "https://raw.githubusercontent.com/olbauday/FPL-Core-Insights/main/data"
DEFAULT_TTL = timedelta(hours=4)

# Core-Insights uses full position names; map to FPL abbreviations.
_POSITION_MAP = {
    "Goalkeeper": "GK",
    "Defender": "DEF",
    "Midfielder": "MID",
    "Forward": "FWD",
}


def make_core_insights_fetcher(ttl: timedelta = DEFAULT_TTL) -> DatasetFetcher:
    """Create a DatasetFetcher configured for the FPL-Core-Insights GitHub dataset."""
    from fpl_cli.paths import user_cache_dir

    return DatasetFetcher(
        base_url=BASE_URL,
        cache_dir=user_cache_dir() / "datasets" / "core-insights",
        ttl=ttl,
    )


class _PlayerLookup:
    """Parsed row from players.csv: maps FPL player_id to identity fields."""

    __slots__ = ("player_code", "web_name", "position", "team_code")

    def __init__(self, player_code: int, web_name: str, position: str, team_code: int):
        self.player_code = player_code
        self.web_name = web_name
        self.position = position
        self.team_code = team_code


class CoreInsightsClient:
    """Client for olbauday/FPL-Core-Insights GitHub dataset (2025-26+).

    Provides season aggregates and GW-level trend profiles for the current
    season. Uses DatasetFetcher for disk-cached HTTP with ETag/TTL.
    """

    MIN_MINUTES = 450
    HISTORICAL_TTL = timedelta(days=30)

    _session_profiles: ClassVar[dict[int, PlayerProfile] | None] = None

    def __init__(self, fetcher: DatasetFetcher) -> None:
        self.fetcher = fetcher
        self._season_year = get_season_year()
        self._season_label = season_label(self._season_year)
        self._ci_season = f"{self._season_year}-{self._season_year + 1}"
        self._player_lookup: dict[int, _PlayerLookup] | None = None
        self._season_data: dict[str, list[SeasonHistory]] | None = None
        self._gw_rows: dict[int, dict[int, _GwRow]] | None = None

    async def close(self) -> None:
        await self.fetcher.close()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        await self.close()

    # --- Player lookup (players.csv join) ---

    async def _fetch_player_lookup(self) -> dict[int, _PlayerLookup]:
        """Fetch players.csv and build {player_id: _PlayerLookup} mapping."""
        if self._player_lookup is not None:
            return self._player_lookup

        text = await self.fetcher.get(f"{self._ci_season}/players.csv")
        reader = csv.DictReader(io.StringIO(text))
        lookup: dict[int, _PlayerLookup] = {}
        for row in reader:
            try:
                pid = int(row["player_id"])
                lookup[pid] = _PlayerLookup(
                    player_code=int(row["player_code"]),
                    web_name=row["web_name"],
                    position=_POSITION_MAP.get(row["position"], "???"),
                    team_code=int(row["team_code"]),
                )
            except (ValueError, KeyError):
                continue

        self._player_lookup = lookup
        return lookup

    # --- Season aggregates ---

    async def _fetch_season_data(self) -> dict[str, list[SeasonHistory]]:
        """Fetch playerstats.csv for the current season, return as SeasonHistory list.

        Uses the root-level playerstats.csv (full columns including minutes/goals).
        Filters to max GW per player to get final cumulative stats.
        """
        if self._season_data is not None:
            return self._season_data

        lookup, text = await asyncio.gather(
            self._fetch_player_lookup(),
            self.fetcher.get(f"{self._ci_season}/playerstats.csv"),
        )

        reader = csv.DictReader(io.StringIO(text))

        # Collect all rows, keep only the max-GW row per player.
        best_gw: dict[int, int] = {}
        best_row: dict[int, dict[str, str]] = {}
        for row in reader:
            try:
                pid = int(row["id"])
                gw = int(row["gw"])
            except (ValueError, KeyError):
                continue
            if pid not in best_gw or gw > best_gw[pid]:
                best_gw[pid] = gw
                best_row[pid] = row

        histories: list[SeasonHistory] = []
        for pid, row in best_row.items():
            player = lookup.get(pid)
            if player is None:
                logger.debug("Player %d in playerstats but not in players.csv, skipping", pid)
                continue

            now_cost = int(round(float(row["now_cost"]) * 10))
            cost_change_start = int(round(float(row["cost_change_start"]) * 10))

            histories.append(SeasonHistory(
                element_code=player.player_code,
                season=self._season_label,
                total_points=int(row["total_points"]),
                minutes=int(row.get("minutes", 0) or 0),
                starts=int(row.get("starts", 0) or 0),
                goals=int(row.get("goals_scored", 0) or 0),
                assists=int(row.get("assists", 0) or 0),
                expected_goals=float(row.get("expected_goals", 0) or 0),
                expected_assists=float(row.get("expected_assists", 0) or 0),
                expected_goal_involvements=float(
                    row.get("expected_goal_involvements", 0) or 0
                ),
                start_cost=now_cost - cost_change_start,
                end_cost=now_cost,
                position=player.position,
                web_name=player.web_name,
                team_id=player.team_code,
            ))

        result = {self._season_label: histories}
        self._season_data = result
        return result

    def _per_90(self, stat: float, minutes: int) -> float:
        if minutes == 0:
            return 0.0
        return round((stat / minutes) * 90, 2)

    def _build_profile(
        self, element_code: int, seasons: list[SeasonHistory],
    ) -> PlayerProfile:
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
        )

    async def get_all_player_histories(self) -> dict[int, PlayerProfile]:
        """Get historical profiles for all players in the current season.

        Results are cached at the class level for the session.
        """
        if CoreInsightsClient._session_profiles is not None:
            return CoreInsightsClient._session_profiles

        all_data = await self._fetch_season_data()
        by_code: dict[int, list[SeasonHistory]] = {}
        for season_list in all_data.values():
            for sh in season_list:
                by_code.setdefault(sh.element_code, []).append(sh)

        profiles = {
            code: self._build_profile(code, seasons)
            for code, seasons in by_code.items()
        }
        CoreInsightsClient._session_profiles = profiles
        return profiles

    async def get_player_history(self, element_code: int) -> PlayerProfile | None:
        all_data = await self._fetch_season_data()
        player_seasons: list[SeasonHistory] = []
        for season_list in all_data.values():
            for sh in season_list:
                if sh.element_code == element_code:
                    player_seasons.append(sh)
        if not player_seasons:
            return None
        return self._build_profile(element_code, player_seasons)

    # --- GW-level trend data ---

    async def get_gw_trends(
        self, last_n: int | None = None,
    ) -> dict[int, GwTrendProfile]:
        """Fetch per-GW data for the current season, return trend profiles.

        Fetches individual GW files concurrently. 404s are skipped (unplayed GWs).
        """
        if self._gw_rows is None:
            self._gw_rows = await self._fetch_all_gw_rows()
        return self._compute_gw_profiles(self._gw_rows, last_n=last_n)

    async def _fetch_single_gw(self, gw: int) -> list[dict[str, str]]:
        """Fetch one GW's player_gameweek_stats.csv, return parsed rows."""
        path = f"{self._ci_season}/By Gameweek/GW{gw}/player_gameweek_stats.csv"
        try:
            ttl = self.HISTORICAL_TTL if gw < self._latest_finished_gw() else None
            text = await self.fetcher.get(path, ttl=ttl)
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404:
                return []
            raise
        reader = csv.DictReader(io.StringIO(text))
        return list(reader)

    def _latest_finished_gw(self) -> int:
        """Estimate latest finished GW from cached data, or 0 if unknown."""
        if self._gw_rows:
            all_gws = set()
            for gw_dict in self._gw_rows.values():
                all_gws.update(gw_dict.keys())
            return max(all_gws) if all_gws else 0
        return 0

    async def _fetch_all_gw_rows(self) -> dict[int, dict[int, _GwRow]]:
        """Fetch all available GW files concurrently, parse into grouped rows."""
        lookup = await self._fetch_player_lookup()

        results = await asyncio.gather(
            *(self._fetch_single_gw(gw) for gw in range(1, 39)),
        )

        by_player: dict[int, dict[int, _GwRow]] = {}
        for gw, rows in enumerate(results, start=1):
            for row in rows:
                try:
                    pid = int(row["id"])
                    now_cost = int(round(float(row["now_cost"]) * 10))
                    transfers_in = int(row.get("transfers_in_event", 0) or 0)
                    transfers_out = int(row.get("transfers_out_event", 0) or 0)
                except (ValueError, KeyError):
                    continue

                player = lookup.get(pid)
                player_gws = by_player.setdefault(pid, {})

                if gw in player_gws:
                    continue

                player_gws[gw] = {
                    "value": now_cost,
                    "transfers_balance": transfers_in - transfers_out,
                    "web_name": player.web_name if player else row.get("web_name", "???"),
                    "position": player.position if player else "???",
                    "team_name": row.get("team_name", "???"),
                }

        return by_player

    def _compute_gw_profiles(
        self,
        by_player: dict[int, dict[int, _GwRow]],
        last_n: int | None = None,
    ) -> dict[int, GwTrendProfile]:
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
