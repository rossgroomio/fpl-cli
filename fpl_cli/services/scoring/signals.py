"""Per-player scoring signals.

Form trajectory, xGI sustainability, the consistency signal family
(CV-xGI, blank rate, floor, involvement, GK consistency) with
position-relative percentiles, and opponent-adjusted npxG derived from
Core-Insights match records.
"""

from __future__ import annotations

import dataclasses
from typing import TYPE_CHECKING, Any

from fpl_cli.services.scoring.constants import ATTACKING_POSITIONS

if TYPE_CHECKING:
    from fpl_cli.api.core_insights import MatchRecord


@dataclasses.dataclass(frozen=True)
class ConsistencySignals:
    """Per-player consistency signal values (Phase 1: display only)."""

    cv_xgi_percentile: float = 0.5
    blank_rate: float | None = None
    floor_percentile: float = 0.5
    involvement_rate: float | None = None
    gk_consistency_percentile: float = 0.5


NEUTRAL_SIGNALS = ConsistencySignals()


def _qualifying_window(
    history: list[dict[str, Any]], current_gw: int, size: int = 7,
) -> list[dict[str, Any]]:
    """Recent qualifying GWs: minutes > 0, within 12-GW lookback, most recent *size*."""
    cutoff = current_gw - 12
    qualifying = [
        h
        for h in history
        if h.get("minutes", 0) > 0 and h.get("round", 0) > cutoff
    ]
    qualifying.sort(key=lambda h: h["round"])
    return qualifying[-size:]


def compute_form_trajectory(history: list[dict[str, Any]], current_gw: int) -> float:
    """Trend multiplier from recent gameweek points history.

    Returns a value in [0.8, 1.2] reflecting whether a player is on an
    upward or downward trajectory.  Median-filters outliers (drops highest
    and lowest) to resist one-off hauls / blanks.

    Returns 1.0 (neutral) when fewer than 4 qualifying GWs are available.
    """
    qualifying = _qualifying_window(history, current_gw)

    if len(qualifying) < 4:
        return 1.0

    points = [h.get("total_points", 0) for h in qualifying]

    # Median filter: drop one instance of the max and one of the min.
    # When ties exist, remove the instance closest to the centre of
    # the window (least chronologically informative) to preserve
    # slope signal from edge positions.
    filtered = list(points)
    for target in (max(filtered), min(filtered)):
        centre = (len(filtered) - 1) / 2
        indices = [i for i, v in enumerate(filtered) if v == target]
        drop = min(indices, key=lambda i: abs(i - centre))
        filtered.pop(drop)

    n = len(filtered)
    if n < 2:
        return 1.0

    # Least-squares linear regression
    x_vals = list(range(n))
    x_mean = sum(x_vals) / n
    y_mean = sum(filtered) / n

    numerator = sum((x - x_mean) * (y - y_mean) for x, y in zip(x_vals, filtered))
    denominator = sum((x - x_mean) ** 2 for x in x_vals)

    if denominator == 0:
        return 1.0

    slope = numerator / denominator

    # Clamped linear interpolation to [0.8, 1.2]
    # Neutral at slope=0; rising > 0, falling < 0
    if slope <= -1.5:
        return 0.8
    if slope <= 0.0:
        # -1.5 -> 0.8, 0.0 -> 1.0
        return 0.8 + (slope + 1.5) / 1.5 * 0.2
    if slope <= 2.0:
        # 0.0 -> 1.0, 2.0 -> 1.2
        return 1.0 + slope / 2.0 * 0.2
    return 1.2


def compute_xgi_sustainability(
    history: list[dict[str, Any]], current_gw: int, position: str
) -> tuple[float, float]:
    """Rolling-window xGI sustainability multiplier for ATK players.

    Computes per-match GI-xGI divergence over recent qualifying GWs and maps
    it to a bounded multiplier in [0.85, 1.15].  Positive divergence (GI > xGI)
    indicates overperformance -> regression risk -> multiplier < 1.0.  Negative
    divergence (GI < xGI) indicates underperformance -> upside -> multiplier > 1.0.

    Returns (multiplier, raw_divergence_per_match).  Returns (1.0, 0.0) for
    DEF/GK positions or when fewer than 4 qualifying GWs are available.

    Unlike compute_form_trajectory, no median filtering is applied: the hauls
    that constitute overperformance are the most informative data points.
    """
    if position not in ATTACKING_POSITIONS:
        return 1.0, 0.0

    qualifying = _qualifying_window(history, current_gw)

    if len(qualifying) < 4:
        return 1.0, 0.0

    divergences = [
        (h.get("goals_scored", 0) + h.get("assists", 0))
        - (float(h.get("expected_goals", 0) or 0) + float(h.get("expected_assists", 0) or 0))
        for h in qualifying
    ]
    avg_divergence = sum(divergences) / len(divergences)

    # Linear interpolation: divergence=0 -> 1.0, divergence=±0.3 -> 0.85/1.15
    raw_mult = 1.0 - (avg_divergence / 0.3) * 0.15
    multiplier = max(0.85, min(1.15, raw_mult))

    return multiplier, avg_divergence


# ---------------------------------------------------------------------------
# Consistency signal computation
# ---------------------------------------------------------------------------

_CONSISTENCY_MIN_MATCHES = 6


def compute_median_elo(
    match_records: dict[int, list[MatchRecord]],
) -> float:
    """Median opponent Elo across all match records, or 1700.0 if empty."""
    import statistics

    all_elos = [
        r["opponent_elo"]
        for records in match_records.values()
        for r in records
    ]
    return statistics.median(all_elos) if all_elos else 1700.0


def _elo_adjusted_xgis(
    window: list[MatchRecord], median_elo: float,
) -> list[float]:
    """Elo-adjusted xGI values for each match in the window."""
    adjusted: list[float] = []
    for m in window:
        opp_elo = m.get("opponent_elo", median_elo)
        if opp_elo <= 0:
            opp_elo = median_elo
        factor = max(0.80, min(1.25, median_elo / opp_elo))
        adjusted.append((m.get("xg", 0.0) + m.get("xa", 0.0)) * factor)
    return adjusted


_CONSISTENCY_MIN_MINUTES = 60


def _match_record_window(
    match_records: list[MatchRecord], current_gw: int, size: int = 7,
) -> list[MatchRecord]:
    """Recent qualifying match records: minutes >= 60, within 12-GW lookback, most recent *size*.

    The 60-minute threshold excludes cameo appearances whose low xGI
    reflects limited playing time rather than output inconsistency.
    Aligns with FPL's meaningful-appearance boundary.
    """
    cutoff = current_gw - 12
    qualifying = [
        m for m in match_records
        if m.get("minutes_played", 0) >= _CONSISTENCY_MIN_MINUTES
        and m.get("gameweek", 0) > cutoff
    ]
    qualifying.sort(key=lambda m: m["gameweek"])
    return qualifying[-size:]


def compute_cv_xgi(
    match_records: list[MatchRecord],
    current_gw: int,
    median_elo: float,
) -> float | None:
    """CV of Elo-adjusted xGI over rolling window.

    Returns None when fewer than 6 qualifying matches or mean xGI is zero.
    """
    import statistics

    window = _match_record_window(match_records, current_gw)
    if len(window) < _CONSISTENCY_MIN_MATCHES:
        return None

    adjusted_xgis = _elo_adjusted_xgis(window, median_elo)

    mean = statistics.mean(adjusted_xgis)
    if mean == 0:
        return None

    return statistics.stdev(adjusted_xgis) / mean


def compute_cv_xgi_fallback(
    history: list[dict[str, Any]], current_gw: int,
) -> float | None:
    """CV of raw xGI from FPL API history (no Elo adjustment).

    Fallback for players without Core-Insights match records.
    """
    import statistics

    qualifying = _qualifying_window(history, current_gw)
    if len(qualifying) < _CONSISTENCY_MIN_MATCHES:
        return None

    xgis = [
        float(h.get("expected_goals", 0) or 0)
        + float(h.get("expected_assists", 0) or 0)
        for h in qualifying
    ]

    mean = statistics.mean(xgis)
    if mean == 0:
        return None

    return statistics.stdev(xgis) / mean


def compute_blank_rate(
    history: list[dict[str, Any]], current_gw: int,
) -> float | None:
    """Fraction of qualifying matches with total_points <= 2 (appearance only).

    Always from FPL API history. Returns None when fewer than 6 qualifying GWs.
    """
    qualifying = _qualifying_window(history, current_gw)
    if len(qualifying) < _CONSISTENCY_MIN_MATCHES:
        return None

    blanks = sum(1 for h in qualifying if h.get("total_points", 0) <= 2)
    return blanks / len(qualifying)


def compute_floor_xgi(
    match_records: list[MatchRecord],
    current_gw: int,
    median_elo: float,
) -> float | None:
    """25th percentile of Elo-adjusted per-match xGI.

    Returns None when fewer than 6 qualifying matches.
    """
    window = _match_record_window(match_records, current_gw)
    if len(window) < _CONSISTENCY_MIN_MATCHES:
        return None

    adjusted_xgis = sorted(_elo_adjusted_xgis(window, median_elo))
    idx = (len(adjusted_xgis) - 1) * 0.25
    lower = int(idx)
    frac = idx - lower
    if lower + 1 < len(adjusted_xgis):
        return adjusted_xgis[lower] * (1 - frac) + adjusted_xgis[lower + 1] * frac
    return adjusted_xgis[lower]


def compute_floor_xgi_fallback(
    history: list[dict[str, Any]], current_gw: int,
) -> float | None:
    """25th percentile of raw xGI from FPL API history."""
    qualifying = _qualifying_window(history, current_gw)
    if len(qualifying) < _CONSISTENCY_MIN_MATCHES:
        return None

    xgis = sorted(
        float(h.get("expected_goals", 0) or 0)
        + float(h.get("expected_assists", 0) or 0)
        for h in qualifying
    )

    idx = (len(xgis) - 1) * 0.25
    lower = int(idx)
    frac = idx - lower
    if lower + 1 < len(xgis):
        return xgis[lower] * (1 - frac) + xgis[lower + 1] * frac
    return xgis[lower]


def compute_involvement_rate(
    match_records: list[MatchRecord] | None,
    history: list[dict[str, Any]],
    current_gw: int,
    position: str,
) -> float | None:
    """Position-specific involvement rate over rolling window.

    ATK (FWD/MID): Core-Insights only. Involved = shots >= 1 OR chances >= 1
        OR opposition box touches >= 3.
    DEF: Primary Core-Insights (CBIT + tackles >= 6), fallback FPL API.
    GK: Returns None (handled by compute_gk_consistency).
    """
    if position == "GK":
        return None

    if position in ATTACKING_POSITIONS:
        if not match_records:
            return None
        window = _match_record_window(match_records, current_gw)
        if len(window) < _CONSISTENCY_MIN_MATCHES:
            return None
        involved = sum(
            1
            for m in window
            if (
                m.get("total_shots", 0) >= 1
                or m.get("chances_created", 0) >= 1
                or m.get("touches_opposition_box", 0) >= 3
            )
        )
        return involved / len(window)

    # DEF
    if match_records:
        window = _match_record_window(match_records, current_gw)
        if len(window) >= _CONSISTENCY_MIN_MATCHES:
            involved = sum(
                1
                for m in window
                if (
                    m.get("clearances", 0)
                    + m.get("blocks", 0)
                    + m.get("interceptions", 0)
                    + m.get("tackles_won", 0)
                ) >= 6
            )
            return involved / len(window)

    # DEF fallback: FPL API history
    qualifying = _qualifying_window(history, current_gw)
    if len(qualifying) < _CONSISTENCY_MIN_MATCHES:
        return None

    involved = sum(
        1
        for h in qualifying
        if (
            int(h.get("clearances_blocks_interceptions", 0) or 0)
            + int(h.get("tackles", 0) or 0)
        ) >= 6
    )
    return involved / len(qualifying)


def compute_gk_consistency(
    match_records: list[MatchRecord],
    current_gw: int,
) -> float | None:
    """CV of saves per 90 from Core-Insights match data.

    Returns None when fewer than 6 qualifying matches.
    """
    import statistics

    window = _match_record_window(match_records, current_gw)
    if len(window) < _CONSISTENCY_MIN_MATCHES:
        return None

    saves_per_90: list[float] = []
    for m in window:
        minutes = m.get("minutes_played", 0)
        if minutes == 0:
            continue
        saves_per_90.append(m.get("saves", 0) / (minutes / 90))

    if len(saves_per_90) < _CONSISTENCY_MIN_MATCHES:
        return None

    mean = statistics.mean(saves_per_90)
    if mean == 0:
        return None

    return statistics.stdev(saves_per_90) / mean


_MIN_PERCENTILE_POOL = 15


def _assign_percentile_ranks(
    values: dict[int, float], *, invert: bool = False,
) -> dict[int, float]:
    """Convert raw values to percentile ranks with average-rank tie handling.

    When *invert* is True, lower raw values get higher percentiles
    (used for CV where low = consistent = good).
    """
    if len(values) < _MIN_PERCENTILE_POOL:
        return {pid: 0.5 for pid in values}

    sorted_items = sorted(values.items(), key=lambda x: x[1])
    n = len(sorted_items)

    # Assign ordinal ranks, then average ties
    ordinal: dict[int, int] = {}
    for rank, (pid, _) in enumerate(sorted_items):
        ordinal[pid] = rank

    # Group by value to handle ties
    by_value: dict[float, list[int]] = {}
    for pid, val in sorted_items:
        by_value.setdefault(val, []).append(pid)

    avg_ranks: dict[int, float] = {}
    for pids in by_value.values():
        avg = sum(ordinal[p] for p in pids) / len(pids)
        for p in pids:
            avg_ranks[p] = avg

    result: dict[int, float] = {}
    for pid in values:
        pct = avg_ranks[pid] / (n - 1) if n > 1 else 0.5
        result[pid] = (1.0 - pct) if invert else pct

    return result


def build_consistency_lookup(
    match_records: dict[int, list[MatchRecord]] | None,
    player_histories: dict[int, list[dict[str, Any]]] | None,
    positions: dict[int, str],
    current_gw: int,
    median_elo: float,
) -> dict[int, ConsistencySignals]:
    """Build per-player consistency signals with position-relative percentiles.

    Two-phase: (1) compute raw per-player values, (2) batch percentile
    conversion within position groups. Players with < 6 qualifying matches
    are excluded from the percentile pool and absent from the result
    (callers use NEUTRAL_SIGNALS default).
    """
    mr = match_records or {}
    ph = player_histories or {}
    all_pids = set(mr.keys()) | set(ph.keys())

    # Phase 1: raw per-player values
    raw_cvs: dict[int, float] = {}
    raw_floors: dict[int, float] = {}
    raw_blank_rates: dict[int, float] = {}
    raw_involvement: dict[int, float] = {}
    raw_gk_cv: dict[int, float] = {}

    for pid in all_pids:
        pos = positions.get(pid, "???")
        records = mr.get(pid)
        history = ph.get(pid, [])

        # CV-xGI: primary from match records, fallback from FPL API
        cv = None
        if records:
            cv = compute_cv_xgi(records, current_gw, median_elo)
        if cv is None and history:
            cv = compute_cv_xgi_fallback(history, current_gw)
        if cv is not None:
            raw_cvs[pid] = cv

        # Blank rate: always from FPL API history
        br = compute_blank_rate(history, current_gw) if history else None
        if br is not None:
            raw_blank_rates[pid] = br

        # Floor: primary from match records, fallback from FPL API
        floor = None
        if records:
            floor = compute_floor_xgi(records, current_gw, median_elo)
        if floor is None and history:
            floor = compute_floor_xgi_fallback(history, current_gw)
        if floor is not None:
            raw_floors[pid] = floor

        # Involvement rate
        inv = compute_involvement_rate(records, history, current_gw, pos)
        if inv is not None:
            raw_involvement[pid] = inv

        # GK consistency
        if pos == "GK" and records:
            gk_cv = compute_gk_consistency(records, current_gw)
            if gk_cv is not None:
                raw_gk_cv[pid] = gk_cv

    # Phase 2: percentile conversion by position group
    pos_groups: dict[str, dict[int, float]] = {}
    for pid, cv in raw_cvs.items():
        pos = positions.get(pid, "???")
        pos_groups.setdefault(pos, {})[pid] = cv

    cv_percentiles: dict[int, float] = {}
    for group in pos_groups.values():
        cv_percentiles.update(_assign_percentile_ranks(group, invert=True))

    # Floor percentiles by position (higher floor = higher percentile)
    floor_pos_groups: dict[str, dict[int, float]] = {}
    for pid, floor in raw_floors.items():
        pos = positions.get(pid, "???")
        floor_pos_groups.setdefault(pos, {})[pid] = floor

    floor_percentiles: dict[int, float] = {}
    for group in floor_pos_groups.values():
        floor_percentiles.update(_assign_percentile_ranks(group, invert=False))

    # GK consistency percentiles (single group)
    gk_percentiles = _assign_percentile_ranks(raw_gk_cv, invert=True)

    # Assemble ConsistencySignals per player
    result: dict[int, ConsistencySignals] = {}
    qualifying_pids = set(cv_percentiles.keys()) | set(raw_blank_rates.keys()) | set(
        floor_percentiles.keys()
    ) | set(raw_involvement.keys()) | set(gk_percentiles.keys())

    for pid in qualifying_pids:
        result[pid] = ConsistencySignals(
            cv_xgi_percentile=cv_percentiles.get(pid, 0.5),
            blank_rate=raw_blank_rates.get(pid),
            floor_percentile=floor_percentiles.get(pid, 0.5),
            involvement_rate=raw_involvement.get(pid),
            gk_consistency_percentile=gk_percentiles.get(pid, 0.5),
        )

    return result


def build_npxg_lookup_from_records(
    all_match_records: dict[int, list[MatchRecord]],
    current_gw: int,
) -> dict[int, float]:
    """Build adjusted npxG lookup from pre-fetched match records.

    Computes median Elo from the records, then delegates to
    ``build_adjusted_npxg_lookup``.
    """
    median_elo = compute_median_elo(all_match_records)
    return build_adjusted_npxg_lookup(all_match_records, current_gw, median_elo)


def compute_adjusted_npxg(
    match_records: list[MatchRecord],
    current_gw: int,
    median_elo: float,
) -> float | None:
    """Opponent-adjusted npxG/90 over a rolling 7-match/12-GW window.

    Normalises each match's xG by the opponent's Elo relative to the league
    median. Factor capped at [0.80, 1.25] to limit early-season noise.
    npxG per match = xg - (penalties_scored + penalties_missed) * 0.76.

    Returns None when fewer than 4 qualifying matches (same threshold as
    form_trajectory), triggering fallback to raw Understat npxG_per_90.
    """
    cutoff = current_gw - 12
    qualifying = [
        m for m in match_records
        if m.get("minutes_played", 0) > 0 and m.get("gameweek", 0) > cutoff
    ]
    qualifying.sort(key=lambda m: m["gameweek"])
    window = qualifying[-7:]

    if len(window) < 4:
        return None

    total_adjusted = 0.0
    total_minutes = 0

    for m in window:
        opp_elo = m.get("opponent_elo", median_elo)
        if opp_elo <= 0:
            opp_elo = median_elo
        factor = max(0.80, min(1.25, median_elo / opp_elo))

        xg = m.get("xg", 0.0)
        penalties = m.get("penalties_scored", 0) + m.get("penalties_missed", 0)
        npxg = xg - penalties * 0.76
        total_adjusted += npxg * factor
        total_minutes += m.get("minutes_played", 0)

    if total_minutes == 0:
        return None

    return total_adjusted / (total_minutes / 90)


def build_adjusted_npxg_lookup(
    all_match_records: dict[int, list[MatchRecord]],
    current_gw: int,
    median_elo: float,
) -> dict[int, float]:
    """Build per-player adjusted npxG/90 lookup from season match data.

    Returns dict keyed by FPL element_id. Players with insufficient data
    (< 4 qualifying matches) are absent; callers fall back to raw npxG_per_90.
    """
    result: dict[int, float] = {}
    for player_id, records in all_match_records.items():
        value = compute_adjusted_npxg(records, current_gw, median_elo)
        if value is not None:
            result[player_id] = value
    return result


def apply_adjusted_npxg(
    enrichment: dict[str, Any],
    player_id: int,
    lookup: dict[int, float] | None,
) -> None:
    """Override npxG_per_90 in enrichment with fixture-adjusted value.

    Always sets raw_npxG_per_90 from enrichment (callers must ensure
    npxG_per_90 is present before calling).
    Overrides npxG_per_90 with adjusted value when player_id is in lookup.
    """
    enrichment["raw_npxG_per_90"] = enrichment.get("npxG_per_90")
    if lookup and player_id in lookup:
        enrichment["npxG_per_90"] = lookup[player_id]


def apply_consistency(
    enrichment: dict[str, Any],
    player_id: int,
    lookup: dict[int, ConsistencySignals] | None,
) -> None:
    """Inject consistency signal fields into enrichment dict."""
    if not lookup:
        return
    signals = lookup.get(player_id)
    if signals is None:
        return
    enrichment["cv_xgi_percentile"] = signals.cv_xgi_percentile
    enrichment["blank_rate"] = signals.blank_rate
    enrichment["floor_percentile"] = signals.floor_percentile
    enrichment["involvement_rate"] = signals.involvement_rate
    enrichment["gk_consistency_percentile"] = signals.gk_consistency_percentile
