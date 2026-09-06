"""FPL draft waiver recommendations."""
# Pattern: via-agent

from __future__ import annotations

import asyncio

import click
from rich.panel import Panel
from rich.table import Table

from fpl_cli.cli._context import (
    console,
    error_console,
    fpl_config,
    get_settings,
    handle_agent_failure,
    print_result_warnings,
    split_result_warnings,
)
from fpl_cli.cli._json import emit_json, emit_json_error, json_output_mode, output_format_option


@click.command("waivers")
@output_format_option
@click.pass_context
def waivers_command(ctx: click.Context, output_format: str) -> None:
    """Show waiver recommendations for your draft league."""
    from fpl_cli.agents.action.waiver import WaiverAgent

    settings = get_settings(ctx)
    fpl_cfg = fpl_config(settings)
    league_id = fpl_cfg.get("draft_league_id")
    entry_id = fpl_cfg.get("draft_entry_id")

    if not league_id:
        if output_format == "json":
            with json_output_mode() as stdout:
                emit_json_error("waivers", "No draft_league_id configured in settings.yaml", file=stdout)
            return
        error_console.print(
            "[yellow]No draft_league_id configured in settings.yaml -- Add your league ID to settings.yaml[/yellow]"
        )
        return

    async def _run() -> None:
        if output_format == "json":
            with json_output_mode() as stdout:
                async with WaiverAgent(config={
                    "draft_league_id": league_id,
                    "draft_entry_id": entry_id,
                }) as agent:
                    result = await agent.run()
                if not result.success:
                    emit_json_error("waivers", result.message, file=stdout)
                    return
                payload, warnings = split_result_warnings(result.data)
                emit_json("waivers", payload, metadata={
                    "format": "draft",
                    "warnings": warnings,
                }, file=stdout)
            return

        async with WaiverAgent(config={
            "draft_league_id": league_id,
            "draft_entry_id": entry_id,
        }) as agent:
            result = await agent.run()

        if not result.success:
            handle_agent_failure(result)

        data = result.data
        print_result_warnings(data)
        console.print(Panel.fit("[bold blue]Draft Waiver Recommendations[/bold blue]"))

        # Waiver position
        if data.get("waiver_position"):
            pos = data["waiver_position"]
            total = data.get("total_waiver_teams", 0)
            style = "green" if pos <= 3 else "yellow" if pos <= 6 else ""
            console.print(f"Your waiver position: [{style}]{pos}/{total}[/{style}]\n")

        # Squad weaknesses
        if data.get("squad_weaknesses"):
            console.print("[bold]Squad Weaknesses:[/bold]")
            for weakness in data["squad_weaknesses"]:
                severity_style = "red" if weakness["severity"] == "high" else "yellow"
                console.print(f"  [{severity_style}]{weakness['position']}[/{severity_style}]: {weakness['reason']}")
            console.print("")

        # Top recommendations
        if data.get("recommendations"):
            console.print("[bold]Waiver Recommendations:[/bold]")
            table = Table(show_header=True, header_style="bold")
            table.add_column("#", justify="center")
            table.add_column("Target")
            table.add_column("Team")
            table.add_column("Pos")
            table.add_column("Form", justify="right")
            table.add_column("Avail", justify="right")
            table.add_column("Con", justify="right")
            table.add_column("Drop")
            table.add_column("Reasons")

            exposure_warnings = []
            for rec in data["recommendations"]:
                target = rec["target"]
                drop = rec.get("drop")
                if drop and drop.get("name"):
                    reason = drop.get("reason", f"{drop['form']:.1f}")
                    drop_str = f"{drop['name']} ({reason})"
                else:
                    drop_str = "-"

                rel = target.get("reliability")
                rel_str = f"{rel:.0%}" if rel is not None else "-"
                con = target.get("cv_xgi_percentile")
                con_str = str(round(con * 100)) if con is not None else "-"

                table.add_row(
                    str(rec["priority"]),
                    target["name"],
                    target["team"],
                    target["position"],
                    f"{target['form']:.1f}",
                    rel_str,
                    con_str,
                    drop_str,
                    ", ".join(rec["reasons"][:2]),
                )

                # Collect exposure warnings
                if rec.get("exposure", {}).get("warning"):
                    exposure_warnings.append((target["name"], rec["exposure"]["warning"]))

            console.print(table)

            # Display exposure warnings
            if exposure_warnings:
                console.print("")
                for name, warning in exposure_warnings:
                    error_console.print(f"  [yellow]\u26a0 {name}: {warning}[/yellow]")

            console.print("")

        # Recently released players
        if data.get("recent_releases"):
            console.print("[bold]Recently Released:[/bold]")
            for release in data["recent_releases"]:
                availability = release.get("availability", "\u2713")
                injury = release.get("injury_news", "")
                status_str = f" [red]({availability})[/red]" if availability != "\u2713" else ""
                injury_str = f" - {injury}" if injury else ""
                dropped_by = release.get("dropped_by", "")
                dropped_str = f" [dim](dropped by {dropped_by})[/dim]" if dropped_by else ""
                console.print(
                    f"  GW{release.get('gameweek', '?')}: {release.get('player_name')} ({release.get('team_short')}) "
                    f"- Form: {release.get('form', 0):.1f}{status_str}{injury_str}{dropped_str}"
                )
            console.print("")

        # Top available by position
        console.print("[bold]Top Available by Position:[/bold]")
        for pos in ["FWD", "MID", "DEF", "GK"]:
            players = data.get("targets_by_position", {}).get(pos, [])[:3]
            if players:
                names = ", ".join(
                    f"{p['player_name']} ({p.get('team_short', '???')}, {p['form']:.1f})"
                    for p in players
                )
                console.print(f"  [bold]{pos}:[/bold] {names}")

    asyncio.run(_run())
