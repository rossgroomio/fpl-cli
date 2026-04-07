"""Composition layer merging vaastav and Core-Insights historical data.

Provides a single entry point for CLI commands and services that need
cross-season player histories and intra-season GW trends.

Season allocation:
  - vaastav: 2022-23, 2023-24, 2024-25 (frozen historical data)
  - Core-Insights: 2025-26+ (current season, updated 3x daily)
"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, ClassVar

from fpl_cli.api.historical_types import GwTrendProfile, PlayerProfile, SeasonHistory
from fpl_cli.season import get_season_year, season_label_range

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

logger = logging.getLogger(__name__)


class HistoricalDataProvider:
    """Merges vaastav (historical seasons) and Core-Insights (current season)."""

    _session_profiles: ClassVar[dict[int, PlayerProfile] | None] = None

    def __init__(self, vaastav, core_insights) -> None:
        self._vaastav = vaastav
        self._core_insights = core_insights

    def _build_profile(
        self, element_code: int, seasons: list[SeasonHistory],
    ) -> PlayerProfile:
        """Delegate profile building to Core-Insights client (has same logic)."""
        return self._core_insights._build_profile(element_code, seasons)

    async def get_all_player_histories(self) -> dict[int, PlayerProfile]:
        """Get profiles spanning all 4 seasons, merged by element_code.

        Results are cached at the class level for the session.
        """
        if HistoricalDataProvider._session_profiles is not None:
            return HistoricalDataProvider._session_profiles

        vaastav_profiles = await self._vaastav.get_all_player_histories()
        ci_profiles = await self._core_insights.get_all_player_histories()

        # Merge by element_code: combine season lists from both sources.
        by_code: dict[int, list[SeasonHistory]] = {}
        for profiles in (vaastav_profiles, ci_profiles):
            for code, profile in profiles.items():
                by_code.setdefault(code, []).extend(profile.seasons)

        merged = {
            code: self._build_profile(code, seasons)
            for code, seasons in by_code.items()
        }
        HistoricalDataProvider._session_profiles = merged
        return merged

    async def get_player_history(self, element_code: int) -> PlayerProfile | None:
        """Get a single player's profile across all available seasons."""
        all_profiles = await self.get_all_player_histories()
        return all_profiles.get(element_code)

    async def get_gw_trends(
        self, last_n: int | None = None,
    ) -> dict[int, GwTrendProfile]:
        """GW trends from Core-Insights (current season only)."""
        return await self._core_insights.get_gw_trends(last_n=last_n)


@asynccontextmanager
async def make_historical_provider() -> AsyncIterator[HistoricalDataProvider]:
    """Create a HistoricalDataProvider with both underlying clients.

    Manages lifecycle of both DatasetFetcher instances.
    """
    from fpl_cli.api.core_insights import CoreInsightsClient, make_core_insights_fetcher
    from fpl_cli.api.vaastav import VaastavClient, make_vaastav_fetcher

    # Vaastav covers historical seasons (all except current).
    vaastav_seasons = season_label_range(get_season_year() - 1, count=3)

    ci_fetcher = None
    vaastav_fetcher = None
    try:
        vaastav_fetcher = make_vaastav_fetcher()
        ci_fetcher = make_core_insights_fetcher()
        vaastav = VaastavClient(vaastav_fetcher, seasons=vaastav_seasons)
        ci = CoreInsightsClient(ci_fetcher)
        yield HistoricalDataProvider(vaastav, ci)
    finally:
        if ci_fetcher is not None:
            try:
                await ci_fetcher.close()
            except Exception:  # noqa: BLE001 — cleanup resilience
                logger.debug("Error closing Core-Insights fetcher", exc_info=True)
        if vaastav_fetcher is not None:
            try:
                await vaastav_fetcher.close()
            except Exception:  # noqa: BLE001 — cleanup resilience
                logger.debug("Error closing vaastav fetcher", exc_info=True)
