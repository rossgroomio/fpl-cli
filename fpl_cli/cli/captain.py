"""Captain analysis command."""
# Pattern: via-agent

from __future__ import annotations

import asyncio

import click
from rich.panel import Panel
from rich.table import Table

from fpl_cli.cli._context import (
    console,
    get_settings,
    handle_agent_failure,
    print_result_warnings,
    split_result_warnings,
)
from fpl_cli.cli._helpers import FDR_EASY, FDR_MEDIUM, require_entry_id
from fpl_cli.cli._json import emit_json, emit_json_error, json_output_mode, output_format_option


@click.command("captain")
@click.option("--global", "-g", "global_mode", is_flag=True, help="Show global captain picks instead of your squad")
@output_format_option
@click.pass_context
def captain_command(ctx: click.Context, global_mode: bool, output_format: str) -> None:
    """Analyze and rank captain options for next gameweek.

    \b
    Examples:
      fpl captain
      fpl captain --global --format json
    """
    from fpl_cli.agents.analysis.captain import CaptainAgent

    settings = get_settings(ctx)
    # An absent classic_entry_id used to leave `entry_id` None, which is the
    # global path -- so `fpl captain` answered with the league's top 30 and
    # exit 0, and only the table mode's dim tip said the squad was never
    # consulted (#228). `--global` is how you ask for that list on purpose.
    entry_id = None if global_mode else require_entry_id(
        settings, is_draft=False, command="captain", output_format=output_format,
        alternative="Or pass --global to rank the top captain options across the game.",
    )

    async def _run() -> None:
        if output_format == "json":
            with json_output_mode() as stdout:
                async with CaptainAgent() as agent:
                    context = {"entry_id": entry_id} if entry_id else None
                    result = await agent.run(context)
                if not result.success:
                    emit_json_error("captain", result.message, file=stdout)
                    return
                payload, warnings = split_result_warnings(result.data)
                emit_json("captain", payload, metadata={
                    "gameweek": payload.get("gameweek"),
                    # Which list this is. Asking for your squad and getting the
                    # global one back is the failure #228 describes, and a
                    # consumer should not have to dig through `data` to notice.
                    "my_squad_mode": payload.get("my_squad_mode", False),
                    "warnings": warnings,
                }, file=stdout)
            return

        async with CaptainAgent() as agent:
            context = {"entry_id": entry_id} if entry_id else None
            result = await agent.run(context)

        if not result.success:
            handle_agent_failure(result)

        data = result.data
        print_result_warnings(data)
        mode_label = "Your Squad" if data.get("my_squad_mode") else "Global Top Players"
        console.print(Panel.fit(f"[bold blue]Captain Picks - GW{data['gameweek']}[/bold blue] ({mode_label})"))

        if data.get("deadline"):
            console.print(f"Deadline: [cyan]{data['deadline']}[/cyan]\n")

        # Top captain picks
        console.print("[bold]Top Captain Options:[/bold]")
        table = Table(show_header=True, header_style="bold")
        table.add_column("#", justify="center")
        table.add_column("Player")
        table.add_column("Team")
        table.add_column("Score", justify="right")
        table.add_column("Atk", justify="right")
        table.add_column("Def", justify="right")
        table.add_column("Form±", justify="right")
        table.add_column("Pos±", justify="right")
        table.add_column("Fixture")

        for i, p in enumerate(data["top_picks"][:10], 1):
            # Format fixture info
            fixture_str = ", ".join(
                f["opponent"].upper() if f["is_home"] else f["opponent"].lower()
                for f in p["fixtures"]
            )
            fdr_style = "green" if p["avg_fdr"] <= FDR_EASY else "yellow" if p["avg_fdr"] <= FDR_MEDIUM else "red"

            # Style matchup scores
            atk = p.get("attack_matchup", 5.0)
            def_ = p.get("defence_matchup", 5.0)
            form_diff = p.get("form_differential", 0.0)
            pos_diff = p.get("position_differential", 0.0)

            atk_style = "green" if atk >= 7 else "yellow" if atk >= 5 else "red"
            def_style = "green" if def_ >= 7 else "yellow" if def_ >= 5 else "red"
            form_style = "green" if form_diff >= 0.2 else "red" if form_diff <= -0.2 else ""
            pos_style = "green" if pos_diff >= 0.2 else "red" if pos_diff <= -0.2 else ""

            rank_style = "bold green" if i == 1 else "bold yellow" if i == 2 else ""

            table.add_row(
                f"[{rank_style}]{i}[/{rank_style}]" if rank_style else str(i),
                p["player_name"],
                p["team_short"],
                f"[bold]{p['captain_score']:.1f}[/bold]",
                f"[{atk_style}]{atk:.1f}[/{atk_style}]",
                f"[{def_style}]{def_:.1f}[/{def_style}]",
                f"[{form_style}]{form_diff:+.2f}[/{form_style}]" if form_style else f"{form_diff:+.2f}",
                f"[{pos_style}]{pos_diff:+.2f}[/{pos_style}]" if pos_style else f"{pos_diff:+.2f}",
                f"[{fdr_style}]{fixture_str}[/{fdr_style}]",
            )
        console.print(table)

        # Show reasoning for top pick
        if data["top_picks"]:
            top = data["top_picks"][0]
            console.print(f"\n[bold green]Recommended Captain: {top['player_name']}[/bold green]")
            console.print("Reasons:")
            for reason in top["reasons"]:
                console.print(f"  - {reason}")

    asyncio.run(_run())
