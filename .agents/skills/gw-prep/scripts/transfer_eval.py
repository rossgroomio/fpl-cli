#!/usr/bin/env python3
"""Transfer evaluation script for gw-prep sub-agents.

Resolves player names, runs TransferEvalAgent, outputs JSON.
Runs on the interpreter fpl-cli is installed on (activate its venv first,
or invoke that venv's Python directly).

Usage:
    python transfer_eval.py --out "Palmer" --in "Salah,Mbeumo,Diaz"

Names may carry a club to disambiguate shared surnames, e.g. "Henderson (CRY)".
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from _bootstrap import bootstrap_user_dirs, emit

from fpl_cli.agents.analysis.transfer_eval import TransferEvalAgent
from fpl_cli.api.fpl import FPLClient
from fpl_cli.cli._json import json_output_mode
from fpl_cli.models.player import resolve_player_or_report, resolve_players_or_report


async def _run(out_name: str, in_names: list[str]) -> None:
    async with FPLClient() as client:
        all_players = await client.get_players()
        all_teams = await client.get_teams()

    errors: list[str] = []
    out_player = resolve_player_or_report(
        out_name, all_players, all_teams, label="OUT", errors=errors,
    )
    in_players = resolve_players_or_report(
        in_names, all_players, all_teams, label="IN", errors=errors,
    )

    if errors:
        emit({"error": True, "messages": errors})
        sys.exit(1)

    assert out_player is not None  # guaranteed by error check above

    # Validate position match
    mismatched = [p for p in in_players if p.position != out_player.position]
    if mismatched:
        names = ", ".join(p.web_name for p in mismatched)
        positions = ", ".join(sorted({p.position_name for p in mismatched}))
        emit({
            "error": True,
            "messages": [
                f"Position mismatch: {out_player.web_name} is {out_player.position_name} "
                f"but {names} {'is' if len(mismatched) == 1 else 'are'} {positions}"
            ],
        })
        sys.exit(1)

    in_ids = [p.id for p in in_players]

    # The agent logs its progress while it works. `json_output_mode()` sends
    # that to stderr and hands back the real stdout for the payload, so the
    # caller parses JSON from byte 0 rather than stripping prose first (#226).
    with json_output_mode() as stdout:
        async with TransferEvalAgent() as agent:
            result = await agent.run(context={
                "out_player_id": out_player.id,
                "in_player_ids": in_ids,
            })

        if not result.success:
            emit({"error": True, "messages": result.errors or [result.message]}, stdout)
            sys.exit(1)

        emit(result.data, stdout)


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
        emit({"error": True, "messages": ["No IN players provided"]})
        sys.exit(1)

    asyncio.run(_run(args.out, in_names))


if __name__ == "__main__":
    main()
