#!/usr/bin/env python3
"""Starting XI analysis script for gw-prep sub-agents.

Resolves player names, runs StartingXIAgent, outputs JSON.
Runs on the interpreter fpl-cli is installed on (activate its venv first,
or invoke that venv's Python directly).

Usage:
    python starting_xi.py --squad "Salah,Saka,Palmer,...,Munoz"

Names may carry a club to disambiguate shared surnames, e.g. "Henderson (CRY)".
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from _bootstrap import bootstrap_user_dirs, emit

from fpl_cli.agents.analysis.starting_xi import StartingXIAgent
from fpl_cli.api.fpl import FPLClient
from fpl_cli.cli._json import json_output_mode
from fpl_cli.models.player import resolve_players_or_report


async def _run(squad_names: list[str]) -> None:
    async with FPLClient() as client:
        all_players = await client.get_players()
        all_teams = await client.get_teams()

    errors: list[str] = []
    squad = resolve_players_or_report(
        squad_names, all_players, all_teams, label="squad", errors=errors,
    )

    if errors:
        emit({"error": True, "messages": errors})
        sys.exit(1)

    # The agent logs its progress while it works. `json_output_mode()` sends
    # that to stderr and hands back the real stdout for the payload, so the
    # caller parses JSON from byte 0 rather than stripping prose first (#226).
    with json_output_mode() as stdout:
        async with StartingXIAgent() as agent:
            result = await agent.run(context={
                "squad": [p.id for p in squad],
            })

        if not result.success:
            emit({"error": True, "messages": result.errors or [result.message]}, stdout)
            sys.exit(1)

        emit(result.data, stdout)


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
        emit({
            "error": True,
            "messages": [f"Expected exactly 15 squad players, got {len(squad_names)}"],
        })
        sys.exit(1)

    asyncio.run(_run(squad_names))


if __name__ == "__main__":
    main()
