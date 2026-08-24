"""Early-season score shrinkage toward position means.

Confidence-weighted shrinkage from Bayesian player priors: low-confidence
early-season scores are pulled toward their position mean until the prior
cutoff gameweek.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from fpl_cli.services.player_prior import PlayerPrior
    from fpl_cli.services.scoring.constants import Position


def shrink_scores(
    scores: list[tuple[int, float, Position]],
    prior_map: dict[int, PlayerPrior] | None,
    current_gw: int,
    cutoff_gw: int,
) -> list[tuple[int, float, Position]]:
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
    result: list[tuple[int, float, Position]] = []
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
