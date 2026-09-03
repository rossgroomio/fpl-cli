"""FPL gameweek fixtures display."""
# Pattern: direct-api

from __future__ import annotations

import asyncio

import click
from rich.panel import Panel
from rich.table import Table

from fpl_cli.cli._context import console, error_console
from fpl_cli.cli._helpers import _fdr_style
from fpl_cli.cli._json import (
    api_failure_boundary,
    emit_failure,
    emit_json,
    emit_json_error,
    output_format_option,
)


@click.command("fixtures")
@click.option("--gameweek", "-g", type=int, help="Gameweek number (default: next)")
@click.option("--mode", "-m", type=click.Choice(["difference", "opponent"]), default="difference",
              help="FDR mode: 'difference' (team vs opponent) or 'opponent' (opponent rating only)")
@output_format_option
def fixtures_command(gameweek: int | None, mode: str, output_format: str):
    """Show fixtures for a gameweek."""

    async def _run():
        from fpl_cli.api.fpl import FPLClient
        from fpl_cli.services.team_ratings import TeamRatingsService, fdr_scale_footer
        from fpl_cli.utils.time import format_kickoff

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

                        home_fdr = ratings_service.get_fixture_fdr(home_name, away_name, "home", mode=mode)
                        away_fdr = ratings_service.get_fixture_fdr(away_name, home_name, "away", mode=mode)

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

                    # `warnings` is always present, empty or not, matching `fdr`
                    # and `stats`: a consumer indexes it rather than checking
                    # for the key first. Without it a table of flat 4.0s reads
                    # as analysis rather than as "nothing to rate these on".
                    warnings: list[dict[str, str]] = []
                    ratings_warning = ratings_service.get_staleness_warning()
                    if ratings_warning:
                        warnings.append({
                            "code": "team_ratings_unusable",
                            "message": ratings_warning,
                        })

                    emit_json("fixtures", fixtures_list, metadata={
                        "gameweek": gw_num,
                        "fdr_mode": mode,
                        "warnings": warnings,
                    })
                except Exception as e:  # noqa: BLE001 — display resilience
                    emit_json_error("fixtures", str(e))
                return

            console.print(Panel.fit(f"[bold blue]Gameweek {gw_num} Fixtures[/bold blue]"))

            try:
                fixtures_data = await client.get_fixtures(gameweek=gw_num)
                teams = {t.id: t for t in await client.get_teams()}
                ratings_service = TeamRatingsService()
                await ratings_service.ensure_fresh(client)

                # Same rating-quality note `fpl fdr` and `fpl preview` print:
                # with no usable ratings every FDR below is the neutral 4.0,
                # and a flat table needs saying so.
                ratings_warning = ratings_service.get_staleness_warning()
                if ratings_warning:
                    error_console.print(f"[yellow]{ratings_warning}[/yellow]\n")

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

                    # The general FDR the fixture agent scores, so this table and
                    # the preview's Gameweek Fixtures table agree on a match (#202)
                    home_fdr = ratings_service.get_fixture_fdr(home_name, away_name, "home", mode=mode)
                    away_fdr = ratings_service.get_fixture_fdr(away_name, home_name, "away", mode=mode)

                    home_fdr_style = _fdr_style(home_fdr)
                    away_fdr_style = _fdr_style(away_fdr)

                    kickoff = format_kickoff(fixture.kickoff_time) if fixture.kickoff_time else "TBC"

                    if fixture.finished:
                        score = f"{fixture.home_score} - {fixture.away_score}"
                    else:
                        score = "vs"

                    table.add_row(
                        home_name,
                        f"[{home_fdr_style}]{home_fdr:.1f}[/{home_fdr_style}]",
                        score,
                        f"[{away_fdr_style}]{away_fdr:.1f}[/{away_fdr_style}]",
                        away_name,
                        kickoff,
                    )

                console.print(table)
                console.print(f"[dim]{fdr_scale_footer(mode)}[/dim]")

            except Exception as e:  # noqa: BLE001 — display resilience
                # Reports and exits 1. Printing and falling off the end left
                # table mode exiting 0 on a failure, and swallowed the error
                # before `api_failure_boundary` below could see it (#159 review).
                emit_failure("fixtures", f"Error fetching fixtures: {e}", output_format, cause=e)

    with api_failure_boundary("fixtures", output_format):
        asyncio.run(_run())
