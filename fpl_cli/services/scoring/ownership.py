"""The ownership scoring family: target, differential, and waiver.

Multi-gameweek acquisition scores built on the shared quality baseline
plus the scalar 3-GW matchup bonus, ownership / position-need
adjustments, and the consistency bonus.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from fpl_cli.services.scoring.constants import (
    _MATCHUP_MAX,
    ATTACKING_POSITIONS,
    CONSISTENCY_CV_DIFF,
    CONSISTENCY_CV_TARGET,
    DIFFERENTIAL_QUALITY_WEIGHTS,
    MINS_FACTOR_START_GW,
    TARGET_QUALITY_WEIGHTS,
    WAIVER_QUALITY_WEIGHTS,
    QualityWeights,
    _consistency_phase,
    _ownership_ceiling_for,
)
from fpl_cli.services.scoring.display import normalise_score
from fpl_cli.services.scoring.value_quality import calculate_mins_factor, calculate_player_quality_score

if TYPE_CHECKING:
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
    ownership_config: dict[str, float] | None = None,
    mins_factor_override: float | None = None,
    differential: bool = False,
) -> float:
    """Raw ownership-family score before normalisation.

    Computes: quality baseline + ownership bonus + matchup bonus +
    consistency bonus + availability penalty. Returns un-normalised float
    so callers can add formula-specific adjustments before normalising.

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
    weights: QualityWeights,
    ceiling: float,
    next_gw_id: int,
    ownership_config: dict[str, float] | None = None,
    differential: bool = False,
) -> int:
    """Shared scoring logic for target, differential, and (via raw) waiver.

    Thin wrapper: delegates to ``_calculate_quality_based_raw`` then normalises.
    """
    raw = _calculate_quality_based_raw(
        evaluation,
        weights=weights,
        next_gw_id=next_gw_id,
        ownership_config=ownership_config,
        differential=differential,
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
    ceiling = _ownership_ceiling_for("target", evaluation.position, next_gw_id=next_gw_id)
    return _calculate_quality_based_score(
        evaluation,
        weights=TARGET_QUALITY_WEIGHTS,
        ceiling=ceiling,
        next_gw_id=next_gw_id,
    )


def calculate_differential_score(
    evaluation: PlayerEvaluation,
    *,
    semi_differential_threshold: float,
    next_gw_id: int,
) -> int:
    """Calculate a differential score for a player."""
    ceiling = _ownership_ceiling_for("differential", evaluation.position, next_gw_id=next_gw_id)
    return _calculate_quality_based_score(
        evaluation,
        weights=DIFFERENTIAL_QUALITY_WEIGHTS,
        ceiling=ceiling,
        next_gw_id=next_gw_id,
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
) -> int:
    """Calculate a waiver priority score for a player.

    Delegates shared flow (quality baseline, regression, matchup,
    availability) to ``_calculate_quality_based_raw`` with a bespoke
    combined_mins_factor (availability * per_appearance) that is
    stricter than the standard mins_factor - draft waivers are a
    season commitment so absolute playing time matters.

    Position-need and team-stacking adjustments are waiver-specific
    and applied to the raw score before normalisation.

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

    # The GK anchor is calendar-scaled only for a keeper whose signals this
    # evaluation actually carries; without them the full anchor stands, or a
    # scaled denominator over a signal-less numerator inflates him (#207).
    waiver_ceiling = _ownership_ceiling_for(
        "waiver",
        evaluation.position,
        next_gw_id=next_gw_id if gk_signals_supplied else None,
    )
    return normalise_score(score, waiver_ceiling)
