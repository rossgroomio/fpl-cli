"""Scoring weights, ceilings, and configuration constants.

Every tunable in the scoring engine lives here: the StatWeight /
QualityWeights types, the per-family weight configurations, position
multipliers, the empirically calibrated quality ceilings (written by
scripts/calibrate_quality_ceilings.py, guarded by the calibration
fingerprint), consistency bonus magnitudes and phase-in, and the
selectors that map a scoring family + position to its weights and
ceiling.
"""

from __future__ import annotations

import dataclasses
import functools
import hashlib
from collections.abc import Collection
from math import inf, isfinite
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
# Calibration inputs shared with the signal implementations
# ---------------------------------------------------------------------------
# Named here rather than as literals at their point of use because the
# calibrated quality ceilings below are functions of them:
# scoring_weights_fingerprint() folds every one into the fingerprint recorded
# at calibration time, and the drift-guard test fails when any of them moves
# without re-running scripts/calibrate_quality_ceilings.py --write.

# compute_form_trajectory clamp (signals.py)
FORM_TRAJECTORY_BOUNDS: tuple[float, float] = (0.8, 1.2)
# compute_form_trajectory slope interpolation breakpoints (signals.py):
# slope <= min saturates at the lower bound, slope >= max at the upper.
# In the fingerprint because the interpolation shape moves the whole pool's
# trajectory distribution even when the clamp bounds stay put.
FORM_TRAJECTORY_SLOPE_RANGE: tuple[float, float] = (-1.5, 2.0)
# compute_xgi_sustainability clamp (signals.py); ATK positions only
XGI_SUSTAINABILITY_BOUNDS: tuple[float, float] = (0.85, 1.15)
# compute_xgi_sustainability divergence scale (signals.py): a per-match
# GI-xGI divergence of ±SCALE maps to the clamp bounds
XGI_DIVERGENCE_SCALE = 0.3
# Rolling-window shape shared by both signals above (signals.py): the most
# recent SIZE qualifying gameweeks inside a LOOKBACK-gameweek window
SIGNAL_WINDOW_LOOKBACK_GWS = 12
SIGNAL_WINDOW_SIZE = 7
# gk_xgc_quality = max(0, ANCHOR - xGC_per_90) * ramp (evaluation.py)
GK_XGC_QUALITY_ANCHOR = 2.0
# GK signal sample-size ramp: min(minutes / RAMP, 1.0) (evaluation.py)
GK_SAMPLE_RAMP_MINUTES = 450
# calculate_mins_factor full-appearance denominator (value_quality.py)
MINS_FACTOR_FULL_APPEARANCE = 80


def scoring_weights_fingerprint() -> str:
    """Digest of every scoring input the calibrated ceilings depend on.

    Recorded as CALIBRATION_FINGERPRINT when scripts/calibrate_quality_ceilings.py
    writes the ceilings; recomputed by the drift-guard test. A mismatch means a
    weight, position multiplier, or signal bound changed after calibration, so
    the ceilings describe a raw-score distribution that no longer exists —
    re-run the script rather than hand-adjusting (see the fpl-cli-docs solution
    note "ceiling arithmetic compounds across weight changes").
    """
    parts: list[str] = []
    for name, weights in (
        ("target", TARGET_QUALITY_WEIGHTS),
        ("differential", DIFFERENTIAL_QUALITY_WEIGHTS),
        ("waiver", WAIVER_QUALITY_WEIGHTS),
        ("value", VALUE_QUALITY_WEIGHTS),
    ):
        for variant_name, variant in (
            ("base", weights),
            ("def", weights.without_xgi()),
            ("gk", weights.for_gk()),
        ):
            parts.append(f"{name}.{variant_name}={variant!r}")
    parts.append(f"position_multiplier={sorted(POSITION_SCORE_MULTIPLIER.items())!r}")
    parts.append(f"form_trajectory={FORM_TRAJECTORY_BOUNDS!r}")
    parts.append(f"form_trajectory_slope={FORM_TRAJECTORY_SLOPE_RANGE!r}")
    parts.append(f"xgi_sustainability={XGI_SUSTAINABILITY_BOUNDS!r}")
    parts.append(f"xgi_divergence_scale={XGI_DIVERGENCE_SCALE!r}")
    parts.append(f"signal_window={(SIGNAL_WINDOW_LOOKBACK_GWS, SIGNAL_WINDOW_SIZE)!r}")
    parts.append(f"gk_xgc_anchor={GK_XGC_QUALITY_ANCHOR!r}")
    parts.append(f"gk_ramp={GK_SAMPLE_RAMP_MINUTES!r}")
    parts.append(f"mins_full_appearance={MINS_FACTOR_FULL_APPEARANCE!r}")
    parts.append(f"mins_factor_start_gw={MINS_FACTOR_START_GW!r}")
    return hashlib.sha256("|".join(parts).encode()).hexdigest()[:16]


# ---------------------------------------------------------------------------
# Normalisation ceilings (SGW theoretical max, MID/FWD path)
# ---------------------------------------------------------------------------

# Captain: (matchup 8*2.0 + form min(7.5*1.5,10)*1.38 + xGI ~3.5 + pen ~1.2)
#   * pos 1.0 * mins 1.0 + home 1.0 + cv_lineup 0.375
CAPTAIN_CEILING_SGW = 34.2
# Bench: core ~32.8 + cv_lineup 0.375 + coverage 2 + set-piece 0.5 + floor 0.75 + inv 0.375 = 36.8
BENCH_CEILING = 36.8
# Starting XI: core ~32.8 + cv_lineup 0.375, no bench bonuses
STARTING_XI_CEILING = 33.2

# Non-quality bonus caps used when deriving ceiling constants. Keep in sync
# with ownership.py: _matchup_bonus, the ownership bonus in
# _calculate_quality_based_raw, and the position-need bonus in
# calculate_waiver_score.
# The matchup-bonus budget the ownership ceilings reserve. Not a derived
# maximum: DGW windows push matchup_avg_3gw past the single-fixture scale
# (fixtures are summed within a gameweek), so ownership._matchup_bonus
# clamps the bonus to this value — the budget is enforced, not assumed.
_MATCHUP_MAX = 6.0
_OWNERSHIP_MAX = 5.0         # (semi_differential_threshold 15 - 0) / divisor 3
_POSITION_NEED_MAX = 5.0     # calculate_waiver_score empty-slot bonus
# Form multipliers applied inside value_quality.calculate_player_quality_score.
# The ATK path gets form_trajectory_max * xgi_sustainability_max (1.2 * 1.15
# = 1.38); GK/DEF paths get form_trajectory_max only because
# signals.compute_xgi_sustainability returns 1.0 for non-ATK positions.
# Used by _theoretical_quality_cap below.
_NON_ATK_FORM_MAX = FORM_TRAJECTORY_BOUNDS[1]

# Consistency bonus headroom — added inside ownership.py's
# _calculate_quality_based_raw. cv_xgi_percentile in [0,1] × magnitude × 0.5
# = max bonus. Value family skips that helper entirely and takes no
# consistency term.
_CONSISTENCY_MAX_TARGET = 0.75   # (1.0 - 0.5) * CONSISTENCY_CV_TARGET (1.5)
_CONSISTENCY_MAX_DIFF = 0.375    # (1.0 - 0.5) * CONSISTENCY_CV_DIFF (0.75)
_CONSISTENCY_MAX_WAIVER = 0.75   # waiver uses CONSISTENCY_CV_TARGET too


def _position_weights(weights: QualityWeights, position: Position) -> QualityWeights:
    """The weight variant a position's quality path actually scores against."""
    if position == "GK":
        return weights.for_gk()
    if position == "DEF":
        return weights.without_xgi()
    return weights


def _quality_term_caps(weights: QualityWeights) -> dict[str, float]:
    """Cap per scoring term for one weight variant, keyed by term name.

    The one place the shape of a variant's headroom is written down, so
    ``_theoretical_quality_cap`` and ``ceiling_attainability`` cannot drift
    apart or from ``QualityWeights`` itself (a test asserts every StatWeight
    field is covered here).

    ``npxg``/``xg_chain`` and ``xgi_fallback`` are two routes to the same
    attacking signal — ``calculate_player_quality_score`` takes one or the
    other, never both — so they collapse into a single ``attacking`` term
    worth whichever route caps higher. Summing all three would invent
    headroom no player can reach.

    A cap may be ``inf`` where a weight is uncapped (waiver npxg); it is each
    caller's business whether that is meaningful for what it computes.
    """
    return {
        "attacking": max(weights.npxg.cap + weights.xg_chain.cap, weights.xgi_fallback.cap),
        "penalty_xg": weights.penalty_xg.cap,
        "form": weights.form.cap,
        "ppg": weights.ppg.cap,
        "dc_per_90": weights.dc_per_90.cap,
        "gk_saves_per_90": weights.gk_saves_per_90.cap,
        "gk_xgc_quality": weights.gk_xgc_quality.cap,
        "gk_cs_rate": weights.gk_cs_rate.cap,
    }


def _theoretical_quality_cap(weights: QualityWeights, position: Position) -> float:
    """Weight-cap sum for a position's quality path, post-attenuation.

    NOT a ceiling: this is the value the pre-#88 derivation used as one, and
    the calibration measured it as overestimating achievable raw quality by
    ~10-40% for every position whose signals do not saturate (elite npxG/90
    is ~0.6 against the 0.8 the cap assumes; no keeper posts the 100%
    clean-sheet rate gk_cs_rate.cap assumes). Kept as the sanity bracket the
    calibrated ceilings are tested against: an anchor far above this is a
    calibration-script bug, an anchor far below it is a weight change that
    outran the recorded calibration. May be ``inf`` where a weight is
    uncapped (waiver npxg) — the bracket test skips those terms' families.
    """
    variant = _position_weights(weights, position)
    caps = _quality_term_caps(variant)
    form_cap, ppg_cap = caps["form"], caps["ppg"]
    # Everything the variant weights except form and ppg, which are attenuated
    # separately below. The zeroed terms of each variant contribute nothing, so
    # this is the GK block for a keeper, dc/90 for a defender, and the attacking
    # route plus penalty xG for the rest.
    per90 = sum(cap for name, cap in caps.items() if name not in ("form", "ppg"))
    form_max = (
        _NON_ATK_FORM_MAX
        if position in ("GK", "DEF")
        else FORM_TRAJECTORY_BOUNDS[1] * XGI_SUSTAINABILITY_BOUNDS[1]
    )
    return (per90 + form_cap * form_max + ppg_cap) * POSITION_SCORE_MULTIPLIER[position]


# Quality ceilings: the empirically calibrated elite raw quality per
# (family, position), replacing both the hand-tuned MID/FWD anchors and the
# theoretical cap-sum GK/DEF derivations (issue #88 — the cap sums assume
# signal saturation that only DEF exhibits, so elite MID/FWD/GK landed ~60
# where elite DEFs landed ~89). Values are post-position-multiplier raw
# quality; ownership families add bonus headroom programmatically below.
#
# The elite target the generated block was calibrated to: the top player of
# a (family, position, snapshot) pool normalises to ~this share of the scale
# (anchor = top_raw / target). It is also the scale of the value family's
# prior-implied raw score (`blend_quality_with_prior`): a player at the top
# of last season's pts/90 percentile is read as an elite of exactly this
# size, so the blend lives on the raw scale the calibration measured and
# re-scales itself on recalibration. Changing it is a recalibration by
# definition — re-run the script with --write; the block's header records
# the value it was built against.
CALIBRATION_ELITE_TARGET = 0.92
# --- BEGIN calibrated quality ceilings (generated) ---
# Calibrated by scripts/calibrate_quality_ceilings.py against 2025-26
# (snapshots GW10, GW15, GW19, GW24, GW29, GW34, GW38; pool 300+ minutes;
# elite anchor top_raw/0.92; run 2026-08-25).
# Do not hand-edit: re-run the script with --write after any weight change.
QUALITY_CEILINGS: dict[tuple[str, Position], float] = {
    ("target", "GK"): 11.99,
    ("target", "DEF"): 9.63,
    ("target", "MID"): 13.90,
    ("target", "FWD"): 20.17,
    ("differential", "GK"): 13.41,
    ("differential", "DEF"): 11.78,
    ("differential", "MID"): 16.03,
    ("differential", "FWD"): 21.49,
    ("waiver", "GK"): 13.75,
    ("waiver", "DEF"): 12.26,
    ("waiver", "MID"): 15.07,
    ("waiver", "FWD"): 18.12,
    ("value", "GK"): 14.42,
    ("value", "DEF"): 13.23,
    ("value", "MID"): 17.00,
    ("value", "FWD"): 21.60,
}
CALIBRATION_FINGERPRINT = "d14f7c1b3886caea"
# --- END calibrated quality ceilings (generated) ---

# Ownership-family ceilings = calibrated quality anchor + bonus headroom.
# The headroom terms stay derived (not calibrated) because their maxima are
# genuinely achievable — a player can face matchup 8.0 and sit at 0%
# ownership — and including them keeps the consistency bonus from silently
# clamping a top-pool player to 100 (same reasoning as the PR #17 DEF
# rebuild, now uniform across all four positions).
_OWNERSHIP_HEADROOM: dict[str, float] = {
    "target": _MATCHUP_MAX + _CONSISTENCY_MAX_TARGET,
    "differential": _OWNERSHIP_MAX + _MATCHUP_MAX + _CONSISTENCY_MAX_DIFF,
    "waiver": _MATCHUP_MAX + _POSITION_NEED_MAX + _CONSISTENCY_MAX_WAIVER,
}

GK_TARGET_CEILING = QUALITY_CEILINGS[("target", "GK")] + _OWNERSHIP_HEADROOM["target"]
DEF_TARGET_CEILING = QUALITY_CEILINGS[("target", "DEF")] + _OWNERSHIP_HEADROOM["target"]
MID_TARGET_CEILING = QUALITY_CEILINGS[("target", "MID")] + _OWNERSHIP_HEADROOM["target"]
FWD_TARGET_CEILING = QUALITY_CEILINGS[("target", "FWD")] + _OWNERSHIP_HEADROOM["target"]
GK_DIFFERENTIAL_CEILING = (
    QUALITY_CEILINGS[("differential", "GK")] + _OWNERSHIP_HEADROOM["differential"]
)
DEF_DIFFERENTIAL_CEILING = (
    QUALITY_CEILINGS[("differential", "DEF")] + _OWNERSHIP_HEADROOM["differential"]
)
MID_DIFFERENTIAL_CEILING = (
    QUALITY_CEILINGS[("differential", "MID")] + _OWNERSHIP_HEADROOM["differential"]
)
FWD_DIFFERENTIAL_CEILING = (
    QUALITY_CEILINGS[("differential", "FWD")] + _OWNERSHIP_HEADROOM["differential"]
)
GK_WAIVER_CEILING = QUALITY_CEILINGS[("waiver", "GK")] + _OWNERSHIP_HEADROOM["waiver"]
DEF_WAIVER_CEILING = QUALITY_CEILINGS[("waiver", "DEF")] + _OWNERSHIP_HEADROOM["waiver"]
MID_WAIVER_CEILING = QUALITY_CEILINGS[("waiver", "MID")] + _OWNERSHIP_HEADROOM["waiver"]
FWD_WAIVER_CEILING = QUALITY_CEILINGS[("waiver", "FWD")] + _OWNERSHIP_HEADROOM["waiver"]
# Value family has no matchup or consistency — compute_quality_value skips
# _calculate_quality_based_raw entirely.
GK_VALUE_CEILING = QUALITY_CEILINGS[("value", "GK")]
DEF_VALUE_CEILING = QUALITY_CEILINGS[("value", "DEF")]
MID_VALUE_CEILING = QUALITY_CEILINGS[("value", "MID")]
FWD_VALUE_CEILING = QUALITY_CEILINGS[("value", "FWD")]

# Legacy shared-ceiling names, kept for the package's public API. MID and FWD
# no longer share a ceiling (elite FWD raw runs ~12% above elite MID, so a
# shared anchor permanently under-scores elite MIDs); these alias the MID
# variant.
TARGET_CEILING = MID_TARGET_CEILING
DIFFERENTIAL_CEILING = MID_DIFFERENTIAL_CEILING
WAIVER_CEILING = MID_WAIVER_CEILING
VALUE_CEILING = MID_VALUE_CEILING

# ---------------------------------------------------------------------------
# Minutes factor activation
# ---------------------------------------------------------------------------
# The minutes factor is disabled at or before this gameweek: too few matches
# have been played for minutes to separate a squad player from a starter, and
# every player would be penalised for the season simply being young.

MINS_FACTOR_START_GW = 5


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

_FAMILY_QUALITY_WEIGHTS: dict[str, QualityWeights] = {
    "target": TARGET_QUALITY_WEIGHTS,
    "differential": DIFFERENTIAL_QUALITY_WEIGHTS,
    "waiver": WAIVER_QUALITY_WEIGHTS,
    "value": VALUE_QUALITY_WEIGHTS,
}


# A full match's minutes: converts a gameweek count into the most sample the
# calendar can have supplied an ever-present keeper.
_FULL_MATCH_MINUTES = 90


# The three signals `gk_signal_enrichment` supplies, and the only terms the
# GK weight variant activates beyond form and ppg.
GK_SIGNAL_TERMS: tuple[str, ...] = ("gk_saves_per_90", "gk_xgc_quality", "gk_cs_rate")


def gk_calendar_ramp(next_gw_id: int) -> float:
    """Share of the GK sample ramp the league calendar has made reachable before *next_gw_id*.

    ``gk_signal_enrichment`` scales every GK signal by
    ``min(minutes / GK_SAMPLE_RAMP_MINUTES, 1)``. The most sample the calendar
    can have supplied an ever-present keeper is ``(next_gw_id - 1) * 90``
    minutes, so this is the ramp such a keeper sits at — 0 before GW1, 1 from
    GW6 — keyed to the date, never to a player's own minutes. Two consumers
    read the same number: ``gk_ceiling_attainability`` scales the GK anchors
    by it, and the value family's prior blend discounts a keeper's
    early-season confidence by it (``prior_blend_weight``), so the signal
    share the calendar has suppressed is neither read as a low score nor
    trusted as a full observation.
    """
    calendar_minutes = max(next_gw_id - 1, 0) * _FULL_MATCH_MINUTES
    return min(calendar_minutes / GK_SAMPLE_RAMP_MINUTES, 1.0)


def ceiling_attainability(
    weights: QualityWeights, missing: Collection[str], *, ramp: float = 0.0
) -> float:
    """Fraction of a calibrated ceiling reachable when *missing* terms cannot be supplied.

    The ceilings in ``QUALITY_CEILINGS`` were calibrated against live snapshots
    where every term a position's weight variant activates had a value. Divide
    a raw score by the full ceiling when the input could never populate some of
    those terms and the whole population is capped below the top of the scale,
    however good it is — issue #143 for a keeper whose signals are still
    sample-ramped, issue #132 for a completed season whose source never
    recorded defensive contribution or the GK block. Scaling the ceiling by the
    share of its budgeted headroom the input can actually reach makes the score
    mean "how good is this, on the signals we have" and keeps positions
    comparable.

    *missing* names terms from ``_quality_term_caps``; an unknown name raises
    KeyError rather than being silently ignored. A term the variant zeroes
    contributes nothing either way, so a caller may pass the full set of terms
    its input lacks and let the variant decide which of them mattered.

    *ramp* is how much of a missing term the input does supply: 0.0 (the
    default) for a term that is structurally absent, and a fraction for one
    that is merely attenuated, as the GK sample ramp attenuates all three GK
    signals early in a season. The position multiplier cancels in the ratio,
    and the result never reaches 0 — form and ppg are always supplied — so it
    is always safe as a ``normalise_score`` denominator.

    A variant carrying an uncapped term (waiver npxg, on the base variant)
    raises ValueError: there is no share of unbounded headroom to take, and a
    silent 1.0 would leave a caller's ceiling undiscounted and its fix looking
    like it did nothing. Neither current caller can reach it — the GK path
    always passes ``for_gk()`` and the returnee radar the VALUE variants,
    all of which cap every term they weight.
    """
    caps = _quality_term_caps(weights)
    total = sum(caps.values())
    if not isfinite(total):
        uncapped = ", ".join(sorted(n for n, cap in caps.items() if not isfinite(cap)))
        raise ValueError(
            f"ceiling attainability is undefined for uncapped terms: {uncapped}"
        )
    if total <= 0:
        return 1.0
    shortfall = sum(caps[name] for name in missing) * (1.0 - ramp)
    reachable = total - shortfall
    return reachable / total if reachable > 0 else 1.0


def gk_ceiling_attainability(next_gw_id: int, weights: QualityWeights) -> float:
    """Fraction of the calibrated GK anchor attainable by this point of the season.

    ``build_scoring_enrichment`` scales all three GK signals (saves/90, xGC
    quality, CS rate) by ``min(minutes / GK_SAMPLE_RAMP_MINUTES, 1)`` as a
    deliberate small-sample guard. The calibrated anchors were measured at
    GW10+ snapshots where every regular keeper's ramp is 1.0, so dividing an
    early-season GK raw score by the full anchor caps the whole position in
    the low 70s however well anyone plays (issue #143: best GK in the league
    read 72 going into GW2). Scaling the anchor's ramped share restores the
    scale's meaning — elite among what a keeper *could* have shown by now —
    without touching raw scores or the recorded anchors.

    Keyed to the league calendar, not the player's own minutes: the most
    sample the calendar can have supplied is ``(next_gw_id - 1) * 90``, and
    only while that sits below ``GK_SAMPLE_RAMP_MINUTES`` is a small sample
    an artefact of the date. From GW6 the function is the identity, and a
    low-minute keeper's suppressed signals read as what they are — evidence
    the player does not play — instead of earning a private, lower ceiling
    that would rank a 180-minute backup above an ever-present starter
    (PR #156 review). A calendar-wide denominator also keeps every keeper in
    a list normalised against the same ceiling, so display order can never
    invert raw order within the position.

    The ramped/unramped split is approximated from the weight caps by
    ``ceiling_attainability``: an elite keeper saturates the GK-signal caps at
    full sample (the premise the calibration validated), so at ramp r those
    contributions scale ~linearly with r while form/ppg do not.
    """
    ramp = gk_calendar_ramp(next_gw_id)
    if ramp >= 1.0:
        return 1.0
    return ceiling_attainability(weights.for_gk(), GK_SIGNAL_TERMS, ramp=ramp)


def _ownership_ceiling_for(
    family: Literal["target", "differential", "waiver"],
    position: Position,
    *,
    next_gw_id: int | None = None,
) -> float:
    """Calibrated ceiling for an ownership family — total over all four positions.

    MID and FWD no longer fall back to a shared base ceiling: every
    (family, position) pair carries its own calibrated anchor. Raises
    KeyError on an unknown family (as before) and on an unknown position
    (previously the silent base-ceiling fallback).

    *next_gw_id*: when supplied for a GK, the anchor share of the ceiling is
    scaled by ``gk_ceiling_attainability`` so pre-GW6 keepers are normalised
    against what the calendar has let their ramped signals reach. Pass it
    only from paths whose evaluations carry the GK signal block — a scaled
    denominator over a signal-less numerator inflates keepers instead
    (PR #156 review: transfer-eval +44%, draft waiver +30% at GW2). The
    bonus headroom (matchup, ownership, position need, consistency) never
    scales. Omitted (None) keeps the full ceiling — the pre-#143 behaviour.
    """
    if family not in _OWNERSHIP_HEADROOM:
        raise KeyError(family)
    anchor = QUALITY_CEILINGS[(family, position)]
    if position == "GK" and next_gw_id is not None:
        anchor *= gk_ceiling_attainability(next_gw_id, _FAMILY_QUALITY_WEIGHTS[family])
    return anchor + _OWNERSHIP_HEADROOM[family]


def _value_weights_and_ceiling(
    position: Position, *, next_gw_id: int | None = None
) -> tuple[QualityWeights, float]:
    """Select VALUE_QUALITY_WEIGHTS variant and calibrated ceiling for a position.

    *next_gw_id*: same GK attainability scaling as ``_ownership_ceiling_for``
    (the value ceiling is the bare anchor, so the whole ceiling scales), with
    the same contract — pass it only when the numerator carries GK signals.
    """
    weights = _position_weights(VALUE_QUALITY_WEIGHTS, position)
    ceiling = QUALITY_CEILINGS[("value", position)]
    if position == "GK" and next_gw_id is not None:
        ceiling *= gk_ceiling_attainability(next_gw_id, VALUE_QUALITY_WEIGHTS)
    return weights, ceiling
