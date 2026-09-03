#!/usr/bin/env python3
"""Transfer evaluation script for gw-prep sub-agents.

Resolves player names, runs TransferEvalAgent, outputs JSON.
Requires fpl-cli venv to be activated before running.

Usage:
    python transfer_eval.py --out "Palmer" --in "Salah,Mbeumo,Diaz"

Names may carry a club to disambiguate shared surnames, e.g. "Henderson (CRY)".
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys

from _bootstrap import bootstrap_user_dirs

from fpl_cli.agents.analysis.transfer_eval import TransferEvalAgent
from fpl_cli.api.fpl import FPLClient
from fpl_cli.models.player import AmbiguousPlayerError, resolve_player


async def _run(out_name: str, in_names: list[str]) -> None:
    async with FPLClient() as client:
        all_players = await client.get_players()
        all_teams = await client.get_teams()

    errors: list[str] = []

    try:
        out_player = resolve_player(out_name, all_players, teams=all_teams)
        if out_player is None:
            errors.append(f"Could not resolve OUT player: '{out_name}'")
    except AmbiguousPlayerError as e:
        out_player = None
        errors.append(f"Ambiguous OUT player: {e}")

    in_players = []
    for name in in_names:
        try:
            player = resolve_player(name, all_players, teams=all_teams)
        except AmbiguousPlayerError as e:
            errors.append(f"Ambiguous IN player: {e}")
            continue
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
    bootstrap_user_dirs()
    parser = argparse.ArgumentParser(description="Transfer evaluation")
    parser.add_argument(
        "--out", required=True,
        help="Player name to transfer out ('Name (TEAM)' to disambiguate)",
    )
    parser.add_argument(
        "--in", dest="in_players", required=True,
        help="Comma-separated replacement candidates ('Name (TEAM)' to disambiguate)",
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
