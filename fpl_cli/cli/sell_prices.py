"""FPL sell price scraper command."""
# Pattern: direct-api

from __future__ import annotations

import asyncio
from collections.abc import Callable, Sequence
from contextlib import nullcontext
from datetime import datetime, timezone
from typing import TYPE_CHECKING

import click
import httpx
from rich.markup import escape as rich_escape
from rich.panel import Panel
from rich.table import Table

from fpl_cli.cli._context import Format, console, error_console, get_format
from fpl_cli.cli._json import (
    emit_failure,
    emit_json,
    emit_json_error,
    json_output_mode,
    output_format_option,
)
from fpl_cli.scraper.fpl_prices import TeamFinances, ordered_squad
from fpl_cli.services.sell_prices import AS_SCRAPED, reprice_squad

if TYPE_CHECKING:
    from fpl_cli.models.player import Player

COMMAND = "sell-prices"

# `metadata.warnings` code for rows that carry an id but no cross-game key.
CODE_MISSING = "sell_prices_code_missing"

# Seconds to wait for today's prices. Short: the lookup only refines the
# cached view, which used to need no network at all, and is shown either way.
_PRICE_LOOKUP_TIMEOUT = 10.0

# Chromium error codes a TLS-inspecting proxy rejecting the ClientHello can surface as,
# depending on whether it RSTs the connection or just drops/hangs it.
_PROXY_TLS_ERROR_MARKERS = (
    "ERR_CONNECTION_RESET",
    "ERR_TUNNEL_CONNECTION_FAILED",
    "ERR_SSL_PROTOCOL_ERROR",
    "ERR_CONNECTION_CLOSED",
    "ERR_EMPTY_RESPONSE",
)


@click.command("sell-prices")
@click.option("--refresh", "-r", is_flag=True, help="Force refresh (scrapes FPL website)")
@click.option("--visible", is_flag=True, help="Show browser window (for debugging)")
@output_format_option
@click.pass_context
def sell_prices_command(ctx: click.Context, refresh: bool, visible: bool, output_format: str) -> None:
    """Show squad sell prices and financial breakdown.

    Displays cached sell price data by default. Use --refresh to scrape
    fresh data from the FPL website (requires browser automation and
    FPL credentials).

    First run: playwright install chromium
    Credentials: `fpl credentials set` or FPL_EMAIL/FPL_PASSWORD env vars.
    """
    from fpl_cli.scraper.fpl_prices import FPLPriceScraper, cache_file, load_cache, save_cache

    is_json = output_format == "json"

    # Held open for the whole body rather than just the scrape, so every
    # console.print below lands on stderr under --format json. The manual
    # `sys.stdout = sys.stderr` this replaces covered only the scrape, which
    # left the paths that bail before it writing prose to stdout -- and the
    # ones that bail after it writing no envelope at all (#140, #144).
    with json_output_mode() if is_json else nullcontext():
        if get_format(ctx) == Format.DRAFT:
            emit_failure(
                COMMAND, "sell-prices is not available in draft format.", output_format,
            )

        if not refresh:
            cached = load_cache()
            if not cached:
                emit_failure(
                    COMMAND,
                    "No cached sell-price data. Run with --refresh to scrape it.",
                    output_format,
                )
            if is_json:
                # Before the price lookup, which is wasted on a payload refused anyway.
                _require_player_ids(cached)
            else:
                console.print(Panel.fit("[bold blue]Squad Budget[/bold blue]"))
                if cached.scraped_at:
                    console.print(f"[dim]Data from {_cache_age_str(cached.scraped_at)}[/dim]\n")
            finances, notes, warnings = _at_todays_prices(cached)
            # On stderr under --format json too, as the --refresh path's warnings
            # are: the skills write the envelope to a file and see only this stream.
            for note in notes:
                error_console.print(rich_escape(note), style="dim")
            for warning in warnings:
                error_console.print(rich_escape(f"Warning: {warning['message']}"), style="yellow")
            if is_json:
                _emit_json_finances(finances, warnings)
                return
            _display_finances(finances)
            return

        scraper = FPLPriceScraper()
        console.print("[bold]Scraping FPL transfers page...[/bold]")
        console.print("[dim]This requires browser automation (may take 10-20 seconds)[/dim]\n")

        async def _run() -> TeamFinances | Exception:
            try:
                return await scraper.scrape(headless=not visible)
            except Exception as e:  # noqa: BLE001 — scraper resilience
                return e

        result = asyncio.run(_run())

        if isinstance(result, Exception):
            # Not `emit_failure`, which prints prose or an envelope but never
            # both: the troubleshooting steps are worth printing even when a
            # script is parsing. So both get written -- the reason first, then
            # the steps on stderr, then the envelope on the stdout a consumer
            # is reading.
            message = f"Error scraping FPL: {result}"
            # Escaped: a Playwright timeout embeds the selector it waited on
            # (`locator("[data-test-id='...']")`), which Rich would parse as
            # markup and silently drop -- losing the one detail these
            # troubleshooting lines exist to show (#159 review).
            error_console.print(f"[red]{rich_escape(message)}[/red]")
            error_console.print("\nTroubleshooting:")
            error_console.print("  1. Run: playwright install chromium")
            error_console.print("  2. Check credentials: `fpl credentials set`")
            error_console.print("  3. Try with --visible flag to see browser")
            if any(marker in str(result) for marker in _PROXY_TLS_ERROR_MARKERS):
                error_console.print(
                    "  4. Behind a TLS-inspecting proxy the browser's ClientHello may be"
                    " rejected. Point at an older bundled browser, e.g."
                    " FPL_BROWSER_EXECUTABLE=/path/to/chromium and"
                    ' FPL_BROWSER_ARGS="--disable-features=EncryptedClientHello".'
                )
            if is_json:
                emit_json_error(COMMAND, message, cause=result)
            raise SystemExit(1) from result

        finances = result

        for warning in finances.warnings:
            error_console.print(f"[yellow]Warning: {warning}[/yellow]")

        if finances.is_suspect:
            console.print("\n[bold red]Scrape returned suspect data - likely a failed extraction.[/bold red]")
            existing = load_cache()
            if existing and not existing.is_suspect:
                console.print("[red]Existing cache preserved (not overwritten with bad data).[/red]")
                console.print("[dim]Try with --visible flag to debug the scrape.[/dim]")
            else:
                _save_cache_or_fail(finances, save_cache, output_format)
                error_console.print(
                    f"[yellow]Saved suspect data to {rich_escape(str(cache_file()))}"
                    f" (no valid cache to preserve).[/yellow]"
                )
                console.print("[dim]Try with --visible flag to debug the scrape.[/dim]")

            if is_json:
                # The table below shows these numbers too, but to someone who
                # can see the (Suspect) label and count the rows. A consumer
                # cannot, and the one documented JSON pipeline --
                # `fpl allocate --sell-prices` -- reads `data` and never
                # `metadata.warnings`, so a warning would not reach it. It
                # gets the refusal instead, matching the element_id check
                # below that already errors on a milder defect than this.
                emit_json_error(
                    COMMAND,
                    "Scrape returned suspect data - likely a failed extraction"
                    f" ({len(finances.squad)} players). Re-run with --refresh,"
                    " or --visible to watch the browser.",
                )
            console.print(Panel.fit("[bold blue]Squad Budget (Suspect)[/bold blue]"))
            _display_finances(finances)
            return

        _save_cache_or_fail(finances, save_cache, output_format)

        if is_json:
            _emit_json_finances(finances)
            return

        console.print(Panel.fit("[bold blue]Squad Budget[/bold blue]"))
        _display_finances(finances)
        console.print(f"\n[green]Data saved to {rich_escape(str(cache_file()))}[/green]")


def _save_cache_or_fail(
    finances: TeamFinances, save_cache: Callable[[TeamFinances], None], output_format: str,
) -> None:
    """Persist the scrape, reporting a write failure instead of raising through.

    A read-only data directory or a full disk raised straight out of the
    command, so `--format json` exited 1 with empty stdout and no envelope --
    the failure mode this contract exists to close (#159 review).
    """
    try:
        save_cache(finances)
    except OSError as exc:
        emit_failure(
            COMMAND, f"Could not write the sell-price cache: {exc}", output_format,
            cause=exc,
        )


def _at_todays_prices(
    cached: TeamFinances,
) -> tuple[TeamFinances, list[str], list[dict[str, str]]]:
    """Bring the cached sell prices forward to today's prices (#426).

    Returns the squad to show, a note naming the sell prices that moved, and
    `metadata.warnings` entries for any left as scraped. A failed lookup
    leaves them all as scraped rather than failing the command: the cache is
    still the best figure to hand, and showing it needed no network before.
    """
    from fpl_cli.api.fpl import FPLClient

    async def _run() -> list[Player]:
        async with FPLClient(timeout=_PRICE_LOOKUP_TIMEOUT) as client:
            return await client.get_players()

    try:
        players = asyncio.run(_run())
    # Unreachable, or a reply that is not the player list: a maintenance page
    # fails the JSON decode (ValueError), a body without `elements` raises
    # KeyError (TypeError if it is not an object), and pydantic's
    # ValidationError is a ValueError too.
    except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
        return cached, [], [_lookup_failed_warning(exc)]
    repricing = reprice_squad(cached, players)
    return repricing.finances, repricing.notes(), repricing.warnings()


def _lookup_failed_warning(exc: Exception) -> dict[str, str]:
    """A fixed phrase naming the failure, never its text: an empty timeout
    message and a status error's multi-line advice both read badly here."""
    if isinstance(exc, httpx.HTTPStatusError):
        detail = f"HTTP {exc.response.status_code}"
    else:
        detail = type(exc).__name__
    return {
        "code": AS_SCRAPED,
        "message": (
            f"Could not fetch today's prices ({detail}), so every sell price is as"
            " scraped and may be out of date."
        ),
    }


def _cache_age_str(scraped_at: str) -> str:
    """Format scraped_at timestamp as human-readable age."""
    try:
        ts = datetime.fromisoformat(scraped_at)
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        age = datetime.now(tz=timezone.utc) - ts
        hours = age.total_seconds() / 3600
        if hours < 1:
            return f"{int(age.total_seconds() / 60)}m ago"
        if hours < 24:
            return f"{hours:.1f}h ago"
        return f"{hours / 24:.0f}d ago"
    except (ValueError, TypeError):
        return scraped_at


def _format_pl(value: float) -> str:
    if value > 0:
        return f"[green]+\u00a3{value:.1f}m[/green]"
    if value < 0:
        return f"[red]-\u00a3{abs(value):.1f}m[/red]"
    return "[dim]\u2014[/dim]"


def _require_player_ids(finances: TeamFinances) -> None:
    """Refuse a payload any player is missing an id from (a DOM-fallback scrape)."""
    if any(p.element_id is None for p in finances.squad):
        # No `json_output_mode()` here: the command body holds it open, and
        # both emitters already fall back to the real stdout (#159 review).
        emit_json_error(
            COMMAND,
            "Sell-price data lacks player IDs (scraped via DOM fallback). "
            "Re-run with --refresh to capture IDs from the FPL API.",
        )


def _emit_json_finances(finances: TeamFinances, warnings: Sequence[dict[str, str]] = ()) -> None:
    """Emit sell-prices data as JSON. Errors if any player lacks element_id.

    `sell_price` is the figure to budget on; `scraped_sell_price` is FPL's own
    from the scrape, which differs where a price change since has moved it.
    """
    _require_player_ids(finances)

    squad_data = [
        {
            "id": p.element_id,
            "code": p.code,
            "name": p.name,
            "position": p.position,
            "sell_price": p.sell_price,
            "scraped_sell_price": p.scraped_sell_price,
        }
        for p in ordered_squad(finances.squad)
    ]
    sell_total = sum(p.sell_price for p in finances.squad)
    emit_json(COMMAND, squad_data, metadata={
        "bank": finances.bank,
        "total_sell_value": sell_total,
        "free_transfers": finances.free_transfers,
        "scraped_at": finances.scraped_at,
        "warnings": [*_code_warnings(finances), *warnings],
    })


def _code_warnings(finances: TeamFinances) -> list[dict[str, str]]:
    """Say so when rows lack `code`, rather than hand a joiner nulls with no reason.

    Not an error like a missing id: `fpl allocate --sell-prices` budgets on id
    and sell price alone, so the payload is still good for that. But a
    consumer joining these rows to another command's on `code` would match
    nothing, and only `--refresh` can fill it in (#419).
    """
    missing = sum(1 for p in finances.squad if p.code is None)
    if not missing:
        return []
    return [{
        "code": CODE_MISSING,
        "message": (
            f"{missing} of {len(finances.squad)} players carry no code: the cache was"
            " scraped before codes were recorded, or the scrape could not read the"
            " player list. Re-run with --refresh to fill them in."
        ),
    }]


def _display_finances(finances: TeamFinances) -> None:
    """Display squad financial breakdown with sell prices and P/L."""
    pos_order = {"GKP": 0, "GK": 0, "DEF": 1, "MID": 2, "FWD": 3}
    has_purchase = any(p.purchase_price != 0.0 for p in finances.squad)

    table = Table(show_header=True, header_style="bold", show_footer=True)
    table.add_column("Player", footer="Totals")
    table.add_column("Pos", justify="center")
    if has_purchase:
        table.add_column("Buy", justify="right")
    sell_total = sum(p.sell_price for p in finances.squad)
    table.add_column("Sell", justify="right", footer=f"\u00a3{sell_total:.1f}m")
    if has_purchase:
        table.add_column("P/L", justify="right")

    sorted_squad = sorted(finances.squad, key=lambda p: (pos_order.get(p.position, 9), p.name))

    for player in sorted_squad:
        row: list[str] = [player.name, player.position]
        if has_purchase:
            row.append(f"\u00a3{player.purchase_price:.1f}m")
        row.append(f"\u00a3{player.sell_price:.1f}m")
        if has_purchase:
            row.append(_format_pl(player.profit_loss))
        table.add_row(*row)

    console.print(table)

    available = sell_total + finances.bank
    console.print(f"\n[bold]Selling value:[/bold] \u00a3{sell_total:.1f}m")
    console.print(f"[bold]In the bank:[/bold]    \u00a3{finances.bank:.1f}m")
    console.print(f"[bold]Available:[/bold]       \u00a3{available:.1f}m")
    console.print(f"[bold]Free transfers:[/bold]  {finances.free_transfers}")
