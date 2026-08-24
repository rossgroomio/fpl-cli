"""Normalisation of raw scores to the 0-100 display scale."""

from __future__ import annotations

from fpl_cli.services.scoring.constants import (
    STARTING_XI_CEILING,
    Position,
    _value_weights_and_ceiling,
)


def normalise_score(raw: float, ceiling: float) -> int:
    """Normalise a raw score to the 0-100 display scale against a ceiling.

    Both ends are clamped. Raw scores can go negative — the availability
    penalty and the team-stacking penalty both subtract — and every consumer
    of this value renders it in a column documented as 0-100, so an unclamped
    negative would be printed verbatim (an injured player at a club the squad
    is already three-deep in scores -23 on the waiver family).

    Selection is unaffected: the scoring engine orders on the raw scores it
    keeps alongside this one (``lineup_score_raw``, ``priority_score_raw``),
    never on the display value.

    The waiver list is the one consumer that does order on a normalised score,
    so it is the only place the clamp can tie players together. Below GW10
    ``apply_shrinkage`` runs on ``waiver_score`` between scoring and sorting
    and separates most of them again — a clamped 0 from a merely weak player is
    pulled toward the position mean like any other low-confidence score.

    Two clamped players who are both known not to be playing do stay tied:
    shrinkage holds them out precisely so their 0 survives, and from GW10 it
    does not run at all. That tie is intended. Their relative order then falls
    to the sort being stable, which is a wash — nothing distinguishes two
    players who are equally unavailable, and both sit below anyone who is not.
    """
    return max(0, min(round(raw / ceiling * 100), 100))


def pick_display_ceiling(position: Position, horizon: int) -> float:
    """Position + horizon aware ceiling for `fpl allocate` display normalisation.

    Two-column model downstream:

    - horizon=1 → ``single_gw_score``. Uses ``STARTING_XI_CEILING`` as a
      cross-position anchor (intentional). DEFs/GKs land lower than MIDs/FWDs
      for the same real-world quality; use ``raw_quality`` for position-agnostic
      ranking in the single-GW context.
    - horizon >= 2 → ``quality_score``. Routes to VALUE-family ceilings via
      ``_value_weights_and_ceiling``, matching ``fpl player`` / ``fpl stats
      --value`` / ``fpl transfer-eval`` for cross-command consistency.
    """
    if horizon <= 1:
        return STARTING_XI_CEILING
    _, ceiling = _value_weights_and_ceiling(position)
    return ceiling
