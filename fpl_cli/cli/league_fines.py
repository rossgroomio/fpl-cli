"""Season fines table read back from the league-history ledger."""
# Pattern: direct-api (reads the local ledger only -- no network at all)

from __future__ import annotations

from contextlib import nullcontext
from typing import TYPE_CHECKING, Any

import click
from rich.markup import escape as rich_escape
from rich.panel import Panel
from rich.table import Table

from fpl_cli.cli._context import Format, console, get_format, get_settings
from fpl_cli.cli._json import (
    config_failure_boundary,
    emit_failure,
    emit_json,
    json_output_mode,
    output_format_option,
)
from fpl_cli.season import is_season_label, season_label

if TYPE_CHECKING:
    from fpl_cli.services.league_history_fines import ManagerFineTally, SeasonFinesTally

# Rule types are config keys ("below-threshold"), which make poor column
# headers. Anything unrecognised falls back to the key itself rather than a
# guess. The fallback is not dead code for a closed set: the columns come
# from `SeasonFinesTally.rule_types`, which extends the configured order with
# every rule type observed in the ledger's own `fine_rules_evaluated`. The
# ledger outlives the release that wrote it, so a rule type recorded by an
# earlier version and since dropped from `VALID_RULE_TYPES` still needs a
# column -- and would reach here without ever passing through the parser.
_RULE_HEADINGS = {
    "last-place": "Last place",
    "below-threshold": "Below threshold",
    "red-card": "Red card",
}


@click.command("league-fines")
@click.option("--gameweek", "-g", type=int,
              help="Tally through this gameweek (default: the latest recorded)")
@click.option("--season", "season_override", type=str,
              help="Season to tally, e.g. 2025-26 (default: the current season)")
@click.option("--draft", "is_draft", is_flag=True, default=False,
              help="Use draft league (only needed when both formats are configured)")
@output_format_option
@click.pass_context
@config_failure_boundary
def league_fines_command(
    ctx: click.Context,
    gameweek: int | None,
    season_override: str | None,
    is_draft: bool,
    output_format: str,
) -> None:
    """Show who has been fined this season, and how much of it was ruled.

    Reads the fines already recorded against each gameweek, so it needs no
    network and works for any season still on disk. Every gameweek that could
    not be ruled - never recorded, unreadable, or recorded without the squad a
    rule needs - is named beneath the table, because a zero there means "not
    known", not "not fined".

    Fines are recorded as `league-recap` runs, so a gameweek you never
    recapped has nothing to tally.
    """
    from fpl_cli.cli._fines import rules_for_format
    from fpl_cli.cli._fines_config import parse_fines_config
    from fpl_cli.services.league_history import LeagueHistoryStore
    from fpl_cli.services.league_history_fines import build_season_fines_tally

    settings = get_settings(ctx)
    fmt = get_format(ctx)
    if fmt == Format.DRAFT:
        is_draft = True
    elif fmt == Format.CLASSIC:
        is_draft = False
    fpl_format = "draft" if is_draft else "classic"

    with json_output_mode() if output_format == "json" else nullcontext() as stdout:
        season = season_override or season_label()
        if season_override and not is_season_label(season_override):
            # Through `emit_failure` rather than a hand-rolled branch (#159):
            # it is the one place that knows prose and an envelope are
            # mutually exclusive, and it escapes the message -- which matters
            # here, because `season_override` is user input on its way into a
            # Rich console.
            emit_failure(
                "league-fines",
                f"'{season_override}' is not a season label. Use the ledger's own form, "
                f"e.g. {season_label()}.",
                output_format,
            )

        key = "draft_league_id" if is_draft else "classic_league_id"
        league_id = settings.get("fpl", {}).get(key)
        if not league_id:
            emit_failure(
                "league-fines",
                f"No {fpl_format} league id is configured, so there is no ledger partition "
                f"to read. Set fpl.{key} in settings.yaml (or run 'fpl init').",
                output_format,
            )

        store = LeagueHistoryStore(season, fpl_format, league_id)
        captured = store.captured_gameweeks()
        through = gameweek if gameweek is not None else (captured[-1] if captured else 0)

        fines_config = parse_fines_config(settings)
        rule_types = (
            [rule.type for rule in rules_for_format(fines_config, fpl_format)]
            if fines_config is not None else []
        )

        # `league_start_gameweek` is deliberately left unresolved: it lives in
        # the API, and this command is offline by design. The tally falls back
        # to the partition's earliest captured gameweek, which cannot invent a
        # gap before its own first row.
        tally = build_season_fines_tally(store, through, rule_types=rule_types)

        if output_format == "json":
            emit_json(
                "league-fines",
                _serialize_managers(tally),
                metadata={
                    "season": tally.season,
                    "fpl_format": tally.fpl_format,
                    "league_id": tally.league_id,
                    "gameweek": tally.through_gameweek,
                    "start_gameweek": tally.start_gameweek,
                    "rule_types": tally.rule_types,
                    "total_fines": tally.total_fines,
                    "qualifiers": tally.qualifiers,
                },
                file=stdout,
            )
            return

        _render_tally(tally)


def _rule_heading(rule_type: str) -> str:
    return _RULE_HEADINGS.get(rule_type, rule_type)


def _serialize_managers(tally: SeasonFinesTally) -> list[dict[str, Any]]:
    """One entry per manager the ledger holds, fined or not.

    Zero-total managers are included on purpose: "recorded and not fined" is
    a fact a consumer needs, and dropping them would leave a caller unable to
    tell them from a manager the ledger has never seen.

    Row shape comes from `serialize_manager_fine_tally`, which `league-recap
    --format json` uses too, so the two commands cannot describe the same
    dataclass differently.
    """
    from fpl_cli.services.league_history_fines import serialize_manager_fine_tally

    return [
        serialize_manager_fine_tally(manager, tally.rule_types)
        for manager in tally.managers
    ]


def _render_tally(tally: SeasonFinesTally) -> None:
    console.print(Panel.fit(
        f"[bold blue]Season Fines - {tally.season} ({tally.fpl_format})[/bold blue]",
    ))

    if not tally.has_records:
        console.print(
            "[yellow]No league history is recorded for this season yet.[/yellow]\n"
            "[dim]Fines are recorded as 'fpl league-recap' runs.[/dim]",
        )
        return

    console.print(
        f"[dim]GW{tally.start_gameweek}-GW{tally.through_gameweek} "
        f"- {tally.total_fines} fine(s) recorded[/dim]\n",
    )

    table = Table(show_header=True, header_style="bold")
    table.add_column("Manager")
    for rule_type in tally.rule_types:
        table.add_column(_rule_heading(rule_type), justify="right")
    table.add_column("Total", justify="right")
    table.add_column("GWs ruled", justify="right")

    for manager in tally.managers:
        table.add_row(*_manager_row(manager, tally))
    console.print(table)

    if tally.qualifiers:
        console.print()
        for line in tally.qualifiers:
            # Escaped for the same reason the table cells are: these lines
            # carry manager names, and Rich reads square brackets in them as
            # markup.
            console.print(f"[dim]{rich_escape(line)}[/dim]")


def _manager_row(manager: ManagerFineTally, tally: SeasonFinesTally) -> list[str]:
    """One table row: per-rule counts, total, and the coverage behind them.

    "GWs ruled" carries an asterisk whenever the manager's own span holds a
    gameweek nothing was ruled in, so a zero total is never read as a clean
    season without the qualifier that explains it.
    """
    ruled = f"{len(manager.ruled_gameweeks)}"
    if not manager.is_fully_ruled:
        ruled += "*"
    counts = [str(manager.counts.get(rule, 0)) for rule in tally.rule_types]
    total = f"[red]{manager.total}[/red]" if manager.total else "0"
    # Escaped: a manager name is user-supplied and Rich reads square brackets
    # in a cell as markup.
    return [rich_escape(manager.manager_name), *counts, total, ruled]
