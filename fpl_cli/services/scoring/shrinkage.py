"""Early-season score shrinkage toward position means.

Confidence-weighted shrinkage from Bayesian player priors: low-confidence
early-season scores are pulled toward their position mean until the prior
cutoff gameweek.

Shrinkage is an empirical-Bayes device for *noisy estimates*. ``adjusted =
mean + conf * (score - mean)`` is the posterior mean under a normal-normal
model, and it assumes the distance between a score and its position mean is
mostly sampling noise whenever confidence is low. That assumption fails for
a player who is known not to be playing: their score is low because of an
observed fact, not because the season is young, and shrinkage hands most of
the position mean back to them. ``PlayerPrior.confidence`` cannot separate
the two cases — it is a function of the gameweek and last season's pts/90
only, and carries no availability signal at all — so an unavailable player
draws exactly the same shrinkage as a fit player with the same history.

Known-unavailable players are therefore held out of shrinkage entirely, both
as inputs to the position mean (the empirical prior should be estimated over
the exchangeable population it describes) and as targets for adjustment.
"""

from __future__ import annotations

from collections.abc import Collection, Iterable, Mapping
from typing import TYPE_CHECKING, Any

from fpl_cli.services.scoring.constants import MINS_FACTOR_START_GW

if TYPE_CHECKING:
    from fpl_cli.services.player_prior import PlayerPrior
    from fpl_cli.services.scoring.constants import Position


_MISSING = object()


def is_known_unavailable(
    *,
    chance_of_playing: int | None,
    minutes: int,
    next_gw_id: int,
) -> bool:
    """Whether a low score states an observed fact rather than a noisy estimate.

    Two signals, both statements about the player rather than about the sample
    size behind their score:

    - ``chance_of_playing == 0`` — FPL's own hard flag that the player will not
      feature. Unlike the 25/50/75 buckets this one is not a doubt.
    - No minutes at all once the minutes factor is live (after
      ``MINS_FACTOR_START_GW``). ``calculate_mins_factor`` returns 0.0 for a
      player with no appearances, which zeroes the quality baseline by
      construction, so the score is not an estimate of anything. At or before
      that gameweek the factor is disabled and nobody has played much, so zero
      minutes carries no signal.

    A missing ``chance_of_playing`` is *not* unavailability: with no signal the
    score keeps the ordinary shrinkage treatment.
    """
    if chance_of_playing == 0:
        return True
    return next_gw_id > MINS_FACTOR_START_GW and minutes <= 0


def _player_field(source: Any, *names: str) -> Any:
    """First present value among *names*, from a mapping or an object."""
    for name in names:
        if isinstance(source, Mapping):
            if name in source:
                return source[name]
        else:
            value = getattr(source, name, _MISSING)
            if value is not _MISSING:
                return value
    return None


def unavailable_player_ids(players: Iterable[Any], next_gw_id: int) -> set[int]:
    """Ids to hold out of shrinkage, from FPL models or the dicts agents pass around.

    Reads ``chance_of_playing`` (enriched dicts) or
    ``chance_of_playing_next_round`` (the ``Player`` model) plus ``minutes``,
    so a call site can build the hold-out set from whichever player
    representation it already has in scope rather than reshaping it first.
    """
    result: set[int] = set()
    for player in players:
        pid = _player_field(player, "id")
        if pid is None:
            continue
        if is_known_unavailable(
            chance_of_playing=_player_field(
                player, "chance_of_playing", "chance_of_playing_next_round",
            ),
            minutes=_player_field(player, "minutes") or 0,
            next_gw_id=next_gw_id,
        ):
            result.add(int(pid))
    return result


def shrink_scores(
    scores: list[tuple[int, float, Position]],
    prior_map: dict[int, PlayerPrior] | None,
    current_gw: int,
    cutoff_gw: int,
    unavailable_ids: Collection[int] | None = None,
) -> list[tuple[int, float, Position]]:
    """Apply confidence-weighted shrinkage toward position means.

    Args:
        scores: List of (player_id, score, position) tuples.
        prior_map: player_id -> PlayerPrior (or None to skip shrinkage).
        current_gw: Current gameweek number.
        cutoff_gw: GW at/after which shrinkage is disabled.
        unavailable_ids: Players held out of shrinkage — excluded from the
            position means and returned with their score untouched. See the
            module docstring for why known-unavailable players do not belong
            in an empirical-Bayes adjustment.

    Returns:
        List of (player_id, adjusted_score, position) in the same order.
    """
    if not scores or prior_map is None or current_gw >= cutoff_gw:
        return scores

    held_out = frozenset(unavailable_ids) if unavailable_ids else frozenset()

    # Collect confidence per player (default 1.0 = no shrinkage)
    confidences: dict[int, float] = {}
    for pid, _, _ in scores:
        prior = prior_map.get(pid)
        confidences[pid] = prior.confidence if prior is not None else 1.0

    # Pass 1: confidence-weighted position means over the shrinkable pool only
    pos_weighted_sum: dict[str, float] = {}
    pos_weight_total: dict[str, float] = {}
    for pid, score, position in scores:
        if pid in held_out:
            continue
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
        if pid in held_out:
            result.append((pid, score, position))
            continue
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
    unavailable_ids: Collection[int] | None = None,
) -> None:
    """Apply early-season shrinkage to scored dicts in place.

    Extracts (id, score, position) from each dict, runs shrink_scores,
    and writes adjusted scores back. Agents call this instead of
    manually wiring the extract-shrink-writeback loop.

    *unavailable_ids* names the players to hold out. Callers should pass it
    from live player data — the FPL models or enriched dicts they scored from
    — because that is where availability is current. Omitting it falls back to
    reading availability off *scored_items* themselves, which works only for
    the call sites whose scored dicts carry ``chance_of_playing`` and
    ``minutes``; it is a safety net for new callers, not the intended path.
    """
    from fpl_cli.services.player_prior import CUTOFF_GW

    if unavailable_ids is None:
        unavailable_ids = unavailable_player_ids(scored_items, current_gw)

    tuples = [
        (item["id"], float(item[score_field]), item["position"])
        for item in scored_items
    ]
    shrunk = shrink_scores(tuples, prior_map, current_gw, CUTOFF_GW, unavailable_ids)
    for item, (_, adj_score, _) in zip(scored_items, shrunk):
        item[score_field] = round(adj_score)
