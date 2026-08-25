"""Quality baseline and the VALUE scoring family.

calculate_player_quality_score is the shared weighted baseline every
scoring family builds on. compute_quality_value is the VALUE-family
pipeline shared by ``fpl player``, ``fpl stats --value``,
``fpl transfer-eval``, and the squad allocator;
compute_rolling_pts_per_m is its recent-form value companion.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, Literal, overload

from fpl_cli.services.scoring.constants import (
    MINS_FACTOR_FULL_APPEARANCE,
    MINS_FACTOR_START_GW,
    POSITION_SCORE_MULTIPLIER,
    Position,
    QualityWeights,
    _as_position,
    _value_weights_and_ceiling,
)
from fpl_cli.services.scoring.display import normalise_score
from fpl_cli.services.scoring.evaluation import build_player_evaluation, build_scoring_enrichment

if TYPE_CHECKING:
    from fpl_cli.services.scoring.signals import ConsistencySignals


# ---------------------------------------------------------------------------
# Shared scoring functions
# ---------------------------------------------------------------------------


def calculate_player_quality_score(
    player: Mapping[str, Any],
    weights: QualityWeights,
    mins_factor: float = 1.0,
    *,
    position: Position | None = None,
) -> float:
    """Shared baseline quality score from form, PPG, and xGI/npxG.

    Pure computation - does not round. Callers add context-specific
    adjustments and handle rounding themselves.

    mins_factor scales per-90 attacking components (npxG, xGChain, xGI
    fallback, penalty_xG) to discount inflated rates from low-minutes
    players. Form, ppg, dc_per_90, and GK signals (saves, xgc_quality,
    cs_rate) are unscaled. When *position* is supplied the final score
    is attenuated by POSITION_SCORE_MULTIPLIER[position]. Default None
    is for formula unit tests that pass naked dicts without a position.
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
    xgi_sustainability = player.get("xgi_sustainability", 1.0)
    capped_form = min(player.get("form", 0) * weights.form.multiplier, weights.form.cap)
    score += capped_form * form_trajectory * xgi_sustainability
    score += min(player.get("ppg", 0) * weights.ppg.multiplier, weights.ppg.cap)

    if weights.dc_per_90.multiplier > 0:
        dc = player.get("dc_per_90", 0) or 0
        score += min(dc * weights.dc_per_90.multiplier, weights.dc_per_90.cap)

    if weights.gk_saves_per_90.multiplier > 0:
        sv = player.get("gk_saves_per_90", 0) or 0
        score += min(sv * weights.gk_saves_per_90.multiplier, weights.gk_saves_per_90.cap)

    if weights.gk_xgc_quality.multiplier > 0:
        xgc_q = player.get("gk_xgc_quality", 0) or 0
        score += min(xgc_q * weights.gk_xgc_quality.multiplier, weights.gk_xgc_quality.cap)

    if weights.gk_cs_rate.multiplier > 0:
        cs = player.get("gk_cs_rate", 0) or 0
        score += min(cs * weights.gk_cs_rate.multiplier, weights.gk_cs_rate.cap)

    if position is not None:
        score *= POSITION_SCORE_MULTIPLIER[position]

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
    if next_gw_id <= MINS_FACTOR_START_GW:
        return 1.0
    if appearances <= 0:
        return 0.0
    return min(minutes / (appearances * MINS_FACTOR_FULL_APPEARANCE), 1.0)


@overload
def compute_quality_value(
    player: Any,
    us_match: dict[str, Any],
    next_gw_id: int,
    *,
    team_short: str = ...,
    gw_history: list[dict[str, Any]] | None = ...,
    raw: Literal[False] = ...,
    consistency_lookup: dict[int, ConsistencySignals] | None = ...,
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
    consistency_lookup: dict[int, ConsistencySignals] | None = ...,
) -> float: ...


def compute_quality_value(
    player: Any,
    us_match: dict[str, Any],
    next_gw_id: int,
    *,
    team_short: str = "???",
    gw_history: list[dict[str, Any]] | None = None,
    raw: bool = False,
    consistency_lookup: dict[int, ConsistencySignals] | None = None,
) -> tuple[int, float | None] | float:
    """Compute quality_score and quality_per_m for a single player.

    Shared by ``fpl player``, ``fpl stats --value``, and the squad
    allocator. Callers handle data fetching; this function owns
    enrichment assembly and scoring.

    When *raw* is True, returns the unrounded float quality score
    (for the ILP solver which needs full precision).

    Returns:
        Default: (quality_score 0-100, quality_per_m or None if price is 0)
        raw=True: raw quality float
    """
    enrichment = build_scoring_enrichment(
        player, us_match, team_short, gw_history, next_gw_id,
        consistency_lookup=consistency_lookup,
    )

    evaluation, _ = build_player_evaluation(player, enrichment=enrichment)
    q_dict = evaluation.as_quality_dict()
    weights, value_ceiling = _value_weights_and_ceiling(player.position_name)
    mins_factor = calculate_mins_factor(player.minutes, player.appearances, next_gw_id)
    raw_score = calculate_player_quality_score(
        q_dict, weights, mins_factor, position=_as_position(player.position_name),
    )
    if raw:
        return raw_score
    q_score = normalise_score(raw_score, value_ceiling)
    quality_per_m = round(q_score / player.price, 1) if player.price > 0 else None
    return q_score, quality_per_m


def compute_rolling_pts_per_m(
    history: list[dict[str, Any]],
    price: float,
    window: int = 5,
) -> tuple[float | None, int | None]:
    """Rolling points per million from recent fixture history.

    Args:
        history: Player element-summary history entries (one per fixture).
        price: Raw price in £0.1m units (e.g. 100 = £10.0m).
        window: Number of qualifying fixtures to consider.

    Returns:
        (rolling_pts_per_m, fixture_count) — both None when fewer than 3
        qualifying fixtures or price <= 0.
    """
    if price <= 0:
        return None, None

    qualifying = [
        h for h in history
        if h.get("minutes", 0) > 0
    ]
    qualifying.sort(key=lambda h: (-h.get("round", 0), -h.get("fixture", 0)))
    qualifying = qualifying[:window]

    if len(qualifying) < 3:
        return None, None

    n = len(qualifying)
    total_pts = sum(h.get("total_points", 0) for h in qualifying)
    price_m = price / 10
    return round(total_pts / n / price_m, 2), n
