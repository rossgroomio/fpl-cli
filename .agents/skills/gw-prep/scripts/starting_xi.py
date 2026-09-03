#!/usr/bin/env python3
"""Starting XI analysis script for gw-prep sub-agents.

Resolves player names, runs StartingXIAgent, outputs JSON.
Requires fpl-cli venv to be activated before running.

Usage:
    python starting_xi.py --squad "Salah,Saka,Palmer,...,Munoz"

Names may carry a club to disambiguate shared surnames, e.g. "Henderson (CRY)".
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys

from _bootstrap import bootstrap_user_dirs
from _resolve import resolve_all

from fpl_cli.agents.analysis.starting_xi import StartingXIAgent
from fpl_cli.api.fpl import FPLClient


async def _run(squad_names: list[str]) -> None:
    async with FPLClient() as client:
        all_players = await client.get_players()
        all_teams = await client.get_teams()

    errors: list[str] = []
    squad = resolve_all(squad_names, all_players, all_teams, label="squad", errors=errors)

    if errors:
        json.dump({"error": True, "messages": errors}, sys.stdout, indent=2)
        sys.exit(1)

    async with StartingXIAgent() as agent:
        result = await agent.run(context={
            "squad": [p.id for p in squad],
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
    parser = argparse.ArgumentParser(description="Starting XI analysis")
    parser.add_argument(
        "--squad", required=True,
        help="Comma-separated squad player names, exactly 15 ('Name (TEAM)' to disambiguate)",
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
