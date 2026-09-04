"""Season preview intel command group."""
# Pattern: direct-api

from __future__ import annotations

import asyncio
from typing import Any

import click
from rich.markup import escape as rich_escape
from rich.panel import Panel
from rich.table import Table

from fpl_cli.cli._context import console, error_console
from fpl_cli.cli._json import emit_failure, emit_json, output_format_option
from fpl_cli.season import season_label
from fpl_cli.services.season_previews import (
    SECTION_DECAY,
    Coverage,
    SeasonPreviewsService,
    TeamPreview,
    Usability,
    example_file,
    expired_sections,
    live_sections,
    previews_dir,
    resolve_preview_names,
    section_confidence,
    team_set_warning,
    unknown_teams,
    unresolved_players,
    write_resolved_codes,
)

COMMAND = "intel"

_USABILITY_HELP: dict[Usability, str] = {
    Usability.FULL: "Full use: intel may support or oppose a pick.",
    Usability.NEGATIVE_FILTER_ONLY: (
        "Negative filter only: use for injuries and rotation risk, never to promote a player."
        " Covered teams would otherwise look better than uncovered ones purely for being written up."
    ),
    Usability.NONE: "No usable intel: nothing loaded, or everything has aged out for this gameweek.",
}


def _warn_load_problems(service: SeasonPreviewsService) -> None:
    """Surface files the loader skipped.

    A preview the user hand-wrote failing silently is worse than no preview at
    all: the squad it would have changed is built without it and nothing says so.
    """
    for warning in service.load_warnings:
        error_console.print(f"[yellow]{rich_escape(warning)}[/yellow]")


async def _upcoming_gameweek(client: Any) -> int:
    """Gameweek to decay against, read from an open client.

    The *upcoming* gameweek is the planning horizon, so next takes precedence
    over current. Pre-season both may be absent and GW1 is the right answer.
    """
    next_gw = await client.get_next_gameweek()
    if next_gw:
        return int(next_gw["id"])
    current_gw = await client.get_current_gameweek()
    if current_gw:
        return int(current_gw["id"])
    return 1


async def _resolve_gameweek(explicit: int | None) -> int:
    """Gameweek to decay against, opening a client only when one is needed."""
    if explicit is not None:
        return explicit

    from fpl_cli.api.fpl import FPLClient

    async with FPLClient() as client:
        return await _upcoming_gameweek(client)


def _decay_rows(gameweek: int) -> list[dict[str, Any]]:
    return [
        {
            "section": section,
            "full_until_gw": full_until,
            "expires_at_gw": expires_at,
            "confidence": section_confidence(section, gameweek),
        }
        for section, (full_until, expires_at) in SECTION_DECAY.items()
    ]


@click.group(COMMAND, invoke_without_command=True)
@click.option("--gameweek", "-g", type=int, default=None,
              help="Decay intel as at this gameweek (default: the upcoming one)")
@click.option("--show-decay", is_flag=True, help="Show when each kind of intel expires")
@output_format_option
@click.pass_context
def intel_group(ctx: click.Context, gameweek: int | None, show_decay: bool, output_format: str) -> None:
    """Show season preview intel you have collected for each team."""
    if ctx.invoked_subcommand is not None:
        return
    _summary(gameweek, show_decay, output_format)


async def _gameweek_and_teams(explicit: int | None) -> tuple[int, set[str]]:
    """Gameweek to decay against, plus the live team short names.

    One client, one bootstrap-static fetch: the gameweek and the team list
    come from the same cached payload.
    """
    from fpl_cli.api.fpl import FPLClient

    async with FPLClient() as client:
        gw = explicit if explicit is not None else await _upcoming_gameweek(client)
        teams = await client.get_teams()
    return gw, {team.short_name.upper() for team in teams}


def _summary(gameweek: int | None, show_decay: bool, output_format: str) -> None:
    service = SeasonPreviewsService()
    previews = service.get_previews()

    async def _run() -> tuple[int, set[str]]:
        return await _gameweek_and_teams(gameweek)

    try:
        gw, valid_shorts = asyncio.run(_run())
    except Exception as exc:  # noqa: BLE001 -- offline must still report on-disk intel
        if gameweek is None:
            error_console.print(f"[yellow]Could not reach FPL API ({exc}); assuming GW1[/yellow]")
        else:
            error_console.print(f"[yellow]Skipping team-name validation: {exc}[/yellow]")
        gw, valid_shorts = gameweek or 1, set()

    coverage = service.coverage(gw, valid_teams=valid_shorts or None)
    unknown = unknown_teams(previews, valid_shorts) if valid_shorts else []
    drift = team_set_warning(previews, valid_shorts, coverage)
    unresolved = unresolved_players(previews)

    if output_format == "json":
        decay = _decay_rows(gw)
        emit_json(
            COMMAND,
            service.as_dicts(gw),
            {
                "gameweek": gw,
                "season": season_label(),
                "previews_dir": str(service.previews_path),
                "coverage": coverage.as_dict(),
                "usage_policy": _USABILITY_HELP[coverage.usable_as],
                "sections_live": [r["section"] for r in decay if r["confidence"] > 0],
                "sections_expired": [r["section"] for r in decay if r["confidence"] <= 0],
                "section_confidence": {
                    r["section"]: r["confidence"] for r in decay if r["confidence"] > 0
                },
                "decay_schedule": decay,
                "unknown_teams": unknown,
                "team_set_warning": drift,
                "unresolved_players": [{"team": t, "name": n} for t, n in unresolved],
                "warnings": service.load_warnings,
            },
        )
        return

    _warn_load_problems(service)
    console.print(Panel.fit(f"[bold blue]Season Preview Intel - {season_label()} (as at GW{gw})[/bold blue]"))

    if not previews:
        console.print(f"[dim]No previews found in {service.previews_path}[/dim]")
        console.print("[dim]Run 'fpl intel init' to scaffold one file per team.[/dim]")
        return

    _render_previews(previews, gw)
    _render_coverage(coverage)

    if drift:
        error_console.print(f"[yellow]{rich_escape(drift)}[/yellow]")
    if unresolved:
        shown = ", ".join(f"{team} {name}" for team, name in unresolved[:6])
        suffix = f" (+{len(unresolved) - 6} more)" if len(unresolved) > 6 else ""
        error_console.print(f"[yellow]Players with no code, will not join to FPL data: {shown}{suffix}[/yellow]")

    if show_decay:
        _render_decay(gw)


def _render_previews(previews: list[TeamPreview], gameweek: int) -> None:
    table = Table(show_header=True, header_style="bold")
    table.add_column("Team")
    table.add_column("Source")
    table.add_column("Published")
    table.add_column("Players", justify="right")
    table.add_column("Finish", justify="right")
    table.add_column("Live intel")

    for preview in previews:
        if not preview.has_content:
            table.add_row(preview.team, "[dim]stub, not filled in[/dim]", "", "", "", "")
            continue
        emitted = preview.as_dict(gameweek)
        present = set(emitted.get("sections_present", []))
        sections = [s for s in emitted["section_confidence"] if s in present]
        table.add_row(
            preview.team,
            rich_escape(preview.source),
            preview.published.isoformat(),
            str(len(emitted.get("players", []))),
            str(preview.predicted_finish or "-"),
            ", ".join(sections) if sections else "[dim]all expired[/dim]",
        )
    console.print(table)


def _render_coverage(coverage: Coverage) -> None:
    style = {
        Usability.FULL: "green",
        Usability.NEGATIVE_FILTER_ONLY: "yellow",
        Usability.NONE: "red",
    }[coverage.usable_as]
    console.print(
        f"\nCoverage: [{style}]{coverage.teams}/{coverage.of} teams"
        f" ({coverage.pct:.0%})[/{style}]"
    )
    console.print(f"[dim]{_USABILITY_HELP[coverage.usable_as]}[/dim]")


def _render_decay(gameweek: int) -> None:
    table = Table(show_header=True, header_style="bold", title=f"Decay schedule (GW{gameweek})")
    table.add_column("Section")
    table.add_column("Full until", justify="center")
    table.add_column("Expires", justify="center")
    table.add_column("Now", justify="right")
    for row in _decay_rows(gameweek):
        confidence = row["confidence"]
        style = "green" if confidence == 1.0 else "yellow" if confidence > 0 else "dim"
        table.add_row(
            row["section"],
            f"GW{row['full_until_gw']}",
            f"GW{row['expires_at_gw']}",
            f"[{style}]{confidence:.2f}[/{style}]",
        )
    console.print()
    console.print(table)


@intel_group.command("show")
@click.argument("team")
@click.option("--gameweek", "-g", type=int, default=None,
              help="Decay intel as at this gameweek (default: the upcoming one)")
@output_format_option
@click.pass_context
def show_command(ctx: click.Context, team: str, gameweek: int | None, output_format: str) -> None:
    """Show the full preview you have collected for one team."""
    if gameweek is None and ctx.parent is not None:
        # `fpl intel -g 5 show ARS` parses -g on the group; honour it rather
        # than silently decaying to the live gameweek.
        gameweek = ctx.parent.params.get("gameweek")

    service = SeasonPreviewsService()
    preview = service.get_preview(team)

    if preview is None:
        if output_format != "json":
            _warn_load_problems(service)
        emit_failure(COMMAND, f"No preview for '{team}' in {service.previews_path}", output_format)

    try:
        gw = asyncio.run(_resolve_gameweek(gameweek))
    except Exception as exc:  # noqa: BLE001 -- offline must still report on-disk intel
        if gameweek is None:
            error_console.print(f"[yellow]Could not reach FPL API ({exc}); assuming GW1[/yellow]")
        gw = gameweek or 1

    emitted = preview.as_dict(gw)

    if output_format == "json":
        emit_json(
            COMMAND,
            emitted,
            {
                "gameweek": gw,
                "season": season_label(),
                "path": str(preview.path) if preview.path else None,
                "coverage": service.coverage(gw).as_dict(),
                "sections_live": live_sections(gw),
                "sections_expired": expired_sections(gw),
                "warnings": service.load_warnings,
            },
        )
        return

    _warn_load_problems(service)
    attribution = f"{preview.source}" + (f" - {preview.author}" if preview.author else "")
    console.print(Panel.fit(f"[bold blue]{preview.team} preview[/bold blue] [dim]({rich_escape(attribution)})[/dim]"))
    console.print(f"[dim]Published {preview.published.isoformat()}, shown as at GW{gw}[/dim]")
    if preview.url:
        console.print(f"[dim]{rich_escape(preview.url)}[/dim]")

    if not preview.has_content:
        console.print("\n[dim]Stub file - no intel filled in yet.[/dim]")
        return

    if "predicted_finish" in emitted:
        console.print(f"\nPredicted finish: [bold]{emitted['predicted_finish']}[/bold]")
    strength = emitted.get("team_strength")
    if strength:
        parts = [f"{key}: {value}" for key, value in strength.items() if key != "notes"]
        if parts:
            console.print(f"Team strength: {', '.join(parts)}")
        if strength.get("notes"):
            console.print(f"[dim]{rich_escape(str(strength['notes']))}[/dim]")

    for key, label in (("transfers_in", "In"), ("transfers_out", "Out")):
        if emitted.get(key):
            console.print(f"{label}: {rich_escape(', '.join(emitted[key]))}")

    players = emitted.get("players", [])
    if players:
        table = Table(show_header=True, header_style="bold")
        table.add_column("Player")
        table.add_column("Status")
        table.add_column("Notes")
        for player in players:
            notes = [
                str(player[key])
                for key in ("injury", "role_change", "notes")
                if player.get(key)
            ]
            if player.get("set_pieces"):
                notes.append(f"set pieces: {', '.join(player['set_pieces'])}")
            if player.get("penalties"):
                notes.append("penalties")
            if player.get("new_signing"):
                notes.append("new signing")
            table.add_row(
                rich_escape(player["name"]),
                player.get("status", "-"),
                rich_escape("; ".join(notes)),
            )
        console.print()
        console.print(table)

    if emitted.get("narrative"):
        console.print(f"\n[dim]{rich_escape(str(emitted['narrative']))}[/dim]")

    expired = expired_sections(gw)
    if expired:
        console.print(f"\n[dim]Expired at GW{gw} and not shown: {', '.join(expired)}[/dim]")


@intel_group.command("init")
@click.option("--force", is_flag=True, help="Overwrite existing preview files")
def init_command(force: bool) -> None:
    """Create an empty preview file for each Premier League team."""
    from fpl_cli.api.fpl import FPLClient

    async def _run() -> list[tuple[str, str]]:
        async with FPLClient() as client:
            teams = await client.get_teams()
        return sorted((team.short_name.upper(), team.name) for team in teams)

    try:
        teams = asyncio.run(_run())
    except Exception as exc:  # noqa: BLE001 -- report the failure rather than a stack trace
        error_console.print(f"[red]Could not fetch teams from the FPL API: {exc}[/red]")
        raise SystemExit(1) from exc

    target = previews_dir()
    target.mkdir(parents=True, exist_ok=True)

    created, skipped = [], []
    for short_name, full_name in teams:
        path = target / f"{short_name}.yaml"
        if path.exists() and not force:
            skipped.append(short_name)
            continue
        path.write_text(_stub(short_name, full_name), encoding="utf-8")
        created.append(short_name)

    console.print(Panel.fit(f"[bold blue]Preview scaffolding - {season_label()}[/bold blue]"))
    console.print(f"Directory: {target}")
    if created:
        console.print(f"[green]Created {len(created)}:[/green] {', '.join(created)}")
    if skipped:
        console.print(f"[dim]Left alone {len(skipped)} (use --force to overwrite):[/dim] {', '.join(skipped)}")
    console.print(
        "\n[dim]Stubs do not count toward coverage until you fill them in."
        f"\nSchema reference: {example_file()}[/dim]"
    )


def _stub(short_name: str, full_name: str) -> str:
    """A valid but empty preview: parses cleanly, contributes no intel.

    Deliberately contentless so scaffolding the league cannot inflate coverage
    into unlocking positive use with nothing behind it.
    """
    from fpl_cli.utils.time import now_uk

    return f"""# {full_name} season preview - {season_label()}
# Fill in from whatever source you read, then check with: fpl intel show {short_name}
# Schema reference: {example_file()}

schema_version: 1
team: {short_name}
season: "{season_label()}"
source: "TODO - where this came from"
published: {now_uk().date().isoformat()}

# predicted_finish: 10
# team_strength:
#   attack: 50
#   defence: 50
#   set_pieces: 50
#   notes: ""

# transfers_in: []
# transfers_out: []

players: []

# narrative: |
#   Free text that does not fit a field above.
"""


@intel_group.command("resolve")
@click.argument("team")
@click.option("--write", is_flag=True, help="Write resolved codes back into the preview file")
@click.option("--all", "resolve_all", is_flag=True, help="Re-resolve players that already have a code")
@output_format_option
def resolve_command(team: str, write: bool, resolve_all: bool, output_format: str) -> None:
    """Match player names in a preview to their FPL player codes."""
    from fpl_cli.api.fpl import FPLClient

    service = SeasonPreviewsService()
    preview = service.get_preview(team)
    if preview is None or preview.path is None:
        emit_failure(COMMAND, f"No preview for '{team}' in {service.previews_path}", output_format)

    async def _run() -> list[Any]:
        async with FPLClient() as client:
            players = await client.get_players()
            teams = await client.get_teams()
        team_ids = {t.id for t in teams if t.short_name.upper() == preview.team}
        return [p for p in players if p.team_id in team_ids]

    try:
        squad = asyncio.run(_run())
    except Exception as exc:  # noqa: BLE001 -- report the failure rather than a stack trace
        emit_failure(
            COMMAND, f"Could not fetch the squad from the FPL API: {exc}", output_format,
            cause=exc,
        )

    if not squad:
        emit_failure(
            COMMAND, f"No Premier League squad found for team code '{preview.team}'",
            output_format,
        )

    matches = resolve_preview_names(preview, squad, only_missing=not resolve_all)
    # --all re-resolves entries that already carry a code, so its writes must be
    # allowed to replace one; the default path never touches an existing code.
    written = write_resolved_codes(preview.path, matches, overwrite=resolve_all) if write else 0
    unresolved = [m for m in matches if m.code is None]

    if output_format == "json":
        emit_json(
            COMMAND,
            [m.as_dict() for m in matches],
            {
                "team": preview.team,
                "path": str(preview.path),
                "squad_size": len(squad),
                "resolved": len(matches) - len(unresolved),
                "unresolved": len(unresolved),
                "written": written,
            },
        )
        return

    _warn_load_problems(service)
    console.print(Panel.fit(f"[bold blue]Resolving {preview.team} player names[/bold blue]"))

    if not matches:
        console.print("[dim]Every player already has a code. Use --all to re-resolve.[/dim]")
        return

    table = Table(show_header=True, header_style="bold")
    table.add_column("Preview name")
    table.add_column("Matched")
    table.add_column("Code", justify="right")
    table.add_column("How")
    for match in matches:
        style = {"exact": "green", "fuzzy": "yellow"}.get(match.how, "red")
        detail = match.matched_name or (", ".join(match.candidates) if match.candidates else "-")
        table.add_row(
            rich_escape(match.name),
            rich_escape(detail),
            str(match.code) if match.code is not None else "-",
            f"[{style}]{match.how}[/{style}]",
        )
    console.print(table)

    if write:
        console.print(f"\n[green]Wrote {written} code(s) to {preview.path}[/green]")
    elif any(m.code is not None for m in matches):
        console.print("\n[dim]Dry run. Re-run with --write to save these codes.[/dim]")

    if unresolved:
        console.print(
            f"[yellow]{len(unresolved)} name(s) need a code filled in by hand:"
            f" {rich_escape(', '.join(m.name for m in unresolved))}[/yellow]"
        )


@intel_group.command("schema")
def schema_command() -> None:
    """Print the preview file format with every field explained."""
    path = example_file()
    try:
        content = path.read_text(encoding="utf-8")
    except OSError as exc:
        error_console.print(f"[red]Could not read the schema reference at {path}: {exc}[/red]")
        raise SystemExit(1) from exc
    console.print(f"[dim]# Schema reference: {path}[/dim]")
    print(content)
