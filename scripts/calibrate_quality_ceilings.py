#!/usr/bin/env python3
"""Calibrate the ownership/value quality ceilings against a completed season.

The 0-100 quality scores normalise a raw weighted score against a ceiling.
A ceiling derived from the theoretical weight-cap sum assumes every signal
saturates, which holds for DEF (form, ppg and dc/90 caps are genuinely
reached) but not for MID/FWD/GK: nobody sustains the npxG/90 >= 0.8 the
npxg cap needs, and no keeper posts the 100% clean-sheet rate the cs_rate
cap needs. The result is issue #88 — elite DEFs read ~89 while elite MIDs
read ~60 on the same 0-100 surface.

This script replaces assumption with measurement. It rebuilds the exact
quality-dict inputs the production scorer consumes — from vaastav's per-GW
merged_gw.csv (form, ppg, minutes, DC, GK stats) and Understat's league
endpoint (npxG/90, xGChain/90, penalty xG/90) — at several gameweek
snapshots of a completed season, scores every player through the real
``calculate_player_quality_score`` with the real family weights, and
anchors each (family, position) ceiling to the observed elite raw score so
the best players land ~90/100.

Faithfulness notes (all deliberate):
- Scoring is the production code path: ``calculate_player_quality_score``,
  ``calculate_mins_factor``, ``compute_form_trajectory`` and
  ``compute_xgi_sustainability`` are imported, never re-implemented — and so
  is the Understat join (``match_fpl_to_understat``, fed the web_name it was
  designed around).
- Understat per-90 rates are season-long aggregates applied to every
  snapshot (Understat's league endpoint has no as-of-GW view). Attack-rate
  distributions are stable enough across a season for a p95-style anchor.
- The Core-Insights Elo adjustment on npxG (clamped [0.80, 1.25]) is not
  replayed; it recentres individual rates, not the pool's upper tail.
- Players without an Understat name match take the xGI fallback branch,
  exactly as unmatched players do in production.

Ceiling structure downstream (unchanged by this script): the ownership
families add matchup / ownership / position-need / consistency headroom on
top of the quality anchor; the VALUE family is the anchor alone. This
script calibrates the quality anchors only.

Usage:
    python3 scripts/calibrate_quality_ceilings.py                # report
    python3 scripts/calibrate_quality_ceilings.py --write        # + update constants.py
    python3 scripts/calibrate_quality_ceilings.py --season 2024-25 --snapshots 19,29,38

Data-source choice (see issue #101 for the runtime-provider version of
this trade-off): vaastav over Core-Insights because calibration needs
per-fixture rows with kickoff times (the 30-day form window and the
trajectory/sustainability signals) in one fetch, and seasons before
2025-26 exist only in vaastav. #101's timeliness concern — a volunteer
archive can lag a just-finished season — is handled by refusing to
calibrate on a season that is missing gameweeks rather than by switching
source.

Network: vaastav GitHub raw + understat.com, cached under the fpl-cli
cache dir (``datasets/ceiling-calibration/``) so re-runs are offline.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import io
import json
import re
import statistics
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import httpx

from fpl_cli.paths import user_cache_dir
from fpl_cli.season import get_season_year, season_label
from fpl_cli.services.scoring.constants import (
    DIFFERENTIAL_QUALITY_WEIGHTS,
    TARGET_QUALITY_WEIGHTS,
    VALUE_QUALITY_WEIGHTS,
    WAIVER_QUALITY_WEIGHTS,
    Position,
    QualityWeights,
    scoring_weights_fingerprint,
)
from fpl_cli.services.scoring.signals import (
    compute_form_trajectory,
    compute_xgi_sustainability,
)
from fpl_cli.services.scoring.value_quality import (
    calculate_mins_factor,
    calculate_player_quality_score,
)

VAASTAV_BASE = "https://raw.githubusercontent.com/vaastav/Fantasy-Premier-League/master/data"

FAMILY_WEIGHTS: dict[str, QualityWeights] = {
    "target": TARGET_QUALITY_WEIGHTS,
    "differential": DIFFERENTIAL_QUALITY_WEIGHTS,
    "waiver": WAIVER_QUALITY_WEIGHTS,
    "value": VALUE_QUALITY_WEIGHTS,
}
FAMILIES = tuple(FAMILY_WEIGHTS)
POSITIONS: tuple[Position, ...] = ("GK", "DEF", "MID", "FWD")

# Elite anchor: the top player of a (family, position, snapshot) pool should
# normalise to ~ELITE_TARGET. The anchor is the median across snapshots of
# top_raw / ELITE_TARGET — median so one freak half-season cannot deflate
# everyone else for a whole calibration cycle.
ELITE_TARGET = 0.92

_POSITION_ALIASES = {"GK": "GK", "GKP": "GK", "DEF": "DEF", "MID": "MID", "FWD": "FWD"}

# FPL's `form` is average points per match over the trailing 30 days.
_FORM_WINDOW = timedelta(days=30)

# understat.TEAM_NAME_MAP tracks the CURRENT season's 20 clubs; a club present
# in a past calibration season but since relegated falls through the map on
# its identity, which only works where the FPL and Understat names agree
# ("Burnley", "West Ham"). These are the ones that don't.
_LEGACY_UNDERSTAT_TEAMS = {
    "Wolves": "Wolverhampton Wanderers",
    "Sheffield Utd": "Sheffield United",
    "Leicester": "Leicester",
}


# ---------------------------------------------------------------------------
# Data acquisition
# ---------------------------------------------------------------------------


def _cache_dir() -> Path:
    path = user_cache_dir() / "datasets" / "ceiling-calibration"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _fetch_text(url: str, cache_name: str) -> str:
    cache_file = _cache_dir() / cache_name
    if cache_file.exists():
        return cache_file.read_text(encoding="utf-8")
    response = httpx.get(url, timeout=120.0, follow_redirects=True)
    response.raise_for_status()
    cache_file.write_text(response.text, encoding="utf-8")
    return response.text


def _fetch_understat_players(season_start_year: int) -> list[dict[str, Any]]:
    """Season-aggregate Understat player list via the production client."""
    cache_file = _cache_dir() / f"understat-{season_start_year}.json"
    if cache_file.exists():
        return json.loads(cache_file.read_text(encoding="utf-8"))

    from fpl_cli.api.understat import UnderstatClient

    async def _run() -> list[dict[str, Any]]:
        async with UnderstatClient(season_year=season_start_year) as client:
            return await client.get_league_players()

    players = asyncio.run(_run())
    cache_file.write_text(json.dumps(players), encoding="utf-8")
    return players


@dataclass
class GwRow:
    round: int
    kickoff: datetime | None
    minutes: int
    total_points: int
    goals_scored: int
    assists: int
    expected_goals: float
    expected_assists: float
    expected_goals_conceded: float
    saves: int
    clean_sheets: int
    defensive_contribution: float


@dataclass
class SeasonPlayer:
    element: int
    name: str
    position: Position
    team: str = ""
    rows: list[GwRow] = field(default_factory=list)
    understat: dict[str, Any] | None = None


def _to_int(value: str | None) -> int:
    try:
        return int(float(value)) if value not in (None, "") else 0
    except ValueError:
        return 0


def _to_float(value: str | None) -> float:
    try:
        return float(value) if value not in (None, "") else 0.0
    except ValueError:
        return 0.0


def _parse_kickoff(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def load_season(season: str) -> list[SeasonPlayer]:
    """Parse merged_gw.csv into per-player GW rows for the four real positions."""
    text = _fetch_text(
        f"{VAASTAV_BASE}/{season}/gws/merged_gw.csv",
        f"merged_gw-{season}.csv",
    )
    players: dict[int, SeasonPlayer] = {}
    for record in csv.DictReader(io.StringIO(text)):
        position = _POSITION_ALIASES.get((record.get("position") or "").strip().upper())
        if position is None:
            continue
        element = _to_int(record.get("element"))
        if element <= 0:
            continue
        player = players.get(element)
        if player is None:
            player = SeasonPlayer(
                element=element,
                name=(record.get("name") or "").strip(),
                position=position,  # type: ignore[arg-type]
            )
            players[element] = player
        # Rows arrive in gameweek order, so this settles on the club the
        # player finished the season at — the one their Understat season
        # aggregate is filed under for non-movers.
        player.team = (record.get("team") or player.team).strip()
        player.rows.append(
            GwRow(
                round=_to_int(record.get("round") or record.get("GW")),
                kickoff=_parse_kickoff(record.get("kickoff_time")),
                minutes=_to_int(record.get("minutes")),
                total_points=_to_int(record.get("total_points")),
                goals_scored=_to_int(record.get("goals_scored")),
                assists=_to_int(record.get("assists")),
                expected_goals=_to_float(record.get("expected_goals")),
                expected_assists=_to_float(record.get("expected_assists")),
                expected_goals_conceded=_to_float(record.get("expected_goals_conceded")),
                saves=_to_int(record.get("saves")),
                clean_sheets=_to_int(record.get("clean_sheets")),
                defensive_contribution=_to_float(record.get("defensive_contribution")),
            )
        )
    for player in players.values():
        player.rows.sort(key=lambda r: r.round)
    return list(players.values())


def load_web_names(season: str) -> dict[int, str]:
    """element id -> FPL web_name from players_raw.csv (bootstrap dump)."""
    text = _fetch_text(
        f"{VAASTAV_BASE}/{season}/players_raw.csv",
        f"players_raw-{season}.csv",
    )
    return {
        _to_int(record.get("id")): (record.get("web_name") or "").strip()
        for record in csv.DictReader(io.StringIO(text))
    }


def attach_understat(
    players: list[SeasonPlayer],
    understat: list[dict[str, Any]],
    web_names: dict[int, str],
) -> float:
    """Join Understat season aggregates via the production matcher.

    Reuses ``match_fpl_to_understat`` — the exact join production runs —
    feeding it the FPL web_name (its designed input: the prefix rule matches
    "B.Fernandes" to "Bruno Fernandes", where vaastav's full legal names —
    "Bruno Borges Fernandes" — defeat it) and the player's end-of-season
    club, pre-translated for clubs the current-season TEAM_NAME_MAP no
    longer carries. Returns the matched share of players with 300+ minutes,
    so the report can flag a degraded join.
    """
    from fpl_cli.api.understat import match_fpl_to_understat

    matched = 0
    eligible = 0
    for player in players:
        total_minutes = sum(r.minutes for r in player.rows)
        player.understat = match_fpl_to_understat(
            web_names.get(player.element) or player.name,
            _LEGACY_UNDERSTAT_TEAMS.get(player.team, player.team),
            understat,
            fpl_position=player.position,
            fpl_minutes=total_minutes,
        )
        if total_minutes >= 300:
            eligible += 1
            if player.understat is not None:
                matched += 1
    return matched / eligible if eligible else 0.0


# ---------------------------------------------------------------------------
# Snapshot reconstruction
# ---------------------------------------------------------------------------


def _effective_weights(weights: QualityWeights, position: Position) -> QualityWeights:
    """Position variant selection, mirroring _calculate_quality_based_raw."""
    if position == "GK":
        return weights.for_gk()
    if position == "DEF":
        return weights.without_xgi()
    return weights


def snapshot_quality_inputs(
    player: SeasonPlayer, gw: int, snapshot_date: datetime | None
) -> tuple[dict[str, Any], float] | None:
    """Rebuild (quality_dict, mins_factor) as of the end of *gw*.

    Returns None for players below 300 season minutes at the snapshot —
    matching the pool the issue's validation criteria are stated over.
    """
    rows = [r for r in player.rows if r.round <= gw]
    minutes = sum(r.minutes for r in rows)
    if minutes < 300:
        return None
    played = [r for r in rows if r.minutes > 0]
    appearances = len(played)
    if appearances == 0:
        return None
    total_points = sum(r.total_points for r in rows)
    ppg = total_points / appearances

    if snapshot_date is not None:
        window = [
            r for r in rows if r.kickoff is not None and r.kickoff >= snapshot_date - _FORM_WINDOW
        ]
    else:
        window = [r for r in rows if r.round > gw - 4]
    form = sum(r.total_points for r in window) / len(window) if window else 0.0

    next_gw_id = gw + 1
    history = [
        {
            "round": r.round,
            "minutes": r.minutes,
            "total_points": r.total_points,
            "goals_scored": r.goals_scored,
            "assists": r.assists,
            "expected_goals": r.expected_goals,
            "expected_assists": r.expected_assists,
        }
        for r in rows
    ]
    form_trajectory = compute_form_trajectory(history, next_gw_id)
    xgi_sustainability, _ = compute_xgi_sustainability(history, next_gw_id, player.position)

    xg = sum(r.expected_goals for r in rows)
    xa = sum(r.expected_assists for r in rows)
    quality: dict[str, Any] = {
        "xGI_per_90": (xg + xa) / minutes * 90,
        "form": form,
        "ppg": ppg,
        "dc_per_90": sum(r.defensive_contribution for r in rows) / minutes * 90,
        "form_trajectory": form_trajectory,
        "xgi_sustainability": xgi_sustainability,
    }
    if player.understat is not None:
        quality["npxG_per_90"] = float(player.understat.get("npxG_per_90", 0) or 0)
        quality["xGChain_per_90"] = float(player.understat.get("xGChain_per_90", 0) or 0)
        quality["penalty_xG_per_90"] = float(player.understat.get("penalty_xG_per_90", 0) or 0)

    if player.position == "GK":
        ramp = min(minutes / 450, 1.0)
        xgc_per_90 = sum(r.expected_goals_conceded for r in rows) / minutes * 90
        quality["gk_saves_per_90"] = sum(r.saves for r in rows) / minutes * 90 * ramp
        quality["gk_xgc_quality"] = max(0.0, 2.0 - xgc_per_90) * ramp
        quality["gk_cs_rate"] = sum(r.clean_sheets for r in rows) / appearances * ramp

    mins_factor = calculate_mins_factor(minutes, appearances, next_gw_id)
    return quality, mins_factor


def score_snapshot(
    players: list[SeasonPlayer], gw: int
) -> dict[tuple[str, Position], list[float]]:
    """Raw quality per (family, position) for every pool player at *gw*."""
    kickoffs = [
        r.kickoff for p in players for r in p.rows if r.round <= gw and r.kickoff is not None
    ]
    snapshot_date = max(kickoffs) if kickoffs else None

    raws: dict[tuple[str, Position], list[float]] = {
        (family, position): [] for family in FAMILIES for position in POSITIONS
    }
    for player in players:
        inputs = snapshot_quality_inputs(player, gw, snapshot_date)
        if inputs is None:
            continue
        quality, mins_factor = inputs
        for family, weights in FAMILY_WEIGHTS.items():
            raw = calculate_player_quality_score(
                quality,
                _effective_weights(weights, player.position),
                mins_factor,
                position=player.position,
            )
            raws[(family, player.position)].append(raw)
    return raws


# ---------------------------------------------------------------------------
# Anchoring and reporting
# ---------------------------------------------------------------------------


def _percentile(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, round(pct / 100 * (len(ordered) - 1))))
    return ordered[index]


def _band_counts(scores: list[int]) -> str:
    buckets = [0] * 10
    for score in scores:
        buckets[min(9, score // 10)] += 1
    return " ".join(f"{count:>3d}" for count in buckets)


def compute_anchors(
    snapshots: dict[int, dict[tuple[str, Position], list[float]]],
) -> dict[tuple[str, Position], float]:
    """Median over snapshots of top_raw / ELITE_TARGET per (family, position)."""
    anchors: dict[tuple[str, Position], float] = {}
    for family in FAMILIES:
        for position in POSITIONS:
            per_snapshot = [
                max(raws) / ELITE_TARGET
                for by_key in snapshots.values()
                if (raws := by_key[(family, position)])
            ]
            anchors[(family, position)] = (
                round(statistics.median(per_snapshot), 2) if per_snapshot else 0.0
            )
    return anchors


def print_report(
    season: str,
    snapshots: dict[int, dict[tuple[str, Position], list[float]]],
    anchors: dict[tuple[str, Position], float],
    match_rate: float,
) -> None:
    print(f"Season {season} — Understat match rate (300+ min pool): {match_rate:.0%}")
    print(f"Snapshots: {', '.join(f'GW{gw}' for gw in snapshots)}; pool: 300+ minutes")
    print()
    for family in FAMILIES:
        print(f"=== {family.upper()} family — raw quality distribution ===")
        header = (
            f"{'pos':<4} {'n(38)':>6} {'p5':>7} {'p50':>7} {'p95':>7} {'max':>7}"
            f" {'anchor':>7}   0-9 ... 90-100 (final snapshot, scores vs anchor)"
        )
        print(header)
        final_gw = max(snapshots)
        for position in POSITIONS:
            final_raws = snapshots[final_gw][(family, position)]
            pooled = [
                value for by_key in snapshots.values() for value in by_key[(family, position)]
            ]
            anchor = anchors[(family, position)]
            scores = (
                [max(0, min(round(raw / anchor * 100), 100)) for raw in final_raws]
                if anchor
                else []
            )
            print(
                f"{position:<4} {len(final_raws):>6d} {_percentile(pooled, 5):>7.2f}"
                f" {_percentile(pooled, 50):>7.2f} {_percentile(pooled, 95):>7.2f}"
                f" {max(pooled) if pooled else 0.0:>7.2f} {anchor:>7.2f}   {_band_counts(scores)}"
            )
            if scores:
                ordered = sorted(scores, reverse=True)
                top5 = ordered[:5]
                bottom5 = ordered[-5:]
                median = statistics.median(scores)
                print(
                    f"     top-5 {top5} | median {median:.0f} | bottom-5 {bottom5}"
                )
        print()


# ---------------------------------------------------------------------------
# Constants writing
# ---------------------------------------------------------------------------

_BEGIN_MARK = "# --- BEGIN calibrated quality ceilings (generated) ---"
_END_MARK = "# --- END calibrated quality ceilings (generated) ---"


def write_constants(
    anchors: dict[tuple[str, Position], float],
    season: str,
    snapshot_gws: list[int],
    constants_path: Path,
) -> None:
    """Rewrite the generated anchor block in scoring/constants.py in place."""
    lines = [
        _BEGIN_MARK,
        f"# Calibrated by scripts/calibrate_quality_ceilings.py against {season}",
        f"# (snapshots GW{', GW'.join(str(g) for g in snapshot_gws)}; pool 300+ minutes;",
        f"# elite anchor top_raw/{ELITE_TARGET}; run {datetime.now().date().isoformat()}).",
        "# Do not hand-edit: re-run the script with --write after any weight change.",
        "QUALITY_CEILINGS: dict[tuple[str, Position], float] = {",
    ]
    for family in FAMILIES:
        for position in POSITIONS:
            lines.append(
                f'    ("{family}", "{position}"): {anchors[(family, position)]:.2f},'
            )
    lines.append("}")
    lines.append(
        f'CALIBRATION_FINGERPRINT = "{scoring_weights_fingerprint()}"'
    )
    lines.append(_END_MARK)
    block = "\n".join(lines)

    source = constants_path.read_text(encoding="utf-8")
    pattern = re.compile(
        re.escape(_BEGIN_MARK) + r".*?" + re.escape(_END_MARK), re.DOTALL
    )
    if not pattern.search(source):
        raise SystemExit(
            f"Marker block not found in {constants_path} — expected "
            f"'{_BEGIN_MARK}' ... '{_END_MARK}'"
        )
    constants_path.write_text(pattern.sub(lambda _: block, source, count=1), encoding="utf-8")
    print(f"Wrote calibrated ceilings to {constants_path}")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(description=(__doc__ or "").split("\n", 1)[0])
    parser.add_argument(
        "--season",
        default=season_label(get_season_year() - 1),
        help="Completed season to calibrate against (default: last season)",
    )
    parser.add_argument(
        "--snapshots",
        default="10,15,19,24,29,34,38",
        help="Comma-separated gameweek snapshots",
    )
    parser.add_argument("--json", type=Path, help="Also dump anchors + stats to this file")
    parser.add_argument(
        "--write",
        action="store_true",
        help="Rewrite the generated block in services/scoring/constants.py",
    )
    args = parser.parse_args()

    snapshot_gws = sorted({int(g) for g in args.snapshots.split(",")})
    season_start_year = int(args.season.split("-")[0])

    players = load_season(args.season)
    if not players:
        raise SystemExit(f"No player rows parsed for season {args.season}")
    max_round = max(r.round for p in players for r in p.rows)
    if max_round < max(snapshot_gws):
        raise SystemExit(
            f"Season {args.season} only has data through GW{max_round} in the "
            f"vaastav archive but snapshots request GW{max(snapshot_gws)} — "
            "an incomplete season would mis-anchor the ceilings. Pick an "
            "earlier season or trim --snapshots."
        )
    understat = _fetch_understat_players(season_start_year)
    match_rate = attach_understat(players, understat, load_web_names(args.season))

    snapshots = {gw: score_snapshot(players, gw) for gw in snapshot_gws}
    anchors = compute_anchors(snapshots)
    print_report(args.season, snapshots, anchors, match_rate)

    if args.json:
        payload = {
            "season": args.season,
            "snapshots": snapshot_gws,
            "elite_target": ELITE_TARGET,
            "match_rate": match_rate,
            "anchors": {
                f"{family}/{position}": anchors[(family, position)]
                for family in FAMILIES
                for position in POSITIONS
            },
        }
        args.json.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        print(f"Wrote {args.json}")

    if args.write:
        constants_path = (
            Path(__file__).resolve().parent.parent
            / "fpl_cli/services/scoring/constants.py"
        )
        write_constants(anchors, args.season, snapshot_gws, constants_path)


if __name__ == "__main__":
    sys.exit(main())
