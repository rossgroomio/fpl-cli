"""Price change analysis command."""
# Pattern: via-agent

from __future__ import annotations

import asyncio

import click
from rich.panel import Panel
from rich.table import Table

from fpl_cli.cli._context import console


@click.command("price-changes")
def price_changes_command():
    """Show price changes and transfer activity."""
    from fpl_cli.agents.data.price import PriceAgent

    async def _run():
        async with PriceAgent() as agent:
            result = await agent.run()

        if not result.success:
            console.print(f"[red]Agent failed: {result.message}[/red]")
            for error in result.errors:
                console.print(f"  [red]{error}[/red]")
            return

        data = result.data
        console.print(Panel.fit("[bold blue]Price Change Analysis[/bold blue]"))

        # Risers this GW
        if data["risers_this_gw"]:
            console.print("\n[bold green]Price Rises This Gameweek:[/bold green]")
            table = Table(show_header=True, header_style="bold")
            table.add_column("Player")
            table.add_column("Team")
            table.add_column("Price", justify="right")
            table.add_column("Change", justify="right")

            for p in data["risers_this_gw"][:10]:
                table.add_row(
                    p["name"],
                    p["team"],
                    f"£{p['current_price']:.1f}m",
                    f"[green]+£{p['change_this_gw']:.1f}m[/green]",
                )
            console.print(table)

        # Fallers this GW
        if data["fallers_this_gw"]:
            console.print("\n[bold red]Price Falls This Gameweek:[/bold red]")
            table = Table(show_header=True, header_style="bold")
            table.add_column("Player")
            table.add_column("Team")
            table.add_column("Price", justify="right")
            table.add_column("Change", justify="right")

            for p in data["fallers_this_gw"][:10]:
                table.add_row(
                    p["name"],
                    p["team"],
                    f"£{p['current_price']:.1f}m",
                    f"[red]£{p['change_this_gw']:.1f}m[/red]",
                )
            console.print(table)

        # Hot transfers in
        console.print("\n[bold]Most Transferred In:[/bold]")
        table = Table(show_header=True, header_style="bold")
        table.add_column("Player")
        table.add_column("Team")
        table.add_column("Transfers In", justify="right")
        table.add_column("Net", justify="right")

        for p in data["hot_transfers_in"][:8]:
            net = p["net_transfers"]
            net_style = "green" if net > 0 else "red"
            table.add_row(
                p["name"],
                p["team"],
                f"{p['transfers_in']:,}",
                f"[{net_style}]{net:+,}[/{net_style}]",
            )
        console.print(table)

        # Hot transfers out
        console.print("\n[bold]Most Transferred Out:[/bold]")
        table = Table(show_header=True, header_style="bold")
        table.add_column("Player")
        table.add_column("Team")
        table.add_column("Transfers Out", justify="right")
        table.add_column("Net", justify="right")

        for p in data["hot_transfers_out"][:8]:
            net = p["net_transfers"]
            net_style = "green" if net > 0 else "red"
            table.add_row(
                p["name"],
                p["team"],
                f"{p['transfers_out']:,}",
                f"[{net_style}]{net:+,}[/{net_style}]",
            )
        console.print(table)

    asyncio.run(_run())
