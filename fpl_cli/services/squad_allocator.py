"""ILP-based squad allocator for optimal FPL Classic squad selection."""

from __future__ import annotations

import dataclasses
from typing import TYPE_CHECKING

from fpl_cli.models.player import POSITION_MAP, PlayerStatus
from fpl_cli.services.player_prior import CUTOFF_GW
from fpl_cli.services.player_scoring import (
    GW_SELECTION_WEIGHTS,
    VALID_FORMATIONS,
    build_fixture_matchups,
    build_player_evaluation,
    build_scoring_enrichment,
    calculate_single_gw_core,
    compute_quality_value,
    per_90_rates,
    shrink_scores,
)

if TYPE_CHECKING:
    from fpl_cli.models.fixture import Fixture
    from fpl_cli.models.player import Player
    from fpl_cli.services.player_scoring import ScoringData
    from fpl_cli.services.team_ratings import TeamRating


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class ScoredPlayer:
    """A player with their raw quality score ready for the solver."""

    player: Player
    raw_quality: float
    position: str
    suspended_gw1: bool = False  # SUSPENDED + chance=0: zero GW1 coefficient


# ---------------------------------------------------------------------------
# Bulk player scoring
# ---------------------------------------------------------------------------


def _is_excluded(player: Player) -> bool:
    """Return True if the player should be excluded from the solver pool."""
    if player.status in (PlayerStatus.NOT_AVAILABLE, PlayerStatus.UNAVAILABLE):
        return True
    if player.status == PlayerStatus.INJURED and player.chance_of_playing_next_round == 0:
        return True
    return False


def score_all_players(
    scoring_data: ScoringData,
) -> list[ScoredPlayer]:
    """Score all eligible PL players via the existing scoring chain.

    Uses compute_quality_value(raw=True) for float precision, then
    applies early-season shrinkage via shrink_scores() (avoids the
    rounding in apply_shrinkage()).
    """
    if scoring_data.players is None:
        msg = "scoring_data.players is required (pass include_players=True)"
        raise ValueError(msg)

    understat_lookup = scoring_data.understat_lookup or {}
    player_histories = scoring_data.player_histories or {}
    next_gw_id = scoring_data.next_gw_id

    scored: list[tuple[Player, float, str, bool]] = []
    for player in scoring_data.players:
        if _is_excluded(player):
            continue

        position = POSITION_MAP.get(player.position.value, "MID")
        us_match = understat_lookup.get(player.id, {})
        team = scoring_data.team_map.get(player.team_id)
        team_short = team.short_name if team else "???"
        gw_history = player_histories.get(player.id)

        raw_quality = compute_quality_value(
            player,
            us_match,
            next_gw_id,
            team_short=team_short,
            gw_history=gw_history,
            raw=True,
        )

        suspended_gw1 = (
            player.status == PlayerStatus.SUSPENDED
            and player.chance_of_playing_next_round == 0
        )
        scored.append((player, raw_quality, position, suspended_gw1))

    # Apply early-season shrinkage (float-preserving)
    shrinkage_input = [(p.id, raw_q, pos) for p, raw_q, pos, _ in scored]
    shrunk = shrink_scores(
        shrinkage_input,
        scoring_data.player_priors,
        next_gw_id,
        CUTOFF_GW,
    )

    return [
        ScoredPlayer(
            player=player,
            raw_quality=adj_score,
            position=position,
            suspended_gw1=suspended_gw1,
        )
        for (player, _, position, suspended_gw1), (_, adj_score, _) in zip(scored, shrunk)
    ]


def score_all_players_sgw(
    scoring_data: ScoringData,
) -> list[ScoredPlayer]:
    """Score all eligible PL players using single-GW scoring for horizon=1.

    Uses ``calculate_single_gw_core`` with ``GW_SELECTION_WEIGHTS`` and
    ``matchup_weight=1.5`` (lineup weight). No shrinkage - single-GW
    decisions want the best current signal.
    """
    if scoring_data.players is None:
        msg = "scoring_data.players is required (pass include_players=True)"
        raise ValueError(msg)

    understat_lookup = scoring_data.understat_lookup or {}
    player_histories = scoring_data.player_histories or {}
    next_gw_id = scoring_data.next_gw_id
    scoring_ctx = scoring_data.scoring_ctx

    result: list[ScoredPlayer] = []
    for player in scoring_data.players:
        if _is_excluded(player):
            continue

        position = POSITION_MAP.get(player.position.value, "MID")

        us_match = understat_lookup.get(player.id, {})
        team = scoring_data.team_map.get(player.team_id)
        team_short = team.short_name if team else "???"
        gw_history = player_histories.get(player.id)
        enrichment = build_scoring_enrichment(player, us_match, team_short, gw_history, next_gw_id)

        matchups = build_fixture_matchups(player.team_id, position, scoring_ctx)

        evaluation, identity = build_player_evaluation(
            player, enrichment=enrichment, fixture_matchups=matchups,
        )

        xg_per_90, xa_per_90 = per_90_rates(evaluation, identity)

        raw_quality = calculate_single_gw_core(
            evaluation,
            GW_SELECTION_WEIGHTS,
            matchups,
            matchup_weight=1.5,
            next_gw_id=next_gw_id,
            xg_per_90=xg_per_90,
            xa_per_90=xa_per_90,
        )

        suspended_gw1 = (
            player.status == PlayerStatus.SUSPENDED
            and player.chance_of_playing_next_round == 0
        )

        result.append(ScoredPlayer(
            player=player,
            raw_quality=raw_quality,
            position=position,
            suspended_gw1=suspended_gw1,
        ))

    return result


# ---------------------------------------------------------------------------
# Fixture coefficient computation
# ---------------------------------------------------------------------------

# Position-variant fixture sensitivity (how much FDR swings the modifier)
FIXTURE_SENSITIVITY: dict[str, float] = {
    "GK": 0.30,
    "DEF": 0.30,
    "MID": 0.15,
    "FWD": 0.10,
}

MODIFIER_FLOOR = 0.25


def _get_opponent_fdr(
    position: str,
    opponent_rating: TeamRating,
    is_home: bool,
) -> float:
    """Get opponent FDR for a position using ``8 - raw_rating`` inversion.

    Returns a value where higher = harder fixture, consistent with the
    codebase FDR convention (see fdr-opponent-axis-inversion.md).

    GK/DEF care about opponent attacking strength.
    MID/FWD care about opponent defensive strength.
    Home fixture uses opponent's away rating, and vice versa.
    """
    if position in ("GK", "DEF"):
        raw = opponent_rating.atk_away if is_home else opponent_rating.atk_home
    else:
        raw = opponent_rating.def_away if is_home else opponent_rating.def_home
    return 8 - raw  # Invert: rating 1 (best) -> FDR 7 (hardest)


def _compute_modifier(position: str, opp_fdr: float) -> float:
    """Compute fixture modifier from opponent FDR and position sensitivity.

    Higher FDR = harder fixture = lower modifier (reduced expected output).
    Lower FDR = easier fixture = higher modifier (boosted expected output).
    """
    sensitivity = FIXTURE_SENSITIVITY.get(position, 0.10)
    return max(MODIFIER_FLOOR, 1.0 - sensitivity * (opp_fdr - 4) / 3)


def _get_team_fixtures_for_gw(
    team_id: int,
    gw: int,
    all_fixtures: list[Fixture],
) -> list[tuple[int, bool]]:
    """Get confirmed fixtures for a team in a given GW.

    Returns list of (opponent_team_id, is_home) tuples.
    Skips fixtures with gameweek=None (postponed/unscheduled).
    """
    result: list[tuple[int, bool]] = []
    for f in all_fixtures:
        if f.gameweek != gw:
            continue
        if f.home_team_id == team_id:
            result.append((f.away_team_id, True))
        elif f.away_team_id == team_id:
            result.append((f.home_team_id, False))
    return result


def compute_fixture_coefficients(
    scored_players: list[ScoredPlayer],
    scoring_data: ScoringData,
    horizon: int,
    start_gw: int | None = None,
) -> dict[int, list[float]]:
    """Compute per-player, per-GW fixture-adjusted coefficients.

    Returns {player_id: [coeff_gw1, coeff_gw2, ...]} where each
    coefficient = raw_quality * modifier (summed across fixtures in DGWs).

    Args:
        scored_players: Output from score_all_players().
        scoring_data: Pre-fetched data with fixtures, ratings, predictions.
        horizon: Number of gameweeks to look ahead.
        start_gw: First GW in range. Defaults to scoring_data.next_gw_id.
    """
    if start_gw is None:
        start_gw = scoring_data.next_gw_id

    end_gw = min(start_gw + horizon, 39)  # Don't exceed GW 38
    gw_range = list(range(start_gw, end_gw))

    ratings_service = scoring_data.ratings_service
    team_map = scoring_data.team_map
    prediction_lookup = scoring_data.scoring_ctx.prediction_lookup or {}

    coefficients: dict[int, list[float]] = {}

    for sp in scored_players:
        player_coeffs: list[float] = []
        team_id = sp.player.team_id

        for gw in gw_range:
            # Zero GW1 coefficient for suspended players
            if sp.suspended_gw1 and gw == start_gw:
                player_coeffs.append(0.0)
                continue

            confirmed_fixtures = _get_team_fixtures_for_gw(
                team_id, gw, scoring_data.all_fixtures,
            )

            gw_coeff = 0.0

            if confirmed_fixtures:
                # Process each confirmed fixture
                for opp_id, is_home in confirmed_fixtures:
                    opp_team = team_map.get(opp_id)
                    opp_short = opp_team.short_name if opp_team else None
                    opp_rating = (
                        ratings_service.get_rating(opp_short)
                        if opp_short else None
                    )
                    if opp_rating is not None:
                        opp_fdr = _get_opponent_fdr(sp.position, opp_rating, is_home)
                        modifier = _compute_modifier(sp.position, opp_fdr)
                    else:
                        modifier = 1.0
                    gw_coeff += sp.raw_quality * modifier

                # Check for predicted DGW extra fixture
                prediction = prediction_lookup.get(gw, {}).get(team_id)
                if prediction is not None:
                    pred_type, confidence = prediction
                    if pred_type == "double" and len(confirmed_fixtures) == 1:
                        # Predicted extra fixture scaled by confidence
                        gw_coeff += sp.raw_quality * 1.0 * confidence

            else:
                # No confirmed fixture for this GW
                prediction = prediction_lookup.get(gw, {}).get(team_id)
                if prediction is not None:
                    pred_type, confidence = prediction
                    if pred_type == "blank":
                        # Predicted blank: partial contribution
                        gw_coeff = sp.raw_quality * 1.0 * (1 - confidence)
                    elif pred_type == "double":
                        # Predicted double (no confirmed fixtures yet):
                        # 2 fixtures both scaled by confidence
                        gw_coeff = sp.raw_quality * 1.0 * confidence * 2
                    else:
                        # Unknown prediction type, assume normal
                        gw_coeff = sp.raw_quality * 1.0
                else:
                    # No fixture data at all: assume 1 fixture with modifier 1.0
                    gw_coeff = sp.raw_quality * 1.0

            player_coeffs.append(gw_coeff)

        coefficients[sp.player.id] = player_coeffs

    return coefficients


# ---------------------------------------------------------------------------
# ILP solver
# ---------------------------------------------------------------------------

SQUAD_SLOTS: dict[str, int] = {"GK": 2, "DEF": 5, "MID": 5, "FWD": 3}
GK_BENCH_DISCOUNT = 0.05
OUTFIELD_BENCH_DISCOUNT = 0.15
# Per-FT decay step in rate formula: rate = 1.0 - (STEP * ft_count).
# 0 FTs -> 1.0 (flat), 1 FT -> 0.96, 3 FTs -> 0.88, 5 FTs -> 0.80.
TEMPORAL_DISCOUNT_BASE_STEP = 0.04
# Bench cost pressure (Solve 2 only): penalise expensive bench players so
# the solver prefers cheap bench in the hierarchical Free Hit solve.
# Units: per £m of player.price.  0.1 flips Free Hit bench picks
# (penalty gap 0.21 > quality gap 0.165).
BENCH_COST_EPSILON = 0.1

DEFAULT_BENCH_DISCOUNT: dict[str, float] = {
    "GK": GK_BENCH_DISCOUNT,
    "DEF": OUTFIELD_BENCH_DISCOUNT,
    "MID": OUTFIELD_BENCH_DISCOUNT,
    "FWD": OUTFIELD_BENCH_DISCOUNT,
}


def _compute_discount_weights(horizon: int, free_transfers: int) -> list[float]:
    """Compute per-GW temporal discount weights.

    Geometric decay: rate^gw_offset where rate = 1.0 - BASE_STEP * ft_count.
    More FTs = steeper decay (more ability to course-correct mid-horizon).
    0 FTs = flat weights (no course-correction ability).
    """
    rate = max(0.5, min(1.0, 1.0 - TEMPORAL_DISCOUNT_BASE_STEP * free_transfers))
    return [rate**gw_offset for gw_offset in range(horizon)]


@dataclasses.dataclass(frozen=True)
class SquadResult:
    """Result from the ILP solver."""

    selected_players: list[ScoredPlayer]
    starter_ids: set[int]
    budget_used: float
    budget_remaining: float
    objective_value: float
    status: str  # "optimal" or "infeasible"
    formation: tuple[int, int, int]
    captain_schedule: dict[int, int]  # gw -> player_id
    owned_ids: frozenset[int] = dataclasses.field(default_factory=frozenset)
    player_savings: dict[int, float] = dataclasses.field(default_factory=dict)


def _effective_price(
    sp: ScoredPlayer,
    price_overrides: dict[int, float] | None,
) -> float:
    """Return sell price if overridden, else market price."""
    if price_overrides is not None:
        return price_overrides.get(sp.player.id, sp.player.price)
    return sp.player.price


def _solve_formation(
    formation: tuple[int, int, int],
    scored_players: list[ScoredPlayer],
    coefficients: dict[int, list[float]],
    budget: float,
    bench_discount: dict[str, float],
    discount_weights: list[float],
    bench_boost_gw_idx: int | None = None,
    price_overrides: dict[int, float] | None = None,
    starter_quality_floor: float | None = None,
) -> SquadResult | None:
    """Solve a single formation's ILP. Returns None if infeasible."""
    if not coefficients:
        return None

    from pulp import PULP_CBC_CMD, LpBinary, LpMaximize, LpProblem, LpVariable, value

    def_n, mid_n, fwd_n = formation
    n_gws = len(next(iter(coefficients.values())))

    prob = LpProblem(f"squad_{def_n}_{mid_n}_{fwd_n}", LpMaximize)

    # Binary variables: x[i] = in squad, s[i] = starter
    x = {sp.player.id: LpVariable(f"x_{sp.player.id}", cat=LpBinary) for sp in scored_players}
    s = {sp.player.id: LpVariable(f"s_{sp.player.id}", cat=LpBinary) for sp in scored_players}

    # Objective: maximise fixture-weighted quality (starters full, bench discounted)
    # Bench cost penalty only in Solve 2 (starter_quality_floor set)
    obj = []
    for sp in scored_players:
        pid = sp.player.id
        coeffs = coefficients[pid]
        bd_default = bench_discount.get(sp.position, OUTFIELD_BENCH_DISCOUNT)
        for gw_idx, gw_coeff in enumerate(coeffs):
            w = discount_weights[gw_idx]
            bd = 1.0 if gw_idx == bench_boost_gw_idx else bd_default
            obj.append(w * gw_coeff * s[pid])
            obj.append(w * bd * gw_coeff * (x[pid] - s[pid]))
            if starter_quality_floor is not None:
                obj.append(-w * BENCH_COST_EPSILON * _effective_price(sp, price_overrides) * (x[pid] - s[pid]))
    prob += sum(obj)

    # Budget constraint
    prob += sum(_effective_price(sp, price_overrides) * x[sp.player.id] for sp in scored_players) <= budget

    # Position slot constraints
    by_pos: dict[str, list[ScoredPlayer]] = {}
    for sp in scored_players:
        by_pos.setdefault(sp.position, []).append(sp)

    for pos, count in SQUAD_SLOTS.items():
        pos_players = by_pos.get(pos, [])
        prob += sum(x[sp.player.id] for sp in pos_players) == count

    # Max 3 per team
    by_team: dict[int, list[ScoredPlayer]] = {}
    for sp in scored_players:
        by_team.setdefault(sp.player.team_id, []).append(sp)

    for team_players in by_team.values():
        prob += sum(x[sp.player.id] for sp in team_players) <= 3

    # Starter constraints
    gk_players = by_pos.get("GK", [])
    prob += sum(s[sp.player.id] for sp in gk_players) == 1

    def_players = by_pos.get("DEF", [])
    prob += sum(s[sp.player.id] for sp in def_players) == def_n

    mid_players = by_pos.get("MID", [])
    prob += sum(s[sp.player.id] for sp in mid_players) == mid_n

    fwd_players = by_pos.get("FWD", [])
    prob += sum(s[sp.player.id] for sp in fwd_players) == fwd_n

    # s[i] <= x[i] (can only start if in squad)
    for sp in scored_players:
        pid = sp.player.id
        prob += s[pid] <= x[pid]

    # Starter quality floor (Solve 2 only): lock starter quality at Solve 1 level
    if starter_quality_floor is not None:
        starter_quality_expr = []
        for sp in scored_players:
            pid = sp.player.id
            coeffs = coefficients[pid]
            for gw_idx, gw_coeff in enumerate(coeffs):
                starter_quality_expr.append(discount_weights[gw_idx] * gw_coeff * s[pid])
        prob += sum(starter_quality_expr) >= starter_quality_floor

    # Solve
    solver = PULP_CBC_CMD(timeLimit=5, msg=False)
    prob.solve(solver)

    if prob.status != 1:  # 1 = Optimal
        return None

    # Extract results (PuLP stubs type value() imprecisely)
    def _val(v: LpVariable) -> float:
        r: float | None = value(v)  # type: ignore[assignment]
        return r if r is not None else 0.0

    selected = [sp for sp in scored_players if _val(x[sp.player.id]) > 0.5]
    starter_ids = {sp.player.id for sp in scored_players if _val(s[sp.player.id]) > 0.5}
    budget_used = sum(_effective_price(sp, price_overrides) for sp in selected)

    # Post-hoc captain schedule: highest coefficient starter per GW
    captain_schedule: dict[int, int] = {}
    for gw_idx in range(n_gws):
        best_pid = -1
        best_coeff = -1.0
        for sp in selected:
            if sp.player.id in starter_ids:
                coeff = coefficients[sp.player.id][gw_idx]
                if coeff > best_coeff:
                    best_coeff = coeff
                    best_pid = sp.player.id
        if best_pid >= 0:
            captain_schedule[gw_idx] = best_pid

    obj_val: float = value(prob.objective)  # type: ignore[assignment]

    # Compute owned-player markers and savings
    owned_ids: frozenset[int] = frozenset()
    player_savings: dict[int, float] = {}
    if price_overrides:
        market_price_by_id = {sp.player.id: sp.player.price for sp in selected}
        selected_owned = {
            pid for pid in market_price_by_id if pid in price_overrides
        }
        owned_ids = frozenset(selected_owned)
        player_savings = {
            pid: round(market_price_by_id[pid] - price_overrides[pid], 1)
            for pid in selected_owned
        }

    return SquadResult(
        selected_players=selected,
        starter_ids=starter_ids,
        budget_used=round(budget_used, 1),
        budget_remaining=round(budget - budget_used, 1),
        objective_value=round(obj_val, 4),
        status="optimal",
        formation=formation,
        captain_schedule=captain_schedule,
        owned_ids=owned_ids,
        player_savings=player_savings,
    )


def _starter_quality(
    starter_ids: set[int],
    coefficients: dict[int, list[float]],
    discount_weights: list[float],
) -> float:
    """Sum of temporal-weighted coefficients for starters."""
    return sum(
        discount_weights[gw_idx] * coefficients[pid][gw_idx]
        for pid in starter_ids
        for gw_idx in range(len(discount_weights))
    )


_HIERARCHICAL_BD_THRESHOLD = 0.05
_FLOOR_TOLERANCE = 1e-6


def solve_squad(
    scored_players: list[ScoredPlayer],
    coefficients: dict[int, list[float]],
    budget: float,
    bench_discount: dict[str, float] | None = None,
    bench_boost_gw_idx: int | None = None,
    free_transfers: int = 1,
    price_overrides: dict[int, float] | None = None,
) -> SquadResult:
    """Solve for the optimal 15-player squad across all valid formations.

    When all bench-discount values are below the hierarchical threshold
    (Free Hit regime), runs a two-pass lexicographic solve per formation:
    Solve 1 maximises quality, Solve 2 locks starter quality as a floor
    and adds bench cost pressure.  Otherwise runs a single pass.
    """
    if bench_discount is None:
        bench_discount = DEFAULT_BENCH_DISCOUNT

    if not coefficients:
        return _infeasible_result(budget)

    n_gws = len(next(iter(coefficients.values())))
    discount_weights = _compute_discount_weights(n_gws, free_transfers)
    hierarchical = all(v < _HIERARCHICAL_BD_THRESHOLD for v in bench_discount.values())

    best: SquadResult | None = None
    best_starter_quality = -1.0

    for formation in VALID_FORMATIONS:
        solve1 = _solve_formation(
            formation, scored_players, coefficients, budget, bench_discount,
            discount_weights,
            bench_boost_gw_idx=bench_boost_gw_idx,
            price_overrides=price_overrides,
        )
        if solve1 is None:
            continue

        if not hierarchical:
            # Single-pass: objective_value is pure quality (no penalty term)
            if best is None or solve1.objective_value > best.objective_value:
                best = solve1
            continue

        # Two-pass: Solve 2 with starter quality floor + bench cost pressure
        s1_quality = _starter_quality(solve1.starter_ids, coefficients, discount_weights)
        floor = s1_quality - _FLOOR_TOLERANCE

        solve2 = _solve_formation(
            formation, scored_players, coefficients, budget, bench_discount,
            discount_weights,
            bench_boost_gw_idx=bench_boost_gw_idx,
            price_overrides=price_overrides,
            starter_quality_floor=floor,
        )

        # Fallback: use Solve 1 only if Solve 2 failed or degraded starters.
        # The floor constraint guarantees starter quality, so cheaper bench
        # with more stranding is the desired outcome on Free Hit.
        if solve2 is None:
            result = solve1
        else:
            s2_quality = _starter_quality(solve2.starter_ids, coefficients, discount_weights)
            if s2_quality >= s1_quality - _FLOOR_TOLERANCE:
                result = solve2
            else:
                result = solve1

        r_quality = _starter_quality(result.starter_ids, coefficients, discount_weights)
        if best is None or r_quality > best_starter_quality or (
            r_quality == best_starter_quality and result.budget_used > best.budget_used
        ):
            best = result
            best_starter_quality = r_quality

    if best is None:
        return _infeasible_result(budget)

    return best


def _infeasible_result(budget: float) -> SquadResult:
    return SquadResult(
        selected_players=[],
        starter_ids=set(),
        budget_used=0.0,
        budget_remaining=budget,
        objective_value=0.0,
        status="infeasible",
        formation=(0, 0, 0),
        captain_schedule={},
    )
