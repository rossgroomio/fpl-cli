"""An empty `fpl:` block behaves like an absent one, everywhere (#228).

A settings.yaml whose `fpl:` key is present with every ID commented out parses
to `None`, so `settings.get("fpl", {})` returns `None` and the default never
fires. `resolve_format` read it first, in the group callback, so `fpl squad`,
`fpl captain` and `fpl status --format json` alike died with
`AttributeError: 'NoneType' object has no attribute 'get'` -- exit 1, empty
stdout, no envelope for a `--format json` consumer to parse.

`fpl_config()` is the one reader of that block. The AST guard at the bottom is
what keeps it the only one: ruff has no rule that can spot `.get("fpl", {})`,
and a banned-import rule cannot help when the offending code imports nothing.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from click.testing import CliRunner

import fpl_cli
from fpl_cli.cli import main
from fpl_cli.cli._context import fpl_config, resolve_format

# An `fpl:` key whose value is null, plus the toggle the experimental commands
# need to be reachable at all.
NULL_BLOCK = {"fpl": None, "custom_analysis": True}


class TestFplConfig:
    def test_absent_key(self):
        assert fpl_config({}) == {}

    def test_null_block(self):
        assert fpl_config({"fpl": None}) == {}

    def test_populated_block_passes_through(self):
        assert fpl_config({"fpl": {"classic_entry_id": 7}}) == {"classic_entry_id": 7}

    def test_resolve_format_survives_a_null_block(self):
        assert resolve_format({"fpl": None}) is None


def _invoke(argv: list[str]):
    """Run *argv* through the group with a null `fpl:` block and no network."""
    client = MagicMock()
    client.get_players = AsyncMock(return_value=[])
    client.get_teams = AsyncMock(return_value=[])
    client.get_current_gameweek = AsyncMock(return_value={"id": 3})
    client.get_next_gameweek = AsyncMock(return_value={"id": 4})
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)

    with patch("fpl_cli.cli.load_settings", return_value=NULL_BLOCK), \
         patch("fpl_cli.cli._context.load_settings", return_value=NULL_BLOCK), \
         patch("fpl_cli.api.fpl.FPLClient", return_value=client):
        return CliRunner().invoke(main, argv)


# (argv, the fragment the command's own "not configured" message carries)
NULL_BLOCK_COMMANDS = [
    pytest.param(["squad"], "classic_entry_id", id="squad"),
    pytest.param(["squad", "grid"], "classic_entry_id", id="squad-grid"),
    pytest.param(["captain"], "classic_entry_id", id="captain"),
]


@pytest.mark.parametrize("argv,missing", NULL_BLOCK_COMMANDS)
def test_null_block_reaches_the_commands_own_message(argv, missing):
    result = _invoke(argv)
    combined = result.output + result.stderr
    assert "Traceback" not in combined
    assert "NoneType" not in combined
    assert missing in combined


@pytest.mark.parametrize("argv,missing", NULL_BLOCK_COMMANDS)
def test_null_block_still_emits_the_json_envelope(argv, missing):
    result = _invoke([*argv, "--format", "json"])
    assert result.exit_code == 1
    payload = json.loads(result.stdout)
    assert missing in payload["error"]


def test_null_block_lets_status_report_no_configured_format():
    """`fpl status --format json` answers with `format: null`, not a traceback."""
    result = _invoke(["status", "--format", "json"])
    assert result.exit_code == 0, result.output + result.stderr
    payload = json.loads(result.stdout)
    assert payload["metadata"]["format"] is None
    assert payload["data"]["gameweek_info"]["current_gw"] == 3


class TestFplBlockReadThroughHelper:
    """Every read of the `fpl:` block goes through `fpl_config()`."""

    @staticmethod
    def _offences(path: Path) -> list[str]:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        # The helper itself is the one place allowed to touch the raw key.
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == "fpl_config":
                for child in ast.walk(node):
                    setattr(child, "_exempt", True)

        found = []
        for node in ast.walk(tree):
            if getattr(node, "_exempt", False):
                continue
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "get"
                and node.args
                and isinstance(node.args[0], ast.Constant)
                and node.args[0].value == "fpl"
            ):
                found.append(f"{path.name}:{node.lineno} .get(\"fpl\", ...)")
            if (
                isinstance(node, ast.Subscript)
                # Assigning the key is how `fpl init` writes the block back,
                # and never a read that a null value could break.
                and not isinstance(node.ctx, ast.Store)
                and isinstance(node.slice, ast.Constant)
                and node.slice.value == "fpl"
            ):
                found.append(f'{path.name}:{node.lineno} ["fpl"]')
        return found

    def test_no_module_reads_the_raw_key(self):
        root = Path(fpl_cli.__file__).parent
        offences = [o for path in sorted(root.rglob("*.py")) for o in self._offences(path)]
        assert not offences, (
            "read the fpl: block with fpl_config(settings) — a null block is "
            f"otherwise a crash (#228): {offences}"
        )
