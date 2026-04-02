"""FPL credentials management via system keyring."""
# Pattern: direct-api

from __future__ import annotations

import click

from fpl_cli.cli._context import console


@click.group("credentials")
def credentials_group():
    """Manage FPL credentials stored in system keyring."""
    pass


@credentials_group.command("set")
def credentials_set():
    """Store FPL email and password in system keyring."""
    import keyring

    email = click.prompt("FPL email")
    password = click.prompt("FPL password", hide_input=True)
    keyring.set_password("fpl-cli", "email", email)
    keyring.set_password("fpl-cli", "password", password)
    console.print("[green]\u2713[/green] Credentials saved to keyring")


@credentials_group.command("clear")
def credentials_clear():
    """Remove FPL credentials from system keyring."""
    import keyring
    from keyring.errors import PasswordDeleteError

    removed = 0
    for key in ("email", "password"):
        try:
            keyring.delete_password("fpl-cli", key)
            removed += 1
        except PasswordDeleteError:
            pass

    if removed:
        console.print(f"[green]\u2713[/green] Removed {removed} credential(s) from keyring")
    else:
        console.print("[yellow]No credentials found in keyring[/yellow]")
