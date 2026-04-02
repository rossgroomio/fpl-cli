#!/usr/bin/env python3
"""Transfer evaluation script for gw-prep sub-agents.

Resolves player names, runs TransferEvalAgent, outputs JSON.
Requires fpl-cli venv to be activated before running.

Usage:
    python transfer_eval.py --out "Palmer" --in "Salah,Mbeumo,Diaz"
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys

from fpl_cli.agents.analysis.transfer_eval import TransferEvalAgent
from fpl_cli.api.fpl import FPLClient
from fpl_cli.models.player import resolve_player


async def _run(out_name: str, in_names: list[str]) -> None:
    async with FPLClient() as client:
        all_players = await client.get_players()
        all_teams = await client.get_teams()

    errors = []

    out_player = resolve_player(out_name, all_players, teams=all_teams)
    if out_player is None:
        errors.append(f"Could not resolve OUT player: '{out_name}'")

    in_players = []
    for name in in_names:
        player = resolve_player(name, all_players, teams=all_teams)
        if player is None:
            errors.append(f"Could not resolve IN player: '{name}'")
        else:
            in_players.append(player)

    if errors:
        json.dump({"error": True, "messages": errors}, sys.stdout, indent=2)
        sys.exit(1)

    assert out_player is not None  # guaranteed by error check above

    # Validate position match
    mismatched = [p for p in in_players if p.position != out_player.position]
    if mismatched:
        names = ", ".join(p.web_name for p in mismatched)
        positions = ", ".join(sorted({p.position_name for p in mismatched}))
        json.dump({
            "error": True,
            "messages": [
                f"Position mismatch: {out_player.web_name} is {out_player.position_name} "
                f"but {names} {'is' if len(mismatched) == 1 else 'are'} {positions}"
            ],
        }, sys.stdout, indent=2)
        sys.exit(1)

    in_ids = [p.id for p in in_players]

    async with TransferEvalAgent() as agent:
        result = await agent.run(context={
            "out_player_id": out_player.id,
            "in_player_ids": in_ids,
        })

    if not result.success:
        json.dump({
            "error": True,
            "messages": result.errors or [result.message],
        }, sys.stdout, indent=2)
        sys.exit(1)

    json.dump(result.data, sys.stdout, indent=2)


def main() -> None:
    parser = argparse.ArgumentParser(description="Transfer evaluation")
    parser.add_argument(
        "--out", required=True,
        help="Player name to transfer out",
    )
    parser.add_argument(
        "--in", dest="in_players", required=True,
        help="Comma-separated player names to evaluate as replacements",
    )
    args = parser.parse_args()

    in_names = [n.strip() for n in args.in_players.split(",") if n.strip()]
    if not in_names:
        json.dump({
            "error": True,
            "messages": ["No IN players provided"],
        }, sys.stdout, indent=2)
        sys.exit(1)

    asyncio.run(_run(args.out, in_names))


if __name__ == "__main__":
    main()
