"""Scoring weights, ceilings, and configuration constants.

Every tunable in the scoring engine lives here: the StatWeight /
QualityWeights types, the per-family weight configurations, position
multipliers, normalisation ceilings (hand-derived MID/FWD anchors plus
programmatically derived GK/DEF variants), consistency bonus magnitudes
and phase-in, and the selectors that map a scoring family + position to
its weights and ceiling.
"""

from __future__ import annotations

import dataclasses
import functools
from math import inf
from typing import Literal, cast

# ---------------------------------------------------------------------------
# Weight types
# ---------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class StatWeight:
    """A (multiplier, cap) pair for a single scoring component."""

    multiplier: float
    cap: float = inf


@dataclasses.dataclass(frozen=True)
class QualityWeights:
    """Weight configuration for the shared player quality baseline.

    Each field is a StatWeight(multiplier, cap) controlling how that
    stat contributes to the quality score. Agents define their own
    module-level instances with different weights.
    """

    npxg: StatWeight
    xg_chain: StatWeight
    xgi_fallback: StatWeight
    form: StatWeight
    ppg: StatWeight
    dc_per_90: StatWeight = dataclasses.field(default_factory=lambda: StatWeight(0, 0))
    penalty_xg: StatWeight = dataclasses.field(default_factory=lambda: StatWeight(0, 0))
    gk_saves_per_90: StatWeight = dataclasses.field(default_factory=lambda: StatWeight(0, 0))
    gk_xgc_quality: StatWeight = dataclasses.field(default_factory=lambda: StatWeight(0, 0))
    gk_cs_rate: StatWeight = dataclasses.field(default_factory=lambda: StatWeight(0, 0))

    @functools.lru_cache(maxsize=None)
    def without_xgi(self) -> QualityWeights:
        """Return a copy with xGI-family weights zeroed and DC/90 activated (for DEF)."""
        zero = StatWeight(0, 0)
        return dataclasses.replace(
            self,
            npxg=zero,
            xg_chain=zero,
            xgi_fallback=zero,
            dc_per_90=StatWeight(0.5, 2),
            penalty_xg=zero,
            gk_saves_per_90=zero,
            gk_xgc_quality=zero,
            gk_cs_rate=zero,
        )

    @functools.lru_cache(maxsize=None)
    def for_gk(self) -> QualityWeights:
        """Return a copy with GK-specific signals activated (for GK scoring path).

        Zeroes: xGI family, dc_per_90, penalty_xg.
        Activates: gk_saves_per_90, gk_xgc_quality, gk_cs_rate.
        Preserves: form, ppg from the parent instance.
        """
        zero = StatWeight(0, 0)
        return dataclasses.replace(
            self,
            npxg=zero,
            xg_chain=zero,
            xgi_fallback=zero,
            dc_per_90=zero,
            penalty_xg=zero,
            gk_saves_per_90=StatWeight(1.5, 6),
            gk_xgc_quality=StatWeight(3.0, 3.5),
            # Halved 2026-04-10: was (8.0, 4.0) - a Phase 1 placeholder that
            # put 50%-CS GKs within 0.8pt of the cap, crowding out xGI signals.
            # At mult=4 the theoretical cap is unchanged (min(1.0*4, 4)=4) so
            # ceiling constants stay valid; only sub-cap contributions drop.
            gk_cs_rate=StatWeight(4.0, 4.0),
        )


# ---------------------------------------------------------------------------
# Weight configurations (moved from agent modules)
# ---------------------------------------------------------------------------

TARGET_QUALITY_WEIGHTS = QualityWeights(
    npxg=StatWeight(10, 8),
    xg_chain=StatWeight(2, 3),
    xgi_fallback=StatWeight(10, 10),
    form=StatWeight(1.0, 5),
    ppg=StatWeight(0.5, 4),
    dc_per_90=StatWeight(0, 0),
    penalty_xg=StatWeight(8, 3),
)

DIFFERENTIAL_QUALITY_WEIGHTS = QualityWeights(
    npxg=StatWeight(10, 8),
    xg_chain=StatWeight(2, 3),
    xgi_fallback=StatWeight(10, 10),
    form=StatWeight(1.3, 7),
    ppg=StatWeight(0.5, 4),
    dc_per_90=StatWeight(0, 0),
    penalty_xg=StatWeight(8, 3),
)

WAIVER_QUALITY_WEIGHTS = QualityWeights(
    npxg=StatWeight(5),
    xg_chain=StatWeight(2, 3),
    xgi_fallback=StatWeight(5),
    form=StatWeight(1.3, 7),
    ppg=StatWeight(0.6, 4.8),
    dc_per_90=StatWeight(0, 0),
    penalty_xg=StatWeight(8, 3),
)

GW_SELECTION_WEIGHTS = QualityWeights(
    npxg=StatWeight(5, 10),
    xg_chain=StatWeight(0, 0),
    xgi_fallback=StatWeight(5, 10),
    form=StatWeight(1.5, 10),
    ppg=StatWeight(0, 0),
    dc_per_90=StatWeight(0, 0),
    penalty_xg=StatWeight(8, 3),
)

VALUE_QUALITY_WEIGHTS = QualityWeights(
    npxg=StatWeight(10, 8),
    xg_chain=StatWeight(1, 2),
    xgi_fallback=StatWeight(10, 10),
    form=StatWeight(1.3, 7),
    ppg=StatWeight(0.8, 5),
    dc_per_90=StatWeight(0, 0),
    penalty_xg=StatWeight(8, 3),
)

# Position multiplier: adjusts ceiling for per-game scoring potential (captain + bench)
Position = Literal["GK", "DEF", "MID", "FWD"]

POSITION_SCORE_MULTIPLIER: dict[str, float] = {
    "FWD": 1.0,
    "MID": 1.0,
    "DEF": 0.85,
    "GK": 0.7,
}


def _as_position(value: str) -> Position:
    """Narrow an enum-derived position string to the Position literal.

    Raises ValueError on unknown values (e.g. the "???" fallback from
    Player.position_name when the FPL enum is out of sync). Callers
    passing a known PlayerPosition-derived string should never trip this.
    """
    if value not in POSITION_SCORE_MULTIPLIER:
        raise ValueError(f"Unknown position: {value!r}")
    return cast(Position, value)


def _position_from_element_type(element_type: int) -> Position:
    """Resolve FPL element_type to Position literal, raising on unknown values.

    Single choke point shared by build_player_evaluation, squad_allocator, and
    player_prior so every PlayerPosition.value -> Position conversion raises
    ValueError (not KeyError) uniformly.
    """
    from fpl_cli.models.player import POSITION_MAP

    raw = POSITION_MAP.get(element_type)
    if raw is None:
        raise ValueError(f"Unknown FPL element_type {element_type!r} has no position mapping")
    return _as_position(raw)


ATTACKING_POSITIONS: frozenset[str] = frozenset({"MID", "FWD"})


# ---------------------------------------------------------------------------
# Normalisation ceilings (SGW theoretical max, MID/FWD path)
# ---------------------------------------------------------------------------

# Captain: (matchup 8*2.0 + form min(7.5*1.5,10)*1.38 + xGI ~3.5 + pen ~1.2)
#   * pos 1.0 * mins 1.0 + home 1.0 + cv_lineup 0.375
CAPTAIN_CEILING_SGW = 34.2
# Target: npxg 8 + xg_chain 3 + form 5*1.38 + ppg 4 + penalty 3 + matchup 6 + cv_target 0.75
TARGET_CEILING = 31.7
# Differential: npxg 8 + xg_chain 3 + form 7*1.38 + ppg 4 + penalty 3 + ownership 5 + matchup 6 + cv_diff 0.375
DIFFERENTIAL_CEILING = 39.1
# Waiver: quality ~25.7 (form 7*1.38) + matchup 6 + position 5 + cv_target 0.75 = 37.5
WAIVER_CEILING = 37.5
# Bench: core ~32.8 + cv_lineup 0.375 + coverage 2 + set-piece 0.5 + floor 0.75 + inv 0.375 = 36.8
BENCH_CEILING = 36.8
# Starting XI: core ~32.8 + cv_lineup 0.375, no bench bonuses
STARTING_XI_CEILING = 33.2
# Value: npxg 8 + xg_chain 2 + form 7*1.38 + ppg 5 + penalty 3 = 27.7 theoretical
# Practical ceiling ~24.3 (elite MID scores ~20 raw). Validated: Salah-tier -> 87-92/100
VALUE_CEILING = 24.3

# Non-quality bonus caps used when deriving ceiling constants. Keep in sync
# with ownership.py: _matchup_bonus, the ownership bonus in
# _calculate_quality_based_raw, and the position-need bonus in
# calculate_waiver_score.
_MATCHUP_MAX = 6.0           # matchup_avg_3gw max 8.0 * 0.75 * mins_factor 1.0
_OWNERSHIP_MAX = 5.0         # (semi_differential_threshold 15 - 0) / divisor 3
_POSITION_NEED_MAX = 5.0     # calculate_waiver_score empty-slot bonus
# Form multipliers applied inside value_quality.calculate_player_quality_score.
# The ATK path gets form_trajectory_max(1.2) * xgi_sustainability_max(1.15) = 1.38;
# GK/DEF paths get form_trajectory_max only because signals.compute_xgi_sustainability
# returns 1.0 for non-ATK positions. The ATK form ceiling is hand-rolled into
# the TARGET/DIFFERENTIAL/VALUE constants above (see the `form N*1.38` terms);
# _NON_ATK_FORM_MAX is used by _gk_quality_cap and _def_quality_cap below.
_NON_ATK_FORM_MAX = 1.2

# Consistency bonus headroom — added inside ownership.py's
# _calculate_quality_based_raw. cv_xgi_percentile in [0,1] × magnitude × 0.5
# = max bonus. Value family skips that helper entirely and takes no
# consistency term.
_CONSISTENCY_MAX_TARGET = 0.75   # (1.0 - 0.5) * CONSISTENCY_CV_TARGET (1.5)
_CONSISTENCY_MAX_DIFF = 0.375    # (1.0 - 0.5) * CONSISTENCY_CV_DIFF (0.75)
_CONSISTENCY_MAX_WAIVER = 0.75   # waiver uses CONSISTENCY_CV_TARGET too


def _gk_quality_cap(weights: QualityWeights) -> float:
    """Theoretical max of calculate_player_quality_score on the GK path.

    Derived from weight caps, pre-attenuation. Matches the signal set
    evaluated inside calculate_player_quality_score when weights.for_gk()
    is used: saves, xgc, cs, form (trajectory only — xgi_sustainability
    is always 1.0 for GK, see compute_xgi_sustainability), ppg.
    """
    gk = weights.for_gk()
    return (
        gk.gk_saves_per_90.cap
        + gk.gk_xgc_quality.cap
        + gk.gk_cs_rate.cap
        + gk.form.cap * _NON_ATK_FORM_MAX
        + gk.ppg.cap
    )


def _def_quality_cap(weights: QualityWeights) -> float:
    """Theoretical max of calculate_player_quality_score on the DEF path.

    Matches the signal set under weights.without_xgi(): form (trajectory
    only — xgi_sustainability is always 1.0 for DEF), ppg, and
    dc_per_90. xGI family, GK components and penalty_xg are zeroed by
    without_xgi().
    """
    defw = weights.without_xgi()
    return (
        defw.form.cap * _NON_ATK_FORM_MAX
        + defw.ppg.cap
        + defw.dc_per_90.cap
    )


# Position-specific ceilings — derived at import so a weight change
# auto-propagates. Quality components attenuated by
# POSITION_SCORE_MULTIPLIER[position]; matchup, ownership and
# position-need bonuses are added un-attenuated. Drift is guarded
# empirically by TestCeilingValidationBands (elite-player bounds).
_GK_MULT = POSITION_SCORE_MULTIPLIER["GK"]
_DEF_MULT = POSITION_SCORE_MULTIPLIER["DEF"]
# Ownership-family ceilings include _CONSISTENCY_MAX_* headroom so a top-pool
# player with high cv_xgi_percentile does not overflow the ceiling and get
# silently clamped to 100 (losing the consistency signal's discrimination).
# MID/FWD TARGET_CEILING / DIFFERENTIAL_CEILING / WAIVER_CEILING already bake
# this in (see the cv_* terms in their derivation comments above); GK and DEF
# must add it explicitly because their caps are computed programmatically.
GK_TARGET_CEILING = (
    _gk_quality_cap(TARGET_QUALITY_WEIGHTS) * _GK_MULT + _MATCHUP_MAX + _CONSISTENCY_MAX_TARGET
)
GK_DIFFERENTIAL_CEILING = (
    _gk_quality_cap(DIFFERENTIAL_QUALITY_WEIGHTS) * _GK_MULT
    + _OWNERSHIP_MAX + _MATCHUP_MAX + _CONSISTENCY_MAX_DIFF
)
GK_WAIVER_CEILING = (
    _gk_quality_cap(WAIVER_QUALITY_WEIGHTS) * _GK_MULT
    + _MATCHUP_MAX + _POSITION_NEED_MAX + _CONSISTENCY_MAX_WAIVER
)
# Value family has no matchup or consistency — compute_quality_value skips
# _calculate_quality_based_raw entirely.
GK_VALUE_CEILING = _gk_quality_cap(VALUE_QUALITY_WEIGHTS) * _GK_MULT

# DEF ceilings: derived from without_xgi() caps, not MID-anchored ceilings.
# Replaces the former _position_ceiling("DEF", ...) scaling which produced a
# mathematical no-op on the VALUE family (both numerator and denominator
# scaled by 0.85) and a MID-anchored ceiling on target/diff/waiver that
# compressed real DEF pools into a narrow upper band.
DEF_TARGET_CEILING = (
    _def_quality_cap(TARGET_QUALITY_WEIGHTS) * _DEF_MULT + _MATCHUP_MAX + _CONSISTENCY_MAX_TARGET
)
DEF_DIFFERENTIAL_CEILING = (
    _def_quality_cap(DIFFERENTIAL_QUALITY_WEIGHTS) * _DEF_MULT
    + _OWNERSHIP_MAX + _MATCHUP_MAX + _CONSISTENCY_MAX_DIFF
)
DEF_WAIVER_CEILING = (
    _def_quality_cap(WAIVER_QUALITY_WEIGHTS) * _DEF_MULT
    + _MATCHUP_MAX + _POSITION_NEED_MAX + _CONSISTENCY_MAX_WAIVER
)
DEF_VALUE_CEILING = _def_quality_cap(VALUE_QUALITY_WEIGHTS) * _DEF_MULT

# ---------------------------------------------------------------------------
# Consistency scoring magnitudes (Phase 2)
# ---------------------------------------------------------------------------
# All enter as additive bonuses: (signal - 0.5) * magnitude.
# Phase-in: linear ramp from GW6 (0%) to GW10 (100%).

CONSISTENCY_CV_TARGET = 1.5       # target/waiver: cv_xgi_percentile
CONSISTENCY_CV_LINEUP = 0.75      # captain/bench/lineup tiebreaker
CONSISTENCY_CV_DIFF = 0.75        # differential: inverted (0.5 - cv)
CONSISTENCY_FLOOR_BENCH = 1.5     # bench: floor_percentile
CONSISTENCY_INV_BENCH = 0.75      # bench: involvement_rate

# Phase-in window: no effect at GW5 or earlier, full effect at GW10+
CONSISTENCY_PHASE_IN_START = 5
CONSISTENCY_PHASE_IN_END = 10


def _consistency_phase(gw: int) -> float:
    """Linear phase-in factor for consistency bonuses (0.0 at GW5, 1.0 at GW10+)."""
    window = CONSISTENCY_PHASE_IN_END - CONSISTENCY_PHASE_IN_START
    return min(1.0, max(0.0, (gw - CONSISTENCY_PHASE_IN_START) / window))


# Valid formations: (DEF, MID, FWD). GK always 1.
# Ordered from most attacking to most defensive for deterministic tiebreaking.
VALID_FORMATIONS: list[tuple[int, int, int]] = [
    (3, 4, 3),
    (3, 5, 2),
    (4, 3, 3),
    (4, 4, 2),
    (4, 5, 1),
    (5, 3, 2),
    (5, 4, 1),
]

# Max PL gameweeks in a season (caps derived appearances)
MAX_GAMEWEEKS = 38

# FDR thresholds for fixture difficulty classification
FDR_EASY = 2.5
FDR_MEDIUM = 3.5

# Default FDR mode used by all scoring agents (difference = team+opponent axis)
FDR_MODE = "difference"

# ---------------------------------------------------------------------------
# Ceiling / weight selectors
# ---------------------------------------------------------------------------

_OWNERSHIP_CEILINGS: dict[tuple[str, str], float] = {
    ("target", "GK"): GK_TARGET_CEILING,
    ("target", "DEF"): DEF_TARGET_CEILING,
    ("differential", "GK"): GK_DIFFERENTIAL_CEILING,
    ("differential", "DEF"): DEF_DIFFERENTIAL_CEILING,
    ("waiver", "GK"): GK_WAIVER_CEILING,
    ("waiver", "DEF"): DEF_WAIVER_CEILING,
}
_OWNERSHIP_BASE_CEILINGS: dict[str, float] = {
    "target": TARGET_CEILING,
    "differential": DIFFERENTIAL_CEILING,
    "waiver": WAIVER_CEILING,
}


def _ownership_ceiling_for(family: Literal["target", "differential", "waiver"], position: Position) -> float:
    return _OWNERSHIP_CEILINGS.get((family, position), _OWNERSHIP_BASE_CEILINGS[family])


def _value_weights_and_ceiling(position: Position) -> tuple[QualityWeights, float]:
    """Select VALUE_QUALITY_WEIGHTS variant and ceiling for a position."""
    if position == "GK":
        return VALUE_QUALITY_WEIGHTS.for_gk(), GK_VALUE_CEILING
    if position == "DEF":
        return VALUE_QUALITY_WEIGHTS.without_xgi(), DEF_VALUE_CEILING
    return VALUE_QUALITY_WEIGHTS, VALUE_CEILING
