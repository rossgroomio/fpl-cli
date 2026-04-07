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
    team_id: int


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
