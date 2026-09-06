"""Transfer evaluation command."""
# Pattern: via-agent

from __future__ import annotations

import asyncio
import copy
from typing import TYPE_CHECKING

import click
from rich.table import Table

from fpl_cli.cli._context import (
    Format,
    console,
    error_console,
    get_format,
    get_settings,
    print_result_warnings,
)
from fpl_cli.cli._helpers import _fdr_style
from fpl_cli.cli._json import (
    api_failure_boundary,
    emit_json,
    emit_json_error,
    json_output_mode,
    output_format_option,
)
from fpl_cli.utils.text import strip_diacritics

if TYPE_CHECKING:
    from fpl_cli.scraper.fpl_prices import TeamFinances


@click.command("transfer-eval")
@click.option("--out", "out_player", required=True, help="Player to transfer out (name, ID, or 'Name (TEAM)')")
@click.option(
    "--in", "in_players", required=True,
    help="Comma-separated candidates to transfer in (name, ID, or 'Name (TEAM)')",
)
@click.pass_context
@output_format_option
def transfer_eval_command(ctx: click.Context, out_player: str, in_players: str, output_format: str) -> None:
    """Compare transfer OUT player against IN candidates on two scoring horizons.

    Shows Outlook (multi-GW quality) and This GW (lineup impact) deltas
    for each candidate, sorted by Outlook delta descending.

    Players can be specified by name, player ID, or 'Name (TEAM)' to
    disambiguate (e.g. 'João Pedro (CHE)'). All candidates must match the
    OUT player's position.

    \b
    Examples:
      fpl transfer-eval --out Watkins --in "Isak,Havertz,Cunha"
      fpl transfer-eval --out "João Pedro (CHE)" --in "Palmer,Nkunku" --format json
    """
    from fpl_cli.agents.analysis.transfer_eval import TransferEvalAgent
    from fpl_cli.api.fpl import FPLClient
    from fpl_cli.models.player import (
        resolve_player_or_report,
        resolve_players_or_report,
    )
    from fpl_cli.scraper.fpl_prices import load_cache

    fmt = get_format(ctx)
    # Resolved here rather than inside the renderer: the settings live on the
    # context, and reaching back for a second load from a helper with no ctx
    # was the last call site doing that (#219).
    rolling_window = int(get_settings(ctx).get("rolling_window", 5))
    in_names = [n.strip() for n in in_players.split(",") if n.strip()]

    async def _run() -> None:
        async with FPLClient() as client:
            all_players = await client.get_players()
            all_teams = await client.get_teams()

        # Resolve player names to IDs
        errors: list[str] = []
        out_resolved = resolve_player_or_report(
            out_player, all_players, all_teams, label="OUT", errors=errors,
        )
        if out_resolved is None:
            msg = errors[0]
            if output_format == "json":
                emit_json_error("transfer-eval", msg)
            else:
                error_console.print(f"[red]{msg}[/red]")
            raise SystemExit(1)

        in_resolved = resolve_players_or_report(
            in_names, all_players, all_teams, label="IN", errors=errors,
        )

        if errors:
            msg = "; ".join(errors)
            if output_format == "json":
                emit_json_error("transfer-eval", msg)
            else:
                for e in errors:
                    error_console.print(f"[red]{e}[/red]")
            raise SystemExit(1)

        # Validate position match
        mismatched = [p for p in in_resolved if p.position != out_resolved.position]
        if mismatched:
            names = ", ".join(p.web_name for p in mismatched)
            positions = ", ".join(
                sorted({p.position_name for p in mismatched})
            )
            msg = (
                f"Position mismatch: {out_resolved.web_name} is {out_resolved.position_name} "
                f"but {names} {'is' if len(mismatched) == 1 else 'are'} {positions}"
            )
            if output_format == "json":
                emit_json_error("transfer-eval", msg)
            else:
                error_console.print(f"[red]{msg}[/red]")
            raise SystemExit(1)

        # Run agent
        async with TransferEvalAgent() as agent:
            result = await agent.run({
                "out_player_id": out_resolved.id,
                "in_player_ids": [p.id for p in in_resolved],
            })

        if not result.success:
            if output_format == "json":
                emit_json_error("transfer-eval", result.message)
            else:
                error_console.print(f"[red]Agent failed: {result.message}[/red]")
            raise SystemExit(1)

        data = result.data

        # Affordability (Classic only)
        finances = load_cache() if fmt != Format.DRAFT else None
        sell_price = _find_sell_price(finances, out_resolved.web_name) if finances else None

        if output_format == "json":
            _emit_json_output(data, finances, sell_price, fmt)
        else:
            print_result_warnings(data)
            _render_table(data, finances, sell_price, fmt, rolling_window)

    with api_failure_boundary("transfer-eval", output_format):
        if output_format == "json":
            # The whole run, not just the emit: the agent logs its progress
            # while it works, and anything printed before the envelope breaks
            # a consumer's parse at byte 0 (#226). `emit_json`/`emit_json_error`
            # reach the real stdout from inside here without being handed it.
            with json_output_mode():
                asyncio.run(_run())
        else:
            asyncio.run(_run())


def _find_sell_price(finances: TeamFinances | None, out_name: str) -> float | None:
    """Find the sell price of the OUT player from scraper cache."""
    if not finances or not finances.squad:
        return None
    out_norm = strip_diacritics(out_name).lower()
    for sp in finances.squad:
        sp_norm = strip_diacritics(sp.name).lower()
        if sp_norm == out_norm or out_norm in sp_norm:
            return sp.sell_price
    return None


def _compute_budget(finances: TeamFinances | None, sell_price: float | None, in_price: float) -> float | None:
    """Compute budget surplus/deficit for an IN candidate."""
    if finances is None or sell_price is None:
        return None
    return round(finances.bank + sell_price - in_price, 1)


def _emit_json_output(
    data: dict, finances: TeamFinances | None, sell_price: float | None, fmt: Format | None,
) -> None:
    """Emit JSON output with optional affordability fields.

    The early-season quality notice the analysis attaches travels in
    ``metadata.warnings``, the same slot ``fpl stats --value`` uses, so a
    consumer reads one place whichever command produced the score.

    Called from inside the command's `json_output_mode()` block, which is
    what routes the envelope to the real stdout while prose goes to stderr.
    """
    output = copy.deepcopy(data)
    warnings = output.pop("warnings", [])
    out = output["out_player"]
    in_players = output["in_players"]

    if fmt != Format.DRAFT and finances:
        out["sell_price"] = sell_price
        for inp in in_players:
            itb = _compute_budget(finances, sell_price, inp["price"])
            inp["itb"] = itb
            inp["affordable"] = itb >= 0 if itb is not None else None

    emit_json("transfer-eval", output, metadata={"warnings": warnings})


def _render_table(
    data: dict, finances: TeamFinances | None, sell_price: float | None, fmt: Format | None, rolling_window: int,
) -> None:
    """Render Rich table with format-aware columns."""
    show_price = fmt != Format.DRAFT
    has_budget = show_price and finances is not None and sell_price is not None

    # Show transfer budget context above the table
    if has_budget:
        assert finances is not None  # narrowed by has_budget
        bank = finances.bank
        out_name = data["out_player"]["web_name"]
        console.print(
            f"[dim]Bank: £{bank:.1f}m  |  "
            f"Sell {out_name}: £{sell_price:.1f}m  |  "
            f"ITB = Bank + Sell - Buy Price[/dim]"
        )

    table = Table(show_header=True, header_style="bold")
    table.add_column("Player")
    table.add_column("Pos")
    table.add_column("Outlook", justify="right")
    table.add_column("This GW", justify="right")
    table.add_column("Fixtures")
    table.add_column("Form", justify="right")
    table.add_column("Status")
    table.add_column("Avail", justify="right")
    table.add_column("Quality", justify="right")
    if show_price:
        table.add_column("Quality/£m", justify="right")
        table.add_column("Rolling", justify="right")
        table.add_column("Price", justify="right")
    if has_budget:
        table.add_column("ITB", justify="right")

    out = data["out_player"]
    fixtures_str = _format_fixtures(out["fixture_matchups"])
    status_str = _format_status(out["status"], out["chance_of_playing"])

    out_row = [
        f"[bold]{out['web_name']}[/bold] ({out['team_short']})",
        out["position"],
        str(out["outlook"]),
        str(out["this_gw"]),
        fixtures_str,
        f"{out['form']:.1f}",
        status_str,
        _format_reliability(out.get("reliability")),
        _format_quality(out.get("quality_score")),
    ]
    if show_price:
        out_row.append(_format_value(out.get("quality_per_m")))
        out_row.append(_format_rolling(out, rolling_window))
        out_row.append(f"£{out['price']:.1f}m")
    if has_budget:
        out_row.append("-")
    table.add_row(*out_row)

    # Separator
    table.add_section()

    for inp in data["in_players"]:
        fixtures_str = _format_fixtures(inp["fixture_matchups"])
        status_str = _format_status(inp["status"], inp["chance_of_playing"])

        outlook_delta = inp["outlook_delta"]
        gw_delta = inp["gw_delta"]

        row = [
            f"{inp['web_name']} ({inp['team_short']})",
            inp["position"],
            _format_delta(outlook_delta),
            _format_delta(gw_delta),
            fixtures_str,
            f"{inp['form']:.1f}",
            status_str,
            _format_reliability(inp.get("reliability")),
            _format_quality(inp.get("quality_score")),
        ]
        if show_price:
            row.append(_format_value(inp.get("quality_per_m")))
            row.append(_format_rolling(inp, rolling_window))
            row.append(f"£{inp['price']:.1f}m")
        if has_budget:
            budget = _compute_budget(finances, sell_price, inp["price"])
            row.append(_format_budget(budget))
        table.add_row(*row)

    console.print(table)


def _format_reliability(score: float | None) -> str:
    return f"{score:.0%}" if score is not None else "-"


def _format_quality(score: int | None) -> str:
    return str(score) if score is not None else "-"


def _format_value(score: float | None) -> str:
    return f"{score:.1f}" if score is not None else "-"


def _format_rolling(player_data: dict, window: int) -> str:
    rv = player_data.get("rolling_pts_per_m")
    rc = player_data.get("rolling_fixture_count")
    if rv is None:
        return "-"
    suffix = "*" if rc is not None and rc < window else ""
    return f"{rv}{suffix}"


def _format_delta(delta: int) -> str:
    if delta > 0:
        return f"[green]+{delta}[/green]"
    if delta < 0:
        return f"[red]{delta}[/red]"
    return "0"


def _format_budget(budget: float | None) -> str:
    if budget is None:
        return "-"
    if budget >= 0:
        return f"[green]+{budget:.1f}m[/green]"
    return f"[red]{budget:.1f}m[/red]"


def _format_fixtures(matchups: list[dict]) -> str:
    parts = []
    for m in matchups:
        fdr = m["fdr"]
        opp = m["opponent"]
        style = _fdr_style(fdr)
        parts.append(f"[{style}]{opp}({fdr:.0f})[/{style}]")
    return " ".join(parts) if parts else "-"


def _format_status(status: str, chance_of_playing: int | None) -> str:
    if status == "a":
        return "[green]A[/green]"
    if chance_of_playing is not None:
        if chance_of_playing < 50:
            return f"[red]{chance_of_playing}%[/red]"
        if chance_of_playing < 75:
            return f"[yellow]{chance_of_playing}%[/yellow]"
        return f"{chance_of_playing}%"
    return f"[yellow]{status}[/yellow]"
