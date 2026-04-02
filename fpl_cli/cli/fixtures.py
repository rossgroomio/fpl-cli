"""FPL gameweek fixtures display."""
# Pattern: direct-api

from __future__ import annotations

import asyncio

import click
from rich.panel import Panel
from rich.table import Table

from fpl_cli.cli._context import console
from fpl_cli.cli._helpers import _fdr_style
from fpl_cli.cli._json import emit_json, emit_json_error, output_format_option


@click.command("fixtures")
@click.option("--gameweek", "-g", type=int, help="Gameweek number (default: next)")
@output_format_option
def fixtures_command(gameweek: int | None, output_format: str):
    """Show fixtures for a gameweek."""

    async def _run():
        from fpl_cli.api.fpl import FPLClient
        from fpl_cli.services.team_ratings import TeamRatingsService

        async with FPLClient() as client:
            # Default to next gameweek if not specified
            if gameweek is None:
                next_gw = await client.get_next_gameweek()
                gw_num = next_gw["id"] if next_gw else 1
            else:
                gw_num = gameweek

            if output_format == "json":
                try:
                    fixtures_data = await client.get_fixtures(gameweek=gw_num)
                    teams = {t.id: t for t in await client.get_teams()}
                    ratings_service = TeamRatingsService()
                    await ratings_service.ensure_fresh(client)

                    fixtures_list = []
                    for fixture in fixtures_data:
                        home_team = teams.get(fixture.home_team_id)
                        away_team = teams.get(fixture.away_team_id)
                        home_name = home_team.short_name if home_team else "???"
                        away_name = away_team.short_name if away_team else "???"

                        away_rating = ratings_service.get_rating(away_name)
                        home_fdr = away_rating.avg_overall_fdr if away_rating else fixture.home_difficulty
                        home_rating = ratings_service.get_rating(home_name)
                        away_fdr = home_rating.avg_overall_fdr if home_rating else fixture.away_difficulty

                        fixtures_list.append({
                            "home": home_name,
                            "away": away_name,
                            "home_fdr": home_fdr,
                            "away_fdr": away_fdr,
                            "kickoff": fixture.kickoff_time.isoformat() if fixture.kickoff_time else None,
                            "finished": bool(fixture.finished),
                            "home_score": fixture.home_score,
                            "away_score": fixture.away_score,
                        })

                    emit_json("fixtures", fixtures_list, metadata={"gameweek": gw_num})
                except Exception as e:  # noqa: BLE001 — display resilience
                    emit_json_error("fixtures", str(e))
                return

            console.print(Panel.fit(f"[bold blue]Gameweek {gw_num} Fixtures[/bold blue]"))

            try:
                fixtures_data = await client.get_fixtures(gameweek=gw_num)
                teams = {t.id: t for t in await client.get_teams()}
                ratings_service = TeamRatingsService()
                await ratings_service.ensure_fresh(client)

                table = Table(show_header=True, header_style="bold")
                table.add_column("Home")
                table.add_column("FDR", justify="center")
                table.add_column("", justify="center")
                table.add_column("FDR", justify="center")
                table.add_column("Away")
                table.add_column("Kickoff")

                for fixture in fixtures_data:
                    home_team = teams.get(fixture.home_team_id)
                    away_team = teams.get(fixture.away_team_id)

                    home_name = home_team.short_name if home_team else "???"
                    away_name = away_team.short_name if away_team else "???"

                    # Team ratings FDR, fallback to FPL API
                    away_rating = ratings_service.get_rating(away_name)
                    home_fdr = away_rating.avg_overall_fdr if away_rating else fixture.home_difficulty
                    home_rating = ratings_service.get_rating(home_name)
                    away_fdr = home_rating.avg_overall_fdr if home_rating else fixture.away_difficulty

                    home_fdr_style = _fdr_style(home_fdr)
                    away_fdr_style = _fdr_style(away_fdr)

                    kickoff = fixture.kickoff_time.strftime("%a %H:%M") if fixture.kickoff_time else "TBC"

                    if fixture.finished:
                        score = f"{fixture.home_score} - {fixture.away_score}"
                    else:
                        score = "vs"

                    # Format FDR with 1 decimal for floats
                    home_fdr_str = f"{home_fdr:.1f}" if isinstance(home_fdr, float) else str(home_fdr)
                    away_fdr_str = f"{away_fdr:.1f}" if isinstance(away_fdr, float) else str(away_fdr)

                    table.add_row(
                        home_name,
                        f"[{home_fdr_style}]{home_fdr_str}[/{home_fdr_style}]",
                        score,
                        f"[{away_fdr_style}]{away_fdr_str}[/{away_fdr_style}]",
                        away_name,
                        kickoff,
                    )

                console.print(table)

            except Exception as e:  # noqa: BLE001 — display resilience
                console.print(f"[red]Error fetching fixtures: {e}[/red]")

    asyncio.run(_run())
