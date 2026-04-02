"""FPL league standings display."""
# Pattern: direct-api

from __future__ import annotations

import asyncio
import logging

import click
from rich.markup import escape as rich_escape
from rich.panel import Panel
from rich.table import Table

from fpl_cli.cli._context import Format, console, get_format, load_settings
from fpl_cli.cli._helpers import _fetch_standings_with_costs

logger = logging.getLogger(__name__)


@click.command("league")
@click.pass_context
def league_command(ctx: click.Context):
    """Show live league standings for Classic and Draft leagues.

    Displays the first 50 managers in each league (FPL API page limit).
    """
    from fpl_cli.api.fpl import FPLClient
    from fpl_cli.api.fpl_draft import FPLDraftClient

    fmt = get_format(ctx)
    settings = load_settings()
    entry_id = settings.get("fpl", {}).get("classic_entry_id")
    classic_league_id = settings.get("fpl", {}).get("classic_league_id")
    draft_league_id = settings.get("fpl", {}).get("draft_league_id")
    draft_entry_id = settings.get("fpl", {}).get("draft_entry_id")

    show_classic = fmt != Format.DRAFT
    show_draft = fmt != Format.CLASSIC

    async def _league():
        async with FPLClient() as client:
            # Get current GW status
            current_gw = await client.get_current_gameweek()
            if not current_gw:
                console.print("[yellow]Could not determine current gameweek[/yellow]")
                return

            gw = current_gw["id"]
            gw_status = "finished" if current_gw.get("finished") else "in progress"
            console.print(Panel.fit(f"[bold blue]League Standings - GW{gw} ({gw_status})[/bold blue]"))

            # Classic League
            if show_classic and classic_league_id and entry_id:
                console.print("\n[bold cyan]# Classic League[/bold cyan]")
                try:
                    standings_data = await client.get_classic_league_standings(classic_league_id)
                    league_name = standings_data.get("league", {}).get("name", "Classic League")
                    standings = standings_data.get("standings", {}).get("results", [])

                    # Find user's entry
                    user_entry = next((e for e in standings if e.get("entry") == entry_id), None)
                    if user_entry:
                        user_rank = user_entry.get("rank", "?")
                        user_total = user_entry.get("total", 0)
                        user_gw_pts = user_entry.get("event_total", 0)

                        console.print("\n[bold]## Summary[/bold]")
                        console.print(f"**{league_name}**")
                        console.print(f"- Position: {user_rank} of {len(standings)}")
                        console.print(f"- GW Points: {user_gw_pts}")
                        console.print(f"- Total Points: {user_total:,}")

                    # Full standings table
                    console.print("\n[bold]## Standings[/bold]")
                    table = Table(show_header=True, header_style="bold")
                    table.add_column("Pos", justify="right")
                    table.add_column("Manager")
                    table.add_column("GW", justify="right")
                    table.add_column("Total", justify="right")

                    for entry in standings:
                        rank = str(entry.get("rank", "?"))
                        name = entry.get("player_name", "Unknown")
                        gw_pts = entry.get("event_total", 0)
                        total = entry.get("total", 0)
                        is_user = entry.get("entry") == entry_id

                        if is_user:
                            table.add_row(
                                f"[bold cyan]{rank}[/bold cyan]",
                                f"[bold cyan]{name}[/bold cyan]",
                                f"[bold cyan]{gw_pts}[/bold cyan]",
                                f"[bold cyan]{total:,}[/bold cyan]",
                            )
                        else:
                            table.add_row(rank, name, str(gw_pts), f"{total:,}")

                    console.print(table)

                    use_net_points = settings.get("use_net_points", False)
                    standings_with_costs = await _fetch_standings_with_costs(
                        client, standings, entry_id, gw, fetch_costs=use_net_points,
                    )

                    # Best 3 GW performers
                    header_suffix = " (Net Points)" if use_net_points else ""
                    sorted_by_net_desc = sorted(standings_with_costs, key=lambda x: x["net_points"], reverse=True)
                    console.print(f"\n[bold]### Best GW Performers{header_suffix}[/bold]")
                    for i, perf in enumerate(sorted_by_net_desc[:3], 1):
                        name = perf["name"]
                        gross = perf["gross_points"]
                        cost = perf["transfer_cost"]
                        net = perf["net_points"]

                        if perf["is_user"]:
                            if cost > 0:
                                console.print(
                                    f"  {i}. [bold cyan]You[/bold cyan] - "
                                    f"{gross} gross, -{cost} hit = {net} net"
                                )
                            else:
                                console.print(f"  {i}. [bold cyan]You[/bold cyan] - {net} pts")
                        else:
                            if cost > 0:
                                console.print(f"  {i}. {name} - {gross} gross, -{cost} hit = {net} net")
                            else:
                                console.print(f"  {i}. {name} - {net} pts")

                    # Worst 5 GW performers
                    sorted_by_net_asc = sorted(standings_with_costs, key=lambda x: x["net_points"])

                    # Get bottom 5, plus user if not already included
                    worst_performers = sorted_by_net_asc[:5]
                    user_in_worst = any(p["is_user"] for p in worst_performers)
                    if not user_in_worst:
                        user_data = next((p for p in standings_with_costs if p["is_user"]), None)
                        if user_data:
                            worst_performers.append(user_data)

                    console.print(f"\n[bold]### Worst GW Performers{header_suffix}[/bold]")
                    for i, perf in enumerate(worst_performers[:5], 1):
                        name = perf["name"]
                        gross = perf["gross_points"]
                        cost = perf["transfer_cost"]
                        net = perf["net_points"]

                        if perf["is_user"]:
                            if cost > 0:
                                console.print(
                                    f"  {i}. [bold cyan]You[/bold cyan] - "
                                    f"{gross} gross, -{cost} hit = {net} net"
                                )
                            else:
                                console.print(f"  {i}. [bold cyan]You[/bold cyan] - {net} pts")
                        else:
                            if cost > 0:
                                console.print(f"  {i}. {name} - {gross} gross, -{cost} hit = {net} net")
                            else:
                                console.print(f"  {i}. {name} - {net} pts")

                except Exception as e:  # noqa: BLE001 — display resilience
                    console.print(f"[yellow]Could not fetch classic league: {rich_escape(str(e))}[/yellow]")

            elif show_classic and not classic_league_id:
                console.print("\n[dim]Set classic_league_id in config/settings.yaml for Classic league[/dim]")

        # Draft League
        if show_draft and draft_league_id:
            if show_classic:
                console.print("\n" + "-" * 50)
            console.print("\n[bold cyan]# Draft League[/bold cyan]")
            try:
                async with FPLDraftClient() as draft_client:
                    league_details = await draft_client.get_league_details(draft_league_id)
                league_name = league_details.get("league", {}).get("name", "Draft League")
                standings = league_details.get("standings", [])
                league_entries = league_details.get("league_entries", [])
                entry_map = {e.get("id"): e for e in league_entries}

                # Find user's entry
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

                    console.print("\n[bold]## Summary[/bold]")
                    console.print(f"**{league_name}**")
                    console.print(f"- Position: {user_rank} of {len(standings)}")
                    console.print(f"- GW Points: {user_gw_pts}")
                    console.print(f"- Total Points: {user_total:,}")

                # Build standings with manager names
                standings_with_names = []
                for s in standings:
                    entry_info = entry_map.get(s.get("league_entry"), {})
                    manager_name = (
                        f"{entry_info.get('player_first_name', '')} {entry_info.get('player_last_name', '')}".strip()
                    )
                    standings_with_names.append({
                        "rank": s.get("rank"),
                        "entry_id": entry_info.get("entry_id"),
                        "manager_name": manager_name or "Unknown",
                        "total": s.get("total", 0),
                        "event_total": s.get("event_total", 0),
                    })

                # Full standings table
                console.print("\n[bold]## Standings[/bold]")
                table = Table(show_header=True, header_style="bold")
                table.add_column("Pos", justify="right")
                table.add_column("Manager")
                table.add_column("GW", justify="right")
                table.add_column("Total", justify="right")

                for entry in standings_with_names:
                    rank = str(entry["rank"])
                    name = entry["manager_name"]
                    gw_pts = entry["event_total"]
                    total = entry["total"]
                    is_user = entry["entry_id"] == draft_entry_id

                    if is_user:
                        table.add_row(
                            f"[bold cyan]{rank}[/bold cyan]",
                            f"[bold cyan]{name}[/bold cyan]",
                            f"[bold cyan]{gw_pts}[/bold cyan]",
                            f"[bold cyan]{total:,}[/bold cyan]",
                        )
                    else:
                        table.add_row(rank, name, str(gw_pts), f"{total:,}")

                console.print(table)

                # Best 3 GW performers
                sorted_by_gw = sorted(standings_with_names, key=lambda x: x["event_total"], reverse=True)
                console.print("\n[bold]### Best GW Performers[/bold]")
                for i, entry in enumerate(sorted_by_gw[:3], 1):
                    name = entry["manager_name"]
                    gw_pts = entry["event_total"]
                    is_user = entry["entry_id"] == draft_entry_id
                    if is_user:
                        console.print(f"  {i}. [bold cyan]You[/bold cyan] - {gw_pts} pts")
                    else:
                        console.print(f"  {i}. {name} - {gw_pts} pts")

                # Worst 3 GW performers (no transfer costs in draft)
                worst_sorted = sorted(standings_with_names, key=lambda x: x["event_total"])[:3]
                console.print("\n[bold]### Worst GW Performers[/bold]")
                for i, entry in enumerate(worst_sorted, 1):
                    name = entry["manager_name"]
                    gw_pts = entry["event_total"]
                    is_user = entry["entry_id"] == draft_entry_id
                    if is_user:
                        console.print(f"  {i}. [bold cyan]You[/bold cyan] - {gw_pts} pts")
                    else:
                        console.print(f"  {i}. {name} - {gw_pts} pts")

            except Exception as e:  # noqa: BLE001 — display resilience
                console.print(f"[yellow]Could not fetch draft league: {rich_escape(str(e))}[/yellow]")

        elif show_draft and not draft_league_id:
            console.print("\n[dim]Set draft_league_id in config/settings.yaml for Draft league[/dim]")

    asyncio.run(_league())
