"""The ownership scoring family: target, differential, and waiver.

Multi-gameweek acquisition scores built on the shared quality baseline
plus the scalar 3-GW matchup bonus, ownership / position-need
adjustments, and the consistency bonus.

Before ``player_prior.CUTOFF_GW`` the quality baseline carries the same
early-season prior blend the value family runs (``blend_quality_with_prior``),
anchored on the family's own calibrated anchor rather than the value one, and
in place of the position-mean shrinkage these families used to stack on top
of their normalised scores (#206). The bonus terms stay pure observation —
the prior models a player's pedigree, not the fixtures in front of them.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal

from fpl_cli.services.scoring.constants import (
    _MATCHUP_MAX,
    _OWNERSHIP_HEADROOM,
    ATTACKING_POSITIONS,
    CONSISTENCY_CV_DIFF,
    CONSISTENCY_CV_TARGET,
    DIFFERENTIAL_QUALITY_WEIGHTS,
    MINS_FACTOR_START_GW,
    TARGET_QUALITY_WEIGHTS,
    WAIVER_QUALITY_WEIGHTS,
    QualityWeights,
    _consistency_phase,
    _ownership_anchor_for,
)
from fpl_cli.services.scoring.display import normalise_score
from fpl_cli.services.scoring.shrinkage import is_known_unavailable
from fpl_cli.services.scoring.value_quality import (
    blend_quality_with_prior,
    calculate_mins_factor,
    calculate_player_quality_score,
)

if TYPE_CHECKING:
    from fpl_cli.services.player_prior import PlayerPrior
    from fpl_cli.services.scoring.evaluation import PlayerEvaluation


def _matchup_bonus(matchup_avg_3gw: float | None, mins_factor: float) -> float:
    """Ownership-family matchup: 3-GW scalar average, weight 0.75, rotation-discounted.

    Parallel to ``single_gw.calculate_single_gw_core``'s ``matchup_weight``
    param which serves the same role for per-fixture data (captain 2.0,
    bench 1.5).

    Clamped to the ``_MATCHUP_MAX`` headroom the calibrated ceilings budget
    for it. DGW windows push ``matchup_avg_3gw`` past the single-fixture
    scale (fixtures are summed within a gameweek — a confirmed double can
    average 12+), and an unclamped bonus would blow through the ceiling and
    pin every elite player in the window at 100, erasing the quality and
    consistency discrimination between them — with real ordering fallout on
    the waiver list, which sorts on the normalised score. Clamping the
    shared team-level term instead keeps the per-player terms deciding.
    """
    return min((matchup_avg_3gw or 0.0) * 0.75, _MATCHUP_MAX) * mins_factor


def _calculate_quality_based_raw(
    evaluation: PlayerEvaluation,
    *,
    weights: QualityWeights,
    next_gw_id: int,
    anchor: float,
    prior: PlayerPrior | None = None,
    ownership_config: dict[str, float] | None = None,
    mins_factor_override: float | None = None,
    differential: bool = False,
) -> float:
    """Raw ownership-family score before normalisation.

    Computes: quality baseline (prior-blended before the cutoff) + ownership
    bonus + matchup bonus + consistency bonus + availability penalty. Returns
    un-normalised float so callers can add formula-specific adjustments before
    normalising.

    *anchor* is the family's calibrated quality anchor for this position at
    this gameweek — ``_ownership_anchor_for``, i.e. the ceiling the caller
    normalises against minus its bonus headroom. Only the blend reads it.

    *prior*: before ``player_prior.CUTOFF_GW`` the quality baseline is blended
    with the score last season's pedigree implies, exactly as the value family
    does (``blend_quality_with_prior``) and in place of the position-mean
    shrinkage these families used to apply after normalisation (#206). Going
    into GW2 form and ppg are one observation of one match and both caps
    saturate on a single good game, so a one-game wonder out-ranks a
    quiet-starting elite. Shrinkage compresses that gap rather than closing
    it: it swaps two players only when their confidences differ enough to
    overcome the distance between their scores, which is nowhere near enough
    to move a saturated one-game score off the top of a list.

    The blend sits between the baseline and the bonuses on purpose. The prior
    is a statement about the player — last season's pts/90 percentile, or
    price for a player without PL history — and models none of what the bonus
    terms measure: the fixtures in front of them, how many managers own them,
    which slot of your squad is thin, or how volatile their returns have been.
    Blending those in would credit a pedigree for a fixture run it never
    played. That also fixes the scale the prior is read on: the anchor is what
    an elite *baseline* reaches, so a top-percentile prior is read as exactly
    that elite and the bonus headroom stays reachable on top.

    *mins_factor_override*: when set, replaces the standard
    ``calculate_mins_factor`` result for both quality score and matchup
    bonus. Used by waiver scoring which applies a stricter combined
    availability factor (season commitment in draft format).

    *differential*: when True, inverts the consistency bonus direction
    (volatile players score higher).
    """
    if evaluation.position in ATTACKING_POSITIONS:
        effective_weights = weights
    elif evaluation.position == "GK":
        effective_weights = weights.for_gk()
    else:
        effective_weights = weights.without_xgi()
    mins_factor = (
        mins_factor_override
        if mins_factor_override is not None
        else calculate_mins_factor(
            evaluation.minutes, evaluation.appearances, next_gw_id,
        )
    )

    score = calculate_player_quality_score(
        evaluation.as_quality_dict(),
        effective_weights,
        mins_factor,
        position=evaluation.position,
    )

    # Early-season prior blend, on the baseline only and before every bonus.
    # Holds out players known not to be playing for the same reason shrinkage
    # did (#122): their low score is an observed fact, not a small sample.
    score = blend_quality_with_prior(
        score, prior,
        ceiling=anchor, next_gw_id=next_gw_id,
        known_unavailable=is_known_unavailable(
            chance_of_playing=evaluation.chance_of_playing,
            minutes=evaluation.minutes,
            next_gw_id=next_gw_id,
        ),
    )

    # Ownership bonus (differential only)
    if ownership_config is not None:
        score += max(
            0,
            (ownership_config["threshold"] - evaluation.ownership)
            / ownership_config["divisor"],
        )

    score += _matchup_bonus(evaluation.matchup_avg_3gw, mins_factor)

    # Consistency bonus (additive, phase-in GW6-10)
    phase = _consistency_phase(next_gw_id)
    if phase > 0:
        cv = evaluation.cv_xgi_percentile
        if differential:
            score += (0.5 - cv) * CONSISTENCY_CV_DIFF * phase
        else:
            score += (cv - 0.5) * CONSISTENCY_CV_TARGET * phase

    # Availability penalty
    if evaluation.status != "a" and evaluation.chance_of_playing is not None and evaluation.chance_of_playing < 75:
        score -= 3

    return score


def _calculate_quality_based_score(
    evaluation: PlayerEvaluation,
    *,
    family: Literal["target", "differential"],
    weights: QualityWeights,
    next_gw_id: int,
    prior: PlayerPrior | None = None,
    ownership_config: dict[str, float] | None = None,
    differential: bool = False,
) -> int:
    """Shared scoring logic for target and differential.

    Thin wrapper: delegates to ``_calculate_quality_based_raw`` then
    normalises. The anchor is resolved once and the ceiling derived from it
    (the definition ``_ownership_ceiling_for`` states), so the blend and the
    normalisation cannot describe different scales and an early-season keeper
    does not pay for the attainability ratio twice. Waiver keeps its own flow
    — it adds position-need and team-stacking terms to the raw score and
    decides the GK scaling per player.
    """
    anchor = _ownership_anchor_for(family, evaluation.position, next_gw_id=next_gw_id)
    raw = _calculate_quality_based_raw(
        evaluation,
        weights=weights,
        next_gw_id=next_gw_id,
        anchor=anchor,
        prior=prior,
        ownership_config=ownership_config,
        differential=differential,
    )
    return normalise_score(raw, anchor + _OWNERSHIP_HEADROOM[family])


# ---------------------------------------------------------------------------
# Scoring formulas
# ---------------------------------------------------------------------------


def calculate_target_score(
    evaluation: PlayerEvaluation,
    *,
    next_gw_id: int,
    prior: PlayerPrior | None = None,
) -> int:
    """Calculate a target score (pure performance, no ownership bias).

    *prior* carries the early-season blend into the quality baseline before
    ``player_prior.CUTOFF_GW``; without it the score is pure observation and
    small-sample dominated going into GW2 (#206).
    """
    return _calculate_quality_based_score(
        evaluation,
        family="target",
        weights=TARGET_QUALITY_WEIGHTS,
        next_gw_id=next_gw_id,
        prior=prior,
    )


def calculate_differential_score(
    evaluation: PlayerEvaluation,
    *,
    semi_differential_threshold: float,
    next_gw_id: int,
    prior: PlayerPrior | None = None,
) -> int:
    """Calculate a differential score for a player.

    *prior* carries the early-season blend into the quality baseline, as for
    ``calculate_target_score``. The ownership bonus stays pure observation:
    how many managers own a player is measured, not estimated.
    """
    return _calculate_quality_based_score(
        evaluation,
        family="differential",
        weights=DIFFERENTIAL_QUALITY_WEIGHTS,
        next_gw_id=next_gw_id,
        prior=prior,
        ownership_config={
            "threshold": semi_differential_threshold,
            "divisor": 3,
        },
        differential=True,
    )


def calculate_waiver_score(
    evaluation: PlayerEvaluation,
    *,
    squad_by_position: dict[str, list],
    team_counts: dict[str, int] | None = None,
    next_gw_id: int,
    gk_signals_supplied: bool = False,
    prior: PlayerPrior | None = None,
) -> int:
    """Calculate a waiver priority score for a player.

    Delegates shared flow (quality baseline, regression, matchup,
    availability) to ``_calculate_quality_based_raw`` with a bespoke
    combined_mins_factor (availability * per_appearance) that is
    stricter than the standard mins_factor - draft waivers are a
    season commitment so absolute playing time matters.

    Position-need and team-stacking adjustments are waiver-specific
    and applied to the raw score before normalisation.

    *prior* carries the early-season blend into the quality baseline, as for
    ``calculate_target_score``. It reaches this family through a draft-keyed
    prior map (#209), and the blend anchor follows *gk_signals_supplied* for
    the same reason the ceiling does — see below.

    *gk_signals_supplied* says whether this evaluation carries the GK signal
    block, which decides whether the GK anchor is calendar-scaled. The draft
    bootstrap publishes neither saves nor expected goals conceded, so the
    caller reaches them by joining the draft element to its main-game
    ``Player`` on ``code`` — a join that can miss. Passing True for a keeper
    it missed would divide a signal-less numerator by a scaled denominator
    and inflate him ~30% at GW2 (#143 / PR #156 review), so the flag is
    per-player, not per-run. Ignored for outfielders.
    """
    # Combined minutes factor: per_appearance * availability (waiver-specific)
    per_appearance = calculate_mins_factor(
        evaluation.minutes, evaluation.appearances, next_gw_id,
    )
    if next_gw_id <= MINS_FACTOR_START_GW:
        combined_mins_factor = 1.0
    elif evaluation.appearances > 0:
        availability = min(evaluation.minutes / 450, 1.0)
        combined_mins_factor = availability * per_appearance
    else:
        combined_mins_factor = 0.0

    # The GK anchor is calendar-scaled only for a keeper whose signals this
    # evaluation actually carries; without them the full anchor stands, or a
    # scaled denominator over a signal-less numerator inflates him (#207).
    # Resolved once and shared by the blend and the normalisation below, so
    # the prior-implied baseline and the observed one it replaces always sit
    # on one scale.
    anchor = _ownership_anchor_for(
        "waiver",
        evaluation.position,
        next_gw_id=next_gw_id if gk_signals_supplied else None,
    )

    score = _calculate_quality_based_raw(
        evaluation,
        weights=WAIVER_QUALITY_WEIGHTS,
        next_gw_id=next_gw_id,
        anchor=anchor,
        prior=prior,
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

    return normalise_score(score, anchor + _OWNERSHIP_HEADROOM["waiver"])
