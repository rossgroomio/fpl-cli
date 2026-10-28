"""League recap command - entertainment-first GW report for all league participants."""
# Pattern: via-agent
# pyright: reportTypedDictNotRequiredAccess=false

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

import click
from rich.markup import escape as rich_escape
from rich.panel import Panel

from fpl_cli.api.providers import ProviderError
from fpl_cli.cli._context import (
    Format,
    console,
    error_console,
    get_format,
    get_settings,
    resolve_output_dir,
    save_failure_message,
)
from fpl_cli.cli._draft_provenance import DRAFT_PROVENANCE_UNAVAILABLE
from fpl_cli.cli._json import (
    api_failure_boundary,
    config_failure_boundary,
    emit_failure,
    emit_json,
    emit_json_error,
    json_output_mode,
    output_format_option,
)
from fpl_cli.cli._league_recap_types import (
    LeagueRecapData,
    PriorSeasonsSummary,
    RecapSeasonRecord,
    RecapStandoutPlayers,
    format_month_honours_line,
    month_awards_withheld_line,
    month_table_withheld_reason,
    recap_month_tables,
    recap_reads_whole_league,
    standout_blanks_heading,
    standout_players_lines,
    summarise_prior_seasons,
)
from fpl_cli.cli._synthesis_retry import synthesise_with_retry
from fpl_cli.constants import DETAIL_CAP
from fpl_cli.services.league_history import GameweekCoverage
from fpl_cli.services.league_history_fines import (
    ManagerFineTally,
    SeasonFinesTally,
    format_fine_breakdown,
    serialize_manager_fine_tally,
)
from fpl_cli.services.league_history_notes import (
    NotesPack,
    NotesPackEntry,
    NoteSurface,
    is_season_milestone,
)

# KTD8: the console stays a highlights view -- only the top few streaks by
# excess-over-minimum, so a rare streak is not buried under common ones (R12).
_CONSOLE_STREAK_LIMIT = 5

# A collection warning rather than a capture one -- it describes which source
# the gameweek's figures came from, not the ledger write -- so it lives beside
# the command rather than with the `league_history_*` codes in
# `_league_recap_history.py`, the same way `synthesis_provider_unavailable`
# does. Stable for scripts, like every other code on that channel.
RECAP_WARNING_STANDINGS_MOVED_ON = "league_standings_moved_on"
# Also a collection warning: FPL has not finished processing the recapped
# gameweek, so the league table it read may not have caught up (issue #424).
RECAP_WARNING_LEAGUE_TABLES_UPDATING = "league_tables_updating"
# A draft recap asked how each player came to his manager and could not say:
# the league's draft, waiver and trade history was unreadable or did not agree
# with FPL's own record of who owns whom (#458). The Standout Players lines
# read without it.
RECAP_WARNING_DRAFT_PROVENANCE_UNAVAILABLE = DRAFT_PROVENANCE_UNAVAILABLE
# A draft recap could say who drafted each player but not in which round,
# because the draft's picks are held by neither the API nor a snapshot and were
# rebuilt from the squads (#458). A notice: the lines are labelled all the same.
RECAP_WARNING_DRAFT_ROUNDS_UNKNOWN = "draft_rounds_unknown"
# FPL's division of the season into months could not be read off the
# bootstrap, so no Manager or Dud of the Month and no honours list (#425).
RECAP_WARNING_MONTHS_UNAVAILABLE = "league_months_unavailable"
# Classic only: FPL's own table for the month just closed disagrees with the
# month totals the ledger gives (#425), so the month's awards are withheld.
RECAP_WARNING_MONTH_TABLE_DISAGREES = "league_month_table_disagrees"

if TYPE_CHECKING:
    from collections.abc import Sequence

    from fpl_cli.api.fpl import FPLClient
    from fpl_cli.api.premier_league import MonthlyAward
    from fpl_cli.api.providers import LLMResponse, TokenUsage
    from fpl_cli.cli._league_recap_history import FetchDraftChoices
    from fpl_cli.cli._league_recap_vouchers import FetchMonthlyAwards
    from fpl_cli.cli._synthesis_retry import SynthesisAttempt
    from fpl_cli.models.season_month import SeasonMonth
    from fpl_cli.services.league_history_months import MonthAwards, MonthlyHonours, MonthTotal

logger = logging.getLogger(__name__)


@click.command("league-recap")
@click.option("--gameweek", "-g", type=int, help="Specific gameweek to recap (default: last completed)")
@click.option("--draft", "is_draft", is_flag=True, default=False,
              help="Use draft league (only needed when both formats are configured)")
@click.option("--save", "-s", is_flag=True, help="Save report to output directory")
@click.option("--output", "-o", type=click.Path(),
              help="Custom output directory for report (the season subdirectory is still added)")
@click.option("--summarise", is_flag=True, help="Add LLM-generated editorial narrative (requires API keys)")
@click.option("--backfill-detail", "backfill_detail", is_flag=True, default=False,
              help="Rebuild earlier gameweeks in full detail (captains, squads, transfers, "
                   "waiver claims won and lost) - one extra request per manager per gameweek")
@click.option("--debug", is_flag=True, help="Save LLM prompts and responses to data/debug/")
@click.option("--dry-run", is_flag=True, help="Build and save prompts to data/debug/ without calling LLMs")
@output_format_option
@click.pass_context
@config_failure_boundary
def league_recap_command(
    ctx: click.Context,
    gameweek: int | None, is_draft: bool, save: bool, output: str | None,
    summarise: bool, backfill_detail: bool, debug: bool, dry_run: bool,
    output_format: str,
) -> None:
    """Recap a completed gameweek for the whole league - awards, standings, and banter."""
    from fpl_cli.agents.orchestration.report import ReportAgent
    from fpl_cli.api.fpl import FPLClient, finished_gameweek_ids
    from fpl_cli.cli._fines_config import PLAYER_OF_THE_MONTH_VOUCHER, parse_fines_config
    from fpl_cli.cli._league_recap_data import (
        RecapReconciliationError,
        collect_classic_recap_data,
        collect_draft_recap_data,
        configured_fine_rule_types,
        evaluate_league_fines,
        league_first_gameweek,
        month_award_entries,
    )
    from fpl_cli.cli._league_recap_history import (
        capture_draft_picks,
        capture_recap_history,
        partial_league_reason,
    )
    from fpl_cli.cli._league_recap_vouchers import (
        capture_player_of_the_month,
        player_of_the_month_line,
        player_of_the_month_unchecked_line,
        recap_vouchers,
        serialize_player_of_the_month,
        voucher_penalties,
    )
    from fpl_cli.cli.review import GameweekResolutionError, _review_resolve_gw

    settings = get_settings(ctx)
    fmt = get_format(ctx)

    # Auto-select in single-format mode; respect --draft flag in BOTH mode
    if fmt == Format.DRAFT:
        is_draft = True
    elif fmt == Format.CLASSIC:
        is_draft = False

    synthesis_provider = None
    synthesis_unavailable: str | None = None

    if summarise or dry_run:
        if not dry_run:
            from fpl_cli.api.providers import get_llm_provider

            try:
                synthesis_provider = get_llm_provider("synthesis", settings)
            except ProviderError as e:
                # The editorial is an add-on. The recap, the saved report and
                # above all the append-only ledger capture are not, and once
                # the season moves on a missed draft capture cannot be
                # reconstructed. Aborting the lot over an absent API key --
                # and doing it with exit 0 and the error on stdout -- cost
                # more than the narrative was worth (#144), so it degrades.
                synthesis_unavailable = str(e)
                error_console.print(f"[yellow]Editorial skipped: {rich_escape(str(e))}[/yellow]")

    async def _run() -> None:
        from contextlib import AsyncExitStack, nullcontext

        async with AsyncExitStack() as stack:
            # The output format is a separate concept from the FPL classic/
            # draft format resolved via `get_format(ctx)` above -- both
            # coexist, as they already do in `status`. Entered first so
            # every console/error_console print for the rest of the run
            # (Rich resolves `sys.stdout` dynamically, not at Console
            # construction time) lands on stderr, keeping stdout JSON-only.
            stdout = stack.enter_context(json_output_mode()) if output_format == "json" else None

            client = await stack.enter_async_context(FPLClient())
            if synthesis_provider is not None:
                await stack.enter_async_context(synthesis_provider)
            # Resolve gameweek
            try:
                gw_result = await _review_resolve_gw(client, gameweek)
            except GameweekResolutionError as exc:
                # The resolver's own reason, not a generic stand-in: under
                # `--format json` the envelope used to say "Could not resolve
                # a gameweek to recap." while "Gameweek 7 is not yet finished"
                # went to stderr, and in table mode the same refusal exited 0
                # (issue #273). `emit_failure` settles both -- stderr prose or
                # the envelope on stdout, exit 1 either way.
                emit_failure("league-recap", str(exc), output_format, cause=exc)
            gw: int = gw_result["gw"]
            # Taken from the resolver rather than re-scanned here: it already
            # found the current gameweek, off the same cached bootstrap
            # payload, to decide which gameweek to recap. One source keeps
            # that decision and the liveness test below from disagreeing.
            api_current_gw_id: int | None = gw_result["api_current_gw_id"]

            console.print(Panel.fit(f"[bold blue]Gameweek {gw} League Recap[/bold blue]"))

            # Fetch shared bootstrap data
            players = await client.get_players()
            player_map: dict[int, Any] = {p.id: p for p in players}
            teams = {t.id: t for t in await client.get_teams()}
            live_data = await client.get_gameweek_live(gw)
            live_stats = {e["id"]: e["stats"] for e in live_data.get("elements", [])}

            # Detect BGW/DGW
            raw_fixtures = await client.get_fixtures(gw)
            is_bgw = len(raw_fixtures) < 10
            is_dgw = len(raw_fixtures) > 10

            # Which clubs had no fixture, so a recorded squad can tell a
            # player who blanked apart from one who never kicked a ball. Same
            # threading shape `review` uses for its per-format helpers.
            from fpl_cli.services.fixture_predictions import find_blank_gameweeks
            from fpl_cli.services.player_clubs import (
                GameweekClubResolver,
                derived_player_codes,
            )

            teams_list = list(teams.values())
            blank_gws = find_blank_gameweeks({gw: raw_fixtures}, teams_list, gw, gw)
            bgw_team_ids = frozenset(t["team_id"] for t in blank_gws.get(gw, []))
            # Both questions the gameweek can answer about itself -- which
            # players' clubs had a fixture (issue #169) and which club each
            # was at (issue #177) -- off one pass over the live payload, since
            # they read the same `explain` entries. None whenever the gameweek
            # cannot answer, and today's clubs carry it as before. One
            # resolver for the whole run, so the handful of moved players it
            # has to look up are fetched once between them rather than once
            # per gameweek a backfill replays.
            club_resolver = GameweekClubResolver(client)
            clubs_now = {p.id: p.team_id for p in players}
            gw_clubs = await club_resolver.resolve(live_data, raw_fixtures, clubs_now)
            with_fixture = gw_clubs.with_fixture if gw_clubs else None
            gameweek_clubs = gw_clubs.clubs if gw_clubs else None

            # Get next GW deadline
            from datetime import datetime, timedelta

            from fpl_cli.season import TOTAL_GAMEWEEKS, season_label
            from fpl_cli.utils.time import format_deadline

            gameweeks = await client.get_gameweeks()
            # From GW1's deadline, not the clock (#91): a ledger row written
            # for a season that overruns the July cutover (2019-20, delayed
            # into July 2020 by COVID) must still land in that season's own
            # partition, not the one the calendar has since rolled into.
            season_year = await client.get_season_year()
            season = season_label(season_year)
            next_gw_data = next((g for g in gameweeks if g["id"] == gw + 1), None)
            next_deadline = None
            waiver_deadline = None
            if next_gw_data and next_gw_data.get("deadline_time"):
                raw = next_gw_data["deadline_time"]
                try:
                    dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
                    next_deadline = format_deadline(dt)
                    # Waiver deadline is 24h before GW deadline
                    waiver_deadline = format_deadline(dt - timedelta(hours=24))
                except (ValueError, AttributeError):
                    next_deadline = raw

            # gw is "live" only while the league standings still describe it.
            # Being the most recently finished gameweek is not enough: the
            # standings' `event_total` and `total` track whatever gameweek the
            # API calls current, so the next deadline moves them on to that one
            # while `max(finished_gws)` still points here -- and every use of
            # `is_live_gw` (the reconciliation, the standings fallbacks, the
            # numbers written to an unreachable manager's ledger row) then
            # reads one gameweek's standings as another's, which is how a
            # recap of the just-finished gameweek came to fail on a
            # reconciliation between two different gameweeks (issue #262).
            # Both halves have to hold, which is the same question `review`
            # asks before it shows a league table for a gameweek
            # (`_review_classic_league`'s `is_historical_review`).
            finished_gws = finished_gameweek_ids(gameweeks)
            is_max_finished = bool(finished_gws) and gw == max(finished_gws)
            is_live_gw = is_max_finished and gw == api_current_gw_id
            # Said out loud only where the user asked for the latest gameweek
            # and the season moved past it -- an explicitly older `-g` is a
            # replay by intent and needs no explaining. Without this the
            # degradation is silent, and for draft it is visible (cumulative
            # totals and positions fall back to the ledger, or go unavailable).
            standings_moved_on = (
                is_max_finished and api_current_gw_id is not None and gw != api_current_gw_id
            )
            if standings_moved_on:
                error_console.print(
                    f"[yellow]Gameweek {api_current_gw_id} has started, so the league table now"
                    f" describes it rather than GW{gw}. Recapping GW{gw} from each manager's"
                    " own gameweek history instead -- run the recap before the next deadline"
                    " for the fullest capture.[/yellow]"
                )

            # FPL marks a gameweek finished before it has refreshed the league
            # tables, and the refresh can trail by hours (issue #424). Inside
            # that window both collectors read a table that has not caught up:
            # classic's totals and draft's ranks. Warned rather than refused,
            # since the refresh can sit on "Updating" for hours and a draft
            # gameweek missed while live cannot be recaptured; a later run
            # supersedes this one in the ledger. Each game has its own feed,
            # because the two refresh their tables separately. Only a live
            # capture reads the table at all, and by the time a gameweek is
            # replayed the feed has moved on, so a replay does not ask.
            from fpl_cli.cli._gameweek_status import (
                fetch_draft_gameweek_status,
                fetch_gameweek_status,
            )

            update_status = None
            if is_live_gw:
                update_status = (
                    await fetch_draft_gameweek_status() if is_draft
                    else await fetch_gameweek_status(client)
                )
            still_processing = update_status.still_processing(gw) if update_status else None
            if still_processing:
                error_console.print(
                    f"[yellow]{_still_processing_message(gw, still_processing)}[/yellow]"
                )

            # Whether anything will read each manager's FPL record before
            # this season (issue #131): the saved report and the editorial
            # are its only surfaces, so a bare run or a JSON run does not
            # pay the request per manager the collector would otherwise
            # make at the league's opener, and nor does an editorial already
            # known to be skipped. The collector applies the opener gate
            # itself; the replay below never asks, since a replayed gameweek
            # only goes to the ledger, which never records it.
            wants_prior_seasons = bool(save or output) or (
                (summarise or dry_run) and synthesis_unavailable is None
            )

            # Collect format-specific data
            try:
                if is_draft:
                    collected_data = await collect_draft_recap_data(
                        settings=settings, gw=gw, live_stats=live_stats,
                        players=players, teams=teams, is_live_gw=is_live_gw,
                        bgw_team_ids=bgw_team_ids, players_with_fixture=with_fixture,
                        gameweek_clubs=gameweek_clubs,
                        # For the gameweek being recapped only: the Standout
                        # Players section it feeds is never built for a
                        # backfill's replays, so they skip the reads (#458).
                        provenance_season=season,
                    )
                else:
                    collected_data = await collect_classic_recap_data(
                        client=client, settings=settings, gw=gw,
                        live_stats=live_stats, player_map=player_map, teams=teams,
                        is_live_gw=is_live_gw, bgw_team_ids=bgw_team_ids,
                        players_with_fixture=with_fixture,
                        gameweek_clubs=gameweek_clubs,
                        with_prior_seasons=wants_prior_seasons,
                    )
            except RecapReconciliationError as e:
                # A stop condition, not a soft skip: exit non-zero so a
                # scripted caller (the gw-prep skill) sees the failure
                # rather than an empty but successful run. Under --format
                # json that has to be the envelope -- `click.ClickException`
                # writes its own prose to stderr and leaves stdout empty,
                # which is the same silence in a different shape (#159 review).
                message = str(e)
                if still_processing:
                    # The check's own diagnosis stands; timing is offered
                    # beside it, not instead of it, since the refresh can sit
                    # on "Updating" for hours and a real mapping regression
                    # landing in that window must not be explained away
                    # (issue #424). Said here as well as on stderr because
                    # the JSON error envelope carries no warnings.
                    message = (
                        f"{message} FPL has not finished processing GW{gw}, though, so this"
                        " may be timing instead: the league table may not have caught up yet."
                        " Re-run once FPL finishes; a failure after that points at the mapping."
                    )
                if output_format == "json":
                    emit_json_error("league-recap", message, file=stdout, cause=e)
                raise click.ClickException(message) from e

            # Add context metadata
            if still_processing:
                # The saved report is the copy pasted into the group chat, so
                # it carries the caveat too, above the editorial it covers.
                collected_data["league_tables_updating"] = still_processing
            collected_data["is_bgw"] = is_bgw
            collected_data["is_dgw"] = is_dgw
            if provenance_problem := collected_data.get("draft_provenance_problem"):
                error_console.print(
                    f"[yellow]{_provenance_unavailable_message(provenance_problem)}[/yellow]"
                )
            if rounds_notice := collected_data.get("draft_provenance_notice"):
                error_console.print(f"[yellow]{rich_escape(rounds_notice)}[/yellow]")
            # Standout players (#455), built here for the recapped gameweek
            # rather than in the collectors, so a backfill's replays -- which
            # only reach the ledger -- never pay for a section nobody reads.
            standouts = _standout_players_or_none(collected_data, gw=gw, is_live_gw=is_live_gw)
            if standouts is not None:
                collected_data["standout_players"] = standouts
            # Which players the gameweek placed exactly, so the identity
            # carry keeps this run's derived club instead of a stale one
            # already on disk (issue #177).
            collected_data["clubs_derived_codes"] = derived_player_codes(
                players, gameweek_clubs,
            )
            collected_data["season_length"] = TOTAL_GAMEWEEKS  # type: ignore[typeddict-unknown-key]
            if next_deadline:
                collected_data["next_deadline"] = next_deadline  # type: ignore[typeddict-unknown-key]

            if is_draft and waiver_deadline:
                collected_data["waiver_deadline"] = waiver_deadline  # type: ignore[typeddict-unknown-key]

            # Parsed here for the ledger's coarse tier and the Player of the
            # Month voucher; the ruling and the rule types below, like each
            # replay, parse the block themselves. A malformed block has
            # already been refused by `config_failure_boundary` on the way in.
            fines_config = parse_fines_config(settings)

            # Evaluate fines per manager (graceful skip when unconfigured)
            ruling = evaluate_league_fines(
                collected_data["managers"], settings, collected_data["fpl_format"],
            )
            if ruling.fines:
                collected_data["fines"] = ruling.fines
                # For the editorial alone, so it can vary how it names the
                # forfeit (#503); replays below reach only the ledger.
                if ruling.penalties:
                    collected_data["fine_penalties"] = ruling.penalties
            # Recorded whether or not anything triggered: a gameweek with no
            # fines and a gameweek nobody ruled on are different facts, and
            # only this tells the season tally which one it is read
            # (issue #136). Paired with the managers the evaluation actually
            # completed for, so a manager it raised on records silence rather
            # than the acquittal this list alone would imply.
            collected_data["fine_rules_evaluated"] = configured_fine_rule_types(
                settings, collected_data["fpl_format"],
            )
            collected_data["fines_ruled_manager_keys"] = sorted(ruling.ruled_manager_keys)

            async def _replay_gameweek(target_gw: int) -> LeagueRecapData | None:
                """Re-collect one finished gameweek for the detailed backfill.

                Goes through the same collectors the live path uses, so a
                replayed gameweek and a live one produce identical rows.
                """
                target_live = await client.get_gameweek_live(target_gw)
                target_stats = {e["id"]: e["stats"] for e in target_live.get("elements", [])}
                target_fixtures = await client.get_fixtures(target_gw)
                target_blanks = find_blank_gameweeks(
                    {target_gw: target_fixtures}, teams_list, target_gw, target_gw,
                )
                target_bgw_ids = frozenset(
                    t["team_id"] for t in target_blanks.get(target_gw, [])
                )
                target = await club_resolver.resolve(
                    target_live, target_fixtures, clubs_now,
                )
                target_with_fixture = target.with_fixture if target else None
                target_clubs = target.clubs if target else None
                if is_draft:
                    replayed = await collect_draft_recap_data(
                        settings=settings, gw=target_gw, live_stats=target_stats,
                        players=players, teams=teams, is_live_gw=False,
                        bgw_team_ids=target_bgw_ids,
                        players_with_fixture=target_with_fixture,
                        gameweek_clubs=target_clubs,
                    )
                else:
                    replayed = await collect_classic_recap_data(
                        client=client, settings=settings, gw=target_gw,
                        live_stats=target_stats, player_map=player_map, teams=teams,
                        is_live_gw=False, bgw_team_ids=target_bgw_ids,
                        players_with_fixture=target_with_fixture,
                        gameweek_clubs=target_clubs,
                    )
                replayed["is_bgw"] = len(target_fixtures) < 10
                replayed["is_dgw"] = len(target_fixtures) > 10
                replayed["clubs_derived_codes"] = derived_player_codes(
                    players, target_clubs,
                )
                # Ruled here, not only on the live path: without this a
                # replayed gameweek lands with `fines=[]`, which reads as "a
                # week nobody was fined" rather than "a week nobody ruled" --
                # so missing a week and then repairing it un-fined that week
                # permanently, with the repair making the gap look closed
                # (issue #136).
                replayed_ruling = evaluate_league_fines(
                    replayed["managers"], settings, replayed["fpl_format"],
                )
                if replayed_ruling.fines:
                    replayed["fines"] = replayed_ruling.fines
                replayed["fine_rules_evaluated"] = configured_fine_rule_types(
                    settings, replayed["fpl_format"],
                )
                replayed["fines_ruled_manager_keys"] = sorted(
                    replayed_ruling.ruled_manager_keys,
                )
                return replayed

            # The season's months (#425), off the bootstrap already in hand.
            # Draft gameweeks are this game's, so a draft league's months are
            # too. Unreadable months cost the monthly awards, never the recap.
            months, months_problem = await _season_months(client)
            if months_problem:
                error_console.print(f"[yellow]{_months_unavailable_message(months_problem)}[/yellow]")

            # Record the gameweek, then fill what the API still allows.
            # Deliberately before synthesis: rendering happens after the LLM
            # call, so capturing at render time would be too late for anything
            # the prompt reads. Never raises -- a store problem warns on stderr
            # and the recap carries on (R4). In JSON mode the same warnings
            # reach the payload as codes (below), so the human-readable prose
            # is suppressed here rather than printed twice -- but the
            # first-capture notice has no such JSON-side counterpart of its
            # own, so it's carried forward via `capture_result.first_capture_
            # store_path` (computed inside `capture_recap_history`, from the
            # same check that gates the notice) rather than silently lost
            # along with the suppressed prose. R13's previous-position
            # correction also happens inside the call below now, before the
            # rows it returns are built -- not as a separate step here -- so
            # both the persisted ledger row and the `--format json` payload
            # see the corrected value too, not just `collected_data`.
            with error_console.capture() if output_format == "json" else nullcontext():
                capture_result = await capture_recap_history(
                    collected_data,
                    season=season,
                    is_live_gw=is_live_gw,
                    # Classic's coarse tier: one call per manager for the
                    # whole season. Draft has no per-manager history endpoint.
                    history_client=None if is_draft else client,
                    finished_gameweeks=finished_gws,
                    replay_gameweek=_replay_gameweek,
                    backfill_detail=backfill_detail,
                    # The coarse tier rules what it structurally can --
                    # last place and below-threshold, both derivable from
                    # cohort points; a red card needs a squad the
                    # manager-history endpoint never returns (issue #136).
                    fines_config=fines_config,
                    use_net_points=bool(settings.get("use_net_points", False)),
                    months=months,
                )
                # Each held draft's picks, kept beside the ledger while the
                # API still serves them (#461). A no-op off draft.
                draft_picks = await capture_draft_picks(
                    collected_data, season=season,
                    fetch_choices=_draft_choices_source(collected_data.get("draft_choices")),
                )
            notes_pack = capture_result.notes_pack

            # Report-surfaced history text, stashed as plain strings so the
            # Jinja template needs no knowledge of NotesPack/NoteSurface.
            # Absent entirely (rather than empty) when capture couldn't build
            # a pack, so the template's `is defined` guards skip the section.
            if notes_pack is not None:
                if NoteSurface.REPORT in notes_pack.season_phase_entry.surfaces:
                    collected_data["league_history_phase_text"] = notes_pack.season_phase_entry.text
                collected_data["league_history_streak_lines"] = [
                    entry.text for entry in notes_pack.entries if NoteSurface.REPORT in entry.surfaces
                ]
                # The report's Season Counts section follows each
                # condition's own CountSurfacePolicy in the registry
                # (issue #164): on an ordinary week only the counts whose
                # increment fired their condition -- a round-number total,
                # an unbroken drought at a run milestone, a second-half
                # first -- plus that condition's qualifying ride-alongs
                # carry the report surface, while at the two milestone
                # gameweeks the whole nonzero set does, the same set-piece
                # rhythm as the printed fines table. The whole set reaches
                # `--format json` regardless.
                collected_data["league_history_season_count_lines"] = [
                    entry.text
                    for entry in notes_pack.season_count_entries
                    if NoteSurface.REPORT in entry.surfaces
                ]
                collected_data["league_history_coverage_lines"] = [
                    entry.text for entry in notes_pack.coverage_entries
                ]
                # The season records the editorial is shown (issue #494),
                # kept for the tie check, which reads a record scored more
                # than once as the tie its line states.
                for entry in notes_pack.season_record_entries:
                    if entry.points is None:
                        continue
                    record = RecapSeasonRecord(
                        points=entry.points,
                        scores=[(holder.manager_name, holder.gameweek) for holder in entry.holders],
                    )
                    if entry.condition_key == "highest_gameweek_score":
                        collected_data["season_highest_score"] = record
                    else:
                        collected_data["season_lowest_score"] = record

            # Prior seasons (issue #131): folded once and read by the report
            # and the editorial alike, so neither surface can describe a
            # manager's past differently from the other. None outside the
            # league's opening gameweek, when nothing was fetched -- the
            # report then omits the section rather than heading an empty one.
            prior_seasons = summarise_prior_seasons(
                collected_data["managers"],
                # The API names a season "2025/26" where the label helper
                # says "2025-26"; the season just finished is the one a
                # manager's most recent season is checked against before
                # the report calls it "last season".
                previous_season_name=season_label(season_year - 1).replace("-", "/"),
            )
            if prior_seasons is not None:
                collected_data["prior_seasons_lines"] = prior_seasons.lines
                collected_data["prior_seasons_coverage_lines"] = prior_seasons.coverage_lines

            # Manager and Dud of the Month (#425): the month this gameweek
            # closed gets its two awards, or the reason it has none.
            month_honours = capture_result.month_honours
            closing_month = month_honours.ending_at(gw) if month_honours is not None else None

            # FPL's own table for the month, against the ledger's totals,
            # before either award is placed anywhere: that table is what a
            # league checks the awards against, so a disagreement withholds
            # them on every surface rather than leaving the report and the
            # editorial to crown what FPL contradicts. Only classic has month
            # tables, and only a settled one is worth reading: while FPL is
            # still refreshing the league tables it may not have caught up
            # with the month's last gameweek, and the report's banner already
            # says the recap's totals may be stale.
            month_table_problem: str | None = None
            league_id = collected_data.get("league_id")
            if (
                month_honours is not None and closing_month is not None and closing_month.awarded
                and league_id is not None and not is_draft and not still_processing
            ):
                mismatches = await _month_table_mismatches(client, league_id, closing_month)
                if mismatches:
                    month_table_problem = _month_table_disagrees_message(closing_month, mismatches)
                    error_console.print(f"[yellow]{rich_escape(month_table_problem)}[/yellow]")
                    month_honours = month_honours.withholding(
                        closing_month.month, month_table_withheld_reason(len(mismatches)),
                    )
                    closing_month = month_honours.ending_at(gw)

            if closing_month is not None:
                if closing_month.awarded:
                    collected_data["awards"].update(month_award_entries(closing_month))
                else:
                    collected_data["month_awards_withheld"] = month_awards_withheld_line(closing_month)
            # The honours list is printed on the season fines table's rhythm:
            # at the halfway boundary and the finale, where half a season's
            # months or all of them are worth reading, and repetition in
            # between. The editorial gets it on those two gameweeks too, so it
            # can speak to the list the report prints, and on every month's
            # last gameweek, so the month's two awards reach it beside the
            # months before them. `--format json` carries it every week.
            honours_milestone = is_season_milestone(gw)
            if honours_milestone and month_honours is not None and month_honours.months:
                collected_data["monthly_honours_lines"] = [
                    format_month_honours_line(awards) for awards in month_honours.months
                ]
            editorial_honours = (
                month_honours if closing_month is not None or honours_milestone else None
            )
            if editorial_honours is not None:
                # The months the editorial is shown, and only those, are
                # what the tie check reads a month tie against.
                collected_data["month_tables"] = recap_month_tables(editorial_honours)

            # The Player of the Month voucher (#492), a draft house rule the
            # league opts into under `fines.draft_vouchers`. After the
            # capture, so the ledger it reads ownership from holds this
            # gameweek, and on the capture's own store; and only the latest
            # finished gameweek's recap announces a month, so a replay never
            # claims one. Its prose is suppressed in JSON mode, where its
            # warnings ride the payload.
            potm_voucher = (
                fines_config.draft_voucher(PLAYER_OF_THE_MONTH_VOUCHER)
                if is_draft and fines_config is not None else None
            )
            potm = None
            if (
                potm_voucher is not None and fines_config is not None and months
                and capture_result.store is not None
            ):
                with error_console.capture() if output_format == "json" else nullcontext():
                    potm = await capture_player_of_the_month(
                        capture_result.store,
                        months, gw,
                        voucher=potm_voucher,
                        fetch_awards=_monthly_awards_source(season),
                        may_announce=is_max_finished,
                        league_start_gameweek=league_first_gameweek(collected_data.get("league_start_event")),
                        bootstrap_names={p.code: p.web_name for p in players if p.code},
                        partial_league=partial_league_reason(collected_data),
                    )
                if announced := potm.fold.announced_at(gw):
                    collected_data["player_of_the_month_lines"] = [
                        player_of_the_month_line(month) for month in announced
                    ]
                if vouchers := recap_vouchers(potm, gw, potm_voucher):
                    collected_data["player_of_the_month_vouchers"] = vouchers
                    # The voucher's forfeit is named by the editorial's
                    # once-only rule like a fine's (#503).
                    if penalties := voucher_penalties(
                        vouchers, fines_config, collected_data.get("fine_penalties", []),
                    ):
                        collected_data["fine_penalties"] = penalties
                if (unchecked := player_of_the_month_unchecked_line(potm)) is not None:
                    collected_data["player_of_the_month_unchecked"] = unchecked

            # The *printed* season table is a set-piece, not a weekly
            # fixture: a full standings-style table answers "who owes what
            # this season", which is worth reading at the halfway boundary
            # and at the finale and is noise in between. Every other week the
            # console and the saved report stay a this-week view, and `fpl
            # league-fines` answers the season question on demand.
            #
            # The prompt is deliberately *not* gated with them. A table and a
            # sentence are different things: the editorial can drop "Bob's
            # fourth last-place of the season" into a paragraph without
            # turning the recap into a ledger dump, and it can only do that
            # for totals it was actually handed -- the system prompt forbids
            # it inferring history it was not given. So the model sees the
            # section every week and decides whether the fact earns its
            # place; only the printed table waits for a milestone.
            printed_fines_tally = (
                capture_result.fines_tally if is_season_milestone(gw) else None
            )

            # Stashed as plain strings for the same reason the history text
            # above is: the template needs no knowledge of the tally's shape.
            # Absent entirely for a non-milestone gameweek, and for a league
            # with no fine rules configured and none ever ruled, so the
            # template's `is defined` guards skip the section rather than
            # heading an empty table.
            if printed_fines_tally is not None and printed_fines_tally.is_reportable:
                collected_data["season_fines_span"] = (
                    f"GW{printed_fines_tally.start_gameweek}-"
                    f"GW{printed_fines_tally.through_gameweek}"
                )
                collected_data["season_fines_lines"] = [
                    _season_fine_line(manager) for manager in printed_fines_tally.managers
                ]
                collected_data["season_fines_coverage_lines"] = list(
                    printed_fines_tally.qualifiers,
                )

            # LLM summarisation (opt-in via --summarise or --dry-run)
            if (summarise or dry_run) and synthesis_unavailable is None:
                # Skipped outright when the provider is already known unusable:
                # the call would format the whole awards/standings/history
                # context and build the prompt, only to reach neither of its
                # two branches (#159 review).
                try:
                    await _recap_llm_summarise(
                        collected_data, gw,
                        synthesis_provider=synthesis_provider,
                        dry_run=dry_run, debug=debug,
                        is_bgw=is_bgw, is_dgw=is_dgw,
                        season_length=TOTAL_GAMEWEEKS,
                        notes_pack=notes_pack,
                        # Ungated: see above -- the model gets season totals
                        # every week and chooses whether to use them.
                        fines_tally=capture_result.fines_tally,
                        prior_seasons=prior_seasons,
                        # On a month's last gameweek and at the two season
                        # milestones (#425): see above.
                        month_honours=editorial_honours,
                    )
                except ProviderError as e:
                    error_console.print(f"[yellow]LLM summarisation failed: {e}[/yellow]")
                except Exception:  # noqa: BLE001 — graceful degradation
                    logger.debug("LLM summarisation failed", exc_info=True)
                    error_console.print("[yellow]LLM summarisation failed (unexpected error)[/yellow]")

            # Display key highlights to console
            _render_console_highlights(collected_data, notes_pack, printed_fines_tally)

            # Generate report if saving
            if save or output:
                output_dir = resolve_output_dir(settings, output, season=season)
                async with ReportAgent(config={"output_dir": output_dir}) as agent:
                    result = await agent.run(context={
                        "report_type": "league-recap",
                        "gameweek": gw,
                        "data": dict(collected_data),
                    })
                if not result.success:
                    message = save_failure_message(result)
                    # The report was the editorial's only destination, and the
                    # provider call that wrote it is paid and not repeatable,
                    # so it goes to stderr rather than out with the failure.
                    # A dry run's placeholder is not worth printing.
                    editorial = collected_data.get("synthesis_summary")
                    if editorial and not dry_run:
                        error_console.print(
                            "[yellow]The editorial could not be saved with the report;"
                            " it follows:[/yellow]\n"
                        )
                        error_console.print(rich_escape(editorial), highlight=False)
                    if output_format == "json":
                        # The envelope below is all a JSON consumer gets, and
                        # the capture's own prose is suppressed in this mode,
                        # so a ledger problem that shares the save's cause
                        # would otherwise vanish. Naming the codes tells the
                        # consumer to look (#474).
                        codes = list(dict.fromkeys(
                            w["code"] for w in capture_result.warnings + draft_picks.warnings
                            + (potm.warnings if potm is not None else [])
                        ))
                        if codes:
                            message += f" (the ledger capture also reported: {', '.join(codes)})"
                    emit_failure("league-recap", message, output_format)
                console.print(f"\n[green]Report saved to {result.data['report_path']}[/green]")

            if output_format == "json":
                from fpl_cli.prompts.league_recap_checks import editorial_warning_message

                # From the in-memory rows this run built, not a re-read of
                # the store: available whether or not the write succeeded,
                # so a capture failure still produces manager data (KTD1's
                # "one schema, three surfaces" -- the stored row shape is
                # the payload shape, unchanged).
                manager_payloads = [row.model_dump(mode="json") for row in capture_result.rows]
                emit_json(
                    "league-recap",
                    manager_payloads,
                    metadata={
                        "fpl_format": collected_data["fpl_format"],
                        "gameweek": gw,
                        "coverage": _serialize_coverage(capture_result.coverage),
                        "season_phase": notes_pack.phase if notes_pack is not None else None,
                        "notes_pack": _serialize_notes_pack(notes_pack) if notes_pack is not None else None,
                        # Ungated, like the prompt's copy and unlike the
                        # printed table: a consumer asking for JSON every
                        # week should not have the season tally appear and
                        # disappear on a calendar it cannot see (KTD8).
                        "season_fines": (
                            _serialize_fines_tally(capture_result.fines_tally)
                            if capture_result.fines_tally is not None else None
                        ),
                        # Every completed month's Manager and Dud of the Month
                        # (#425), every week like `season_fines`; null when the
                        # months could not be read or the ledger could not be.
                        "monthly_honours": (
                            _serialize_monthly_honours(month_honours)
                            if month_honours is not None else None
                        ),
                        # Every completed month's Player of the Month and who
                        # held him (#492), every week like `monthly_honours`;
                        # null off draft, where no voucher is configured, and
                        # when the months or the ledger could not be read.
                        "player_of_the_month": (
                            serialize_player_of_the_month(potm, gw) if potm is not None else None
                        ),
                        # The week's top scorers and costliest blanks, every
                        # player row with the main game's `id` and the shared
                        # `code` beside it, and each line's sentence as `text`
                        # (#455). Not in `data`, whose rows are the ledger's.
                        "standout_players": collected_data.get("standout_players"),
                        "synthesis_summary": collected_data.get("synthesis_summary"),
                        # Drafts judged (1, or 2 after a retry), which of them
                        # shipped -- a retry only ships when it ranks better --
                        # and the tokens every judged draft spent; null
                        # whenever no editorial came back: not asked for, a
                        # dry run, or a failed call (#450).
                        "synthesis_attempts": collected_data.get("synthesis_attempts"),
                        "synthesis_shipped_attempt": collected_data.get("synthesis_shipped_attempt"),
                        "synthesis_usage": collected_data.get("synthesis_usage"),
                        # The skipped editorial rides the same channel as the
                        # capture's own warnings: `synthesis_summary` being
                        # null does not say whether it was asked for.
                        "warnings": capture_result.warnings + draft_picks.warnings + (
                            potm.warnings if potm is not None else []
                        ) + (
                            [{
                                "code": "synthesis_provider_unavailable",
                                "message": (
                                    "The editorial was requested but skipped:"
                                    f" {synthesis_unavailable}. Everything else in"
                                    " this recap, the ledger capture included, ran"
                                    " normally."
                                ),
                            }] if synthesis_unavailable else []
                        ) + (
                            # A truncated editorial is indistinguishable from a
                            # complete one in the payload itself, so the stop
                            # reason rides the warnings channel rather than
                            # leaving a consumer to guess (#266).
                            [{
                                "code": "synthesis_stopped_early",
                                "message": (
                                    "The provider refused the editorial: it"
                                    " stopped with stop_reason"
                                    f" {collected_data['synthesis_stop_reason']!r}"
                                    + (
                                        f" (category {collected_data['synthesis_stop_category']!r})"
                                        if collected_data.get("synthesis_stop_category") else ""
                                    )
                                    + ", so the text may be incomplete."
                                ) if collected_data.get("synthesis_refused") else (
                                    "The editorial may be cut off: the provider"
                                    " stopped with stop_reason"
                                    f" {collected_data['synthesis_stop_reason']!r}"
                                    " rather than finishing normally."
                                ),
                            }] if collected_data.get("synthesis_stop_reason") else []
                        ) + [
                            # The editorial is saved as written, so a claim its
                            # data contradicts is named here rather than left
                            # for a consumer to spot against the rows -- one
                            # warning per check, so a script can tell a
                            # misattributed race from a wrong net or tie
                            # (#357, #359).
                            {"code": code, "message": editorial_warning_message(code, problems)}
                            for code, problems in collected_data.get("synthesis_problems", {}).items()
                        ] + (
                            [{
                                "code": RECAP_WARNING_STANDINGS_MOVED_ON,
                                "message": (
                                    f"Gameweek {api_current_gw_id} has started, so the league"
                                    f" table describes it rather than GW{gw}. GW{gw} was"
                                    " recapped from each manager's own gameweek history;"
                                    " figures that only the live table can supply are"
                                    " derived or left unavailable."
                                ),
                            }] if standings_moved_on else []
                        ) + (
                            [{
                                "code": RECAP_WARNING_LEAGUE_TABLES_UPDATING,
                                "message": _still_processing_message(gw, still_processing),
                            }] if still_processing else []
                        ) + (
                            [{
                                "code": RECAP_WARNING_DRAFT_PROVENANCE_UNAVAILABLE,
                                "message": _provenance_unavailable_message(provenance_problem),
                            }] if provenance_problem else []
                        ) + (
                            [{
                                "code": RECAP_WARNING_DRAFT_ROUNDS_UNKNOWN,
                                "message": rounds_notice,
                            }] if rounds_notice else []
                        ) + (
                            [{
                                "code": RECAP_WARNING_MONTHS_UNAVAILABLE,
                                "message": _months_unavailable_message(months_problem),
                            }] if months_problem else []
                        ) + (
                            [{
                                "code": RECAP_WARNING_MONTH_TABLE_DISAGREES,
                                "message": month_table_problem,
                            }] if month_table_problem else []
                        ),
                        # Only set on this partition's very first capture --
                        # the one moment a container-local data directory is
                        # still cheap to notice (table mode prints this to
                        # stderr instead; JSON mode's warning suppression
                        # would otherwise drop it with no replacement).
                        "first_capture_store_path": (
                            str(capture_result.first_capture_store_path)
                            if capture_result.first_capture_store_path else None
                        ),
                        # The draft-picks snapshots this run wrote (#461), so
                        # a run that commits the ledger sees a new file; the
                        # stderr notice naming them is suppressed here.
                        "draft_picks_saved": [str(path) for path in draft_picks.saved],
                    },
                    file=stdout,
                )

    with api_failure_boundary("league-recap", output_format):
        asyncio.run(_run())


def _standout_players_or_none(
    data: LeagueRecapData, *, gw: int, is_live_gw: bool,
) -> RecapStandoutPlayers | None:
    """The Standout Players section for the recapped gameweek, or None.

    Best-effort, like the editorial checks: the section is an addition that
    nothing else reads, so a row shape it did not expect costs the section,
    never the console recap, the report or the ledger capture. The managers
    are the whole league only when the standings were not cut short and
    every member's picks came back -- otherwise a line never says "all" or
    "everyone but" of them.
    """
    from fpl_cli.cli._league_recap_data import find_standout_players

    managers = data["managers"]
    cohort = data.get("standings_cohort")
    whole_league = recap_reads_whole_league(data)
    try:
        return find_standout_players(
            managers, fpl_format=data["fpl_format"], gameweek=gw,
            live=is_live_gw, whole_league=whole_league,
            league_names=[c["manager_name"] for c in cohort] if cohort else None,
        )
    except (KeyError, TypeError, ValueError) as e:
        logger.debug("Standout players could not be built", exc_info=True)
        error_console.print(
            f"[yellow]Standout players skipped: a squad row could not be read ({type(e).__name__})[/yellow]"
        )
        return None


async def _fetch_draft_choices(league_id: int) -> dict[str, Any]:
    """The draft API's picks for a league, from a client opened for them alone."""
    from fpl_cli.api.fpl_draft import FPLDraftClient

    async with FPLDraftClient() as draft_client:
        return await draft_client.get_draft_choices(league_id)


def _draft_choices_source(held: dict[str, Any] | None) -> FetchDraftChoices:
    """Where the picks snapshot gets the choices response: the one the recap's
    provenance replay already read, so both see the same payload and the
    request is made once; a fresh read when the replay had none."""
    async def fetch(league_id: int) -> dict[str, Any]:
        return held if held is not None else await _fetch_draft_choices(league_id)

    return fetch


def _monthly_awards_source(season: str) -> FetchMonthlyAwards:
    """Where the Player of the Month voucher reads the Premier League's
    awards (#492): a client opened for them alone, and only when a month
    needs it."""
    async def fetch() -> list[MonthlyAward]:
        from fpl_cli.api.premier_league import PremierLeagueClient

        async with PremierLeagueClient() as awards_client:
            return await awards_client.get_monthly_awards(season)

    return fetch


def _provenance_unavailable_message(reason: str) -> str:
    """The stderr line and the JSON warning's message, worded once for both."""
    return (
        "Standout Players cannot say how each player came to his manager, so its lines read"
        f" without it: {reason}."
    )


def _still_processing_message(gw: int, reason: str) -> str:
    """The stderr line and the JSON warning's message, worded once for both."""
    return (
        f"FPL has not finished processing GW{gw}: {reason}, so league positions and"
        " totals may be stale. Re-run the recap once FPL finishes, before the next"
        " deadline, to record the settled table."
    )


async def _season_months(client: FPLClient) -> tuple[list[SeasonMonth], str | None]:
    """The season's months (#425), or none and why they could not be read.

    The bootstrap is already cached by the time this runs, so the only way
    to fail is a payload that has lost or reshaped its months.
    """
    from pydantic import ValidationError

    try:
        months = await client.get_months()
    except ValidationError as exc:
        return [], f"a month in FPL's bootstrap could not be read ({exc.error_count()} problem(s))"
    if not months:
        return [], "FPL's bootstrap lists no months"
    return months, None


def _months_unavailable_message(reason: str) -> str:
    """The stderr line and the JSON warning's message, worded once for both."""
    return (
        f"Manager and Dud of the Month skipped: {reason}, so the season's months are unknown."
        " Everything else in this recap, the ledger capture included, ran normally."
    )


async def _month_table_mismatches(
    client: FPLClient, league_id: int, awards: MonthAwards,
) -> list[tuple[str, int, int]]:
    """Each manager FPL's own table for the month scores differently from the
    ledger (#425), as (name, the ledger's total, FPL's), or none.

    A table that cannot be read is a debug line and no disagreement: the
    check is a guard on the awards, not a source of them. Only the table's
    first page is read, which is the whole league, since a league past one
    page of standings has its months withheld before this is reached.
    """
    import httpx

    from fpl_cli.cli._league_recap_data import month_table_mismatches

    try:
        response = await client.get_classic_league_standings(league_id, month_id=awards.month.id)
    except (httpx.HTTPError, ValueError) as exc:
        logger.debug("FPL's %s table could not be read for the cross-check: %s", awards.month.name, exc)
        return []
    standings = response.get("standings") if isinstance(response, dict) else None
    rows = standings.get("results") if isinstance(standings, dict) else None
    return month_table_mismatches(awards, rows if isinstance(rows, list) else [])


def _month_table_disagrees_message(awards: MonthAwards, mismatches: Sequence[tuple[str, int, int]]) -> str:
    """The stderr line and the JSON warning's message, worded once for both:
    the first `DETAIL_CAP` disagreements with both totals, the rest counted,
    so a change to how FPL scores a month does not read out the league."""
    shown = [f"{name}: {ours} here, {theirs} on FPL" for name, ours, theirs in mismatches[:DETAIL_CAP]]
    if omitted := len(mismatches) - len(shown):
        shown.append(f"{omitted} more")
    count = len(mismatches)
    return (
        f"FPL's own {awards.month.name} table disagrees with the ledger's totals for {count}"
        f" manager{'s' if count != 1 else ''} ({'; '.join(shown)}), so this recap withholds the"
        " month's Manager and Dud of the Month rather than contradict it."
    )


def _serialize_coverage(coverage: list[GameweekCoverage]) -> list[dict[str, Any]]:
    """Per-gameweek tier and status counts, JSON-shaped (R9, R16).

    Enum members pass straight through rather than being `.value`-mapped
    here: they're already `str` subclasses, and `emit_json`'s `_json_default`
    (`fpl_cli/cli/_json.py`) already coerces any `Enum` -- as a value or a
    dict key -- to its `.value` during `json.dumps`.
    """
    return [
        {
            "gameweek": c.gameweek,
            "readable": c.readable,
            "tier_counts": dict(c.tier_counts),
            "unknown_count": c.unknown_count,
            "unknown_manager_keys": c.unknown_manager_keys,
            "claims_unrecorded_count": c.claims_unrecorded_count,
        }
        for c in coverage
    ]


def _serialize_notes_pack_entry(entry: NotesPackEntry) -> dict[str, Any]:
    return {
        "kind": entry.kind,
        "text": entry.text,
        "surfaces": sorted(entry.surfaces),
        "tier": entry.tier,
        "window": (
            {"start_gameweek": entry.window.start_gameweek, "end_gameweek": entry.window.end_gameweek}
            if entry.window is not None else None
        ),
        "manager_key": entry.manager_key,
        "manager_name": entry.manager_name,
        "condition_key": entry.condition_key,
        "length": entry.length,
        "held_count": entry.held_count,
        "inapplicable_count": entry.inapplicable_count,
        "excess": entry.excess,
        "occurrences": entry.occurrences,
        "points": entry.points,
        "holders": [
            {"manager_key": h.manager_key, "manager_name": h.manager_name, "gameweek": h.gameweek}
            for h in entry.holders
        ],
        "previous_points": entry.previous_points,
        "previous_holders": [
            {"manager_key": h.manager_key, "manager_name": h.manager_name, "gameweek": h.gameweek}
            for h in entry.previous_holders
        ],
    }


def _serialize_notes_pack(pack: NotesPack) -> dict[str, Any]:
    """The whole pack, JSON-shaped (KTD8: `--format json` emits every entry
    regardless of which rendering surfaces it declares)."""
    return {
        "season": pack.season,
        "fpl_format": pack.fpl_format,
        "league_id": pack.league_id,
        "gameweek": pack.gameweek,
        "phase": pack.phase,
        "league_start_gameweek": pack.league_start_gameweek,
        "season_phase_entry": _serialize_notes_pack_entry(pack.season_phase_entry),
        "entries": [_serialize_notes_pack_entry(entry) for entry in pack.entries],
        # Every nonzero season count, not just the ones that grew this
        # gameweek and earned surfaces (issue #164) -- same KTD8 rule the
        # below-minimum streaks follow.
        "season_count_entries": [
            _serialize_notes_pack_entry(entry) for entry in pack.season_count_entries
        ],
        "season_record_entries": [
            _serialize_notes_pack_entry(entry) for entry in pack.season_record_entries
        ],
        # Every manager's own highest and lowest, not just the ones this
        # gameweek set and the editorial was shown (issue #496).
        "personal_record_entries": [
            _serialize_notes_pack_entry(entry) for entry in pack.personal_record_entries
        ],
        "coverage_entries": [_serialize_notes_pack_entry(entry) for entry in pack.coverage_entries],
        "entry_count": pack.entry_count,
    }


def _season_fine_line(manager: ManagerFineTally) -> str:
    """One manager's season fine record as a single sentence-fragment line.

    Everyone the ledger holds gets a line, fined or not: a fines table that
    lists only the fined reads as though everyone else were checked and
    cleared, which is exactly the claim the coverage lines beside it exist to
    qualify.
    """
    if not manager.total:
        return f"{manager.manager_name}: none"
    return f"{manager.manager_name}: {manager.total} ({format_fine_breakdown(manager)})"


def _serialize_fines_tally(tally: SeasonFinesTally) -> dict[str, Any]:
    """The whole tally, JSON-shaped -- emitted every week, whether or not it
    is reportable and whether or not the gameweek is a milestone, on the same
    principle KTD8 sets for the notes pack: `--format json` is a machine
    surface and carries everything the fold computed, leaving the
    is-it-worth-showing judgement to the human-facing surfaces."""
    return {
        "season": tally.season,
        "fpl_format": tally.fpl_format,
        "league_id": tally.league_id,
        "through_gameweek": tally.through_gameweek,
        "start_gameweek": tally.start_gameweek,
        "rule_types": tally.rule_types,
        "total_fines": tally.total_fines,
        "qualifiers": tally.qualifiers,
        # Same row shape `league-fines --format json` emits, from the same
        # helper: one dataclass, one serialization.
        "managers": [
            serialize_manager_fine_tally(manager, tally.rule_types)
            for manager in tally.managers
        ],
    }


def _serialize_monthly_honours(honours: MonthlyHonours) -> dict[str, Any]:
    """Every completed month's awards, JSON-shaped, every month's full table
    included (#425). A withheld month carries its reason and empty lists, and
    `text` is the line the report and the editorial print."""
    def _total(total: MonthTotal, *, ranked: bool = False) -> dict[str, Any]:
        row = {
            "manager_key": total.manager_key,
            "manager_name": total.manager_name,
            "points": total.points,
        }
        if ranked:
            row["rank"] = total.rank
        return row

    return {
        "season": honours.season,
        "fpl_format": honours.fpl_format,
        "league_id": honours.league_id,
        "through_gameweek": honours.through_gameweek,
        "months": [
            {
                "id": awards.month.id,
                "name": awards.month.name,
                "first_gameweek": awards.month.first_gameweek,
                "last_gameweek": awards.month.last_gameweek,
                "awarded": awards.awarded,
                "withheld_reason": awards.withheld_reason,
                "manager_of_the_month": [_total(total) for total in awards.managers_of_the_month],
                "dud_of_the_month": [_total(total) for total in awards.duds_of_the_month],
                "totals": [_total(total, ranked=True) for total in awards.totals],
                "text": format_month_honours_line(awards),
            }
            for awards in honours.months
        ],
    }


def _render_console_highlights(
    data: LeagueRecapData,
    notes_pack: NotesPack | None = None,
    fines_tally: SeasonFinesTally | None = None,
) -> None:
    """Print key recap highlights to console."""
    awards = data.get("awards", {})
    managers = data.get("managers", [])
    fmt = data.get("fpl_format", "classic")

    console.print(f"\n[bold]{data.get('league_name', 'League')}[/bold] - GW{data.get('gameweek')}")
    console.print(f"[dim]{len(managers)} managers[/dim]\n")

    if awards.get("gw_winner"):
        console.print(f"[green]GW Winner:[/green] {awards['gw_winner']['detail']}")
    if awards.get("gw_loser"):
        console.print(f"[red]GW Loser:[/red] {awards['gw_loser']['detail']}")
    if awards.get("biggest_bench_haul"):
        console.print(f"[yellow]Biggest Bench:[/yellow] {awards['biggest_bench_haul']['detail']}")
    if fmt == "classic" and awards.get("best_captain"):
        console.print(f"[green]Best Captain:[/green] {awards['best_captain']['detail']}")
    if fmt == "classic" and awards.get("worst_captain"):
        console.print(f"[red]Worst Captain:[/red] {awards['worst_captain']['detail']}")

    if fmt == "classic" and awards.get("transfer_genius"):
        console.print(f"[green]Transfer Genius:[/green] {awards['transfer_genius']['detail']}")
    if fmt == "classic" and awards.get("transfer_disaster"):
        console.print(f"[red]Transfer Disaster:[/red] {awards['transfer_disaster']['detail']}")
    if fmt == "draft" and awards.get("waiver_genius"):
        console.print(f"[green]Waiver Genius:[/green] {awards['waiver_genius']['detail']}")
    if fmt == "draft" and awards.get("waiver_disaster"):
        console.print(f"[red]Waiver Disaster:[/red] {awards['waiver_disaster']['detail']}")
    if fmt == "draft" and awards.get("most_contested"):
        console.print(f"[yellow]Most Contested:[/yellow] {awards['most_contested']['detail']}")
    # The month's two awards (#425), on the gameweek that closes it. Escaped,
    # as they carry manager names Rich could read as markup.
    if awards.get("manager_of_the_month"):
        console.print(
            f"[green]Manager of the Month:[/green] {rich_escape(awards['manager_of_the_month']['detail'])}"
        )
    if awards.get("dud_of_the_month"):
        console.print(f"[red]Dud of the Month:[/red] {rich_escape(awards['dud_of_the_month']['detail'])}")
    if withheld := data.get("month_awards_withheld"):
        console.print(f"[dim]Manager and Dud of the Month: {rich_escape(withheld)}[/dim]")
    # Draft's Player of the Month (#492), on the recap that announces him.
    # Escaped, as the lines carry manager names.
    for line in data.get("player_of_the_month_lines", []):
        console.print(f"[green]Player of the Month:[/green] {rich_escape(line)}")
    if unchecked := data.get("player_of_the_month_unchecked"):
        console.print(f"[dim]Player of the Month: {rich_escape(unchecked)}[/dim]")

    # Standout players (#455), the same sentences the editorial gets; the
    # saved report leaves them out (#482). Escaped: they carry manager names, which are
    # user-supplied and can hold square brackets Rich would read as markup.
    standouts = data.get("standout_players")
    scorer_lines, blank_lines = standout_players_lines(standouts) if standouts else ([], [])
    if standouts and (scorer_lines or blank_lines):
        console.print("\n[bold]Standout Players:[/bold]")
        if scorer_lines:
            console.print("  [dim]Top scorers[/dim]")
            for line in scorer_lines:
                console.print(f"  [green]{rich_escape(line)}[/green]")
        if blank_lines:
            console.print(f"  [dim]{standout_blanks_heading(standouts)}[/dim]")
            for line in blank_lines:
                console.print(f"  [red]{rich_escape(line)}[/red]")

    # Fines, grouped by the fine where several managers took the same one
    # (#504), then any Player of the Month voucher (#492): the same lines the
    # saved report prints. Escaped: they carry manager names, which Rich
    # would otherwise read as markup.
    fines = data.get("fines", [])
    vouchers = data.get("player_of_the_month_vouchers", [])
    if fines or vouchers:
        from fpl_cli.cli._league_recap_data import recap_fine_lines
        from fpl_cli.cli._league_recap_vouchers import recap_voucher_lines

        console.print("\n[bold]Fines:[/bold]")
        for line in recap_fine_lines(fines, data.get("fine_rules_evaluated", [])):
            lead, detail = rich_escape(line.lead), rich_escape(line.detail)
            if line.grouped:
                console.print(f"  [red]{lead}[/red] - {detail}")
            else:
                console.print(f"  [red]{lead}:[/red] {detail}")
        for line in recap_voucher_lines(vouchers):
            console.print(f"  [green]{rich_escape(line.lead)}:[/green] {rich_escape(line.detail)}")

    # Season totals, at the milestone gameweeks the caller gates on -- a
    # `None` tally here means either "not a milestone week" or "no fine
    # rules configured", and this renderer deliberately does not
    # re-litigate which. Only the fined are listed, since the console is a
    # highlights view and the full table is `fpl league-fines`, but the
    # coverage lines still print: a total nobody can trust is worse than no
    # total. The editorial gets the tally every week regardless; only this
    # printed block waits for a milestone.
    if fines_tally is not None and fines_tally.is_reportable:
        fined = fines_tally.fined_managers
        console.print(
            f"\n[bold]Season Fines[/bold] [dim](GW{fines_tally.start_gameweek}-"
            f"GW{fines_tally.through_gameweek})[/dim]",
        )
        if fined:
            for manager in fined:
                # Escaped: both these lines carry manager names, which are
                # user-supplied and full of square brackets often enough that
                # Rich would read one as markup.
                console.print(f"  [red]{rich_escape(_season_fine_line(manager))}[/red]")
        else:
            console.print("  [dim]Nobody has been fined this season.[/dim]")
        for line in fines_tally.qualifiers:
            console.print(f"  [dim]{rich_escape(line)}[/dim]")

    # Every completed month's awards (#425), at the same two milestones as the
    # season fines above: the caller sets the lines on those gameweeks alone.
    # Escaped, as they carry manager names.
    honours = data.get("monthly_honours_lines")
    if honours:
        console.print("\n[bold]Monthly Honours[/bold]")
        for line in honours:
            console.print(f"  {rich_escape(line)}")

    # Standings movement. A manager missing either position has no movement
    # to report -- see _assign_point_in_time_positions, which leaves both
    # unset rather than deriving a position it cannot stand behind.
    movers = [
        m for m in managers
        if m.get("previous_rank") is not None
        and m.get("overall_rank") is not None
        and m["previous_rank"] != m["overall_rank"]
    ]
    if movers:
        console.print("\n[bold]Standings Movement:[/bold]")
        for m in sorted(movers, key=lambda x: x["previous_rank"] - x["overall_rank"]):
            prev = m["previous_rank"]
            curr = m["overall_rank"]
            diff = prev - curr
            arrow = "[green]↑[/green]" if diff > 0 else "[red]↓[/red]"
            console.print(f"  {arrow} {m['manager_name']}: {prev} → {curr}")

    # R10: a manager whose position or total could not be derived (e.g. a
    # replayed draft gameweek the ledger holds no whole table for) is named as
    # unavailable rather than silently dropped from the standings -- the
    # same constants `_format_standings_block` uses for the report surface,
    # so the two can't drift onto different wording.
    from fpl_cli.agents.orchestration.report import POSITION_UNAVAILABLE, TOTAL_UNAVAILABLE

    unavailable: list[str] = []
    for m in managers:
        missing = []
        if m.get("overall_rank") is None:
            missing.append(POSITION_UNAVAILABLE)
        if m.get("total_points") is None:
            missing.append(TOTAL_UNAVAILABLE)
        if missing:
            unavailable.append(f"  {m['manager_name']}: {', '.join(missing)}")
    if unavailable:
        console.print("\n[bold]Unavailable:[/bold]")
        for line in unavailable:
            console.print(f"[dim]{line}[/dim]")

    # Streaks (R12, KTD8): only the leaders, so the console stays a
    # highlights view -- the report carries the full report-surfaced set.
    if notes_pack is not None:
        console_entries = [e for e in notes_pack.entries if NoteSurface.CONSOLE in e.surfaces]
        if console_entries:
            console.print("\n[bold]Streaks:[/bold]")
            for entry in console_entries[:_CONSOLE_STREAK_LIMIT]:
                console.print(f"  {entry.text}")


async def _editorial_with_data_checks(
    provider: Any,
    *,
    prompt: str,
    system_prompt: str,
    collected_data: LeagueRecapData,
) -> tuple[SynthesisAttempt[dict[str, list[str]]], int, TokenUsage]:
    """Query the editorial, retrying once if it contradicts its own data or
    the provider cut it off at its token ceiling.

    Returns the attempt that ships, the number of answers judged and the
    tokens they spent between them, as `synthesise_with_retry` does. The attempt's verdict is its problems keyed
    by warning code as `check_recap_editorial()` returns them, and its prompt
    is the one that drew it, so `--debug` saves a pair that replays (#450).
    The attempt loop is the one `review` shares (`synthesise_with_retry`):
    the retry is told exactly which claims were wrong and what the data says
    instead, or that the last draft was cut off (#357, #359, #372), and a
    retry that fails outright leaves the first attempt standing. What this
    command adds is its judge and its ranking -- complete rather than cut off,
    then fewer wrong claims -- so a worse second roll never ships over a
    better one. The first call's failure is the caller's, as it always was.
    """
    from fpl_cli.prompts.league_recap import normalise_recap_editorial
    from fpl_cli.prompts.league_recap_checks import (
        check_recap_editorial,
        get_recap_editorial_retry_prompt,
        recap_editorial_flaw,
        split_recap_problems,
    )

    def judge(summary: str, response: LLMResponse) -> dict[str, list[str]]:
        try:
            return check_recap_editorial(summary, collected_data)
        except (KeyError, TypeError, ValueError, IndexError) as e:
            # The checks are advisory: a bug in one (a row shape it did not
            # expect) must cost the check, never an editorial already paid for.
            logger.debug("Editorial check failed", exc_info=True)
            error_console.print(
                f"[yellow]  ⚠ The editorial could not be checked against its data ({type(e).__name__})"
                " -- saving it unchecked[/yellow]"
            )
            return {}

    # A cut-off attempt ranks below any complete one: it may simply have
    # stopped before reaching the claim the other got wrong, so its clean
    # check proves nothing. Among equals, fewer claims the data contradicts
    # wins, then fewer broken briefs (#459). A contradicted claim is stated as
    # fact and a roll call is a lesser fault, so the two are never traded at
    # any count: a retry that fixes a false claim but rolls the call still
    # ships, and one that clears the roll call by adding a false claim never
    # does. Weighing every problem as one tied the first of those, and let
    # several roll calls buy the second.
    def damage(attempt: SynthesisAttempt[dict[str, list[str]]]) -> tuple[int, int, int]:
        contradicted, broken = split_recap_problems(attempt.verdict)
        return int(attempt.response.stopped_early), len(contradicted), len(broken)

    return await synthesise_with_retry(
        provider,
        prompt=prompt,
        system_prompt=system_prompt,
        subject="The editorial",
        judge=judge,
        retry_reason=recap_editorial_flaw,
        damage=damage,
        retry_prompt=get_recap_editorial_retry_prompt,
        # The report writes its own title; whatever heading the model opened
        # with anyway is demoted beneath it or, when it only restates the
        # title, dropped (#349). The JSON payload carries the same text, so a
        # consumer sees the editorial as saved.
        normalise=lambda text: normalise_recap_editorial(text, league_name=collected_data["league_name"]),
    )


async def _recap_llm_summarise(
    collected_data: LeagueRecapData,
    gw: int,
    *,
    synthesis_provider: Any = None,
    dry_run: bool = False,
    debug: bool = False,
    is_bgw: bool = False,
    is_dgw: bool = False,
    season_length: int = 38,
    notes_pack: NotesPack | None = None,
    fines_tally: SeasonFinesTally | None = None,
    prior_seasons: PriorSeasonsSummary | None = None,
    month_honours: MonthlyHonours | None = None,
) -> None:
    """Run LLM summarisation for league recap. Mutates collected_data to add summaries."""
    from fpl_cli.prompts.league_recap import (
        collect_player_clubs,
        format_recap_awards_context,
        format_recap_captains_context,
        format_recap_chips_context,
        format_recap_fines_context,
        format_recap_league_history_context,
        format_recap_monthly_honours_context,
        format_recap_player_clubs_context,
        format_recap_prior_seasons_context,
        format_recap_season_fines_context,
        format_recap_standings_context,
        format_recap_standout_players_context,
        format_recap_transfers_context,
        format_recap_waivers_context,
        get_recap_synthesis_prompt,
    )
    from fpl_cli.prompts.league_recap_checks import recap_editorial_flaw

    # Setup debug directory
    debug_dir = None
    if debug or dry_run:
        debug_dir = Path("data/debug")
        debug_dir.mkdir(parents=True, exist_ok=True)

    awards_text = format_recap_awards_context(collected_data)
    standings_text = format_recap_standings_context(collected_data)
    standout_players_text = format_recap_standout_players_context(collected_data)
    # One walk of the squads/transfers, shared by both sections that need it.
    player_clubs = collect_player_clubs(collected_data)
    captains_text = format_recap_captains_context(collected_data, player_clubs)
    player_clubs_text = format_recap_player_clubs_context(player_clubs)
    chips_text = format_recap_chips_context(collected_data)
    transfers_text = format_recap_transfers_context(collected_data)
    waivers_text = format_recap_waivers_context(collected_data)
    fines_text = format_recap_fines_context(collected_data, fines_tally)
    league_history_text = format_recap_league_history_context(notes_pack)
    monthly_honours_text = format_recap_monthly_honours_context(month_honours)
    season_fines_text = format_recap_season_fines_context(fines_tally)
    prior_seasons_text = format_recap_prior_seasons_context(prior_seasons)

    system_prompt, user_prompt = get_recap_synthesis_prompt(
        gw=gw,
        league_name=collected_data["league_name"],
        fpl_format=collected_data["fpl_format"],
        awards_text=awards_text,
        standings_text=standings_text,
        fines_text=fines_text,
        season_fines_text=season_fines_text,
        captains_text=captains_text,
        chips_text=chips_text,
        transfers_text=transfers_text,
        waivers_text=waivers_text,
        player_clubs_text=player_clubs_text,
        league_history_text=league_history_text,
        monthly_honours_text=monthly_honours_text,
        prior_seasons_text=prior_seasons_text,
        standout_players_text=standout_players_text,
        is_bgw=is_bgw,
        is_dgw=is_dgw,
        season_length=season_length,
    )

    # The prompt `--debug` saves: the one that drew the editorial that
    # shipped, which after a retry is the first plus what it got wrong. A dry
    # run, or a call that never landed, saves the prompt as built.
    shipped_prompt = user_prompt

    if dry_run:
        console.print("[dim]  Dry run: saving prompts without calling LLMs...[/dim]")
        if debug_dir:
            (debug_dir / "recap_system.txt").write_text(system_prompt, encoding="utf-8")
            (debug_dir / "recap_prompt.txt").write_text(user_prompt, encoding="utf-8")
            console.print("[dim]    Saved recap_system.txt, recap_prompt.txt[/dim]")
        collected_data["synthesis_summary"] = "[DRY RUN - synthesis provider not called]"
    elif synthesis_provider:
        try:
            console.print("[dim]  Generating league editorial...[/dim]")
            shipped, attempts, usage = await _editorial_with_data_checks(
                synthesis_provider,
                prompt=user_prompt,
                system_prompt=system_prompt,
                collected_data=collected_data,
            )
            summary, problems, synthesis_result = shipped.text, shipped.verdict, shipped.response
            shipped_prompt = shipped.prompt
            collected_data["synthesis_summary"] = summary
            # How many drafts were judged, which one shipped, and what they
            # cost between them, so a recap that "feels different" can be
            # read against the last one rather than guessed at (#450).
            collected_data["synthesis_attempts"] = attempts
            collected_data["synthesis_shipped_attempt"] = shipped.number
            collected_data["synthesis_usage"] = {
                "input_tokens": usage.input_tokens,
                "output_tokens": usage.output_tokens,
            }
            if problems:
                # Kept, not scrubbed: the rest of the editorial is sound, and
                # the saved report carries these lines above it so the wrong
                # claim is never read as fact weeks later (#357, #359).
                collected_data["synthesis_problems"] = problems
                error_console.print(
                    f"[yellow]  ⚠ The editorial still {recap_editorial_flaw(problems)}:[/yellow]"
                )
                for found in problems.values():
                    for problem in found:
                        error_console.print(f"[yellow]    - {rich_escape(problem)}[/yellow]")
            if synthesis_result.stopped_early:
                # The recap's editorial goes into a saved report too, so a
                # truncated one must not look finished. Recorded for the JSON
                # payload and the report's callout as well as said out loud
                # here (#266, #372).
                collected_data["synthesis_stop_reason"] = str(synthesis_result.stop_reason)
                stop_reason = rich_escape(str(synthesis_result.stop_reason))
                if synthesis_result.refused:
                    # Not "cut off": a refusal was declined, not truncated,
                    # and re-running usually draws the same refusal (#399).
                    collected_data["synthesis_refused"] = True
                    category = ""
                    if synthesis_result.stop_category:
                        # Which check declined it, when the provider said (#450).
                        collected_data["synthesis_stop_category"] = synthesis_result.stop_category
                        category = f", category: {rich_escape(synthesis_result.stop_category)}"
                    error_console.print(
                        "[yellow]  The provider refused the editorial, so it may be incomplete"
                        f" (stop_reason: {stop_reason}{category})[/yellow]"
                    )
                else:
                    error_console.print(
                        "[yellow]  The editorial may be cut off: the provider stopped early"
                        f" (stop_reason: {stop_reason})[/yellow]"
                    )
            else:
                console.print("[green]  Done[/green] League editorial complete")
        except ProviderError as e:
            error_console.print(f"[yellow]  LLM synthesis failed: {e}[/yellow]")
        except Exception:  # noqa: BLE001 — graceful degradation
            logger.debug("Synthesis provider failed for recap", exc_info=True)
            error_console.print("[yellow]  LLM synthesis failed (unexpected error)[/yellow]")

    if debug and debug_dir:
        (debug_dir / "recap_system.txt").write_text(system_prompt, encoding="utf-8")
        (debug_dir / "recap_prompt.txt").write_text(shipped_prompt, encoding="utf-8")
