#!/usr/bin/env python3
"""Join the returnee radar to the draft waiver pool on `code`.

Reads `fpl returnees --format json` and `fpl waivers --format json` and emits
the radar narrowed to the tracked returnees a draft manager can actually
claim: those whose `code` matches a row in the waivers `data.pool`.

This is the one step of the Draft stash path that used to be left to the
sub-agent reading two payloads, and when it did the two had no key in common
(#418): the radar's `id` is the main game's element id, the pool's is the
draft's, and a player added mid-season holds a different one in each. Done
here the join is deterministic and testable, and the sub-agent receives a
list it cannot mis-read.

Emits JSON to stdout:
  {"ok": bool,
   "metadata": {...the radar's, plus "pool_size"...},
   "entries": [...the claimable radar entries, unchanged...],
   "excluded": [{"web_name": ..., "code": ..., "reason": "owned" | "no-code"}],
   "warnings": [{"type": ...}]}

`ok` is false only when the join could not run -- no usable `data.pool`, or a
pool whose rows carry no `code` (an older fpl-cli) -- and `entries` is then
empty: a join that cannot run offers nothing to claim rather than everything,
so the stash path fails closed. Exit code is 0 whatever the join finds; the
startup guard in `_bootstrap` is the one non-zero exit, and reports itself as
{"error": true, "messages": [...]}.

Runs on the interpreter fpl-cli is installed on (activate its venv first,
or invoke that venv's Python directly).

Usage:
    python3 claimable_returnees.py \\
        --returnees-json /tmp/returnees.json \\
        --waivers-json /tmp/waivers.json
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any, TypedDict

# Imported first and for its side effect: _bootstrap reaches for the fpl_cli
# package on its own, turning a wrong interpreter into this script's JSON error
# envelope instead of a ModuleNotFoundError traceback.
import _bootstrap  # noqa: F401 — import guard, see module docstring


class Excluded(TypedDict):
    web_name: str
    code: int | None
    reason: str


class Warning(TypedDict):
    type: str


class JoinResult(TypedDict):
    ok: bool
    metadata: dict[str, Any]
    entries: list[dict[str, Any]]
    excluded: list[Excluded]
    warnings: list[Warning]


def _as_code(value: object) -> int | None:
    """A usable `code`, or None: the join never matches on a missing key."""
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else None


def pool_codes(waivers: dict[str, Any]) -> tuple[set[int] | None, Warning | None]:
    """The `code` of every row in the waivers `data.pool`, or None with the reason.

    None means the join cannot run: there is no pool list to read, or there
    is one and not a row of it carries `code`, which is what `fpl waivers`
    emitted before #418. An empty pool is readable and matches nobody.
    """
    data = waivers.get("data")
    pool = data.get("pool") if isinstance(data, dict) else None
    if not isinstance(pool, list):
        return None, Warning(type="pool-unreadable")
    rows = [row for row in pool if isinstance(row, dict)]
    codes = {code for row in rows if (code := _as_code(row.get("code"))) is not None}
    if rows and not codes:
        return None, Warning(type="pool-without-code")
    return codes, None


def radar_entries(radar: dict[str, Any]) -> tuple[list[dict[str, Any]], Warning | None]:
    """The radar's `data.entries`, or an empty list with the reason."""
    data = radar.get("data")
    entries = data.get("entries") if isinstance(data, dict) else None
    if not isinstance(entries, list):
        return [], Warning(type="radar-unreadable")
    return [entry for entry in entries if isinstance(entry, dict)], None


def join(radar: dict[str, Any], waivers: dict[str, Any]) -> JoinResult:
    """Narrow the radar to the entries whose `code` is in the waiver pool."""
    metadata = radar.get("metadata")
    result: JoinResult = {
        "ok": True,
        "metadata": dict(metadata) if isinstance(metadata, dict) else {},
        "entries": [],
        "excluded": [],
        "warnings": [],
    }

    entries, radar_warning = radar_entries(radar)
    if radar_warning is not None:
        result["warnings"].append(radar_warning)
    codes, pool_warning = pool_codes(waivers)
    if codes is None:
        # Nothing to claim rather than everything: the stash path fails closed.
        result["ok"] = False
        if pool_warning is not None:
            result["warnings"].append(pool_warning)
        return result
    result["metadata"]["pool_size"] = len(codes)

    for entry in entries:
        code = _as_code(entry.get("code"))
        web_name = str(entry.get("web_name", ""))
        if code is None:
            result["excluded"].append(Excluded(web_name=web_name, code=None, reason="no-code"))
        elif code in codes:
            result["entries"].append(entry)
        else:
            result["excluded"].append(Excluded(web_name=web_name, code=code, reason="owned"))
    return result


def _run(returnees_json: str, waivers_json: str) -> None:
    with open(returnees_json, encoding="utf-8") as f:
        radar = json.load(f)
    with open(waivers_json, encoding="utf-8") as f:
        waivers = json.load(f)
    if not isinstance(radar, dict) or not isinstance(waivers, dict):
        # Neither file is an envelope at all; say so in the same shape rather
        # than raise, since the orchestrator parses stdout either way.
        json.dump(
            JoinResult(ok=False, metadata={}, entries=[], excluded=[], warnings=[Warning(type="envelope-unreadable")]),
            sys.stdout,
        )
        return
    json.dump(join(radar, waivers), sys.stdout)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Narrow the returnee radar to the players still in the draft waiver pool, joined on code.",
    )
    parser.add_argument(
        "--returnees-json",
        required=True,
        metavar="PATH",
        dest="returnees_json",
        help="Path to fpl returnees --format json output.",
    )
    parser.add_argument(
        "--waivers-json",
        required=True,
        metavar="PATH",
        dest="waivers_json",
        help="Path to fpl waivers --format json output.",
    )
    args = parser.parse_args()
    _run(args.returnees_json, args.waivers_json)


if __name__ == "__main__":
    main()
