"""FPL sell price scraper command."""
# Pattern: direct-api

from __future__ import annotations

import asyncio
from contextlib import ExitStack
from datetime import datetime, timezone

import click
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
from fpl_cli.scraper.fpl_prices import TeamFinances

COMMAND = "sell-prices"

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
    with ExitStack() as stack:
        if is_json:
            stack.enter_context(json_output_mode())

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
                _emit_json_finances(cached)
                return
            console.print(Panel.fit("[bold blue]Squad Budget[/bold blue]"))
            if cached.scraped_at:
                console.print(f"[dim]Data from {_cache_age_str(cached.scraped_at)}[/dim]\n")
            _display_finances(cached)
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
            # Not `emit_failure`, which picks one channel or the other: the
            # troubleshooting steps are worth printing even when a script is
            # parsing, and inside this block prose is already on stderr. So
            # both get written -- the reason first, then the steps, then the
            # envelope on the stdout a consumer is reading.
            message = f"Error scraping FPL: {result}"
            console.print(f"[red]{message}[/red]")
            console.print("\nTroubleshooting:")
            console.print("  1. Run: playwright install chromium")
            console.print("  2. Check credentials: `fpl credentials set`")
            console.print("  3. Try with --visible flag to see browser")
            if any(marker in str(result) for marker in _PROXY_TLS_ERROR_MARKERS):
                console.print(
                    "  4. Behind a TLS-inspecting proxy the browser's ClientHello may be"
                    " rejected. Point at an older bundled browser, e.g."
                    " FPL_BROWSER_EXECUTABLE=/path/to/chromium and"
                    ' FPL_BROWSER_ARGS="--disable-features=EncryptedClientHello".'
                )
            if is_json:
                emit_json_error(COMMAND, message)
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
                save_cache(finances)
                error_console.print(
                    f"[yellow]Saved suspect data to {rich_escape(str(cache_file()))}"
                    f" (no valid cache to preserve).[/yellow]"
                )
                console.print("[dim]Try with --visible flag to debug the scrape.[/dim]")

            if is_json:
                # Still a success envelope -- the table path shows the numbers
                # too, loudly flagged. A consumer cannot see the flag, so it
                # travels in metadata.warnings rather than being dropped.
                _emit_json_finances(finances, suspect=True)
                return
            console.print(Panel.fit("[bold blue]Squad Budget (Suspect)[/bold blue]"))
            _display_finances(finances)
            return

        save_cache(finances)

        if is_json:
            _emit_json_finances(finances)
            return

        console.print(Panel.fit("[bold blue]Squad Budget[/bold blue]"))
        _display_finances(finances)
        console.print(f"\n[green]Data saved to {rich_escape(str(cache_file()))}[/green]")


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


def _emit_json_finances(finances: TeamFinances, *, suspect: bool = False) -> None:
    """Emit sell-prices data as JSON. Errors if any player lacks element_id.

    *suspect* marks a scrape the extraction heuristics distrust. It stays a
    success envelope, since the table path shows the same numbers, but the
    warning a table reader can see has to reach a consumer some other way.
    """
    if any(p.element_id is None for p in finances.squad):
        with json_output_mode() as stdout:
            emit_json_error(
                COMMAND,
                "Sell-price data lacks player IDs (scraped via DOM fallback). "
                "Re-run with --refresh to capture IDs from the FPL API.",
                file=stdout,
            )

    squad_data = [
        {
            "id": p.element_id,
            "name": p.name,
            "position": p.position,
            "sell_price": p.sell_price,
        }
        for p in finances.squad
    ]
    sell_total = sum(p.sell_price for p in finances.squad)
    metadata: dict = {
        "bank": finances.bank,
        "total_sell_value": sell_total,
        "free_transfers": finances.free_transfers,
        "scraped_at": finances.scraped_at,
    }
    if suspect:
        metadata["warnings"] = [{
            "code": "scrape_suspect",
            "message": (
                f"The scrape extracted {len(finances.squad)} players and the"
                " extraction heuristics distrust the result. Re-run with"
                " --refresh, or --visible to watch the browser."
            ),
        }]
    with json_output_mode() as stdout:
        emit_json(COMMAND, squad_data, metadata=metadata, file=stdout)


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
