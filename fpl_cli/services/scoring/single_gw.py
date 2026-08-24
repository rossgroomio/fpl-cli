"""The single-gameweek scoring family: captain, bench, lineup, starting XI.

Per-fixture scores for one gameweek built on the shared single-GW core
(matchup + form + xGI + penalty threat), plus the formation optimiser
that picks a starting XI from 15 scored squad players.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from fpl_cli.services.scoring.constants import (
    BENCH_CEILING,
    CAPTAIN_CEILING_SGW,
    CONSISTENCY_CV_LINEUP,
    CONSISTENCY_FLOOR_BENCH,
    CONSISTENCY_INV_BENCH,
    FDR_EASY,
    FDR_MEDIUM,
    GW_SELECTION_WEIGHTS,
    POSITION_SCORE_MULTIPLIER,
    STARTING_XI_CEILING,
    VALID_FORMATIONS,
    QualityWeights,
    _consistency_phase,
)
from fpl_cli.services.scoring.display import normalise_score
from fpl_cli.services.scoring.evaluation import FixtureMatchup, PlayerEvaluation, PlayerIdentity
from fpl_cli.services.scoring.value_quality import calculate_mins_factor

if TYPE_CHECKING:
    from fpl_cli.models.types import CaptainCandidate, FixtureDetail


def per_90_rates(
    evaluation: PlayerEvaluation, identity: PlayerIdentity,
) -> tuple[float, float]:
    """Derive xG and xA per-90 from season totals. Used by all single-GW consumers."""
    minutes_safe = max(evaluation.minutes, 1)
    return (
        (identity.expected_goals / minutes_safe) * 90,
        (identity.expected_assists / minutes_safe) * 90,
    )


# ---------------------------------------------------------------------------
# Single-GW core (shared by captain + bench)
# ---------------------------------------------------------------------------


def calculate_single_gw_core(
    evaluation: PlayerEvaluation,
    weights: QualityWeights,
    fixture_matchups: list[FixtureMatchup],
    *,
    matchup_weight: float,
    next_gw_id: int,
    xg_per_90: float = 0.0,
    xa_per_90: float = 0.0,
) -> float:
    """Core single-gameweek score shared by captain and bench scoring.

    Computes matchup contribution, form, xGI, penalty score, applies
    position multiplier and mins_factor, then adds home bonus.

    Args:
        matchup_weight: Per-fixture matchup multiplier (captain 2.0,
            bench 1.5). Parallel to ``ownership._matchup_bonus``'s
            hardcoded 0.75 which serves the same role for the ownership
            family's scalar 3-GW average.
        xg_per_90: FPL-derived xG per 90 (from identity.expected_goals).
        xa_per_90: FPL-derived xA per 90 (from identity.expected_assists).

    Returns a raw (un-normalised) float score.
    """
    if not fixture_matchups:
        return 0.0

    fixture_count = len(fixture_matchups)

    # Sum matchup scores across fixtures (DGW sums, not averages)
    matchup_total = sum(
        (fm.matchup_breakdown or {}).get("matchup_score", fm.matchup_score)
        for fm in fixture_matchups
    )

    # Form score (capped via weights, then scaled by trajectory and xGI sustainability)
    form_score = (
        min(evaluation.form * weights.form.multiplier, weights.form.cap)
        * evaluation.form_trajectory
        * evaluation.xgi_sustainability
    )

    # xGI score: prefer npxG when available (strips penalty noise)
    if evaluation.npxg_per_90 is not None:
        xgi_score = min((evaluation.npxg_per_90 + xa_per_90) * weights.npxg.multiplier, weights.npxg.cap)
    else:
        xgi_per_90_fallback = xg_per_90 + xa_per_90
        xgi_score = min(xgi_per_90_fallback * weights.xgi_fallback.multiplier, weights.xgi_fallback.cap)

    # Scale xGI by fixture count for DGW
    xgi_score *= fixture_count

    # Penalty xG score via StatWeight (per-90, scales with mins_factor)
    pen_raw = (evaluation.penalty_xg_per_90 or 0) * weights.penalty_xg.multiplier
    penalty_score = min(pen_raw, weights.penalty_xg.cap)

    # Minutes factor
    mins_factor = calculate_mins_factor(evaluation.minutes, evaluation.appearances, next_gw_id)

    # Ceiling components with position multiplier
    pos_mult = POSITION_SCORE_MULTIPLIER.get(evaluation.position, 1.0)
    ceiling_score = (
        matchup_total * matchup_weight +
        form_score +
        xgi_score * 1.0 +
        penalty_score
    ) * pos_mult * mins_factor

    # Flat bonuses (not affected by position multiplier)
    home_bonus = 1.0 if any(fm.is_home for fm in fixture_matchups) else 0.0

    # Consistency tiebreaker (additive, phase-in GW6-10)
    phase = _consistency_phase(next_gw_id)
    consistency_bonus = 0.0
    if phase > 0:
        consistency_bonus = (evaluation.cv_xgi_percentile - 0.5) * CONSISTENCY_CV_LINEUP * phase

    return ceiling_score + home_bonus + consistency_bonus


def calculate_captain_score(
    evaluation: PlayerEvaluation,
    identity: PlayerIdentity,
    *,
    next_gw_id: int,
) -> CaptainCandidate | None:
    """Score a player as a captain candidate.

    Returns a CaptainCandidate TypedDict (score + reasons + display data),
    or None if the player has no fixtures this gameweek.
    """
    if not evaluation.fixture_matchups:
        return None  # Blank gameweek

    fixture_count = len(evaluation.fixture_matchups)

    # Build fixture details for display and FDR calculation
    fixture_details: list[FixtureDetail] = []
    total_fdr = 0.0
    matchup_scores: list[dict[str, Any]] = []

    for fm in evaluation.fixture_matchups:
        fixture_details.append({
            "opponent": fm.opponent_short,
            "is_home": fm.is_home,
            "fdr": fm.opponent_fdr,
        })
        total_fdr += fm.opponent_fdr
        if fm.matchup_breakdown:
            matchup_scores.append(fm.matchup_breakdown)
        else:
            matchup_scores.append({"matchup_score": fm.matchup_score})

    avg_fdr = total_fdr / fixture_count

    # Matchup total for display
    matchup_total = sum(
        m.get("matchup_score", fm.matchup_score)
        for m, fm in zip(matchup_scores, evaluation.fixture_matchups)
    )

    xg_per_90, xa_per_90 = per_90_rates(evaluation, identity)

    # Delegate to shared core (captain uses matchup_weight=2.0)
    captain_score_raw = calculate_single_gw_core(
        evaluation,
        GW_SELECTION_WEIGHTS,
        evaluation.fixture_matchups,
        matchup_weight=2.0,
        next_gw_id=next_gw_id,
        xg_per_90=xg_per_90,
        xa_per_90=xa_per_90,
    )

    # pen_bonus for display (derived from StatWeight, not flat conditional)
    w = GW_SELECTION_WEIGHTS
    pen_raw = (evaluation.penalty_xg_per_90 or 0) * w.penalty_xg.multiplier
    penalty_score = min(pen_raw, w.penalty_xg.cap)
    mins_factor = calculate_mins_factor(evaluation.minutes, evaluation.appearances, next_gw_id)
    pen_bonus = round(penalty_score * mins_factor, 2)

    # Normalise to 0-100: SGW-based ceiling so DGW advantage shows naturally
    captain_score = normalise_score(captain_score_raw, CAPTAIN_CEILING_SGW)

    # Generate reasoning from ALL fixture matchups
    reasons: list[str] = []
    for matchup in matchup_scores:
        if matchup.get("reasoning"):
            reasons.extend(matchup["reasoning"])

    if avg_fdr <= FDR_EASY:
        reasons.append(f"Excellent FDR ({avg_fdr:.1f})")
    elif avg_fdr <= FDR_MEDIUM:
        reasons.append(f"Good FDR ({avg_fdr:.1f})")

    if fixture_count > 1:
        reasons.append(f"Double gameweek ({fixture_count} games)")

    if evaluation.form >= 6:
        reasons.append(f"In great form ({evaluation.form})")
    elif evaluation.form >= 4:
        reasons.append(f"In decent form ({evaluation.form})")

    xgi_per_90 = xg_per_90 + xa_per_90
    if xgi_per_90 >= 0.6:
        reasons.append(f"High xGI ({xgi_per_90:.2f}/90)")
    elif xg_per_90 >= 0.5:
        reasons.append(f"High xG ({xg_per_90:.2f}/90)")
    elif xa_per_90 >= 0.3:
        reasons.append(f"High xA ({xa_per_90:.2f}/90)")

    if any(fm.is_home for fm in evaluation.fixture_matchups):
        reasons.append("Playing at home")

    if evaluation.penalties_order == 1:
        reasons.append("Primary penalty taker")

    # Availability warning
    if evaluation.status != "a" and evaluation.chance_of_playing is not None:
        reasons.append(f"Flagged ({evaluation.chance_of_playing}% chance)")

    # Blank rate reason (display only)
    if evaluation.blank_rate is not None:
        if evaluation.blank_rate >= 0.6:
            reasons.append(f"Blanks in {evaluation.blank_rate:.0%} of recent matches")
        elif evaluation.blank_rate <= 0.15:
            reasons.append(f"Returns in {1 - evaluation.blank_rate:.0%} of recent matches")

    primary = matchup_scores[0] if matchup_scores else {}
    result: CaptainCandidate = {
        "id": identity.id,
        "player_name": identity.web_name,
        "team_short": identity.team_short,
        "position": identity.position_name,
        "price": identity.price,
        "ownership": identity.ownership,
        "form": evaluation.form,
        "ppg": identity.points_per_game,
        "xG": round(identity.expected_goals, 2),
        "xA": round(identity.expected_assists, 2),
        "xGI": round(identity.expected_goals + identity.expected_assists, 2),
        "xG_per_90": round(xg_per_90, 2),
        "xA_per_90": round(xa_per_90, 2),
        "xGI_per_90": round(xgi_per_90, 2),
        "fixtures": fixture_details,
        "fixture_count": fixture_count,
        "avg_fdr": round(avg_fdr, 2),
        "matchup_score": round(matchup_total, 2),
        "attack_matchup": round(float(primary.get("attack_matchup", 5.0)), 2),
        "defence_matchup": round(float(primary.get("defence_matchup", 5.0)), 2),
        "form_differential": round(float(primary.get("form_differential", 0.0)), 2),
        "position_differential": round(float(primary.get("position_differential", 0.0)), 2),
        "pen_bonus": pen_bonus,
        "captain_score": captain_score,
        "captain_score_raw": round(captain_score_raw, 2),
        "cv_xgi_percentile": evaluation.cv_xgi_percentile,
        "reasons": reasons,
    }
    if evaluation.raw_npxg_per_90 is not None:
        result["raw_npxg_per_90"] = round(evaluation.raw_npxg_per_90, 4)
        if (evaluation.npxg_per_90 is not None
                and evaluation.npxg_per_90 != evaluation.raw_npxg_per_90):
            result["adjusted_npxg_per_90"] = round(evaluation.npxg_per_90, 4)
    return result




def calculate_bench_score(
    evaluation: PlayerEvaluation,
    identity: PlayerIdentity,
    *,
    availability_risks: list[dict[str, Any]],
    next_gw_id: int,
) -> dict[str, Any]:
    """Score a bench player for priority ordering.

    Uses the shared single-GW core (matchup + form + xGI + penalty +
    position multiplier + mins_factor + home bonus) then adds
    bench-specific bonuses on top.

    Returns a display dict with priority_score (normalised 0-100 int),
    priority_score_raw (un-normalised float), reasons, and metadata.
    """
    reasons: list[str] = []

    xg_per_90, xa_per_90 = per_90_rates(evaluation, identity)

    # Core score via shared engine (bench uses matchup_weight=1.5)
    score = calculate_single_gw_core(
        evaluation,
        GW_SELECTION_WEIGHTS,
        evaluation.fixture_matchups,
        matchup_weight=1.5,
        next_gw_id=next_gw_id,
        xg_per_90=xg_per_90,
        xa_per_90=xa_per_90,
    )

    # --- Bench-specific bonuses (outside core) ---

    # Boost for covering a risky starter at the same position
    position_at_risk = any(
        r["position"] == evaluation.position and r["risk_level"] >= 2
        for r in availability_risks
    )
    if position_at_risk:
        score += 2
        reasons.append("Covers risky starter")

    # Availability check
    if evaluation.status != "a":
        if evaluation.chance_of_playing is not None:
            if evaluation.chance_of_playing < 50:
                score -= 5
                reasons.append(f"Doubt ({evaluation.chance_of_playing}%)")
        else:
            score -= 3
            reasons.append("Availability doubt")

    # Set-piece taker micro-bonus (tiebreaker)
    if evaluation.penalties_order == 1:
        score += 0.5
        reasons.append("Primary penalty taker")
    elif (
        evaluation.corners_and_indirect_freekicks_order is not None
        or evaluation.direct_freekicks_order is not None
    ):
        score += 0.25
        reasons.append("Set-piece taker")

    # Consistency bonus: floor + involvement (additive, phase-in GW6-10)
    phase = _consistency_phase(next_gw_id)
    if phase > 0:
        score += (evaluation.floor_percentile - 0.5) * CONSISTENCY_FLOOR_BENCH * phase
        if evaluation.involvement_rate is not None:
            score += (evaluation.involvement_rate - 0.5) * CONSISTENCY_INV_BENCH * phase

    if evaluation.floor_percentile >= 0.7:
        reasons.append("Consistent performer")
    elif evaluation.floor_percentile <= 0.3:
        reasons.append("Volatile output")

    raw_score = round(score, 2)
    return {
        "id": identity.id,
        "name": identity.web_name,
        "team": identity.team_short,
        "position": identity.position_name,
        "price": identity.price,
        "form": evaluation.form,
        "ppg": identity.points_per_game,
        "priority_score": normalise_score(score, BENCH_CEILING),
        "priority_score_raw": raw_score,
        "reasons": reasons if reasons else ["Standard bench option"],
    }


def calculate_lineup_score(
    evaluation: PlayerEvaluation,
    identity: PlayerIdentity,
    *,
    next_gw_id: int,
) -> dict[str, Any]:
    """Score a squad player for starting XI selection.

    Uses the shared single-GW core (matchup_weight=1.5, same as bench)
    then applies lineup-specific tiered availability penalties.  Unlike
    bench scoring, availability is gated on ``chance_of_playing`` directly
    regardless of status — any doubt signal matters for starting decisions.

    Returns a display dict with lineup_score (normalised 0-100 int),
    lineup_score_raw (un-normalised float), excluded flag, reasons, and
    metadata.
    """
    reasons: list[str] = []

    xg_per_90, xa_per_90 = per_90_rates(evaluation, identity)

    # Core score via shared engine (lineup uses matchup_weight=1.5)
    score = calculate_single_gw_core(
        evaluation,
        GW_SELECTION_WEIGHTS,
        evaluation.fixture_matchups,
        matchup_weight=1.5,
        next_gw_id=next_gw_id,
        xg_per_90=xg_per_90,
        xa_per_90=xa_per_90,
    )

    # --- Lineup-specific availability adjustment ---
    # Gates on chance_of_playing directly (forward-looking FPL flag),
    # not status-first like bench. Any doubt signal matters for starting.
    excluded = False
    exclusion_reason: str | None = None
    cop = evaluation.chance_of_playing

    if cop is not None:
        if cop < 50:
            excluded = True
            exclusion_reason = f"Low availability ({cop}%)"
            reasons.append(f"Excluded ({cop}% chance)")
        elif cop < 75:
            score -= 3
            reasons.append(f"Availability doubt ({cop}%)")
        elif cop < 100:
            score -= 1
            reasons.append(f"Minor doubt ({cop}%)")

    raw_score = round(score, 2)
    return {
        "id": identity.id,
        "name": identity.web_name,
        "team": identity.team_short,
        "position": identity.position_name,
        "price": identity.price,
        "form": evaluation.form,
        "ppg": identity.points_per_game,
        "lineup_score": normalise_score(score, STARTING_XI_CEILING),
        "lineup_score_raw": raw_score,
        "excluded": excluded,
        "exclusion_reason": exclusion_reason,
        "positional_fdr": evaluation.positional_fdr,
        "reasons": reasons if reasons else ["Available"],
    }


def select_starting_xi(
    scored_players: list[dict[str, Any]],
    *,
    team_fixtures: dict[str, dict[str, float]] | None = None,
) -> dict[str, Any]:
    """Select optimal starting XI from 15 scored squad players.

    Brute-force over 7 valid formations, picking top N per position,
    applying team exposure penalties. Deterministic: tied formations
    resolve to the most attacking option (fewest DEF).

    Args:
        scored_players: Output of calculate_lineup_score() for 15 players.
        team_fixtures: Optional {team_short: {"atk_fdr": float, "def_fdr": float}}
            for team exposure penalty. If None, no exposure penalty applied.

    Returns dict with starting_xi, bench, formation, total_score,
    team_exposure_penalties. If no valid formation has enough available
    (non-excluded) players per position, starting_xi is empty, formation
    is None, and total_score is 0.0 -- every player lands on the bench.
    """
    # Separate by position
    by_pos: dict[str, list[dict[str, Any]]] = {"GK": [], "DEF": [], "MID": [], "FWD": []}
    for p in scored_players:
        by_pos.setdefault(p["position"], []).append(p)

    # Sort each position by raw score descending, ID tiebreaker (deterministic)
    for pos in by_pos:
        by_pos[pos] = sorted(by_pos[pos], key=lambda x: (-x["lineup_score_raw"], x["id"]))

    # Partition excluded players (hard floor <50%) - they go to bench
    available: dict[str, list[dict[str, Any]]] = {}
    excluded: list[dict[str, Any]] = []
    for pos, players in by_pos.items():
        available[pos] = []
        for p in players:
            if p["excluded"]:
                excluded.append(p)
            else:
                available[pos].append(p)

    # GK: always exactly 1 starter (best available)
    gk_starter = available["GK"][0] if available["GK"] else None

    best_formation: str | None = None
    best_xi: list[dict[str, Any]] = []
    best_total: float | None = None
    best_penalties: list[dict[str, Any]] = []

    for def_n, mid_n, fwd_n in VALID_FORMATIONS:
        # Check we have enough available players per position
        if len(available["DEF"]) < def_n or len(available["MID"]) < mid_n or len(available["FWD"]) < fwd_n:
            continue

        picks = {
            "DEF": available["DEF"][:def_n],
            "MID": available["MID"][:mid_n],
            "FWD": available["FWD"][:fwd_n],
        }
        outfield = picks["DEF"] + picks["MID"] + picks["FWD"]
        formation_total = sum(p["lineup_score_raw"] for p in outfield)
        if gk_starter:
            formation_total += gk_starter["lineup_score_raw"]

        # Team exposure penalty
        penalties: list[dict[str, Any]] = []
        if team_fixtures:
            team_counts: dict[str, list[dict[str, Any]]] = {}
            xi_players = ([gk_starter] if gk_starter else []) + outfield
            for p in xi_players:
                team_counts.setdefault(p["team"], []).append(p)

            for team_short, team_players in team_counts.items():
                if len(team_players) < 2:
                    continue
                tf = team_fixtures.get(team_short, {})
                for p in team_players[1:]:
                    # ATK FDR for MID/FWD, DEF FDR for GK/DEF
                    if p["position"] in ("MID", "FWD"):
                        fdr = tf.get("atk_fdr", 0.0)
                    else:
                        fdr = tf.get("def_fdr", 0.0)
                    if fdr >= 5.0:
                        formation_total -= 2
                        penalties.append({
                            "team": team_short,
                            "player": p["name"],
                            "fdr": fdr,
                            "penalty": -2,
                        })

        if best_total is None or formation_total > best_total:
            best_total = formation_total
            best_formation = f"{def_n}-{mid_n}-{fwd_n}"
            best_xi = ([gk_starter] if gk_starter else []) + outfield
            best_penalties = penalties

    # Bench: everyone not in the XI
    xi_ids = {p["id"] for p in best_xi}
    bench = [p for p in scored_players if p["id"] not in xi_ids]
    bench = sorted(bench, key=lambda x: (-x["lineup_score_raw"], x["id"]))

    return {
        "starting_xi": best_xi,
        "bench": bench,
        "formation": best_formation,
        "total_score": round(best_total, 2) if best_total is not None else 0.0,
        "team_exposure_penalties": best_penalties,
    }
