"""League recap command - entertainment-first GW report for all league participants."""
# Pattern: via-agent
# pyright: reportTypedDictNotRequiredAccess=false

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

import click
from rich.panel import Panel

from fpl_cli.api.providers import ProviderError
from fpl_cli.cli._context import Format, console, error_console, get_format, load_settings, resolve_output_dir
from fpl_cli.cli._json import emit_json, emit_json_error, json_output_mode, output_format_option
from fpl_cli.cli._league_recap_types import LeagueRecapData
from fpl_cli.services.league_history import GameweekCoverage
from fpl_cli.services.league_history_notes import NotesPack, NotesPackEntry, NoteSurface

# KTD8: the console stays a highlights view -- only the top few streaks by
# excess-over-minimum, so a rare streak is not buried under common ones (R12).
_CONSOLE_STREAK_LIMIT = 5

logger = logging.getLogger(__name__)


@click.command("league-recap")
@click.option("--gameweek", "-g", type=int, help="Specific gameweek to recap (default: last completed)")
@click.option("--draft", "is_draft", is_flag=True, default=False,
              help="Use draft league (only needed when both formats are configured)")
@click.option("--save", "-s", is_flag=True, help="Save report to output directory")
@click.option("--output", "-o", type=click.Path(), help="Custom output directory for report")
@click.option("--summarise", is_flag=True, help="Add LLM-generated editorial narrative (requires API keys)")
@click.option("--backfill-detail", "backfill_detail", is_flag=True, default=False,
              help="Rebuild earlier gameweeks in full detail (captains, squads, transfers) "
                   "- one extra request per manager per gameweek")
@click.option("--debug", is_flag=True, help="Save LLM prompts and responses to data/debug/")
@click.option("--dry-run", is_flag=True, help="Build and save prompts to data/debug/ without calling LLMs")
@output_format_option
@click.pass_context
def league_recap_command(
    ctx: click.Context,
    gameweek: int | None, is_draft: bool, save: bool, output: str | None,
    summarise: bool, backfill_detail: bool, debug: bool, dry_run: bool,
    output_format: str,
) -> None:
    """Recap a completed gameweek for the whole league - awards, standings, and banter."""
    from fpl_cli.agents.orchestration.report import ReportAgent
    from fpl_cli.api.fpl import FPLClient
    from fpl_cli.cli._league_recap_data import (
        RecapReconciliationError,
        collect_classic_recap_data,
        collect_draft_recap_data,
        evaluate_league_fines,
    )
    from fpl_cli.cli._league_recap_history import capture_recap_history
    from fpl_cli.cli.review import _review_resolve_gw

    settings = load_settings()
    fmt = get_format(ctx)

    # Auto-select in single-format mode; respect --draft flag in BOTH mode
    if fmt == Format.DRAFT:
        is_draft = True
    elif fmt == Format.CLASSIC:
        is_draft = False

    synthesis_provider = None

    if summarise or dry_run:
        if not dry_run:
            from fpl_cli.api.providers import get_llm_provider

            try:
                synthesis_provider = get_llm_provider("synthesis", settings)
            except ProviderError as e:
                console.print(f"[red]Error: {e}[/red]")
                return

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
            from fpl_cli.services.fixture_predictions import find_blank_gameweeks

            teams_list = list(teams.values())
            blank_gws = find_blank_gameweeks({gw: raw_fixtures}, teams_list, gw, gw)
            bgw_team_ids = frozenset(t["team_id"] for t in blank_gws.get(gw, []))

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

            # gw is "live" when it's the most recently finished gameweek --
            # only then do current standings describe the same point in time
            # as the collected data, so only then can the two be reconciled.
            finished_gws = [g["id"] for g in gameweeks if g.get("finished")]
            is_live_gw = bool(finished_gws) and gw == max(finished_gws)

            # Collect format-specific data
            try:
                if is_draft:
                    collected_data = await collect_draft_recap_data(
                        settings=settings, gw=gw, live_stats=live_stats,
                        players=players, teams=teams, is_live_gw=is_live_gw,
                        bgw_team_ids=bgw_team_ids,
                    )
                else:
                    collected_data = await collect_classic_recap_data(
                        client=client, settings=settings, gw=gw,
                        live_stats=live_stats, player_map=player_map, teams=teams,
                        is_live_gw=is_live_gw, bgw_team_ids=bgw_team_ids,
                    )
            except RecapReconciliationError as e:
                # A stop condition, not a soft skip: exit non-zero so a
                # scripted caller (the gw-prep skill) sees the failure
                # rather than an empty but successful run.
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
            fines = evaluate_league_fines(
                collected_data["managers"], settings, collected_data["fpl_format"],
            )
            if fines:
                collected_data["fines"] = fines

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
                if is_draft:
                    replayed = await collect_draft_recap_data(
                        settings=settings, gw=target_gw, live_stats=target_stats,
                        players=players, teams=teams, is_live_gw=False,
                        bgw_team_ids=target_bgw_ids,
                    )
                else:
                    replayed = await collect_classic_recap_data(
                        client=client, settings=settings, gw=target_gw,
                        live_stats=target_stats, player_map=player_map, teams=teams,
                        is_live_gw=False, bgw_team_ids=target_bgw_ids,
                    )
                replayed["is_bgw"] = len(target_fixtures) < 10
                replayed["is_dgw"] = len(target_fixtures) > 10
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
                )
            notes_pack = capture_result.notes_pack

            # Report-surfaced history text, stashed as plain strings so the
            # Jinja template needs no knowledge of NotesPack/NoteSurface.
            # Absent entirely (rather than empty) when capture couldn't build
            # a pack, so the template's `is defined` guards skip the section.
            if notes_pack is not None:
                collected_data["league_history_phase_text"] = notes_pack.season_phase_entry.text
                collected_data["league_history_streak_lines"] = [
                    entry.text for entry in notes_pack.entries if NoteSurface.REPORT in entry.surfaces
                ]
                collected_data["league_history_coverage_lines"] = [
                    entry.text for entry in notes_pack.coverage_entries
                ]

            # LLM summarisation (opt-in via --summarise or --dry-run)
            if summarise or dry_run:
                try:
                    await _recap_llm_summarise(
                        collected_data, gw,
                        synthesis_provider=synthesis_provider,
                        dry_run=dry_run, debug=debug,
                        is_bgw=is_bgw, is_dgw=is_dgw,
                        season_length=TOTAL_GAMEWEEKS,
                        notes_pack=notes_pack,
                    )
                except ProviderError as e:
                    error_console.print(f"[yellow]LLM summarisation failed: {e}[/yellow]")
                except Exception:  # noqa: BLE001 — graceful degradation
                    logger.debug("LLM summarisation failed", exc_info=True)
                    error_console.print("[yellow]LLM summarisation failed (unexpected error)[/yellow]")

            # Display key highlights to console
            _render_console_highlights(collected_data, notes_pack)

            # Generate report if saving
            if save or output:
                output_dir = str(resolve_output_dir(settings, output))
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
                        "synthesis_summary": collected_data.get("synthesis_summary"),
                        "warnings": capture_result.warnings,
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
        "coverage_entries": [_serialize_notes_pack_entry(entry) for entry in pack.coverage_entries],
        "entry_count": pack.entry_count,
    }


def _render_console_highlights(data: LeagueRecapData, notes_pack: NotesPack | None = None) -> None:
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
) -> None:
    """Run LLM summarisation for league recap. Mutates collected_data to add summaries."""
    from fpl_cli.prompts.league_recap import (
        format_recap_awards_context,
        format_recap_captains_context,
        format_recap_chips_context,
        format_recap_fines_context,
        format_recap_league_history_context,
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
    captains_text = format_recap_captains_context(collected_data)
    chips_text = format_recap_chips_context(collected_data)
    fines_text = format_recap_fines_context(collected_data)
    league_history_text = format_recap_league_history_context(notes_pack)

    system_prompt, user_prompt = get_recap_synthesis_prompt(
        gw=gw,
        league_name=collected_data["league_name"],
        fpl_format=collected_data["fpl_format"],
        awards_text=awards_text,
        standings_text=standings_text,
        fines_text=fines_text,
        captains_text=captains_text,
        chips_text=chips_text,
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
