"""Centralised player scoring engine.

All scoring formulas live here: quality baseline, target, differential,
waiver, captain, and bench. Agents build PlayerEvaluation objects and
delegate scoring to this module.
"""

from __future__ import annotations

import asyncio
import dataclasses
import functools
from collections.abc import Mapping
from math import inf
from typing import TYPE_CHECKING, Any, Literal, overload

if TYPE_CHECKING:
    from fpl_cli.models.player import Player
    from fpl_cli.models.types import CaptainCandidate, FixtureDetail
    from fpl_cli.services.fixture_predictions import PredictionLookup
    from fpl_cli.services.player_prior import PlayerPrior
    from fpl_cli.services.team_ratings import TeamRatingsService


# ---------------------------------------------------------------------------
# Weight types
# ---------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class StatWeight:
    """A (multiplier, cap) pair for a single scoring component."""

    multiplier: float
    cap: float = inf


@dataclasses.dataclass(frozen=True)
class QualityWeights:
    """Weight configuration for the shared player quality baseline.

    Each field is a StatWeight(multiplier, cap) controlling how that
    stat contributes to the quality score. Agents define their own
    module-level instances with different weights.
    """

    npxg: StatWeight
    xg_chain: StatWeight
    xgi_fallback: StatWeight
    form: StatWeight
    ppg: StatWeight
    dc_per_90: StatWeight = dataclasses.field(default_factory=lambda: StatWeight(0, 0))
    penalty_xg: StatWeight = dataclasses.field(default_factory=lambda: StatWeight(0, 0))

    @functools.lru_cache(maxsize=None)
    def without_xgi(self) -> QualityWeights:
        """Return a copy with xGI-family weights zeroed and DC/90 activated (for GK/DEF)."""
        zero = StatWeight(0, 0)
        return dataclasses.replace(
            self,
            npxg=zero,
            xg_chain=zero,
            xgi_fallback=zero,
            dc_per_90=StatWeight(0.5, 2),
            penalty_xg=zero,
        )


# ---------------------------------------------------------------------------
# Weight configurations (moved from agent modules)
# ---------------------------------------------------------------------------

TARGET_QUALITY_WEIGHTS = QualityWeights(
    npxg=StatWeight(10, 8),
    xg_chain=StatWeight(2, 3),
    xgi_fallback=StatWeight(10, 10),
    form=StatWeight(1.0, 5),
    ppg=StatWeight(0.5, 4),
    dc_per_90=StatWeight(0, 0),
    penalty_xg=StatWeight(8, 3),
)

DIFFERENTIAL_QUALITY_WEIGHTS = QualityWeights(
    npxg=StatWeight(10, 8),
    xg_chain=StatWeight(2, 3),
    xgi_fallback=StatWeight(10, 10),
    form=StatWeight(1.3, 7),
    ppg=StatWeight(0.5, 4),
    dc_per_90=StatWeight(0, 0),
    penalty_xg=StatWeight(8, 3),
)

WAIVER_QUALITY_WEIGHTS = QualityWeights(
    npxg=StatWeight(5),
    xg_chain=StatWeight(2, 3),
    xgi_fallback=StatWeight(5),
    form=StatWeight(1.3, 7),
    ppg=StatWeight(0.6, 4.8),
    dc_per_90=StatWeight(0, 0),
    penalty_xg=StatWeight(8, 3),
)

GW_SELECTION_WEIGHTS = QualityWeights(
    npxg=StatWeight(5, 10),
    xg_chain=StatWeight(0, 0),
    xgi_fallback=StatWeight(5, 10),
    form=StatWeight(1.5, 10),
    ppg=StatWeight(0, 0),
    dc_per_90=StatWeight(0, 0),
    penalty_xg=StatWeight(8, 3),
)

VALUE_QUALITY_WEIGHTS = QualityWeights(
    npxg=StatWeight(10, 8),
    xg_chain=StatWeight(1, 2),
    xgi_fallback=StatWeight(10, 10),
    form=StatWeight(1.3, 7),
    ppg=StatWeight(0.8, 5),
    dc_per_90=StatWeight(0, 0),
    penalty_xg=StatWeight(8, 3),
)

# Position multiplier: adjusts ceiling for per-game scoring potential (captain + bench)
POSITION_SCORE_MULTIPLIER: dict[str, float] = {
    "FWD": 1.0,
    "MID": 1.0,
    "DEF": 0.85,
    "GK": 0.7,
}

ATTACKING_POSITIONS: frozenset[str] = frozenset({"MID", "FWD"})


# ---------------------------------------------------------------------------
# Normalisation ceilings (SGW theoretical max, MID/FWD path)
# ---------------------------------------------------------------------------

# Captain: (matchup 8*2.0 + form min(7.5*1.5,10)*1.2 + xGI ~3.5 + pen ~1.2) * pos 1.0 * mins 1.0 + home 1.0
CAPTAIN_CEILING_SGW = 32.0
# Target: npxg 8 + xg_chain 3 + form 5*1.2 + ppg 4 + penalty 3 + regression 3 + matchup 6
TARGET_CEILING = 33.0
# Differential: npxg 8 + xg_chain 3 + form 7*1.2 + ppg 4 + penalty 3 + ownership 5 + regression 3 + matchup 6
DIFFERENTIAL_CEILING = 40.4
# Waiver: quality ~24.4 (form 7*1.2) + regression 3 + matchup 6 + position 5 = 38.4
WAIVER_CEILING = 38.4
# Bench: core ~31 (matchup 12 + form 10*1.2 + xGI 4 + pen 2 + home 1) + coverage 2 + set-piece 0.5
BENCH_CEILING = 33.0
# Starting XI: same core as bench (matchup 12 + form 10*1.2 + xGI 4 + pen 2 + home 1), no bench bonuses
STARTING_XI_CEILING = 31.0
# Value: npxg 8 + xg_chain 2 + form 7*1.2 + ppg 5 + penalty 3 = 26.4 theoretical
# Practical ceiling ~23.0 (elite MID scores ~20 raw). Validated: Salah-tier -> 87-92/100
VALUE_CEILING = 23.0

# Valid formations: (DEF, MID, FWD). GK always 1.
# Ordered from most attacking to most defensive for deterministic tiebreaking.
VALID_FORMATIONS: list[tuple[int, int, int]] = [
    (3, 4, 3),
    (3, 5, 2),
    (4, 3, 3),
    (4, 4, 2),
    (4, 5, 1),
    (5, 3, 2),
    (5, 4, 1),
]

# Max PL gameweeks in a season (caps derived appearances)
MAX_GAMEWEEKS = 38

# FDR thresholds for fixture difficulty classification
FDR_EASY = 2.5
FDR_MEDIUM = 3.5

# Default FDR mode used by all scoring agents (difference = team+opponent axis)
FDR_MODE = "difference"


# ---------------------------------------------------------------------------
# ScoringContext - shared data preparation infrastructure
# ---------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class ScoringContext:
    """Pre-fetched data shared across player scoring within a single agent run.

    Agents build one context per run, then pass it to helper functions
    (build_fixture_matchups, compute_aggregate_matchup) for per-player work.
    """

    team_map: dict[int, Any]  # team_id -> Team model
    team_fixture_map: dict[int, list[dict[str, Any]]]  # team_id -> [{fixture, is_home}]
    ratings_service: TeamRatingsService

    # Optional enrichments (None when not requested)
    team_form_by_id: dict[int, dict[str, Any]] | None = None
    understat_lookup: dict[int, dict[str, Any]] | None = None
    gw_fixture_maps: list[dict[int, list[dict[str, Any]]]] | None = None
    next_gw_id: int | None = None
    prediction_lookup: PredictionLookup | None = None


async def build_scoring_context(
    *,
    teams: list[Any],
    fixtures: list[Any],
    ratings_service: Any,
    next_gw_id: int,
    all_fixtures: list[Any] | None = None,
    include_team_form: bool = False,
    understat_lookup: dict[int, dict[str, Any]] | None = None,
    prediction_lookup: PredictionLookup | None = None,
    team_map: dict[int, Any] | None = None,
) -> ScoringContext:
    """Build shared scoring context from pre-fetched data.

    Args:
        teams: List of Team models from FPL API.
        fixtures: Next-GW fixtures (used for team_fixture_map).
        ratings_service: TeamRatingsService instance.
        next_gw_id: Next gameweek ID.
        all_fixtures: All fixtures (needed for 3-GW matchup window).
        include_team_form: Whether to compute team form (needed for matchup scores).
        understat_lookup: Pre-built understat lookup (agent-owned, passed in).
        prediction_lookup: Pre-built fixture prediction lookup from
            build_prediction_lookup (gw -> team_id -> (type, multiplier)).
        team_map: Pre-built team_id -> Team map. Built internally if not provided.
    """
    from fpl_cli.services.matchup import build_gw_fixture_maps, build_team_fixture_map

    if team_map is None:
        team_map = {t.id: t for t in teams}
    team_fixture_map = build_team_fixture_map(fixtures)

    team_form_by_id: dict[int, dict[str, Any]] | None = None
    if include_team_form:
        from fpl_cli.services.team_form import calculate_team_form

        team_form_list = calculate_team_form(all_fixtures or fixtures, teams)
        team_form_by_id = {tf["team_id"]: tf for tf in team_form_list}

    gw_fixture_maps = None
    if all_fixtures is not None:
        gw_fixture_maps = build_gw_fixture_maps(all_fixtures, next_gw_id)

    return ScoringContext(
        team_map=team_map,
        team_fixture_map=team_fixture_map,
        ratings_service=ratings_service,
        team_form_by_id=team_form_by_id,
        understat_lookup=understat_lookup,
        gw_fixture_maps=gw_fixture_maps,
        next_gw_id=next_gw_id,
        prediction_lookup=prediction_lookup,
    )


_UNDERSTAT_FIELDS = ("npxG_per_90", "xGChain_per_90", "penalty_xG_per_90")


async def build_understat_by_player_id(
    all_players: list[Player],
    team_map: dict[int, Any],
    *,
    fields: tuple[str, ...] = _UNDERSTAT_FIELDS,
) -> dict[int, dict[str, float]]:
    """Build {player_id: {field: value}} from Understat for all players.

    Wraps fetch_understat_lookup with the standard Player-model adapter
    and field extraction. Returns only players with at least one matched field.
    """
    from fpl_cli.agents.common import fetch_understat_lookup

    us_adapter = [
        {
            "player_name": p.web_name,
            "position": p.position_name,
            "minutes": p.minutes,
            "_team_id": p.team_id,
        }
        for p in all_players
    ]
    us_lookup = await fetch_understat_lookup(
        us_adapter,
        lambda p: (team_map.get(p["_team_id"]).name  # type: ignore[union-attr]
                   if team_map.get(p["_team_id"]) else None),
    )
    result: dict[int, dict[str, float]] = {}
    for i, us_match in us_lookup.items():
        pid = all_players[i].id
        data: dict[str, float] = {}
        for key in fields:
            val = us_match.get(key)
            if val is not None:
                data[key] = val
        if data:
            result[pid] = data
    return result


@dataclasses.dataclass(frozen=True)
class ScoringData:
    """Pre-fetched base data shared across all scoring agents.

    Returned by ``prepare_scoring_data`` to replace the duplicated
    fetch-then-build blocks in each agent's ``run()`` method.
    """

    teams: list[Any]
    team_map: dict[int, Any]
    all_fixtures: list[Any]
    next_gw_fixtures: list[Any]
    next_gw_id: int
    next_gw: dict[str, Any] | None  # raw dict from get_next_gameweek
    scoring_ctx: ScoringContext
    ratings_service: TeamRatingsService

    # Optional - populated when include_players / include_understat / include_history / include_prior is True
    players: list[Player] | None = None
    understat_lookup: dict[int, dict[str, float]] | None = None
    player_histories: dict[int, list[dict[str, Any]]] | None = None
    player_priors: dict[int, PlayerPrior] | None = None


async def prepare_scoring_data(
    client: Any,
    *,
    include_players: bool = False,
    include_understat: bool = False,
    include_history: bool = False,
    include_prior: bool = False,
) -> ScoringData:
    """Fetch common base data and build a ScoringContext.

    Consolidates the 5+ API calls and ScoringContext construction that
    every scoring agent duplicates.  Agent-specific data (draft players,
    custom Understat fields) stays agent-owned.

    Args:
        client: FPLClient instance for API calls.
        include_players: Fetch all players via ``get_players()``.
        include_understat: Build understat lookup (requires include_players).
        include_history: Fetch per-GW history for players with minutes > 0.
        include_prior: Generate Bayesian player priors (requires include_players).

    Raises:
        ValueError: If include_understat or include_prior is True but include_players is False.
    """
    if include_understat and not include_players:
        msg = "include_understat requires include_players"
        raise ValueError(msg)
    if include_prior and not include_players:
        msg = "include_prior requires include_players"
        raise ValueError(msg)

    from fpl_cli.services.team_ratings import TeamRatingsService

    teams = await client.get_teams()
    all_fixtures = await client.get_fixtures()
    next_gw = await client.get_next_gameweek()
    next_gw_id = next_gw["id"] if next_gw else 38

    next_gw_fixtures = [f for f in all_fixtures if f.gameweek == next_gw_id] if next_gw else []

    ratings_service = TeamRatingsService()
    await ratings_service.ensure_fresh(client)

    # Build fixture prediction lookup before ScoringContext (frozen dataclass)
    from fpl_cli.services.fixture_predictions import (
        FixturePredictionsService,
        build_prediction_lookup,
    )

    team_map = {t.id: t for t in teams}
    fps = FixturePredictionsService()
    prediction_lookup = build_prediction_lookup(fps, team_map, min_gw=next_gw_id)

    scoring_ctx = await build_scoring_context(
        teams=teams,
        fixtures=next_gw_fixtures,
        ratings_service=ratings_service,
        next_gw_id=next_gw_id,
        all_fixtures=all_fixtures,
        include_team_form=True,
        prediction_lookup=prediction_lookup,
        team_map=team_map,
    )

    players: list[Player] | None = None
    understat_lookup: dict[int, dict[str, float]] | None = None

    if include_players:
        players = await client.get_players()

    if include_understat and players is not None:
        understat_lookup = await build_understat_by_player_id(
            players, scoring_ctx.team_map,
        )

    player_histories: dict[int, list[dict[str, Any]]] | None = None

    if include_history:
        history_players = players if players is not None else await client.get_players()
        candidates = [p for p in history_players if p.minutes > 0]

        player_histories = {}
        batch_size = 50
        for i in range(0, len(candidates), batch_size):
            batch = candidates[i : i + batch_size]
            tasks = [client.get_player_detail(p.id) for p in batch]
            results = await asyncio.gather(*tasks, return_exceptions=True)
            for p, result in zip(batch, results):
                if isinstance(result, dict):
                    player_histories[p.id] = result.get("history", [])

    player_priors: dict[int, PlayerPrior] | None = None

    if include_prior and players is not None:
        try:
            from fpl_cli.api.vaastav import VaastavClient
            from fpl_cli.services.player_prior import generate_player_prior, load_cached_priors

            cached = load_cached_priors(next_gw_id)
            if cached is not None:
                player_priors = cached
            else:
                from fpl_cli.season import vaastav_season
                from fpl_cli.services.player_prior import _save_prior_cache

                async with VaastavClient() as vaastav:
                    profiles = await vaastav.get_all_player_histories()
                player_priors = generate_player_prior(profiles, players, next_gw_id)
                _save_prior_cache(player_priors, vaastav_season(), next_gw_id)
        except Exception:  # noqa: BLE001 — graceful degradation: vaastav unavailable
            import logging

            logging.getLogger(__name__).warning(
                "Failed to generate player priors", exc_info=True,
            )

    return ScoringData(
        teams=teams,
        team_map=scoring_ctx.team_map,
        all_fixtures=all_fixtures,
        next_gw_fixtures=next_gw_fixtures,
        next_gw_id=next_gw_id,
        next_gw=next_gw,
        scoring_ctx=scoring_ctx,
        ratings_service=ratings_service,
        players=players,
        understat_lookup=understat_lookup,
        player_histories=player_histories,
        player_priors=player_priors,
    )


# ---------------------------------------------------------------------------
# View-building helpers (use ScoringContext)
# ---------------------------------------------------------------------------


def build_fixture_matchups(
    player_team_id: int,
    position: str,
    context: ScoringContext,
) -> list[FixtureMatchup]:
    """Build per-fixture FixtureMatchup objects from shared context.

    Uses positional FDR (not avg_overall_fdr) and computes matchup scores
    when team form is available.
    """
    from fpl_cli.services.matchup import calculate_matchup_score

    fixtures = context.team_fixture_map.get(player_team_id, [])
    if not fixtures:
        return []

    player_team = context.team_map.get(player_team_id)
    player_team_form = (
        context.team_form_by_id.get(player_team_id, {})
        if context.team_form_by_id
        else {}
    )

    matchups: list[FixtureMatchup] = []
    for f_data in fixtures:
        fixture = f_data["fixture"]
        is_home = f_data["is_home"]
        opponent_id = fixture.away_team_id if is_home else fixture.home_team_id
        opponent = context.team_map.get(opponent_id)

        opponent_short = opponent.short_name if opponent else "???"
        team_short = player_team.short_name if player_team else ""
        venue = "home" if is_home else "away"

        # Positional FDR (falls back to 4.0 inside ratings_service)
        opponent_fdr = context.ratings_service.get_positional_fdr(
            position=position,
            team=team_short,
            opponent=opponent_short,
            venue=venue,
            mode=FDR_MODE,
        )

        # Matchup score from team form (requires both teams' form data)
        opponent_form = (
            context.team_form_by_id.get(opponent_id, {})
            if context.team_form_by_id
            else {}
        )
        if player_team_form and opponent_form:
            matchup = calculate_matchup_score(
                player_team_form, opponent_form, position, is_home,
            )
        else:
            matchup = {"matchup_score": 5.0}

        matchups.append(FixtureMatchup(
            opponent_short=opponent_short,
            is_home=is_home,
            opponent_fdr=opponent_fdr,
            matchup_score=matchup["matchup_score"],
            matchup_breakdown=matchup,
        ))

    return matchups


def compute_aggregate_matchup(
    team_id: int,
    position: str,
    context: ScoringContext,
    *,
    matchup_cache: dict[tuple[int, str], float] | None = None,
) -> tuple[float | None, float | None]:
    """Compute aggregate matchup data: (matchup_avg_3gw, positional_fdr).

    Used by stats/waiver agents that need scalar matchup values rather than
    per-fixture FixtureMatchup objects.

    matchup_cache is updated in-place when provided (team+position dedup).
    """
    from fpl_cli.services.matchup import compute_3gw_matchup

    matchup_avg_3gw: float | None = None
    positional_fdr: float | None = None

    # 3-GW weighted matchup
    if context.gw_fixture_maps is not None and context.team_form_by_id is not None:
        cache_key = (team_id, position)
        if matchup_cache is not None and cache_key in matchup_cache:
            matchup_avg_3gw = matchup_cache[cache_key]
        else:
            val = compute_3gw_matchup(
                team_id=team_id,
                all_fixtures=[],  # Not used when gw_fixture_maps provided
                next_gw_id=context.next_gw_id or 38,
                team_form_by_id=context.team_form_by_id,
                position=position,
                gw_fixture_maps=context.gw_fixture_maps,
                predictions=context.prediction_lookup,
            )
            matchup_avg_3gw = round(val, 2)
            if matchup_cache is not None:
                matchup_cache[cache_key] = matchup_avg_3gw

    # Positional FDR from first next-GW fixture
    fixtures = context.team_fixture_map.get(team_id, [])
    if fixtures:
        f_data = fixtures[0]
        fixture = f_data["fixture"]
        is_home = f_data["is_home"]
        opp_id = fixture.away_team_id if is_home else fixture.home_team_id
        opp_team = context.team_map.get(opp_id)
        player_team = context.team_map.get(team_id)

        if opp_team and player_team:
            positional_fdr = round(
                context.ratings_service.get_positional_fdr(
                    position=position,
                    team=player_team.short_name,
                    opponent=opp_team.short_name,
                    venue="home" if is_home else "away",
                    mode=FDR_MODE,
                ),
                1,
            )

    return matchup_avg_3gw, positional_fdr


# ---------------------------------------------------------------------------
# Shared scoring functions
# ---------------------------------------------------------------------------


def calculate_player_quality_score(
    player: Mapping[str, Any],
    weights: QualityWeights,
    mins_factor: float = 1.0,
) -> float:
    """Shared baseline quality score from form, PPG, and xGI/npxG.

    Pure computation - does not round. Callers add context-specific
    adjustments and handle rounding themselves.

    mins_factor scales per-90 attacking components (npxG, xGChain, xGI
    fallback, penalty_xG) to discount inflated rates from low-minutes
    players. Form, ppg, and dc_per_90 are unscaled.
    """
    per90 = 0.0

    npxg = player.get("npxG_per_90")
    if npxg is not None:
        per90 += min(npxg * weights.npxg.multiplier, weights.npxg.cap)
        xg_chain = player.get("xGChain_per_90") or 0
        per90 += min(xg_chain * weights.xg_chain.multiplier, weights.xg_chain.cap)
    else:
        xgi = player.get("xGI_per_90", 0) or 0
        per90 += min(xgi * weights.xgi_fallback.multiplier, weights.xgi_fallback.cap)

    if weights.penalty_xg.multiplier > 0:
        pen = player.get("penalty_xG_per_90") or 0
        per90 += min(pen * weights.penalty_xg.multiplier, weights.penalty_xg.cap)

    score = per90 * mins_factor

    form_trajectory = player.get("form_trajectory", 1.0)
    score += min(player.get("form", 0) * weights.form.multiplier, weights.form.cap) * form_trajectory
    score += min(player.get("ppg", 0) * weights.ppg.multiplier, weights.ppg.cap)

    if weights.dc_per_90.multiplier > 0:
        dc = player.get("dc_per_90", 0) or 0
        score += min(dc * weights.dc_per_90.multiplier, weights.dc_per_90.cap)

    return score


def calculate_mins_factor(
    minutes: int,
    appearances: int,
    next_gw_id: int,
) -> float:
    """Minutes-per-appearance factor for rotation risk.

    Returns 1.0 for nailed starters, <1.0 for rotation-prone players,
    0.0 for players with no appearances. Disabled before GW5.
    """
    if next_gw_id <= 5:
        return 1.0
    if appearances <= 0:
        return 0.0
    return min(minutes / (appearances * 80), 1.0)


def compute_form_trajectory(history: list[dict[str, Any]], current_gw: int) -> float:
    """Trend multiplier from recent gameweek points history.

    Returns a value in [0.8, 1.2] reflecting whether a player is on an
    upward or downward trajectory.  Median-filters outliers (drops highest
    and lowest) to resist one-off hauls / blanks.

    Returns 1.0 (neutral) when fewer than 4 qualifying GWs are available.
    """
    cutoff = current_gw - 12
    qualifying = [
        h
        for h in history
        if h.get("minutes", 0) > 0 and h.get("round", 0) > cutoff
    ]
    qualifying.sort(key=lambda h: h["round"])
    qualifying = qualifying[-7:]  # most recent 7

    if len(qualifying) < 4:
        return 1.0

    points = [h.get("total_points", 0) for h in qualifying]

    # Median filter: drop one instance of the max and one of the min.
    # When ties exist, remove the instance closest to the centre of
    # the window (least chronologically informative) to preserve
    # slope signal from edge positions.
    filtered = list(points)
    for target in (max(filtered), min(filtered)):
        centre = (len(filtered) - 1) / 2
        indices = [i for i, v in enumerate(filtered) if v == target]
        drop = min(indices, key=lambda i: abs(i - centre))
        filtered.pop(drop)

    n = len(filtered)
    if n < 2:
        return 1.0

    # Least-squares linear regression
    x_vals = list(range(n))
    x_mean = sum(x_vals) / n
    y_mean = sum(filtered) / n

    numerator = sum((x - x_mean) * (y - y_mean) for x, y in zip(x_vals, filtered))
    denominator = sum((x - x_mean) ** 2 for x in x_vals)

    if denominator == 0:
        return 1.0

    slope = numerator / denominator

    # Clamped linear interpolation to [0.8, 1.2]
    # Neutral at slope=0; rising > 0, falling < 0
    if slope <= -1.5:
        return 0.8
    if slope <= 0.0:
        # -1.5 -> 0.8, 0.0 -> 1.0
        return 0.8 + (slope + 1.5) / 1.5 * 0.2
    if slope <= 2.0:
        # 0.0 -> 1.0, 2.0 -> 1.2
        return 1.0 + slope / 2.0 * 0.2
    return 1.2


def normalise_score(raw: float, ceiling: float) -> int:
    """Normalise a raw score to 0-100 against a ceiling."""
    return min(round(raw / ceiling * 100), 100)


def build_scoring_enrichment(
    player: Any,
    us_match: dict[str, Any],
    team_short: str,
    gw_history: list[dict[str, Any]] | None,
    next_gw_id: int,
) -> dict[str, Any]:
    """Build the enrichment dict shared by quality and single-GW scoring paths."""
    enrichment: dict[str, Any] = {"team_short": team_short, **us_match}
    minutes_safe = max(player.minutes, 1)
    enrichment["xGI_per_90"] = (
        (player.expected_goals + player.expected_assists) / minutes_safe * 90
    )
    enrichment["dc_per_90"] = player.defensive_contribution_per_90

    if gw_history:
        enrichment["form_trajectory"] = compute_form_trajectory(gw_history, next_gw_id)

    return enrichment


@overload
def compute_quality_value(
    player: Any,
    us_match: dict[str, Any],
    next_gw_id: int,
    *,
    team_short: str = ...,
    gw_history: list[dict[str, Any]] | None = ...,
    raw: Literal[False] = ...,
) -> tuple[int, float | None]: ...


@overload
def compute_quality_value(
    player: Any,
    us_match: dict[str, Any],
    next_gw_id: int,
    *,
    team_short: str = ...,
    gw_history: list[dict[str, Any]] | None = ...,
    raw: Literal[True],
) -> float: ...


def compute_quality_value(
    player: Any,
    us_match: dict[str, Any],
    next_gw_id: int,
    *,
    team_short: str = "???",
    gw_history: list[dict[str, Any]] | None = None,
    raw: bool = False,
) -> tuple[int, float | None] | float:
    """Compute quality_score and value_score for a single player.

    Shared by ``fpl player``, ``fpl stats --value``, and the squad
    allocator. Callers handle data fetching; this function owns
    enrichment assembly and scoring.

    When *raw* is True, returns the unrounded float quality score
    (for the ILP solver which needs full precision).

    Returns:
        Default: (quality_score 0-100, value_score or None if price is 0)
        raw=True: raw quality float
    """
    enrichment = build_scoring_enrichment(player, us_match, team_short, gw_history, next_gw_id)

    evaluation, _ = build_player_evaluation(player, enrichment=enrichment)
    q_dict = evaluation.as_quality_dict()
    is_defensive = player.position_name in ("GK", "DEF")
    weights = VALUE_QUALITY_WEIGHTS.without_xgi() if is_defensive else VALUE_QUALITY_WEIGHTS
    mins_factor = calculate_mins_factor(player.minutes, player.appearances, next_gw_id)
    raw_score = calculate_player_quality_score(q_dict, weights, mins_factor)
    if raw:
        return raw_score
    q_score = normalise_score(raw_score, VALUE_CEILING)
    v_score = round(q_score / player.price, 1) if player.price > 0 else None
    return q_score, v_score


def shrink_scores(
    scores: list[tuple[int, float, str]],
    prior_map: dict[int, PlayerPrior] | None,
    current_gw: int,
    cutoff_gw: int,
) -> list[tuple[int, float, str]]:
    """Apply confidence-weighted shrinkage toward position means.

    Args:
        scores: List of (player_id, score, position) tuples.
        prior_map: player_id -> PlayerPrior (or None to skip shrinkage).
        current_gw: Current gameweek number.
        cutoff_gw: GW at/after which shrinkage is disabled.

    Returns:
        List of (player_id, adjusted_score, position) in the same order.
    """
    if not scores or prior_map is None or current_gw >= cutoff_gw:
        return scores

    # Collect confidence per player (default 1.0 = no shrinkage)
    confidences: dict[int, float] = {}
    for pid, _, _ in scores:
        prior = prior_map.get(pid)
        confidences[pid] = prior.confidence if prior is not None else 1.0

    # Pass 1: confidence-weighted position means
    pos_weighted_sum: dict[str, float] = {}
    pos_weight_total: dict[str, float] = {}
    for pid, score, position in scores:
        conf = confidences[pid]
        pos_weighted_sum[position] = pos_weighted_sum.get(position, 0.0) + conf * score
        pos_weight_total[position] = pos_weight_total.get(position, 0.0) + conf

    pos_mean: dict[str, float] = {}
    for pos in pos_weighted_sum:
        total = pos_weight_total[pos]
        pos_mean[pos] = pos_weighted_sum[pos] / total if total > 0 else 0.0

    # Pass 2: shrink each score toward its position mean
    result: list[tuple[int, float, str]] = []
    for pid, score, position in scores:
        mean = pos_mean.get(position, score)
        conf = confidences[pid]
        adjusted = mean + conf * (score - mean)
        result.append((pid, adjusted, position))

    return result


def apply_shrinkage(
    scored_items: list[dict[str, Any]],
    score_field: str,
    prior_map: dict[int, PlayerPrior] | None,
    current_gw: int,
) -> None:
    """Apply early-season shrinkage to scored dicts in place.

    Extracts (id, score, position) from each dict, runs shrink_scores,
    and writes adjusted scores back. Agents call this instead of
    manually wiring the extract-shrink-writeback loop.
    """
    from fpl_cli.services.player_prior import CUTOFF_GW

    tuples = [
        (item["id"], float(item[score_field]), item["position"])
        for item in scored_items
    ]
    shrunk = shrink_scores(tuples, prior_map, current_gw, CUTOFF_GW)
    for item, (_, adj_score, _) in zip(scored_items, shrunk):
        item[score_field] = round(adj_score)


# ---------------------------------------------------------------------------
# Evaluation types
# ---------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class FixtureMatchup:
    """Pre-resolved fixture data for a single fixture within a gameweek."""

    opponent_short: str
    is_home: bool
    opponent_fdr: float
    matchup_score: float
    matchup_breakdown: dict[str, Any] | None = None


@dataclasses.dataclass(frozen=True)
class PlayerEvaluation:
    """Scoring-relevant data for a single player. Immutable.

    All fields that scoring functions read for arithmetic live here.
    Display-only fields live in PlayerIdentity.
    """

    # Quality baseline inputs (keys match calculate_player_quality_score dict interface)
    form: float
    ppg: float
    xgi_per_90: float
    npxg_per_90: float | None
    xg_chain_per_90: float | None
    dc_per_90: float
    penalty_xg_per_90: float | None

    # Minutes risk (scorers derive mins_factor from these via calculate_mins_factor)
    minutes: int
    appearances: int

    # Position (for without_xgi gate and position multiplier)
    position: str

    # Fixture data
    fixture_matchups: list[FixtureMatchup]
    matchup_avg_3gw: float | None = None
    positional_fdr: float | None = None

    # Regression inputs
    gi_minus_xgi: float = 0.0

    # Ownership (differential scoring)
    ownership: float = 0.0

    # Availability
    status: str = "a"
    chance_of_playing: int | None = None

    # Team context (waiver stacking)
    team_id: int = 0
    team_short: str = ""

    # Set pieces
    penalties_order: int | None = None
    corners_and_indirect_freekicks_order: int | None = None
    direct_freekicks_order: int | None = None

    # Form trajectory (multiplier on form contribution: 0.8=falling, 1.0=stable, 1.2=rising)
    form_trajectory: float = 1.0

    # Bayesian prior confidence (1.0=trust current data fully, <1.0=shrink toward position mean)
    prior_confidence: float = 1.0

    def as_quality_dict(self) -> dict[str, Any]:
        """Return a dict compatible with calculate_player_quality_score's Mapping interface."""
        return {
            "npxG_per_90": self.npxg_per_90,
            "xGChain_per_90": self.xg_chain_per_90,
            "xGI_per_90": self.xgi_per_90,
            "form": self.form,
            "ppg": self.ppg,
            "dc_per_90": self.dc_per_90,
            "penalty_xG_per_90": self.penalty_xg_per_90,
            "form_trajectory": self.form_trajectory,
            "prior_confidence": self.prior_confidence,
        }


@dataclasses.dataclass(frozen=True)
class PlayerIdentity:
    """Display-only fields passed through to scoring output dicts.

    No scoring function reads these for arithmetic.
    """

    id: int
    web_name: str
    team_short: str
    position_name: str
    price: float
    ownership: float
    expected_goals: float = 0.0
    expected_assists: float = 0.0
    points_per_game: float = 0.0


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


def _extract_status(val: Any) -> str:
    """Convert a PlayerStatus enum or string to its single-char status code."""
    from fpl_cli.models.player import PlayerStatus

    if isinstance(val, PlayerStatus):
        return val.value
    return str(val)


def build_player_evaluation(
    player: Player | Mapping[str, Any],
    *,
    enrichment: dict[str, Any] | None = None,
    fixture_matchups: list[FixtureMatchup] | None = None,
    matchup_avg_3gw: float | None = None,
    positional_fdr: float | None = None,
) -> tuple[PlayerEvaluation, PlayerIdentity]:
    """Build evaluation and identity from a Player model or enriched dict.

    Normalises both input shapes to the same field set. When *enrichment*
    is provided its keys overlay the base player data.
    """
    # Unified accessor: try attribute first (Player model), fall back to dict
    def _get(key: str, default: Any = None) -> Any:
        if enrichment and key in enrichment:
            return enrichment[key]
        if hasattr(player, key):
            return getattr(player, key)
        if isinstance(player, Mapping):
            return player.get(key, default)
        return default

    minutes = _get("minutes", 0)
    appearances = _get("appearances", 0)

    # Position: Player model stores as enum, dicts store as string
    position_raw = _get("position")
    if hasattr(position_raw, "value"):
        # PlayerPosition enum -> need POSITION_MAP
        from fpl_cli.models.player import POSITION_MAP

        position = POSITION_MAP.get(position_raw.value, "MID")
    else:
        position = str(position_raw) if position_raw else "MID"

    # Position name for identity (same as position for dicts, computed for model)
    position_name = _get("position_name") or position

    # Build evaluation
    evaluation = PlayerEvaluation(
        form=float(_get("form", 0)),
        ppg=float(_get("ppg") if _get("ppg") is not None else _get("points_per_game", 0)),
        xgi_per_90=float(_get("xGI_per_90", 0) or 0),
        npxg_per_90=_get("npxG_per_90"),
        xg_chain_per_90=_get("xGChain_per_90"),
        dc_per_90=float(_get("dc_per_90", 0) or 0),
        penalty_xg_per_90=_get("penalty_xG_per_90"),
        minutes=minutes,
        appearances=appearances,
        position=position,
        fixture_matchups=fixture_matchups or [],
        matchup_avg_3gw=matchup_avg_3gw,
        positional_fdr=positional_fdr,
        gi_minus_xgi=float(_get("GI_minus_xGI", 0) or 0),
        ownership=float(_get("ownership", 0) or _get("selected_by_percent", 0) or 0),
        status=_extract_status(_get("status", "a")),
        chance_of_playing=_get("chance_of_playing") or _get("chance_of_playing_next_round"),
        team_id=int(_get("team_id", 0)),
        team_short=str(_get("team_short", "")),
        penalties_order=_get("penalties_order"),
        corners_and_indirect_freekicks_order=_get("corners_and_indirect_freekicks_order"),
        direct_freekicks_order=_get("direct_freekicks_order"),
        form_trajectory=float(_get("form_trajectory", 1.0) or 1.0),
        prior_confidence=float(_get("prior_confidence", 1.0) or 1.0),
    )

    # Build identity
    identity = PlayerIdentity(
        id=int(_get("id", 0)),
        web_name=str(_get("web_name", "")),
        team_short=str(_get("team_short", "")),
        position_name=position_name,
        price=float(_get("price", 0)),
        ownership=float(_get("ownership", 0) or _get("selected_by_percent", 0) or 0),
        expected_goals=float(_get("expected_goals", 0)),
        expected_assists=float(_get("expected_assists", 0)),
        points_per_game=float(_get("ppg", 0) or _get("points_per_game", 0)),
    )

    return evaluation, identity


# ---------------------------------------------------------------------------
# Shared scoring helpers
# ---------------------------------------------------------------------------


def per_90_rates(
    evaluation: PlayerEvaluation, identity: PlayerIdentity,
) -> tuple[float, float]:
    """Derive xG and xA per-90 from season totals. Used by all single-GW consumers."""
    minutes_safe = max(evaluation.minutes, 1)
    return (
        (identity.expected_goals / minutes_safe) * 90,
        (identity.expected_assists / minutes_safe) * 90,
    )


def _matchup_bonus(matchup_avg_3gw: float | None, mins_factor: float) -> float:
    """Ownership-family matchup: 3-GW scalar average, weight 0.75, rotation-discounted.

    Parallel to ``calculate_single_gw_core``'s ``matchup_weight`` param which
    serves the same role for per-fixture data (captain 2.0, bench 1.5).
    """
    return (matchup_avg_3gw or 0.0) * 0.75 * mins_factor


def _calculate_quality_based_raw(
    evaluation: PlayerEvaluation,
    *,
    weights: QualityWeights,
    next_gw_id: int,
    ownership_config: dict[str, float] | None = None,
    mins_factor_override: float | None = None,
) -> float:
    """Raw ownership-family score before normalisation.

    Computes: quality baseline + ownership bonus + underperformance bonus +
    matchup bonus + availability penalty. Returns un-normalised float so
    callers can add formula-specific adjustments before normalising.

    *mins_factor_override*: when set, replaces the standard
    ``calculate_mins_factor`` result for both quality score and matchup
    bonus. Used by waiver scoring which applies a stricter combined
    availability factor (season commitment in draft format).
    """
    effective_weights = (
        weights
        if evaluation.position in ATTACKING_POSITIONS
        else weights.without_xgi()
    )
    mins_factor = (
        mins_factor_override
        if mins_factor_override is not None
        else calculate_mins_factor(
            evaluation.minutes, evaluation.appearances, next_gw_id,
        )
    )

    score = calculate_player_quality_score(
        evaluation.as_quality_dict(), effective_weights, mins_factor,
    )

    # Ownership bonus (differential only)
    if ownership_config is not None:
        score += max(
            0,
            (ownership_config["threshold"] - evaluation.ownership)
            / ownership_config["divisor"],
        )

    # Underperformance bonus (players due positive regression)
    if evaluation.gi_minus_xgi < -1:
        score += min(abs(evaluation.gi_minus_xgi), 3)

    score += _matchup_bonus(evaluation.matchup_avg_3gw, mins_factor)

    # Availability penalty
    if evaluation.status != "a" and evaluation.chance_of_playing is not None and evaluation.chance_of_playing < 75:
        score -= 3

    return score


def _calculate_quality_based_score(
    evaluation: PlayerEvaluation,
    *,
    weights: QualityWeights,
    ceiling: float,
    next_gw_id: int,
    ownership_config: dict[str, float] | None = None,
) -> int:
    """Shared scoring logic for target, differential, and (via raw) waiver.

    Thin wrapper: delegates to ``_calculate_quality_based_raw`` then normalises.
    """
    raw = _calculate_quality_based_raw(
        evaluation,
        weights=weights,
        next_gw_id=next_gw_id,
        ownership_config=ownership_config,
    )
    return normalise_score(raw, ceiling)


# ---------------------------------------------------------------------------
# Scoring formulas
# ---------------------------------------------------------------------------


def calculate_target_score(
    evaluation: PlayerEvaluation,
    *,
    next_gw_id: int,
) -> int:
    """Calculate a target score (pure performance, no ownership bias)."""
    return _calculate_quality_based_score(
        evaluation,
        weights=TARGET_QUALITY_WEIGHTS,
        ceiling=TARGET_CEILING,
        next_gw_id=next_gw_id,
    )


def calculate_differential_score(
    evaluation: PlayerEvaluation,
    *,
    semi_differential_threshold: float,
    next_gw_id: int,
) -> int:
    """Calculate a differential score for a player."""
    return _calculate_quality_based_score(
        evaluation,
        weights=DIFFERENTIAL_QUALITY_WEIGHTS,
        ceiling=DIFFERENTIAL_CEILING,
        next_gw_id=next_gw_id,
        ownership_config={
            "threshold": semi_differential_threshold,
            "divisor": 3,
        },
    )


def calculate_waiver_score(
    evaluation: PlayerEvaluation,
    *,
    squad_by_position: dict[str, list],
    team_counts: dict[str, int] | None = None,
    next_gw_id: int,
) -> int:
    """Calculate a waiver priority score for a player.

    Delegates shared flow (quality baseline, regression, matchup,
    availability) to ``_calculate_quality_based_raw`` with a bespoke
    combined_mins_factor (availability * per_appearance) that is
    stricter than the standard mins_factor - draft waivers are a
    season commitment so absolute playing time matters.

    Position-need and team-stacking adjustments are waiver-specific
    and applied to the raw score before normalisation.
    """
    # Combined minutes factor: per_appearance * availability (waiver-specific)
    per_appearance = calculate_mins_factor(
        evaluation.minutes, evaluation.appearances, next_gw_id,
    )
    if next_gw_id <= 5:
        combined_mins_factor = 1.0
    elif evaluation.appearances > 0:
        availability = min(evaluation.minutes / 450, 1.0)
        combined_mins_factor = availability * per_appearance
    else:
        combined_mins_factor = 0.0

    score = _calculate_quality_based_raw(
        evaluation,
        weights=WAIVER_QUALITY_WEIGHTS,
        next_gw_id=next_gw_id,
        mins_factor_override=combined_mins_factor,
    )

    # Position need bonus
    if evaluation.position in squad_by_position:
        position_players = squad_by_position[evaluation.position]
        if position_players:
            avg_form = sum(p.get("form", 0) for p in position_players) / len(position_players)
            if avg_form < 3:
                score += 3
        else:
            score += 5

    # Team stacking penalty
    if team_counts:
        current_count = team_counts.get(evaluation.team_short, 0)
        if current_count >= 3:
            score -= 5
        elif current_count == 2:
            score -= 2

    return normalise_score(score, WAIVER_CEILING)


# ---------------------------------------------------------------------------
# Single-GW core (shared by captain + bench)
# ---------------------------------------------------------------------------


def calculate_single_gw_core(
    evaluation: PlayerEvaluation,
    weights: QualityWeights,
    fixture_matchups: list[FixtureMatchup],
    *,
    matchup_weight: float,
    next_gw_id: int,
    xg_per_90: float = 0.0,
    xa_per_90: float = 0.0,
) -> float:
    """Core single-gameweek score shared by captain and bench scoring.

    Computes matchup contribution, form, xGI, penalty score, applies
    position multiplier and mins_factor, then adds home bonus.

    Args:
        matchup_weight: Per-fixture matchup multiplier (captain 2.0,
            bench 1.5). Parallel to ``_matchup_bonus``'s hardcoded 0.75
            which serves the same role for the ownership family's scalar
            3-GW average.
        xg_per_90: FPL-derived xG per 90 (from identity.expected_goals).
        xa_per_90: FPL-derived xA per 90 (from identity.expected_assists).

    Returns a raw (un-normalised) float score.
    """
    if not fixture_matchups:
        return 0.0

    fixture_count = len(fixture_matchups)

    # Sum matchup scores across fixtures (DGW sums, not averages)
    matchup_total = sum(
        (fm.matchup_breakdown or {}).get("matchup_score", fm.matchup_score)
        for fm in fixture_matchups
    )

    # Form score (capped via weights, then scaled by trajectory)
    form_score = min(evaluation.form * weights.form.multiplier, weights.form.cap) * evaluation.form_trajectory

    # xGI score: prefer npxG when available (strips penalty noise)
    if evaluation.npxg_per_90 is not None:
        xgi_score = min((evaluation.npxg_per_90 + xa_per_90) * weights.npxg.multiplier, weights.npxg.cap)
    else:
        xgi_per_90_fallback = xg_per_90 + xa_per_90
        xgi_score = min(xgi_per_90_fallback * weights.xgi_fallback.multiplier, weights.xgi_fallback.cap)

    # Scale xGI by fixture count for DGW
    xgi_score *= fixture_count

    # Penalty xG score via StatWeight (per-90, scales with mins_factor)
    pen_raw = (evaluation.penalty_xg_per_90 or 0) * weights.penalty_xg.multiplier
    penalty_score = min(pen_raw, weights.penalty_xg.cap)

    # Minutes factor
    mins_factor = calculate_mins_factor(evaluation.minutes, evaluation.appearances, next_gw_id)

    # Ceiling components with position multiplier
    pos_mult = POSITION_SCORE_MULTIPLIER.get(evaluation.position, 1.0)
    ceiling_score = (
        matchup_total * matchup_weight +
        form_score +
        xgi_score * 1.0 +
        penalty_score
    ) * pos_mult * mins_factor

    # Flat bonuses (not affected by position multiplier)
    home_bonus = 1.0 if any(fm.is_home for fm in fixture_matchups) else 0.0

    return ceiling_score + home_bonus


def calculate_captain_score(
    evaluation: PlayerEvaluation,
    identity: PlayerIdentity,
    *,
    next_gw_id: int,
) -> CaptainCandidate | None:
    """Score a player as a captain candidate.

    Returns a CaptainCandidate TypedDict (score + reasons + display data),
    or None if the player has no fixtures this gameweek.
    """
    if not evaluation.fixture_matchups:
        return None  # Blank gameweek

    fixture_count = len(evaluation.fixture_matchups)

    # Build fixture details for display and FDR calculation
    fixture_details: list[FixtureDetail] = []
    total_fdr = 0.0
    matchup_scores: list[dict[str, Any]] = []

    for fm in evaluation.fixture_matchups:
        fixture_details.append({
            "opponent": fm.opponent_short,
            "is_home": fm.is_home,
            "fdr": fm.opponent_fdr,
        })
        total_fdr += fm.opponent_fdr
        if fm.matchup_breakdown:
            matchup_scores.append(fm.matchup_breakdown)
        else:
            matchup_scores.append({"matchup_score": fm.matchup_score})

    avg_fdr = total_fdr / fixture_count

    # Matchup total for display
    matchup_total = sum(
        m.get("matchup_score", fm.matchup_score)
        for m, fm in zip(matchup_scores, evaluation.fixture_matchups)
    )

    xg_per_90, xa_per_90 = per_90_rates(evaluation, identity)

    # Delegate to shared core (captain uses matchup_weight=2.0)
    captain_score_raw = calculate_single_gw_core(
        evaluation,
        GW_SELECTION_WEIGHTS,
        evaluation.fixture_matchups,
        matchup_weight=2.0,
        next_gw_id=next_gw_id,
        xg_per_90=xg_per_90,
        xa_per_90=xa_per_90,
    )

    # pen_bonus for display (derived from StatWeight, not flat conditional)
    w = GW_SELECTION_WEIGHTS
    pen_raw = (evaluation.penalty_xg_per_90 or 0) * w.penalty_xg.multiplier
    penalty_score = min(pen_raw, w.penalty_xg.cap)
    mins_factor = calculate_mins_factor(evaluation.minutes, evaluation.appearances, next_gw_id)
    pen_bonus = round(penalty_score * mins_factor, 2)

    # Normalise to 0-100: SGW-based ceiling so DGW advantage shows naturally
    captain_score = normalise_score(captain_score_raw, CAPTAIN_CEILING_SGW)

    # Generate reasoning from ALL fixture matchups
    reasons: list[str] = []
    for matchup in matchup_scores:
        if matchup.get("reasoning"):
            reasons.extend(matchup["reasoning"])

    if avg_fdr <= FDR_EASY:
        reasons.append(f"Excellent FDR ({avg_fdr:.1f})")
    elif avg_fdr <= FDR_MEDIUM:
        reasons.append(f"Good FDR ({avg_fdr:.1f})")

    if fixture_count > 1:
        reasons.append(f"Double gameweek ({fixture_count} games)")

    if evaluation.form >= 6:
        reasons.append(f"In great form ({evaluation.form})")
    elif evaluation.form >= 4:
        reasons.append(f"In decent form ({evaluation.form})")

    xgi_per_90 = xg_per_90 + xa_per_90
    if xgi_per_90 >= 0.6:
        reasons.append(f"High xGI ({xgi_per_90:.2f}/90)")
    elif xg_per_90 >= 0.5:
        reasons.append(f"High xG ({xg_per_90:.2f}/90)")
    elif xa_per_90 >= 0.3:
        reasons.append(f"High xA ({xa_per_90:.2f}/90)")

    if any(fm.is_home for fm in evaluation.fixture_matchups):
        reasons.append("Playing at home")

    if evaluation.penalties_order == 1:
        reasons.append("Primary penalty taker")

    # Availability warning
    if evaluation.status != "a" and evaluation.chance_of_playing is not None:
        reasons.append(f"Flagged ({evaluation.chance_of_playing}% chance)")

    primary = matchup_scores[0] if matchup_scores else {}
    result: CaptainCandidate = {
        "id": identity.id,
        "player_name": identity.web_name,
        "team_short": identity.team_short,
        "position": identity.position_name,
        "price": identity.price,
        "ownership": identity.ownership,
        "form": evaluation.form,
        "ppg": identity.points_per_game,
        "xG": round(identity.expected_goals, 2),
        "xA": round(identity.expected_assists, 2),
        "xGI": round(identity.expected_goals + identity.expected_assists, 2),
        "xG_per_90": round(xg_per_90, 2),
        "xA_per_90": round(xa_per_90, 2),
        "xGI_per_90": round(xgi_per_90, 2),
        "fixtures": fixture_details,
        "fixture_count": fixture_count,
        "avg_fdr": round(avg_fdr, 2),
        "matchup_score": round(matchup_total, 2),
        "attack_matchup": round(float(primary.get("attack_matchup", 5.0)), 2),
        "defence_matchup": round(float(primary.get("defence_matchup", 5.0)), 2),
        "form_differential": round(float(primary.get("form_differential", 0.0)), 2),
        "position_differential": round(float(primary.get("position_differential", 0.0)), 2),
        "pen_bonus": pen_bonus,
        "captain_score": captain_score,
        "captain_score_raw": round(captain_score_raw, 2),
        "reasons": reasons,
    }
    return result




def calculate_bench_score(
    evaluation: PlayerEvaluation,
    identity: PlayerIdentity,
    *,
    availability_risks: list[dict[str, Any]],
    next_gw_id: int,
) -> dict[str, Any]:
    """Score a bench player for priority ordering.

    Uses the shared single-GW core (matchup + form + xGI + penalty +
    position multiplier + mins_factor + home bonus) then adds
    bench-specific bonuses on top.

    Returns a display dict with priority_score (normalised 0-100 int),
    priority_score_raw (un-normalised float), reasons, and metadata.
    """
    reasons: list[str] = []

    xg_per_90, xa_per_90 = per_90_rates(evaluation, identity)

    # Core score via shared engine (bench uses matchup_weight=1.5)
    score = calculate_single_gw_core(
        evaluation,
        GW_SELECTION_WEIGHTS,
        evaluation.fixture_matchups,
        matchup_weight=1.5,
        next_gw_id=next_gw_id,
        xg_per_90=xg_per_90,
        xa_per_90=xa_per_90,
    )

    # --- Bench-specific bonuses (outside core) ---

    # Boost for covering a risky starter at the same position
    position_at_risk = any(
        r["position"] == evaluation.position and r["risk_level"] >= 2
        for r in availability_risks
    )
    if position_at_risk:
        score += 2
        reasons.append("Covers risky starter")

    # Availability check
    if evaluation.status != "a":
        if evaluation.chance_of_playing is not None:
            if evaluation.chance_of_playing < 50:
                score -= 5
                reasons.append(f"Doubt ({evaluation.chance_of_playing}%)")
        else:
            score -= 3
            reasons.append("Availability doubt")

    # Set-piece taker micro-bonus (tiebreaker)
    if evaluation.penalties_order == 1:
        score += 0.5
        reasons.append("Primary penalty taker")
    elif (
        evaluation.corners_and_indirect_freekicks_order is not None
        or evaluation.direct_freekicks_order is not None
    ):
        score += 0.25
        reasons.append("Set-piece taker")

    raw_score = round(score, 2)
    return {
        "id": identity.id,
        "name": identity.web_name,
        "team": identity.team_short,
        "position": identity.position_name,
        "price": identity.price,
        "form": evaluation.form,
        "ppg": identity.points_per_game,
        "priority_score": normalise_score(score, BENCH_CEILING),
        "priority_score_raw": raw_score,
        "reasons": reasons if reasons else ["Standard bench option"],
    }


def calculate_lineup_score(
    evaluation: PlayerEvaluation,
    identity: PlayerIdentity,
    *,
    next_gw_id: int,
) -> dict[str, Any]:
    """Score a squad player for starting XI selection.

    Uses the shared single-GW core (matchup_weight=1.5, same as bench)
    then applies lineup-specific tiered availability penalties.  Unlike
    bench scoring, availability is gated on ``chance_of_playing`` directly
    regardless of status — any doubt signal matters for starting decisions.

    Returns a display dict with lineup_score (normalised 0-100 int),
    lineup_score_raw (un-normalised float), excluded flag, reasons, and
    metadata.
    """
    reasons: list[str] = []

    xg_per_90, xa_per_90 = per_90_rates(evaluation, identity)

    # Core score via shared engine (lineup uses matchup_weight=1.5)
    score = calculate_single_gw_core(
        evaluation,
        GW_SELECTION_WEIGHTS,
        evaluation.fixture_matchups,
        matchup_weight=1.5,
        next_gw_id=next_gw_id,
        xg_per_90=xg_per_90,
        xa_per_90=xa_per_90,
    )

    # --- Lineup-specific availability adjustment ---
    # Gates on chance_of_playing directly (forward-looking FPL flag),
    # not status-first like bench. Any doubt signal matters for starting.
    excluded = False
    exclusion_reason: str | None = None
    cop = evaluation.chance_of_playing

    if cop is not None:
        if cop < 50:
            excluded = True
            exclusion_reason = f"Low availability ({cop}%)"
            reasons.append(f"Excluded ({cop}% chance)")
        elif cop < 75:
            score -= 3
            reasons.append(f"Availability doubt ({cop}%)")
        elif cop < 100:
            score -= 1
            reasons.append(f"Minor doubt ({cop}%)")

    raw_score = round(score, 2)
    return {
        "id": identity.id,
        "name": identity.web_name,
        "team": identity.team_short,
        "position": identity.position_name,
        "price": identity.price,
        "form": evaluation.form,
        "ppg": identity.points_per_game,
        "lineup_score": normalise_score(score, STARTING_XI_CEILING),
        "lineup_score_raw": raw_score,
        "excluded": excluded,
        "exclusion_reason": exclusion_reason,
        "positional_fdr": evaluation.positional_fdr,
        "reasons": reasons if reasons else ["Available"],
    }


def select_starting_xi(
    scored_players: list[dict[str, Any]],
    *,
    team_fixtures: dict[str, dict[str, float]] | None = None,
) -> dict[str, Any]:
    """Select optimal starting XI from 15 scored squad players.

    Brute-force over 7 valid formations, picking top N per position,
    applying team exposure penalties. Deterministic: tied formations
    resolve to the most attacking option (fewest DEF).

    Args:
        scored_players: Output of calculate_lineup_score() for 15 players.
        team_fixtures: Optional {team_short: {"atk_fdr": float, "def_fdr": float}}
            for team exposure penalty. If None, no exposure penalty applied.

    Returns dict with starting_xi, bench, formation, total_score,
    team_exposure_penalties.
    """
    # Separate by position
    by_pos: dict[str, list[dict[str, Any]]] = {"GK": [], "DEF": [], "MID": [], "FWD": []}
    for p in scored_players:
        by_pos.setdefault(p["position"], []).append(p)

    # Sort each position by raw score descending, ID tiebreaker (deterministic)
    for pos in by_pos:
        by_pos[pos] = sorted(by_pos[pos], key=lambda x: (-x["lineup_score_raw"], x["id"]))

    # Partition excluded players (hard floor <50%) - they go to bench
    available: dict[str, list[dict[str, Any]]] = {}
    excluded: list[dict[str, Any]] = []
    for pos, players in by_pos.items():
        available[pos] = []
        for p in players:
            if p["excluded"]:
                excluded.append(p)
            else:
                available[pos].append(p)

    # GK: always exactly 1 starter (best available)
    gk_starter = available["GK"][0] if available["GK"] else None

    best_formation = None
    best_xi: list[dict[str, Any]] = []
    best_total = -1.0
    best_penalties: list[dict[str, Any]] = []

    for def_n, mid_n, fwd_n in VALID_FORMATIONS:
        # Check we have enough available players per position
        if len(available["DEF"]) < def_n or len(available["MID"]) < mid_n or len(available["FWD"]) < fwd_n:
            continue

        picks = {
            "DEF": available["DEF"][:def_n],
            "MID": available["MID"][:mid_n],
            "FWD": available["FWD"][:fwd_n],
        }
        outfield = picks["DEF"] + picks["MID"] + picks["FWD"]
        formation_total = sum(p["lineup_score_raw"] for p in outfield)
        if gk_starter:
            formation_total += gk_starter["lineup_score_raw"]

        # Team exposure penalty
        penalties: list[dict[str, Any]] = []
        if team_fixtures:
            team_counts: dict[str, list[dict[str, Any]]] = {}
            xi_players = ([gk_starter] if gk_starter else []) + outfield
            for p in xi_players:
                team_counts.setdefault(p["team"], []).append(p)

            for team_short, team_players in team_counts.items():
                if len(team_players) < 2:
                    continue
                tf = team_fixtures.get(team_short, {})
                for p in team_players[1:]:
                    # ATK FDR for MID/FWD, DEF FDR for GK/DEF
                    if p["position"] in ("MID", "FWD"):
                        fdr = tf.get("atk_fdr", 0.0)
                    else:
                        fdr = tf.get("def_fdr", 0.0)
                    if fdr >= 5.0:
                        formation_total -= 2
                        penalties.append({
                            "team": team_short,
                            "player": p["name"],
                            "fdr": fdr,
                            "penalty": -2,
                        })

        if formation_total > best_total:
            best_total = formation_total
            best_formation = f"{def_n}-{mid_n}-{fwd_n}"
            best_xi = ([gk_starter] if gk_starter else []) + outfield
            best_penalties = penalties

    # Bench: everyone not in the XI
    xi_ids = {p["id"] for p in best_xi}
    bench = [p for p in scored_players if p["id"] not in xi_ids]
    bench = sorted(bench, key=lambda x: (-x["lineup_score_raw"], x["id"]))

    return {
        "starting_xi": best_xi,
        "bench": bench,
        "formation": best_formation or "4-4-2",
        "total_score": round(best_total, 2),
        "team_exposure_penalties": best_penalties,
    }
