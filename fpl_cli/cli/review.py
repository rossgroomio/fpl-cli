"""Gameweek review command."""
# Pattern: via-agent

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

import click
from rich.markup import escape as rich_escape
from rich.panel import Panel

from fpl_cli.cli._context import (
    Format,
    console,
    error_console,
    fpl_config,
    get_format,
    get_settings,
    is_custom_analysis_enabled,
    resolve_output_dir,
    save_failure_message,
    warn_prediction_problems,
)
from fpl_cli.cli._helpers import rec_in_options, waiver_superseded_reason
from fpl_cli.cli._json import config_failure_boundary, emit_failure
from fpl_cli.cli._review_analysis import (
    _review_fixtures,
    _review_global_stats,
    _review_league_table,
    _review_next_gameweek,
)
from fpl_cli.cli._review_classic import (
    _empty_classic_team,
    _review_classic_league,
    _review_classic_team,
    _review_classic_transfers,
)
from fpl_cli.cli._review_draft import _empty_draft_result, _review_draft
from fpl_cli.cli._review_summarisation import _review_compare_recs, _review_llm_summarise
from fpl_cli.season import season_label
from fpl_cli.services.fixture_predictions import (
    FixturePredictionsService,
    find_blank_gameweeks,
    find_double_gameweeks,
    resolve_players_with_double,
    resolve_players_with_fixture,
)
from fpl_cli.utils.text import join_and
from fpl_cli.utils.time import format_deadline

if TYPE_CHECKING:
    from fpl_cli.api.fpl import FPLClient


class GameweekResolutionError(Exception):
    """No gameweek could be resolved to review or recap, and why.

    The resolver used to print its own prose and hand back `None`, which left
    each caller with a failure it could not describe: `league-recap` emitted a
    generic "Could not resolve a gameweek to recap." under `--format json`
    while the specific reason went to a different stream, and two of the
    branches printed that reason on stdout (issue #273). Carrying the reason
    out instead lets every call site report it through `emit_failure()` -- one
    stream, one exit code, and the reason the reader actually needs.
    """


async def _review_resolve_gw(client: FPLClient, gameweek: int | None) -> dict[str, Any]:
    """Resolve which gameweek to review. Returns {gw, gw_data, api_current_gw_id}.

    Raises `GameweekResolutionError` carrying the reason when no gameweek can
    be resolved -- an unknown id, one still being played, or a season with
    nothing finished to look back on.
    """
    gameweeks = await client.get_gameweeks()
    current_gw = await client.get_current_gameweek()
    api_current_gw_id = current_gw["id"] if current_gw else None

    if gameweek is not None:
        gw_data = next((g for g in gameweeks if g["id"] == gameweek), None)
        if not gw_data:
            raise GameweekResolutionError(f"Gameweek {gameweek} not found")
        if not gw_data.get("finished"):
            raise GameweekResolutionError(
                f"Gameweek {gameweek} is not yet finished"
                " - only completed gameweeks can be reviewed"
            )
        gw = gameweek
    else:
        if current_gw and current_gw.get("finished"):
            gw = current_gw["id"]
        elif current_gw:
            gw = current_gw["id"] - 1
            if gw < 1:
                raise GameweekResolutionError("No completed gameweeks yet")
        elif gameweeks and not any(g.get("finished") for g in gameweeks):
            # No current gameweek and nothing finished: the season has not
            # kicked off yet, which is not the same as a lookup failure.
            first_gw = min(gameweeks, key=lambda g: g["id"])
            deadline = first_gw.get("deadline_time")
            deadline_note = f" (GW1 deadline: {format_deadline(deadline)})" if deadline else ""
            raise GameweekResolutionError(
                f"Season hasn't started - no completed gameweeks to review{deadline_note}"
            )
        else:
            raise GameweekResolutionError("Could not determine current gameweek")

        gw_data = next((g for g in gameweeks if g["id"] == gw), None)
        if not gw_data:
            raise GameweekResolutionError(f"Gameweek {gw} not found")

        if not gw_data.get("finished"):
            raise GameweekResolutionError(
                f"Gameweek {gw} is not yet finished"
                " - use -g/--gameweek to specify a completed gameweek"
            )

    return {"gw": gw, "gw_data": gw_data, "api_current_gw_id": api_current_gw_id}


_SQUAD_MODE_LABELS = {"wildcard": "Wildcard", "freehit": "Free Hit"}


def _unread_recs_warning(recs_comparison: dict[str, Any], recs_path: str) -> str:
    """The stderr warning naming what nothing could be read from in a recommendations file.

    Part by part, because a captain read on its own does not make the
    transfers readable (#355). The likely cause is the gw-prep template moving
    on without the parser, which is how #354 went unnoticed for four weeks, so
    the warning says so.
    """
    where = rich_escape(recs_path)
    hint = "Its layout may no longer match the gw-prep template."
    if recs_comparison["nothing_read"]:
        return f"[yellow]No recommendations could be read from {where}, so none are compared. {hint}[/yellow]"
    unread = recs_comparison["unread"]
    if len(unread) == 1:
        return f"[yellow]Could not read the {unread[0]} from {where}, so it is not graded. {hint}[/yellow]"
    return f"[yellow]Could not read the {join_and(unread)} from {where}, so they are not graded. {hint}[/yellow]"


def _print_squad_comparison(sq: dict[str, Any]) -> None:
    """The `--compare-recs` lines for a week whose recommendation was a squad.

    Each player brought in is graded against the recommended 15 rather than
    paired with a recommended move, because a wildcard or free hit
    recommendation names none.
    """
    label = _SQUAD_MODE_LABELS.get(sq.get("mode") or "", "Full")
    fielded = sq.get("fielded")
    if fielded is not None:
        console.print(f"  Squad: {label} squad recommended: {fielded} of {sq['size']} fielded")
    else:
        console.print(f"  Squad: {label} squad recommended ({sq['size']} players)")
    for t in sq.get("incoming", []):
        move = f"{rich_escape(str(t['actual_in']))} ← {rich_escape(str(t['actual_out']))}"
        net = t.get("actual_net", 0)
        if t.get("advised") is None:
            console.print(f"  Transfer: [dim]?[/dim] {move} (not graded, net {net})")
        elif t.get("advised"):
            console.print(f"  Transfer: [green]✓[/green] {move} (in recommended squad, net {net})")
        else:
            console.print(f"  Transfer: [dim]⚠ Unadvised:[/dim] {move} (not in recommended squad, net {net})")
    if not sq.get("incoming"):
        console.print("  Transfers: [yellow]✗[/yellow] No transfers made")
    if sq.get("missed"):
        console.print(f"  Recommended but not fielded: {rich_escape(', '.join(sq['missed']))}")


def _print_captain_comparison(rc: dict[str, Any]) -> None:
    """The `--compare-recs` captain line.

    Both figures are captain points, the raw score times that week's
    multiplier, so the delta between them is like for like (#400). The change
    to the team total follows it when it can be known.
    """
    rec = rich_escape(str(rc["rec_captain"]))
    actual = rich_escape(str(rc["actual_captain"] or "unknown"))
    if rc.get("captain_ungraded"):
        console.print(f"  Captain: [dim]?[/dim] Rec {rec} → Actual {actual} (not graded)")
        return
    if rc["captain_followed"]:
        console.print(f"  Captain: [green]✓[/green] {rec} (followed)")
        return
    if rc.get("actual_captain_played") is False:
        armband = rc.get("actual_armband")
        handover = f"; vice {rich_escape(str(armband))} took the armband" if armband else ""
        actual += f" (didn't play{handover}, {rc['actual_captain_pts']} pts)"
    else:
        actual += f" ({rc['actual_captain_pts']} pts)"
    if not rc.get("rec_captain_in_squad"):
        console.print(f"  Captain: [yellow]✗[/yellow] Rec {rec} (not in squad) → Actual {actual}")
        return
    if rc.get("rec_captain_played") is False:
        console.print(f"  Captain: [yellow]✗[/yellow] Rec {rec} (didn't play) → Actual {actual}")
        return
    deltas = [f"{_signed(rc['captain_pts_delta'])} captain pts"]
    if "captain_total_delta" in rc:
        deltas.append(f"{_signed(rc['captain_total_delta'])} on your total")
    console.print(
        f"  Captain: [yellow]✗[/yellow] Rec {rec} ({rc['rec_captain_pts']} pts)"
        f" → Actual {actual} [{', '.join(deltas)}]"
    )


def _signed(delta: int) -> str:
    """A delta signed and coloured: green when the pick beat the advice."""
    if delta > 0:
        return f"[green]+{delta}[/green]"
    if delta < 0:
        return f"[red]{delta}[/red]"
    return "0"


@click.command("review")
@click.option("--gameweek", "-g", type=int, help="Specific gameweek to review (default: last completed)")
@click.option("--save", "-s", is_flag=True, help="Save report to output directory")
@click.option("--output", "-o", type=click.Path(),
              help="Custom output directory for report (the season subdirectory is still added)")
@click.option("--summarise", is_flag=True, help="Add LLM-generated summary (requires API keys)")
@click.option("--debug", is_flag=True, help="Save LLM prompts and responses to data/debug/")
@click.option("--dry-run", is_flag=True, help="Build and save prompts to data/debug/ without calling LLMs")
@click.option("--compare-recs", is_flag=True, help="Compare recommendations vs actual decisions")
@click.pass_context
@config_failure_boundary
def review_command(
    ctx: click.Context,
    gameweek: int | None, save: bool, output: str | None,
    summarise: bool, debug: bool, dry_run: bool, compare_recs: bool,
) -> None:
    """Review a completed gameweek - your squad's performance and league standings.

    \b
    Examples:
      fpl review
      fpl review --gameweek 30 --save
      fpl review --summarise --compare-recs
      fpl review --dry-run --debug
    """
    from fpl_cli.agents.orchestration.report import ReportAgent
    from fpl_cli.api.fpl import FPLClient

    fmt = get_format(ctx)
    show_classic = fmt != Format.DRAFT
    show_draft = fmt != Format.CLASSIC

    settings = get_settings(ctx)

    research_provider = None
    synthesis_provider = None
    research_unavailable: str | None = None
    synthesis_unavailable: str | None = None

    # Resolve LLM providers if summarise requested (--dry-run calls neither)
    if summarise and not dry_run:
        from fpl_cli.api.providers import (
            ProviderError,
            ProviderNotConfiguredError,
            get_llm_provider,
        )

        # One `try` per role, and an absent key is fatal to neither. The review
        # itself -- squad, transfers, standings, fixtures, results -- needs no
        # key at all, so losing the lot because an add-on cannot run is the
        # trade #144 already ruled on for `league-recap`'s editorial. Sharing
        # one `try` cost as much again: a missing key for either role took the
        # other half down with it, so holding one key bought nothing (#287).
        #
        # Only the absent key degrades, though. `get_llm_provider` also rejects
        # a provider name it does not know, a malformed model string and a
        # `base_url` that is neither https nor loopback -- settings mistakes,
        # not a key the user has yet to obtain, and a run that shrugged them off
        # with exit 0 would let a typo'd `llm.research.provider` sit unnoticed
        # in a cron job forever. Those keep the hard failure they had before
        # this degradation existed; the boundary on the callback only catches
        # `ConfigError`, so letting them propagate would mean a traceback.
        try:
            research_provider = get_llm_provider("research", settings)
        except ProviderNotConfiguredError as e:
            research_unavailable = str(e)
            error_console.print(
                f"[yellow]Community narrative skipped: {rich_escape(str(e))}[/yellow]"
            )
        except ProviderError as e:
            emit_failure("review", str(e), "table", cause=e)
        try:
            synthesis_provider = get_llm_provider("synthesis", settings)
        except ProviderNotConfiguredError as e:
            synthesis_unavailable = str(e)
            error_console.print(
                f"[yellow]Personal analysis skipped: {rich_escape(str(e))}[/yellow]"
            )
        except ProviderError as e:
            emit_failure("review", str(e), "table", cause=e)

    # With both halves out there is nothing for the summariser to do
    # (`league-recap` skips its own call the same way, #159 review).
    # `--dry-run` never resolves a provider, so it always runs.
    run_summarise = dry_run or research_provider is not None or synthesis_provider is not None

    fpl_cfg = fpl_config(settings)
    entry_id = fpl_cfg.get("classic_entry_id")
    classic_league_id = fpl_cfg.get("classic_league_id")
    draft_league_id = fpl_cfg.get("draft_league_id")
    draft_entry_id = fpl_cfg.get("draft_entry_id")

    async def _review() -> None:
        from contextlib import AsyncExitStack

        async with AsyncExitStack() as stack:
            client = await stack.enter_async_context(FPLClient())
            if research_provider is not None:
                await stack.enter_async_context(research_provider)
            if synthesis_provider is not None:
                await stack.enter_async_context(synthesis_provider)

            try:
                gw_result = await _review_resolve_gw(client, gameweek)
            except GameweekResolutionError as exc:
                # `review` has no `--format json`, so the format is always
                # table: the reason on stderr, exit 1. It used to print on
                # stdout and exit 0, which told a script the review had
                # succeeded and left `2>/dev/null` showing the failure
                # (issue #273). Declining a gameweek still being played is
                # unchanged -- only how the refusal is reported.
                emit_failure("review", str(exc), "table", cause=exc)
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
            # Both `find_*_gameweeks` answer from the club a player is at now,
            # which is a different club from the one whose fixtures he was on
            # once the gameweek under review predates a transfer (issue #174).
            # These read the same facts off the gameweek's own live data and
            # take precedence wherever they can answer; the team-id sets stay
            # the fallback for the gameweeks and players they cannot.
            players_with_fixture = resolve_players_with_fixture(live_data, raw_fixtures)
            players_with_double = resolve_players_with_double(live_data, raw_fixtures)

            # Started here rather than at its use below, and only when a prompt
            # is being built: nothing it needs arrives after `teams`, and it
            # carries real latency of its own -- a fixtures fetch, plus a
            # ratings refresh that can re-derive from a season of results. Left
            # in place it was pure addition to the run; here it overlaps the
            # sections that follow. Cancelled on unwind so an exception before
            # the await never leaves it pending against a closing client.
            # Gated on the synthesis half rather than on `run_summarise`: the
            # personal analysis is the only thing that reads this, so a run
            # holding a research key and no synthesis key would otherwise pay
            # the latency above and discard the answer unread (#287 review).
            next_gameweek_task = None
            if dry_run or synthesis_provider is not None:
                next_gameweek_task = asyncio.create_task(
                    _review_next_gameweek(
                        client, gw, teams,
                        custom_analysis=is_custom_analysis_enabled(settings),
                    )
                )
                stack.callback(next_gameweek_task.cancel)

            # Classic section
            if show_classic:
                console.print("\n[bold cyan]# Classic[/bold cyan]")
                classic_team = await _review_classic_team(
                    client, entry_id, gw, player_map, teams, gw_data, live_stats,
                    bgw_team_ids=bgw_team_ids, dgw_team_ids=dgw_team_ids,
                    players_with_fixture=players_with_fixture,
                    players_with_double=players_with_double,
                )
                classic_transfers_data = await _review_classic_transfers(
                    client, entry_id, gw, player_map, teams, live_stats
                )
                classic_league_data = await _review_classic_league(
                    client, classic_league_id, entry_id, gw, api_current_gw_id,
                    use_net_points=settings.get("use_net_points", False),
                )
            else:
                # Must match return shape of _review_classic_team / _review_classic_transfers / _review_classic_league
                classic_team = _empty_classic_team()
                classic_transfers_data = []
                classic_league_data = None

            # Global stats (BGW teams excluded from blankers)
            global_data = await _review_global_stats(
                client, gw, player_map, teams, live_stats,
                bgw_team_ids=bgw_team_ids,
                players_with_fixture=players_with_fixture,
            )
            # BGW/DGW team names for prompt formatting (derived at point of use)
            global_data["bgw_team_names"] = {teams[tid].short_name for tid in bgw_team_ids if tid in teams}
            global_data["dgw_team_names"] = {teams[tid].short_name for tid in dgw_team_ids if tid in teams}

            # Predicted future DGWs for prompt context
            pred_service = FixturePredictionsService()
            global_data["predicted_dgw_teams"] = pred_service.get_predicted_doubles(min_gw=gw + 1)
            warn_prediction_problems(pred_service)

            # Draft section
            if show_draft:
                draft_result = await _review_draft(
                    client, draft_league_id, draft_entry_id, gw, api_current_gw_id,
                    players, player_map, teams, live_stats,
                    bgw_team_ids=bgw_team_ids, dgw_team_ids=dgw_team_ids,
                    players_with_fixture=players_with_fixture,
                    players_with_double=players_with_double,
                )
            else:
                draft_result = _empty_draft_result()

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
                "draft_lost_claims": draft_result["draft_lost_claims_data"],
                "draft_league": draft_result["draft_league_data"],
                "fixtures": fixtures_data,
                "league_table": league_table_data,
                "fpl_format": str(fmt) if fmt else None,
            }

            # LLM summarisation if requested (or dry-run to preview prompts)
            if run_summarise:
                next_gameweek = await next_gameweek_task if next_gameweek_task else None
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
                    next_gameweek=next_gameweek,
                )
                collected_data["research_summary"] = llm["research_summary"]
                collected_data["synthesis_summary"] = llm["synthesis_summary"]
                # Empty on a clean run. When it isn't, the saved report says so
                # in its own body: the stderr warning is gone by the time
                # someone reads the file weeks later, and a verdict the model
                # dropped must not read as one deliberately omitted (#266).
                collected_data["synthesis_problems"] = llm["synthesis_problems"]

            # Set outside the branch above, because with both keys absent that
            # branch does not run at all and the report is exactly the one that
            # needs to explain itself. A file read weeks later has no stderr
            # left to read, and a section skipped for want of a key must not
            # look like one nobody asked for (#266's reasoning, same file).
            collected_data["summary_unavailable"] = [
                *([f"Community narrative: {research_unavailable}"] if research_unavailable else []),
                *([f"Personal analysis: {synthesis_unavailable}"] if synthesis_unavailable else []),
            ]

            # --compare-recs reads from the same season directory --save writes
            # to, so resolve it once for both rather than per branch: resolving
            # twice would also warn twice about a stale directory. Pure and
            # cheap, so it is not worth guarding on the flags.
            # Derived from GW1's deadline rather than the clock (#91), so a
            # gameweek reviewed after the season overruns the July cutover
            # still lands in that season's own directory.
            output_dir = resolve_output_dir(settings, output, season=season_label(await client.get_season_year()))

            # Compare recommendations vs actuals if requested
            if compare_recs:
                from fpl_cli.parsers.recommendations import parse_recommendations

                recs_path = output_dir / f"gw{gw}-recommendations.md"
                recs = parse_recommendations(recs_path)
                recs_comparison: dict[str, Any] | None = None

                if recs is None:
                    error_console.print(f"\n[yellow]No recommendations file found at {recs_path}[/yellow]")
                else:
                    recs_comparison = _review_compare_recs(recs, collected_data, gameweek=gw)
                    collected_data["recs_comparison"] = recs_comparison
                    if recs_comparison["unread"]:
                        error_console.print(f"\n{_unread_recs_warning(recs_comparison, str(recs_path))}")

                # A file nothing could be read from has no recommended side to
                # print, so the comparison is left out rather than shown empty.
                if recs_comparison is not None and not recs_comparison["nothing_read"]:
                    unread = recs_comparison["unread"]

                    # Print comparison summary to console
                    console.print("\n" + "-" * 50)
                    console.print("\n[bold cyan]# Recommendations vs Actuals[/bold cyan]")

                    rc = recs_comparison["classic"]
                    if rc.get("rec_captain"):
                        _print_captain_comparison(rc)
                    elif "captain" in unread:
                        console.print("  Captain: [dim]?[/dim] not graded (could not be read from the file)")

                    if rc.get("no_transfers_possible"):
                        console.print("  Transfers: [dim]n/a - no transfers exist in GW1[/dim]")
                    elif "transfers" in unread:
                        console.print("  Transfers: [dim]?[/dim] not graded (could not be read from the file)")
                    elif sq := rc.get("squad"):
                        _print_squad_comparison(sq)
                    elif rc.get("rec_roll") and rc.get("actual_roll"):
                        console.print("  Transfers: [green]✓[/green] Rolled (aligned)")
                    elif rc.get("rec_roll") and not rc.get("actual_roll"):
                        console.print(
                            "  Transfers: [yellow]✗[/yellow] Rec roll, but made transfers"
                        )
                    else:
                        for t in rc.get("transfers", []):
                            if t.get("ungraded"):
                                console.print(
                                    f"  Transfer: [dim]?[/dim] "
                                    f"{rec_in_options(t)} ← {t['rec_out']} (not graded)"
                                )
                            elif t.get("followed"):
                                net = t.get("actual_net", 0)
                                taken = f" with {t['followed_in']}" if t.get("followed_in") else ""
                                console.print(
                                    f"  Transfer: [green]✓[/green] "
                                    f"{rec_in_options(t)} ← {t['rec_out']}"
                                    f" (followed{taken}, net {net})"
                                )
                            elif t.get("not_made"):
                                console.print(
                                    f"  Transfer: [yellow]✗[/yellow] "
                                    f"{rec_in_options(t)} ← {t['rec_out']} (not made)"
                                )
                            else:
                                console.print(
                                    f"  Transfer: [yellow]~[/yellow] "
                                    f"Sold {t['rec_out']} but got "
                                    f"{t.get('actual_in')} instead of "
                                    f"rec {rec_in_options(t)}"
                                )

                    for t in rc.get("unadvised_transfers", []):
                        console.print(
                            f"  Transfer: [dim]⚠ Unadvised:[/dim] "
                            f"{t['actual_in']} ← {t['actual_out']}"
                            f" (net {t.get('actual_net', 0)})"
                        )

                    rd = recs_comparison["draft"]
                    if "waivers" in unread:
                        console.print("  Waivers: [dim]?[/dim] not graded (could not be read from the file)")
                    for w in rd.get("waivers", []):
                        p = w["priority"]
                        # A claim they made and lost is activity either way,
                        # so it qualifies whichever branch the move itself
                        # landed in rather than replacing it.
                        lost_note = " after claiming him and losing him" if w.get("claimed_and_lost") else ""
                        if w.get("ungraded"):
                            console.print(
                                f"  Waiver P{p}: [dim]?[/dim] "
                                f"{w['rec_in']} ← {w['rec_out']} (not graded)"
                            )
                        elif w.get("followed"):
                            console.print(
                                f"  Waiver P{p}: [green]✓[/green] "
                                f"{w['rec_in']} ← {w['rec_out']}"
                                f" (followed{lost_note}, net {w.get('actual_net', 0)})"
                            )
                        elif w.get("lost_claim"):
                            # Name the player they actually claimed, which is
                            # not always the one advised -- a claim matched on
                            # the drop alone is a different claim.
                            claimed_in = w.get("claimed_in") or w["rec_in"]
                            detail = (
                                f"claimed {claimed_in} instead, lost to a rival"
                                if w.get("different_claim")
                                else "claimed, lost to a rival"
                            )
                            console.print(
                                f"  Waiver P{p}: [yellow]✗[/yellow] "
                                f"{claimed_in} ← {w['rec_out']}"
                                f" ({detail})"
                            )
                        # Neither a hit nor a miss: another claim on the same
                        # drop answers for this one (#416).
                        elif w.get("superseded"):
                            console.print(
                                f"  Waiver P{p}: [dim]·[/dim] "
                                f"{w['rec_in']} ← {w['rec_out']}"
                                f" (not needed: {waiver_superseded_reason(w)})"
                            )
                        elif w.get("fallback_to"):
                            console.print(
                                f"  Waiver P{p}: [dim]·[/dim] "
                                f"{w['rec_in']} ← {w['rec_out']}"
                                f" (fallback to {w['fallback_to']}, not reached)"
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
                                f"rec {w['rec_in']}{lost_note}"
                            )

                    for w in rd.get("unadvised_waivers", []):
                        console.print(
                            f"  Waiver: [dim]⚠ Unadvised:[/dim] "
                            f"{w['actual_in']} ← {w['actual_out']}"
                            f" (net {w.get('actual_net', 0)})"
                        )

                    # A warning, so stderr: the move it belongs to went
                    # ungraded, and the reason is the recommendation's wording.
                    for a in recs_comparison.get("ambiguous_names", []):
                        club = f" ({a['team']})" if a.get("team") else ""
                        error_console.print(
                            f"[yellow]  Not graded: \"{rich_escape(a['name'])}\"{club} could be "
                            f"{rich_escape(' or '.join(a['candidates']))}[/yellow]"
                        )

            # Generate report if requested
            if save:
                error_console.print("\n[dim]Generating report...[/dim]")
                async with ReportAgent(config={"output_dir": output_dir}) as report_agent:
                    report_result = await report_agent.run(context={
                        "report_type": "review",
                        "gameweek": gw,
                        "data": collected_data,
                    })

                if report_result.success:
                    console.print(f"[green]✓[/green] Report saved to: {report_result.data['report_path']}")
                else:
                    emit_failure("review", save_failure_message(report_result), "table")

    asyncio.run(_review())
