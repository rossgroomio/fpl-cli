#!/usr/bin/env python3
"""Backtest: does prior-blending fix early-season quality-score ranking?

Issue #143's design question, answered and shipped for the value family;
issue #206's, for the ownership families (`--family target|differential|
waiver`). Going into GW2 a family's quality baseline is pure observation of
one gameweek — form and ppg are the same single number, their caps saturate
on one good game — so one-game wonders out-read quiet-starting elites
(Emersonn 100 / Haaland 59, live 2026-08-25). The fix blends the observed raw
score with a prior-implied raw score under the production confidence curve:

    prior_raw = prior_strength * ceiling * CALIBRATION_ELITE_TARGET
    blended   = conf * observed_raw + (1 - conf) * prior_raw
    conf      = _compute_confidence(next_gw, prior_strength)   # production curve

where ``prior_strength`` is the player's previous-season pts/90 percentile
within position (price percentile * 0.5 for players without qualifying
history) — exactly the quantities ``fpl_cli.services.player_prior`` computes
in production — and ``ceiling`` is the family's calibrated anchor for the
position at that gameweek (the calendar-attainable one for a pre-GW6 keeper),
so the prior-implied score lives on the same raw scale the calibration
measured. For the value family that is the whole ceiling; for the ownership
families it is the ceiling without its matchup / ownership / position-need /
consistency headroom, because production blends the baseline before adding
those terms. The prior alone out-ranks the blend for keepers on rest-of-season
points at several early snapshots; a keeper-specific confidence discount was
evaluated for that and not shipped (it shifts the discounted share onto the
prior, unevenly against outfielders on the allocator's shared objective), so
the keeper improvement left on the table is a better keeper prior.

This script measures whether that blend ranks better than the incumbent — pure
observation for the value family, observation plus position-mean shrinkage for
the ownership ones — under the fpl-data-scientist skill's protocol:
walk-forward replay, rank metrics over RMSE, position-stratified, zero-minute
players filtered. It scores the blend through ``blend_quality_with_prior`` and
the shrinkage arm through ``shrink_scores``, so re-running it after a change to
either, to the confidence curve, or to the anchors measures what actually
ships.

Protocol
--------
- Replay completed-season snapshots (default 2025-26 after GW 1,2,3,4,7 —
  i.e. going into GW 2,3,4,5,8) using the calibration script's
  season-reconstruction machinery; pool = players with 45+ minutes at the
  snapshot and any rest-of-season minutes.
- Score every pool player through the real production scoring functions, for
  the family named by ``--family`` (default value), unblended and blended.
- Outcomes: total FPL points over the next 6 GWs and over the rest of the
  season. Metrics per position per snapshot: Spearman rank correlation and
  precision@5 (share of the signal's top 5 that finish in the outcome's
  top 5).
- Baselines: the unblended score, the shrunk score (the ownership families'
  incumbent — shrinkage is nearly but not quite order-preserving, since a
  strong prior shrinks a score less than a weak one, so it can reorder two
  close players), the prior alone (if this beats the blend, the confidence
  curve trusts early data too much), ppg at the snapshot, and vaastav's per-GW
  ``xP`` for the next gameweek (FPL's own prior-informed prediction — the
  ep_next equivalent available historically).

Fidelity caveats (shared by every arm, so comparisons stay fair)
----------------------------------------------------------------
- No Understat attachment: production at GW2 has current-season small-sample
  npxG; season-aggregate Understat would leak the future into a GW1
  snapshot, so every reconstructed player scores through the xGI-fallback
  path using cumulative FPL xG/xA instead.
- Players yet to appear by the snapshot are excluded — production shows no
  quality score for them either.
- ``xP`` predicts one gameweek; holding it against 6-GW and rest-of-season
  outcomes stretches it beyond its design, which flatters the other signals
  slightly.
- The ownership families are scored on their *quality baseline* only: the
  reconstruction has no team ratings, so no 3-GW matchup term, no ownership
  percentages and no squad to need a position filled. Those terms are pure
  observation in production and untouched by the blend, so this measures
  exactly the part of the score that changed. The waiver family does get its
  stricter combined minutes factor, which is part of its baseline.
- The shrinkage arm shrinks raw scores rather than the normalised ones
  production shrinks. Normalisation clamps at 100, which would tie the top of
  each pool together and flatter that arm's rank metrics; below the clamp the
  two are the same ranking. Nothing in the reconstruction is
  known-unavailable (every pool player has 45+ minutes and the replay carries
  no injury flags), so the hold-out set is empty.

Usage:
    python scripts/backtest_early_season_prior_blend.py
    python scripts/backtest_early_season_prior_blend.py --family waiver
    python scripts/backtest_early_season_prior_blend.py --snapshots 1,2,3 --json out.json
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import math
import sys
from pathlib import Path
from typing import Any, Literal, cast

sys.path.insert(0, str(Path(__file__).resolve().parent))

import calibrate_quality_ceilings as calib  # noqa: E402

from fpl_cli.services.player_prior import (  # noqa: E402
    CUTOFF_GW,
    MIN_MINUTES,
    PRICE_CONFIDENCE_FACTOR,
    PlayerPrior,
    # The production confidence curve itself: reimplementing it here would
    # let the backtest drift from what the shipped blend actually does.
    _compute_confidence,
    percentile_rank,
)
from fpl_cli.services.scoring import (  # noqa: E402
    CALIBRATION_ELITE_TARGET,
    MINS_FACTOR_START_GW,
    Position,
    _value_weights_and_ceiling,
    blend_quality_with_prior,
    calculate_player_quality_score,
    shrink_scores,
)
from fpl_cli.services.scoring.constants import _ownership_anchor_for  # noqa: E402

DEFAULT_SNAPSHOTS = (1, 2, 3, 4, 7)
POOL_MIN_MINUTES = 45
NEXT_WINDOW_GWS = 6
TOP_N = 5

FAMILIES = ("value", "target", "differential", "waiver")
SIGNALS = ("blended", "unblended", "shrunk", "prior_only", "ppg", "xp_next")
OUTCOMES = ("next6", "ros")


def family_anchor(family: str, position: Position, next_gw_id: int) -> float:
    """The raw-score anchor *family* places a prior-implied elite on at *next_gw_id*.

    The value family blends against its whole ceiling; the ownership families
    against the ceiling minus the bonus headroom, because production blends
    their baseline before the matchup, ownership, position-need and
    consistency terms are added. Both are the production selectors, so a
    recalibration moves this with them.
    """
    if family == "value":
        return _value_weights_and_ceiling(position, next_gw_id=next_gw_id)[1]
    return _ownership_anchor_for(
        cast('Literal["target", "differential", "waiver"]', family),
        position,
        next_gw_id=next_gw_id,
    )


def family_mins_factor(
    family: str, minutes: int, appearances: int, mins_factor: float, next_gw_id: int,
) -> float:
    """The minutes factor *family* scales its per-90 terms by.

    Every family but waiver uses ``calculate_mins_factor``, already computed by
    ``snapshot_quality_inputs`` and passed in as *mins_factor*. Waiver
    multiplies it by an absolute-playing-time term (``calculate_waiver_score``)
    because a draft claim is a season commitment; that is part of the baseline
    the blend adjusts, so the backtest reproduces it.
    """
    if family != "waiver":
        return mins_factor
    if next_gw_id <= MINS_FACTOR_START_GW:
        return 1.0
    if appearances <= 0:
        return 0.0
    return min(minutes / 450, 1.0) * mins_factor


# ---------------------------------------------------------------------------
# Prior construction (previous-season pts/90 percentile, price fallback)
# ---------------------------------------------------------------------------


def load_player_codes(season: str) -> dict[int, int]:
    """element id -> stable cross-season code from players_raw.csv."""
    text = calib._fetch_text(
        f"{calib.VAASTAV_BASE}/{season}/players_raw.csv",
        f"players_raw-{season}.csv",
    )
    return {
        calib._to_int(record.get("id")): calib._to_int(record.get("code"))
        for record in csv.DictReader(io.StringIO(text))
    }


def load_player_costs(season: str) -> dict[int, int]:
    """element id -> now_cost (season-end bootstrap dump; price prior fallback)."""
    text = calib._fetch_text(
        f"{calib.VAASTAV_BASE}/{season}/players_raw.csv",
        f"players_raw-{season}.csv",
    )
    return {
        calib._to_int(record.get("id")): calib._to_int(record.get("now_cost"))
        for record in csv.DictReader(io.StringIO(text))
    }


def load_prev_season_pts90(season: str) -> dict[int, tuple[Position, float]]:
    """Previous season's code -> (position, pts/90) for players with 450+ minutes.

    Aggregated from merged_gw.csv (which lacks defensive_contribution before
    2025-26, so ``calib.load_season``'s schema guard cannot be reused here —
    only minutes, points and position are needed).
    """
    text = calib._fetch_text(
        f"{calib.VAASTAV_BASE}/{season}/gws/merged_gw.csv",
        f"merged_gw-{season}.csv",
    )
    codes = load_player_codes(season)
    minutes: dict[int, int] = {}
    points: dict[int, int] = {}
    positions: dict[int, Position] = {}
    seen: set[tuple[int, int, int]] = set()
    for record in csv.DictReader(io.StringIO(text)):
        element = calib._to_int(record.get("element"))
        if element <= 0:
            continue
        row_key = (
            element,
            calib._to_int(record.get("round") or record.get("GW")),
            calib._to_int(record.get("fixture")),
        )
        if row_key in seen:
            continue
        seen.add(row_key)
        position = calib._POSITION_ALIASES.get(
            (record.get("position") or "").strip().upper()
        )
        if position is not None:
            positions[element] = position  # type: ignore[assignment]
        minutes[element] = minutes.get(element, 0) + calib._to_int(record.get("minutes"))
        points[element] = points.get(element, 0) + calib._to_int(record.get("total_points"))

    result: dict[int, tuple[Position, float]] = {}
    for element, mins in minutes.items():
        code = codes.get(element, 0)
        position = positions.get(element)
        if code <= 0 or position is None or mins < MIN_MINUTES:
            continue
        result[code] = (position, points[element] / mins * 90)
    return result


def build_prior_strengths(
    players: list[Any],
    season: str,
    prev_season: str,
) -> tuple[dict[int, float], float]:
    """element id -> prior_strength, mirroring generate_player_prior's rules.

    History source: previous-season pts/90 percentile within position.
    Price source (no qualifying history): price percentile * 0.5.

    Also returns the share of players whose prior came from history — counted
    by source, not by comparing the strength against a threshold: history
    strengths are percentiles in [0, 1] while PRICE_CONFIDENCE_FACTOR is a
    confidence weight, so a value comparison would misclassify every
    below-median history player as a price fallback.
    """
    codes = load_player_codes(season)
    costs = load_player_costs(season)
    prev = load_prev_season_pts90(prev_season)

    prev_by_position: dict[Position, list[float]] = {"GK": [], "DEF": [], "MID": [], "FWD": []}
    for position, pts90 in prev.values():
        prev_by_position[position].append(pts90)
    cost_by_position: dict[Position, list[float]] = {"GK": [], "DEF": [], "MID": [], "FWD": []}
    for player in players:
        cost_by_position[player.position].append(float(costs.get(player.element, 0)))

    strengths: dict[int, float] = {}
    history_sourced = 0
    for player in players:
        code = codes.get(player.element, 0)
        history = prev.get(code)
        if history is not None and history[0] == player.position:
            strengths[player.element] = percentile_rank(
                history[1], prev_by_position[player.position]
            )
            history_sourced += 1
        else:
            price_pct = percentile_rank(
                float(costs.get(player.element, 0)),
                cost_by_position[player.position],
            )
            strengths[player.element] = price_pct * PRICE_CONFIDENCE_FACTOR
    history_share = history_sourced / len(players) if players else 0.0
    return strengths, history_share


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------


def _average_ranks(values: list[float]) -> list[float]:
    order = sorted(range(len(values)), key=lambda i: values[i])
    ranks = [0.0] * len(values)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and values[order[j + 1]] == values[order[i]]:
            j += 1
        rank = (i + j) / 2 + 1
        for k in range(i, j + 1):
            ranks[order[k]] = rank
        i = j + 1
    return ranks


def spearman(xs: list[float], ys: list[float]) -> float | None:
    """Spearman's rho via Pearson on average ranks (tie-safe, no scipy)."""
    if len(xs) < 3:
        return None
    rx, ry = _average_ranks(xs), _average_ranks(ys)
    mx = sum(rx) / len(rx)
    my = sum(ry) / len(ry)
    cov = sum((a - mx) * (b - my) for a, b in zip(rx, ry))
    vx = math.sqrt(sum((a - mx) ** 2 for a in rx))
    vy = math.sqrt(sum((b - my) ** 2 for b in ry))
    if vx == 0 or vy == 0:
        return None
    return cov / (vx * vy)


def precision_at(n: int, scores: list[float], outcomes: list[float]) -> float | None:
    """Share of the signal's top *n* that land in the outcome's top *n*."""
    if len(scores) < n:
        return None
    top_by_score = set(sorted(range(len(scores)), key=lambda i: -scores[i])[:n])
    top_by_outcome = set(sorted(range(len(outcomes)), key=lambda i: -outcomes[i])[:n])
    return len(top_by_score & top_by_outcome) / n


# ---------------------------------------------------------------------------
# Snapshot scoring
# ---------------------------------------------------------------------------


def score_pool(
    players: list[Any],
    strengths: dict[int, float],
    gw: int,
    family: str,
) -> dict[Position, list[dict[str, float]]]:
    """Signals and outcomes per position for the snapshot after *gw*."""
    kickoffs = [
        r.kickoff for p in players for r in p.rows if r.round <= gw and r.kickoff is not None
    ]
    snapshot_date = max(kickoffs) if kickoffs else None
    next_gw_id = gw + 1
    weights = calib.FAMILY_WEIGHTS[family]

    pool: dict[Position, list[dict[str, float]]] = {"GK": [], "DEF": [], "MID": [], "FWD": []}
    priors: dict[Position, dict[int, PlayerPrior]] = {"GK": {}, "DEF": {}, "MID": {}, "FWD": {}}
    for player in players:
        inputs = calib.snapshot_quality_inputs(
            player, gw, snapshot_date, min_minutes=POOL_MIN_MINUTES
        )
        if inputs is None:
            continue
        future_rows = [r for r in player.rows if r.round > gw]
        if sum(r.minutes for r in future_rows) <= 0:
            continue
        quality, mins_factor = inputs
        rows = [r for r in player.rows if r.round <= gw]
        raw = calculate_player_quality_score(
            quality,
            calib._effective_weights(weights, player.position),
            family_mins_factor(
                family,
                sum(r.minutes for r in rows),
                sum(1 for r in rows if r.minutes > 0),
                mins_factor,
                next_gw_id,
            ),
            position=player.position,
        )
        strength = strengths.get(player.element, 0.0)
        prior = PlayerPrior(
            prior_strength=strength,
            confidence=_compute_confidence(next_gw_id, strength),
            source="history",
        )
        anchor = family_anchor(family, player.position, next_gw_id)
        priors[player.position][player.element] = prior
        pool[player.position].append({
            "element": float(player.element),
            "blended": blend_quality_with_prior(
                raw, prior, ceiling=anchor, next_gw_id=next_gw_id,
            ),
            "unblended": raw,
            "shrunk": raw,  # replaced below, once the position pool is complete
            "prior_only": strength * anchor * CALIBRATION_ELITE_TARGET,
            "ppg": float(quality["ppg"]),
            "xp_next": sum(r.xp for r in player.rows if r.round == next_gw_id),
            "next6": float(
                sum(r.total_points for r in future_rows if r.round <= gw + NEXT_WINDOW_GWS)
            ),
            "ros": float(sum(r.total_points for r in future_rows)),
        })

    # The incumbent arm: position-mean shrinkage, which needs the whole
    # position pool. Nothing in the replay is known-unavailable, so the
    # hold-out set is empty (see the module docstring).
    for position, rows_out in pool.items():
        if not rows_out:
            continue
        shrunk = shrink_scores(
            [(int(r["element"]), r["unblended"], position) for r in rows_out],
            priors[position], next_gw_id, CUTOFF_GW, unavailable_ids=(),
        )
        for row, (_, adjusted, _) in zip(rows_out, shrunk):
            row["shrunk"] = adjusted
    return pool


def evaluate(
    players: list[Any],
    strengths: dict[int, float],
    snapshots: tuple[int, ...],
    family: str,
) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for gw in snapshots:
        pool = score_pool(players, strengths, gw, family)
        for position, rows in pool.items():
            if not rows:
                continue
            entry: dict[str, Any] = {
                "snapshot_gw": gw,
                "into_gw": gw + 1,
                "position": position,
                "pool": len(rows),
            }
            for outcome in OUTCOMES:
                outcome_values = [r[outcome] for r in rows]
                for signal in SIGNALS:
                    signal_values = [r[signal] for r in rows]
                    rho = spearman(signal_values, outcome_values)
                    entry[f"rho_{signal}_{outcome}"] = (
                        round(rho, 3) if rho is not None else None
                    )
                    if outcome == "ros":
                        p_at = precision_at(TOP_N, signal_values, outcome_values)
                        entry[f"p{TOP_N}_{signal}"] = (
                            round(p_at, 2) if p_at is not None else None
                        )
            results.append(entry)
    return results


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------


def print_report(
    results: list[dict[str, Any]], snapshots: tuple[int, ...], family: str,
) -> None:
    print(f"Early-season prior-blend backtest — {family}-family quality baseline")
    print(f"signals: {', '.join(SIGNALS)}; outcomes: next {NEXT_WINDOW_GWS} GWs, rest of season")
    for gw in snapshots:
        rows = [r for r in results if r["snapshot_gw"] == gw]
        if not rows:
            continue
        print(f"\n=== snapshot after GW{gw} (going into GW{gw + 1}) ===")
        header = (
            f"{'pos':<4} {'pool':>4} | "
            + " ".join(f"ρ6:{s[:7]:<7}" for s in SIGNALS)
            + " | "
            + " ".join(f"ρros:{s[:7]:<7}" for s in SIGNALS)
            + " | "
            + " ".join(f"P{TOP_N}:{s[:7]:<7}" for s in SIGNALS)
        )
        print(header)
        for row in rows:
            def _fmt(value: float | None, width: int) -> str:
                return f"{value:>{width}.3f}" if value is not None else " " * (width - 1) + "-"

            line = (
                f"{row['position']:<4} {row['pool']:>4} | "
                + " ".join(_fmt(row[f"rho_{s}_next6"], 10) for s in SIGNALS)
                + " | "
                + " ".join(_fmt(row[f"rho_{s}_ros"], 12) for s in SIGNALS)
                + " | "
                + " ".join(_fmt(row[f"p{TOP_N}_{s}"], 10) for s in SIGNALS)
            )
            print(line)

    for incumbent in ("unblended", "shrunk"):
        print(
            f"\n=== summary: mean Spearman delta (blended - {incumbent}) across snapshots ==="
        )
        for position in ("GK", "DEF", "MID", "FWD"):
            rows = [r for r in results if r["position"] == position]
            for outcome in OUTCOMES:
                deltas = [
                    r[f"rho_blended_{outcome}"] - r[f"rho_{incumbent}_{outcome}"]
                    for r in rows
                    if r[f"rho_blended_{outcome}"] is not None
                    and r[f"rho_{incumbent}_{outcome}"] is not None
                ]
                if deltas:
                    mean_delta = sum(deltas) / len(deltas)
                    wins = sum(1 for d in deltas if d > 0)
                    print(
                        f"{position:<4} {outcome:<6} Δρ={mean_delta:+.3f} "
                        f"(blended better in {wins}/{len(deltas)} snapshots)"
                    )


def main() -> None:
    parser = argparse.ArgumentParser(description=(__doc__ or "").split("\n")[0])
    parser.add_argument("--season", default="2025-26", help="Completed season to replay")
    parser.add_argument(
        "--prev-season", default="2024-25", help="Season supplying the priors"
    )
    parser.add_argument(
        "--snapshots",
        default=",".join(str(s) for s in DEFAULT_SNAPSHOTS),
        help="Comma-separated snapshot GWs (score as of the END of each)",
    )
    parser.add_argument(
        "--family",
        default="value",
        choices=FAMILIES,
        help="Scoring family whose quality baseline to replay (default: value)",
    )
    parser.add_argument("--json", type=Path, default=None, help="Write results JSON here")
    args = parser.parse_args()

    snapshots = tuple(int(s) for s in args.snapshots.split(",") if s.strip())
    players = calib.load_season(args.season)
    strengths, history_share = build_prior_strengths(players, args.season, args.prev_season)
    print(
        f"{args.season}: {len(players)} players; priors from {args.prev_season} "
        f"({history_share:.0%} history-sourced, rest price fallback)"
    )

    results = evaluate(players, strengths, snapshots, args.family)
    print_report(results, snapshots, args.family)

    if args.json is not None:
        args.json.write_text(json.dumps(results, indent=2), encoding="utf-8")
        print(f"\nresults written to {args.json}")


if __name__ == "__main__":
    main()
