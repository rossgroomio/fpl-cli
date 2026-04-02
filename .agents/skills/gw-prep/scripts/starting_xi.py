#!/usr/bin/env python3
"""Starting XI analysis script for gw-prep sub-agents.

Resolves player names, runs StartingXIAgent, outputs JSON.
Requires fpl-cli venv to be activated before running.

Usage:
    python starting_xi.py --squad "Salah,Saka,Palmer,...,Munoz"
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys

from fpl_cli.agents.analysis.starting_xi import StartingXIAgent
from fpl_cli.api.fpl import FPLClient
from fpl_cli.models.player import resolve_player


async def _run(squad_names: list[str]) -> None:
    async with FPLClient() as client:
        all_players = await client.get_players()

    errors = []
    squad_ids = []
    for name in squad_names:
        player = resolve_player(name, all_players)
        if player is None:
            errors.append(f"Could not resolve squad player: '{name}'")
        else:
            squad_ids.append(player.id)

    if errors:
        json.dump({"error": True, "messages": errors}, sys.stdout, indent=2)
        sys.exit(1)

    async with StartingXIAgent() as agent:
        result = await agent.run(context={
            "squad": squad_ids,
        })

    if not result.success:
        json.dump({
            "error": True,
            "messages": result.errors or [result.message],
        }, sys.stdout, indent=2)
        sys.exit(1)

    json.dump(result.data, sys.stdout, indent=2)


def main() -> None:
    parser = argparse.ArgumentParser(description="Starting XI analysis")
    parser.add_argument(
        "--squad", required=True,
        help="Comma-separated squad player names (exactly 15)",
    )
    args = parser.parse_args()

    squad_names = [n.strip() for n in args.squad.split(",") if n.strip()]
    if len(squad_names) != 15:
        json.dump({
            "error": True,
            "messages": [f"Expected exactly 15 squad players, got {len(squad_names)}"],
        }, sys.stdout, indent=2)
        sys.exit(1)

    asyncio.run(_run(squad_names))


if __name__ == "__main__":
    main()
