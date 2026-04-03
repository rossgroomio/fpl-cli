"""Gameweek review command."""
# Pattern: via-agent

from __future__ import annotations

import asyncio
from pathlib import Path

import click
from rich.panel import Panel

from fpl_cli.cli._context import Format, console, error_console, get_format, load_settings, resolve_output_dir
from fpl_cli.cli._review_analysis import _review_fixtures, _review_global_stats, _review_league_table
from fpl_cli.cli._review_classic import _review_classic_league, _review_classic_team, _review_classic_transfers
from fpl_cli.cli._review_draft import _review_draft
from fpl_cli.cli._review_summarisation import _review_compare_recs, _review_llm_summarise
from fpl_cli.services.fixture_predictions import (
    FixturePredictionsService,
    find_blank_gameweeks,
    find_double_gameweeks,
)


async def _review_resolve_gw(client, gameweek):
    """Resolve which gameweek to review. Returns {gw, gw_data, api_current_gw_id} or None."""
    gameweeks = await client.get_gameweeks()
    current_gw = await client.get_current_gameweek()
    api_current_gw_id = current_gw["id"] if current_gw else None

    if gameweek is not None:
        gw_data = next((g for g in gameweeks if g["id"] == gameweek), None)
        if not gw_data:
            console.print(f"[red]Gameweek {gameweek} not found[/red]")
            return None
        if not gw_data.get("finished"):
            console.print(f"[red]Gameweek {gameweek} is not yet finished[/red]")
            console.print("Only completed gameweeks can be reviewed")
            return None
        gw = gameweek
    else:
        if current_gw and current_gw.get("finished"):
            gw = current_gw["id"]
        elif current_gw:
            gw = current_gw["id"] - 1
            if gw < 1:
                error_console.print("[yellow]No completed gameweeks yet[/yellow]")
                return None
        else:
            error_console.print("[yellow]Could not determine current gameweek[/yellow]")
            return None

        gw_data = next((g for g in gameweeks if g["id"] == gw), None)
        if not gw_data:
            console.print(f"[red]Gameweek {gw} not found[/red]")
            return None

        if not gw_data.get("finished"):
            error_console.print(f"[yellow]Gameweek {gw} is not yet finished[/yellow]")
            console.print("Use -g/--gameweek to specify a completed gameweek")
            return None

    return {"gw": gw, "gw_data": gw_data, "api_current_gw_id": api_current_gw_id}


@click.command("review")
@click.option("--gameweek", "-g", type=int, help="Specific gameweek to review (default: last completed)")
@click.option("--save", "-s", is_flag=True, help="Save report to output directory")
@click.option("--output", "-o", type=click.Path(), help="Custom output directory for report")
@click.option("--summarise", is_flag=True, help="Add LLM-generated summary (requires API keys)")
@click.option("--debug", is_flag=True, help="Save LLM prompts and responses to data/debug/")
@click.option("--dry-run", is_flag=True, help="Build and save prompts to data/debug/ without calling LLMs")
@click.option("--compare-recs", is_flag=True, help="Compare recommendations vs actual decisions")
@click.pass_context
def review_command(
    ctx: click.Context,
    gameweek: int | None, save: bool, output: str | None,
    summarise: bool, debug: bool, dry_run: bool, compare_recs: bool,
):
    """Review a completed gameweek - your squad's performance and league standings."""
    from fpl_cli.agents.orchestration.report import ReportAgent
    from fpl_cli.api.fpl import FPLClient

    fmt = get_format(ctx)
    show_classic = fmt != Format.DRAFT
    show_draft = fmt != Format.CLASSIC

    settings = load_settings()

    research_provider = None
    synthesis_provider = None

    # Resolve LLM providers if summarise or dry_run requested
    if summarise or dry_run:
        if not dry_run:
            from fpl_cli.api.providers import ProviderError, get_llm_provider

            try:
                research_provider = get_llm_provider("research", settings)
                synthesis_provider = get_llm_provider("synthesis", settings)
            except ProviderError as e:
                console.print(f"[red]Error: {e}[/red]")
                return
    entry_id = settings.get("fpl", {}).get("classic_entry_id")
    classic_league_id = settings.get("fpl", {}).get("classic_league_id")
    draft_league_id = settings.get("fpl", {}).get("draft_league_id")
    draft_entry_id = settings.get("fpl", {}).get("draft_entry_id")

    async def _review():
        from contextlib import AsyncExitStack

        async with AsyncExitStack() as stack:
            client = await stack.enter_async_context(FPLClient())
            if research_provider is not None:
                await stack.enter_async_context(research_provider)
            if synthesis_provider is not None:
                await stack.enter_async_context(synthesis_provider)

            gw_result = await _review_resolve_gw(client, gameweek)
            if gw_result is None:
                return
            gw = gw_result["gw"]
            gw_data = gw_result["gw_data"]
            api_current_gw_id = gw_result["api_current_gw_id"]

            console.print(Panel.fit(f"[bold blue]Gameweek {gw} Review[/bold blue]"))

            # Get all players and teams
            players = await client.get_players()
            player_map = {p.id: p for p in players}
            teams = {t.id: t for t in await client.get_teams()}

            # Fetch live GW stats once - shared by all _review_* helpers
            live_data = await client.get_gameweek_live(gw)
            live_stats = {e["id"]: e["stats"] for e in live_data.get("elements", [])}

            # Fetch fixtures early for BGW/DGW detection (reused by _review_fixtures later)
            raw_fixtures = await client.get_fixtures(gw)
            teams_list = list(teams.values())
            fixtures_by_gw = {gw: raw_fixtures}
            blank_gws = find_blank_gameweeks(fixtures_by_gw, teams_list, gw, gw)
            double_gws = find_double_gameweeks(fixtures_by_gw, teams_list, gw, gw)
            bgw_team_ids = frozenset(t["team_id"] for t in blank_gws.get(gw, []))
            dgw_team_ids = frozenset(t["team_id"] for t in double_gws.get(gw, []))

            # Classic section
            if show_classic:
                console.print("\n[bold cyan]# Classic[/bold cyan]")
                classic_team = await _review_classic_team(
                    client, entry_id, gw, player_map, teams, gw_data, live_stats,
                    bgw_team_ids=bgw_team_ids, dgw_team_ids=dgw_team_ids,
                )
                classic_transfers_data = await _review_classic_transfers(
                    client, entry_id, gw, player_map, teams, classic_team["team_points_data"], live_stats
                )
                classic_league_data = await _review_classic_league(
                    client, classic_league_id, entry_id, gw, api_current_gw_id,
                    use_net_points=settings.get("use_net_points", False),
                )
            else:
                # Must match return shape of _review_classic_team / _review_classic_transfers / _review_classic_league
                classic_team = {
                    "my_entry_summary": None, "active_chip": None,
                    "team_points_data": [], "my_picks_data": [],
                }
                classic_transfers_data = []
                classic_league_data = None

            # Global stats (BGW teams excluded from blankers)
            global_data = await _review_global_stats(
                client, gw, player_map, teams, live_stats,
                bgw_team_ids=bgw_team_ids,
            )
            # BGW/DGW team names for prompt formatting (derived at point of use)
            global_data["bgw_team_names"] = {teams[tid].short_name for tid in bgw_team_ids if tid in teams}
            global_data["dgw_team_names"] = {teams[tid].short_name for tid in dgw_team_ids if tid in teams}

            # Predicted future DGWs for prompt context
            pred_service = FixturePredictionsService()
            global_data["predicted_dgw_teams"] = pred_service.get_predicted_doubles(min_gw=gw + 1)

            # Draft section
            if show_draft:
                draft_result = await _review_draft(
                    client, draft_league_id, draft_entry_id, gw, api_current_gw_id,
                    players, player_map, teams, live_stats,
                    bgw_team_ids=bgw_team_ids, dgw_team_ids=dgw_team_ids,
                )
            else:
                # Must match return shape of _review_draft
                draft_result = {
                    "draft_squad_points_data": [], "draft_transactions_data": [],
                    "draft_league_data": None, "draft_automatic_subs": [],
                    "draft_player_map": {},
                }

            # Fixture results (reuses pre-fetched raw_fixtures to avoid second HTTP call)
            fixtures_data = await _review_fixtures(
                client, gw, player_map, teams, classic_team["my_picks_data"],
                fixtures=raw_fixtures,
            )

            # League table
            league_table_data = await _review_league_table()

            # Assemble collected data for report
            collected_data = {
                "points": {
                    "total": classic_team["my_entry_summary"]["points"] if classic_team["my_entry_summary"] else None,
                    "rank": classic_team["my_entry_summary"]["rank"] if classic_team["my_entry_summary"] else None,
                    "overall_rank": (
                        classic_team["my_entry_summary"]["overall_rank"]
                        if classic_team["my_entry_summary"] else None
                    ),
                    "highest": gw_data.get("highest_score"),
                    "average": gw_data.get("average_entry_score"),
                },
                "active_chip": classic_team["active_chip"],
                "team_points": classic_team["team_points_data"],
                "classic_transfers": classic_transfers_data,
                "classic_league": classic_league_data,
                "global_stats": global_data,
                "draft_squad_points": draft_result["draft_squad_points_data"],
                "draft_transactions": draft_result["draft_transactions_data"],
                "draft_league": draft_result["draft_league_data"],
                "fixtures": fixtures_data,
                "league_table": league_table_data,
                "fpl_format": str(fmt) if fmt else None,
            }

            # LLM summarisation if requested (or dry-run to preview prompts)
            if summarise or dry_run:
                llm = await _review_llm_summarise(
                    gw=gw,
                    gw_data=gw_data,
                    collected_data=collected_data,
                    classic_team=classic_team,
                    classic_transfers_data=classic_transfers_data,
                    classic_league_data=classic_league_data,
                    draft_result=draft_result,
                    global_data=global_data,
                    player_map=player_map,
                    teams=teams,
                    settings=settings,
                    dry_run=dry_run,
                    debug=debug,
                    research_provider=research_provider,
                    synthesis_provider=synthesis_provider,
                )
                collected_data["research_summary"] = llm["research_summary"]
                collected_data["synthesis_summary"] = llm["synthesis_summary"]

            # Compare recommendations vs actuals if requested
            if compare_recs:
                from fpl_cli.parsers.recommendations import parse_recommendations

                recs_dir = Path(output) if output else resolve_output_dir(settings)
                recs_path = recs_dir / f"gw{gw}-recommendations.md"
                recs = parse_recommendations(recs_path)

                if recs is None:
                    error_console.print(f"\n[yellow]No recommendations file found at {recs_path}[/yellow]")
                else:
                    recs_comparison = _review_compare_recs(recs, collected_data, player_map, teams)
                    collected_data["recs_comparison"] = recs_comparison

                    # Print comparison summary to console
                    console.print("\n" + "-" * 50)
                    console.print("\n[bold cyan]# Recommendations vs Actuals[/bold cyan]")

                    rc = recs_comparison["classic"]
                    if rc.get("rec_captain"):
                        if rc["captain_followed"]:
                            console.print(
                                f"  Captain: [green]✓[/green] "
                                f"{rc['rec_captain']} (followed)"
                            )
                        else:
                            delta = rc.get("captain_pts_delta", 0)
                            sign = "+" if delta > 0 else ""
                            style = "green" if delta > 0 else "red" if delta < 0 else ""
                            d = f"[{style}]{sign}{delta}[/{style}]" if style else str(delta)
                            console.print(
                                f"  Captain: [yellow]✗[/yellow] "
                                f"Rec {rc['rec_captain']} ({rc['rec_captain_pts']} pts)"
                                f" → Actual {rc['actual_captain']}"
                                f" ({rc['actual_captain_pts']} pts) [{d} delta]"
                            )

                    if rc.get("rec_roll") and rc.get("actual_roll"):
                        console.print("  Transfers: [green]✓[/green] Rolled (aligned)")
                    elif rc.get("rec_roll") and not rc.get("actual_roll"):
                        console.print(
                            "  Transfers: [yellow]✗[/yellow] Rec roll, but made transfers"
                        )
                    else:
                        for t in rc.get("transfers", []):
                            if t.get("followed"):
                                net = t.get("actual_net", 0)
                                console.print(
                                    f"  Transfer: [green]✓[/green] "
                                    f"{t['rec_in']} ← {t['rec_out']}"
                                    f" (followed, net {net})"
                                )
                            elif t.get("not_made"):
                                console.print(
                                    f"  Transfer: [yellow]✗[/yellow] "
                                    f"{t['rec_in']} ← {t['rec_out']} (not made)"
                                )
                            else:
                                console.print(
                                    f"  Transfer: [yellow]~[/yellow] "
                                    f"Sold {t['rec_out']} but got "
                                    f"{t.get('actual_in')} instead of "
                                    f"rec {t['rec_in']}"
                                )

                    for t in rc.get("unadvised_transfers", []):
                        console.print(
                            f"  Transfer: [dim]⚠ Unadvised:[/dim] "
                            f"{t['actual_in']} ← {t['actual_out']}"
                            f" (net {t.get('actual_net', 0)})"
                        )

                    rd = recs_comparison["draft"]
                    for w in rd.get("waivers", []):
                        p = w["priority"]
                        if w.get("followed"):
                            console.print(
                                f"  Waiver P{p}: [green]✓[/green] "
                                f"{w['rec_in']} ← {w['rec_out']}"
                                f" (followed, net {w.get('actual_net', 0)})"
                            )
                        elif w.get("not_executed"):
                            console.print(
                                f"  Waiver P{p}: [yellow]✗[/yellow] "
                                f"{w['rec_in']} ← {w['rec_out']}"
                                f" (not executed)"
                            )
                        else:
                            console.print(
                                f"  Waiver P{p}: [yellow]~[/yellow] "
                                f"Dropped {w['rec_out']} but got "
                                f"{w.get('actual_in')} instead of "
                                f"rec {w['rec_in']}"
                            )

                    for w in rd.get("unadvised_waivers", []):
                        console.print(
                            f"  Waiver: [dim]⚠ Unadvised:[/dim] "
                            f"{w['actual_in']} ← {w['actual_out']}"
                            f" (net {w.get('actual_net', 0)})"
                        )

            # Generate report if requested
            if save:
                output_dir = Path(output) if output else resolve_output_dir(settings)

                console.print("\n[dim]Generating report...[/dim]")
                async with ReportAgent(config={"output_dir": output_dir}) as report_agent:
                    report_result = await report_agent.run(context={
                        "report_type": "review",
                        "gameweek": gw,
                        "data": collected_data,
                    })

                if report_result.success:
                    console.print(f"[green]✓[/green] Report saved to: {report_result.data['report_path']}")
                else:
                    console.print(f"[red]✗[/red] Failed to save report: {report_result.message}")

    asyncio.run(_review())
