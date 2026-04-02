#!/usr/bin/env python3
"""Bench order analysis script for gw-prep sub-agents.

Resolves player names, runs BenchOrderAgent, outputs JSON.
Requires fpl-cli venv to be activated before running.

Usage:
    python bench_order.py --starting "Salah,Saka,..." --bench "Mbeumo,Munoz,..."
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys

from fpl_cli.agents.analysis.bench_order import BenchOrderAgent
from fpl_cli.api.fpl import FPLClient
from fpl_cli.models.player import resolve_player


async def _run(starting_names: list[str], bench_names: list[str]) -> None:
    async with FPLClient() as client:
        all_players = await client.get_players()

    errors = []
    starting_ids = []
    for name in starting_names:
        player = resolve_player(name, all_players)
        if player is None:
            errors.append(f"Could not resolve starting player: '{name}'")
        else:
            starting_ids.append(player.id)

    bench_ids = []
    for name in bench_names:
        player = resolve_player(name, all_players)
        if player is None:
            errors.append(f"Could not resolve bench player: '{name}'")
        else:
            bench_ids.append(player.id)

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
    parser = argparse.ArgumentParser(description="Bench order analysis")
    parser.add_argument(
        "--starting", required=True,
        help="Comma-separated starting XI player names",
    )
    parser.add_argument(
        "--bench", required=True,
        help="Comma-separated bench player names",
    )
    args = parser.parse_args()

    starting_names = [n.strip() for n in args.starting.split(",") if n.strip()]
    bench_names = [n.strip() for n in args.bench.split(",") if n.strip()]

    asyncio.run(_run(starting_names, bench_names))


if __name__ == "__main__":
    main()
