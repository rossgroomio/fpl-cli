"""Draft FPL review helpers: team points, transactions, league."""

from __future__ import annotations

from rich.markup import escape as rich_escape
from rich.table import Table

from fpl_cli.cli._context import console
from fpl_cli.cli._helpers import (
    _assign_tie_ranks,
    _format_pts_display,
    _format_review_player,
    _live_player_stats,
    _slice_with_ties,
)
from fpl_cli.models.player import POSITION_MAP
from fpl_cli.utils.text import strip_diacritics


def _format_review_draft_player(p: dict) -> str:
    """Format a draft squad player for LLM prompt."""
    return _format_review_player(p, points_key="points", show_captain=False)


async def _review_draft(
    client, draft_league_id, draft_entry_id, gw, api_current_gw_id,
    players, player_map, teams, live_stats,
    *, bgw_team_ids: frozenset[int] = frozenset(), dgw_team_ids: frozenset[int] = frozenset(),
):
    """Fetch and display draft league data. Returns dict with draft data."""
    from fpl_cli.api.fpl_draft import FPLDraftClient

    draft_league_data = None
    draft_league_name = "Draft League"
    draft_squad_points_data = []
    draft_transactions_data = []
    draft_automatic_subs = []
    draft_player_map = {}  # Will be populated from Draft API bootstrap

    if not draft_league_id:
        return {
            "draft_league_data": draft_league_data,
            "draft_squad_points_data": draft_squad_points_data,
            "draft_transactions_data": draft_transactions_data,
            "draft_automatic_subs": draft_automatic_subs,
            "draft_player_map": draft_player_map,
        }

    console.print("\n" + "-" * 50)
    console.print("\n[bold cyan]# Draft[/bold cyan]")

    try:
        async with FPLDraftClient() as draft_client:
            league_details = await draft_client.get_league_details(draft_league_id)

            # Build Draft-specific player map (Draft API uses different element IDs!)
            draft_bootstrap = await draft_client.get_bootstrap_static()
            draft_elements = draft_bootstrap.get("elements", [])
            draft_player_map = {p["id"]: p for p in draft_elements}

            # Map Draft element IDs to Main FPL element IDs by web_name AND team for GW history lookup
            # Using (web_name, team_id) tuple to avoid ambiguous matches (e.g., two players named Martinez)
            main_player_by_name_team = {(strip_diacritics(p.web_name).lower(), p.team_id): p for p in players}
            draft_to_main_id = {}
            for dp in draft_elements:
                key = (strip_diacritics(dp.get("web_name", "")).lower(), dp.get("team"))
                main_player = main_player_by_name_team.get(key)
                if main_player:
                    draft_to_main_id[dp["id"]] = main_player.id

            # Get standings and entry mapping
            # Note: standings use 'league_entry' which maps to 'id' in league_entries
            draft_league_name = league_details.get("league", {}).get("name", "Draft League")
            standings = league_details.get("standings", [])
            league_entries = league_details.get("league_entries", [])
            entry_map = {e.get("id"): e for e in league_entries}

            # Find user's entry in standings
            user_standing = None
            for s in standings:
                entry_info = entry_map.get(s.get("league_entry"))
                if entry_info and entry_info.get("entry_id") == draft_entry_id:
                    user_standing = s
                    break

            if user_standing and draft_entry_id:
                user_rank = user_standing.get("rank", "?")
                user_total = user_standing.get("total", 0)
                user_gw_pts = user_standing.get("event_total", 0)
                total_entries = len(standings)

                # Fetch draft squad picks for this gameweek
                try:
                    picks_data = await draft_client.get_entry_picks(draft_entry_id, gw)
                    draft_picks = picks_data.get("picks", [])

                    # Extract automatic subs from Draft API response
                    # Note: Draft API uses "subs" key, not "automatic_subs"
                    draft_automatic_subs = picks_data.get("subs", [])
                    draft_auto_sub_in_ids = {
                        sub["element_in"] for sub in draft_automatic_subs
                    }
                    draft_auto_sub_out_ids = {
                        sub["element_out"] for sub in draft_automatic_subs
                    }

                    if draft_picks:
                        for pick in draft_picks:
                            draft_elem_id = pick.get("element")
                            draft_player = draft_player_map.get(draft_elem_id)
                            if draft_player:
                                # Look up Main FPL API element ID for GW history
                                main_elem_id = draft_to_main_id.get(draft_elem_id)

                                gw_points, gw_minutes, red_cards = _live_player_stats(live_stats, main_elem_id)

                                # Get team short name
                                player_team_id = draft_player.get("team")
                                team_short = (
                                    teams.get(player_team_id).short_name
                                    if teams.get(player_team_id) else "???"
                                )

                                # Get position name from element_type
                                pos_name = POSITION_MAP.get(draft_player.get("element_type"), "???")

                                # In draft, position 1-11 are starting XI, 12-15 are bench
                                squad_position = pick.get("position", 1)
                                is_starter = squad_position <= 11

                                draft_squad_points_data.append({
                                    "id": draft_elem_id,
                                    "name": draft_player.get("web_name", "Unknown"),
                                    "team": team_short,
                                    "position": pos_name,
                                    "points": gw_points,
                                    "minutes": gw_minutes,
                                    "squad_position": squad_position,
                                    "is_starter": is_starter,
                                    "contributed": is_starter,  # Will be updated after auto-sub inference
                                    "red_cards": red_cards,
                                    "auto_sub_in": draft_elem_id in draft_auto_sub_in_ids,
                                    "auto_sub_out": draft_elem_id in draft_auto_sub_out_ids,
                                    "bgw": player_team_id in bgw_team_ids,
                                    "dgw": player_team_id in dgw_team_ids,
                                })

                        # Infer auto-subs if Draft API didn't return them
                        if not draft_automatic_subs:
                            # Find starters who didn't play (0 minutes)
                            starters_out = [p for p in draft_squad_points_data if p["is_starter"] and p["minutes"] == 0]
                            # Bench players sorted by bench position (12, 13, 14, 15)
                            bench = sorted(
                                [p for p in draft_squad_points_data if not p["is_starter"]],
                                key=lambda x: x["squad_position"]
                            )

                            # Simple auto-sub: for each starter who didn't play, sub in first available bench player
                            # (FPL has formation rules, but for simplicity we just match by order)
                            bench_idx = 0
                            for starter in starters_out:
                                # GK can only be replaced by GK (position 12 is usually backup GK)
                                if starter["position"] == "GK":
                                    bench_gk = next(
                                        (p for p in bench if p["position"] == "GK" and not p.get("auto_sub_in")), None
                                    )
                                    if bench_gk:
                                        starter["auto_sub_out"] = True
                                        starter["contributed"] = False
                                        bench_gk["auto_sub_in"] = True
                                        bench_gk["contributed"] = True
                                        draft_automatic_subs.append({
                                            "element_in": bench_gk["id"],
                                            "element_out": starter["id"]
                                        })
                                else:
                                    # Find next available outfield bench player
                                    while bench_idx < len(bench):
                                        bench_player = bench[bench_idx]
                                        bench_idx += 1
                                        if bench_player["position"] != "GK" and not bench_player.get("auto_sub_in"):
                                            starter["auto_sub_out"] = True
                                            starter["contributed"] = False
                                            bench_player["auto_sub_in"] = True
                                            bench_player["contributed"] = True
                                            draft_automatic_subs.append({
                                                "element_in": bench_player["id"],
                                                "element_out": starter["id"]
                                            })
                                            break

                        # Sort: contributing first (by points desc), then non-contributing
                        draft_squad_points_data.sort(key=lambda p: (not p["contributed"], -p["points"]))

                        console.print("\n[bold]## Team Points[/bold]")
                        has_reds = any(p.get("red_cards", 0) > 0 for p in draft_squad_points_data)
                        table = Table(show_header=True, header_style="bold")
                        table.add_column("Player")
                        table.add_column("Team")
                        table.add_column("Pos")
                        table.add_column("Pts", justify="right")
                        if has_reds:
                            table.add_column("🟥", justify="center")

                        for p in draft_squad_points_data:
                            pts_display = _format_pts_display(p, points_key="points")
                            if has_reds:
                                red_card_display = "[bold red]🟥[/bold red]" if p.get("red_cards", 0) > 0 else ""
                                table.add_row(p["name"], p["team"], p["position"], pts_display, red_card_display)
                            else:
                                table.add_row(p["name"], p["team"], p["position"], pts_display)
                        console.print(table)
                except Exception as e:  # noqa: BLE001 — display resilience
                    console.print(f"[dim]Could not fetch draft picks: {rich_escape(str(e))}[/dim]")

                # Fetch Draft transactions for this GW
                draft_transactions_data = []
                try:
                    transactions = await draft_client.get_league_transactions(draft_league_id)
                    all_txns = transactions.get("transactions", [])

                    # Filter to user's successful transactions for this GW
                    # Note: transaction 'entry' field corresponds to entry_id (draft_entry_id)
                    gw_txns = [
                        t for t in all_txns
                        if t.get("event") == gw
                        and t.get("entry") == draft_entry_id
                        and t.get("result") == "a"  # 'a' = accepted/successful
                        and t.get("element_in")  # Has a player coming in
                    ]

                    if gw_txns:
                        console.print("\n[bold]## Transactions[/bold]")
                        txn_table = Table(show_header=True, header_style="bold")
                        txn_table.add_column("In")
                        txn_table.add_column("Pts", justify="right")
                        txn_table.add_column("Out")
                        txn_table.add_column("Pts", justify="right")
                        txn_table.add_column("Net", justify="right")
                        txn_table.add_column("Verdict")

                        for txn in gw_txns:
                            player_out_id = txn.get("element_out")
                            player_in_id = txn.get("element_in")
                            # Use Draft player map for lookups
                            draft_player_out = draft_player_map.get(player_out_id) if player_out_id else None
                            draft_player_in = draft_player_map.get(player_in_id)

                            if draft_player_in:
                                # Get points for OUT player this GW (if there was one)
                                out_points = 0
                                out_name = "-"
                                out_abbr = ""
                                if draft_player_out:
                                    # Look up Main FPL API element ID for GW history
                                    main_out_id = draft_to_main_id.get(player_out_id)
                                    if main_out_id:
                                        out_points, _, _ = _live_player_stats(live_stats, main_out_id)
                                    out_team = teams.get(draft_player_out.get("team"))
                                    out_abbr = out_team.short_name if out_team else "???"
                                    out_name = f"{draft_player_out.get('web_name', 'Unknown')} ({out_abbr})"

                                # Get points for IN player this GW
                                in_pick = next(
                                    (p for p in draft_squad_points_data
                                     if p["name"] == draft_player_in.get("web_name")),
                                    None,
                                )
                                in_points = in_pick["points"] if in_pick else 0
                                in_team = teams.get(draft_player_in.get("team"))
                                in_abbr = in_team.short_name if in_team else "???"

                                net = in_points - out_points

                                # Verdict
                                if net > 1:
                                    verdict = "[green]✓ Hit[/green]"
                                    verdict_plain = "✓ Hit"
                                elif net < -1:
                                    verdict = "[red]✗ Miss[/red]"
                                    verdict_plain = "✗ Miss"
                                else:
                                    verdict = "[dim]→ Neutral[/dim]"
                                    verdict_plain = "→ Neutral"

                                net_style = "green" if net > 0 else "red" if net < 0 else ""
                                net_sign = '+' if net > 0 else ''
                                net_display = (
                                    f"[{net_style}]{net_sign}{net}[/{net_style}]" if net_style else str(net)
                                )

                                txn_table.add_row(
                                    f"{draft_player_in.get('web_name', 'Unknown')} ({in_abbr})",
                                    str(in_points),
                                    out_name if draft_player_out else "[dim]-[/dim]",
                                    str(out_points) if draft_player_out else "-",
                                    net_display,
                                    verdict,
                                )

                                draft_transactions_data.append({
                                    "player_out": draft_player_out.get("web_name") if draft_player_out else None,
                                    "player_out_team": out_abbr if draft_player_out else None,
                                    "player_out_points": out_points if draft_player_out else None,
                                    "player_in": draft_player_in.get("web_name", "Unknown"),
                                    "player_in_team": in_abbr,
                                    "player_in_points": in_points,
                                    "net": net,
                                    "verdict": verdict_plain,
                                })

                        console.print(txn_table)

                        # Summary stats
                        hits = sum(1 for t in draft_transactions_data if t["net"] > 1)
                        misses = sum(1 for t in draft_transactions_data if t["net"] < -1)
                        total_net = sum(t["net"] for t in draft_transactions_data)
                        net_style = "green" if total_net > 0 else "red" if total_net < 0 else ""
                        net_sign = '+' if total_net > 0 else ''
                        console.print(
                            f"\nHits: {hits} | Misses: {misses} | Net: [{net_style}]{net_sign}{total_net}[/{net_style}]"
                        )

                except Exception as e:  # noqa: BLE001 — display resilience
                    console.print(f"[dim]Could not fetch transactions: {rich_escape(str(e))}[/dim]")

                # ## League section - only show for current GW (live data)
                is_historical_review = api_current_gw_id is not None and gw != api_current_gw_id
                if is_historical_review:
                    console.print("\n[bold]## League[/bold]")
                    console.print(f"[dim]League standings not shown for historical GW{gw} review[/dim]")
                    console.print("[dim]Use 'fpl league' for current standings[/dim]")
                else:
                    console.print("\n[bold]## League[/bold]")
                    console.print(f"- Position: {user_rank} of {total_entries}")
                    console.print(f"- GW Points: {user_gw_pts} (Total: {user_total:,})")

                    # Build standings with manager names
                    standings_with_names = []
                    for s in standings:
                        entry_info = entry_map.get(s.get("league_entry"), {})
                        manager_name = (
                            f"{entry_info.get('player_first_name', '')} "
                            f"{entry_info.get('player_last_name', '')}".strip()
                        )
                        standings_with_names.append({
                            "rank": s.get("rank"),
                            "entry_id": entry_info.get("entry_id"),
                            "manager_name": manager_name or "Unknown",
                            "total": s.get("total", 0),
                            "event_total": s.get("event_total", 0),
                        })

                    # Best GW performers in draft league (top 3 + ties)
                    sorted_by_gw = sorted(standings_with_names, key=lambda x: x["event_total"], reverse=True)
                    _assign_tie_ranks(sorted_by_gw, "event_total")
                    best_gw_display = _slice_with_ties(sorted_by_gw, 3)

                    console.print("\n[bold]### Best GW Performers[/bold]")
                    for entry in best_gw_display:
                        name = entry["manager_name"]
                        gw_pts = entry["event_total"]
                        rank = entry["rank_str"]
                        is_user = entry["entry_id"] == draft_entry_id
                        if is_user:
                            console.print(f"  {rank}. [bold cyan]You[/bold cyan] - {gw_pts} pts")
                        else:
                            console.print(f"  {rank}. {name} - {gw_pts} pts")

                    # Snapshot best data and user GW rank before worst sort overwrites ranks
                    best_for_report = [
                        {"name": e["manager_name"], "points": e["event_total"], "rank_str": e["rank_str"]}
                        for e in best_gw_display
                    ]
                    user_entry = next(
                        (e for e in sorted_by_gw if e["entry_id"] == draft_entry_id),
                        None,
                    )
                    user_gw_rank = user_entry["rank_str"] if user_entry else None

                    # Worst performers: sorted ascending (bottom 3 + ties)
                    worst_sorted = sorted(standings_with_names, key=lambda x: x["event_total"])
                    _assign_tie_ranks(worst_sorted, "event_total")
                    worst_gw_display = _slice_with_ties(worst_sorted, 3)

                    console.print("\n[bold]### Worst GW Performers[/bold]")
                    for entry in worst_gw_display:
                        name = entry["manager_name"]
                        gw_pts = entry["event_total"]
                        rank = entry["rank_str"]
                        is_user = entry["entry_id"] == draft_entry_id
                        if is_user:
                            console.print(f"  {rank}. [bold cyan]You[/bold cyan] - {gw_pts} pts")
                        else:
                            console.print(f"  {rank}. {name} - {gw_pts} pts")

                    # Store for report (worst_performers sorted ascending - lowest first)
                    draft_league_data = {
                        "league_name": draft_league_name,
                        "user_position": user_rank,
                        "user_gw_rank": user_gw_rank,
                        "total_entries": total_entries,
                        "user_gw_points": user_gw_pts,
                        "user_total": user_total,
                        "best_performers": best_for_report,
                        "worst_performers": [
                            {
                                "name": e["manager_name"],
                                "points": e["event_total"],
                                "rank_str": e["rank_str"],
                                "is_user": e["entry_id"] == draft_entry_id,
                            }
                            for e in worst_gw_display
                        ],
                    }

            elif not draft_entry_id:
                console.print("[dim]Set draft_entry_id in config/settings.yaml to see your draft squad[/dim]")

    except Exception as e:  # noqa: BLE001 — display resilience
        console.print(f"[yellow]Could not fetch draft league data: {rich_escape(str(e))}[/yellow]")

    return {
        "draft_league_data": draft_league_data,
        "draft_league_name": draft_league_name,
        "draft_squad_points_data": draft_squad_points_data,
        "draft_transactions_data": draft_transactions_data,
        "draft_automatic_subs": draft_automatic_subs,
        "draft_player_map": draft_player_map,
    }
