"""Classic FPL review helpers: team, transfers, league."""

from __future__ import annotations

import logging

from rich.markup import escape as rich_escape
from rich.table import Table

from fpl_cli.cli._context import console, error_console
from fpl_cli.cli._helpers import (
    _assign_tie_ranks,
    _center_window_with_ties,
    _fetch_standings_with_costs,
    _format_pts_display,
    _format_review_player,
    _live_player_stats,
    _net_transfer_ids,
    _slice_with_ties,
)
from fpl_cli.services.fixture_predictions import is_blank_gameweek, is_double_gameweek
from fpl_cli.utils.gameweek import is_opening_gameweek

logger = logging.getLogger(__name__)

# 3 above the user + the user + 3 below -- see #149.
_NEARBY_RIVALS_WINDOW = 7


def _format_review_classic_player(p: dict) -> str:
    """Format a classic team player for LLM prompt."""
    return _format_review_player(p, points_key="display_points", show_captain=True)


async def _review_classic_team(
    client, entry_id, gw, player_map, teams, gw_data, live_stats,
    *, bgw_team_ids: frozenset[int] = frozenset(), dgw_team_ids: frozenset[int] = frozenset(),
    players_with_fixture: frozenset[int] | None = None,
    players_with_double: frozenset[int] | None = None,
):
    """Fetch and display user's classic team performance. Returns dict with team data.

    The team-id sets say which clubs blanked and doubled this gameweek; the
    `bgw` flag one of them sets is what excuses a zero. The two player sets
    answer the same questions from the gameweek's own live data and take
    precedence where they can, so reviewing a past gameweek does not excuse a
    real zero -- or award a double -- on the strength of a club the player
    only joined later (issue #174). `resolve_players_with_fixture` and
    `resolve_players_with_double` build them.
    """
    my_entry_summary = None
    my_picks_data = []
    team_points_data = []
    automatic_subs = []
    active_chip = None

    if entry_id:
        try:
            console.print("[dim]Fetching your team data...[/dim]")
            picks_response = await client.get_manager_picks(entry_id, gw)

            entry_history = picks_response.get("entry_history", {})
            active_chip = picks_response.get("active_chip")
            # Derive captain multiplier from the captain's pick multiplier (most reliable source)
            captain_pick_raw = next((p for p in picks_response.get("picks", []) if p.get("is_captain")), None)
            if captain_pick_raw and captain_pick_raw.get("multiplier", 0) > 0:
                captain_multiplier = captain_pick_raw["multiplier"]
            else:
                captain_multiplier = 3 if active_chip == "3xc" else 2
            is_triple_captain = captain_multiplier == 3

            # Extract automatic subs from API response
            automatic_subs = picks_response.get("automatic_subs", [])
            auto_sub_in_ids = {sub["element_in"] for sub in automatic_subs}
            auto_sub_out_ids = {sub["element_out"] for sub in automatic_subs}

            my_entry_summary = {
                "points": entry_history.get("points", 0),
                "total_points": entry_history.get("total_points", 0),
                "rank": entry_history.get("rank"),
                "overall_rank": entry_history.get("overall_rank"),
                "bank": entry_history.get("bank", 0) / 10,
                "value": entry_history.get("value", 0) / 10,
                "transfers": entry_history.get("event_transfers", 0),
                "transfers_cost": entry_history.get("event_transfers_cost", 0),
            }

            # Get picks with points
            picks = picks_response.get("picks", [])
            for pick in picks:
                player = player_map.get(pick["element"])
                if player:
                    gw_points, _, red_cards = _live_player_stats(live_stats, player.id)

                    squad_slot = pick.get("position", 0)
                    multiplier = pick.get("multiplier", 1)
                    player_team = teams.get(player.team_id)
                    my_picks_data.append({
                        "id": player.id,
                        "name": player.web_name,
                        "team": player_team.short_name if player_team else "???",
                        # None, never a placeholder: the LLM prompt presents this
                        # as the authority on the player's club, and "Unknown club"
                        # reads like one. Absent, the formatter falls back to the code.
                        "team_name": player_team.name if player_team else None,
                        "position": player.position_name,
                        "points": gw_points,
                        "total_points": gw_points * multiplier,
                        "multiplier": multiplier,
                        "is_captain": pick.get("is_captain", False),
                        "is_vice": pick.get("is_vice_captain", False),
                        "red_cards": red_cards,
                        "auto_sub_in": player.id in auto_sub_in_ids,
                        "auto_sub_out": player.id in auto_sub_out_ids,
                        "bgw": is_blank_gameweek(
                            player.id, player.team_id,
                            players_with_fixture=players_with_fixture,
                            bgw_team_ids=bgw_team_ids,
                        ),
                        "dgw": is_double_gameweek(
                            player.id, player.team_id,
                            players_with_double=players_with_double,
                            dgw_team_ids=dgw_team_ids,
                        ),
                        "is_bench": squad_slot > 11,
                    })

            # Determine if captain played - if not, vice gets the multiplier
            captain_pick = next((p for p in my_picks_data if p["is_captain"]), None)
            captain_played = captain_pick and captain_pick["multiplier"] > 0

            # Build unified team_points list with display_points
            team_points_data = []
            for p in my_picks_data:
                is_bench_boost_player = active_chip == "bboost" and p["is_bench"]
                contributed = p["multiplier"] > 0 or is_bench_boost_player
                # Under BB all 15 score, so auto-subs create no points delta
                bb_no_sub_impact = active_chip == "bboost" and (p["auto_sub_in"] or p["auto_sub_out"])
                display_points = p["points"]

                # Apply captain/vice multiplier for display
                if contributed and p["is_captain"]:
                    display_points = p["points"] * captain_multiplier
                elif contributed and p["is_vice"] and not captain_played:
                    display_points = p["points"] * captain_multiplier

                team_points_data.append({
                    "name": p["name"],
                    "team": p["team"],
                    "team_name": p["team_name"],
                    "position": p["position"],
                    "points": p["points"],
                    "display_points": display_points,
                    "contributed": contributed,
                    "is_captain": p["is_captain"],
                    "is_vice": p["is_vice"],
                    "is_vice_active": p["is_vice"] and not captain_played,
                    "is_triple_captain": p["is_captain"] and is_triple_captain,
                    "red_cards": p["red_cards"],
                    "auto_sub_in": p["auto_sub_in"],
                    "auto_sub_out": p["auto_sub_out"],
                    "bgw": p["bgw"],
                    "dgw": p["dgw"],
                    "is_bench_boost_player": is_bench_boost_player,
                    "bb_no_sub_impact": bb_no_sub_impact,
                })

            # Sort: contributing first (by display_points desc), then non-contributing
            team_points_data.sort(key=lambda p: (not p["contributed"], -p["display_points"]))

        except Exception as e:  # noqa: BLE001 — display resilience
            error_console.print(f"[yellow]Could not fetch your team: {rich_escape(str(e))}[/yellow]")

    # Display Classic FPL section
    # Team Summary
    if my_entry_summary:
        console.print("\n[bold]## Team Summary[/bold]")

        summary_table = Table(show_header=True, header_style="bold", box=None)
        summary_table.add_column("Metric")
        summary_table.add_column("Value", justify="right")

        points = my_entry_summary["points"]
        points_style = (
            "bold green" if points >= 60 else "green" if points >= 50 else "yellow" if points >= 40 else "red"
        )
        summary_table.add_row("Points", f"[{points_style}]{points}[/{points_style}]")

        if my_entry_summary["rank"]:
            summary_table.add_row("GW Rank", f"{my_entry_summary['rank']:,}")
        if my_entry_summary["overall_rank"]:
            summary_table.add_row("Overall Rank", f"{my_entry_summary['overall_rank']:,}")

        if active_chip:
            chip_display = {
                "3xc": "Triple Captain", "wildcard": "Wildcard",
                "freehit": "Free Hit", "bboost": "Bench Boost",
            }
            chip_label = chip_display.get(active_chip, active_chip)
            summary_table.add_row(
                "Active Chip", f"[bold yellow]{chip_label}[/bold yellow]",
            )

        summary_table.add_row("GW Average", str(gw_data.get("average_entry_score", "N/A")))
        summary_table.add_row("GW Highest", str(gw_data.get("highest_score", "N/A")))

        if my_entry_summary["transfers"] > 0:
            cost_str = f" (-{my_entry_summary['transfers_cost']} pts)" if my_entry_summary["transfers_cost"] > 0 else ""
            summary_table.add_row("Transfers", f"{my_entry_summary['transfers']}{cost_str}")

        console.print(summary_table)

        # Team Points table (unified - no separate bench)
        if team_points_data:
            console.print("\n[bold]## Team Points[/bold]")
            has_reds = any(p.get("red_cards", 0) > 0 for p in team_points_data)
            table = Table(show_header=True, header_style="bold")
            table.add_column("Player")
            table.add_column("Team")
            table.add_column("Pos")
            table.add_column("Pts", justify="right")
            if has_reds:
                table.add_column("🟥", justify="center")

            for p in team_points_data:
                # Build player name with (C)/(TC) or (V) marker
                name_display = p["name"]
                if p.get("is_triple_captain"):
                    name_display = f"{p['name']} [bold yellow](TC)[/bold yellow]"
                elif p["is_captain"]:
                    name_display = f"{p['name']} [bold yellow](C)[/bold yellow]"
                elif p["is_vice_active"]:
                    name_display = f"{p['name']} [bold yellow](V)[/bold yellow]"

                pts_display = _format_pts_display(p, points_key="display_points")

                if has_reds:
                    red_card_display = "[bold red]🟥[/bold red]" if p.get("red_cards", 0) > 0 else ""
                    table.add_row(name_display, p["team"], p["position"], pts_display, red_card_display)
                else:
                    table.add_row(name_display, p["team"], p["position"], pts_display)
            console.print(table)

    elif entry_id:
        error_console.print("[yellow]Could not fetch your team data[/yellow]")
    else:
        console.print("[dim]Set classic_entry_id in config/settings.yaml to see your squad[/dim]")

    return {
        "my_entry_summary": my_entry_summary,
        "my_picks_data": my_picks_data,
        "team_points_data": team_points_data,
        "automatic_subs": automatic_subs,
        "active_chip": active_chip,
    }


def _collapse_transfer_churn(gw_transfers, player_map):
    """Collapse same-GW transfer churn into a net squad delta, paired by position.

    A player transferred in then later out (or vice versa) within the same GW is a
    no-op for the final squad and should not appear as a row. For the remaining
    net ins/outs, pair by (position, web_name) so rows are like-for-like where
    possible. Returns a list of (player_in, player_out) tuples. Players not found
    in `player_map` are dropped.
    """
    def _sort_key(pid):
        p = player_map.get(pid)
        return (p.position, p.web_name) if p else (99, "")

    net_in_ids, net_out_ids = _net_transfer_ids(gw_transfers, sort_key=_sort_key)
    net_ins = [player_map[pid] for pid in net_in_ids if pid in player_map]
    net_outs = [player_map[pid] for pid in net_out_ids if pid in player_map]
    return list(zip(net_ins, net_outs, strict=False))


async def _review_classic_transfers(client, entry_id, gw, player_map, teams, live_stats):
    """Fetch and display classic transfers for this GW. Returns list of transfer data."""
    classic_transfers_data = []
    if not entry_id:
        return classic_transfers_data

    try:
        all_transfers = await client.get_manager_transfers(entry_id)
        gw_transfers = [t for t in all_transfers if t.get("event") == gw]

        paired = _collapse_transfer_churn(gw_transfers, player_map)

        if paired:
            console.print("\n[bold]## Transfers[/bold]")
            transfers_table = Table(show_header=True, header_style="bold")
            transfers_table.add_column("In")
            transfers_table.add_column("Pts", justify="right")
            transfers_table.add_column("Out")
            transfers_table.add_column("Pts", justify="right")
            transfers_table.add_column("Net", justify="right")
            transfers_table.add_column("Verdict")

            for player_in, player_out in paired:
                if player_out and player_in:
                    out_points, _, _ = _live_player_stats(live_stats, player_out.id)
                    in_points, _, _ = _live_player_stats(live_stats, player_in.id)

                    net = in_points - out_points

                    # Verdict: >1 = Hit, <-1 = Miss, else Neutral
                    if net > 1:
                        verdict = "[green]✓ Hit[/green]"
                        verdict_plain = "✓ Hit"
                    elif net < -1:
                        verdict = "[red]✗ Miss[/red]"
                        verdict_plain = "✗ Miss"
                    else:
                        verdict = "[dim]→ Neutral[/dim]"
                        verdict_plain = "→ Neutral"

                    out_team = teams.get(player_out.team_id)
                    in_team = teams.get(player_in.team_id)
                    out_abbr = out_team.short_name if out_team else "???"
                    in_abbr = in_team.short_name if in_team else "???"

                    # Net display styling
                    net_style = "green" if net > 0 else "red" if net < 0 else ""
                    net_display = f"[{net_style}]{'+' if net > 0 else ''}{net}[/{net_style}]" if net_style else str(net)

                    transfers_table.add_row(
                        f"{player_in.web_name} ({in_abbr})",
                        str(in_points),
                        f"{player_out.web_name} ({out_abbr})",
                        str(out_points),
                        net_display,
                        verdict,
                    )

                    classic_transfers_data.append({
                        "player_out": player_out.web_name,
                        "player_out_team": out_abbr,
                        "player_out_team_name": out_team.name if out_team else None,
                        "player_out_points": out_points,
                        "player_in": player_in.web_name,
                        "player_in_team": in_abbr,
                        "player_in_team_name": in_team.name if in_team else None,
                        "player_in_points": in_points,
                        "net": net,
                        "verdict": verdict_plain,
                    })

            console.print(transfers_table)

            # Summary stats
            hits = sum(1 for t in classic_transfers_data if t["net"] > 1)
            misses = sum(1 for t in classic_transfers_data if t["net"] < -1)
            total_net = sum(t["net"] for t in classic_transfers_data)
            net_style = "green" if total_net > 0 else "red" if total_net < 0 else ""
            net_sign = '+' if total_net > 0 else ''
            console.print(f"\nHits: {hits} | Misses: {misses} | Net: [{net_style}]{net_sign}{total_net}[/{net_style}]")
        elif is_opening_gameweek(gw):
            # An empty GW1 transfer list is not a rolled transfer: the squad was
            # bought pre-season and the first free transfer only arrives in GW2.
            console.print("\n[bold]## Transfers[/bold]")
            console.print("[dim]None - GW1 squads are bought pre-season, so there is nothing to review here[/dim]")

    except Exception as e:  # noqa: BLE001 — display resilience
        console.print(f"[dim]Could not fetch transfers: {rich_escape(str(e))}[/dim]")

    return classic_transfers_data


async def _review_classic_league(
    client, classic_league_id, entry_id, gw, api_current_gw_id,
    *, use_net_points: bool = False,
):
    """Fetch and display classic league standings. Returns league data dict or None."""
    if not (classic_league_id and entry_id):
        return None

    classic_league_data = None
    try:
        standings_data = await client.get_classic_league_standings(classic_league_id)
        league_name = standings_data.get("league", {}).get("name", "Classic League")
        standings = standings_data.get("standings", {}).get("results", [])

        # Check if we're reviewing a historical GW (league data would be stale)
        is_historical_review = api_current_gw_id is not None and gw != api_current_gw_id
        if is_historical_review:
            console.print("\n[bold]## League[/bold]")
            console.print(f"[dim]League standings not shown for historical GW{gw} review[/dim]")
            console.print("[dim]Use 'fpl league' for current standings[/dim]")
            return {"league_name": league_name}

        # FPL builds a mini-league's table for the first time only once the
        # opening gameweek is finalised -- until then every entry sits in
        # `new_entries` and `results` is empty. Rendering the sections anyway
        # printed bare headings and handed the report a 0-point, 0-entry league.
        if not standings:
            console.print("\n[bold]## League[/bold]")
            console.print(
                f"[dim]{league_name}: standings not published yet"
                " -- FPL builds mini-league tables after the opening gameweek is finalised[/dim]"
            )
            console.print("[dim]Re-run once the table appears, or use 'fpl league'[/dim]")
            return {"league_name": league_name, "standings_pending": True}

        console.print("\n[bold]## League[/bold]")

        # Find user's position and points
        user_rank: int | str = "?"
        user_total = 0
        user_gw_pts = 0
        total_entries = len(standings)
        nearby: list = []
        nearby_window: list = []
        nearby_omitted = 0
        user_entry = next((e for e in standings if e.get("entry") == entry_id), None)
        if user_entry:
            user_rank = user_entry.get("rank", "?")
            user_total = user_entry.get("total", 0)
            user_gw_pts = user_entry.get("event_total", 0)

            console.print(f"**{league_name}**")
            console.print(f"- Position: {user_rank} of {total_entries}")
            console.print(f"- GW Points: {user_gw_pts} (Total: {user_total:,})")

            # Find nearby rivals (+/- 25 points)
            nearby = [
                e for e in standings
                if abs(e.get("total", 0) - user_total) <= 25
            ]
            nearby.sort(key=lambda x: x.get("total", 0), reverse=True)

            if len(nearby) > 1:  # More than just the user
                user_nearby_index = next(
                    i for i, e in enumerate(nearby) if e.get("entry") == entry_id
                )
                nearby_window, nearby_omitted = _center_window_with_ties(
                    nearby, user_nearby_index, _NEARBY_RIVALS_WINDOW, "total",
                )

                console.print("\n[bold]### Nearby Rivals (+/- 25 pts)[/bold]")
                for entry in nearby_window:
                    rank = entry.get("rank", "?")
                    name = entry.get("player_name", "Unknown")
                    total = entry.get("total", 0)
                    diff = total - user_total
                    is_user = entry.get("entry") == entry_id

                    if is_user:
                        console.print(f"  {rank}. [bold cyan]You[/bold cyan] - {total:,} pts")
                    else:
                        diff_str = f"+{diff}" if diff > 0 else str(diff)
                        diff_style = "red" if diff > 0 else "green"
                        console.print(f"  {rank}. {name} - {total:,} pts ([{diff_style}]{diff_str}[/{diff_style}])")
                if nearby_omitted:
                    console.print(f"  [dim]...and {nearby_omitted} more within 25[/dim]")

        standings_with_costs = await _fetch_standings_with_costs(
            client, standings, entry_id, gw, fetch_costs=use_net_points,
        )

        # Best GW performers (top 3 + ties)
        header_suffix = " (Net Points)" if use_net_points else ""
        sorted_by_net_desc = sorted(standings_with_costs, key=lambda x: x["net_points"], reverse=True)
        _assign_tie_ranks(sorted_by_net_desc, "net_points")
        best_performers_display = _slice_with_ties(sorted_by_net_desc, 3)

        console.print(f"\n[bold]### Best GW Performers{header_suffix}[/bold]")
        for perf in best_performers_display:
            name = perf["name"]
            gross = perf["gross_points"]
            cost = perf["transfer_cost"]
            net = perf["net_points"]
            rank = perf["rank_str"]

            if perf["is_user"]:
                if cost > 0:
                    console.print(f"  {rank}. [bold cyan]You[/bold cyan] - {gross} gross, -{cost} hit = {net} net")
                else:
                    console.print(f"  {rank}. [bold cyan]You[/bold cyan] - {net} pts")
            else:
                if cost > 0:
                    console.print(f"  {rank}. {name} - {gross} gross, -{cost} hit = {net} net")
                else:
                    console.print(f"  {rank}. {name} - {net} pts")

        # Snapshot best performer report data before ascending sort overwrites ranks
        best_performers_for_report = [
            {
                "name": e["name"],
                "points": e["net_points"],
                "gross_points": e["gross_points"],
                "transfer_cost": e["transfer_cost"],
                "rank_str": e["rank_str"],
            }
            for e in best_performers_display
        ]

        # Capture user's GW rank within the classic league (by net points)
        user_gw_entry = next(
            (e for e in sorted_by_net_desc if e["is_user"]), None
        )
        classic_user_gw_rank = user_gw_entry["rank_str"] if user_gw_entry else None

        # Worst performers (bottom 5 + ties)
        sorted_by_net_asc = sorted(standings_with_costs, key=lambda x: x["net_points"])
        _assign_tie_ranks(sorted_by_net_asc, "net_points")

        worst_performers_data = _slice_with_ties(sorted_by_net_asc, 5)
        user_in_bottom = any(p["is_user"] for p in worst_performers_data)
        if not user_in_bottom:
            user_data = next((p for p in standings_with_costs if p["is_user"]), None)
            if user_data:
                worst_performers_data.append(user_data)

        # Calculate transfer impact narrative (only when net points are tracked)
        transfer_impact = None
        user_entry_data = next((p for p in standings_with_costs if p["is_user"]), None)
        if use_net_points and len(worst_performers_data) >= 2:
            last_place = worst_performers_data[0]

            if user_entry_data:
                user_transfer_cost = user_entry_data["transfer_cost"]
                user_is_last = user_entry_data == last_place
                if user_is_last and user_transfer_cost > 0:
                    sorted_by_gross = sorted(standings_with_costs, key=lambda x: x["gross_points"])
                    user_gross_rank = sorted_by_gross.index(user_entry_data)
                    if user_gross_rank > 0:
                        transfer_impact = f"Your -{user_transfer_cost} hit dropped you to last place"
                elif not user_is_last and last_place["transfer_cost"] > 0:
                    last_without_hit = last_place["gross_points"]
                    if user_entry_data["net_points"] < last_without_hit:
                        transfer_impact = (
                            f"{last_place['name']}'s -{last_place['transfer_cost']} hit saved you from last place"
                        )

        console.print(f"\n[bold]### Worst GW Performers{header_suffix}[/bold]")
        for perf in worst_performers_data:
            name = perf["name"]
            gross = perf["gross_points"]
            cost = perf["transfer_cost"]
            net = perf["net_points"]
            rank = perf["rank_str"]

            if perf["is_user"]:
                if cost > 0:
                    console.print(f"  {rank}. [bold cyan]You[/bold cyan] - {gross} gross, -{cost} hit = {net} net")
                else:
                    console.print(f"  {rank}. [bold cyan]You[/bold cyan] - {net} pts")
            else:
                if cost > 0:
                    console.print(f"  {rank}. {name} - {gross} gross, -{cost} hit = {net} net")
                else:
                    console.print(f"  {rank}. {name} - {net} pts")

        if transfer_impact:
            error_console.print(f"\n[yellow]  ⚠ {transfer_impact}[/yellow]")

        # Store for report
        classic_league_data = {
            "league_name": league_name,
            "user_position": user_rank,
            "user_gw_rank": classic_user_gw_rank,
            "total_entries": total_entries,
            "user_gw_points": user_gw_pts,
            "user_total": user_total,
            # `total_entries` alone can't distinguish "unknown" from "known but
            # not on this page" -- a >50-entry league still reports a nonzero
            # page size even when the user's own entry wasn't in it.
            "user_found_in_standings": user_entry is not None,
            "nearby_rivals": [
                {
                    "rank": e.get("rank"),
                    "manager_name": e.get("player_name", "Unknown"),
                    "total": e.get("total", 0),
                    "is_user": e.get("entry") == entry_id,
                }
                for e in nearby_window
            ],
            "nearby_rivals_omitted": nearby_omitted,
            "best_performers": best_performers_for_report,
            "worst_performers": [
                {
                    "name": e["name"],
                    "points": e["net_points"],
                    "gross_points": e["gross_points"],
                    "transfer_cost": e["transfer_cost"],
                    "rank_str": e["rank_str"],
                    "is_user": e.get("is_user", False),
                }
                for e in worst_performers_data
            ],
            "transfer_impact": transfer_impact,
        }
        if use_net_points and user_entry_data:
            classic_league_data["user_gw_net_points"] = user_entry_data.get("net_points", user_gw_pts)

    except Exception as e:  # noqa: BLE001 — display resilience
        error_console.print(f"[yellow]Could not fetch classic league standings: {rich_escape(str(e))}[/yellow]")

    return classic_league_data
