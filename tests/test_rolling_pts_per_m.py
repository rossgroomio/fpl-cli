"""Tests for compute_rolling_pts_per_m."""
from __future__ import annotations

from fpl_cli.services.scoring import compute_rolling_pts_per_m


def _make_history(entries: list[tuple[int, int, int]], fixture_start: int = 100) -> list[dict]:
    """Build history list from (round, minutes, total_points) tuples."""
    return [
        {"round": r, "minutes": m, "total_points": pts, "fixture": fixture_start + i}
        for i, (r, m, pts) in enumerate(entries)
    ]


class TestHappyPath:
    def test_five_qualifying_fixtures(self):
        history = _make_history([
            (20, 90, 6), (21, 90, 8), (22, 90, 4), (23, 90, 10), (24, 90, 7),
        ])
        value, count = compute_rolling_pts_per_m(history, price=100, window=5)
        # avg = 35/5 = 7.0, price_m = 10.0, result = 7.0/10.0 = 0.7
        assert value == 0.7
        assert count == 5

    def test_exactly_five_returns_count_five(self):
        history = _make_history([
            (20, 90, 5), (21, 90, 5), (22, 90, 5), (23, 90, 5), (24, 90, 5),
        ])
        value, count = compute_rolling_pts_per_m(history, price=50, window=5)
        # avg = 5.0, price_m = 5.0, result = 1.0
        assert value == 1.0
        assert count == 5

    def test_three_qualifying_minimum_viable(self):
        history = _make_history([(20, 90, 6), (21, 90, 9), (22, 90, 3)])
        value, count = compute_rolling_pts_per_m(history, price=60, window=5)
        # avg = 18/3 = 6.0, price_m = 6.0, result = 1.0
        assert value == 1.0
        assert count == 3

    def test_custom_window_three(self):
        history = _make_history([
            (20, 90, 2), (21, 90, 4), (22, 90, 6), (23, 90, 8), (24, 90, 10),
        ])
        value, count = compute_rolling_pts_per_m(history, price=50, window=3)
        # Most recent 3: rounds 22(6), 23(8), 24(10) — avg = 24/3 = 8.0, price_m = 5.0, result = 1.6
        assert value == 1.6
        assert count == 3

    def test_dgw_two_entries_same_round(self):
        history = _make_history([
            (20, 90, 5), (20, 90, 3),  # DGW: two fixtures in round 20
            (21, 90, 7),
        ])
        value, count = compute_rolling_pts_per_m(history, price=50, window=5)
        # 3 qualifying fixtures, avg = 15/3 = 5.0, price_m = 5.0, result = 1.0
        assert value == 1.0
        assert count == 3

    def test_window_respected_different_sizes(self):
        history = _make_history([
            (18, 90, 2), (19, 90, 3), (20, 90, 4), (21, 90, 5),
            (22, 90, 6), (23, 90, 7), (24, 90, 8),
        ])
        v3, c3 = compute_rolling_pts_per_m(history, price=50, window=3)
        v7, c7 = compute_rolling_pts_per_m(history, price=50, window=7)
        assert c3 == 3
        assert c7 == 7
        # Both should be similar magnitude (per-fixture-average scale)
        assert v3 is not None and v7 is not None
        assert abs(v3 - v7) < 1.0  # scale-independent: similar magnitude

    def test_scale_independence(self):
        """Same player with different windows returns similar magnitude."""
        history = _make_history([
            (18, 90, 5), (19, 90, 5), (20, 90, 5), (21, 90, 5),
            (22, 90, 5), (23, 90, 5), (24, 90, 5),
        ])
        v3, _ = compute_rolling_pts_per_m(history, price=50, window=3)
        v7, _ = compute_rolling_pts_per_m(history, price=50, window=7)
        # With constant points, the values should be identical
        assert v3 == v7


class TestEdgeCases:
    def test_two_qualifying_returns_none(self):
        history = _make_history([(20, 90, 6), (21, 90, 8)])
        value, count = compute_rolling_pts_per_m(history, price=100, window=5)
        assert value is None
        assert count is None

    def test_zero_qualifying_returns_none(self):
        value, count = compute_rolling_pts_per_m([], price=100, window=5)
        assert value is None
        assert count is None

    def test_price_zero_returns_none(self):
        history = _make_history([(20, 90, 6), (21, 90, 8), (22, 90, 4)])
        value, count = compute_rolling_pts_per_m(history, price=0, window=5)
        assert value is None
        assert count is None

    def test_all_zero_minutes_returns_none(self):
        history = _make_history([(20, 0, 0), (21, 0, 0), (22, 0, 0), (23, 0, 0), (24, 0, 0)])
        value, count = compute_rolling_pts_per_m(history, price=100, window=5)
        assert value is None
        assert count is None

    def test_skips_zero_minute_fixtures_extends_backwards(self):
        history = _make_history([
            (18, 90, 4), (19, 90, 5),  # older qualifying
            (20, 0, 0),   # skipped (0 mins)
            (21, 90, 6), (22, 90, 7), (23, 90, 8),
        ])
        value, count = compute_rolling_pts_per_m(history, price=50, window=5)
        # 5 qualifying: rounds 18(4), 19(5), 21(6), 22(7), 23(8)
        # avg = 30/5 = 6.0, price_m = 5.0, result = 1.2
        assert value == 1.2
        assert count == 5

    def test_window_larger_than_qualifying(self):
        history = _make_history([(20, 90, 5), (21, 90, 5), (22, 90, 5), (23, 90, 5)])
        value, count = compute_rolling_pts_per_m(history, price=50, window=10)
        # Only 4 qualifying, window=10 but returns (value, 4)
        assert count == 4
        assert value == 1.0
