"""Shared data preparation for scoring agents.

prepare_scoring_data consolidates the API fetches every scoring agent
needs (teams, fixtures, ratings, predictions, and optional players /
Understat / history / priors / match data) into a ScoringData bundle,
with per-player view helpers built on the shared ScoringContext.
"""

from __future__ import annotations

import asyncio
import dataclasses
from typing import TYPE_CHECKING, Any

from fpl_cli.services.scoring.constants import FDR_MODE
from fpl_cli.services.scoring.evaluation import FixtureMatchup
from fpl_cli.services.scoring.signals import (
    ConsistencySignals,
    build_adjusted_npxg_lookup,
    build_consistency_lookup,
    compute_median_elo,
)

if TYPE_CHECKING:
    from fpl_cli.api.core_insights import MatchRecord
    from fpl_cli.models.player import Player
    from fpl_cli.services.fixture_predictions import PredictionLookup
    from fpl_cli.services.player_prior import PlayerPrior
    from fpl_cli.services.team_ratings import TeamRatingsService


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

    # Optional - populated when include_players / include_understat /
    # include_history / include_prior / include_match_data is True
    players: list[Player] | None = None
    understat_lookup: dict[int, dict[str, float]] | None = None
    player_histories: dict[int, list[dict[str, Any]]] | None = None
    player_priors: dict[int, PlayerPrior] | None = None
    match_records: dict[int, list[MatchRecord]] | None = None
    adjusted_npxg_lookup: dict[int, float] | None = None
    consistency_lookup: dict[int, ConsistencySignals] | None = None


async def prepare_scoring_data(
    client: Any,
    *,
    include_players: bool = False,
    include_understat: bool = False,
    include_history: bool = False,
    include_prior: bool = False,
    include_match_data: bool = False,
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
        include_match_data: Fetch Core-Insights match-level CSVs and compute the
            fixture-adjusted npxG lookup (requires include_players).

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
    from fpl_cli.cli._context import warn_prediction_problems
    from fpl_cli.services.fixture_predictions import (
        FixturePredictionsService,
        build_prediction_lookup,
    )

    team_map = {t.id: t for t in teams}
    fps = FixturePredictionsService()
    prediction_lookup = build_prediction_lookup(fps, team_map, min_gw=next_gw_id)
    # Goes to stderr, so agents whose commands emit JSON on stdout stay parseable.
    warn_prediction_problems(fps)

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
        from fpl_cli.services.player_prior import load_or_generate_player_priors

        player_priors = await load_or_generate_player_priors(players, next_gw_id)

    match_records: dict[int, list[MatchRecord]] | None = None
    adjusted_npxg_lookup: dict[int, float] | None = None
    consistency_lookup: dict[int, ConsistencySignals] | None = None

    if include_match_data and players is not None:
        match_records = await fetch_match_records(next_gw_id)
        if match_records:
            median_elo = compute_median_elo(match_records)
            adjusted_npxg_lookup = build_adjusted_npxg_lookup(
                match_records, next_gw_id, median_elo,
            )
            position_map = {p.id: p.position_name for p in players}
            consistency_lookup = build_consistency_lookup(
                match_records, player_histories, position_map,
                next_gw_id, median_elo,
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
        match_records=match_records,
        adjusted_npxg_lookup=adjusted_npxg_lookup,
        consistency_lookup=consistency_lookup,
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


async def fetch_match_records(
    current_gw: int,
) -> dict[int, list[MatchRecord]] | None:
    """Fetch Core-Insights match-level CSVs and return raw records.

    Owns the CoreInsightsClient lifecycle. Returns None on any failure
    (graceful degradation). Callers use the records to build derived
    lookups (adjusted npxG, consistency signals).
    """
    try:
        from fpl_cli.api.core_insights import CoreInsightsClient, make_core_insights_fetcher

        async with CoreInsightsClient(make_core_insights_fetcher()) as ci_client:
            all_match_records = await ci_client.get_match_stats(current_gw)

        return all_match_records or None
    except Exception:  # noqa: BLE001 — graceful degradation: CI match data unavailable
        import logging

        logging.getLogger(__name__).warning(
            "Failed to fetch match records", exc_info=True,
        )
        return None
