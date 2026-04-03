"""Pre-gameweek preview command."""
# Keep display sections in sync with agents/orchestration/report.py
# Pattern: via-agent

from __future__ import annotations

import asyncio

import click
from rich.panel import Panel
from rich.table import Table

from fpl_cli.cli._context import (
    Format,
    console,
    error_console,
    get_format,
    is_custom_analysis_enabled,
    load_settings,
    resolve_output_dir,
    resolve_research_dir,
)
from fpl_cli.cli._helpers import _fdr_style
from fpl_cli.models.player import POSITION_MAP


def _preview_build_fixture_map(gw_fixtures: list[dict]) -> dict[str, str]:
    fixture_lists: dict[str, list[str]] = {}
    for fix in gw_fixtures:
        home, away = fix["home_team"], fix["away_team"]
        fixture_lists.setdefault(home, []).append(away.upper())
        fixture_lists.setdefault(away, []).append(home.lower())
    return {t: ", ".join(fs) for t, fs in fixture_lists.items()}


@click.command("preview")
@click.option("--save", "-s", is_flag=True, help="Save report to output directory")
@click.option("--output", "-o", type=click.Path(), help="Custom output directory for report")
@click.option("--scout", is_flag=True, help="Run deep research for BUY/SELL analysis")
@click.option("--dry-run", is_flag=True, help="Build scout prompts and save to data/debug/ without calling LLM")
@click.pass_context
def preview_command(ctx: click.Context, save: bool, output: str | None, scout: bool, dry_run: bool):
    """Run full pre-gameweek analysis and generate report."""
    from datetime import datetime
    from pathlib import Path

    from fpl_cli.agents.analysis.stats import StatsAgent
    from fpl_cli.agents.common import get_actual_squad_picks
    from fpl_cli.agents.data.fixture import FixtureAgent
    from fpl_cli.agents.data.price import PriceAgent
    from fpl_cli.agents.orchestration.report import ReportAgent
    from fpl_cli.api.fpl import FPLClient
    from fpl_cli.api.fpl_draft import FPLDraftClient

    fmt = get_format(ctx)
    show_classic = fmt != Format.DRAFT
    show_draft = fmt != Format.CLASSIC

    # Early check for research provider if --scout is used (not needed for dry-run)
    if scout and not dry_run:
        from fpl_cli.api.providers import ProviderError, get_llm_provider

        try:
            get_llm_provider("research", load_settings())
        except ProviderError as e:
            console.print(f"[red]Error:[/red] {e}")
            return

    settings = load_settings()
    entry_id = settings.get("fpl", {}).get("classic_entry_id")
    draft_league_id = settings.get("fpl", {}).get("draft_league_id")
    draft_entry_id = settings.get("fpl", {}).get("draft_entry_id")

    async def _preview():
        console.print(Panel.fit("[bold blue]Pre-Gameweek Preview[/bold blue]"))

        # Get gameweek info
        async with FPLClient() as client:
            next_gw = await client.get_next_gameweek()
            if not next_gw:
                console.print("[red]Could not determine next gameweek[/red]")
                return

            gw = next_gw["id"]
            deadline = next_gw.get("deadline_time", "Unknown")
            generated_at = datetime.now().strftime("%Y-%m-%d %H:%M")
            console.print(f"Generated: [dim]{generated_at}[/dim]")
            console.print(f"Deadline: [cyan]{deadline}[/cyan]\n")

            # Get all players, teams, and fixtures for common use
            all_players = await client.get_players()
            player_map = {p.id: p for p in all_players}
            teams = await client.get_teams()
            team_map = {t.id: t for t in teams}

            # Collect data from agents
            collected_data = {
                "deadline": deadline,
                "generated_at": generated_at,
            }

            # 1. Fixture Analysis (includes team form)
            console.print("[dim]Running FixtureAgent...[/dim]")
            async with FixtureAgent() as fixture_agent:
                fixture_result = await fixture_agent.run()
            if fixture_result.success:
                collected_data["fixtures"] = fixture_result.data
                console.print("[green]✓[/green] Fixture analysis complete")
            else:
                error_console.print(f"[yellow]⚠[/yellow] Fixture analysis: {fixture_result.message}")

            # Get gameweek fixtures (for display)
            gw_fixtures = await client.get_fixtures(gameweek=gw)
            collected_data["gw_fixtures"] = [
                {
                    "home_team": team_map[f.home_team_id].short_name if f.home_team_id in team_map else "???",
                    "home_fdr": f.home_difficulty,
                    "away_team": team_map[f.away_team_id].short_name if f.away_team_id in team_map else "???",
                    "away_fdr": f.away_difficulty,
                    "kickoff": f.kickoff_time.strftime("%a %H:%M") if f.kickoff_time else "TBC",
                }
                for f in gw_fixtures
            ]

            # Build team → fixture string map (handles DGW/BGW)
            team_fixture_map = _preview_build_fixture_map(collected_data["gw_fixtures"])

            # 2. Stats Analysis
            console.print("[dim]Running StatsAgent...[/dim]")
            async with StatsAgent() as stats_agent:
                stats_result = await stats_agent.run()
            if stats_result.success:
                collected_data["stats"] = stats_result.data
                console.print("[green]✓[/green] Stats analysis complete")
            else:
                error_console.print(f"[yellow]⚠[/yellow] Stats analysis: {stats_result.message}")

            # 3. Price Analysis
            console.print("[dim]Running PriceAgent...[/dim]")
            async with PriceAgent() as price_agent:
                price_result = await price_agent.run()
            if price_result.success:
                collected_data["prices"] = price_result.data
                console.print("[green]✓[/green] Price analysis complete")
            else:
                error_console.print(f"[yellow]⚠[/yellow] Price analysis: {price_result.message}")

            # 4. Main FPL squad with injury status
            if show_classic and entry_id:
                console.print("[dim]Fetching your FPL squad...[/dim]")
                try:
                    # Get picks from latest completed gameweek, checking for Free Hit
                    last_gw = gw - 1 if gw > 1 else None
                    if last_gw:
                        picks_data, last_gw = await get_actual_squad_picks(
                            client, entry_id, last_gw
                        )

                        pick_ids = [p["element"] for p in picks_data.get("picks", [])]
                        my_squad = []
                        for pid in pick_ids:
                            p = player_map.get(pid)
                            if p:
                                # Determine status display
                                if p.chance_of_playing_next_round is None or p.chance_of_playing_next_round == 100:
                                    status = "✓"
                                else:
                                    status = f"{p.chance_of_playing_next_round}%"
                                    if p.news:
                                        status += f" - {p.news[:30]}"
                                team_short = team_map[p.team_id].short_name if p.team_id in team_map else "???"
                                my_squad.append({
                                    "name": p.web_name,
                                    "team": team_short,
                                    "fixture": team_fixture_map.get(team_short, "—"),
                                    "position": p.position_name,
                                    "form": p.form,
                                    "ownership": p.selected_by_percent,
                                    "status": status,
                                })
                        collected_data["my_squad"] = my_squad
                        console.print("[green]✓[/green] FPL squad fetched")
                except Exception as e:  # noqa: BLE001 — display resilience
                    error_console.print(f"[yellow]⚠[/yellow] Could not fetch FPL squad: {e}")

            # 5. Draft squad with injury status
            if show_draft and draft_league_id and draft_entry_id:
                console.print("[dim]Fetching your draft squad...[/dim]")
                try:
                    async with FPLDraftClient() as draft_client:
                        draft_bootstrap = await draft_client.get_bootstrap_static()
                        draft_players = {dp["id"]: dp for dp in draft_bootstrap.get("elements", [])}
                        draft_teams = {dt["id"]: dt for dt in draft_bootstrap.get("teams", [])}

                        # Get my squad from last completed gameweek
                        last_gw = gw - 1 if gw > 1 else 1
                        picks_response = await draft_client.get_entry_picks(draft_entry_id, last_gw)
                    picks = picks_response.get("picks", [])
                    # O(1) lookup for draft→main FPL player mapping
                    main_by_name_team = {(p.web_name, p.team_id): p for p in all_players}
                    draft_squad = []
                    for pick in picks:
                        dp = draft_players.get(pick["element"])
                        if dp:
                            dt = draft_teams.get(dp.get("team"))
                            main_player = main_by_name_team.get((dp.get("web_name"), dp.get("team")))

                            if main_player:
                                cop = main_player.chance_of_playing_next_round
                                if cop is None or cop == 100:
                                    status = "✓"
                                else:
                                    status = f"{main_player.chance_of_playing_next_round}%"
                                    if main_player.news:
                                        status += f" - {main_player.news[:30]}"
                            else:
                                status = "✓"

                            team_short = dt.get("short_name", "???") if dt else "???"
                            draft_squad.append({
                                "name": dp.get("web_name", "???"),
                                "team": team_short,
                                "fixture": team_fixture_map.get(team_short, "—"),
                                "position": POSITION_MAP.get(dp.get("element_type"), "???"),
                                "form": float(dp.get("form", 0)),
                                "status": status,
                            })
                    collected_data["draft_squad"] = draft_squad
                    if draft_squad:
                        console.print("[green]✓[/green] Draft squad fetched")
                    else:
                        error_console.print("[yellow]⚠[/yellow] Draft squad fetched but no players found")
                except Exception as e:  # noqa: BLE001 — display resilience
                    error_console.print(f"[yellow]⚠[/yellow] Could not fetch draft squad: {e}")

        console.print("")

        # Display summary
        custom_on = is_custom_analysis_enabled(settings)
        _display_preview_summary(collected_data, gw, custom_on=custom_on)

        # Generate report if requested
        if save:
            output_dir = Path(output) if output else resolve_output_dir(settings)

            console.print("\n[dim]Generating report...[/dim]")
            async with ReportAgent(config={"output_dir": output_dir}) as report_agent:
                report_result = await report_agent.run(context={
                    "report_type": "preview",
                    "gameweek": gw,
                    "data": collected_data,
                })

            if report_result.success:
                console.print(f"[green]✓[/green] Report saved to: {report_result.data['report_path']}")
            else:
                console.print(f"[red]✗[/red] Failed to save report: {report_result.message}")

        # Run scout analysis if requested (or dry-run to preview prompts)
        if scout or dry_run:
            from fpl_cli.prompts.scout import SCOUT_SYSTEM_PROMPT, build_scout_user_prompt

            if dry_run:
                # Dry run: save prompts without calling API
                console.print("\n[dim]Dry run: building scout prompts...[/dim]")
                debug_dir = Path("data/debug")
                debug_dir.mkdir(parents=True, exist_ok=True)

                # Build position reference for dry-run prompt (reuse data from main fetch)
                position_reference = ""
                unavailable_players = ""
                try:
                    from fpl_cli.agents.data.scout import ScoutAgent

                    scout_team_map = {t.id: t.short_name for t in teams}
                    async with ScoutAgent() as scout_agent:
                        position_reference = scout_agent.build_position_reference(all_players, scout_team_map)
                        unavailable_players = scout_agent.build_unavailable_list(all_players, scout_team_map)
                except Exception as e:  # noqa: BLE001 — best-effort enrichment
                    console.print(f"[dim]  Warning: Could not fetch player positions: {e}[/dim]")

                scout_user_prompt = build_scout_user_prompt(gw, position_reference, unavailable_players)
                (debug_dir / "scout_system.txt").write_text(SCOUT_SYSTEM_PROMPT, encoding="utf-8")
                (debug_dir / "scout_prompt.txt").write_text(scout_user_prompt, encoding="utf-8")
                console.print(f"[dim]  Debug output → {debug_dir}/[/dim]")
                console.print("[dim]    → Saved scout_system.txt, scout_prompt.txt[/dim]")
                console.print("[green]  ✓[/green] Scout prompts saved to data/debug/")
            elif scout:
                console.print("\n[dim]Running scout analysis...[/dim]")
                from fpl_cli.agents.data.scout import ScoutAgent
                async with ScoutAgent() as scout_agent:
                    scout_result = await scout_agent.run(context={"gameweek": gw})

                if scout_result.success:
                    scout_data = scout_result.data
                    scout_generated = datetime.now().strftime("%Y-%m-%d %H:%M")

                    # Metadata header for scout files
                    metadata = (
                        f"---\n"
                        f"gameweek: {gw}\n"
                        f"generated: {scout_generated}\n"
                        f"deadline: {deadline}\n"
                        f"source: scout\n"
                        f"---\n\n"
                    )

                    # Save scout reports to dedicated directory
                    scout_dir = resolve_research_dir(settings) / "ai-scout-reports"
                    scout_dir.mkdir(parents=True, exist_ok=True)

                    # Save referenced version (with citations appended)
                    referenced_path = scout_dir / f"gw{gw}-scout-preview-referenced.md"
                    content_with_refs = scout_data["content_referenced"]
                    citations = scout_data.get("citations", [])
                    if citations:
                        refs_section = "\n\n## References\n"
                        for i, url in enumerate(citations, 1):
                            refs_section += f"[{i}] {url}\n"
                        content_with_refs += refs_section
                    referenced_path.write_text(metadata + content_with_refs, encoding="utf-8")
                    console.print(f"[green]✓[/green] Scout report (with refs): {referenced_path}")

                    # Save clean version
                    clean_path = scout_dir / f"gw{gw}-scout-preview.md"
                    clean_path.write_text(metadata + scout_data["content_clean"], encoding="utf-8")
                    console.print(f"[green]✓[/green] Scout report (clean): {clean_path}")
                else:
                    error_console.print(f"[yellow]⚠[/yellow] Scout analysis: {scout_result.message}")

    def _display_preview_summary(data: dict, gw: int, *, custom_on: bool = True):
        """Display a summary of the preview analysis."""

        # --- Fixture Analysis ---
        console.print("[bold underline]Fixture Analysis[/bold underline]\n")

        # Gameweek Fixtures
        if data.get("gw_fixtures"):
            console.print("[bold]Gameweek Fixtures:[/bold]")
            table = Table(show_header=True, header_style="bold")
            table.add_column("Home")
            table.add_column("FDR", justify="center")
            table.add_column("", justify="center")
            table.add_column("FDR", justify="center")
            table.add_column("Away")
            table.add_column("Kickoff")

            for f in data["gw_fixtures"]:
                home_style = _fdr_style(f["home_fdr"])
                away_style = _fdr_style(f["away_fdr"])
                # Format FDR with 1 decimal for floats
                home_fdr_str = f"{f['home_fdr']:.1f}" if isinstance(f["home_fdr"], float) else str(f["home_fdr"])
                away_fdr_str = f"{f['away_fdr']:.1f}" if isinstance(f["away_fdr"], float) else str(f["away_fdr"])
                table.add_row(
                    f["home_team"],
                    f"[{home_style}]{home_fdr_str}[/{home_style}]",
                    "vs",
                    f"[{away_style}]{away_fdr_str}[/{away_style}]",
                    f["away_team"],
                    f["kickoff"],
                )
            console.print(table)
            console.print("")

        # Teams with Easy Fixtures
        if data.get("fixtures") and data["fixtures"].get("easy_fixture_runs"):
            easy_runs = data["fixtures"]["easy_fixture_runs"]
            # Support both old list format and new dict format with positional FDR
            if isinstance(easy_runs, dict):
                easy_list = easy_runs.get("overall", [])
            else:
                easy_list = easy_runs
            console.print("[bold]Teams with Easy Fixtures:[/bold]")
            table = Table(show_header=True, header_style="bold")
            table.add_column("Team")
            table.add_column("Avg FDR", justify="right")
            if custom_on:
                table.add_column("ATK", justify="right")
                table.add_column("DEF", justify="right")
            table.add_column("Next 6 Fixtures")

            def style_fdr(val: float) -> str:
                """Format FDR value with color styling."""
                if val < 2.5:
                    return f"[green]{val:.2f}[/green]"
                elif val < 3.0:
                    return f"[yellow]{val:.2f}[/yellow]"
                else:
                    return f"{val:.2f}"

            for team in easy_list[:8]:
                fdr = team["average_fdr"]
                if custom_on:
                    fdr_atk = team.get("average_fdr_atk", fdr)
                    fdr_def = team.get("average_fdr_def", fdr)
                    table.add_row(
                        team["short_name"],
                        style_fdr(fdr),
                        style_fdr(fdr_atk),
                        style_fdr(fdr_def),
                        team["fixtures_summary"],
                    )
                else:
                    table.add_row(
                        team["short_name"],
                        style_fdr(fdr),
                        team["fixtures_summary"],
                    )
            console.print(table)
            console.print("")

        # --- Team Form ---
        if data.get("fixtures") and data["fixtures"].get("team_form"):
            console.print("[bold underline]Team Form[/bold underline]\n")
            table = Table(show_header=True, header_style="bold")
            table.add_column("Team")
            table.add_column("Pts (6)", justify="right")
            table.add_column("GS (6)", justify="right")
            table.add_column("GC (6)", justify="right")
            table.add_column("Next", justify="center")
            table.add_column("Pts (H/A)", justify="right")
            table.add_column("GS (H/A)", justify="right")
            table.add_column("GC (H/A)", justify="right")

            for t in data["fixtures"]["team_form"]:
                table.add_row(
                    t["team"],
                    str(t["pts_6"]),
                    str(t["gs_6"]),
                    str(t["gc_6"]),
                    t["next_venue"],
                    str(t["pts_ha"]),
                    str(t["gs_ha"]),
                    str(t["gc_ha"]),
                )
            console.print(table)
            console.print("")

        # --- Classic FPL ---
        has_classic_data = data.get("my_squad") or data.get("prices")
        if show_classic and has_classic_data:
            console.print("[bold underline]Classic FPL[/bold underline]\n")

        # My Squad
        if show_classic and data.get("my_squad"):
            console.print("[bold]My Squad:[/bold]")
            table = Table(show_header=True, header_style="bold")
            table.add_column("Player")
            table.add_column("Team")
            table.add_column("Fixture")
            table.add_column("Pos")
            table.add_column("Form", justify="right")
            table.add_column("Own%", justify="right")
            table.add_column("Status")

            for p in data["my_squad"]:
                form_style = "green" if p["form"] >= 5 else "yellow" if p["form"] >= 3 else "red"
                status_style = "green" if p["status"] == "✓" else "yellow"
                table.add_row(
                    p["name"],
                    p["team"],
                    p.get("fixture", "—"),
                    p["position"],
                    f"[{form_style}]{p['form']:.1f}[/{form_style}]",
                    f"{p['ownership']}%",
                    f"[{status_style}]{p['status']}[/{status_style}]",
                )
            console.print(table)
            console.print("")

        # Transfer Activity (classic-only: prices and transfer volumes)
        if show_classic and data.get("prices"):
            console.print("[bold]Transfer Activity:[/bold]")
            console.print(
                "[dim]Note: Transfer data reflects the moment this report was generated"
                " and changes until deadline[/dim]"
            )

            # Price Changes
            if data["prices"].get("risers_this_gw"):
                console.print("\n[green]Price Rises This Gameweek:[/green]")
                table = Table(show_header=True, header_style="bold")
                table.add_column("Player")
                table.add_column("Team")
                table.add_column("Price", justify="right")
                table.add_column("Change", justify="right")
                for p in data["prices"]["risers_this_gw"][:10]:
                    table.add_row(
                        p["name"],
                        p["team"],
                        f"£{p['current_price']:.1f}m",
                        f"[green]+£{p['change_this_gw']:.1f}m[/green]",
                    )
                console.print(table)

            if data["prices"].get("fallers_this_gw"):
                console.print("\n[red]Price Falls This Gameweek:[/red]")
                table = Table(show_header=True, header_style="bold")
                table.add_column("Player")
                table.add_column("Team")
                table.add_column("Price", justify="right")
                table.add_column("Change", justify="right")
                for p in data["prices"]["fallers_this_gw"][:10]:
                    table.add_row(
                        p["name"],
                        p["team"],
                        f"£{p['current_price']:.1f}m",
                        f"[red]£{p['change_this_gw']:.1f}m[/red]",
                    )
                console.print(table)

            # Most Transferred In/Out
            if data["prices"].get("hot_transfers_in"):
                console.print("\n[bold]Most Transferred In:[/bold]")
                table = Table(show_header=True, header_style="bold")
                table.add_column("Player")
                table.add_column("Team")
                table.add_column("Transfers In", justify="right")
                table.add_column("Net", justify="right")
                for p in data["prices"]["hot_transfers_in"][:8]:
                    net = p["net_transfers"]
                    net_style = "green" if net > 0 else "red"
                    table.add_row(
                        p["name"],
                        p["team"],
                        f"{p['transfers_in']:,}",
                        f"[{net_style}]{net:+,}[/{net_style}]",
                    )
                console.print(table)

            if data["prices"].get("hot_transfers_out"):
                console.print("\n[bold]Most Transferred Out:[/bold]")
                table = Table(show_header=True, header_style="bold")
                table.add_column("Player")
                table.add_column("Team")
                table.add_column("Transfers Out", justify="right")
                table.add_column("Net", justify="right")
                for p in data["prices"]["hot_transfers_out"][:8]:
                    net = p["net_transfers"]
                    net_style = "green" if net > 0 else "red"
                    table.add_row(
                        p["name"],
                        p["team"],
                        f"{p['transfers_out']:,}",
                        f"[{net_style}]{net:+,}[/{net_style}]",
                    )
                console.print(table)
            console.print("")

        # Performance Stats
        if data.get("stats"):
            console.print("[bold]Performance Stats (Last 6 GWs):[/bold]")

            # Top xGI per 90
            if data["stats"].get("top_xgi_per_90"):
                console.print("\n[bold]Top xGI per 90:[/bold]")
                table = Table(show_header=True, header_style="bold")
                table.add_column("Player")
                table.add_column("Team")
                table.add_column("xG", justify="right")
                table.add_column("xA", justify="right")
                table.add_column("xGI/90", justify="right")
                table.add_column("Goals", justify="right")
                table.add_column("Assists", justify="right")
                for p in data["stats"]["top_xgi_per_90"][:10]:
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
            if data["stats"].get("underperformers"):
                console.print("\n[green]Underperformers (G+A < xGI, due a rise):[/green]")
                table = Table(show_header=True, header_style="bold")
                table.add_column("Player")
                table.add_column("Team")
                table.add_column("G+A", justify="right")
                table.add_column("xGI", justify="right")
                table.add_column("Diff", justify="right")
                for p in data["stats"]["underperformers"][:8]:
                    table.add_row(
                        p["player_name"],
                        p["team_short"],
                        str(p["GI"]),
                        f"{p['xGI']:.2f}",
                        f"[green]{p['difference']:.2f}[/green]",
                    )
                console.print(table)

            # Value Picks (experimental - gated by custom analysis toggle)
            if custom_on and data["stats"].get("value_picks"):
                console.print("\n[cyan]Value Picks (high xGI, low ownership):[/cyan]")
                table = Table(show_header=True, header_style="bold")
                table.add_column("Player")
                table.add_column("Team")
                table.add_column("Price", justify="right")
                table.add_column("Own%", justify="right")
                table.add_column("xGI/90", justify="right")
                for p in data["stats"]["value_picks"][:8]:
                    table.add_row(
                        p["player_name"],
                        p["team_short"],
                        f"£{p['price']:.1f}m",
                        f"{p['ownership']:.1f}%",
                        f"[bold]{p['xGI_per_90']:.2f}[/bold]",
                    )
                console.print(table)
            console.print("")

        # --- Draft ---
        if data.get("draft_squad"):
            console.print("[bold underline]Draft[/bold underline]\n")
            console.print("[bold]My Squad:[/bold]")
            table = Table(show_header=True, header_style="bold")
            table.add_column("Player")
            table.add_column("Team")
            table.add_column("Fixture")
            table.add_column("Pos")
            table.add_column("Form", justify="right")
            table.add_column("Status")

            for p in data["draft_squad"]:
                form_style = "green" if p["form"] >= 5 else "yellow" if p["form"] >= 3 else "red"
                status_style = "green" if p["status"] == "✓" else "yellow"
                table.add_row(
                    p["name"],
                    p["team"],
                    p.get("fixture", "—"),
                    p["position"],
                    f"[{form_style}]{p['form']:.1f}[/{form_style}]",
                    f"[{status_style}]{p['status']}[/{status_style}]",
                )
            console.print(table)
            console.print("")

    asyncio.run(_preview())
