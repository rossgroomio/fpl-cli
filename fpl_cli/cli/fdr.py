"""Fixture difficulty analysis command."""
# Pattern: via-agent

from __future__ import annotations

import asyncio
from collections import defaultdict
from typing import Any

import click
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from fpl_cli.cli._context import (
    CLIContext,
    Format,
    console,
    error_console,
    get_format,
    is_custom_analysis_enabled,
    load_settings,
)
from fpl_cli.cli._helpers import _fdr_style
from fpl_cli.cli._json import emit_json, emit_json_error, json_output_mode, output_format_option
from fpl_cli.season import TOTAL_GAMEWEEKS
from fpl_cli.services.fixture_predictions import (
    BlankPrediction,
    BlankTeamInfo,
    DoublePrediction,
    DoubleTeamInfo,
    FixturePredictionsService,
    find_blank_gameweeks,
    find_double_gameweeks,
)

_CONFIDENCE_COLORS = {
    "confirmed": "green", "high": "green", "medium": "yellow", "low": "red",
}


def _render_blanks_doubles(
    target_console: Console,
    confirmed_blanks: dict[int, list[BlankTeamInfo]],
    predicted_blanks: list[BlankPrediction],
    confirmed_doubles: dict[int, list[DoubleTeamInfo]],
    predicted_doubles: list[DoublePrediction],
) -> None:
    """Render confirmed + predicted blank/double GWs to console."""
    if confirmed_blanks or predicted_blanks:
        target_console.print("\n[bold yellow]Blank Gameweeks:[/bold yellow]")
        for gw, teams in confirmed_blanks.items():
            team_names = ", ".join(t["short_name"] for t in teams)
            target_console.print(f"  GW{gw}: {team_names} [green](confirmed)[/green]")
        confirmed_gws = set(confirmed_blanks.keys())
        for pred in predicted_blanks:
            if pred.gameweek not in confirmed_gws:
                team_names = ", ".join(pred.teams)
                color = _CONFIDENCE_COLORS[pred.confidence.value]
                target_console.print(
                    f"  GW{pred.gameweek}: {team_names} "
                    f"[{color}]({pred.confidence.value})[/{color}]"
                )

    if confirmed_doubles or predicted_doubles:
        target_console.print("\n[bold green]Double Gameweeks:[/bold green]")
        for gw, teams in confirmed_doubles.items():
            team_names = ", ".join(t["short_name"] for t in teams)
            target_console.print(f"  GW{gw}: {team_names} [green](confirmed)[/green]")
        confirmed_gws = set(confirmed_doubles.keys())
        for pred in predicted_doubles:
            if pred.gameweek not in confirmed_gws:
                team_names = ", ".join(pred.teams)
                color = _CONFIDENCE_COLORS[pred.confidence.value]
                target_console.print(
                    f"  GW{pred.gameweek}: {team_names} "
                    f"[{color}]({pred.confidence.value})[/{color}]"
                )


@click.command("fdr")
@click.option("--mode", "-m", type=click.Choice(["difference", "opponent"]), default="difference",
              help="FDR mode: 'difference' (team vs opponent) or 'opponent' (opponent rating only)")
@click.option("--position", "-p", type=click.Choice(["all", "atk", "def"]), default="all",
              help="Show FDR for: 'all', 'atk' (FWD/MID), or 'def' (DEF/GK)")
@click.option("--from-gw", type=int, default=None, help="Start gameweek for FDR window (default: current GW)")
@click.option("--to-gw", type=int, default=None, help="End gameweek for FDR window (default: from-gw + 6)")
@click.option("--my-squad", is_flag=True, help="Show squad exposure to blank/double GWs (use --draft for draft format)")
@click.option("--draft", is_flag=True, help="Use draft squad (auto-selected in draft-only mode)")
@click.option("--blanks", is_flag=True, help="Show only blank/double GW schedule (confirmed + predicted)")
@output_format_option
@click.pass_context
def fdr_command(
    ctx: click.Context, mode: str, position: str, from_gw: int | None,
    to_gw: int | None, my_squad: bool, draft: bool, blanks: bool,
    output_format: str,
):
    """Analyze fixture difficulty - easy runs, blanks, doubles.

    FDR mode determines how fixture difficulty is calculated:
    - difference: Considers both team strength and opponent (recommended)
    - opponent: Based solely on opponent's rating

    Position filter shows easy runs for specific positions:
    - atk: Best fixtures for attackers (FWD/MID) based on opponent's defensive weakness
    - def: Best fixtures for defenders (DEF/GK) based on opponent's offensive threat
    """
    from fpl_cli.api.fpl import FPLClient

    fmt = get_format(ctx)
    # In single-format mode, auto-select squad source; --draft only needed in both/unconfigured
    use_draft = draft or fmt == Format.DRAFT

    # --blanks flag validation
    if blanks and my_squad:
        raise click.UsageError(
            "--blanks cannot be combined with --my-squad. Use 'fpl fdr --my-squad' for squad exposure."
        )
    if blanks and mode != "difference":
        raise click.UsageError("--blanks cannot be combined with --mode")
    if blanks and position != "all":
        raise click.UsageError("--blanks cannot be combined with --position")

    # Gate custom analysis features
    settings = ctx.obj.settings if isinstance(ctx.obj, CLIContext) else {}
    custom_on = is_custom_analysis_enabled(settings)

    # Position-specific FDR requires Bayesian ratings (custom analysis)
    if not custom_on and position != "all" and not blanks:
        console.print(
            f"[red]--position {position} requires custom analysis (Bayesian ATK/DEF split)."
            " Enable it with: fpl init[/red]"
        )
        raise SystemExit(1)

    async def _run():
        # direct-api: blanks-only path bypasses agent
        if blanks:
            async with FPLClient() as client:
                next_gw = await client.get_next_gameweek()
                if not next_gw:
                    console.print("[red]Could not determine next gameweek[/red]")
                    return

                current_gw = next_gw["id"]
                start_gw = from_gw if from_gw is not None else current_gw
                end_gw = to_gw if to_gw is not None else TOTAL_GAMEWEEKS

                all_fixtures = await client.get_fixtures()
                teams = await client.get_teams()

            fixtures_by_gw: dict[int, list[Any]] = defaultdict(list)
            for f in all_fixtures:
                if f.gameweek is not None and start_gw <= f.gameweek <= end_gw:
                    fixtures_by_gw[f.gameweek].append(f)
            fixtures_by_gw_dict = dict(fixtures_by_gw)

            confirmed_blanks = find_blank_gameweeks(fixtures_by_gw_dict, teams, start_gw, end_gw)
            confirmed_doubles = find_double_gameweeks(fixtures_by_gw_dict, teams)

            pred_service = FixturePredictionsService()
            metadata = pred_service.get_metadata()
            last_updated = metadata.get("last_updated") or "unknown"
            if pred_service.is_stale:
                console.print(
                    f"[yellow]Fixture predictions may be stale"
                    f" (last updated: {last_updated})[/yellow]\n",
                )
            predicted_blanks = pred_service.get_predicted_blanks(min_gw=current_gw)
            predicted_doubles = pred_service.get_predicted_doubles(min_gw=current_gw)

            if output_format == "json":
                blanks_data = {
                    "confirmed_blanks": [
                        {"gw": gw, "teams": [t["short_name"] for t in teams_list]}
                        for gw, teams_list in confirmed_blanks.items()
                    ],
                    "predicted_blanks": [
                        {"gw": p.gameweek, "teams": p.teams, "confidence": p.confidence.value}
                        for p in predicted_blanks
                    ],
                    "confirmed_doubles": [
                        {"gw": gw, "teams": [t["short_name"] for t in teams_list]}
                        for gw, teams_list in confirmed_doubles.items()
                    ],
                    "predicted_doubles": [
                        {"gw": p.gameweek, "teams": p.teams, "confidence": p.confidence.value}
                        for p in predicted_doubles
                    ],
                    "last_updated": last_updated,
                }
                emit_json("fdr", blanks_data, metadata={
                    "gameweek": current_gw,
                    "mode": "blanks",
                    "from_gw": start_gw,
                    "to_gw": end_gw,
                })
                return

            gw_label = f"GW{start_gw}-{end_gw}"
            console.print(Panel.fit(f"[bold blue]Blank/Double GW Schedule ({gw_label})[/bold blue]"))
            console.print(f"[dim]Predictions last updated: {last_updated}[/dim]")

            _render_blanks_doubles(console, confirmed_blanks, predicted_blanks, confirmed_doubles, predicted_doubles)

            if not confirmed_blanks and not predicted_blanks and not confirmed_doubles and not predicted_doubles:
                console.print("[dim]No blank or double gameweeks in range[/dim]")

            return

        # direct-api: raw FPL API FDR when custom analysis is off
        if not custom_on:
            async with FPLClient() as client:
                next_gw = await client.get_next_gameweek()
                if not next_gw:
                    console.print("[red]Could not determine next gameweek[/red]")
                    return

                current_gw = next_gw["id"]
                start_gw = from_gw if from_gw is not None else current_gw
                end_gw = to_gw if to_gw is not None else min(start_gw + 5, TOTAL_GAMEWEEKS)

                all_fixtures = await client.get_fixtures()
                teams = await client.get_teams()

            team_map = {t.id: t for t in teams}

            # Compute average FDR per team over the GW window
            team_fdrs: dict[int, list[int]] = defaultdict(list)
            team_fixture_strs: dict[int, list[str]] = defaultdict(list)
            for f in all_fixtures:
                if f.gameweek is not None and start_gw <= f.gameweek <= end_gw:
                    # Home team
                    team_fdrs[f.home_team_id].append(f.home_difficulty)
                    opp = team_map.get(f.away_team_id)
                    team_fixture_strs[f.home_team_id].append(
                        (opp.short_name if opp else "???").lower()
                    )
                    # Away team
                    team_fdrs[f.away_team_id].append(f.away_difficulty)
                    opp = team_map.get(f.home_team_id)
                    team_fixture_strs[f.away_team_id].append(
                        (opp.short_name if opp else "???").upper()
                    )

            # Build sorted list (easiest first)
            fdr_rows = []
            for tid, fdrs in team_fdrs.items():
                t = team_map.get(tid)
                if not t:
                    continue
                avg = sum(fdrs) / len(fdrs)
                fdr_rows.append({
                    "short_name": t.short_name,
                    "average_fdr": round(avg, 2),
                    "fixtures_summary": ", ".join(team_fixture_strs[tid]),
                })
            fdr_rows.sort(key=lambda r: r["average_fdr"])

            if output_format == "json":
                with json_output_mode() as stdout:
                    emit_json("fdr", {"easy_fixture_runs": fdr_rows}, metadata={
                        "gameweek": current_gw,
                        "mode": "raw",
                        "custom_analysis": False,
                        "from_gw": start_gw,
                        "to_gw": end_gw,
                    }, file=stdout)
                return

            gw_label = f"GW{start_gw}-{end_gw}"
            console.print(Panel.fit(
                f"[bold blue]Fixture Difficulty ({gw_label}) - FPL API Ratings[/bold blue]"
            ))
            console.print("[dim]FDR on 1-5 scale from FPL API (1 = easiest)[/dim]\n")

            table = Table(show_header=True, header_style="bold")
            table.add_column("Team")
            table.add_column("FDR", justify="center")
            table.add_column("Upcoming Fixtures")

            for row in fdr_rows[:12]:
                fdr_val = row["average_fdr"]
                # Style for 1-5 scale
                if fdr_val <= 2.5:
                    style = "green"
                elif fdr_val <= 3.0:
                    style = "yellow"
                else:
                    style = "white"
                table.add_row(
                    row["short_name"],
                    f"[{style}]{fdr_val:.2f}[/{style}]",
                    row["fixtures_summary"],
                )
            console.print(table)

            # Show blanks/doubles too
            fixtures_by_gw: dict[int, list[Any]] = defaultdict(list)
            for f in all_fixtures:
                if f.gameweek is not None and start_gw <= f.gameweek <= end_gw:
                    fixtures_by_gw[f.gameweek].append(f)

            confirmed_blanks = find_blank_gameweeks(dict(fixtures_by_gw), teams, start_gw, end_gw)
            confirmed_doubles = find_double_gameweeks(dict(fixtures_by_gw), teams, start_gw, end_gw)

            pred_service = FixturePredictionsService()
            predicted_blanks = pred_service.get_predicted_blanks(min_gw=current_gw)
            predicted_doubles = pred_service.get_predicted_doubles(min_gw=current_gw)

            _render_blanks_doubles(
                console, confirmed_blanks, predicted_blanks, confirmed_doubles, predicted_doubles,
            )
            return

        # Default path: full FDR analysis via agent
        from fpl_cli.agents.data.fixture import FixtureAgent
        from fpl_cli.services.team_ratings import TeamRatingsService

        config: dict = {"fdr_mode": mode}
        if from_gw is not None:
            config["from_gw"] = from_gw
        if to_gw is not None:
            config["to_gw"] = to_gw

        context: dict | None = None
        if my_squad:
            settings = load_settings()
            if use_draft:
                entry_id = settings.get("fpl", {}).get("draft_entry_id")
                if not entry_id:
                    error_console.print("[yellow]draft_entry_id not configured[/yellow]")
                else:
                    from fpl_cli.api.fpl_draft import FPLDraftClient
                    async with FPLDraftClient() as draft_client:
                        game_data = await draft_client.get_game_state()
                        current_gw = game_data.get("current_event", 1)
                        bootstrap = await draft_client.get_bootstrap_static()
                        draft_player_map = {p["id"]: p for p in bootstrap.get("elements", [])}
                        try:
                            picks_data = await draft_client.get_entry_picks(entry_id, current_gw)
                        except Exception:  # noqa: BLE001 — best-effort enrichment
                            picks_data = await draft_client.get_entry_picks(entry_id, current_gw - 1)
                        pick_ids = [p["element"] for p in picks_data.get("picks", [])]
                        squad = [
                            {
                                "team_id": draft_player_map[pid]["team"],
                                "element_type": draft_player_map[pid]["element_type"],
                                "web_name": draft_player_map[pid]["web_name"],
                            }
                            for pid in pick_ids
                            if pid in draft_player_map
                        ]
                        context = {"squad": squad}
            else:
                entry_id = settings.get("fpl", {}).get("classic_entry_id")
                if not entry_id:
                    error_console.print("[yellow]classic_entry_id not configured - cannot show squad exposure[/yellow]")
                else:
                    async with FPLClient() as client:
                        next_gw_data = await client.get_next_gameweek()
                        last_gw = (next_gw_data["id"] - 1) if next_gw_data else None
                        if last_gw and last_gw > 0:
                            picks_data = await client.get_manager_picks(entry_id, last_gw)
                            if picks_data.get("active_chip") == "freehit":
                                last_gw -= 1
                                picks_data = await client.get_manager_picks(entry_id, last_gw)
                            pick_ids = [p["element"] for p in picks_data.get("picks", [])]
                            players = await client.get_players()
                            player_map = {p.id: p for p in players}
                            squad = [
                                {
                                    "team_id": player_map[pid].team_id,
                                    "element_type": int(player_map[pid].position),
                                    "web_name": player_map[pid].web_name,
                                }
                                for pid in pick_ids
                                if pid in player_map
                            ]
                            context = {"squad": squad}

        if output_format == "json":
            with json_output_mode() as stdout:
                async with FixtureAgent(config=config) as agent:
                    result = await agent.run(context=context)
                if not result.success:
                    emit_json_error("fdr", result.message, file=stdout)
                    return
                emit_json("fdr", result.data, metadata={
                    "gameweek": result.data.get("current_gameweek"),
                    "format": "draft" if use_draft else "classic",
                    "custom_analysis": True,
                    "mode": mode,
                    "position": position,
                }, file=stdout)
            return

        async with FixtureAgent(config=config) as agent:
            result = await agent.run(context=context)

        if not result.success:
            console.print(f"[red]Agent failed: {result.message}[/red]")
            for error in result.errors:
                console.print(f"  [red]{error}[/red]")
            return

        data = result.data
        current_gw = data["current_gameweek"]
        mode_label = "Difference" if mode == "difference" else "Opponent"
        gw_label = f"GW{from_gw}-{to_gw}" if from_gw or to_gw else f"GW{current_gw}+"
        title = f"[bold blue]Fixture Analysis ({gw_label}) - {mode_label} Mode[/bold blue]"
        console.print(Panel.fit(title))

        # Check for stale ratings
        ratings_service = TeamRatingsService()
        staleness_warning = ratings_service.get_staleness_warning()
        if staleness_warning:
            error_console.print(f"[yellow]{staleness_warning}[/yellow]\n")

        easy_runs = data["easy_fixture_runs"]

        # Show position-specific easy runs
        if position == "atk":
            console.print("\n[bold]Best Fixtures for Attackers (FWD/MID):[/bold]")
            teams_list = easy_runs.get("for_attackers", easy_runs.get("overall", []))
            fdr_key = "average_fdr_atk"
        elif position == "def":
            console.print("\n[bold]Best Fixtures for Defenders (DEF/GK):[/bold]")
            teams_list = easy_runs.get("for_defenders", easy_runs.get("overall", []))
            fdr_key = "average_fdr_def"
        else:
            console.print("\n[bold]Teams with Easiest Fixture Runs:[/bold]")
            teams_list = easy_runs.get("overall", easy_runs) if isinstance(easy_runs, dict) else easy_runs
            fdr_key = "average_fdr"

        table = Table(show_header=True, header_style="bold")
        table.add_column("Team")
        if position == "all":
            table.add_column("FDR", justify="center", header_style="dim")
            table.add_column("ATK", justify="center")
            table.add_column("DEF", justify="center")
        else:
            table.add_column("FDR", justify="center")
        table.add_column("Upcoming Fixtures")

        for team in teams_list[:8]:
            fdr = team.get(fdr_key, team.get("average_fdr", 4.0))
            style = _fdr_style(fdr)

            if position == "all":
                atk_fdr = team.get("average_fdr_atk", fdr)
                def_fdr = team.get("average_fdr_def", fdr)
                atk_style = _fdr_style(atk_fdr)
                def_style = _fdr_style(def_fdr)
                table.add_row(
                    team["short_name"],
                    f"[{style}]{fdr:.2f}[/{style}]",
                    f"[{atk_style}]{atk_fdr:.2f}[/{atk_style}]",
                    f"[{def_style}]{def_fdr:.2f}[/{def_style}]",
                    team["fixtures_summary"],
                )
            else:
                table.add_row(
                    team["short_name"],
                    f"[{style}]{fdr:.2f}[/{style}]",
                    team["fixtures_summary"],
                )
        console.print(table)

        if position == "all":
            console.print("[dim]FDR / ATK / DEF all on 1-7 scale (1 = easiest)[/dim]")

        # Show blank/double gameweeks (confirmed + predicted, filtered to current GW+)
        pred_service = FixturePredictionsService()

        confirmed_blanks = data.get("blank_gameweeks", {})
        predicted_blanks = pred_service.get_predicted_blanks(min_gw=current_gw)
        confirmed_doubles = data.get("double_gameweeks", {})
        predicted_doubles = pred_service.get_predicted_doubles(min_gw=current_gw)

        _render_blanks_doubles(console, confirmed_blanks, predicted_blanks, confirmed_doubles, predicted_doubles)

        # Show squad exposure if --my-squad was requested
        if my_squad and context is not None:
            squad_exposure = data.get("squad_exposure", [])
            console.print("\n[bold]Squad Exposure:[/bold]")
            if squad_exposure:
                for entry in squad_exposure:
                    gw_type = entry["type"].upper()
                    players_str = ", ".join(entry["players"])
                    source_tag = "" if entry["source"] == "confirmed" else " [dim](predicted)[/dim]"
                    if gw_type == "BLANK":
                        label_style = "red" if entry["affected"] >= 4 else "yellow"
                    else:
                        label_style = "green" if entry["affected"] >= 6 else "cyan"
                    console.print(
                        f"  GW{entry['gw']} {gw_type}: "
                        f"[{label_style}]{entry['affected']}/{entry['total']} affected "
                        f"({entry['starters']} starters)[/{label_style}]"
                        f"{source_tag} — {players_str}"
                    )
            else:
                console.print("  [dim]No blank/double GW overlap with your squad[/dim]")

    asyncio.run(_run())
