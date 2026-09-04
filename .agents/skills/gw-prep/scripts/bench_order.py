#!/usr/bin/env python3
"""Bench order analysis script for gw-prep sub-agents.

Resolves player names, runs BenchOrderAgent, outputs JSON.
Runs on the interpreter fpl-cli is installed on (activate its venv first,
or invoke that venv's Python directly).

Usage:
    python bench_order.py --starting "Salah,Saka,..." --bench "Mbeumo,Munoz,..."

Names may carry a club to disambiguate shared surnames, e.g. "Henderson (CRY)".
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from _bootstrap import bootstrap_user_dirs, emit

from fpl_cli.agents.analysis.bench_order import BenchOrderAgent
from fpl_cli.api.fpl import FPLClient
from fpl_cli.cli._json import json_output_mode
from fpl_cli.models.player import resolve_players_or_report


async def _run(starting_names: list[str], bench_names: list[str]) -> None:
    async with FPLClient() as client:
        all_players = await client.get_players()
        all_teams = await client.get_teams()

    errors: list[str] = []
    starting = resolve_players_or_report(
        starting_names, all_players, all_teams, label="starting", errors=errors,
    )
    bench = resolve_players_or_report(
        bench_names, all_players, all_teams, label="bench", errors=errors,
    )

    if errors:
        emit({"error": True, "messages": errors})
        sys.exit(1)

    # The agent logs its progress while it works. `json_output_mode()` sends
    # that to stderr and hands back the real stdout for the payload, so the
    # caller parses JSON from byte 0 rather than stripping prose first (#226).
    with json_output_mode() as stdout:
        async with BenchOrderAgent() as agent:
            result = await agent.run(context={
                "starting_xi": [p.id for p in starting],
                "bench": [p.id for p in bench],
            })

        if not result.success:
            emit({"error": True, "messages": result.errors or [result.message]}, stdout)
            sys.exit(1)

        emit(result.data, stdout)


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
