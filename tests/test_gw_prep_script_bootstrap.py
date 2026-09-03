"""Tests for .agents/skills/gw-prep/scripts/_bootstrap.py — the interpreter guard.

Every gw-prep helper script imports `fpl_cli`, so running one on an
interpreter that lacks the package is the adopter's easiest mistake (issue
#182): a standalone `fpl` on PATH makes `fpl status` work while
`python3 transfer_eval.py` dies. The guard turns that into the JSON error
envelope Phase A3/D1/E already parse instead of a traceback.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import textwrap
from pathlib import Path
from types import ModuleType

import pytest

SCRIPTS_DIR = Path(__file__).parent.parent / ".agents/skills/gw-prep/scripts"

# Every script a phase of SKILL.md invokes directly.
ENTRY_SCRIPTS = [
    "bench_order.py",
    "extract_classic_squad.py",
    "normalise_entities.py",
    "starting_xi.py",
    "transfer_eval.py",
    "validate_draft_waivers.py",
]


def _load_bootstrap() -> ModuleType:
    """Load _bootstrap.py as a module (it's not a package)."""
    spec = importlib.util.spec_from_file_location("gw_prep_bootstrap", SCRIPTS_DIR / "_bootstrap.py")
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_mod = _load_bootstrap()


# ---- Which failures the guard claims ----------------------------------------


@pytest.mark.parametrize("name", ["fpl_cli", "fpl_cli.paths", "fpl_cli.utils.markdown"])
def test_missing_package_is_the_guards(name):
    assert _mod.is_fpl_cli_missing(ModuleNotFoundError(f"No module named '{name}'", name=name))


@pytest.mark.parametrize("name", ["pydantic", "click", "fpl_clique", ""])
def test_missing_dependency_is_not_the_guards(name):
    """A broken install keeps its traceback — naming the interpreter would mislead."""
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
def test_wrong_interpreter_emits_the_error_envelope(script):
    result = _run_without_fpl_cli(script)

    assert result.returncode == 1
    data = json.loads(result.stdout)
    assert data["error"] is True
    assert "ModuleNotFoundError" not in result.stderr


@pytest.mark.parametrize("script", ENTRY_SCRIPTS)
def test_wrong_interpreter_message_names_the_cause(script):
    """The reader gets the package, the interpreter, and the fix, not a stack."""
    messages = json.loads(_run_without_fpl_cli(script).stdout)["messages"]
    joined = " ".join(messages)

    assert "fpl_cli" in joined
    assert sys.executable in joined
    assert "venv" in joined
