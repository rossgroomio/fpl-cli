"""Shared player-name resolution for gw-prep scripts.

All three analysis scripts take player names on the command line and hand
element ids to an agent. They share one contract for turning the former
into the latter: a name that matches nothing, and a name that two players
answer to exactly, are both reported rather than guessed at (issue #180).
One copy of it, so the error wording and the exception handling cannot
drift apart across the three.
"""

from __future__ import annotations

from fpl_cli.models.player import AmbiguousPlayerError, Player, resolve_player
from fpl_cli.models.team import Team


def resolve_one(
    name: str,
    players: list[Player],
    teams: list[Team],
    *,
    label: str,
    errors: list[str],
) -> Player | None:
    """Resolve one name, appending to *errors* and returning None on failure.

    *label* names the role in the message ("bench", "squad", "OUT").
    Passing *teams* is what makes the ``Name (TEAM)`` disambiguator work, so
    a caller holding one of two players who share a surname can say which.
    """
    try:
        player = resolve_player(name, players, teams=teams)
    except AmbiguousPlayerError as exc:
        errors.append(f"Ambiguous {label} player: {exc}")
        return None
    if player is None:
        errors.append(f"Could not resolve {label} player: '{name}'")
    return player


def resolve_all(
    names: list[str],
    players: list[Player],
    teams: list[Team],
    *,
    label: str,
    errors: list[str],
) -> list[Player]:
    """Resolve every name, collecting failures into *errors*.

    Resolves the whole list rather than stopping at the first failure, so a
    run that mistypes two names reports both.
    """
    resolved = [
        resolve_one(name, players, teams, label=label, errors=errors)
        for name in names
    ]
    return [p for p in resolved if p is not None]
