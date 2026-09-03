#!/usr/bin/env python3
"""Bench order analysis script for gw-prep sub-agents.

Resolves player names, runs BenchOrderAgent, outputs JSON.
Requires fpl-cli venv to be activated before running.

Usage:
    python bench_order.py --starting "Salah,Saka,..." --bench "Mbeumo,Munoz,..."

Names may carry a club to disambiguate shared surnames, e.g. "Henderson (CRY)".
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys

from _bootstrap import bootstrap_user_dirs

from fpl_cli.agents.analysis.bench_order import BenchOrderAgent
from fpl_cli.api.fpl import FPLClient
from fpl_cli.models.player import AmbiguousPlayerError, Player, resolve_player
from fpl_cli.models.team import Team


def _resolve_ids(
    names: list[str],
    all_players: list[Player],
    all_teams: list[Team],
    label: str,
    errors: list[str],
) -> list[int]:
    """Resolve *names* to element ids, collecting failures into *errors*."""
    ids: list[int] = []
    for name in names:
        try:
            player = resolve_player(name, all_players, teams=all_teams)
        except AmbiguousPlayerError as e:
            errors.append(f"Ambiguous {label} player: {e}")
            continue
        if player is None:
            errors.append(f"Could not resolve {label} player: '{name}'")
        else:
            ids.append(player.id)
    return ids


async def _run(starting_names: list[str], bench_names: list[str]) -> None:
    async with FPLClient() as client:
        all_players = await client.get_players()
        all_teams = await client.get_teams()

    errors: list[str] = []
    starting_ids = _resolve_ids(starting_names, all_players, all_teams, "starting", errors)
    bench_ids = _resolve_ids(bench_names, all_players, all_teams, "bench", errors)

    if errors:
        json.dump({"error": True, "messages": errors}, sys.stdout, indent=2)
        sys.exit(1)

    async with BenchOrderAgent() as agent:
        result = await agent.run(context={
            "starting_xi": starting_ids,
            "bench": bench_ids,
        })

    if not result.success:
        json.dump({
            "error": True,
            "messages": result.errors or [result.message],
        }, sys.stdout, indent=2)
        sys.exit(1)

    json.dump(result.data, sys.stdout, indent=2)


def main() -> None:
    bootstrap_user_dirs()
    parser = argparse.ArgumentParser(description="Bench order analysis")
    parser.add_argument(
        "--starting", required=True,
        help="Comma-separated starting XI player names ('Name (TEAM)' to disambiguate)",
    )
    parser.add_argument(
        "--bench", required=True,
        help="Comma-separated bench player names ('Name (TEAM)' to disambiguate)",
    )
    args = parser.parse_args()

    starting_names = [n.strip() for n in args.starting.split(",") if n.strip()]
    bench_names = [n.strip() for n in args.bench.split(",") if n.strip()]

    asyncio.run(_run(starting_names, bench_names))


if __name__ == "__main__":
    main()
