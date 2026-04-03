"""FPL sell price scraper command."""
# Pattern: direct-api

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import click
from rich.panel import Panel
from rich.table import Table

from fpl_cli.cli._context import Format, console, error_console, get_format
from fpl_cli.cli._json import emit_json, emit_json_error, json_output_mode, output_format_option
from fpl_cli.scraper.fpl_prices import TeamFinances


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
    if get_format(ctx) == Format.DRAFT:
        error_console.print("[yellow]sell-prices is not available in draft format[/yellow]")
        return

    from fpl_cli.scraper.fpl_prices import CACHE_FILE, FPLPriceScraper, load_cache, save_cache

    is_json = output_format == "json"

    if not refresh:
        cached = load_cache()
        if cached:
            if is_json:
                _emit_json_finances(cached)
                return
            console.print(Panel.fit("[bold blue]Squad Budget[/bold blue]"))
            if cached.scraped_at:
                console.print(f"[dim]Data from {_cache_age_str(cached.scraped_at)}[/dim]\n")
            _display_finances(cached)
            return
        else:
            error_console.print("[yellow]No cached data found. Run with --refresh to scrape.[/yellow]")
            return

    import sys

    scraper = FPLPriceScraper()
    original_stdout = sys.stdout

    # When JSON output is requested, redirect all console output to stderr
    # so shell redirection (> file.json) captures only the JSON envelope.
    if is_json:
        sys.stdout = sys.stderr

    try:
        console.print("[bold]Scraping FPL transfers page...[/bold]")
        console.print("[dim]This requires browser automation (may take 10-20 seconds)[/dim]\n")

        async def _run() -> TeamFinances | Exception:
            try:
                return await scraper.scrape(headless=not visible)
            except Exception as e:  # noqa: BLE001 — scraper resilience
                return e

        result = asyncio.run(_run())

        if isinstance(result, Exception):
            console.print(f"[red]Error scraping FPL: {result}[/red]")
            console.print("\nTroubleshooting:")
            console.print("  1. Run: playwright install chromium")
            console.print("  2. Check credentials: `fpl credentials set`")
            console.print("  3. Try with --visible flag to see browser")
            return

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
                    f"[yellow]Saved suspect data to {CACHE_FILE} (no valid cache to preserve).[/yellow]"
                )
                console.print("[dim]Try with --visible flag to debug the scrape.[/dim]")

            console.print(Panel.fit("[bold blue]Squad Budget (Suspect)[/bold blue]"))
            _display_finances(finances)
            return

        save_cache(finances)
    finally:
        sys.stdout = original_stdout

    if is_json:
        _emit_json_finances(finances)
        return

    console.print(Panel.fit("[bold blue]Squad Budget[/bold blue]"))
    _display_finances(finances)
    console.print(f"\n[green]Data saved to {CACHE_FILE}[/green]")


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


def _emit_json_finances(finances: TeamFinances) -> None:
    """Emit sell-prices data as JSON. Errors if any player lacks element_id."""
    if any(p.element_id is None for p in finances.squad):
        with json_output_mode() as stdout:
            emit_json_error(
                "sell-prices",
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
    with json_output_mode() as stdout:
        emit_json("sell-prices", squad_data, metadata={
            "bank": finances.bank,
            "total_sell_value": sell_total,
            "free_transfers": finances.free_transfers,
            "scraped_at": finances.scraped_at,
        }, file=stdout)


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
