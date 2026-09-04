"""FPL gameweek fixtures display."""
# Pattern: direct-api

from __future__ import annotations

import asyncio

import click
from click.core import ParameterSource
from rich.panel import Panel
from rich.table import Table

from fpl_cli.cli._context import (
    CLIContext,
    console,
    error_console,
    is_custom_analysis_enabled,
)
from fpl_cli.cli._helpers import _api_fdr_style, _fdr_style
from fpl_cli.cli._json import (
    api_failure_boundary,
    emit_failure,
    emit_json,
    emit_json_error,
    output_format_option,
)

_API_SCALE_FOOTER = (
    "FDR scale: 1 (easiest) - 5 (hardest), the FPL API's own difficulty. "
    "Enable custom analysis (`fpl init`) for the venue-aware 1-7 team-ratings FDR."
)


@click.command("fixtures")
@click.option("--gameweek", "-g", type=int, help="Gameweek number (default: next)")
@click.option("--mode", "-m", type=click.Choice(["difference", "opponent"]), default="difference",
              help="FDR mode: 'difference' (team vs opponent) or 'opponent' (opponent rating only)")
@output_format_option
@click.pass_context
def fixtures_command(ctx: click.Context, gameweek: int | None, mode: str, output_format: str):
    """Show fixtures for a gameweek."""
    # Same gate as `fpl fdr` and `fpl preview`: with custom analysis off, the
    # canonical FPL API difficulty rather than the Bayesian team ratings, so
    # one match reads the same whichever command prints it (#202).
    settings = ctx.obj.settings if isinstance(ctx.obj, CLIContext) else {}
    custom_on = is_custom_analysis_enabled(settings)

    # --mode only means something against the team ratings. Saying so beats
    # accepting the flag and quietly printing the same table either way; it is
    # a note rather than an error so the fixture list still comes back.
    if not custom_on and ctx.get_parameter_source("mode") == ParameterSource.COMMANDLINE:
        error_console.print(
            "[yellow]--mode applies to the team-ratings FDR and custom analysis is off; "
            "showing FPL API difficulty.[/yellow]"
        )

    async def _run():
        from fpl_cli.api.fpl import FPLClient
        from fpl_cli.services.team_ratings import TeamRatingsService, fdr_scale_footer
        from fpl_cli.utils.time import format_kickoff

        async def _resolve_ratings(client) -> TeamRatingsService | None:
            """The ratings service, or None when custom analysis is off.

            None is the signal to fall back to API difficulty, so the opted-out
            path never constructs a service or refreshes ratings at all.
            """
            if not custom_on:
                return None
            service = TeamRatingsService()
            await service.ensure_fresh(client)
            return service

        def _fixture_fdr(fixture, home_name, away_name, service) -> tuple[float, float]:
            """(home, away) FDR for one fixture, on whichever scale is in play."""
            if service is None:
                return fixture.home_difficulty, fixture.away_difficulty
            return (
                service.get_fixture_fdr(home_name, away_name, "home", mode=mode),
                service.get_fixture_fdr(away_name, home_name, "away", mode=mode),
            )

        def _ratings_warnings(service) -> list[dict[str, str]]:
            """Coded `metadata.warnings` entries; empty when ratings are not in play.

            Always a list, so a consumer indexes it rather than checking for the
            key first, matching `fdr` and `stats`. Without it a table of flat
            4.0s reads as analysis rather than as "nothing to rate these on".
            """
            warning = service.get_staleness_warning() if service is not None else None
            return [{"code": "team_ratings_unusable", "message": warning}] if warning else []

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

                    ratings_service = await _resolve_ratings(client)

                    fixtures_list = []
                    for fixture in fixtures_data:
                        home_team = teams.get(fixture.home_team_id)
                        away_team = teams.get(fixture.away_team_id)
                        home_name = home_team.short_name if home_team else "???"
                        away_name = away_team.short_name if away_team else "???"

                        home_fdr, away_fdr = _fixture_fdr(fixture, home_name, away_name, ratings_service)

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

                    emit_json("fixtures", fixtures_list, metadata={
                        "gameweek": gw_num,
                        "custom_analysis": custom_on,
                        # The scale the fdr values are on, so a consumer never
                        # has to infer it from the numbers it happened to get
                        "fdr_scale": "team_ratings_1_7" if custom_on else "fpl_api_1_5",
                        "fdr_mode": mode if custom_on else None,
                        "warnings": _ratings_warnings(ratings_service),
                    })
                except Exception as e:  # noqa: BLE001 — display resilience
                    emit_json_error("fixtures", str(e))
                return

            title = f"[bold blue]Gameweek {gw_num} Fixtures[/bold blue]"
            if not custom_on:
                title += " [dim]- FPL API Ratings[/dim]"
            console.print(Panel.fit(title))

            try:
                fixtures_data = await client.get_fixtures(gameweek=gw_num)
                teams = {t.id: t for t in await client.get_teams()}

                ratings_service = await _resolve_ratings(client)

                # Same rating-quality note `fpl fdr` and `fpl preview` print:
                # with no usable ratings every FDR below is the neutral 4.0,
                # and a flat table needs saying so.
                for warning in _ratings_warnings(ratings_service):
                    error_console.print(f"[yellow]{warning['message']}[/yellow]\n")

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
                    home_fdr, away_fdr = _fixture_fdr(fixture, home_name, away_name, ratings_service)

                    if ratings_service is not None:
                        home_fdr_str, away_fdr_str = f"{home_fdr:.1f}", f"{away_fdr:.1f}"
                        home_fdr_style, away_fdr_style = _fdr_style(home_fdr), _fdr_style(away_fdr)
                    else:
                        home_fdr_str, away_fdr_str = str(home_fdr), str(away_fdr)
                        home_fdr_style, away_fdr_style = _api_fdr_style(home_fdr), _api_fdr_style(away_fdr)

                    kickoff = format_kickoff(fixture.kickoff_time) if fixture.kickoff_time else "TBC"

                    if fixture.finished:
                        score = f"{fixture.home_score} - {fixture.away_score}"
                    else:
                        score = "vs"

                    table.add_row(
                        home_name,
                        f"[{home_fdr_style}]{home_fdr_str}[/{home_fdr_style}]",
                        score,
                        f"[{away_fdr_style}]{away_fdr_str}[/{away_fdr_style}]",
                        away_name,
                        kickoff,
                    )

                console.print(table)
                console.print(f"[dim]{fdr_scale_footer(mode) if custom_on else _API_SCALE_FOOTER}[/dim]")

            except Exception as e:  # noqa: BLE001 — display resilience
                # Reports and exits 1. Printing and falling off the end left
                # table mode exiting 0 on a failure, and swallowed the error
                # before `api_failure_boundary` below could see it (#159 review).
                emit_failure("fixtures", f"Error fetching fixtures: {e}", output_format, cause=e)

    with api_failure_boundary("fixtures", output_format):
        asyncio.run(_run())
