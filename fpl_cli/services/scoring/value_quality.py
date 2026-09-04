"""Quality baseline and the VALUE scoring family.

calculate_player_quality_score is the shared weighted baseline every
scoring family builds on. compute_quality_value is the VALUE-family
pipeline shared by ``fpl player``, ``fpl stats --value``,
``fpl transfer-eval``, and the squad allocator;
compute_rolling_pts_per_m is its recent-form value companion.
blend_quality_with_prior is the early-season device the value and ownership
families share: before ``player_prior.CUTOFF_GW`` the observed raw quality is
blended with the score last season's pedigree implies, in place of the
position-mean shrinkage those families used to run. It lives here beside the
baseline it adjusts; ``ownership`` imports it and supplies its own anchor.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, Literal, overload

from fpl_cli.services.scoring.constants import (
    CALIBRATION_ELITE_TARGET,
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
from fpl_cli.services.scoring.shrinkage import is_known_unavailable

if TYPE_CHECKING:
    from fpl_cli.services.player_prior import PlayerPrior
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


# ---------------------------------------------------------------------------
# Early-season prior blend
# ---------------------------------------------------------------------------


def blend_quality_with_prior(
    raw: float,
    prior: PlayerPrior | None,
    *,
    ceiling: float,
    next_gw_id: int,
    known_unavailable: bool = False,
) -> float:
    """Blend an observed raw quality score with the score last season's pedigree implies.

    Before ``player_prior.CUTOFF_GW`` a raw quality score is one observation
    of a handful of gameweeks: going into GW2, form and ppg are the same
    single number and both caps saturate on one good game, so a one-game
    wonder out-reads a quiet-starting elite (issue #143: Haaland 59 behind
    Emersonn 100). Ceilings cannot reorder that — they are monotonic
    per-position scalers — so the prior enters the score itself::

        prior_raw = prior_strength * ceiling * CALIBRATION_ELITE_TARGET
        blended   = w * raw + (1 - w) * prior_raw     # w = prior.confidence

    *ceiling* is the calibrated anchor the caller's family measures an elite
    baseline against, for this position at this gameweek — the attainable one
    for a pre-GW6 keeper. The value family passes its whole ceiling
    (``_value_weights_and_ceiling``); the ownership family passes
    ``_ownership_anchor_for``, its ceiling *without* the matchup / ownership /
    position-need / consistency headroom, because it blends the baseline
    before adding those terms and a prior models none of them. Either way the
    prior-implied score sits on the same scale as the observed one it
    replaces: a player at the top of last season's pts/90 percentile is read
    as an elite of exactly the size the calibration anchored, and a
    price-sourced prior (strength capped at 0.5) can never claim more than
    mid-pack. The weight is ``PlayerPrior.confidence`` for every position —
    the same prior share for the same track record at the same gameweek, so
    two players with identical inputs land at the same point of their own
    scales whatever their position, which ``fpl allocate`` relies on when it
    sums raw quality across positions. It rises with the gameweek and the
    player's track record and reaches 1 by the cutoff, so the blend
    self-extinguishes; backtested over 2025-26 GW1-7 snapshots it ranked
    rest-of-season points better than pure observation at every snapshot for
    GK, DEF and FWD and was neutral for MID (value family), and better than
    the position-mean shrinkage it replaced at every position of the target,
    differential and waiver families — GK +0.15, FWD +0.22, DEF +0.08, MID
    +0.01 mean Spearman, against -0.03 (DEF) and -0.04 (MID) on the
    six-gameweek horizon those families are not primarily read on (#206).

    A keeper-specific discount on the weight (the GK calendar ramp, on the
    grounds that a keeper's early signals are sample-ramped) was evaluated
    and not shipped: the discounted share can only move onto the prior, so
    the same prior and the same empty observation read 75 for a keeper and
    43 for a forward going into GW2, and the within-position gain it bought
    (+0.08 rank correlation on ~24 keepers a snapshot) sat inside the noise.
    The backtest's finding that the prior alone out-ranks the blend for
    keepers early on still stands; a better keeper prior is the route that
    does not touch the weight.

    Pure observation is returned when there is no prior, at or after the
    cutoff, once the weight has saturated, and for a *known_unavailable*
    player: their low score states a fact (ruled out, or no minutes once the
    minutes factor is live), not a small sample, and a prior would hand them
    a standing they cannot use this gameweek — the same hold-out
    ``shrink_scores`` applies, for the same reason.
    """
    from fpl_cli.services.player_prior import CUTOFF_GW

    if prior is None or known_unavailable or next_gw_id >= CUTOFF_GW:
        return raw
    weight = max(0.0, min(prior.confidence, 1.0))
    if weight >= 1.0:
        return raw
    prior_raw = prior.prior_strength * ceiling * CALIBRATION_ELITE_TARGET
    return weight * raw + (1.0 - weight) * prior_raw


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
    prior: PlayerPrior | None = ...,
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
    prior: PlayerPrior | None = ...,
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
    prior: PlayerPrior | None = None,
) -> tuple[int, float | None] | float:
    """Compute quality_score and quality_per_m for a single player.

    Shared by ``fpl player``, ``fpl stats --value``, and the squad
    allocator. Callers handle data fetching; this function owns
    enrichment assembly and scoring.

    When *raw* is True, returns the unrounded float quality score
    (for the ILP solver which needs full precision).

    *prior* is the player's ``PlayerPrior``; before ``CUTOFF_GW`` the raw
    score is blended with the score it implies (``blend_quality_with_prior``),
    unless the player is known not to be playing. Without it the score is
    pure observation — the caller should say so before GW6.

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
    position = _as_position(player.position_name)
    weights, value_ceiling = _value_weights_and_ceiling(
        position, next_gw_id=next_gw_id,
    )
    mins_factor = calculate_mins_factor(player.minutes, player.appearances, next_gw_id)
    raw_score = calculate_player_quality_score(
        q_dict, weights, mins_factor, position=position,
    )
    raw_score = blend_quality_with_prior(
        raw_score, prior,
        ceiling=value_ceiling, next_gw_id=next_gw_id,
        known_unavailable=is_known_unavailable(
            chance_of_playing=evaluation.chance_of_playing,
            minutes=evaluation.minutes,
            next_gw_id=next_gw_id,
        ),
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
