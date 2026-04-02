"""Transfer targets command."""
# Pattern: via-agent

from __future__ import annotations

import asyncio

import click
from rich.panel import Panel
from rich.table import Table

from fpl_cli.cli._context import console
from fpl_cli.cli._json import emit_json, emit_json_error, json_output_mode, output_format_option


@click.command("targets")
@click.option("--min-own", "-o", type=float, default=0, help="Minimum ownership % (default: 0)")
@click.option("--min-minutes", "-m", type=int, default=60, help="Minimum minutes played (default: 60)")
@output_format_option
def targets_command(min_own: float, min_minutes: int, output_format: str):
    """Find transfer targets - high performers across all ownership levels."""
    from fpl_cli.agents.analysis.stats import StatsAgent

    async def _run():
        if output_format == "json":
            with json_output_mode() as stdout:
                async with StatsAgent(config={
                    "min_minutes": min_minutes,
                    "gameweeks": None,
                    "views": {"targets"},
                }) as agent:
                    result = await agent.run()
                if not result.success:
                    emit_json_error("targets", result.message, file=stdout)
                    return
                emit_json("targets", result.data, metadata={}, file=stdout)
            return

        async with StatsAgent(config={
            "min_minutes": min_minutes,
            "gameweeks": None,  # Use whole season
            "views": {"targets"},
        }) as agent:
            result = await agent.run()

        if not result.success:
            console.print(f"[red]Agent failed: {result.message}[/red]")
            for error in result.errors:
                console.print(f"  [red]{error}[/red]")
            return

        data = result.data
        targets = data.get("targets", {})
        window_label = data.get("window_label", "whole season")

        # Apply min ownership filter if specified
        all_targets = targets.get("all", [])
        if min_own > 0:
            all_targets = [p for p in all_targets if p["ownership"] >= min_own]
            console.print(Panel.fit(f"[bold blue]Transfer Targets[/bold blue] ({window_label}, >={min_own}% owned)"))
        else:
            console.print(Panel.fit(f"[bold blue]Transfer Targets[/bold blue] ({window_label})"))

        # Top performers table
        if all_targets:
            console.print("\n[bold]Top Performers:[/bold]")
            table = Table(show_header=True, header_style="bold")
            table.add_column("Player")
            table.add_column("Team")
            table.add_column("Pos")
            table.add_column("Own%", justify="right")
            table.add_column("xGI/90", justify="right")
            table.add_column("Matchup", justify="right")
            table.add_column("vs Next")
            table.add_column("Score", justify="right")

            for p in all_targets[:15]:
                # Color ownership by tier
                own = p["ownership"]
                if own >= 30:
                    own_style = "red"  # Template
                elif own >= 15:
                    own_style = "yellow"  # Popular
                else:
                    own_style = "green"  # Differential

                # Style matchup score
                matchup = p.get("matchup_score", 5.0)
                matchup_style = "green" if matchup >= 7 else "yellow" if matchup >= 5 else "red"
                next_opp = p.get("next_opponent") or "-"

                table.add_row(
                    p["player_name"],
                    p["team_short"],
                    p["position"],
                    f"[{own_style}]{own:.1f}%[/{own_style}]",
                    f"{p['xGI_per_90']:.2f}",
                    f"[{matchup_style}]{matchup:.1f}[/{matchup_style}]",
                    next_opp,
                    f"[bold]{p['target_score']:.1f}[/bold]",
                )
            console.print(table)

        # By ownership tier
        by_tier = targets.get("by_tier", {})

        template = by_tier.get("template", [])
        if template and min_own <= 30:
            console.print("\n[bold red]Template Players (>30% owned):[/bold red]")
            for pos in ["FWD", "MID", "DEF", "GK"]:
                players = [p for p in template if p["position"] == pos][:3]
                if players:
                    names = ", ".join(f"{p['player_name']} ({p['ownership']:.1f}%)" for p in players)
                    console.print(f"  [bold]{pos}:[/bold] {names}")

        popular = by_tier.get("popular", [])
        if popular and min_own <= 15:
            console.print("\n[bold yellow]Popular Picks (15-30% owned):[/bold yellow]")
            for pos in ["FWD", "MID", "DEF", "GK"]:
                players = [p for p in popular if p["position"] == pos][:3]
                if players:
                    names = ", ".join(f"{p['player_name']} ({p['ownership']:.1f}%)" for p in players)
                    console.print(f"  [bold]{pos}:[/bold] {names}")

        differential = by_tier.get("differential", [])
        if differential and min_own == 0:
            console.print("\n[bold green]Differentials (<15% owned):[/bold green]")
            for pos in ["FWD", "MID", "DEF", "GK"]:
                players = [p for p in differential if p["position"] == pos][:3]
                if players:
                    names = ", ".join(f"{p['player_name']} ({p['ownership']:.1f}%)" for p in players)
                    console.print(f"  [bold]{pos}:[/bold] {names}")

    asyncio.run(_run())
