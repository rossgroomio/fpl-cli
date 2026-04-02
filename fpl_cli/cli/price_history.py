"""Season-long price trajectory and transfer momentum."""
# Pattern: direct-api

from __future__ import annotations

import asyncio

import click
from rich.table import Table

from fpl_cli.cli._context import console
from fpl_cli.cli._helpers import _validate_team_filter
from fpl_cli.cli._json import emit_json, emit_json_error, json_output_mode, output_format_option

PRICE_HISTORY_SORT_FIELDS = [
    "price_change", "price_slope", "price_acceleration",
    "transfer_momentum", "price_current",
]


@click.command("price-history")
@click.option("--position", "-p", type=click.Choice(["GK", "DEF", "MID", "FWD"], case_sensitive=False),
              default=None, help="Filter by position")
@click.option("--team", "-t", default=None, help="Filter by team short name (e.g. ARS)")
@click.option("--sort", "-s", "sort_field", type=click.Choice(PRICE_HISTORY_SORT_FIELDS, case_sensitive=False),
              default="price_change", help="Field to sort by")
@click.option("--limit", "-l", type=click.IntRange(min=1, max=100), default=30, help="Number of results")
@click.option("--last-n", "-n", type=click.IntRange(min=4), default=None,
              help="Scope metrics to last N gameweeks (min: 4, default: full season)")
@click.option("--reverse", "-r", is_flag=True, help="Sort ascending instead of descending")
@output_format_option
def price_history_command(
    position: str | None, team: str | None, sort_field: str,
    limit: int, last_n: int | None, reverse: bool, output_format: str,
):
    """Show price trajectory and transfer momentum."""
    from fpl_cli.api.fpl import FPLClient
    from fpl_cli.api.vaastav import VaastavClient

    async def _run():
        import httpx

        async with FPLClient() as fpl_client, VaastavClient() as vaastav:
            # Fetch all data in parallel - GW trends and FPL metadata are independent
            try:
                current_gw_data, gw_trends, all_players, all_teams = (
                    await asyncio.gather(
                        fpl_client.get_current_gameweek(),
                        vaastav.get_gw_trends(last_n=last_n),
                        fpl_client.get_players(),
                        fpl_client.get_teams(),
                    )
                )
            except httpx.HTTPError as e:
                if output_format == "json":
                    with json_output_mode() as stdout:
                        emit_json_error("price-history", "Failed to fetch price history data", file=stdout)
                    return
                console.print(f"[red]Failed to fetch price history: {e}[/red]")
                raise SystemExit(1)

            current_gw = current_gw_data["id"] if current_gw_data else 0

            if not gw_trends:
                if output_format == "json":
                    with json_output_mode() as stdout:
                        emit_json("price-history", [], metadata={
                            "latest_gw": 0, "current_gw": current_gw, "is_stale": True,
                            "window_used": last_n,
                        }, file=stdout)
                    return
                console.print("[yellow]No price history data available.[/yellow]")
                return

            latest_gw = max(t.latest_gw for t in gw_trends.values())
            is_stale = (current_gw - latest_gw) > 3 if current_gw > 0 else False
            team_map = {t.id: t.short_name for t in all_teams}
            player_map = {p.id: p for p in all_players}

            # Build display records
            records = []
            for element, trend in gw_trends.items():
                fpl_player = player_map.get(element)
                team_name = trend.team_name  # Vaastav uses full team name
                pos = trend.position

                # Use FPL API data if available (short name, consistent position)
                if fpl_player:
                    pos = fpl_player.position_name
                    team_name = team_map.get(fpl_player.team_id, team_name)

                records.append({
                    "element": element,
                    "web_name": fpl_player.web_name if fpl_player else trend.web_name,
                    "position": pos,
                    "team": team_name,
                    "first_gw": trend.first_gw,
                    "price_start": trend.price_start,
                    "price_current": (
                        trend.price_current if not is_stale
                        else (fpl_player.now_cost if fpl_player else trend.price_current)
                    ),
                    "price_change": (
                        trend.price_change if not is_stale
                        else (fpl_player.cost_change_start if fpl_player else trend.price_change)
                    ),
                    "price_slope": trend.price_slope if not is_stale else None,
                    "price_acceleration": trend.price_acceleration if not is_stale else None,
                    "transfer_momentum": trend.transfer_momentum if not is_stale else None,
                    "gw_count": trend.gw_count,
                    "latest_gw": trend.latest_gw,
                })

            # Filter
            team_upper = _validate_team_filter(team, all_teams)
            if position:
                records = [r for r in records if r["position"] == position.upper()]
            if team_upper:
                records = [r for r in records if r["team"].upper() == team_upper]

            # Sort
            def sort_key(r):
                val = r.get(sort_field)
                if val is None:
                    return float("-inf")
                return val

            records.sort(key=sort_key, reverse=not reverse)
            records = records[:limit]

            if not records:
                if output_format == "json":
                    with json_output_mode() as stdout:
                        emit_json("price-history", [], metadata={
                            "latest_gw": latest_gw, "current_gw": current_gw, "is_stale": is_stale,
                            "window_used": last_n,
                        }, file=stdout)
                    return
                console.print("[yellow]No players match the given filters.[/yellow]")
                return

            if output_format == "json":
                with json_output_mode() as stdout:
                    emit_json("price-history", records, metadata={
                        "latest_gw": latest_gw, "current_gw": current_gw, "is_stale": is_stale,
                        "window_used": last_n,
                    }, file=stdout)
                return

            # Table output
            if last_n is not None:
                console.print(
                    f"[dim]Metrics scoped to last {last_n} GWs[/dim]\n"
                )

            if is_stale:
                console.print(
                    f"[yellow]Warning: Price history data covers GW1-{latest_gw} "
                    f"(current: GW{current_gw}). Showing live price change only.[/yellow]\n"
                )

            first_gw = min(r["first_gw"] for r in records)
            arrow = " \u25b2" if reverse else " \u25bc"
            table = Table(show_header=True, header_style="bold")
            table.add_column("Player")
            table.add_column("Pos", justify="center")
            table.add_column("Team", justify="center")
            table.add_column(f"GW{first_gw}", justify="right")
            table.add_column("Now", justify="right")

            change_header = "+/-"
            if sort_field == "price_change":
                change_header += arrow
            table.add_column(change_header, justify="right")

            if not is_stale:
                slope_header = "Trend"
                if sort_field == "price_slope":
                    slope_header += arrow
                table.add_column(slope_header, justify="right")

                accel_header = "Accel"
                if sort_field == "price_acceleration":
                    accel_header += arrow
                table.add_column(accel_header, justify="right")

                momentum_header = "Net Transfers" if last_n is not None else "Momentum"
                if sort_field == "transfer_momentum":
                    momentum_header += arrow
                table.add_column(momentum_header, justify="right")

            for r in records:
                change = r["price_change"]
                change_str = f"{change / 10:+.1f}"
                change_style = "green" if change > 0 else "red" if change < 0 else ""
                change_cell = f"[{change_style}]{change_str}[/{change_style}]" if change_style else change_str

                row = [
                    r["web_name"],
                    r["position"],
                    r["team"],
                    f"\u00a3{r['price_start'] / 10:.1f}m",
                    f"\u00a3{r['price_current'] / 10:.1f}m",
                    change_cell,
                ]

                if not is_stale:
                    slope = r["price_slope"]
                    row.append(f"{slope:+.2f}" if slope is not None else "-")

                    accel = r["price_acceleration"]
                    row.append(f"{accel:+.2f}" if accel is not None else "-")

                    momentum = r["transfer_momentum"]
                    if momentum is not None:
                        mom_str = f"{momentum:+,}"
                        mom_style = "green" if momentum > 0 else "red" if momentum < 0 else ""
                        row.append(f"[{mom_style}]{mom_str}[/{mom_style}]" if mom_style else mom_str)
                    else:
                        row.append("-")

                table.add_row(*row)

            console.print(table)

    asyncio.run(_run())
