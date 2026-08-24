"""Centralised player scoring engine.

All scoring formulas live here: quality baseline, target, differential,
waiver, captain, and bench. Agents build PlayerEvaluation objects and
delegate scoring to this package, importing the public API from the
package root.

Module map:

- ``constants``     — weights, ceilings, and scoring configuration
- ``signals``       — form trajectory, xGI sustainability, consistency, adjusted npxG
- ``evaluation``    — enrichment assembly and PlayerEvaluation / PlayerIdentity
- ``value_quality`` — quality baseline + VALUE family (quality_score, rolling pts/£m)
- ``ownership``     — target / differential / waiver family
- ``single_gw``     — captain / bench / lineup / starting XI family
- ``display``       — 0-100 normalisation and display ceiling routing
- ``shrinkage``     — early-season shrinkage toward position means
- ``data_prep``     — ScoringContext / ScoringData and prepare_scoring_data
"""

from fpl_cli.services.scoring.constants import (
    ATTACKING_POSITIONS,
    BENCH_CEILING,
    CAPTAIN_CEILING_SGW,
    CONSISTENCY_CV_DIFF,
    CONSISTENCY_CV_LINEUP,
    CONSISTENCY_CV_TARGET,
    CONSISTENCY_FLOOR_BENCH,
    CONSISTENCY_INV_BENCH,
    CONSISTENCY_PHASE_IN_END,
    CONSISTENCY_PHASE_IN_START,
    DEF_DIFFERENTIAL_CEILING,
    DEF_TARGET_CEILING,
    DEF_VALUE_CEILING,
    DEF_WAIVER_CEILING,
    DIFFERENTIAL_CEILING,
    DIFFERENTIAL_QUALITY_WEIGHTS,
    FDR_EASY,
    FDR_MEDIUM,
    FDR_MODE,
    GK_DIFFERENTIAL_CEILING,
    GK_TARGET_CEILING,
    GK_VALUE_CEILING,
    GK_WAIVER_CEILING,
    GW_SELECTION_WEIGHTS,
    MAX_GAMEWEEKS,
    POSITION_SCORE_MULTIPLIER,
    STARTING_XI_CEILING,
    TARGET_CEILING,
    TARGET_QUALITY_WEIGHTS,
    VALID_FORMATIONS,
    VALUE_CEILING,
    VALUE_QUALITY_WEIGHTS,
    WAIVER_CEILING,
    WAIVER_QUALITY_WEIGHTS,
    Position,
    QualityWeights,
    StatWeight,
)
from fpl_cli.services.scoring.data_prep import (
    ScoringContext,
    ScoringData,
    build_fixture_matchups,
    build_scoring_context,
    build_understat_by_player_id,
    compute_aggregate_matchup,
    fetch_match_records,
    prepare_scoring_data,
)
from fpl_cli.services.scoring.display import normalise_score, pick_display_ceiling
from fpl_cli.services.scoring.evaluation import (
    FixtureMatchup,
    PlayerEvaluation,
    PlayerIdentity,
    build_player_evaluation,
    build_scoring_enrichment,
)
from fpl_cli.services.scoring.ownership import (
    calculate_differential_score,
    calculate_target_score,
    calculate_waiver_score,
)
from fpl_cli.services.scoring.shrinkage import apply_shrinkage, shrink_scores
from fpl_cli.services.scoring.signals import (
    NEUTRAL_SIGNALS,
    ConsistencySignals,
    apply_adjusted_npxg,
    apply_consistency,
    build_adjusted_npxg_lookup,
    build_consistency_lookup,
    build_npxg_lookup_from_records,
    compute_adjusted_npxg,
    compute_blank_rate,
    compute_cv_xgi,
    compute_cv_xgi_fallback,
    compute_floor_xgi,
    compute_floor_xgi_fallback,
    compute_form_trajectory,
    compute_gk_consistency,
    compute_involvement_rate,
    compute_median_elo,
    compute_xgi_sustainability,
)
from fpl_cli.services.scoring.single_gw import (
    calculate_bench_score,
    calculate_captain_score,
    calculate_lineup_score,
    calculate_single_gw_core,
    per_90_rates,
    select_starting_xi,
)
from fpl_cli.services.scoring.value_quality import (
    calculate_mins_factor,
    calculate_player_quality_score,
    compute_quality_value,
    compute_rolling_pts_per_m,
)

__all__ = [
    # constants
    "ATTACKING_POSITIONS",
    "BENCH_CEILING",
    "CAPTAIN_CEILING_SGW",
    "CONSISTENCY_CV_DIFF",
    "CONSISTENCY_CV_LINEUP",
    "CONSISTENCY_CV_TARGET",
    "CONSISTENCY_FLOOR_BENCH",
    "CONSISTENCY_INV_BENCH",
    "CONSISTENCY_PHASE_IN_END",
    "CONSISTENCY_PHASE_IN_START",
    "DEF_DIFFERENTIAL_CEILING",
    "DEF_TARGET_CEILING",
    "DEF_VALUE_CEILING",
    "DEF_WAIVER_CEILING",
    "DIFFERENTIAL_CEILING",
    "DIFFERENTIAL_QUALITY_WEIGHTS",
    "FDR_EASY",
    "FDR_MEDIUM",
    "FDR_MODE",
    "GK_DIFFERENTIAL_CEILING",
    "GK_TARGET_CEILING",
    "GK_VALUE_CEILING",
    "GK_WAIVER_CEILING",
    "GW_SELECTION_WEIGHTS",
    "MAX_GAMEWEEKS",
    "POSITION_SCORE_MULTIPLIER",
    "STARTING_XI_CEILING",
    "TARGET_CEILING",
    "TARGET_QUALITY_WEIGHTS",
    "VALID_FORMATIONS",
    "VALUE_CEILING",
    "VALUE_QUALITY_WEIGHTS",
    "WAIVER_CEILING",
    "WAIVER_QUALITY_WEIGHTS",
    "Position",
    "QualityWeights",
    "StatWeight",
    # signals
    "NEUTRAL_SIGNALS",
    "ConsistencySignals",
    "apply_adjusted_npxg",
    "apply_consistency",
    "build_adjusted_npxg_lookup",
    "build_consistency_lookup",
    "build_npxg_lookup_from_records",
    "compute_adjusted_npxg",
    "compute_blank_rate",
    "compute_cv_xgi",
    "compute_cv_xgi_fallback",
    "compute_floor_xgi",
    "compute_floor_xgi_fallback",
    "compute_form_trajectory",
    "compute_gk_consistency",
    "compute_involvement_rate",
    "compute_median_elo",
    "compute_xgi_sustainability",
    # evaluation
    "FixtureMatchup",
    "PlayerEvaluation",
    "PlayerIdentity",
    "build_player_evaluation",
    "build_scoring_enrichment",
    # display
    "normalise_score",
    "pick_display_ceiling",
    # value_quality
    "calculate_mins_factor",
    "calculate_player_quality_score",
    "compute_quality_value",
    "compute_rolling_pts_per_m",
    # shrinkage
    "apply_shrinkage",
    "shrink_scores",
    # ownership
    "calculate_differential_score",
    "calculate_target_score",
    "calculate_waiver_score",
    # single_gw
    "calculate_bench_score",
    "calculate_captain_score",
    "calculate_lineup_score",
    "calculate_single_gw_core",
    "per_90_rates",
    "select_starting_xi",
    # data_prep
    "ScoringContext",
    "ScoringData",
    "build_fixture_matchups",
    "build_scoring_context",
    "build_understat_by_player_id",
    "compute_aggregate_matchup",
    "fetch_match_records",
    "prepare_scoring_data",
]
