"""Commands read their settings off the Click context, not by re-loading (#219).

`main()` merges defaults and user overrides once and hands the result to every
command as a `CLIContext`. These tests invoke through the group and patch no
command -- only the loaders the group itself reaches for -- so a command that
went back to calling `load_settings()` for itself would sail past the group's
settings and fail here.

Each case answers from the settings alone: the ID lookup and its "not
configured" message both land before the command touches the network.

`FormatAwareGroup` resolves its experimental gate while parsing, before the
group callback has put anything on the context, so a gated command like
`fpl waivers` never reaches its settings on the group patch alone. Opening
that gate takes a second patch, on the loader the gate itself falls back to
-- deliberately carrying the toggle and no IDs, so a command that went back
to loading its own settings still finds nothing and fails the check.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest
from click.testing import CliRunner

CONFIGURED_ID = 4242

# Opens the experimental gate and nothing else -- no `fpl` block, so a command
# reading this instead of the context finds no ID and reports it missing.
GATE_ONLY = {"custom_analysis": True}

# (argv, the settings key the command reads, a fragment of its "missing" message)
COMMANDS = [
    pytest.param(["chips", "sync"], "classic_entry_id", "classic_entry_id", id="chips-sync"),
    pytest.param(["chips", "timing"], "classic_entry_id", "classic_entry_id", id="chips-timing"),
    pytest.param(["squad", "grid"], "classic_entry_id", "classic_entry_id is not set", id="squad-grid"),
    pytest.param(["league-fines"], "classic_league_id", "no classic league id", id="league-fines"),
    pytest.param(["waivers"], "draft_league_id", "no draft_league_id configured", id="waivers"),
]


def _invoke(argv: list[str], settings: dict):
    from fpl_cli.cli import main as cli_main

    with (
        patch("fpl_cli.cli.load_settings", return_value=settings),
        patch("fpl_cli.cli._context.load_settings", return_value=GATE_ONLY),
    ):
        return CliRunner().invoke(cli_main, argv)


@pytest.mark.parametrize("argv,key,missing", COMMANDS)
def test_command_reports_the_id_the_group_settings_lack(argv, key, missing):
    result = _invoke(argv, {"fpl": {}})
    assert missing.lower() in (result.output + result.stderr).lower()


@pytest.mark.parametrize("argv,key,missing", COMMANDS)
def test_command_accepts_the_id_the_group_settings_carry(argv, key, missing):
    """With the ID present the command moves on -- to the blocked network, which is fine here."""
    result = _invoke(argv, {"fpl": {key: CONFIGURED_ID}})
    assert missing.lower() not in (result.output + result.stderr).lower()
