"""Review analysis helpers: global stats, fixtures, league table."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, TypedDict

from rich.markup import escape as rich_escape
from rich.table import Table

from fpl_cli.cli._context import console

if TYPE_CHECKING:
    from fpl_cli.models.fixture import Fixture
    from fpl_cli.services.fixture_predictions import DoublePrediction


class GlobalReviewData(TypedDict, total=False):
    """Typed shape for the global_data dict threaded through the review pipeline."""

    summary: dict[str, Any]
    dream_team: list[dict[str, Any]]
    blankers: list[dict[str, Any]]
    bgw_team_names: set[str]
    dgw_team_names: set[str]
    predicted_dgw_teams: list[DoublePrediction]


async def _review_global_stats(
    client, gw, player_map, teams, live_stats,
    *,
    bgw_team_ids: frozenset[int] = frozenset(),
):
    """Fetch global GW stats: top scorers, dream team, blankers. Returns dict."""
    global_data: GlobalReviewData = {}
    try:
        console.print("\n[bold]## Global[/bold]")

        # Get top scorers for this gameweek (from event live data)
        top_scorers = sorted(
            [(pid, stats.get("total_points", 0)) for pid, stats in live_stats.items()],
            key=lambda x: x[1],
            reverse=True
        )[:5]

        console.print("\n[bold]### Summary[/bold]")

        # Top GW scorers
        console.print("\n[dim]Top Scorers This GW:[/dim]")
        for pid, pts in top_scorers:
            player = player_map.get(pid)
            if player:
                team = teams.get(player.team_id)
                team_abbr = team.short_name if team else "???"
                console.print(f"  {player.web_name} ({team_abbr}) - {pts} pts")

        # Store summary data for report
        global_data["summary"] = {
            "top_scorers": [
                {
                    "name": player_map[pid].web_name,
                    "team": (
                        teams.get(player_map[pid].team_id).short_name
                        if teams.get(player_map[pid].team_id)
                        else "???"
                    ),
                    "points": pts,
                }
                for pid, pts in top_scorers
                if player_map.get(pid)
            ],
        }

        # Dream Team
        console.print("\n[bold]### Dream Team[/bold]")
        try:
            dream_team_data = await client.get_dream_team(gw)
            dream_team_players = dream_team_data.get("team", [])

            if dream_team_players:
                dt_table = Table(show_header=True, header_style="bold")
                dt_table.add_column("Player")
                dt_table.add_column("Team")
                dt_table.add_column("Pos")
                dt_table.add_column("Pts", justify="right")

                dream_team_list = []
                for dt_player in dream_team_players:
                    player = player_map.get(dt_player.get("element"))
                    if player:
                        team = teams.get(player.team_id)
                        team_abbr = team.short_name if team else "???"
                        pts = dt_player.get("points", 0)
                        dream_team_list.append({
                            "name": player.web_name,
                            "team": team_abbr,
                            "position": player.position_name,
                            "points": pts,
                        })
                        dt_table.add_row(player.web_name, team_abbr, player.position_name, str(pts))

                console.print(dt_table)

                # Top player highlight
                top_player = dream_team_data.get("top_player")
                if top_player:
                    top_p = player_map.get(top_player.get("id"))
                    if top_p:
                        console.print(
                            f"\n[bold green]Star Player:[/bold green]"
                            f" {top_p.web_name} ({top_player.get('points', 0)} pts)"
                        )

                global_data["dream_team"] = dream_team_list
            else:
                console.print("[dim]Dream team not available[/dim]")
        except Exception as e:  # noqa: BLE001 — best-effort enrichment
            console.print(f"[dim]Could not fetch dream team: {rich_escape(str(e))}[/dim]")

        # Blankers: High-ownership players (>5%) who scored ≤2 pts (excludes BGW teams)
        console.print("\n[bold]### Blankers[/bold]")
        try:
            # Build blankers list from live_stats and players data
            blankers_list = []
            for elem_id, stats in live_stats.items():
                gw_pts = stats.get("total_points", 0)
                player = player_map.get(elem_id)
                if player and gw_pts <= 2 and player.team_id not in bgw_team_ids:
                    ownership = player.selected_by_percent
                    if ownership > 5.0:
                        team = teams.get(player.team_id)
                        team_abbr = team.short_name if team else "???"
                        blankers_list.append({
                            "name": player.web_name,
                            "team": team_abbr,
                            "ownership": ownership,
                            "points": gw_pts,
                        })

            # Sort by ownership descending, take top 10
            blankers_list.sort(key=lambda x: x["ownership"], reverse=True)
            blankers_list = blankers_list[:10]

            if blankers_list:
                blankers_table = Table(show_header=True, header_style="bold")
                blankers_table.add_column("Player")
                blankers_table.add_column("Team")
                blankers_table.add_column("Own%", justify="right")
                blankers_table.add_column("Pts", justify="right")

                for b in blankers_list:
                    blankers_table.add_row(
                        b["name"],
                        b["team"],
                        f"{b['ownership']:.1f}%",
                        str(b["points"]),
                    )
                console.print(blankers_table)
                global_data["blankers"] = blankers_list
            else:
                console.print("[dim]No high-ownership blankers this GW[/dim]")
        except Exception as e:  # noqa: BLE001 — best-effort enrichment
            console.print(f"[dim]Could not calculate blankers: {rich_escape(str(e))}[/dim]")

    except Exception as e:  # noqa: BLE001 — display resilience
        console.print(f"[yellow]Could not fetch global stats: {rich_escape(str(e))}[/yellow]")

    return global_data


async def _review_fixtures(client, gw, player_map, teams, my_picks_data, *, fixtures: list[Fixture] | None = None):
    """Fetch and display fixture results. Returns list of fixture data.

    Args:
        fixtures: Pre-fetched Fixture objects. When provided, skips the API call.
    """
    fixtures_data = []
    console.print("\n" + "-" * 50)
    console.print("\n[bold cyan]# Results[/bold cyan]")
    try:
        resolved_fixtures = fixtures if fixtures is not None else await client.get_fixtures(gw)
        finished_fixtures = [f for f in resolved_fixtures if f.finished]

        for fixture in finished_fixtures:
            home_team = teams.get(fixture.home_team_id)
            away_team = teams.get(fixture.away_team_id)
            home_name = home_team.short_name if home_team else "???"
            away_name = away_team.short_name if away_team else "???"
            home_score = fixture.home_score or 0
            away_score = fixture.away_score or 0

            # Fixture result line
            console.print(f"\n  [bold]{home_name} {home_score}-{away_score} {away_name}[/bold]")

            # Goal scorers
            goal_scorers = fixture.get_goal_scorers()
            goal_strs = []
            if goal_scorers:
                # Group by player and count goals
                goals_by_player = {}
                for g in goal_scorers:
                    player = player_map.get(g.get("element"))
                    if player:
                        team = teams.get(player.team_id)
                        team_abbr = team.short_name if team else "???"
                        name_with_team = f"{player.web_name} ({team_abbr})"
                        count = g.get("value", 1)
                        if name_with_team in goals_by_player:
                            goals_by_player[name_with_team] += count
                        else:
                            goals_by_player[name_with_team] = count

                for name, count in goals_by_player.items():
                    if count > 1:
                        # Extract base name and append count
                        base_name = name.rsplit(" (", 1)[0]
                        team_part = name.rsplit(" (", 1)[1] if " (" in name else ""
                        goal_strs.append(f"{base_name} x{count} ({team_part}" if team_part else f"{base_name} x{count}")
                    else:
                        goal_strs.append(name)
                console.print(f"    Goals: {', '.join(goal_strs)}")

            # Assists
            assists = fixture.get_assists()
            assist_names = []
            if assists:
                for a in assists:
                    player = player_map.get(a.get("element"))
                    if player:
                        team = teams.get(player.team_id)
                        team_abbr = team.short_name if team else "???"
                        count = a.get("value", 1)
                        if count > 1:
                            assist_names.append(f"{player.web_name} x{count} ({team_abbr})")
                        else:
                            assist_names.append(f"{player.web_name} ({team_abbr})")
                if assist_names:
                    console.print(f"    Assists: {', '.join(assist_names)}")

            # Bonus points
            bonus = fixture.get_bonus()
            bonus_strs = []
            if bonus:
                for b in bonus:
                    player = player_map.get(b.get("element"))
                    if player:
                        team = teams.get(player.team_id)
                        team_abbr = team.short_name if team else "???"
                        pts = b.get("value", 0)
                        bonus_strs.append(f"{player.web_name} ({team_abbr}, {pts})")
                if bonus_strs:
                    console.print(f"    Bonus: {', '.join(bonus_strs)}")

            # Red cards
            red_cards = fixture.get_red_cards()
            red_card_strs = []
            red_card_strs_plain = []
            if red_cards:
                for r in red_cards:
                    player = player_map.get(r.get("element"))
                    if player:
                        team = teams.get(player.team_id)
                        team_abbr = team.short_name if team else "???"
                        is_my_player = any(p.get("id") == player.id for p in my_picks_data)
                        if is_my_player:
                            red_card_strs.append(f"[bold red]{player.web_name} ({team_abbr}) ⚠️ YOUR PLAYER[/bold red]")
                            red_card_strs_plain.append(f"{player.web_name} ({team_abbr}) ⚠️ YOUR PLAYER")
                        else:
                            red_card_strs.append(f"{player.web_name} ({team_abbr})")
                            red_card_strs_plain.append(f"{player.web_name} ({team_abbr})")
                if red_card_strs:
                    console.print(f"    [red]Red Cards: {', '.join(red_card_strs)}[/red]")

            # Own goals
            own_goals = fixture.get_own_goals()
            own_goal_strs = []
            if own_goals:
                for og in own_goals:
                    player = player_map.get(og.get("element"))
                    if player:
                        team = teams.get(player.team_id)
                        team_abbr = team.short_name if team else "???"
                        own_goal_strs.append(f"{player.web_name} ({team_abbr})")
                if own_goal_strs:
                    console.print(f"    [red]Own Goals: {', '.join(own_goal_strs)}[/red]")

            # Store fixture data for report
            fixtures_data.append({
                "home_team": home_name,
                "away_team": away_name,
                "home_score": home_score,
                "away_score": away_score,
                "goals": ", ".join(goal_strs) if goal_scorers and goal_strs else None,
                "assists": ", ".join(assist_names) if assists and assist_names else None,
                "bonus": ", ".join(bonus_strs) if bonus_strs else None,
                "red_cards": ", ".join(red_card_strs_plain) if red_card_strs_plain else None,
                "own_goals": ", ".join(own_goal_strs) if own_goal_strs else None,
            })

    except Exception as e:  # noqa: BLE001 — display resilience
        console.print(f"[yellow]Could not fetch fixture results: {rich_escape(str(e))}[/yellow]")

    return fixtures_data


async def _review_league_table():
    """Fetch PL league table from football-data.org. Returns list of standings."""
    league_table_data = []
    try:
        from fpl_cli.api.football_data import FootballDataClient

        async with FootballDataClient() as fd_client:
            if fd_client.is_configured:
                league_table_data = await fd_client.get_standings()
    except Exception as e:  # noqa: BLE001 — graceful degradation
        console.print(f"[yellow]Could not fetch league table: {rich_escape(str(e))}[/yellow]")

    if league_table_data:
        console.print("\n[bold cyan]## League Table[/bold cyan]")
        lt_table = Table(show_header=True, header_style="bold")
        lt_table.add_column("Pos", justify="right")
        lt_table.add_column("Team")
        lt_table.add_column("P", justify="right")
        lt_table.add_column("W", justify="right")
        lt_table.add_column("D", justify="right")
        lt_table.add_column("L", justify="right")
        lt_table.add_column("GD", justify="right")
        lt_table.add_column("Pts", justify="right")
        for t in league_table_data:
            lt_table.add_row(
                str(t["position"]),
                t["name"],
                str(t["played"]),
                str(t["win"]),
                str(t["draw"]),
                str(t["loss"]),
                str(t["goal_difference"]),
                str(t["points"]),
            )
        console.print(lt_table)

    return league_table_data
