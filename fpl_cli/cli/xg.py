"""Underlying stats analysis command (xG, xA)."""
# Pattern: via-agent

from __future__ import annotations

import asyncio

import click
from rich.panel import Panel
from rich.table import Table

from fpl_cli.cli._context import CLIContext, console, is_custom_analysis_enabled
from fpl_cli.cli._json import emit_json, emit_json_error, json_output_mode, output_format_option


@click.command("xg")
@click.option("--last-n", "-n", type=int, default=6, help="Number of gameweeks to analyze (default: 6)")
@click.option("--all", "all_season", is_flag=True, help="Analyze whole season instead of recent gameweeks")
@output_format_option
@click.pass_context
def xg_command(ctx: click.Context, last_n: int, all_season: bool, output_format: str):
    """Analyse underlying stats: xG, xA, overperformers."""
    from fpl_cli.agents.analysis.stats import StatsAgent

    # --all flag overrides last_n to None (whole season)
    gw_config = None if all_season else last_n

    # Gate experimental views behind custom_analysis toggle
    settings = ctx.obj.settings if isinstance(ctx.obj, CLIContext) else {}
    custom_on = is_custom_analysis_enabled(settings)

    if custom_on:
        table_views = {"underperformers", "value_picks", "top_xgi_per_90"}
        json_views = {"underperformers", "overperformers", "value_picks", "top_xgi_per_90"}
    else:
        table_views = {"underperformers", "top_xgi_per_90"}
        json_views = {"underperformers", "top_xgi_per_90"}

    async def _run():
        if output_format == "json":
            with json_output_mode() as stdout:
                async with StatsAgent(config={
                    "gameweeks": gw_config,
                    "views": json_views,
                }) as agent:
                    result = await agent.run()
                if not result.success:
                    emit_json_error("xg", result.message, file=stdout)
                    return
                emit_json("xg", result.data, metadata={
                    "window": gw_config if gw_config is not None else "all",
                    "custom_analysis": custom_on,
                }, file=stdout)
            return

        async with StatsAgent(config={
            "gameweeks": gw_config,
            "views": table_views,
        }) as agent:
            result = await agent.run()

        if not result.success:
            console.print(f"[red]Agent failed: {result.message}[/red]")
            for error in result.errors:
                console.print(f"  [red]{error}[/red]")
            return

        data = result.data
        window_label = data.get("window_label", "whole season")
        console.print(Panel.fit(f"[bold blue]Underlying Stats Analysis[/bold blue] ({window_label})"))

        # Top xGI per 90
        console.print("\n[bold]Top xGI per 90 (xG + xA):[/bold]")
        table = Table(show_header=True, header_style="bold")
        table.add_column("Player")
        table.add_column("Team")
        table.add_column("xG", justify="right")
        table.add_column("xA", justify="right")
        table.add_column("xGI/90", justify="right")
        table.add_column("Goals", justify="right")
        table.add_column("Assists", justify="right")

        for p in data["top_xgi_per_90"][:10]:
            table.add_row(
                p["player_name"],
                p["team_short"],
                f"{p['xG']:.2f}",
                f"{p['xA']:.2f}",
                f"[bold]{p['xGI_per_90']:.2f}[/bold]",
                str(p["goals"]),
                str(p["assists"]),
            )
        console.print(table)

        # Underperformers
        if data["underperformers"]:
            console.print("\n[bold green]Underperformers (G+A < xGI, due a rise):[/bold green]")
            table = Table(show_header=True, header_style="bold")
            table.add_column("Player")
            table.add_column("Team")
            table.add_column("G+A", justify="right")
            table.add_column("xGI", justify="right")
            table.add_column("Diff", justify="right")

            for p in data["underperformers"][:8]:
                table.add_row(
                    p["player_name"],
                    p["team_short"],
                    str(p["GI"]),
                    f"{p['xGI']:.2f}",
                    f"[green]{p['difference']:.2f}[/green]",
                )
            console.print(table)

        # Value picks
        if data["value_picks"]:
            console.print("\n[bold cyan]Value Picks (high xGI, low ownership):[/bold cyan]")
            table = Table(show_header=True, header_style="bold")
            table.add_column("Player")
            table.add_column("Team")
            table.add_column("Price", justify="right")
            table.add_column("Own%", justify="right")
            table.add_column("xGI/90", justify="right")

            for p in data["value_picks"][:8]:
                table.add_row(
                    p["player_name"],
                    p["team_short"],
                    f"£{p['price']:.1f}m",
                    f"{p['ownership']:.1f}%",
                    f"[bold]{p['xGI_per_90']:.2f}[/bold]",
                )
            console.print(table)

    asyncio.run(_run())
