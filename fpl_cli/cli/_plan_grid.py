"""Plan grid subcommand: squad fixture difficulty grid."""

from __future__ import annotations

import asyncio

import click
import httpx
from rich.table import Table

from fpl_cli.cli._context import console, load_settings
from fpl_cli.cli._helpers import _fdr_style
from fpl_cli.cli._json import emit_json, json_output_mode, output_format_option
from fpl_cli.models.player import resolve_player


@click.command("grid")
@click.option("--gws", "-n", type=int, default=6, help="Number of GWs to show (default: 6)")
@click.option("--watch", "-w", multiple=True, help="Additional player names to include (can repeat)")
@click.option("--mode", "-m", type=click.Choice(["difference", "opponent"]), default="difference",
              help="FDR mode: 'difference' (team vs opponent) or 'opponent' (opponent rating only)")
@click.option("--draft", "is_draft", is_flag=True, default=False, help="Use draft squad instead of classic")
@output_format_option
def grid_command(gws: int, watch: tuple[str, ...], mode: str, is_draft: bool, output_format: str):
    """Show squad fixture difficulty grid.

    Displays each player's positional FDR colour-coded across upcoming GWs.
    GK/DEF see opponent offensive ratings; MID/FWD see opponent defensive ratings.
    """
    settings = load_settings()
    draft_entry_id: int | None = None
    entry_id: int | None = None

    if is_draft:
        draft_entry_id = settings.get("fpl", {}).get("draft_entry_id")
        if not draft_entry_id:
            console.print("[red]Error: draft_entry_id not configured in settings.yaml[/red]")
            return
    else:
        entry_id = settings.get("fpl", {}).get("classic_entry_id")
        if not entry_id:
            console.print("[red]Error: classic_entry_id not configured in settings.yaml[/red]")
            return

    async def _grid():
        from fpl_cli.api.fpl import FPLClient
        from fpl_cli.services.team_ratings import TeamRatingsService

        async with FPLClient() as client:
            ratings_svc = TeamRatingsService()
            await ratings_svc.ensure_fresh(client)

            players = await client.get_players()
            teams = await client.get_teams()
            current_gw_data = await client.get_next_gameweek() or await client.get_current_gameweek()
            if not current_gw_data:
                console.print("[red]Could not determine current gameweek[/red]")
                return
            start_gw = current_gw_data["id"]

            player_map = {p.id: p for p in players}
            team_map = {t.id: t for t in teams}

            if is_draft:
                from fpl_cli.agents.common import get_draft_squad_players
                from fpl_cli.api.fpl_draft import FPLDraftClient

                assert draft_entry_id is not None
                async with FPLDraftClient() as draft_client:
                    try:
                        squad_players = await get_draft_squad_players(
                            draft_client, players, draft_entry_id, start_gw,
                            log=lambda msg: console.print(f"[yellow]{msg}[/yellow]"),
                        )
                    except Exception as e:  # noqa: BLE001 — display resilience
                        console.print(f"[red]Could not fetch draft squad: {e}[/red]")
                        return
            else:
                assert entry_id is not None
                try:
                    lookup_gw = start_gw
                    try:
                        picks_data = await client.get_manager_picks(entry_id, lookup_gw)
                    except (httpx.HTTPError, ValueError):
                        lookup_gw = start_gw - 1
                        picks_data = await client.get_manager_picks(entry_id, lookup_gw)

                    while picks_data.get("active_chip") == "freehit" and lookup_gw > 1:
                        lookup_gw -= 1
                        picks_data = await client.get_manager_picks(entry_id, lookup_gw)

                    pick_ids = [p["element"] for p in picks_data.get("picks", [])]
                    squad_players = [player_map[pid] for pid in pick_ids if pid in player_map]
                except Exception as e:  # noqa: BLE001 — display resilience
                    console.print(f"[red]Could not fetch squad: {e}[/red]")
                    return

            watch_players = []
            for name in watch:
                match = resolve_player(name, players, teams=teams)
                if match:
                    watch_players.append(match)
                else:
                    console.print(f"[yellow]Watch: '{name}' not found[/yellow]")

            all_fixtures = await client.get_fixtures()
            fixture_grid: dict[int, dict[int, list[tuple[str, bool]]]] = {}
            for t in teams:
                fixture_grid[t.id] = {}
            for f in all_fixtures:
                if f.gameweek is None or f.gameweek < start_gw or f.gameweek >= start_gw + gws:
                    continue
                fixture_grid[f.home_team_id].setdefault(f.gameweek, []).append(
                    (team_map[f.away_team_id].short_name, True)
                )
                fixture_grid[f.away_team_id].setdefault(f.gameweek, []).append(
                    (team_map[f.home_team_id].short_name, False)
                )

        gw_range = list(range(start_gw, start_gw + gws))
        pos_order = {"GK": 0, "DEF": 1, "MID": 2, "FWD": 3}
        sorted_squad = sorted(squad_players, key=lambda p: pos_order.get(p.position_name, 9))

        if output_format == "json":
            def _player_json(p):
                player_team = team_map.get(p.team_id)
                team_short = player_team.short_name if player_team else None
                gw_data: dict[str, list[dict]] = {}
                for gw in gw_range:
                    if not team_short:
                        gw_data[str(gw)] = []
                        continue
                    fixtures_this_gw = fixture_grid.get(p.team_id, {}).get(gw, [])
                    entries = []
                    for opp_short, is_home in fixtures_this_gw:
                        venue = "home" if is_home else "away"
                        fdr = ratings_svc.get_positional_fdr(
                            position=p.position_name,
                            team=team_short,
                            opponent=opp_short,
                            venue=venue,
                            mode=mode,
                        )
                        entries.append({"opponent": opp_short, "venue": venue, "fdr": fdr})
                    gw_data[str(gw)] = entries
                return {
                    "player": p.web_name,
                    "position": p.position_name,
                    "team": team_short,
                    "gameweeks": gw_data,
                }

            records = [_player_json(p) for p in sorted_squad]
            if watch_players:
                records.extend(_player_json(p) for p in watch_players)

            metadata = {
                "gameweek": start_gw,
                "format": "draft" if is_draft else "classic",
                "gws": gws,
                "mode": mode,
            }
            with json_output_mode() as stdout:
                emit_json("plan-grid", records, metadata=metadata, file=stdout)
            return

        table = Table(show_header=True, header_style="bold", box=None, padding=(0, 1))
        table.add_column("Pos", width=4)
        table.add_column("Player", min_width=16)
        for gw in gw_range:
            table.add_column(f"GW{gw}", justify="center", min_width=5)

        def render_player_row(p):
            row = [p.position_name, p.web_name]
            player_team = team_map.get(p.team_id)
            if not player_team:
                for gw in gw_range:
                    row.append("-")
                return row
            team_short = player_team.short_name
            for gw in gw_range:
                fixtures_this_gw = fixture_grid.get(p.team_id, {}).get(gw, [])
                if not fixtures_this_gw:
                    row.append("[dim]-[/dim]")
                    continue
                cell_parts = []
                for opp_short, is_home in fixtures_this_gw:
                    venue = "home" if is_home else "away"
                    fdr = ratings_svc.get_positional_fdr(
                        position=p.position_name,
                        team=team_short,
                        opponent=opp_short,
                        venue=venue,
                        mode=mode,
                    )
                    style = _fdr_style(fdr)
                    label = opp_short.upper() if is_home else opp_short.lower()
                    cell_parts.append(f"[{style}]{label}[/{style}]")
                row.append(" ".join(cell_parts))
            return row

        prev_pos = None
        for p in sorted_squad:
            if prev_pos and p.position_name != prev_pos:
                table.add_row(*[""] * (2 + len(gw_range)))
            prev_pos = p.position_name
            table.add_row(*render_player_row(p))

        if watch_players:
            table.add_row(*[""] * (2 + len(gw_range)))
            for p in watch_players:
                table.add_row(*render_player_row(p))

        grid_label = "Fixture Grid (Draft)" if is_draft else "Fixture Grid"
        console.print(f"\n[bold]{grid_label} - GW{start_gw} to GW{start_gw + gws - 1}[/bold]")
        console.print(
            "[dim]UPPER = home, lower = away | "
            "Colour: [green]easy[/green] [yellow]ok[/yellow] [orange1]hard[/orange1] [red]tough[/red][/dim]\n"
        )
        console.print(table)

    asyncio.run(_grid())
