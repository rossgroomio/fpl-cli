"""Shared domain types for historical player data clients."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import TypedDict

MOMENTUM_WINDOW = 5


@dataclass
class SeasonHistory:
    """One player, one season."""

    element_code: int
    season: str
    total_points: int
    minutes: int
    starts: int
    goals: int
    assists: int
    expected_goals: float
    expected_assists: float
    expected_goal_involvements: float
    start_cost: int
    end_cost: int
    position: str
    web_name: str
    team_id: int  # FPL club code (bootstrap teams[].code): stable across seasons and sources


@dataclass
class PlayerProfile:
    """One player across multiple seasons with computed signals."""

    element_code: int
    web_name: str
    current_position: str
    seasons: list[SeasonHistory] = field(default_factory=list)
    pts_per_90: list[float] = field(default_factory=list)
    pts_per_90_trend: float = 0.0
    cost_trajectory: float = 0.0
    xgi_per_90: list[float] = field(default_factory=list)
    xgi_per_90_trend: float | None = None
    minutes_per_start: list[float] = field(default_factory=list)
    reliability: float | None = None


@dataclass
class GwTrendProfile:
    """One player's intra-season price and transfer trend signals."""

    element: int
    web_name: str
    position: str
    team_name: str
    price_start: int
    price_current: int
    price_change: int
    price_slope: float
    price_acceleration: float
    transfer_momentum: int
    gw_count: int
    latest_gw: int
    first_gw: int


class _GwRow(TypedDict):
    value: int
    transfers_balance: int
    web_name: str
    position: str
    team_name: str


def compute_trend(values: list[float]) -> float:
    """Least-squares slope across season indices. Positive = improving."""
    n = len(values)
    if n <= 1:
        return 0.0
    xs = list(range(n))
    sum_x = sum(xs)
    sum_y = sum(values)
    sum_xy = sum(x * y for x, y in zip(xs, values))
    sum_x2 = sum(x * x for x in xs)
    denom = n * sum_x2 - sum_x * sum_x
    if denom == 0:
        return 0.0
    return round((n * sum_xy - sum_x * sum_y) / denom, 2)


def compute_reliability(
    seasons: list[SeasonHistory],
    current_season: str | None = None,
    current_gw: int = 38,
    weights: tuple[int, ...] = (3, 2, 1),
) -> float | None:
    """Recency-weighted historical availability score (0.0-1.0), or None if no data.

    Uses starts/denominator per season as a proxy for availability. All seasons
    count (no MIN_MINUTES filter) -- low-minute seasons are evidence of poor
    availability. Seasons are capped at the 3 most recent.

    Args:
        seasons: All SeasonHistory records for a player (any order).
        current_season: Label of the in-progress season (e.g. "2025-26").
            When provided and current_gw >= 10, uses current_gw as the
            denominator instead of 38.
        current_gw: Number of GWs played in the current season.
        weights: Recency weights where weights[0] applies to the most recent
            season. Default (3, 2, 1) = newest 3x, middle 2x, oldest 1x.
            Truncated to match the number of available seasons.
    """
    if not seasons:
        return None

    sorted_seasons = sorted(seasons, key=lambda s: s.season)

    # Exclude current season if current_gw < 10 (insufficient data)
    if current_season is not None and current_gw < 10:
        sorted_seasons = [s for s in sorted_seasons if s.season != current_season]

    if not sorted_seasons:
        return None

    # Cap at 3 most recent
    recent = sorted_seasons[-3:]

    if not recent:
        return None

    rates = []
    for s in recent:
        if current_season is not None and s.season == current_season:
            denominator = current_gw
        else:
            denominator = 38
        rate = s.starts / denominator if denominator > 0 else 0.0
        rates.append(rate)

    # Reverse weights so that weights[0] (newest) aligns with rates[-1] (newest).
    w = weights[:len(rates)]
    weighted_sum = sum(r * wt for r, wt in zip(rates, reversed(w)))
    weight_total = sum(w)
    if weight_total == 0:
        return None

    return min(1.0, weighted_sum / weight_total)


def compute_acceleration(values: list[float]) -> float:
    """Quadratic regression coefficient. Positive = price curve bending upward."""
    n = len(values)
    if n < 4:
        return 0.0
    # Solve y = a*x^2 + b*x + c via normal equations (Cramer's rule on 3x3)
    sx = sx2 = sx3 = sx4 = sy = sxy = sx2y = 0.0
    for i, y in enumerate(values):
        x = float(i)
        x2 = x * x
        sx += x
        sx2 += x2
        sx3 += x2 * x
        sx4 += x2 * x2
        sy += y
        sxy += x * y
        sx2y += x2 * y
    det = (
        sx4 * (sx2 * n - sx * sx)
        - sx3 * (sx3 * n - sx * sx2)
        + sx2 * (sx3 * sx - sx2 * sx2)
    )
    if abs(det) < 1e-12:
        return 0.0
    det_a = (
        sx2y * (sx2 * n - sx * sx)
        - sx3 * (sxy * n - sx * sy)
        + sx2 * (sxy * sx - sx2 * sy)
    )
    return round(det_a / det, 2)
