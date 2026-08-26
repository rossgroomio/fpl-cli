"""Gameweek-numbering helpers shared across review and recap prompts."""

from __future__ import annotations

from collections.abc import Iterable


def is_opening_gameweek(gameweek: int | None) -> bool:
    """True for GW1, the season opener where transfers, waivers and league
    tables don't exist yet in the same way they do for every later gameweek.
    """
    return gameweek == 1


def format_gameweek_list(gameweeks: Iterable[int]) -> str:
    """Render a gameweek list compactly, e.g. "GW1-3, GW7".

    Shared rather than duplicated: the recap's coverage report and the season
    fines tally both name gameweek sets in user-facing prose, and two copies
    would eventually drift onto two different renderings of the same set.
    """
    ordered = sorted(set(gameweeks))
    if not ordered:
        return ""
    runs: list[tuple[int, int]] = [(ordered[0], ordered[0])]
    for gameweek in ordered[1:]:
        start, end = runs[-1]
        if gameweek == end + 1:
            runs[-1] = (start, gameweek)
        else:
            runs.append((gameweek, gameweek))
    return ", ".join(f"GW{s}" if s == e else f"GW{s}-{e}" for s, e in runs)
