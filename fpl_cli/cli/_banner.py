"""ANSI Shadow banner for fpl init."""

from __future__ import annotations

from fpl_cli.cli._context import console

# Generated from ANSI Shadow FIGlet font, hardcoded for zero dependencies.
_BANNER = """\
███████╗██████╗ ██╗           ██████╗██╗     ██╗
██╔════╝██╔══██╗██║          ██╔════╝██║     ██║
█████╗  ██████╔╝██║    █████╗██║     ██║     ██║
██╔══╝  ██╔═══╝ ██║    ╚════╝██║     ██║     ██║
██║     ██║     ███████╗      ╚██████╗███████╗██║
╚═╝     ╚═╝     ╚══════╝      ╚═════╝╚══════╝╚═╝"""

_BANNER_WIDTH = max(len(line) for line in _BANNER.splitlines())


def show_banner() -> None:
    """Print the banner if the terminal supports it."""
    if not console.is_terminal:
        return
    # +2 for minimum padding so the banner doesn't touch the terminal edge
    if console.width < _BANNER_WIDTH + 2:
        return
    console.print(_BANNER, style="green")
    console.print()
