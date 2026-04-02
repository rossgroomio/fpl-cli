"""FPL player lookup and display."""
# Pattern: direct-api

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

import click
import httpx
from rich.panel import Panel

from fpl_cli.cli._context import Format, console, get_format, is_custom_analysis_enabled, load_settings
from fpl_cli.cli._helpers import _fdr_style
from fpl_cli.cli._json import emit_json, json_output_mode, output_format_option
from fpl_cli.models.player import resolve_players
from fpl_cli.services.player_scoring import compute_quality_value

if TYPE_CHECKING:
    from fpl_cli.api.fpl import FPLClient
    from fpl_cli.api.vaastav import PlayerProfile
    from fpl_cli.models.player import Player
    from fpl_cli.models.team import Team

logger = logging.getLogger(__name__)


@click.command("player")
@click.argument("name")
@click.option("--fixtures", "-f", is_flag=True, help="Show upcoming fixture run with positional FDR")
@click.option("--mode", "-m", type=click.Choice(["difference", "opponent"]), default="difference",
              help="FDR mode (use with --fixtures): 'difference' or 'opponent'")
@click.option("--detail", "-d", is_flag=True, help="Show GW-by-GW match performance")
@click.option("--understat", "-u", is_flag=True, help="Show Understat shot analysis and situation profile")
@click.option("--history", "-H", is_flag=True, help="Show historical career arc from vaastav dataset")
@output_format_option
@click.pass_context
def player_command(
    ctx: click.Context, name: str, fixtures: bool, mode: str,
    detail: bool, understat: bool, history: bool, output_format: str,
):
    """Look up a player's stats, xG, ownership and fixture run.

    NAME can be a player name, numeric ID, or 'Name (TEAM)' to disambiguate.
    Shows up to 5 matches when multiple players match.
    """
    from fpl_cli.api.fpl import FPLClient
    from fpl_cli.api.fpl_draft import FPLDraftClient

    fmt = get_format(ctx)
    show_draft = fmt != Format.CLASSIC
    show_classic_meta = fmt != Format.DRAFT

    async def _run():
        async with FPLClient() as client:
            settings = load_settings()
            draft_league_id = settings.get("fpl", {}).get("draft_league_id")

            try:
                players = await client.get_players()
                teams = {t.id: t for t in await client.get_teams()}

                # Fetch Understat league data for enrichment
                from fpl_cli.api.understat import UnderstatClient, match_fpl_to_understat
                try:
                    async with UnderstatClient() as us_client:
                        understat_players = await us_client.get_league_players()
                except httpx.HTTPError:
                    understat_players = []

                # Fetch next gameweek for quality scoring (cached, no extra API cost)
                next_gw = await client.get_next_gameweek()
                next_gw_id = next_gw["id"] if next_gw else 38

                # Get draft ownership info if configured
                draft_owned = {}
                draft_entries = {}
                main_to_draft_id = {}  # Map main FPL player IDs to draft player IDs
                if show_draft and draft_league_id:
                    try:
                        async with FPLDraftClient() as draft_client:
                            league_details = await draft_client.get_league_details(draft_league_id)
                            draft_bootstrap = await draft_client.get_bootstrap_static()

                            # Map entry IDs to manager names
                            for entry in league_details.get("league_entries", []):
                                manager_name = (
                                    f"{entry.get('player_first_name', '')} {entry.get('player_last_name', '')}".strip()
                                )
                                draft_entries[entry["entry_id"]] = manager_name or "Unknown"

                            # Get accurate ownership from actual squads (element-status can be stale)
                            draft_owned = await draft_client.get_league_ownership(draft_league_id, draft_bootstrap)

                            # Create mapping from main FPL IDs to draft IDs
                            # Draft API may use different player IDs than main FPL API
                            draft_players = draft_bootstrap.get("elements", [])
                            draft_by_name_team = {
                                (dp.get("web_name"), dp.get("team")): dp["id"]
                                for dp in draft_players
                            }
                            for p in players:
                                draft_id = draft_by_name_team.get((p.web_name, p.team_id))
                                if draft_id:
                                    main_to_draft_id[p.id] = draft_id
                    except Exception as e:  # noqa: BLE001 — best-effort enrichment
                        logger.warning("Draft ID mapping failed: %s", e)

                # Search by name, ID, or Name (TEAM)
                team_list = list(teams.values())
                matches = resolve_players(name, players, teams=team_list)

                if not matches:
                    console.print(f"[yellow]No players found matching '{name}'[/yellow]")
                    return

                display = matches[:5]

                # Pre-compute Understat matches for panel enrichment
                us_matches: dict[int, dict] = {}
                for p in display:
                    if understat_players:
                        team_obj = teams.get(p.team_id)
                        t_name = team_obj.name if team_obj else ""
                        us = match_fpl_to_understat(
                            p.web_name, t_name, understat_players,
                            fpl_position=p.position_name, fpl_minutes=p.minutes,
                        )
                        if us:
                            us_matches[p.id] = us

                # Pre-fetch per-player data in parallel
                detail_map: dict[int, dict] = {}
                understat_data_map: dict[int, dict] = {}
                history_map: dict[int, PlayerProfile | str] = {}

                async def _fetch_detail(pid: int):
                    return pid, await client.get_player_detail(pid)

                async def _fetch_understat(pid: int, us_id: int, us_client):
                    return pid, await us_client.get_player(us_id)

                async def _fetch_history(pid: int, code: int):
                    from fpl_cli.api.vaastav import VaastavClient
                    try:
                        async with VaastavClient() as vaastav:
                            return pid, await vaastav.get_player_history(code)
                    except httpx.HTTPStatusError as exc:  # noqa: BLE001 — graceful degradation
                        if exc.response.status_code == 429:
                            return pid, "rate_limited"
                        return pid, None
                    except (httpx.TimeoutException, httpx.ConnectError):
                        return pid, "network_error"
                    except Exception:  # noqa: BLE001 — graceful degradation
                        return pid, None

                tasks = []
                # Always fetch detail for players with Understat match (form_trajectory for quality scoring)
                # The --detail flag controls display of GW table, not whether history is fetched
                scored_pids = set(us_matches.keys())
                if detail:
                    tasks.extend(_fetch_detail(p.id) for p in display)
                else:
                    tasks.extend(_fetch_detail(p.id) for p in display if p.id in scored_pids)
                if history:
                    tasks.extend(_fetch_history(p.id, p.code) for p in display)

                # Understat detail needs a single shared client
                if understat and us_matches:
                    async with UnderstatClient() as us_detail_client:
                        us_tasks = [
                            _fetch_understat(pid, us["id"], us_detail_client)
                            for pid, us in us_matches.items()
                        ]
                        results = await asyncio.gather(*tasks, *us_tasks, return_exceptions=True)
                else:
                    results = await asyncio.gather(*tasks, return_exceptions=True) if tasks else []

                for r in results:
                    if isinstance(r, BaseException):
                        continue
                    pid, data = r  # type: ignore[misc]
                    if data is None:
                        continue
                    # Route result to the right map based on type
                    if isinstance(data, str):
                        history_map[pid] = data
                    elif isinstance(data, dict) and "history" in data:
                        detail_map[pid] = data
                    elif isinstance(data, dict) and ("shots" in data or "matches" in data):
                        understat_data_map[pid] = data
                    else:
                        history_map[pid] = data  # type: ignore[assignment]

                # Compute quality and value scores (custom analysis only)
                quality_scores: dict[int, int] = {}
                value_scores: dict[int, float | None] = {}
                custom_on = is_custom_analysis_enabled(settings)
                if custom_on:
                    for p in display:
                        us_match = us_matches.get(p.id)
                        if not us_match:
                            continue
                        team_obj = teams.get(p.team_id)
                        player_detail = detail_map.get(p.id)
                        gw_hist = player_detail.get("history", []) if player_detail else None
                        q, v = compute_quality_value(
                            p, us_match, next_gw_id,
                            team_short=team_obj.short_name if team_obj else "???",
                            gw_history=gw_hist or None,
                        )
                        quality_scores[p.id] = q
                        value_scores[p.id] = v

                # JSON output mode
                if output_format == "json":
                    with json_output_mode() as stdout:
                        players_data = []
                        for p in display:
                            team = teams.get(p.team_id)
                            team_name = team.name if team else "Unknown"
                            team_short = team.short_name if team else "???"

                            status_text = _status_display_plain(p)

                            is_gk = p.position_name == "GK"
                            info: dict = {
                                "id": p.id,
                                "web_name": p.web_name,
                                "full_name": p.full_name,
                                "team": team_name,
                                "team_short": team_short,
                                "position": p.position_name,
                                "price": round(float(p.price), 1),
                                "form": float(p.form),
                                "total_points": p.total_points,
                                "points_per_game": float(p.points_per_game),
                                "goals_scored": p.goals_scored,
                                "assists": p.assists,
                                "expected_assists": float(p.expected_assists),
                                "status": status_text,
                            }
                            if is_gk:
                                info["penalties_saved"] = p.penalties_saved
                            else:
                                info["expected_goals"] = float(p.expected_goals)
                            player_dict: dict = {"info": info}

                            if show_classic_meta:
                                player_dict["info"]["selected_by_percent"] = float(p.selected_by_percent)

                            if show_draft and draft_league_id:
                                draft_player_id = main_to_draft_id.get(p.id)
                                if draft_player_id is not None and draft_player_id in draft_owned:
                                    owner_id = draft_owned[draft_player_id]
                                    owner_name = draft_entries.get(owner_id, f"Team #{owner_id}")
                                    player_dict["info"]["draft_ownership"] = f"Owned by {owner_name}"
                                else:
                                    player_dict["info"]["draft_ownership"] = "Available"

                            if (p.penalties_order is not None
                                    or p.corners_and_indirect_freekicks_order is not None
                                    or p.direct_freekicks_order is not None):
                                player_dict["info"]["set_pieces"] = {
                                    "penalties_order": p.penalties_order,
                                    "corners_order": p.corners_and_indirect_freekicks_order,
                                    "direct_freekicks_order": p.direct_freekicks_order,
                                }

                            if p.position_name != "GK" and p.defensive_contribution_per_90 > 0:
                                dc_per_90 = float(p.defensive_contribution_per_90)
                                player_dict["info"]["defensive_contribution_per_90"] = dc_per_90

                            us_match = us_matches.get(p.id)
                            if us_match and not is_gk:
                                player_dict["info"]["npxG"] = us_match["npxG"]
                                player_dict["info"]["xGChain"] = us_match["xGChain"]
                                player_dict["info"]["xGBuildup"] = us_match["xGBuildup"]

                            if custom_on:
                                if p.id in quality_scores:
                                    player_dict["info"]["quality_score"] = quality_scores[p.id]
                                    player_dict["info"]["value_score"] = value_scores[p.id]
                                else:
                                    player_dict["info"]["quality_score"] = None
                                    player_dict["info"]["value_score"] = None

                            if fixtures:
                                player_dict["fixtures"] = await _get_fixture_run_data(
                                    p, team, teams, client, mode=mode,
                                )

                            if detail and p.id in detail_map:
                                player_dict["detail"] = _build_detail_json(
                                    detail_map[p.id], p.position_name, teams,
                                )

                            if understat and p.id in understat_data_map:
                                player_dict["understat"] = understat_data_map[p.id]

                            if history:
                                h_profile = history_map.get(p.id)
                                if isinstance(h_profile, str):
                                    player_dict["history_error"] = h_profile
                                elif h_profile:
                                    player_dict["history"] = _build_history_json(h_profile)

                            players_data.append(player_dict)

                        emit_json("player", players_data, metadata={
                            "query": name,
                            "matches": len(display),
                        }, file=stdout)
                    return

                # Render each player
                for p in display:
                    team = teams.get(p.team_id)
                    team_name = team.name if team else "Unknown"

                    # Build draft status line
                    draft_line = ""
                    if show_draft and draft_league_id:
                        draft_player_id = main_to_draft_id.get(p.id)
                        if draft_player_id is not None and draft_player_id in draft_owned:
                            owner_id = draft_owned[draft_player_id]
                            owner_name = draft_entries.get(owner_id, f"Team #{owner_id}")
                            draft_line = f"Draft: [red]Owned by {owner_name}[/red]\n"
                        else:
                            draft_line = "Draft: [green]Available[/green]\n"

                    # Understat enrichment for base panel
                    understat_line = ""
                    us_match = us_matches.get(p.id)
                    if us_match:
                        understat_line = (
                            f"npxG: {us_match['npxG']:.2f} | "
                            f"xGChain: {us_match['xGChain']:.2f} | "
                            f"xGBuildup: {us_match['xGBuildup']:.2f}\n"
                        )

                    # Build panel lines conditionally
                    is_gk = p.position_name == "GK"
                    lines = [
                        f"[bold]{p.web_name}[/bold] ({p.full_name})",
                        f"Team: {team_name} | Position: {p.position_name}",
                        f"Price: £{p.price:.1f}m | Form: {p.form:.1f}",
                        f"Points: {p.total_points} | PPG: {p.points_per_game:.1f}",
                        f"Goals: {p.goals_scored} | Assists: {p.assists}",
                    ]
                    if is_gk:
                        lines.append(f"Penalties saved: {p.penalties_saved} | xA: {p.expected_assists:.2f}")
                    else:
                        lines.append(f"xG: {p.expected_goals:.2f} | xA: {p.expected_assists:.2f}")
                    if understat_line and not is_gk:
                        lines.append(understat_line.rstrip("\n"))
                    set_piece_line = _build_set_piece_line(p)
                    if set_piece_line:
                        lines.append(set_piece_line)
                    if p.position_name != "GK" and p.defensive_contribution_per_90 > 0:
                        lines.append(f"DC/90: {p.defensive_contribution_per_90:.1f}")
                    if show_classic_meta:
                        lines.append(f"Selected by: {p.selected_by_percent}%")
                    if p.id in quality_scores:
                        q = quality_scores[p.id]
                        v = value_scores[p.id]
                        v_str = f"{v}/£m" if v is not None else "N/A"
                        lines.append(f"Quality: {q} | Value: {v_str}")
                    if draft_line:
                        lines.append(draft_line.rstrip("\n"))
                    lines.append(f"Status: {_status_display(p)}")

                    console.print(Panel.fit(
                        "\n".join(lines),
                        title=f"Player #{p.id}",
                    ))

                    if fixtures:
                        await _show_player_fixtures(p, team, teams, client, mode=mode)

                    if detail and p.id in detail_map:
                        _show_match_detail_from_data(detail_map[p.id], p.web_name, teams, p.position_name)
                    elif detail:
                        console.print("[yellow]No match data available[/yellow]")

                    if understat:
                        if p.id in understat_data_map:
                            _show_understat_analysis(understat_data_map[p.id], p.web_name)
                        elif p.id not in us_matches:
                            console.print("[yellow]No Understat match found[/yellow]")

                    if history:
                        h_profile = history_map.get(p.id)
                        if h_profile == "rate_limited":
                            console.print(
                                "[yellow]Historical data unavailable"
                                " (GitHub rate limit - try again shortly)[/yellow]",
                            )
                        elif h_profile == "network_error":
                            console.print("[yellow]Historical data unavailable (network error)[/yellow]")
                        elif h_profile is not None and not isinstance(h_profile, str):
                            _show_player_history(h_profile)
                        else:
                            console.print("[yellow]No historical data available[/yellow]")

            except Exception as e:  # noqa: BLE001 — display resilience
                console.print(f"[red]Error: {e}[/red]")

    asyncio.run(_run())


def _build_set_piece_line(player: Player) -> str | None:
    """Build set-piece duties string, or None if no qualifying duties."""
    ordinal = {1: "1st", 2: "2nd", 3: "3rd"}
    parts: list[str] = []
    pen = player.penalties_order
    cor = player.corners_and_indirect_freekicks_order
    dfk = player.direct_freekicks_order
    if pen is not None and pen <= 2:
        parts.append(f"Pens ({ordinal.get(pen, f'{pen}th')})")
    if cor is not None and cor <= 2:
        parts.append(f"Corners ({ordinal.get(cor, f'{cor}th')})")
    if dfk is not None and dfk == 1:
        parts.append("Direct FKs (1st)")
    return f"Set pieces: {' · '.join(parts)}" if parts else None


def _status_display_plain(player: Player) -> str:
    """Get plain-text display string for player status."""
    from fpl_cli.models.player import PlayerStatus

    if player.status == PlayerStatus.AVAILABLE:
        return "Available"
    if player.status == PlayerStatus.DOUBTFUL:
        return f"Doubtful ({player.chance_of_playing_next_round}%)"
    if player.status == PlayerStatus.INJURED:
        return f"Injured - {player.news}"
    if player.status == PlayerStatus.SUSPENDED:
        return f"Suspended - {player.news}"
    return f"Unavailable - {player.news}"


def _status_display(player: Player) -> str:
    """Get Rich markup display string for player status."""
    from fpl_cli.models.player import PlayerStatus

    plain = _status_display_plain(player)
    if player.status == PlayerStatus.AVAILABLE:
        return f"[green]{plain}[/green]"
    if player.status == PlayerStatus.DOUBTFUL:
        return f"[yellow]{plain}[/yellow]"
    return f"[red]{plain}[/red]"


def _show_match_detail_from_data(
    player_data: dict, player_name: str, teams: dict[int, Team], position: str,
) -> None:
    """Show GW-by-GW match performance from pre-fetched element-summary data."""
    from rich.table import Table

    history = player_data.get("history", [])

    if not history:
        console.print("[yellow]No match data available[/yellow]")
        return

    is_gk = position == "GK"
    is_def = position == "DEF"
    is_defensive = is_gk or is_def

    recent = history[-10:][::-1]

    table = Table(title=f"Match Detail: {player_name}")
    table.add_column("GW", justify="right")
    table.add_column("Opponent")
    table.add_column("Mins", justify="right")
    if not is_gk:
        table.add_column("G", justify="right")
        table.add_column("xG", justify="right")
    table.add_column("A", justify="right")
    table.add_column("xA", justify="right")
    if is_defensive:
        table.add_column("CS", justify="right")
        table.add_column("GC", justify="right")
        table.add_column("xGC", justify="right")
    if is_gk:
        table.add_column("Sv", justify="right")
    table.add_column("Bon", justify="right")
    table.add_column("Pts", justify="right")

    for h in recent:
        opponent_id = h.get("opponent_team")
        team_obj = teams.get(opponent_id)
        opponent_short = team_obj.short_name if team_obj else "???"
        is_home = h.get("was_home", False)
        opp_display = opponent_short.upper() if is_home else opponent_short.lower()

        row = [
            str(h.get("round", "")),
            opp_display,
            str(h.get("minutes", 0)),
        ]
        if not is_gk:
            row.append(str(h.get("goals_scored", 0)))
            row.append(f"{float(h.get('expected_goals', 0)):.2f}")
        row.extend([
            str(h.get("assists", 0)),
            f"{float(h.get('expected_assists', 0)):.2f}",
        ])
        if is_defensive:
            row.append(str(h.get("clean_sheets", 0)))
            row.append(str(h.get("goals_conceded", 0)))
            row.append(f"{float(h.get('expected_goals_conceded', 0)):.2f}")
        if is_gk:
            row.append(str(h.get("saves", 0)))
        row.append(str(h.get("bonus", 0)))
        row.append(str(h.get("total_points", 0)))

        table.add_row(*row)

    console.print(table)


async def _get_fixture_run_data(
    player: Player, team: Team | None, teams: dict[int, Team],
    client: FPLClient, mode: str = "difference",
) -> list[dict]:
    """Build fixture run data for a player. Used by both JSON and Rich output paths."""
    from collections import defaultdict

    from fpl_cli.agents.data.fixture import FixtureAgent

    fixture_agent = FixtureAgent()
    next_gw = await client.get_next_gameweek()
    if not next_gw:
        return []

    current_gw = next_gw["id"]
    all_fixtures = await client.get_fixtures()
    team_id = player.team_id
    end_gw = current_gw + 5

    fixtures_by_gw: dict[int, list] = defaultdict(list)
    for f in all_fixtures:
        if f.gameweek and current_gw <= f.gameweek <= end_gw:
            if f.home_team_id == team_id or f.away_team_id == team_id:
                fixtures_by_gw[f.gameweek].append(f)

    result = []
    for gw in range(current_gw, end_gw + 1):
        gw_fixtures = fixtures_by_gw.get(gw, [])
        gw_entry: dict = {"gameweek": gw, "fixtures": []}
        for f in gw_fixtures:
            is_home = f.home_team_id == team_id
            opponent_id = f.away_team_id if is_home else f.home_team_id
            opponent = teams.get(opponent_id)
            opponent_short = opponent.short_name if opponent else "???"
            pos_fdr = fixture_agent.get_positional_fdr(
                position=player.position_name,
                team_short=team.short_name if team else "???",
                opponent_short=opponent_short,
                is_home=is_home,
                mode=mode,
            )
            gw_entry["fixtures"].append({
                "opponent": opponent_short,
                "venue": "home" if is_home else "away",
                "fdr": round(pos_fdr, 2),
                "is_home": is_home,
            })
        result.append(gw_entry)
    return result


async def _show_player_fixtures(
    player: Player, team: Team | None, teams: dict[int, Team],
    client: FPLClient, mode: str = "difference",
) -> None:
    """Show upcoming fixture run for a player with positional FDR."""
    from fpl_cli.services.team_ratings import TeamRatingsService

    fixture_data = await _get_fixture_run_data(player, team, teams, client, mode=mode)
    if not fixture_data:
        console.print("[yellow]No upcoming gameweeks found[/yellow]")
        return

    start_gw = fixture_data[0]["gameweek"]
    end_gw = fixture_data[-1]["gameweek"]
    console.print(f"\n[bold]Fixture Run (GW{start_gw}-{end_gw}):[/bold]")

    # Check for stale ratings
    ratings_service = TeamRatingsService()
    await ratings_service.ensure_fresh(client)
    staleness_warning = ratings_service.get_staleness_warning()
    if staleness_warning:
        console.print(f"[dim]{staleness_warning}[/dim]")

    total_fdr = 0.0
    fixture_count = 0
    home_count = 0
    away_count = 0
    blank_gws = []
    double_gws = []

    for gw_data in fixture_data:
        gw = gw_data["gameweek"]
        gw_fixtures = gw_data["fixtures"]

        if not gw_fixtures:
            blank_gws.append(gw)
            console.print(f"  GW{gw}: [dim]— BLANK —[/dim]")
            continue

        if len(gw_fixtures) > 1:
            double_gws.append(gw)

        for fx in gw_fixtures:
            pos_fdr = fx["fdr"]
            total_fdr += pos_fdr
            fixture_count += 1
            if fx["is_home"]:
                home_count += 1
            else:
                away_count += 1

            opp_display = fx["opponent"].upper() if fx["is_home"] else fx["opponent"].lower()
            bar_length = int(pos_fdr)
            bar = "\u2588" * bar_length

            fdr_style = _fdr_style(pos_fdr)
            label = " \u2190 Tough" if fdr_style == "red" else ""

            dgw_marker = " [cyan](DGW)[/cyan]" if len(gw_fixtures) > 1 else ""
            console.print(
                f"  GW{gw}: {opp_display} "
                f"[{fdr_style}]{bar} {pos_fdr:.1f}[/{fdr_style}]{label}{dgw_marker}"
            )

    avg_fdr = total_fdr / fixture_count if fixture_count > 0 else 0
    avg_style = _fdr_style(avg_fdr)
    blank_str = ", ".join(f"GW{gw}" for gw in blank_gws) if blank_gws else "None"
    double_str = ", ".join(f"GW{gw}" for gw in double_gws) if double_gws else "None"
    console.print(
        f"\n  Avg FDR: [{avg_style}]{avg_fdr:.2f}[/{avg_style}] | "
        f"Home: {home_count} | Away: {away_count} | "
        f"Blank: {blank_str} | DGW: {double_str}"
    )


def _show_understat_analysis(player_data: dict, name: str) -> None:
    """Display combined Understat analysis: shots + situation profile with staleness caveat."""
    from datetime import datetime

    from rich.table import Table

    from fpl_cli.season import understat_season

    current_season = understat_season()

    # Compute data-through date from matches
    all_matches = player_data.get("matches", [])
    season_matches = [m for m in all_matches if m.get("season") == current_season]
    if not season_matches:
        season_matches = all_matches
    match_dates = [m.get("date", "")[:10] for m in season_matches if m.get("date")]
    data_through = max(match_dates) if match_dates else None

    # Staleness warning
    if data_through:
        try:
            through_date = datetime.strptime(data_through, "%Y-%m-%d")
            days_old = (datetime.now() - through_date).days
            if days_old > 14:
                staleness_msg = f"Understat data through: {data_through} ({days_old} days ago)"
                console.print(f"[yellow]{staleness_msg}[/yellow]")
            else:
                console.print(f"Understat data through: {data_through}", style="dim")
        except ValueError:
            console.print(f"Understat data through: {data_through}", style="dim")

    # Shot analysis
    all_shots = player_data.get("shots", [])
    shots = [s for s in all_shots if s.get("season") == current_season]
    if not shots:
        seasons = sorted({s.get("season", "") for s in all_shots}, reverse=True)
        if seasons:
            shots = [s for s in all_shots if s.get("season") == seasons[0]]

    if shots:
        total_shots = len(shots)
        on_target = sum(1 for s in shots if s.get("result") in ("Goal", "SavedShot"))
        total_xg = sum(float(s.get("xG", 0)) for s in shots)
        avg_xg = total_xg / total_shots if total_shots else 0
        by_head = sum(1 for s in shots if s.get("shotType") == "Head")
        by_foot = total_shots - by_head

        situations: dict[str, int] = {}
        for s in shots:
            sit = s.get("situation", "Unknown")
            situations[sit] = situations.get(sit, 0) + 1

        table = Table(title=f"Shot Analysis: {name}")
        table.add_column("Metric", style="bold")
        table.add_column("Value", justify="right")
        table.add_row("Total shots", str(total_shots))
        table.add_row("On target", str(on_target))
        table.add_row("Total xG", f"{total_xg:.2f}")
        table.add_row("Avg xG/shot", f"{avg_xg:.3f}")
        table.add_row("Foot / Head", f"{by_foot} / {by_head}")
        for sit, count in sorted(situations.items(), key=lambda x: x[1], reverse=True):
            table.add_row(f"  {sit}", str(count))
        console.print(table)
    else:
        console.print("[yellow]No shot data available[/yellow]")

    # Situation profile
    groups = player_data.get("groups", {})
    situation_by_season = groups.get("situation", {})
    season = current_season
    situation_data = situation_by_season.get(season)
    if not situation_data:
        available = sorted(situation_by_season.keys(), reverse=True)
        if available:
            season = available[0]
            situation_data = situation_by_season[season]

    if situation_data:
        profile_table = Table(title=f"Situation Profile: {name} ({season})")
        profile_table.add_column("Situation", style="bold")
        profile_table.add_column("xG", justify="right")
        profile_table.add_column("%", justify="right")
        profile_table.add_column("Shots", justify="right")
        profile_table.add_column("Goals", justify="right")

        total_xg = sum(float(v.get("xG", 0)) for v in situation_data.values())
        rows = []
        for situation, data in situation_data.items():
            xg = float(data.get("xG", 0))
            pct = (xg / total_xg * 100) if total_xg > 0 else 0
            rows.append((situation, xg, pct, data.get("shots", "0"), data.get("goals", "0")))
        rows.sort(key=lambda r: r[1], reverse=True)

        for situation, xg, pct, shots_count, goals in rows:
            profile_table.add_row(
                situation, f"{xg:.2f}", f"{pct:.0f}%", str(shots_count), str(goals),
            )
        console.print(profile_table)
    else:
        console.print("[yellow]No situation breakdown available[/yellow]")


def _show_player_history(profile: PlayerProfile) -> None:
    """Display historical career arc table."""
    from rich.table import Table

    table = Table(title=f"Career History: {profile.web_name}")
    table.add_column("Season", style="bold")
    table.add_column("Pts", justify="right")
    table.add_column("Min", justify="right")
    table.add_column("Starts", justify="right")
    table.add_column("G", justify="right")
    table.add_column("A", justify="right")
    table.add_column("xGI", justify="right")
    table.add_column("Cost", justify="right")

    for s in profile.seasons:
        cost_str = f"\u00a3{s.start_cost / 10:.1f}m\u2192\u00a3{s.end_cost / 10:.1f}m"
        xgi_str = f"{s.expected_goal_involvements:.1f}" if s.expected_goal_involvements > 0 else "-"
        min_style = "dim" if s.minutes < 450 else ""
        table.add_row(
            s.season, str(s.total_points), str(s.minutes), str(s.starts),
            str(s.goals), str(s.assists), xgi_str, cost_str,
            style=min_style,
        )

    console.print(table)

    # Trend summary
    trend_lines = []
    n_qualifying = len(profile.pts_per_90)
    label = "trend" if n_qualifying >= 3 else "change"

    if profile.pts_per_90:
        direction = "\u2191" if profile.pts_per_90_trend > 0 else "\u2193" if profile.pts_per_90_trend < 0 else "\u2192"
        trend_lines.append(f"Pts/90 {label}: {direction} {abs(profile.pts_per_90_trend):.2f}")

    if profile.xgi_per_90_trend is not None:
        direction = "\u2191" if profile.xgi_per_90_trend > 0 else "\u2193" if profile.xgi_per_90_trend < 0 else "\u2192"
        trend_lines.append(f"xGI/90 {label}: {direction} {abs(profile.xgi_per_90_trend):.2f}")

    if profile.cost_trajectory != 0:
        direction = "\u2191" if profile.cost_trajectory > 0 else "\u2193"
        trend_lines.append(f"Cost {label}: {direction} \u00a3{abs(profile.cost_trajectory / 10):.1f}m/season")

    if trend_lines:
        console.print("  ".join(trend_lines))


# --- JSON helper functions ---



def _build_detail_json(player_data: dict, position: str, teams: dict) -> list[dict]:
    """Build match detail data for JSON output."""
    history = player_data.get("history", [])
    if not history:
        return []
    recent = history[-10:][::-1]
    result = []
    is_gk = position == "GK"
    for h in recent:
        opponent_id = h.get("opponent_team")
        opponent = teams.get(opponent_id)
        entry: dict = {
            "gameweek": h.get("round"),
            "opponent": opponent.short_name if opponent else "???",
            "was_home": h.get("was_home", False),
            "minutes": h.get("minutes", 0),
            "assists": h.get("assists", 0),
            "expected_assists": float(h.get("expected_assists", 0)),
            "bonus": h.get("bonus", 0),
            "total_points": h.get("total_points", 0),
        }
        if not is_gk:
            entry["goals_scored"] = h.get("goals_scored", 0)
            entry["expected_goals"] = float(h.get("expected_goals", 0))
        if position in ("GK", "DEF"):
            entry["clean_sheets"] = h.get("clean_sheets", 0)
            entry["goals_conceded"] = h.get("goals_conceded", 0)
            entry["expected_goals_conceded"] = float(h.get("expected_goals_conceded", 0))
        if is_gk:
            entry["saves"] = h.get("saves", 0)
        result.append(entry)
    return result


def _build_history_json(profile: PlayerProfile) -> dict:
    """Build career history data for JSON output."""
    seasons = []
    for s in profile.seasons:
        seasons.append({
            "season": s.season,
            "team": s.team_id,
            "total_points": s.total_points,
            "minutes": s.minutes,
            "starts": s.starts,
            "goals": s.goals,
            "assists": s.assists,
            "expected_goal_involvements": s.expected_goal_involvements,
            "start_cost": s.start_cost,
            "end_cost": s.end_cost,
        })
    return {
        "seasons": seasons,
        "trends": {
            "pts_per_90": profile.pts_per_90,
            "pts_per_90_trend": profile.pts_per_90_trend,
            "xgi_per_90": profile.xgi_per_90,
            "xgi_per_90_trend": profile.xgi_per_90_trend,
            "cost_trajectory": profile.cost_trajectory,
        },
    }
