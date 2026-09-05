"""Squad command group: health analysis and fixture grid."""
# Pattern: via-agent

from __future__ import annotations

import asyncio

import click
import httpx
from rich.panel import Panel

from fpl_cli.cli._context import (
    Format,
    console,
    error_console,
    get_format,
    get_settings,
    handle_agent_failure,
)
from fpl_cli.cli._helpers import require_entry_id
from fpl_cli.cli._json import (
    api_failure_boundary,
    emit_failure,
    emit_json,
    emit_json_error,
    json_output_mode,
    output_format_option,
)
from fpl_cli.cli._plan_grid import COMMAND as GRID_COMMAND
from fpl_cli.cli._plan_grid import grid_command
from fpl_cli.cli.sell_prices import sell_prices_command


def _resolve_is_draft(
    fmt: Format | None,
    *,
    is_draft: bool,
    is_classic: bool,
    command: str,
    output_format: str,
) -> bool:
    """Whether to read the draft squad, an explicit flag beating the configured format.

    `--draft` on its own gave no way to say "I meant classic". With only the
    draft IDs configured -- which is also what a config that lost its
    `classic_entry_id` looks like -- the format resolved to DRAFT, and `fpl
    squad --format json` answered with a different league's roster and exit 0.
    A consumer only found out by reading `metadata.format`, if it thought to
    (#228). `--classic` pins the request, so the absent ID comes back as the
    error envelope `require_entry_id` already produces.

    Neither flag still auto-selects in single-format mode: that is what lets a
    draft-only manager run `fpl squad` unadorned, and why both flags are
    documented as needed only when both formats are configured.
    """
    if is_draft and is_classic:
        emit_failure(
            command,
            "--draft and --classic are mutually exclusive: pass one, or neither to "
            "use the format your settings.yaml configures.",
            output_format,
        )
    if is_draft or is_classic:
        return is_draft
    return fmt == Format.DRAFT


@click.group("squad", invoke_without_command=True, subcommand_metavar="[COMMAND] [ARGS]...")
@click.option("--draft", "is_draft", is_flag=True, default=False,
              help="Use draft squad (only needed when both formats are configured)")
@click.option("--classic", "is_classic", is_flag=True, default=False,
              help="Use classic squad (only needed when both formats are configured)")
@click.pass_context
@output_format_option
def squad_group(ctx: click.Context, is_draft: bool, is_classic: bool, output_format: str) -> None:
    """Analyze your FPL squad health and fixtures."""
    if ctx.invoked_subcommand is not None:
        return

    # Default behaviour: show squad health
    from fpl_cli.agents.analysis.squad_analyzer import SquadAnalyzerAgent
    from fpl_cli.agents.common import get_draft_squad_players

    settings = get_settings(ctx)
    fmt = get_format(ctx)

    # Auto-select in single-format mode; respect --draft/--classic otherwise
    is_draft = _resolve_is_draft(
        fmt, is_draft=is_draft, is_classic=is_classic,
        command="squad", output_format=output_format,
    )

    # A missing entry ID is a failure, not a quiet no-op: it used to print a
    # hint and return 0, so a script saw success and an empty stdout (#144).
    entry_id: int | None = None
    draft_entry_id: int | None = None
    if is_draft:
        draft_entry_id = require_entry_id(
            settings, is_draft=True, command="squad", output_format=output_format,
        )
    else:
        entry_id = require_entry_id(
            settings, is_draft=False, command="squad", output_format=output_format,
        )

    def _report_no_squad(message: str) -> None:
        if output_format == "json":
            with json_output_mode() as stdout:
                emit_json_error("squad", message, file=stdout)
            return
        error_console.print(f"[yellow]{message}[/yellow]")
        raise SystemExit(1) from None

    async def _run() -> None:
        from fpl_cli.agents.common import (
            NO_SQUAD_YET,
            SquadPicksUnavailableError,
            get_own_squad_picks,
        )
        from fpl_cli.api.fpl import FPLClient

        async with FPLClient() as client:
            all_players = await client.get_players()
            gw_data = await client.get_next_gameweek()
            gw = gw_data["id"] if gw_data else 1

            if is_draft:
                from fpl_cli.api.fpl_draft import FPLDraftClient

                assert draft_entry_id is not None
                async with FPLDraftClient() as draft_client:
                    try:
                        squad_players = await get_draft_squad_players(
                            draft_client, all_players, draft_entry_id, gw,
                            log=lambda msg: error_console.print(f"[yellow]{msg}[/yellow]"),
                        )
                    except httpx.HTTPStatusError as exc:
                        if exc.response.status_code != 404:
                            raise
                        _report_no_squad(NO_SQUAD_YET.format(gameweek=gw))
                        return
                picks = [p.id for p in squad_players]
                context: dict = {"picks": picks, "format": "draft"}
            else:
                # Resolve picks here so the agent doesn't refetch bootstrap-static
                target_gw = max(gw - 1, 1)
                assert entry_id is not None
                try:
                    picks_data, _ = await get_own_squad_picks(client, entry_id, target_gw)
                except SquadPicksUnavailableError as exc:
                    _report_no_squad(str(exc))
                    return
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
            handle_agent_failure(result)

        _render(result.data, is_draft)

    with api_failure_boundary("squad", output_format):
        asyncio.run(_run())


squad_group.add_command(sell_prices_command)


@squad_group.command("grid")
@click.option("--gws", "-n", type=int, default=6, help="Number of GWs to show (default: 6)")
@click.option("--watch", "-w", multiple=True, help="Additional player names to include (can repeat)")
@click.option("--mode", "-m", type=click.Choice(["difference", "opponent"]), default="difference",
              help="FDR mode: 'difference' (team vs opponent) or 'opponent' (opponent rating only)")
@click.option("--draft", "is_draft", is_flag=True, default=False,
              help="Use draft squad (only needed when both formats are configured)")
@click.option("--classic", "is_classic", is_flag=True, default=False,
              help="Use classic squad (only needed when both formats are configured)")
@output_format_option
@click.pass_context
def grid_subcommand(
    ctx: click.Context, gws: int, watch: tuple[str, ...], mode: str,
    is_draft: bool, is_classic: bool, output_format: str,
) -> None:
    """Show squad fixture difficulty grid."""
    is_draft = _resolve_is_draft(
        get_format(ctx), is_draft=is_draft, is_classic=is_classic,
        command=GRID_COMMAND, output_format=output_format,
    )

    ctx.invoke(grid_command, gws=gws, watch=watch, mode=mode, is_draft=is_draft, output_format=output_format)


def _render(data: dict, is_draft: bool) -> None:
    """Render squad analysis to the console."""
    # Name the format in the heading. `metadata.format` already told a JSON
    # consumer which roster it got; the table said nothing, so a single-format
    # auto-selection was invisible to the one reader who cannot query for it
    # (#228). The absent Team Value / Bank rows are a hint, not an answer.
    console.print(
        Panel.fit(f"[bold blue]Squad Analysis[/bold blue] ({'Draft' if is_draft else 'Classic'})")
    )

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
