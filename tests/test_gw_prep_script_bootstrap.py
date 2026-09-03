"""Tests for .agents/skills/gw-prep/scripts/_bootstrap.py — the interpreter guard.

Every gw-prep helper script imports `fpl_cli`, so running one on an
interpreter that lacks the package is the adopter's easiest mistake (issue
#182): a standalone `fpl` on PATH makes `fpl status` work while
`python3 transfer_eval.py` dies. The guard turns that into the JSON error
envelope Phase A3/D1/E already parse instead of a traceback.
"""

from __future__ import annotations

import json
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from tests.conftest import load_gw_prep_script

SCRIPTS_DIR = Path(__file__).parent.parent / ".agents/skills/gw-prep/scripts"

# Discovered, not hand-listed: a script added later is covered the day it
# lands. Hand-maintaining this would let a 7th script ship without the guard
# and without a failing test — the regression this suite exists to catch.
# Underscore-prefixed modules are shared internals, not phase entry points.
ENTRY_SCRIPTS = sorted(p.name for p in SCRIPTS_DIR.glob("*.py") if not p.name.startswith("_"))

_mod = load_gw_prep_script("_bootstrap.py")


def test_the_inventory_was_actually_discovered():
    """Guard the guard: an empty glob would make every test below vacuous."""
    assert len(ENTRY_SCRIPTS) >= 6


# ---- Which failures the guard claims ----------------------------------------


def test_absent_package_is_the_guards():
    """Python names the first component it cannot find, so this is the whole set."""
    assert _mod.is_fpl_cli_missing(ModuleNotFoundError("No module named 'fpl_cli'", name="fpl_cli"))


@pytest.mark.parametrize(
    "name",
    [
        "fpl_cli.paths",           # corrupt install, right interpreter
        "fpl_cli.utils.markdown",  # ditto, deeper
        "pydantic",                # missing dependency
        "click",
        "fpl_clique",              # merely shares a prefix
        "",
    ],
)
def test_everything_else_keeps_its_traceback(name):
    """Only a wrong interpreter earns the envelope; the rest would misdiagnose.

    A missing submodule of an importable fpl_cli is on the *correct*
    interpreter, so telling its reader to activate a venv sends them
    somewhere useless — the traceback names the actual missing file.
    """
    assert not _mod.is_fpl_cli_missing(ModuleNotFoundError(f"No module named '{name}'", name=name))


def test_nameless_exception_is_not_the_guards():
    assert not _mod.is_fpl_cli_missing(ModuleNotFoundError("no name attribute"))


# ---- End to end: a real run on an interpreter without fpl_cli ---------------


def _run_without_fpl_cli(script: str) -> subprocess.CompletedProcess[str]:
    """Run `script` in a subprocess where importing fpl_cli raises.

    A meta-path finder standing in for the missing package reproduces the
    adopter's interpreter without needing one, and `runpy` keeps the script's
    own directory on sys.path exactly as `python3 <script>` would.
    """
    harness = textwrap.dedent(
        f"""
        import runpy, sys

        class _NoFplCli:
            def find_spec(self, name, path=None, target=None):
                if name == "fpl_cli" or name.startswith("fpl_cli."):
                    raise ModuleNotFoundError(f"No module named {{name!r}}", name=name)
                return None

        sys.meta_path.insert(0, _NoFplCli())
        sys.path.insert(0, {str(SCRIPTS_DIR)!r})
        runpy.run_path({str(SCRIPTS_DIR)!r} + "/{script}", run_name="__main__")
        """
    )
    return subprocess.run(
        [sys.executable, "-c", harness],
        capture_output=True,
        text=True,
    )


@pytest.mark.parametrize("script", ENTRY_SCRIPTS)
def test_wrong_interpreter_reports_itself(script):
    """One interpreter start per script, then every assertion on that result.

    The reader gets the package, the interpreter and the fix, not a stack.
    """
    result = _run_without_fpl_cli(script)

    assert result.returncode == 1
    assert "ModuleNotFoundError" not in result.stderr

    data = json.loads(result.stdout)
    assert data["error"] is True

    joined = " ".join(data["messages"])
    assert "fpl_cli" in joined
    assert sys.executable in joined
    assert "venv" in joined
