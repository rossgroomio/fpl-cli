"""Normalisation of raw scores to the 0-100 display scale."""

from __future__ import annotations

from fpl_cli.services.scoring.constants import (
    STARTING_XI_CEILING,
    Position,
    _value_weights_and_ceiling,
)


def normalise_score(raw: float, ceiling: float) -> int:
    """Normalise a raw score to 0-100 against a ceiling."""
    return min(round(raw / ceiling * 100), 100)


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
