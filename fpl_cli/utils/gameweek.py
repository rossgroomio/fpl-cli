"""Gameweek-numbering helpers shared across review and recap prompts."""

from __future__ import annotations


def is_opening_gameweek(gameweek: int | None) -> bool:
    """True for GW1, the season opener where transfers, waivers and league
    tables don't exist yet in the same way they do for every later gameweek.
    """
    return gameweek == 1
