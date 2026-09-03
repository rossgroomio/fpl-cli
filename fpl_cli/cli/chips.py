"""Chip planning command group."""
# Pattern: mixed (chips_group: direct-api; chips_timing: via-agent)

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any, NamedTuple

import click
from rich.panel import Panel
from rich.table import Table

from fpl_cli.cli._context import console, error_console, load_settings
from fpl_cli.cli._json import (
    api_failure_boundary,
    emit_json,
    emit_json_error,
    json_output_mode,
    output_format_option,
)
from fpl_cli.models.chip_plan import ChipPlan, ChipType, PlannedChip, UsedChip
from fpl_cli.season import TOTAL_GAMEWEEKS

if TYPE_CHECKING:
    from fpl_cli.api.fpl import FPLClient

CHIP_NAMES = {
    "wildcard": "Wildcard",
    "freehit": "Free Hit",
    "bboost": "Bench Boost",
    "3xc": "Triple Captain",
}

CHIP_ABBREVS = {"wildcard": "WC", "freehit": "FH", "bboost": "BB", "3xc": "TC"}


def _format_used(used: UsedChip) -> str:
    return f"{CHIP_NAMES.get(used.chip, used.chip)} (GW{used.gameweek})"


def _resolve_current_gw(plan: ChipPlan) -> int:
    """Use stored current_gw from last sync, or fall back to flat count (GW 0)."""
    return plan.current_gw


@click.group("chips", invoke_without_command=True, subcommand_metavar="[COMMAND] [ARGS]...")
@click.pass_context
@output_format_option
def chips_group(ctx: click.Context, output_format: str) -> None:
    """View and plan FPL chip usage."""
    if ctx.invoked_subcommand is not None:
        return

    plan = ChipPlan.load()
    current_gw = _resolve_current_gw(plan)

    if output_format == "json":
        available = plan.get_available_chips(current_gw)
        used_sorted = sorted(plan.chips_used, key=lambda u: u.gameweek)
        planned_sorted = sorted(plan.chips, key=lambda c: c.gameweek)
        emit_json("chips", {
            "available": [
                {"chip": c.value, "name": CHIP_NAMES.get(c.value, c.value)}
                for c in available
            ],
            "used": [{"chip": u.chip, "gameweek": u.gameweek} for u in used_sorted],
            "planned": [
                {"chip": c.chip, "gameweek": c.gameweek, "notes": c.notes}
                for c in planned_sorted
            ],
        }, metadata={"gameweek": current_gw})
        return

    console.print(Panel.fit("[bold blue]Chip Status[/bold blue]"))

    if not current_gw:
        console.print("[dim]Run `fpl chips sync` for accurate availability[/dim]")

    available = plan.get_available_chips(current_gw)
    if available:
        labels = [CHIP_NAMES.get(c.value, c.value) for c in available]
        console.print(f"[bold]Available:[/bold] {', '.join(labels)}")
    else:
        console.print("[dim]All chips used[/dim]")

    if plan.chips_used:
        used_sorted = sorted(plan.chips_used, key=lambda u: u.gameweek)
        used_labels = [_format_used(u) for u in used_sorted]
        console.print(f"[bold]Used:[/bold] {', '.join(used_labels)}")

    if plan.chips:
        console.print("\n[bold]Planned:[/bold]")
        for chip in sorted(plan.chips, key=lambda c: c.gameweek):
            name = CHIP_NAMES.get(chip.chip, chip.chip)
            line = f"  [cyan]GW{chip.gameweek}[/cyan]: {name}"
            if chip.notes:
                line += f" [dim]({chip.notes})[/dim]"
            console.print(line)
    else:
        console.print("\n[dim]No chips planned[/dim]")


@chips_group.command("add")
@click.argument("chip_type", type=click.Choice(["wildcard", "freehit", "bboost", "3xc"]))
@click.option("--gw", "-g", type=click.IntRange(1, TOTAL_GAMEWEEKS), required=True, help="Gameweek to use chip")
@click.option("--notes", "-n", help="Notes about the chip usage")
def chips_add(chip_type: str, gw: int, notes: str | None) -> None:
    """Plan a chip for a gameweek.

    Chip types: wildcard (Wildcard), freehit (Free Hit),
    bboost (Bench Boost), 3xc (Triple Captain).

    Example:
      fpl chips add wildcard --gw 26
      fpl chips add freehit --gw 29 -n "Blank gameweek"
    """
    plan = ChipPlan.load()
    current_gw = _resolve_current_gw(plan)

    chip_enum = ChipType(chip_type)
    available = plan.get_available_chips(current_gw)
    if chip_enum not in available:
        name = CHIP_NAMES.get(chip_type, chip_type)
        console.print(f"[red]Error: {name} not available (already used)[/red]")
        avail_names = ", ".join(
            CHIP_NAMES.get(c.value, c.value) for c in available
        )
        console.print(f"  Available: {avail_names}")
        return

    existing = next((c for c in plan.chips if c.gameweek == gw), None)
    if existing:
        name = CHIP_NAMES.get(existing.chip, existing.chip)
        console.print(
            f"[red]Error: GW{gw} already has a chip planned: {name}[/red]",
        )
        console.print("  Remove it first with: fpl chips remove --gw N")
        return

    plan.chips.append(PlannedChip(chip=chip_enum, gameweek=gw, notes=notes or ""))
    plan.save()

    name = CHIP_NAMES.get(chip_type, chip_type)
    console.print(f"[green]\u2713[/green] Planned {name} for GW{gw}")


@chips_group.command("remove")
@click.option("--gw", "-g", type=click.IntRange(1, TOTAL_GAMEWEEKS), required=True, help="Gameweek to remove chip from")
def chips_remove(gw: int) -> None:
    """Remove a planned chip from a gameweek.

    Example:
      fpl chips remove --gw 26
    """
    plan = ChipPlan.load()

    original_count = len(plan.chips)
    plan.chips = [c for c in plan.chips if c.gameweek != gw]

    if len(plan.chips) == original_count:
        error_console.print(f"[yellow]No chip planned for GW{gw}[/yellow]")
        return

    plan.save()
    console.print(f"[green]\u2713[/green] Removed chip from GW{gw}")


@chips_group.command("sync")
def chips_sync() -> None:
    """Sync chip usage from FPL API.

    Fetches your chip usage history and updates the plan so available
    chips are tracked correctly.
    """
    from fpl_cli.api.fpl import FPLClient

    settings = load_settings()
    entry_id = settings.get("fpl", {}).get("classic_entry_id")

    if not entry_id:
        console.print("[red]Error: classic_entry_id not configured in settings.yaml[/red]")
        return

    async def _run() -> None:
        plan = ChipPlan.load()

        async with FPLClient() as client:
            history = await client.get_manager_history(entry_id)
            next_gw = await client.get_next_gameweek()

        current_gw = next_gw.get("id", 1) if next_gw else 1

        chips_data = history.get("chips", [])
        valid_chips = {c.value for c in ChipType}

        used_chips = [
            UsedChip(chip=ChipType(name), gameweek=event)
            for chip in chips_data
            # Filter: valid chip name; event=0 is falsy so skipped intentionally
            if (name := chip.get("name", "").lower()) in valid_chips
            and (event := chip.get("event", 0))
        ]

        plan.chips_used = used_chips
        plan.current_gw = current_gw

        cleared = plan.cleanup_exhausted_plans()

        plan.save()

        console.print(
            f"[green]\u2713[/green] Synced {len(used_chips)} used chips from FPL",
        )
        for c in cleared:
            name = CHIP_NAMES.get(c.chip, c.chip)
            console.print(
                f"[green]\u2713[/green] Cleared planned {name} GW{c.gameweek} (already played)",
            )

        if used_chips:
            console.print("\n[bold]Chips Used:[/bold]")
            for chip in sorted(used_chips, key=lambda u: u.gameweek):
                console.print(f"  \u2022 {_format_used(chip)}")

        available = plan.get_available_chips(current_gw)
        if available:
            console.print("\n[bold]Available:[/bold]")
            for chip in available:
                console.print(
                    f"  \u2022 [green]{CHIP_NAMES.get(chip.value, chip.value)}[/green]",
                )

    asyncio.run(_run())


# -- Chip timing: uses FixtureAgent for exposure data --


class _DoubleCandidate(NamedTuple):
    """A Triple Captain candidate's double, scored on its own fixtures."""

    player: str
    fdr: float
    fixture_count: int


def _best_double_candidate(
    players: list[str],
    gw: int,
    name_to_team_id: dict[str, int],
    id_to_short: dict[int, str],
    fdr_by_team: dict[str, dict[str, Any]],
    rated_teams: set[str],
) -> _DoubleCandidate | None:
    """Pick the squad player whose double in ``gw`` is the easiest, and score it.

    Each candidate is scored on the fixture agent's ``fdr_by_gameweek`` for
    their own team - the mean general FDR of that team's fixtures in the
    gameweek, on the venue-aware ATK/DEF model `fpl fdr` and `fpl preview`
    show - so a candidate facing two weak sides scores low. Grading the
    candidate's own club instead inverts the signal: `avg_overall_fdr` is the
    difficulty of facing that club, so a top club reads "hard" and the weakest
    club in the league reads "easy" (#201).

    Two candidates are not scoreable, and are skipped rather than graded on a
    number that does not mean what it says:

    - No fixture in the gameweek. A predicted double can sit beyond the
      agent's FDR window, leaving nothing to average.
    - An unrated club. Every FDR for one scores the ratings service's neutral
      4.0, which clears the "possible" threshold on a placeholder rather than
      a fixture read. Relevant at the season rollover, when a ratings file can
      pass every date check while knowing nothing about the promoted clubs
      (see `TeamRatingsService.check_team_set`).

    ``fixture_count`` reports how many fixtures the score was averaged over, so
    a caller can say when a double was graded on less than the full pair.

    Returns:
        The easiest scoreable candidate, or None if there is no such candidate.
    """
    scored: list[_DoubleCandidate] = []

    for player_name in players:
        tid = name_to_team_id.get(player_name)
        if not tid:
            continue
        short = id_to_short.get(tid)
        if not short or short not in rated_teams:
            continue
        gw_fdr = fdr_by_team.get(short, {}).get("fdr_by_gameweek", {}).get(gw)
        if not gw_fdr:
            continue
        scored.append(
            _DoubleCandidate(player_name, gw_fdr["fdr"], gw_fdr["fixture_count"]),
        )

    if not scored:
        return None
    return min(scored, key=lambda c: c.fdr)


def _compute_chip_signals(
    exposure: list[dict],
    unplayed: set[str],
    name_to_team_id: dict[str, int],
    id_to_short: dict[int, str],
    fdr_by_team: dict[str, dict[str, Any]],
    rated_teams: set[str],
) -> list[dict]:
    """Compute FH/BB/TC signals from squad exposure data."""
    signals: list[dict] = []

    for entry in exposure:
        gw = entry["gw"]
        gw_type = entry["type"]
        affected = entry["affected"]
        affected_players = entry["players"]
        source = entry["source"]

        if gw_type == "blank" and "freehit" in unplayed:
            if affected >= 5:
                signals.append({
                    "gw": gw, "signal": "FH", "strength": "strong", "source": source,
                    "players": affected_players, "detail": f"{affected} squad players blank",
                })
            elif affected >= 3:
                signals.append({
                    "gw": gw, "signal": "FH", "strength": "possible", "source": source,
                    "players": affected_players, "detail": f"{affected} squad players blank",
                })

        if gw_type == "double" and "bboost" in unplayed:
            if affected >= 8:
                signals.append({
                    "gw": gw, "signal": "BB", "strength": "strong", "source": source,
                    "players": affected_players, "detail": f"{affected} squad players double",
                })
            elif affected >= 6:
                signals.append({
                    "gw": gw, "signal": "BB", "strength": "possible", "source": source,
                    "players": affected_players, "detail": f"{affected} squad players double",
                })

        if gw_type == "double" and "3xc" in unplayed:
            best = _best_double_candidate(
                affected_players, gw, name_to_team_id, id_to_short,
                fdr_by_team, rated_teams,
            )

            if best is not None:
                # Same 1-7 scale as `fpl fdr`, on the double's own fixtures.
                # Thresholds are more lenient than that table's colouring
                # (2.5/3.0) because a DGW doubles the upside.
                if best.fdr <= 3.0:
                    tc_strength: str | None = "strong"
                elif best.fdr <= 4.0:
                    tc_strength = "possible"
                else:
                    tc_strength = None

                if tc_strength:
                    # A predicted double is only scoreable on the leg the
                    # fixtures API already lists, so say when the figure covers
                    # one fixture rather than the pair - an unscheduled second
                    # leg against a title contender is invisible to the mean.
                    partial = (
                        "" if best.fixture_count >= 2
                        else f", {best.fixture_count} of 2 scheduled"
                    )
                    signals.append({
                        "gw": gw, "signal": "TC", "strength": tc_strength, "source": source,
                        "players": affected_players,
                        "detail": f"{best.player} (FDR {best.fdr:.1f}{partial})",
                        "fixtures_scored": best.fixture_count,
                    })

    return signals


async def _fetch_and_compute(
    client: FPLClient,
    plan: ChipPlan,
    entry_id: int,
    current_gw: int,
    last_gw: int,
) -> tuple[set[str], dict[int, str], list[dict] | None]:
    """Fetch squad exposure and compute chip signals.

    Returns (unplayed, planned_by_gw, signals). signals is None on agent failure.
    """
    from fpl_cli.agents.data.fixture import FixtureAgent

    unplayed = {c.value for c in plan.get_available_chips(current_gw)}
    planned_by_gw: dict[int, str] = {c.gameweek: c.chip for c in plan.chips}

    picks_data = await client.get_manager_picks(entry_id, last_gw)
    if picks_data.get("active_chip") == "freehit":
        last_gw -= 1
        picks_data = await client.get_manager_picks(entry_id, last_gw)

    pick_ids = [p["element"] for p in picks_data.get("picks", [])]
    players = await client.get_players()
    player_map = {p.id: p for p in players}
    squad = [
        {
            "team_id": player_map[pid].team_id,
            "element_type": int(player_map[pid].position),
            "web_name": player_map[pid].web_name,
        }
        for pid in pick_ids
        if pid in player_map
    ]

    name_to_team_id = {p["web_name"]: p["team_id"] for p in squad}
    teams = await client.get_teams()
    id_to_short = {t.id: t.short_name for t in teams}

    async with FixtureAgent(config={}, client=client) as agent:
        result = await agent.run(context={"squad": squad})
        # Coverage check only - which clubs the ratings file actually knows.
        # The FDR itself comes from the agent, which scores an unrated club's
        # fixture at a neutral 4.0 that would clear the TC threshold on its own.
        rated_teams = {
            short for short in id_to_short.values()
            if agent.ratings_service.get_rating(short) is not None
        }

    if not result.success:
        return unplayed, planned_by_gw, None

    exposure = result.data.get("squad_exposure", [])
    signals = _compute_chip_signals(
        exposure, unplayed, name_to_team_id, id_to_short,
        result.data.get("fdr_by_team", {}), rated_teams,
    )
    return unplayed, planned_by_gw, signals


@chips_group.command("timing")
@output_format_option
def chips_timing(output_format: str) -> None:
    """Recommend chip timing based on blank/double GW exposure.

    Shows FH/BB/TC signals using your squad's exposure to upcoming
    blank and double gameweeks, cross-referenced with your chip plan.

    Thresholds (full 15-player squad):
      FH: 5+ squad players in a blank = strong, 3+ = possible
      BB: 8+ squad players in a double = strong, 6+ = possible
      TC: best DGW candidate's two fixtures average FDR <= 3.0 = strong,
          <= 4.0 = possible
    """
    from fpl_cli.api.fpl import FPLClient

    async def _run() -> None:
        plan = ChipPlan.load()

        settings = load_settings()
        entry_id = settings.get("fpl", {}).get("classic_entry_id")
        if not entry_id:
            if output_format == "json":
                emit_json_error("chips-timing", "classic_entry_id not configured")
            else:
                error_console.print("[yellow]classic_entry_id not configured[/yellow]")
            return

        if output_format == "json":
            with json_output_mode() as stdout:
                async with FPLClient() as client:
                    next_gw_data = await client.get_next_gameweek()
                    current_gw = next_gw_data.get("id", 1) if next_gw_data else 1
                    last_gw = current_gw - 1
                    if last_gw <= 0:
                        emit_json_error("chips-timing", "No completed gameweek found", file=stdout)
                        return

                    unplayed, planned_by_gw, signals = await _fetch_and_compute(
                        client, plan, entry_id, current_gw, last_gw,
                    )

                if signals is None:
                    emit_json_error("chips-timing", "Fixture agent failed", file=stdout)
                    return

                emit_json("chips-timing", signals, metadata={
                    "gameweek": current_gw,
                    "unplayed": sorted(unplayed),
                    "planned": [{"chip": c.chip, "gameweek": c.gameweek} for c in plan.chips],
                }, file=stdout)
            return

        async with FPLClient() as client:
            next_gw_data = await client.get_next_gameweek()
            current_gw = next_gw_data.get("id", 1) if next_gw_data else 1
            last_gw = current_gw - 1
            if last_gw <= 0:
                error_console.print("[yellow]No completed gameweek found[/yellow]")
                return

            unplayed, planned_by_gw, signals = await _fetch_and_compute(
                client, plan, entry_id, current_gw, last_gw,
            )

        if signals is None:
            console.print("[red]Agent failed[/red]")
            raise SystemExit(1)

        console.print(Panel.fit("[bold blue]Chip Timing Signals[/bold blue]"))

        if unplayed:
            chip_labels = [CHIP_ABBREVS.get(c, c.upper()) for c in sorted(unplayed)]
            console.print(f"[dim]Unplayed:[/dim] {' | '.join(chip_labels)}")
        else:
            console.print("[dim]All chips used[/dim]")

        if plan.chips:
            planned_str = "  ".join(
                f"{CHIP_ABBREVS.get(c.chip, c.chip.upper())} GW{c.gameweek}"
                for c in plan.chips
            )
            console.print(f"[dim]Planned:[/dim] {planned_str}")

        console.print()

        if not signals:
            console.print("[dim]No chip signals in confirmed/predicted fixtures[/dim]")
            return

        strength_style = {"strong": "green bold", "possible": "yellow"}

        table = Table(show_header=True, header_style="bold", box=None)
        table.add_column("GW", justify="center", width=4)
        table.add_column("Chip", justify="center", width=8)
        table.add_column("Strength", width=10)
        table.add_column("Detail", width=35)
        table.add_column("Source", width=10)
        table.add_column("Players")

        for sig in sorted(signals, key=lambda s: (s["gw"], s["signal"])):
            gw = sig["gw"]
            signal = sig["signal"]
            strength = sig["strength"]
            source = sig["source"]
            player_list = ", ".join(sig["players"][:5])
            if len(sig["players"]) > 5:
                player_list += f" +{len(sig['players']) - 5}"
            planned = gw in planned_by_gw and CHIP_ABBREVS.get(planned_by_gw[gw]) == signal
            tag = " [planned]" if planned else ""
            style = strength_style.get(strength, "")

            table.add_row(
                str(gw),
                f"{signal}{tag}",
                strength,
                sig["detail"],
                source,
                player_list,
                style=style,
            )

        console.print(table)

    with api_failure_boundary("chips-timing", output_format):
        asyncio.run(_run())
