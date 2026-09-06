"""A present-but-empty settings block behaves like an absent one (#228).

A settings.yaml key present with every line beneath it commented out parses to
`None`, so `settings.get(key, {})` returns `None` and the default never fires.
`resolve_format` read `fpl:` first, in the group callback, so `fpl squad`,
`fpl captain` and `fpl status --format json` alike died with
`AttributeError: 'NoneType' object has no attribute 'get'` -- exit 1, empty
stdout, no envelope for a `--format json` consumer to parse.

Two mechanisms, because there are two cases. A block the shipped defaults
carry is settled at load: `_deep_merge` ignores the null override and the
defaults survive, which covers `llm:`, `thresholds:` and the rest wherever
they are read. A block the defaults do not carry has nothing to preserve, so
it is read through `settings_block()` -- `USER_ONLY_BLOCKS` names them, and
the AST guard at the bottom keeps that the only way in.
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
from fpl_cli.cli._context import _deep_merge, fpl_config, resolve_format, settings_block
from fpl_cli.paths import SHIPPED_CONFIG_DIR

# The settings blocks the shipped defaults do not carry, so `_deep_merge` has
# no default to preserve and `None` reaches the reader intact. Every other
# block is settled at merge time; these two are why `settings_block` exists.
USER_ONLY_BLOCKS = ("fpl", "reports")

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

    @pytest.mark.parametrize("key", USER_ONLY_BLOCKS)
    def test_settings_block_normalises_any_user_only_block(self, key):
        assert settings_block({key: None}, key) == {}
        assert settings_block({}, key) == {}


class TestDeepMergeNullOverride:
    """A present-but-empty block must not wipe the shipped defaults (#259 review).

    `llm:` with every line beneath it commented out parses to `None`, and
    taking that literally replaced the whole shipped block, so
    `settings.get("llm", {}).get(role)` raised the same AttributeError #228
    reported for `fpl:` -- one block over.
    """

    def test_null_override_keeps_the_shipped_mapping(self):
        base = {"llm": {"research": {"provider": "perplexity"}}}
        _deep_merge(base, {"llm": None})
        assert base["llm"] == {"research": {"provider": "perplexity"}}

    def test_a_real_override_still_merges(self):
        base = {"llm": {"research": {"provider": "perplexity"}, "synthesis": {"provider": "anthropic"}}}
        _deep_merge(base, {"llm": {"research": {"provider": "openai"}}})
        assert base["llm"]["research"]["provider"] == "openai"
        assert base["llm"]["synthesis"]["provider"] == "anthropic"

    def test_a_null_override_for_an_unshipped_key_is_still_none(self):
        """Nothing to preserve, which is exactly why `settings_block` exists."""
        base = {}
        _deep_merge(base, {"fpl": None})
        assert base["fpl"] is None
        assert settings_block(base, "fpl") == {}

    def test_every_shipped_mapping_survives_being_emptied(self):
        """The claim `settings_block`'s docstring makes, checked against the file."""
        import yaml

        defaults = yaml.safe_load((SHIPPED_CONFIG_DIR / "defaults.yaml").read_text())
        blocks = [k for k, v in defaults.items() if isinstance(v, dict)]
        assert blocks, "defaults.yaml carries no mapping blocks -- has it moved?"

        merged = dict(defaults)
        _deep_merge(merged, dict.fromkeys(blocks))
        for key in blocks:
            assert merged[key] == defaults[key], key

    @pytest.mark.parametrize("key", USER_ONLY_BLOCKS)
    def test_user_only_blocks_really_have_no_shipped_default(self, key):
        """If one gains a default, `_deep_merge` covers it and the guard can relax."""
        import yaml

        defaults = yaml.safe_load((SHIPPED_CONFIG_DIR / "defaults.yaml").read_text())
        assert key not in defaults


def _invoke(argv: list[str]):
    """Run *argv* through the group with a null `fpl:` block and no network."""
    client = MagicMock()
    client.get_players = AsyncMock(return_value=[])
    client.get_teams = AsyncMock(return_value=[])
    client.get_current_gameweek = AsyncMock(return_value={"id": 3})
    client.get_next_gameweek = AsyncMock(return_value={"id": 4})
    client.get_season_year = AsyncMock(return_value=2026)
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


class TestUserOnlyBlocksReadThroughHelper:
    """Every read of a user-only block goes through `settings_block()`.

    Only these blocks need it -- `_deep_merge` settles the shipped ones -- and
    only a test can enforce it: ruff has no rule that can spot
    `.get("fpl", {})`, and a banned-import rule cannot help when the offending
    code imports nothing.
    """

    @staticmethod
    def _offences(path: Path) -> list[str]:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        # The helper itself is the one place allowed to touch the raw key.
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name in ("fpl_config", "settings_block"):
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
                and node.args[0].value in USER_ONLY_BLOCKS
            ):
                found.append(f'{path.name}:{node.lineno} .get("{node.args[0].value}", ...)')
            if (
                isinstance(node, ast.Subscript)
                # Assigning the key is how `fpl init` writes the block back,
                # and never a read that a null value could break.
                and not isinstance(node.ctx, ast.Store)
                and isinstance(node.slice, ast.Constant)
                and node.slice.value in USER_ONLY_BLOCKS
            ):
                found.append(f'{path.name}:{node.lineno} ["{node.slice.value}"]')
        return found

    def test_no_module_reads_the_raw_key(self):
        root = Path(fpl_cli.__file__).parent
        offences = [o for path in sorted(root.rglob("*.py")) for o in self._offences(path)]
        assert not offences, (
            "read these blocks with settings_block(settings, key) (or fpl_config) — "
            "the shipped defaults do not carry them, so a null block is otherwise "
            f"a crash (#228): {offences}"
        )
