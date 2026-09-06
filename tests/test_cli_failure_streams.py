"""Table mode says why it is exiting 1 on stderr, whatever the command (#162).

`--format json` has had a written contract since #140: both envelopes on
stdout, every human-readable line on stderr. Table mode had none, so the same
kind of message -- a command explaining why it produced nothing -- landed
wherever each call site happened to put it. `fpl squad grid` reported a
missing entry ID on stdout while `fpl squad sell-prices`, one subcommand
along, reported a missing cache on stderr; `fpl player` used both, depending
on which way the lookup failed. `2>/dev/null` is the ordinary way to quieten
a CLI, and it discarded the reason for some commands and not others.

The rule is now the Unix one: stdout carries the output that was asked for,
stderr carries the reason there is none. This module tests the rule rather
than the commands -- the tree walk covers a command the day it is registered,
and the cases below it cover the failure paths an outage cannot reach.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import yaml
from click.testing import CliRunner

from fpl_cli.cli import main
from fpl_cli.models.player import PlayerPosition
from tests.conftest import make_agent, make_player, make_team

# The same commands the JSON contract walks -- every command with a `--format`
# flag, and so every command that has promised its reader a stream at all.
# Imported rather than re-derived: one walker, two formats, no drift between
# what the two contracts are held to. The outage they are both driven down is
# the `offline` fixture in `conftest.py`, shared for the same reason.
from tests.test_cli_json_contract import JSON_COMMANDS, REQUIRED_PARAMS


def _args_for(command: tuple[str, ...]) -> list[str]:
    """The JSON contract's arguments without `--format json`: table mode."""
    return list(command) + REQUIRED_PARAMS.get(" ".join(command), [])


@pytest.mark.parametrize(
    "command", JSON_COMMANDS, ids=[" ".join(c) or "(root)" for c in JSON_COMMANDS],
)
def test_a_failing_command_reports_on_stderr_and_leaves_stdout_clean(command, offline):
    """Exit 1 with an empty upstream: the reason is on stderr, stdout is bare.

    Nothing here has produced any output to interleave with -- the API is
    unreachable and no entry IDs are configured, so every one of these
    commands fails before it prints a row. Anything on stdout is therefore
    failure prose that belongs on the other stream. A command that
    legitimately renders part of its output before failing is a different
    case and needs its own test, not a loosening of this one.

    A command that does not exit 1 here is skipped rather than failed, which
    is the walk's blind spot -- and the one that let three commands exit 0 in
    table mode while the envelope beside them exited 1 (#286).
    `test_cli_failure_parity.py` closes it by comparing the two codes rather
    than asserting either, so a command that quietly stops exiting 1 here is
    caught there. What stays skipped is what lies past an early exit:
    `chips timing` stops at its missing entry ID, so its agent-failure print
    is pinned in `test_cli_chips.py` instead, where the harness that gets
    past that already lives (#251 review).
    """
    args = _args_for(command)
    result = CliRunner().invoke(main, args)

    if result.exit_code != 1:
        return

    assert result.stderr.strip(), (
        f"`fpl {' '.join(args)}` exited 1 without saying why on stderr"
    )
    assert result.stdout == "", (
        f"`fpl {' '.join(args)}` exited 1 with prose on stdout: "
        f"{result.stdout[:200]!r} -- table-mode failures go to stderr (#162)"
    )


@pytest.mark.parametrize(
    "command", JSON_COMMANDS, ids=[" ".join(c) or "(root)" for c in JSON_COMMANDS],
)
def test_a_malformed_settings_block_is_prose_and_not_a_traceback(command, malformed_fines):
    """The table-mode half of #170.

    A `ConfigError` that escapes click is on the right stream and in the
    wrong form: 37 lines of Python that name the file and line the parser
    raised at, with the sentence the user needs -- which rule, and the valid
    set -- as the last of them. The rule for table mode is one red line
    saying why, the same as every other reason a command gives up.

    stdout is not asserted empty, unlike the outage walk above: `fpl status`
    prints the gameweek and the next deadline before it reads the fines
    block, and that is output it was asked for, not failure prose.
    """
    args = _args_for(command)
    result = CliRunner().invoke(main, args)

    if result.exception is not None and not isinstance(result.exception, SystemExit):
        raise AssertionError(
            f"`fpl {' '.join(args)}` raised {result.exception!r} instead of reporting "
            f"the failure as prose"
        ) from result.exception

    if result.exit_code != 1:
        return

    assert result.stderr.strip(), (
        f"`fpl {' '.join(args)}` exited 1 without saying why on stderr"
    )


def test_the_two_squad_subcommands_report_on_the_same_stream(offline):
    """The pair from #162: same group, same missing prerequisite, one stream."""
    grid = CliRunner().invoke(main, ["squad", "grid"])
    sell_prices = CliRunner().invoke(main, ["squad", "sell-prices"])

    assert grid.exit_code == sell_prices.exit_code == 1
    assert "classic_entry_id is not set" in grid.stderr
    assert "No cached sell-price data" in sell_prices.stderr
    assert grid.stdout == sell_prices.stdout == ""


class TestOneCommandUsesOneStream:
    """`fpl player` reported a bad name on stderr and a dead API on stdout.

    Same command, same exit code, two channels -- the split #162 opens with.
    Both paths are driven here because they are reached from opposite ends of
    the command body, and only one of them was ever wrong.
    """

    @staticmethod
    def _client(players):
        client = MagicMock()
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock(return_value=False)
        client.get_players = AsyncMock(return_value=players)
        client.get_teams = AsyncMock(return_value=[])
        client.get_next_gameweek = AsyncMock(return_value={"id": 30})
        return client

    def test_a_name_that_matches_nothing(self, monkeypatch):
        monkeypatch.setattr("fpl_cli.api.fpl.FPLClient", lambda *a, **k: self._client([]))

        result = CliRunner().invoke(main, ["player", "Zzzzzz"])

        assert result.exit_code == 1
        assert "No players found matching" in result.stderr
        assert result.stdout == ""

    def test_a_fetch_that_fails(self, monkeypatch):
        client = self._client([])
        client.get_players = AsyncMock(side_effect=ValueError("boom"))
        monkeypatch.setattr("fpl_cli.api.fpl.FPLClient", lambda *a, **k: client)

        result = CliRunner().invoke(main, ["player", "Salah"])

        assert result.exit_code == 1
        assert "Could not load player data: boom" in result.stderr
        assert result.stdout == ""


class TestFailuresThatSkipTheHelpers:
    """The paths that report failure themselves rather than via `emit_failure`.

    `emit_failure` and `handle_agent_failure` are two of the three ways a
    command reports a failure; the third is a hand-rolled print next to a
    `raise SystemExit(1)`, which is how the split arose in the first place.
    One case per shape that the outage walk above cannot reach.
    """

    @pytest.fixture
    def custom_analysis(self, tmp_path):
        """`transfer-eval` is gated, so without this it exits 2 at the gate."""
        (tmp_path / "user-config" / "settings.yaml").write_text(
            yaml.safe_dump({"custom_analysis": True}), encoding="utf-8",
        )

    @staticmethod
    def _client():
        client = MagicMock()
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock(return_value=False)
        client.get_players = AsyncMock(return_value=[
            make_player(id=1, web_name="Salah", team_id=1,
                        position=PlayerPosition.MIDFIELDER),
            make_player(id=2, web_name="Saka", team_id=1,
                        position=PlayerPosition.MIDFIELDER),
        ])
        client.get_teams = AsyncMock(return_value=[
            make_team(id=1, name="Arsenal", short_name="ARS"),
        ])
        return client

    def test_transfer_eval_reports_an_unresolvable_name(self, custom_analysis):
        with patch("fpl_cli.api.fpl.FPLClient", return_value=self._client()):
            result = CliRunner().invoke(
                main, ["transfer-eval", "--out", "Zzzzzz", "--in", "Saka"],
            )

        assert result.exit_code == 1
        assert "Could not resolve OUT player" in result.stderr
        assert result.stdout == ""

    def test_transfer_eval_reports_an_agent_failure(self, custom_analysis):
        agent = make_agent(success=False, message="no data")

        with patch("fpl_cli.api.fpl.FPLClient", return_value=self._client()), \
             patch("fpl_cli.agents.analysis.transfer_eval.TransferEvalAgent",
                   return_value=agent):
            result = CliRunner().invoke(
                main, ["transfer-eval", "--out", "Salah", "--in", "Saka"],
            )

        assert result.exit_code == 1
        assert "Agent failed: no data" in result.stderr
        assert result.stdout == ""


class TestTheHelpersThemselves:
    """Both shared failure paths, tested where every command inherits them."""

    def test_emit_failure_writes_prose_to_stderr(self, capsys):
        from fpl_cli.cli._json import emit_failure

        with pytest.raises(SystemExit) as exc:
            emit_failure("stats", "nothing to show", "table")

        assert exc.value.code == 1
        captured = capsys.readouterr()
        assert "nothing to show" in captured.err
        assert captured.out == ""

    def test_handle_agent_failure_writes_prose_to_stderr(self, capsys):
        from fpl_cli.agents.base import AgentResult, AgentStatus
        from fpl_cli.cli._context import handle_agent_failure

        result = AgentResult(
            agent_name="ScoringAgent", status=AgentStatus.FAILED,
            message="could not score", errors=["upstream 500"],
        )

        with pytest.raises(SystemExit) as exc:
            handle_agent_failure(result)

        assert exc.value.code == 1
        captured = capsys.readouterr()
        assert "could not score" in captured.err
        assert "upstream 500" in captured.err
        assert captured.out == ""
