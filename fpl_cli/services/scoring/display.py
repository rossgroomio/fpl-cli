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

    The waiver list orders on a normalised score, as do the target and
    differential lists (StatsAgent sorts both on the normalised value), so
    those are the places the clamp can tie players together. Below GW10 the
    ownership family's prior blend keeps most of them apart before they get
    here: it lifts the quality baseline toward last season's pedigree while
    the score is still a raw float, so a merely weak player rarely reaches the
    clamp at all (#206 — the blend runs inside the score, unlike the
    position-mean shrinkage it replaced, which adjusted this value afterwards).

    Two clamped players who are both known not to be playing do stay tied: the
    blend holds them out precisely so their 0 survives, and from GW10 it does
    not run at all. That tie is intended. Their relative order then falls
    to the sort being stable, which is a wash — nothing distinguishes two
    players who are equally unavailable, and both sit below anyone who is not.
    """
    return max(0, min(round(raw / ceiling * 100), 100))


def pick_display_ceiling(
    position: Position, horizon: int, *, next_gw_id: int | None = None
) -> float:
    """Position + horizon aware ceiling for `fpl allocate` display normalisation.

    Two-column model downstream:

    - horizon=1 → ``single_gw_score``. Uses ``STARTING_XI_CEILING`` as a
      cross-position anchor (intentional). DEFs/GKs land lower than MIDs/FWDs
      for the same real-world quality; use ``raw_quality`` for position-agnostic
      ranking in the single-GW context.
    - horizon >= 2 → ``quality_score``. Routes to VALUE-family ceilings via
      ``_value_weights_and_ceiling``, matching ``fpl player`` / ``fpl stats
      --value`` / ``fpl transfer-eval`` for cross-command consistency. Pass
      *next_gw_id* so a pre-GW6 GK is normalised against the calendar-scaled
      attainable ceiling, same as those commands — being calendar-keyed the
      ceiling is identical for every keeper in the table, so the printed
      score can never invert the ``raw_quality`` order the solver used.
    """
    if horizon <= 1:
        return STARTING_XI_CEILING
    _, ceiling = _value_weights_and_ceiling(position, next_gw_id=next_gw_id)
    return ceiling
