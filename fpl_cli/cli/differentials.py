"""Differential picks command."""
# Pattern: via-agent

from __future__ import annotations

import asyncio
import logging

import click
from rich.panel import Panel
from rich.table import Table

from fpl_cli.cli._context import console
from fpl_cli.cli._json import emit_json, emit_json_error, json_output_mode, output_format_option

logger = logging.getLogger(__name__)


@click.command("differentials")
@click.option("--threshold", "-t", type=float, default=5.0, help="Ownership threshold for elite differentials")
@click.option("--min-minutes", "-m", type=int, default=60, help="Minimum minutes played (default: 60)")
@output_format_option
def differentials_command(threshold: float, min_minutes: int, output_format: str):
    """Find differential picks - high potential, low ownership players."""
    from fpl_cli.agents.analysis.stats import StatsAgent

    async def _run():
        if output_format == "json":
            with json_output_mode() as stdout:
                from fpl_cli.api.fpl import FPLClient

                async with FPLClient() as client:
                    next_gw = await client.get_next_gameweek()
                gameweek = next_gw["id"] if next_gw else None

                async with StatsAgent(config={
                    "differential_threshold": threshold,
                    "semi_differential_threshold": 15.0,
                    "min_minutes": min_minutes,
                    "gameweeks": None,
                    "views": {"differentials"},
                }) as agent:
                    result = await agent.run()

                if not result.success:
                    emit_json_error("differentials", result.message, file=stdout)
                    return

                combined: dict = {"differentials": result.data.get("differentials", {})}

                try:
                    from fpl_cli.agents.analysis.captain import CaptainAgent

                    async with CaptainAgent(config={"differential_threshold": 10.0}) as captain_agent:
                        captain_result = await captain_agent.run()
                    if captain_result.success:
                        combined["differential_captains"] = captain_result.data.get("differential_picks", [])
                except Exception:  # noqa: BLE001 — CaptainAgent failure is graceful
                    logger.debug("CaptainAgent failed in differentials JSON", exc_info=True)

                emit_json("differentials", combined, metadata={"gameweek": gameweek}, file=stdout)
            return

        async with StatsAgent(config={
            "differential_threshold": threshold,
            "semi_differential_threshold": 15.0,
            "min_minutes": min_minutes,
            "gameweeks": None,  # Use whole season for differentials
            "views": {"differentials"},
        }) as agent:
            result = await agent.run()

        if not result.success:
            console.print(f"[red]Agent failed: {result.message}[/red]")
            for error in result.errors:
                console.print(f"  [red]{error}[/red]")
            return

        data = result.data
        differentials = data.get("differentials", {})

        console.print(Panel.fit(f"[bold blue]Differential Picks (<{threshold}% owned)[/bold blue]"))

        # Elite differentials
        elite = differentials.get("elite", [])
        if elite:
            console.print(f"\n[bold green]Elite Differentials (<{threshold}% ownership):[/bold green]")
            table = Table(show_header=True, header_style="bold")
            table.add_column("Player")
            table.add_column("Team")
            table.add_column("Pos")
            table.add_column("Own%", justify="right")
            table.add_column("xGI/90", justify="right")
            table.add_column("Matchup", justify="right")
            table.add_column("vs Next")
            table.add_column("Score", justify="right")

            for p in elite[:12]:
                own_style = "green" if p["ownership"] < 2 else "cyan"

                # Style matchup score
                matchup = p.get("matchup_score", 5.0)
                matchup_style = "green" if matchup >= 7 else "yellow" if matchup >= 5 else "red"
                next_opp = p.get("next_opponent") or "-"

                table.add_row(
                    p["player_name"],
                    p["team_short"],
                    p["position"],
                    f"[{own_style}]{p['ownership']:.1f}%[/{own_style}]",
                    f"{p['xGI_per_90']:.2f}",
                    f"[{matchup_style}]{matchup:.1f}[/{matchup_style}]",
                    next_opp,
                    f"[bold]{p['differential_score']:.1f}[/bold]",
                )
            console.print(table)

        # By position
        by_position = differentials.get("by_position", {})
        console.print("\n[bold]Top Differentials by Position:[/bold]")

        for pos in ["FWD", "MID", "DEF", "GK"]:
            players = by_position.get(pos, [])[:3]
            if players:
                names = ", ".join(
                    f"{p['player_name']} ({p['ownership']:.1f}%)"
                    for p in players
                )
                console.print(f"  [bold]{pos}:[/bold] {names}")

        # Differential captain picks
        console.print("\n[bold cyan]Differential Captain Options:[/bold cyan]")
        from fpl_cli.agents.analysis.captain import CaptainAgent

        async with CaptainAgent(config={"differential_threshold": 10.0}) as captain_agent:
            captain_result = await captain_agent.run()

        if captain_result.success:
            diff_captains = captain_result.data.get("differential_picks", [])
            if diff_captains:
                table = Table(show_header=True, header_style="bold")
                table.add_column("Player")
                table.add_column("Team")
                table.add_column("Own%", justify="right")
                table.add_column("Fixture")
                table.add_column("Score", justify="right")

                for p in diff_captains[:5]:
                    fixture_str = ", ".join(
                        f["opponent"].upper() if f["is_home"] else f["opponent"].lower()
                        for f in p.get("fixtures", [])
                    )
                    table.add_row(
                        p["player_name"],
                        p["team_short"],
                        f"[green]{p['ownership']:.1f}%[/green]",
                        fixture_str,
                        f"{p['captain_score']:.1f}",
                    )
                console.print(table)
            else:
                console.print("  [dim]No viable differential captain picks this week[/dim]")

    asyncio.run(_run())
