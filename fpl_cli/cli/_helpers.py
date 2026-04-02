"""Shared CLI helper functions: ranking, formatting, FDR styling."""

from __future__ import annotations

import asyncio
import logging

logger = logging.getLogger(__name__)

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
    "defensive_contribution_per_90", "value_form", "value_season",
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
        pts_str = f"{pts} [AUTO-SUB IN]"
    elif p.get("auto_sub_out"):
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

    line = f"- {p['name']} ({p['team']}, {p['position']}): {pts_str} pts"
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
        return f"[{pts_style}]{pts}[/{pts_style}]" if pts_style else str(pts)
    else:
        if pts >= 6:
            return f"[yellow]({pts}) [UNUSED!][/yellow]"
        return f"[dim]({pts})[/dim]"


def _validate_team_filter(team: str | None, all_teams: list) -> str | None:
    """Return uppercase short name or exit with error if team is unknown."""
    if not team:
        return None
    from fpl_cli.cli._context import console
    valid = {t.short_name.upper(): t.short_name for t in all_teams}
    if team.upper() not in valid:
        sorted_names = sorted(valid.values())
        console.print(
            f"[red]Unknown team '{team}'. Valid teams:[/red] "
            f"{', '.join(sorted_names)}"
        )
        raise SystemExit(1)
    return team.upper()


def _format_sort_value(field: str, value) -> str:
    """Format a player stat value for table display."""
    fmt = _PLAYERS_FIELD_FORMAT.get(field)
    if fmt == "price":
        return f"£{value / 10:.1f}m"
    if fmt == "pct":
        return f"{value:.1f}%"
    if field in _PLAYERS_FLOAT_FIELDS:
        return f"{value:.1f}"
    return str(value)
