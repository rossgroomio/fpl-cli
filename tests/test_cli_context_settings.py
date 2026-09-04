"""Commands read their settings off the Click context, not by re-loading (#219).

`main()` merges defaults and user overrides once and hands the result to every
command as a `CLIContext`. These tests invoke through the group with only
`fpl_cli.cli.load_settings` patched -- the seam the group itself uses, and no
per-command patch -- so a command that went back to calling `load_settings()`
for itself would sail past the group's settings and fail here.

Each case answers from the settings alone: the ID lookup and its "not
configured" message both land before the command touches the network. That
rules out the experimental commands as probes -- `FormatAwareGroup` resolves
its gate while parsing, before the group callback has put anything on the
context, so they answer from a load either way.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest
from click.testing import CliRunner

CONFIGURED_ID = 4242

# (argv, the settings key the command reads, a fragment of its "missing" message)
COMMANDS = [
    pytest.param(["chips", "sync"], "classic_entry_id", "classic_entry_id", id="chips-sync"),
    pytest.param(["chips", "timing"], "classic_entry_id", "classic_entry_id", id="chips-timing"),
    pytest.param(["squad", "grid"], "classic_entry_id", "classic_entry_id is not set", id="squad-grid"),
    pytest.param(["league-fines"], "classic_league_id", "no classic league id", id="league-fines"),
]


def _invoke(argv: list[str], settings: dict):
    from fpl_cli.cli import main as cli_main

    with patch("fpl_cli.cli.load_settings", return_value=settings):
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
