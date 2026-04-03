"""League recap command - entertainment-first GW report for all league participants."""
# Pattern: via-agent
# pyright: reportTypedDictNotRequiredAccess=false

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

import click
from rich.panel import Panel

from fpl_cli.api.providers import ProviderError
from fpl_cli.cli._context import Format, console, error_console, get_format, load_settings, resolve_output_dir
from fpl_cli.cli._league_recap_types import LeagueRecapData

logger = logging.getLogger(__name__)


@click.command("league-recap")
@click.option("--gameweek", "-g", type=int, help="Specific gameweek to recap (default: last completed)")
@click.option("--draft", "is_draft", is_flag=True, default=False,
              help="Use draft league (only needed when both formats are configured)")
@click.option("--save", "-s", is_flag=True, help="Save report to output directory")
@click.option("--output", "-o", type=click.Path(), help="Custom output directory for report")
@click.option("--summarise", is_flag=True, help="Add LLM-generated editorial narrative (requires API keys)")
@click.option("--debug", is_flag=True, help="Save LLM prompts and responses to data/debug/")
@click.option("--dry-run", is_flag=True, help="Build and save prompts to data/debug/ without calling LLMs")
@click.pass_context
def league_recap_command(
    ctx: click.Context,
    gameweek: int | None, is_draft: bool, save: bool, output: str | None,
    summarise: bool, debug: bool, dry_run: bool,
) -> None:
    """Recap a completed gameweek for the whole league - awards, standings, and banter."""
    from fpl_cli.agents.orchestration.report import ReportAgent
    from fpl_cli.api.fpl import FPLClient
    from fpl_cli.cli._league_recap_data import (
        collect_classic_recap_data,
        collect_draft_recap_data,
        evaluate_league_fines,
    )
    from fpl_cli.cli.review import _review_resolve_gw

    settings = load_settings()
    fmt = get_format(ctx)

    # Auto-select in single-format mode; respect --draft flag in BOTH mode
    if fmt == Format.DRAFT:
        is_draft = True
    elif fmt == Format.CLASSIC:
        is_draft = False

    synthesis_provider = None

    if summarise or dry_run:
        if not dry_run:
            from fpl_cli.api.providers import get_llm_provider

            try:
                synthesis_provider = get_llm_provider("synthesis", settings)
            except ProviderError as e:
                console.print(f"[red]Error: {e}[/red]")
                return

    async def _run() -> None:
        from contextlib import AsyncExitStack

        async with AsyncExitStack() as stack:
            client = await stack.enter_async_context(FPLClient())
            if synthesis_provider is not None:
                await stack.enter_async_context(synthesis_provider)
            # Resolve gameweek
            gw_result = await _review_resolve_gw(client, gameweek)
            if gw_result is None:
                return
            gw: int = gw_result["gw"]

            console.print(Panel.fit(f"[bold blue]Gameweek {gw} League Recap[/bold blue]"))

            # Fetch shared bootstrap data
            players = await client.get_players()
            player_map: dict[int, Any] = {p.id: p for p in players}
            teams = {t.id: t for t in await client.get_teams()}
            live_data = await client.get_gameweek_live(gw)
            live_stats = {e["id"]: e["stats"] for e in live_data.get("elements", [])}

            # Detect BGW/DGW
            raw_fixtures = await client.get_fixtures(gw)
            is_bgw = len(raw_fixtures) < 10
            is_dgw = len(raw_fixtures) > 10

            # Get next GW deadline
            from datetime import datetime, timedelta

            from fpl_cli.season import TOTAL_GAMEWEEKS

            gameweeks = await client.get_gameweeks()
            next_gw_data = next((g for g in gameweeks if g["id"] == gw + 1), None)
            next_deadline = None
            waiver_deadline = None
            if next_gw_data and next_gw_data.get("deadline_time"):
                raw = next_gw_data["deadline_time"]
                try:
                    dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
                    # Format as UK time (UTC for now, consistent with FPL)
                    next_deadline = dt.strftime("%a %d %b, %H:%M UTC")
                    # Waiver deadline is 24h before GW deadline
                    waiver_dt = dt - timedelta(hours=24)
                    waiver_deadline = waiver_dt.strftime("%a %d %b, %H:%M UTC")
                except (ValueError, AttributeError):
                    next_deadline = raw

            # Collect format-specific data
            if is_draft:
                collected_data = await collect_draft_recap_data(
                    settings=settings, gw=gw, live_stats=live_stats,
                    players=players, teams=teams,
                )
            else:
                collected_data = await collect_classic_recap_data(
                    client=client, settings=settings, gw=gw,
                    live_stats=live_stats, player_map=player_map, teams=teams,
                )

            # Add context metadata
            collected_data["is_bgw"] = is_bgw  # type: ignore[typeddict-unknown-key]
            collected_data["is_dgw"] = is_dgw  # type: ignore[typeddict-unknown-key]
            collected_data["season_length"] = TOTAL_GAMEWEEKS  # type: ignore[typeddict-unknown-key]
            if next_deadline:
                collected_data["next_deadline"] = next_deadline  # type: ignore[typeddict-unknown-key]

            if is_draft and waiver_deadline:
                collected_data["waiver_deadline"] = waiver_deadline  # type: ignore[typeddict-unknown-key]

            # Evaluate fines per manager (graceful skip when unconfigured)
            fines = evaluate_league_fines(
                collected_data["managers"], settings, collected_data["fpl_format"],
            )
            if fines:
                collected_data["fines"] = fines

            # LLM summarisation (opt-in via --summarise or --dry-run)
            if summarise or dry_run:
                try:
                    await _recap_llm_summarise(
                        collected_data, gw,
                        synthesis_provider=synthesis_provider,
                        dry_run=dry_run, debug=debug,
                        is_bgw=is_bgw, is_dgw=is_dgw,
                        season_length=TOTAL_GAMEWEEKS,
                    )
                except ProviderError as e:
                    error_console.print(f"[yellow]LLM summarisation failed: {e}[/yellow]")
                except Exception:  # noqa: BLE001 — graceful degradation
                    logger.debug("LLM summarisation failed", exc_info=True)
                    error_console.print("[yellow]LLM summarisation failed (unexpected error)[/yellow]")

            # Display key highlights to console
            _render_console_highlights(collected_data)

            # Generate report if saving
            if save or output:
                output_dir = output or str(resolve_output_dir(settings))
                agent = ReportAgent(config={"output_dir": output_dir})
                result = await agent.run(context={
                    "report_type": "league-recap",
                    "gameweek": gw,
                    "data": dict(collected_data),
                })
                if result.data and result.data.get("report_path"):
                    console.print(f"\n[green]Report saved to {result.data['report_path']}[/green]")

    asyncio.run(_run())


def _render_console_highlights(data: LeagueRecapData) -> None:
    """Print key recap highlights to console."""
    awards = data.get("awards", {})
    managers = data.get("managers", [])
    fmt = data.get("fpl_format", "classic")

    console.print(f"\n[bold]{data.get('league_name', 'League')}[/bold] - GW{data.get('gameweek')}")
    console.print(f"[dim]{len(managers)} managers[/dim]\n")

    if awards.get("gw_winner"):
        console.print(f"[green]GW Winner:[/green] {awards['gw_winner']['detail']}")
    if awards.get("gw_loser"):
        console.print(f"[red]GW Loser:[/red] {awards['gw_loser']['detail']}")
    if awards.get("biggest_bench_haul"):
        console.print(f"[yellow]Biggest Bench:[/yellow] {awards['biggest_bench_haul']['detail']}")
    if fmt == "classic" and awards.get("best_captain"):
        console.print(f"[green]Best Captain:[/green] {awards['best_captain']['detail']}")
    if fmt == "classic" and awards.get("worst_captain"):
        console.print(f"[red]Worst Captain:[/red] {awards['worst_captain']['detail']}")

    if fmt == "classic" and awards.get("transfer_genius"):
        console.print(f"[green]Transfer Genius:[/green] {awards['transfer_genius']['detail']}")
    if fmt == "classic" and awards.get("transfer_disaster"):
        console.print(f"[red]Transfer Disaster:[/red] {awards['transfer_disaster']['detail']}")
    if fmt == "draft" and awards.get("waiver_genius"):
        console.print(f"[green]Waiver Genius:[/green] {awards['waiver_genius']['detail']}")
    if fmt == "draft" and awards.get("waiver_disaster"):
        console.print(f"[red]Waiver Disaster:[/red] {awards['waiver_disaster']['detail']}")

    # Fines
    fines = data.get("fines", [])
    if fines:
        console.print("\n[bold]Fines:[/bold]")
        for f in fines:
            console.print(f"  [red]{f['manager_name']}:[/red] {f['message']}")

    # Standings movement
    movers = [m for m in managers if m.get("previous_rank", 0) != m.get("overall_rank", 0)]
    if movers:
        console.print("\n[bold]Standings Movement:[/bold]")
        for m in sorted(movers, key=lambda x: x.get("previous_rank", 0) - x.get("overall_rank", 0)):
            prev = m["previous_rank"]
            curr = m["overall_rank"]
            diff = prev - curr
            arrow = "[green]↑[/green]" if diff > 0 else "[red]↓[/red]"
            console.print(f"  {arrow} {m['manager_name']}: {prev} → {curr}")


async def _recap_llm_summarise(
    collected_data: LeagueRecapData,
    gw: int,
    *,
    synthesis_provider: Any = None,
    dry_run: bool = False,
    debug: bool = False,
    is_bgw: bool = False,
    is_dgw: bool = False,
    season_length: int = 38,
) -> None:
    """Run LLM summarisation for league recap. Mutates collected_data to add summaries."""
    from fpl_cli.prompts.league_recap import (
        format_recap_awards_context,
        format_recap_fines_context,
        format_recap_standings_context,
        get_recap_synthesis_prompt,
    )

    # Setup debug directory
    debug_dir = None
    if debug or dry_run:
        debug_dir = Path("data/debug")
        debug_dir.mkdir(parents=True, exist_ok=True)

    awards_text = format_recap_awards_context(collected_data)
    standings_text = format_recap_standings_context(collected_data)
    fines_text = format_recap_fines_context(collected_data)

    system_prompt, user_prompt = get_recap_synthesis_prompt(
        gw=gw,
        league_name=collected_data["league_name"],
        fpl_format=collected_data["fpl_format"],
        awards_text=awards_text,
        standings_text=standings_text,
        fines_text=fines_text,
        is_bgw=is_bgw,
        is_dgw=is_dgw,
        season_length=season_length,
    )

    if dry_run:
        console.print("[dim]  Dry run: saving prompts without calling LLMs...[/dim]")
        if debug_dir:
            (debug_dir / "recap_system.txt").write_text(system_prompt, encoding="utf-8")
            (debug_dir / "recap_prompt.txt").write_text(user_prompt, encoding="utf-8")
            console.print("[dim]    Saved recap_system.txt, recap_prompt.txt[/dim]")
        collected_data["synthesis_summary"] = "[DRY RUN - synthesis provider not called]"
    elif synthesis_provider:
        try:
            console.print("[dim]  Generating league editorial...[/dim]")
            synthesis_result = await synthesis_provider.query(
                prompt=user_prompt,
                system_prompt=system_prompt,
            )
            collected_data["synthesis_summary"] = synthesis_provider.post_process(synthesis_result.content)
            console.print("[green]  Done[/green] League editorial complete")
        except ProviderError as e:
            error_console.print(f"[yellow]  LLM synthesis failed: {e}[/yellow]")
        except Exception:  # noqa: BLE001 — graceful degradation
            logger.debug("Synthesis provider failed for recap", exc_info=True)
            error_console.print("[yellow]  LLM synthesis failed (unexpected error)[/yellow]")

    if debug and debug_dir:
        (debug_dir / "recap_system.txt").write_text(system_prompt, encoding="utf-8")
        (debug_dir / "recap_prompt.txt").write_text(user_prompt, encoding="utf-8")
