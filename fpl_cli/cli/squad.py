"""Squad command group: health analysis and fixture grid."""
# Pattern: via-agent

from __future__ import annotations

import asyncio

import click
from rich.panel import Panel

from fpl_cli.cli._context import CLIContext, Format, console, get_format, load_settings
from fpl_cli.cli._json import emit_json, emit_json_error, json_output_mode, output_format_option
from fpl_cli.cli._plan_grid import grid_command
from fpl_cli.cli.sell_prices import sell_prices_command


@click.group("squad", invoke_without_command=True, subcommand_metavar="[COMMAND] [ARGS]...")
@click.option("--draft", "is_draft", is_flag=True, default=False,
              help="Use draft squad (only needed when both formats are configured)")
@click.pass_context
@output_format_option
def squad_group(ctx: click.Context, is_draft: bool, output_format: str) -> None:
    """Analyze your FPL squad health and fixtures."""
    if ctx.invoked_subcommand is not None:
        return

    # Default behaviour: show squad health
    from fpl_cli.agents.analysis.squad_analyzer import SquadAnalyzerAgent
    from fpl_cli.agents.common import get_draft_squad_players

    settings = load_settings()
    fpl_cfg = settings.get("fpl", {})
    fmt = get_format(ctx)

    # Auto-select in single-format mode; respect --draft flag in BOTH mode
    if fmt == Format.DRAFT:
        is_draft = True
    elif fmt == Format.CLASSIC:
        is_draft = False

    entry_id = fpl_cfg.get("classic_entry_id")
    draft_entry_id = fpl_cfg.get("draft_entry_id")

    if is_draft:
        if not draft_entry_id:
            console.print(
                "[yellow]Please set draft_entry_id in config/settings.yaml[/yellow]"
            )
            return
    elif not entry_id:
        console.print(
            "[yellow]Please provide your entry ID via classic_entry_id"
            " in config/settings.yaml[/yellow]"
        )
        console.print(
            "Find it in your FPL URL: fantasy.premierleague.com/entry/[bold]ENTRY_ID[/bold]/event/..."
        )
        return

    async def _run() -> None:
        from fpl_cli.agents.common import get_actual_squad_picks
        from fpl_cli.api.fpl import FPLClient

        async with FPLClient() as client:
            all_players = await client.get_players()
            gw_data = await client.get_next_gameweek()
            gw = gw_data["id"] if gw_data else 1

            if is_draft:
                from fpl_cli.api.fpl_draft import FPLDraftClient

                async with FPLDraftClient() as draft_client:
                    squad_players = await get_draft_squad_players(
                        draft_client, all_players, draft_entry_id, gw,
                        log=lambda msg: console.print(f"[yellow]{msg}[/yellow]"),
                    )
                picks = [p.id for p in squad_players]
                context: dict = {"picks": picks, "format": "draft"}
            else:
                # Resolve picks here so the agent doesn't refetch bootstrap-static
                target_gw = max(gw - 1, 1)
                picks_data, _ = await get_actual_squad_picks(client, entry_id, target_gw)
                picks = [p["element"] for p in picks_data.get("picks", [])]
                context = {"picks": picks, "format": "classic"}

        if output_format == "json":
            with json_output_mode() as stdout:
                async with SquadAnalyzerAgent(config={"entry_id": entry_id}) as agent:
                    result = await agent.run(context=context)
                if not result.success:
                    emit_json_error("squad", result.message, file=stdout)
                    return
                emit_json("squad", result.data, metadata={
                    "gameweek": gw,
                    "format": "draft" if is_draft else "classic",
                }, file=stdout)
            return

        async with SquadAnalyzerAgent(config={"entry_id": entry_id}) as agent:
            result = await agent.run(context=context)

        if not result.success:
            console.print(f"[red]Agent failed: {result.message}[/red]")
            for error in result.errors:
                console.print(f"  [red]{error}[/red]")
            return

        _render(result.data, is_draft)

    asyncio.run(_run())


squad_group.add_command(sell_prices_command)


@squad_group.command("grid")
@click.option("--gws", "-n", type=int, default=6, help="Number of GWs to show (default: 6)")
@click.option("--watch", "-w", multiple=True, help="Additional player names to include (can repeat)")
@click.option("--mode", "-m", type=click.Choice(["difference", "opponent"]), default="difference",
              help="FDR mode: 'difference' (team vs opponent) or 'opponent' (opponent rating only)")
@click.option("--draft", "is_draft", is_flag=True, default=False,
              help="Use draft squad (only needed when both formats are configured)")
@output_format_option
@click.pass_context
def grid_subcommand(
    ctx: click.Context, gws: int, watch: tuple[str, ...], mode: str, is_draft: bool, output_format: str,
) -> None:
    """Show squad fixture difficulty grid."""
    fmt = ctx.obj.format if isinstance(ctx.obj, CLIContext) else None

    if fmt == Format.DRAFT:
        is_draft = True
    elif fmt == Format.CLASSIC:
        is_draft = False

    ctx.invoke(grid_command, gws=gws, watch=watch, mode=mode, is_draft=is_draft, output_format=output_format)


def _render(data: dict, is_draft: bool) -> None:
    """Render squad analysis to the console."""
    console.print(Panel.fit("[bold blue]Squad Analysis[/bold blue]"))

    overview = data["squad_overview"]
    console.print("\n[bold]Squad Overview:[/bold]")
    console.print(f"  Total Points: {overview['total_points']:,}")
    if not is_draft:
        console.print(f"  Team Value: £{overview['team_value']}m")
        console.print(f"  Bank: £{overview['bank']}m")
    console.print(f"  Average Form: {overview['average_form']}")

    # Position breakdown
    console.print("\n[bold]By Position:[/bold]")
    for pos, pos_data in data["position_analysis"].items():
        console.print(f"  {pos}: {pos_data['count']} players, avg form {pos_data['average_form']}")

    # Injury risks
    if data["injury_risks"]:
        console.print("\n[bold red]Injury/Availability Concerns:[/bold red]")
        for risk in data["injury_risks"]:
            chance = f"{risk['chance_of_playing']}%" if risk['chance_of_playing'] else "Unknown"
            console.print(f"  - {risk['name']} ({risk['team']}): {chance}")
            if risk["news"]:
                console.print(f"    [dim]{risk['news']}[/dim]")

    # Form analysis
    console.print("\n[bold green]In Form:[/bold green]")
    for p in data["form_analysis"]["in_form"][:3]:
        console.print(f"  - {p['name']} ({p['team']}) - Form: {p['form']}")

    console.print("\n[bold red]Out of Form:[/bold red]")
    for p in data["form_analysis"]["out_of_form"][:3]:
        console.print(f"  - {p['name']} ({p['team']}) - Form: {p['form']}")

    # Recommendations
    if data["recommendations"]:
        console.print("\n[bold]Recommendations:[/bold]")
        for rec in data["recommendations"][:5]:
            priority_style = (
                "red" if rec["priority"] == "high" else "yellow" if rec["priority"] == "medium" else "dim"
            )
            console.print(f"  [{priority_style}]\\[{rec['priority'].upper()}][/{priority_style}] {rec['message']}")
            console.print(f"    [dim]{rec['suggestion']}[/dim]")
