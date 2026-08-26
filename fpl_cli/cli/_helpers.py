"""Shared CLI helper functions: ranking, formatting, FDR styling."""

from __future__ import annotations

import asyncio
import logging
from collections import Counter
from collections.abc import Callable
from typing import Any

logger = logging.getLogger(__name__)


def _net_transfer_ids(
    gw_transfers: list[dict[str, Any]],
    sort_key: Callable[[Any], Any] | None = None,
) -> tuple[list[Any], list[Any]]:
    """Cancel same-GW transfer churn and return (net_in_ids, net_out_ids).

    A player id appearing as both `element_in` and `element_out` within the GW
    cancels up to the lower multiplicity — the squad delta only reflects the
    residual net change. `None` ids (draft free-agent pickups with no drop) are
    excluded before counting. When `sort_key` is supplied, both lists are sorted
    by it (callers use this to pair ins/outs like-for-like).
    """
    in_ids = [t.get("element_in") for t in gw_transfers if t.get("element_in") is not None]
    out_ids = [t.get("element_out") for t in gw_transfers if t.get("element_out") is not None]
    net_in = Counter(in_ids) - Counter(out_ids)
    net_out = Counter(out_ids) - Counter(in_ids)
    net_in_list = [pid for pid, c in net_in.items() for _ in range(c)]
    net_out_list = [pid for pid, c in net_out.items() for _ in range(c)]
    if sort_key is not None:
        net_in_list.sort(key=sort_key)
        net_out_list.sort(key=sort_key)
    return net_in_list, net_out_list

def _entry_league_meta(manager_data: dict[str, Any], league_id: int | None) -> dict[str, int]:
    """Exact league size and rank, taken from the entry payload.

    `entry/{id}/` lists every classic league the manager is in, each with
    `rank_count` (entries in the league) and `entry_rank` (their true rank).
    Both survive a league too big for one 50-entry page of standings, and both
    are free whenever the caller already fetches this endpoint. Standings can
    only ever report the size of the page it was handed.
    """
    if not league_id:
        return {}
    for league in manager_data.get("leagues", {}).get("classic", []):
        if league.get("id") == league_id:
            return {
                key: league[key]
                for key in ("rank_count", "entry_rank")
                if isinstance(league.get(key), int)
            }
    return {}


# Centralised FDR threshold constants (1-7 scale)
FDR_EASY = 2.5
FDR_MEDIUM = 3.5
FDR_HARD = 4.5

_PICKS_CONCURRENCY = 10


def _assign_tie_ranks(sorted_items: list[dict], score_key: str) -> None:
    """Assign tie-aware ranks in-place to an already-sorted list.

    Standard competition ranking: ties share rank with '=' suffix.
    e.g. 1, 2, 3=, 3=, 5
    """
    for i, item in enumerate(sorted_items):
        if i > 0 and item[score_key] == sorted_items[i - 1][score_key]:
            item["rank"] = sorted_items[i - 1]["rank"]
        else:
            item["rank"] = i + 1
    rank_counts: dict[int, int] = {}
    for item in sorted_items:
        rank_counts[item["rank"]] = rank_counts.get(item["rank"], 0) + 1
    for item in sorted_items:
        item["rank_str"] = f"{item['rank']}=" if rank_counts[item["rank"]] > 1 else str(item["rank"])


def _gw_position_with_half(position: int | str, total: int) -> str:
    """Return position string annotated with pre-computed half classification.

    Removes the need for the LLM to do arithmetic. Returns e.g. "5 [BOTTOM HALF, 4th worst]".
    Top half: position <= total // 2. Bottom half: position > total // 2.
    Exact middle only exists in odd-numbered leagues (position == (total + 1) // 2).
    """
    try:
        pos = int(str(position).rstrip("="))
    except (ValueError, TypeError):
        return str(position)
    is_tied = str(position).endswith("=")
    mid = (total + 1) // 2
    if total % 2 == 1 and pos == mid:
        label = "EXACT MIDDLE"
    elif pos <= total // 2:
        label = "TOP HALF"
    else:
        worst_rank = total - pos + 1
        label = f"BOTTOM HALF, {worst_rank}{'=' if is_tied else ''} worst"
    return f"{position} [{label}]"


def _slice_with_ties(sorted_items: list[dict], n: int) -> list[dict]:
    """Slice to *n* items, extending to include all entries sharing the boundary rank."""
    if not sorted_items or n <= 0:
        return []
    if n >= len(sorted_items):
        return list(sorted_items)
    boundary_rank = sorted_items[n - 1]["rank"]
    return [item for item in sorted_items if item["rank"] <= boundary_rank]


def _fdr_style(fdr: int | float) -> str:
    """Get Rich style for FDR value on 1-7 scale."""
    if fdr <= FDR_EASY:
        return "green"
    elif fdr <= FDR_MEDIUM:
        return "yellow"
    elif fdr <= FDR_HARD:
        return "orange1"
    else:
        return "red"


# Formatting rules for the dynamic sort column
_PLAYERS_FIELD_FORMAT: dict[str, str] = {
    "now_cost": "price",  # £X.Xm
    "selected_by_percent": "pct",  # X.X%
}

# Fields stored as float on the Player model
_PLAYERS_FLOAT_FIELDS = {
    "points_per_game", "form", "influence", "creativity", "threat", "ict_index",
    "expected_goals", "expected_assists", "expected_goal_involvements", "expected_goals_conceded",
    "selected_by_percent",
    "defensive_contribution_per_90",
    "form_per_m", "pts_per_m",
    "ep_next", "ep_this",
}


async def _fetch_standings_with_costs(
    client, standings: list[dict], entry_id: int | None, gw: int,
    *, fetch_costs: bool = True,
) -> list[dict]:
    """Fetch transfer costs for all managers in parallel (bounded concurrency).

    Used by league.py and _review_classic.py to calculate net GW points.
    When fetch_costs=False, skips the N API calls and returns gross-only data.
    """
    if not fetch_costs:
        return [
            {
                "entry_id": e.get("entry"),
                "name": e.get("player_name", "Unknown"),
                "gross_points": e.get("event_total", 0),
                "transfer_cost": 0,
                "net_points": e.get("event_total", 0),
                "is_user": e.get("entry") == entry_id,
            }
            for e in standings
        ]

    sem = asyncio.Semaphore(_PICKS_CONCURRENCY)

    async def _fetch_one(entry: dict) -> dict:
        league_entry_id = entry.get("entry")
        gross_pts = entry.get("event_total", 0)
        async with sem:
            try:
                picks_data = await client.get_manager_picks(league_entry_id, gw)
                transfer_cost = picks_data.get("entry_history", {}).get("event_transfers_cost", 0)
            except Exception as e:  # noqa: BLE001 — best-effort enrichment
                transfer_cost = 0
                logger.warning("Failed to fetch transfer cost for entry %s: %s", league_entry_id, e)
        return {
            "entry_id": league_entry_id,
            "name": entry.get("player_name", "Unknown"),
            "gross_points": gross_pts,
            "transfer_cost": transfer_cost,
            "net_points": gross_pts - transfer_cost,
            "is_user": league_entry_id == entry_id,
        }

    return list(await asyncio.gather(*(_fetch_one(e) for e in standings)))


def _live_player_stats(live_stats: dict, player_id: int | None) -> tuple[int, int, int]:
    """Look up (total_points, minutes, red_cards) from live GW data, defaulting to 0."""
    if player_id is None:
        return 0, 0, 0
    stats = live_stats.get(player_id, {})
    return (
        stats.get("total_points", 0),
        stats.get("minutes", 0),
        stats.get("red_cards", 0),
    )


def _format_review_player(p: dict, points_key: str = "points", show_captain: bool = False) -> str:
    """Format a player for LLM review prompt. Shared by classic and draft review."""
    pts = p[points_key]

    # BGW overrides all other status annotations (most specific reason for 0 pts)
    if p.get("bgw"):
        if p.get("auto_sub_out"):
            pts_str = f"({pts}) [DIDN'T PLAY - BGW]"
        else:
            pts_str = f"({pts}) [BGW]"
    elif p.get("auto_sub_in"):
        if p.get("bb_no_sub_impact"):
            pts_str = f"{pts} [AUTO-SUB IN - no points impact: BB active, all 15 score]"
        else:
            pts_str = f"{pts} [AUTO-SUB IN]"
    elif p.get("auto_sub_out"):
        if p.get("bb_no_sub_impact"):
            pts_str = f"({pts}) [DIDN'T PLAY - auto-subbed out; no points impact: BB active]"
        else:
            pts_str = f"({pts}) [DIDN'T PLAY - auto-subbed out]"
    elif not p.get("contributed", True):
        if pts >= 6:
            pts_str = f"({pts}) [BENCH - {pts} pts unused!]"
        else:
            pts_str = f"({pts}) [BENCH]"
    else:
        pts_str = str(pts)

    # DGW is an additive suffix - no precedence conflict
    if p.get("dgw"):
        pts_str += " [DGW]"

    # Full club name where we have one, not just the 3-letter code: the model
    # otherwise has to expand the code itself, and for a player who changed
    # clubs it expands from a stale training prior rather than from this data.
    club = p.get("team_name") or p["team"]
    line = f"- {p['name']} ({club}, {p['position']}): {pts_str} pts"
    if show_captain:
        if p.get("is_triple_captain"):
            line += " (TC)"
        elif p.get("is_captain"):
            line += " (C)"
    if p.get("red_cards", 0) > 0:
        line += " 🟥"
    return line


def _format_pts_display(p: dict, points_key: str = "points") -> str:
    """Format points with auto-sub/bench markers for Rich table display."""
    pts = p[points_key]

    if p.get("auto_sub_in"):
        pts_style = "bold green" if pts >= 10 else "green" if pts >= 6 else ""
        pts_val = f"[{pts_style}]{pts}[/{pts_style}]" if pts_style else str(pts)
        return f"{pts_val} [cyan][SUB IN][/cyan]"
    elif p.get("auto_sub_out"):
        return f"[dim]({pts}) [DIDN'T PLAY][/dim]"
    elif p.get("contributed", True):
        pts_style = "bold green" if pts >= 10 else "green" if pts >= 6 else ""
        pts_val = f"[{pts_style}]{pts}[/{pts_style}]" if pts_style else str(pts)
        bb_suffix = " [cyan][BB][/cyan]" if p.get("is_bench_boost_player") else ""
        return f"{pts_val}{bb_suffix}"
    else:
        if pts >= 6:
            return f"[yellow]({pts}) [UNUSED!][/yellow]"
        return f"[dim]({pts})[/dim]"


def require_entry_id(
    settings: dict[str, Any], *, is_draft: bool, command: str, output_format: str,
) -> int:
    """Return the configured entry ID for the active format, or report and exit 1.

    `squad`, `squad grid` and any later consumer need the same lookup and the
    same message, and both call sites used to carry their own copy of both --
    including, for a while, two different wordings of the same error (#159
    review). *command* and *output_format* are required for the same reason
    they are on `_validate_team_filter`: the caller's format decides whether
    this comes back as prose or as the error envelope.
    """
    from fpl_cli.cli._json import emit_failure

    key = "draft_entry_id" if is_draft else "classic_entry_id"
    entry_id = settings.get("fpl", {}).get(key)
    if not entry_id:
        hint = "" if is_draft else (
            " Find it in your FPL URL:"
            " fantasy.premierleague.com/entry/ENTRY_ID/event/..."
        )
        emit_failure(command, f"{key} is not set in settings.yaml.{hint}", output_format)
    return entry_id


def _validate_team_filter(
    team: str | None, all_teams: list, *, command: str, output_format: str,
) -> str | None:
    """Return uppercase short name, or report an unknown team and exit 1.

    *command* and *output_format* are required because the caller's format
    decides the channel: under `--format json` an unknown team has to come back
    as the error envelope, not as prose on stdout ahead of it (#140).
    """
    if not team:
        return None
    from fpl_cli.cli._json import emit_failure
    valid = {t.short_name.upper(): t.short_name for t in all_teams}
    if team.upper() not in valid:
        sorted_names = sorted(valid.values())
        emit_failure(
            command,
            f"Unknown team '{team}'. Valid teams: {', '.join(sorted_names)}",
            output_format,
        )
    return team.upper()


def _format_sort_value(field: str, value) -> str:
    """Format a player stat value for table display."""
    fmt = _PLAYERS_FIELD_FORMAT.get(field)
    if fmt == "price":
        return f"£{value / 10:.1f}m"
    if fmt == "pct":
        return f"{value:.1f}%"
    if field in _PLAYERS_FLOAT_FIELDS:
        # Missing-data UX: render em dash. A table column can't be omitted
        # per-row, so we substitute a visible placeholder. The Rich panel
        # in `fpl player` drops the whole segment instead — different
        # surface, different right answer.
        return f"{value:.1f}" if value is not None else "—"
    return str(value)
