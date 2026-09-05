"""League recap command - entertainment-first GW report for all league participants."""
# Pattern: via-agent
# pyright: reportTypedDictNotRequiredAccess=false

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

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
)
from fpl_cli.cli._json import (
    api_failure_boundary,
    config_failure_boundary,
    emit_json,
    emit_json_error,
    json_output_mode,
    output_format_option,
)
from fpl_cli.cli._league_recap_types import LeagueRecapData
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
              help="Rebuild earlier gameweeks in full detail (captains, squads, transfers) "
                   "- one extra request per manager per gameweek")
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
    from fpl_cli.cli._fines_config import parse_fines_config
    from fpl_cli.cli._league_recap_data import (
        RecapReconciliationError,
        collect_classic_recap_data,
        collect_draft_recap_data,
        configured_fine_rule_types,
        evaluate_league_fines,
    )
    from fpl_cli.cli._league_recap_history import capture_recap_history
    from fpl_cli.cli.review import _review_resolve_gw

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
            gw_result = await _review_resolve_gw(client, gameweek)
            if gw_result is None:
                if output_format == "json":
                    emit_json_error("league-recap", "Could not resolve a gameweek to recap.", file=stdout)
                return
            gw: int = gw_result["gw"]

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
            from fpl_cli.services.fixture_predictions import (
                find_blank_gameweeks,
                resolve_players_with_fixture,
            )

            teams_list = list(teams.values())
            blank_gws = find_blank_gameweeks({gw: raw_fixtures}, teams_list, gw, gw)
            bgw_team_ids = frozenset(t["team_id"] for t in blank_gws.get(gw, []))
            # Same question answered from the gameweek rather than from
            # today's clubs, which is the only answer a replay can trust
            # (issue #169). None whenever the gameweek cannot answer, and
            # `bgw_team_ids` carries it as before.
            with_fixture = resolve_players_with_fixture(live_data, raw_fixtures)

            # Get next GW deadline
            from datetime import datetime, timedelta

            from fpl_cli.season import TOTAL_GAMEWEEKS
            from fpl_cli.utils.time import format_deadline

            gameweeks = await client.get_gameweeks()
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
            current_gw_id = next((g["id"] for g in gameweeks if g.get("is_current")), None)
            is_live_gw = (
                bool(finished_gws) and gw == max(finished_gws) and gw == current_gw_id
            )
            # Said out loud only where the user asked for the latest gameweek
            # and the season moved past it -- an explicitly older `-g` is a
            # replay by intent and needs no explaining. Without this the
            # degradation is silent, and for draft it is visible (cumulative
            # totals and positions fall back to the ledger, or go unavailable).
            standings_moved_on = (
                bool(finished_gws)
                and gw == max(finished_gws)
                and current_gw_id is not None
                and gw != current_gw_id
            )
            if standings_moved_on:
                error_console.print(
                    f"[yellow]Gameweek {current_gw_id} has started, so the league table now"
                    f" describes it rather than GW{gw}. Recapping GW{gw} from each manager's"
                    " own gameweek history instead -- run the recap before the next deadline"
                    " for the fullest capture.[/yellow]"
                )

            # Collect format-specific data
            try:
                if is_draft:
                    collected_data = await collect_draft_recap_data(
                        settings=settings, gw=gw, live_stats=live_stats,
                        players=players, teams=teams, is_live_gw=is_live_gw,
                        bgw_team_ids=bgw_team_ids, players_with_fixture=with_fixture,
                    )
                else:
                    collected_data = await collect_classic_recap_data(
                        client=client, settings=settings, gw=gw,
                        live_stats=live_stats, player_map=player_map, teams=teams,
                        is_live_gw=is_live_gw, bgw_team_ids=bgw_team_ids,
                        players_with_fixture=with_fixture,
                    )
            except RecapReconciliationError as e:
                # A stop condition, not a soft skip: exit non-zero so a
                # scripted caller (the gw-prep skill) sees the failure
                # rather than an empty but successful run. Under --format
                # json that has to be the envelope -- `click.ClickException`
                # writes its own prose to stderr and leaves stdout empty,
                # which is the same silence in a different shape (#159 review).
                if output_format == "json":
                    emit_json_error("league-recap", str(e), file=stdout, cause=e)
                raise click.ClickException(str(e)) from e

            # Add context metadata
            collected_data["is_bgw"] = is_bgw
            collected_data["is_dgw"] = is_dgw
            collected_data["season_length"] = TOTAL_GAMEWEEKS  # type: ignore[typeddict-unknown-key]
            if next_deadline:
                collected_data["next_deadline"] = next_deadline  # type: ignore[typeddict-unknown-key]

            if is_draft and waiver_deadline:
                collected_data["waiver_deadline"] = waiver_deadline  # type: ignore[typeddict-unknown-key]

            # Evaluate fines per manager (graceful skip when unconfigured)
            ruling = evaluate_league_fines(
                collected_data["managers"], settings, collected_data["fpl_format"],
            )
            if ruling.fines:
                collected_data["fines"] = ruling.fines
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
                target_with_fixture = resolve_players_with_fixture(
                    target_live, target_fixtures,
                )
                if is_draft:
                    replayed = await collect_draft_recap_data(
                        settings=settings, gw=target_gw, live_stats=target_stats,
                        players=players, teams=teams, is_live_gw=False,
                        bgw_team_ids=target_bgw_ids,
                        players_with_fixture=target_with_fixture,
                    )
                else:
                    replayed = await collect_classic_recap_data(
                        client=client, settings=settings, gw=target_gw,
                        live_stats=target_stats, player_map=player_map, teams=teams,
                        is_live_gw=False, bgw_team_ids=target_bgw_ids,
                        players_with_fixture=target_with_fixture,
                    )
                replayed["is_bgw"] = len(target_fixtures) < 10
                replayed["is_dgw"] = len(target_fixtures) > 10
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
                    fines_config=parse_fines_config(settings),
                    use_net_points=bool(settings.get("use_net_points", False)),
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
                output_dir = resolve_output_dir(settings, output)
                agent = ReportAgent(config={"output_dir": output_dir})
                result = await agent.run(context={
                    "report_type": "league-recap",
                    "gameweek": gw,
                    "data": dict(collected_data),
                })
                if result.data and result.data.get("report_path"):
                    console.print(f"\n[green]Report saved to {result.data['report_path']}[/green]")

            if output_format == "json":
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
                        "synthesis_summary": collected_data.get("synthesis_summary"),
                        # The skipped editorial rides the same channel as the
                        # capture's own warnings: `synthesis_summary` being
                        # null does not say whether it was asked for.
                        "warnings": capture_result.warnings + (
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
                            [{
                                "code": RECAP_WARNING_STANDINGS_MOVED_ON,
                                "message": (
                                    f"Gameweek {current_gw_id} has started, so the league"
                                    f" table describes it rather than GW{gw}. GW{gw} was"
                                    " recapped from each manager's own gameweek history;"
                                    " figures that only the live table can supply are"
                                    " derived or left unavailable."
                                ),
                            }] if standings_moved_on else []
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
                    },
                    file=stdout,
                )

    with api_failure_boundary("league-recap", output_format):
        asyncio.run(_run())


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
        "excess": entry.excess,
        "occurrences": entry.occurrences,
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

    # Fines
    fines = data.get("fines", [])
    if fines:
        console.print("\n[bold]Fines:[/bold]")
        for f in fines:
            console.print(f"  [red]{f['manager_name']}:[/red] {f['message']}")

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
    # replayed draft gameweek with no earlier rows) is named as unavailable
    # rather than silently dropped from the standings -- the same constants
    # `_format_standings_block` uses for the report surface, so the two
    # can't drift onto different wording.
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
) -> None:
    """Run LLM summarisation for league recap. Mutates collected_data to add summaries."""
    from fpl_cli.prompts.league_recap import (
        collect_player_clubs,
        format_recap_awards_context,
        format_recap_captains_context,
        format_recap_chips_context,
        format_recap_fines_context,
        format_recap_league_history_context,
        format_recap_player_clubs_context,
        format_recap_season_fines_context,
        format_recap_standings_context,
        get_recap_synthesis_prompt,
    )

    # Setup debug directory
    debug_dir = None
    if debug or dry_run:
        debug_dir = Path("data/debug")
        debug_dir.mkdir(parents=True, exist_ok=True)

    awards_text = format_recap_awards_context(collected_data)
    standings_text = format_recap_standings_context(collected_data)
    # One walk of the squads/transfers, shared by both sections that need it.
    player_clubs = collect_player_clubs(collected_data)
    captains_text = format_recap_captains_context(collected_data, player_clubs)
    player_clubs_text = format_recap_player_clubs_context(player_clubs)
    chips_text = format_recap_chips_context(collected_data)
    fines_text = format_recap_fines_context(collected_data, fines_tally)
    league_history_text = format_recap_league_history_context(notes_pack)
    season_fines_text = format_recap_season_fines_context(fines_tally)

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
        player_clubs_text=player_clubs_text,
        league_history_text=league_history_text,
        is_bgw=is_bgw,
        is_dgw=is_dgw,
        season_length=season_length,
    )

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
            synthesis_result = await synthesis_provider.query(
                prompt=user_prompt,
                system_prompt=system_prompt,
            )
            collected_data["synthesis_summary"] = synthesis_provider.post_process(synthesis_result.content)
            console.print("[green]  Done[/green] League editorial complete")
        except ProviderError as e:
            error_console.print(f"[yellow]  LLM synthesis failed: {e}[/yellow]")
        except Exception:  # noqa: BLE001 — graceful degradation
            logger.debug("Synthesis provider failed for recap", exc_info=True)
            error_console.print("[yellow]  LLM synthesis failed (unexpected error)[/yellow]")

    if debug and debug_dir:
        (debug_dir / "recap_system.txt").write_text(system_prompt, encoding="utf-8")
        (debug_dir / "recap_prompt.txt").write_text(user_prompt, encoding="utf-8")
