"""Team ratings command group."""
# Pattern: direct-api

from __future__ import annotations

import asyncio

import click
from rich.markup import escape as rich_escape
from rich.panel import Panel
from rich.table import Table

from fpl_cli.cli._context import console, error_console


@click.group("ratings", invoke_without_command=True)
@click.pass_context
def ratings_group(ctx):
    """Display or recalculate team ratings."""
    if ctx.invoked_subcommand is None:
        _show_ratings()


def _show_ratings():
    """Display current team ratings with staleness info."""
    from fpl_cli.api.fpl import FPLClient
    from fpl_cli.services.team_ratings import TeamRatingsService

    service = TeamRatingsService()

    async def _refresh():
        async with FPLClient() as client:
            await service.ensure_fresh(client)

    asyncio.run(_refresh())
    ratings_data = service.get_all_ratings()

    # Check staleness
    warning = service.get_staleness_warning()
    if warning:
        error_console.print(f"[yellow]{warning}[/yellow]\n")

    # Show metadata
    meta = service.metadata
    if meta and meta.last_updated:
        console.print(f"[dim]Last updated: {meta.last_updated.strftime('%Y-%m-%d')}[/dim]")
        console.print(f"[dim]Source: {meta.source or 'unknown'}[/dim]")
        if meta.based_on_gws:
            console.print(f"[dim]Based on: GW{meta.based_on_gws[0]}-{meta.based_on_gws[1]}[/dim]")

    console.print(Panel.fit("[bold blue]Team Ratings (1=Best, 7=Worst)[/bold blue]"))

    # Display table
    table = Table(show_header=True, header_style="bold")
    table.add_column("Team")
    table.add_column("Atk H", justify="center")
    table.add_column("Atk A", justify="center")
    table.add_column("Def H", justify="center")
    table.add_column("Def A", justify="center")
    table.add_column("Avg", justify="center")

    def rating_style(r: int) -> str:
        if r <= 2:
            return "green"
        elif r <= 4:
            return "yellow"
        else:
            return "red"

    for team in sorted(ratings_data.keys()):
        r = ratings_data[team]
        avg = r.avg_overall
        avg_style = "green" if avg <= 2.5 else "yellow" if avg <= 4.5 else "red"
        table.add_row(
            team,
            f"[{rating_style(r.atk_home)}]{r.atk_home}[/{rating_style(r.atk_home)}]",
            f"[{rating_style(r.atk_away)}]{r.atk_away}[/{rating_style(r.atk_away)}]",
            f"[{rating_style(r.def_home)}]{r.def_home}[/{rating_style(r.def_home)}]",
            f"[{rating_style(r.def_away)}]{r.def_away}[/{rating_style(r.def_away)}]",
            f"[{avg_style}]{avg:.1f}[/{avg_style}]",
        )

    console.print(table)


@ratings_group.command(name="update")
@click.option("--since-gw", type=int, default=None, help="Calculate from this GW onwards (recent form)")
@click.option("--dry-run", is_flag=True, help="Show calculated ratings without saving")
@click.option("--use-xg", is_flag=True, help="Use Understat xG data instead of actual goals (full season only)")
def ratings_update(since_gw: int | None, dry_run: bool, use_xg: bool):
    """Recalculate ratings from fixture results.

    By default, uses full season actual goals. Use --since-gw N for recent form,
    or --use-xg for expected goals (less noise, full season only).
    """
    from fpl_cli.api.fpl import FPLClient
    from fpl_cli.services.team_ratings import TeamRatingsCalculator, TeamRatingsService

    if use_xg and since_gw:
        console.print(
            "[yellow]Warning: --since-gw is ignored when --use-xg is set (xG path uses full season only)[/yellow]\n"
        )

    async def _update():
        from fpl_cli.services.team_ratings_prior import (
            BLENDING_CUTOFF_GW,
            REGRESSION_CONSTANT,
            blend_with_prior,
            generate_prior,
        )

        service = TeamRatingsService()
        # Captured before anything is written: save_ratings updates the
        # service in place, so the Change column needs the pre-save state.
        current_ratings = service.get_all_ratings()

        async with FPLClient() as client:
            calculator = TeamRatingsCalculator(client)

            based_on_gws: tuple[int, int] | None = None
            if use_xg:
                console.print("[bold]Calculating ratings from Understat xG data (full season)...[/bold]\n")
                ratings, performances = await calculator.calculate_from_xg()
                source = "understat_xg"
                method = "full_season_xg"
                summary = "Understat xG (full season)"
                # Understat covers the season to date, so the sample is however
                # many gameweeks have completed. Reads from the bootstrap-static
                # cache (already warmed by calculate_from_xg's get_teams() call)
                # rather than a fresh fixtures/ request.
                gameweeks = await client.get_gameweeks()
                sample_gws = max(
                    (gw["id"] for gw in gameweeks if gw.get("finished")), default=0
                )
            else:
                min_gw = since_gw or 1
                method = "recent_form" if since_gw else "full_season"
                console.print(f"[bold]Calculating ratings from GW{min_gw} fixtures...[/bold]\n")
                ratings, performances = await calculator.calculate_from_fixtures(min_gw=min_gw)
                source = "calculated"
                # Determine GW range for display
                fixtures = await client.get_fixtures()
                completed = [f for f in fixtures if f.finished and f.gameweek and f.gameweek >= min_gw]
                max_gw = max((f.gameweek for f in completed if f.gameweek), default=min_gw) if completed else min_gw
                summary = f"GW{min_gw}-{max_gw} ({len(completed)} fixtures)"
                based_on_gws = (min_gw, max_gw)
                # Gameweeks of current-season evidence, not the absolute GW
                # number: --since-gw 8 at GW10 is a three-gameweek sample.
                sample_gws = max_gw - min_gw + 1 if completed else 0

            if not ratings:
                # Nothing to rate teams on for this window. A file already on
                # disk (current_ratings, loaded before anything ran) is left
                # alone rather than replaced by a coarser prior estimate --
                # e.g. --since-gw 15 requested before GW15 has produced a full
                # home/away cycle for any club must not clobber GW1-14 data.
                if current_ratings:
                    console.print(
                        "[yellow]No completed fixtures to calculate from for this window - "
                        "keeping existing ratings unchanged.[/yellow]"
                    )
                    return
                # Nothing usable is on disk either. Saving nothing here leaves
                # a season-rollover file in place (ignored on its season
                # stamp, so every fixture scores a neutral 4.0) — and
                # `fpl doctor` sends users here precisely then. The
                # previous-season prior is available the whole time.
                if dry_run:
                    # Check for real rather than assuming a prior exists, so
                    # this can't promise an outcome the real run refuses.
                    prior = await generate_prior(client)
                    if prior:
                        console.print(
                            "[yellow]No completed fixtures to calculate from - ratings would "
                            "be estimated from last season's prior.[/yellow]"
                        )
                    else:
                        console.print(
                            "[yellow]No completed fixtures to calculate from, and no "
                            "previous-season data available either - ratings would be "
                            "unchanged.[/yellow]"
                        )
                    return
                console.print(
                    "[yellow]No completed fixtures to calculate from - estimating from "
                    "last season's prior.[/yellow]"
                )
                if not await service.seed_from_prior(client):
                    error_console.print(
                        "[red]No previous-season data available either - ratings unchanged[/red]"
                    )
                    return
                console.print(
                    f"\n[green]Estimated ratings saved to "
                    f"{rich_escape(str(service.config_path))}[/green]"
                )
                console.print("[dim]Run `fpl ratings` to view them.[/dim]")
                return

            # Same Bayesian shrinkage the auto-refresh applies. Without it this
            # command would overwrite a blended file with twenty clubs rated on
            # a handful of matches each, and stamp it fresh so the auto-refresh
            # would not correct it.
            blend_note = None
            if 0 < sample_gws < BLENDING_CUTOFF_GW:
                prior = await generate_prior(client)
                if prior:
                    ratings = blend_with_prior(prior, ratings, sample_gws)
                    current_weight = sample_gws / (sample_gws + REGRESSION_CONSTANT)
                    blend_note = (
                        f"Blended with last season's prior: {current_weight:.0%} current form / "
                        f"{1 - current_weight:.0%} prior (shrinkage ends at GW{BLENDING_CUTOFF_GW})"
                    )
                    source = f"{source}_blended"
                    method = f"{method}_blended"

        # Display results
        console.print(Panel.fit("[bold blue]Calculated Team Ratings[/bold blue]"))
        console.print(f"[dim]Based on {summary}[/dim]")
        if blend_note:
            console.print(f"[dim]{blend_note}[/dim]")
        console.print()

        table = Table(show_header=True, header_style="bold")
        table.add_column("Team")
        table.add_column("Atk H", justify="center")
        table.add_column("Atk A", justify="center")
        table.add_column("Def H", justify="center")
        table.add_column("Def A", justify="center")
        table.add_column("Change", justify="left")

        def rating_style(r: int) -> str:
            if r <= 2:
                return "green"
            elif r <= 4:
                return "yellow"
            else:
                return "red"

        def format_change(old: int, new: int) -> str:
            if old == new:
                return ""
            diff = old - new  # Positive = improvement (lower rating is better)
            if diff > 0:
                return f"[green]↑{diff}[/green]"
            else:
                return f"[red]↓{abs(diff)}[/red]"

        for team in sorted(ratings.keys()):
            r = ratings[team]
            old = current_ratings.get(team)

            changes = []
            if old:
                if r.atk_home != old.atk_home:
                    changes.append(f"AH:{format_change(old.atk_home, r.atk_home)}")
                if r.atk_away != old.atk_away:
                    changes.append(f"AA:{format_change(old.atk_away, r.atk_away)}")
                if r.def_home != old.def_home:
                    changes.append(f"DH:{format_change(old.def_home, r.def_home)}")
                if r.def_away != old.def_away:
                    changes.append(f"DA:{format_change(old.def_away, r.def_away)}")

            change_str = " ".join(changes) if changes else "[dim]-[/dim]"

            table.add_row(
                team,
                f"[{rating_style(r.atk_home)}]{r.atk_home}[/{rating_style(r.atk_home)}]",
                f"[{rating_style(r.atk_away)}]{r.atk_away}[/{rating_style(r.atk_away)}]",
                f"[{rating_style(r.def_home)}]{r.def_home}[/{rating_style(r.def_home)}]",
                f"[{rating_style(r.def_away)}]{r.def_away}[/{rating_style(r.def_away)}]",
                change_str,
            )

        console.print(table)

        # Show performance stats
        stat_label = "xG" if use_xg else "GS"
        conceded_label = "xGA" if use_xg else "GC"
        console.print("\n[bold]Underlying Stats (per game):[/bold]")
        stats_table = Table(show_header=True, header_style="bold")
        stats_table.add_column("Team")
        stats_table.add_column(f"{stat_label} Home", justify="right")
        stats_table.add_column(f"{stat_label} Away", justify="right")
        stats_table.add_column(f"{conceded_label} Home", justify="right")
        stats_table.add_column(f"{conceded_label} Away", justify="right")
        stats_table.add_column("Games", justify="center")

        for team in sorted(performances.keys()):
            p = performances[team]
            stats_table.add_row(
                team,
                f"{p.goals_scored_home:.2f}",
                f"{p.goals_scored_away:.2f}",
                f"{p.goals_conceded_home:.2f}",
                f"{p.goals_conceded_away:.2f}",
                f"{p.home_games}H/{p.away_games}A",
            )

        console.print(stats_table)

        if dry_run:
            console.print("\n[dim]Dry run - ratings not saved[/dim]")
        else:
            service.save_ratings(
                ratings,
                source=source,
                based_on_gws=based_on_gws,
                calculation_method=method,
            )
            console.print(f"\n[green]Ratings saved to {rich_escape(str(service.config_path))}[/green]")

    asyncio.run(_update())

