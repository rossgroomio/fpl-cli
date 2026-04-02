"""Matchup scoring service.

Provides position-weighted matchup scoring and multi-GW matchup windows.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from fpl_cli.models.fixture import Fixture

if TYPE_CHECKING:
    from fpl_cli.services.fixture_predictions import PredictionLookup

# Position-specific weights for matchup scoring
POSITION_WEIGHTS = {
    "FWD": {"attack": 0.45, "defence": 0.05, "form_diff": 0.35, "position": 0.15},
    "MID": {"attack": 0.35, "defence": 0.15, "form_diff": 0.35, "position": 0.15},
    "DEF": {"attack": 0.15, "defence": 0.35, "form_diff": 0.35, "position": 0.15},
    "GK": {"attack": 0.05, "defence": 0.45, "form_diff": 0.35, "position": 0.15},
}

# Recency weights for 3-GW window (nearest GW first)
GW_WEIGHTS = [0.5, 0.3, 0.2]


def build_team_fixture_map(
    fixtures: list[Fixture],
) -> dict[int, list[dict]]:
    """Map team IDs to their fixtures with home/away context."""
    result: dict[int, list[dict]] = {}
    for f in fixtures:
        result.setdefault(f.home_team_id, []).append({"fixture": f, "is_home": True})
        result.setdefault(f.away_team_id, []).append({"fixture": f, "is_home": False})
    return result


def calculate_matchup_score(
    player_team_form: dict[str, Any],
    opponent_form: dict[str, Any],
    position: str,
    is_home: bool,
) -> dict[str, Any]:
    """Calculate position-weighted matchup score with component breakdown.

    Args:
        player_team_form: Form data for player's team (from calculate_team_form)
        opponent_form: Form data for opponent team
        position: Player position (FWD/MID/DEF/GK)
        is_home: Whether player's team is playing at home

    Returns:
        Dictionary with matchup_score and component breakdown
    """
    # Get venue-specific stats
    # If player is home, use their home stats and opponent's AWAY stats
    # If player is away, use their away stats and opponent's HOME stats
    if is_home:
        team_gs = player_team_form.get("gs_home", 0)
        team_gc = player_team_form.get("gc_home", 0)
        opp_gs = opponent_form.get("gs_away", 0)
        opp_gc = opponent_form.get("gc_away", 0)
    else:
        team_gs = player_team_form.get("gs_away", 0)
        team_gc = player_team_form.get("gc_away", 0)
        opp_gs = opponent_form.get("gs_home", 0)
        opp_gc = opponent_form.get("gc_home", 0)

    # 1. Attack Matchup (0-10): How many goals can we expect?
    # Team's scoring rate + opponent's conceding rate
    team_attack_rate = team_gs / 6  # Goals per game at venue
    opponent_leak_rate = opp_gc / 6  # Goals conceded per game at venue
    attack_matchup = min((team_attack_rate + opponent_leak_rate) * 2.5, 10)

    # 2. Defence Matchup (0-10): How likely is a clean sheet?
    # Inverse of attack: fewer goals conceded + blunt opponent = higher score
    # 2.0 ceiling ≈ PL worst-case per-game rate; keeps 0-1 range per factor
    team_defence_rate = max(1 - (team_gc / 6) / 2.0, 0)  # Solid defence → high
    opponent_blunt_rate = max(1 - (opp_gs / 6) / 2.0, 0)  # Blunt opponent → high
    defence_matchup = min((team_defence_rate + opponent_blunt_rate) * 5, 10)

    # 3. Form Differential (-1 to +1): Recent momentum
    team_pts = player_team_form.get("pts_6", 0)
    opp_pts = opponent_form.get("pts_6", 0)
    form_diff = (team_pts - opp_pts) / 18  # Max diff is 18 (18-0)

    # 4. League Position Advantage (-1 to +1): Season quality
    team_pos = player_team_form.get("league_position", 10)
    opp_pos = opponent_form.get("league_position", 10)
    position_diff = (opp_pos - team_pos) / 19  # Max diff is 19 (20-1)

    # Get position-specific weights
    weights = POSITION_WEIGHTS.get(position, POSITION_WEIGHTS["MID"])

    # Calculate weighted matchup score (0-10 scale)
    matchup_score = (
        attack_matchup * weights["attack"]
        + defence_matchup * weights["defence"]
        + (form_diff + 1) * 5 * weights["form_diff"]  # Normalize -1..1 to 0..10
        + (position_diff + 1) * 5 * weights["position"]  # Normalize -1..1 to 0..10
    )

    # Generate reasoning
    reasoning = []
    if attack_matchup >= 7:
        reasoning.append(f"Strong attack matchup ({attack_matchup:.1f})")
    elif attack_matchup >= 5:
        reasoning.append(f"Good attack matchup ({attack_matchup:.1f})")

    if defence_matchup >= 7:
        reasoning.append(f"Strong defence matchup ({defence_matchup:.1f})")
    elif defence_matchup >= 5:
        reasoning.append(f"Good defence matchup ({defence_matchup:.1f})")

    if form_diff >= 0.3:
        reasoning.append(f"Form advantage +{form_diff:.2f}")
    elif form_diff <= -0.3:
        reasoning.append(f"Form disadvantage {form_diff:.2f}")

    if position_diff >= 0.3:
        reasoning.append("Position advantage")
    elif position_diff <= -0.3:
        reasoning.append("Position disadvantage")

    return {
        "matchup_score": round(matchup_score, 2),
        "attack_matchup": round(attack_matchup, 2),
        "defence_matchup": round(defence_matchup, 2),
        "form_differential": round(form_diff, 2),
        "position_differential": round(position_diff, 2),
        "reasoning": reasoning,
    }


def build_gw_fixture_maps(
    all_fixtures: list[Fixture],
    next_gw_id: int,
    window: int = 3,
) -> list[dict[int, list[dict]]]:
    """Pre-build team fixture maps for a GW window.

    Returns a list of ``window`` maps, one per GW offset. Each map is
    the output of ``build_team_fixture_map`` for that GW's fixtures.
    Call once, then pass to ``compute_3gw_matchup`` for each player.
    """
    maps: list[dict[int, list[dict]]] = []
    for gw_offset in range(window):
        gw_id = next_gw_id + gw_offset
        gw_fixtures = [f for f in all_fixtures if f.gameweek == gw_id]
        maps.append(build_team_fixture_map(gw_fixtures))
    return maps


def compute_3gw_matchup(
    team_id: int,
    all_fixtures: list[Fixture],
    next_gw_id: int,
    team_form_by_id: dict[int, dict[str, Any]],
    position: str,
    window: int = 3,
    gw_fixture_maps: list[dict[int, list[dict]]] | None = None,
    predictions: PredictionLookup | None = None,
) -> float:
    """Compute recency-weighted matchup score over a multi-GW window.

    For each GW in the window, sums matchup scores across fixtures (handling
    DGWs) then applies recency weights (nearest GW weighted highest).
    Blank GWs contribute 0 at their weight position.

    When *predictions* is provided and a GW has no confirmed fixtures for
    the team, predicted blanks/doubles are used with confidence-scaled
    recency weights.  Predictions never override confirmed fixtures.

    Args:
        team_id: Team to compute matchup for
        all_fixtures: All fixtures across all gameweeks (ignored if gw_fixture_maps provided)
        next_gw_id: Starting gameweek ID
        team_form_by_id: Team form data keyed by team_id
        position: Player position (FWD/MID/DEF/GK)
        window: Number of GWs to look ahead (default 3)
        gw_fixture_maps: Pre-built maps from build_gw_fixture_maps (avoids
            re-filtering fixtures per call)
        predictions: gw -> team_id -> (prediction_type, confidence_multiplier)
            from build_prediction_lookup.  None = no prediction enrichment.

    Returns:
        Weighted average matchup score (0-10 scale)
    """
    if gw_fixture_maps is None:
        gw_fixture_maps = build_gw_fixture_maps(all_fixtures, next_gw_id, window)

    player_team_form = team_form_by_id.get(team_id, {})
    base_weights = GW_WEIGHTS[:window]
    gw_matchups: list[float] = []
    effective_weights: list[float] = []

    for gw_offset in range(window):
        gw_number = next_gw_id + gw_offset
        recency_weight = base_weights[gw_offset] if gw_offset < len(base_weights) else 0.0
        fixtures_for_team = gw_fixture_maps[gw_offset].get(team_id, [])

        if fixtures_for_team:
            # Confirmed fixtures: full recency weight, no prediction override (R1)
            gw_total = 0.0
            for f_data in fixtures_for_team:
                fixture = f_data["fixture"]
                is_home = f_data["is_home"]
                opponent_id = fixture.away_team_id if is_home else fixture.home_team_id
                opponent_form = team_form_by_id.get(opponent_id, {})

                if player_team_form and opponent_form:
                    matchup = calculate_matchup_score(
                        player_team_form, opponent_form, position, is_home
                    )
                    gw_total += matchup["matchup_score"]
                else:
                    gw_total += 5.0  # Neutral fallback

            gw_matchups.append(gw_total)
            effective_weights.append(recency_weight)
            continue

        # No confirmed fixtures - check predictions
        pred = (
            predictions.get(gw_number, {}).get(team_id)
            if predictions
            else None
        )

        if pred is not None:
            pred_type, confidence = pred
            if pred_type == "double":
                gw_matchups.append(10.0)  # Two neutral 5.0 fixtures
            else:
                gw_matchups.append(0.0)  # Blank
            effective_weights.append(recency_weight * confidence)
        else:
            # No fixtures, no prediction: existing behaviour (R5)
            gw_matchups.append(0.0)
            effective_weights.append(recency_weight)

    if not gw_matchups:
        return 0.0

    # Apply effective weights (confidence-scaled where predictions used),
    # normalised so result stays on 0-10 scale
    weight_sum = sum(effective_weights)
    if weight_sum == 0:
        return 0.0
    weighted_sum = sum(
        gw_matchup * weight
        for gw_matchup, weight in zip(gw_matchups, effective_weights)
    )
    return weighted_sum / weight_sum
