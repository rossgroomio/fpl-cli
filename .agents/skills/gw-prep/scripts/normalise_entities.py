#!/usr/bin/env python3
"""Repair HTML-escaped text in a report assembled from sub-agent output.

A sub-agent's returned section can arrive HTML-escaped somewhere in the return
path, and an orchestrator that concatenates it verbatim writes the entities
straight into the report -- a `&gt;` blockquote marker then renders as literal
text instead of opening a quote block. This rewrites the file in place,
decoding the five HTML-special characters, and reports any entity reference it
deliberately left alone so a shape it does not recognise is visible at the
point of writing.

Emits JSON to stdout: {"ok": bool, "changed": bool, "unescaped": int,
"residual": [{"line": int, "entity": str}]}. `ok` is false only when residual
entities remain; the caller warns rather than blocking. Exit code is 0 unless
the startup guard fires, or the file cannot be read, decoded as UTF-8, or
written -- and those exit 1 with {"error": true, "messages": [...]}, never a
traceback, because every caller parses stdout to decide what to say.

Runs on the interpreter fpl-cli is installed on (activate its venv first,
or invoke that venv's Python directly).

Usage:
    python3 normalise_entities.py --file path/to/gw34-recommendations.md
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import TypedDict

# Imported first and for its side effect: _bootstrap reaches for the fpl_cli
# package on its own, turning a wrong interpreter into this script's JSON error
# envelope instead of a ModuleNotFoundError traceback.
import _bootstrap  # noqa: F401 — import guard, see module docstring

from fpl_cli.utils.markdown import find_entities, unescape_specials


class Residual(TypedDict):
    line: int
    entity: str


class NormaliseResult(TypedDict):
    ok: bool
    changed: bool
    unescaped: int
    residual: list[Residual]


def _fail(message: str) -> None:
    json.dump({"error": True, "messages": [message]}, sys.stdout, indent=2)
    sys.exit(1)


def _run(file_path: str) -> None:
    path = Path(file_path)
    try:
        original = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        _fail(f"File not found: {file_path}")
        return
    except OSError as exc:
        _fail(f"Could not read {file_path}: {exc}")
        return
    except UnicodeDecodeError as exc:
        # Not an OSError, so it needs catching separately -- and it is exactly
        # the kind of transit damage this script exists for, which makes a
        # traceback the worst possible response: every caller parses stdout as
        # JSON to decide whether to warn.
        _fail(f"{file_path} is not valid UTF-8, so it cannot be normalised: {exc}")
        return

    normalised = unescape_specials(original)
    changed = normalised != original

    if changed:
        try:
            path.write_text(normalised, encoding="utf-8")
        except OSError as exc:
            _fail(f"Could not write {file_path}: {exc}")
            return

    residual: list[Residual] = [
        {"line": number, "entity": entity}
        for number, entity in find_entities(normalised)
    ]
    result: NormaliseResult = {
        "ok": not residual,
        "changed": changed,
        # Characters recovered, not entities replaced: decoding runs to a fixed
        # point, so the length difference is the honest measure of the rewrite.
        "unescaped": len(original) - len(normalised),
        "residual": residual,
    }
    json.dump(result, sys.stdout, indent=2)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Decode HTML-escaped text in an assembled report, in place.",
    )
    parser.add_argument(
        "--file",
        required=True,
        metavar="PATH",
        help="Path to the markdown report to normalise.",
    )
    args = parser.parse_args()
    _run(args.file)


if __name__ == "__main__":
    main()
