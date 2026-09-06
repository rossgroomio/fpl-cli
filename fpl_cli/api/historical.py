"""Composition layer merging vaastav and Core-Insights historical data.

Provides a single entry point for CLI commands and services that need
cross-season player histories and intra-season GW trends.

Season allocation (`historical_season_windows`): a four-season trailing
window ending at the season in progress, Core-Insights serving the newest
two -- all it publishes, refreshed 3x daily -- and vaastav the frozen two
before them. Both windows come from that one function, so they roll
together at the July cutover and cannot overlap by accident (#101).
"""
from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, ClassVar, NamedTuple

from fpl_cli.api.historical_types import GwTrendProfile, PlayerProfile, SeasonHistory
from fpl_cli.season import season_label_range

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from fpl_cli.api.core_insights import CoreInsightsClient
    from fpl_cli.api.vaastav import VaastavClient

logger = logging.getLogger(__name__)

HISTORICAL_SEASON_COUNT = 4
CORE_INSIGHTS_SEASON_COUNT = 2


class HistoricalSeasonWindows(NamedTuple):
    """The seasons each source serves, oldest first, newest last."""

    vaastav: tuple[str, ...]
    core_insights: tuple[str, ...]


def historical_season_windows(year: int | None = None) -> HistoricalSeasonWindows:
    """Split the trailing season window between the two sources, disjointly.

    Core-Insights takes the newest ``CORE_INSIGHTS_SEASON_COUNT`` seasons of
    the ``HISTORICAL_SEASON_COUNT``-season window ending at ``year`` (the
    current season by default); vaastav takes the rest. Widening one side
    without narrowing the other is exactly the overlap `merge_season_histories`
    guards against, which is why both windows come from here and nowhere
    else -- ``fpl doctor --providers`` probes the same allocation.

    >>> historical_season_windows(2026)
    HistoricalSeasonWindows(vaastav=('2023-24', '2024-25'), core_insights=('2025-26', '2026-27'))
    """
    window = season_label_range(year, count=HISTORICAL_SEASON_COUNT)
    split = HISTORICAL_SEASON_COUNT - CORE_INSIGHTS_SEASON_COUNT
    return HistoricalSeasonWindows(vaastav=window[:split], core_insights=window[split:])


def merge_season_histories(
    ranked_sources: Sequence[tuple[str, Mapping[int, PlayerProfile]]],
) -> dict[int, list[SeasonHistory]]:
    """Collect one SeasonHistory per (player, season) across ranked sources.

    Sources are read in order and the first row seen for an
    ``(element_code, season)`` pair wins. A season two sources both return
    is therefore taken from the higher-ranked one on purpose, rather than
    both rows surviving into the profile -- where every per-season mean
    would count that season twice and the reliability window would spend
    two of its three slots on it, with nothing raised (#101).

    Neither kind of repeat is expected: the source windows are allocated to
    be disjoint, and each source publishes one row per player per season.
    So each is announced once per season and source pair, the way #97 names
    provider drift, instead of being absorbed silently.
    """
    by_code: dict[int, list[SeasonHistory]] = {}
    kept_by: dict[tuple[int, str], str] = {}
    # Players affected per (season, winner, loser), not rows: the count says
    # how widespread a repeat is, and a player tripled must read as one.
    dropped: dict[tuple[str, str, str], set[int]] = {}
    for source, profiles in ranked_sources:
        for code, profile in profiles.items():
            for row in profile.seasons:
                key = (code, row.season)
                winner = kept_by.get(key)
                if winner is None:
                    kept_by[key] = source
                    by_code.setdefault(code, []).append(row)
                    continue
                dropped.setdefault((row.season, winner, source), set()).add(code)

    for (season, winner, loser), codes in sorted(dropped.items()):
        players = len(codes)
        if winner == loser:
            logger.warning(
                "%s returned %s more than once for %d player(s) — the upstream "
                "format may have changed; the first row was kept",
                winner, season, players,
            )
        else:
            logger.warning(
                "%s and %s both returned %s for %d player(s); the %s rows were "
                "kept — the historical source windows are meant to be disjoint",
                winner, loser, season, players, winner,
            )
    return by_code


class HistoricalDataProvider:
    """Merges vaastav (the older seasons) and Core-Insights (the newest)."""

    _session_profiles: ClassVar[dict[int, PlayerProfile] | None] = None

    def __init__(self, vaastav: VaastavClient, core_insights: CoreInsightsClient) -> None:
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

        # One row per (player, season), Core-Insights outranking vaastav: it
        # is the sole current-season source and refreshes 3x daily, where
        # vaastav is a volunteer archive that can lag a just-finished season.
        by_code = merge_season_histories([
            ("Core-Insights", ci_profiles),
            ("vaastav", vaastav_profiles),
        ])

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

    windows = historical_season_windows()

    ci_fetcher = None
    vaastav_fetcher = None
    try:
        vaastav_fetcher = make_vaastav_fetcher()
        ci_fetcher = make_core_insights_fetcher()
        vaastav = VaastavClient(vaastav_fetcher, seasons=windows.vaastav)
        ci = CoreInsightsClient(ci_fetcher, seasons=windows.core_insights)
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
