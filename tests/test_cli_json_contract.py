"""Every `--format json` command keeps stdout machine-parseable.

`docs/command-reference.md` promises a consumer one thing: parse stdout and
you get an envelope either way -- `{command, metadata, data}` on success,
`{command, error}` on failure -- with every human-readable line on stderr.
Individual command tests assert that for the paths they happen to exercise,
which is how #140, #141 and #144 all reached a release: each was a path
nobody had a test for, in a command whose happy path was covered.

This module tests the contract instead of the commands. It walks the click
tree, so a new `--format json` command is covered the moment it is
registered, and drives every one of them down a failure path -- the FPL API
unreachable, no entry IDs configured -- because that is the side the bugs
were on. Prose printed before the envelope, or an early `return` that skips
it, breaks a consumer here rather than in someone's script.

`TestSuccessPathsThatRunAnAgent` at the bottom covers the other side, and
the gw-prep helper scripts alongside the commands: the scripts are vendored
into user vaults and parsed by an LLM orchestrator, so they owe a consumer
the same clean stdout a command does (#226).
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import click
import httpx
import pytest
import yaml
from click.testing import CliRunner

from fpl_cli.cli import main
from fpl_cli.models.player import PlayerPosition
from tests.conftest import (
    load_gw_prep_script,
    make_logging_agent,
    make_player,
    make_team,
)

# Whatever a command needs before click will hand control to its body --
# required arguments and required options alike. Without them click exits 2
# and the command body never runs, which tests click rather than the contract.
# `test_no_command_is_masked_by_a_usage_error` below keeps this list honest:
# `transfer-eval` was absent, so its missing `api_failure_boundary` sat behind
# an exit 2 and the contract tests reported it as covered (#159 review).
REQUIRED_PARAMS: dict[str, list[str]] = {
    "player": ["Salah"],
    "intel show": ["Arsenal"],
    "intel resolve": ["Arsenal"],
    "transfer-eval": ["--out", "Salah", "--in", "Haaland"],
}


def _args_for(command: tuple[str, ...]) -> list[str]:
    return list(command) + REQUIRED_PARAMS.get(" ".join(command), []) + ["--format", "json"]


def _json_commands(cmd: click.Command, path: tuple[str, ...] = ()) -> list[tuple[str, ...]]:
    """Every command path in the tree that accepts `--format json`.

    A group can carry the option itself (`invoke_without_command=True`, as
    `fpl squad` and `fpl chips` do), so groups are yielded as well as walked.
    """
    found: list[tuple[str, ...]] = []
    accepts_format = any(p.name == "output_format" for p in cmd.params)
    if isinstance(cmd, click.Group):
        if getattr(cmd, "invoke_without_command", False) and accepts_format:
            found.append(path)
        for name, sub in cmd.commands.items():
            found.extend(_json_commands(sub, path + (name,)))
    elif accepts_format:
        found.append(path)
    return found


JSON_COMMANDS = _json_commands(main)


def test_command_discovery_found_the_tree():
    """Guard the walker: a silent zero here would make every test below vacuous."""
    assert len(JSON_COMMANDS) > 15
    assert ("stats",) in JSON_COMMANDS
    assert ("squad", "grid") in JSON_COMMANDS
    assert ("squad",) in JSON_COMMANDS, "invoke_without_command groups must be covered"


@pytest.fixture(params=[True, False], ids=["custom-on", "custom-off"])
def offline(request, monkeypatch, tmp_path):
    """A configured install whose upstream APIs are all unreachable.

    Run twice, because `custom_analysis` picks between two different bodies
    for the same command -- `fdr` serves Bayesian ratings under one and raw
    API difficulty under the other, and only the second has the early return
    that skips the envelope. No entry IDs are set either way, so the commands
    that need one take their not-configured path. All of it is ordinary
    first-week state for a real user.
    """
    (tmp_path / "user-config" / "settings.yaml").write_text(
        yaml.safe_dump({"custom_analysis": request.param}), encoding="utf-8",
    )

    async def _unreachable(*args, **kwargs):
        raise httpx.ConnectError("connection refused")

    def _unreachable_sync(*args, **kwargs):
        raise httpx.ConnectError("connection refused")

    for module, attr in (
        ("fpl_cli.api.fpl", "FPLClient"),
        ("fpl_cli.api.fpl_draft", "FPLDraftClient"),
    ):
        mod = __import__(module, fromlist=[attr])
        monkeypatch.setattr(getattr(mod, attr), "_get", _unreachable, raising=False)
    monkeypatch.setattr(httpx.AsyncClient, "send", _unreachable)
    monkeypatch.setattr(httpx.Client, "send", _unreachable_sync)


@pytest.mark.parametrize(
    "command", JSON_COMMANDS, ids=[" ".join(c) or "(root)" for c in JSON_COMMANDS],
)
def test_stdout_is_json_or_empty_on_failure(command, offline):
    """stdout parses as JSON from byte 0, whatever the command did.

    Exit 2 is click's own parameter handling, which writes its usage message
    to stderr and never reaches the command body -- that is click's contract,
    not ours, so the only thing to assert is that it left stdout alone.
    """
    args = _args_for(command)
    result = CliRunner().invoke(main, args)

    if result.exception is not None and not isinstance(result.exception, SystemExit):
        raise AssertionError(
            f"`fpl {' '.join(args)}` raised {result.exception!r} instead of reporting "
            f"the failure as an envelope"
        ) from result.exception

    if result.exit_code == 2:
        assert result.stdout == "", (
            f"`fpl {' '.join(args)}` wrote to stdout on a click usage error"
        )
        return

    assert result.stdout.strip(), (
        f"`fpl {' '.join(args)}` exited {result.exit_code} with nothing on stdout -- "
        f"a consumer gets no envelope to parse. stderr was: {result.stderr[:300]!r}"
    )
    try:
        envelope = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise AssertionError(
            f"`fpl {' '.join(args)}` stdout is not JSON ({exc}). "
            f"First 200 chars: {result.stdout[:200]!r}"
        ) from exc
    assert envelope["command"], "envelope must name the command that produced it"


@pytest.mark.parametrize(
    "command", JSON_COMMANDS, ids=[" ".join(c) or "(root)" for c in JSON_COMMANDS],
)
def test_exit_code_matches_envelope_kind(command, offline):
    """Exit 0 means data, exit 1 means an error, and nothing else means either.

    The exit code is the only signal a shell script checks before it bothers
    parsing, so an error envelope behind exit 0 (#144) is as broken as no
    envelope at all.
    """
    args = _args_for(command)
    result = CliRunner().invoke(main, args)

    if result.exit_code == 2:
        return

    assert result.exit_code in (0, 1), (
        f"`fpl {' '.join(args)}` exited {result.exit_code}; the contract defines 0 and 1"
    )
    envelope = json.loads(result.stdout)
    if result.exit_code == 0:
        assert "error" not in envelope, "exit 0 must not carry an error envelope"
        assert "data" in envelope, "a success envelope carries data"
    else:
        assert "error" in envelope, (
            f"`fpl {' '.join(args)}` exited 1 without an error envelope: {envelope!r}"
        )


@pytest.mark.parametrize(
    "command", JSON_COMMANDS, ids=[" ".join(c) or "(root)" for c in JSON_COMMANDS],
)
def test_no_command_is_masked_by_a_usage_error(command, monkeypatch, tmp_path):
    """Every command must actually reach its body under the tests above.

    Exit 2 is the one code those tests tolerate, which makes it a blind spot:
    a command missing an entry in REQUIRED_PARAMS never runs, and reports as
    covered while its failure paths go untested. With custom analysis on
    nothing else exits 2, so any exit 2 here is a missing entry.
    """
    (tmp_path / "user-config" / "settings.yaml").write_text(
        yaml.safe_dump({"custom_analysis": True}), encoding="utf-8",
    )

    async def _unreachable(*args, **kwargs):
        raise httpx.ConnectError("connection refused")

    monkeypatch.setattr(httpx.AsyncClient, "send", _unreachable)

    result = CliRunner().invoke(main, _args_for(command))

    assert result.exit_code != 2, (
        f"`fpl {' '.join(_args_for(command))}` exited 2 -- click rejected the invocation "
        f"before the command body ran, so the contract tests above never reached it. "
        f"Add its required parameters to REQUIRED_PARAMS. stderr: {result.stderr[:300]!r}"
    )


class TestFailuresThatAreNotNetworkShaped:
    """Paths the `offline` fixture cannot reach, each found by review on #159.

    The contract tests above drive every command down an outage. That misses
    everything that fails for another reason before or after the API calls --
    a malformed input file, an unwritable cache, a name that matches nothing.
    Each is the same bug in a different shape, so each gets a case here.
    """

    @pytest.fixture
    def custom_analysis(self, tmp_path):
        """`allocate` is gated, so without this it exits 2 at the gate."""
        (tmp_path / "user-config" / "settings.yaml").write_text(
            yaml.safe_dump({"custom_analysis": True}), encoding="utf-8",
        )

    def test_allocate_reports_a_malformed_sell_prices_file(self, tmp_path, custom_analysis):
        bad = tmp_path / "sell-prices.json"
        bad.write_text("{not json", encoding="utf-8")

        result = CliRunner().invoke(
            main, ["allocate", "--sell-prices", str(bad), "--format", "json"],
        )

        assert result.exit_code == 1
        assert "Error reading sell-prices file" in json.loads(result.stdout)["error"]

    def test_allocate_reports_sell_prices_missing_a_field(self, tmp_path, custom_analysis):
        bad = tmp_path / "sell-prices.json"
        bad.write_text(json.dumps({"data": [{"name": "Salah"}]}), encoding="utf-8")

        result = CliRunner().invoke(
            main, ["allocate", "--sell-prices", str(bad), "--format", "json"],
        )

        assert result.exit_code == 1
        assert "missing required field" in json.loads(result.stdout)["error"]

    def test_player_reports_a_name_that_matches_nothing(self, monkeypatch):
        from unittest.mock import AsyncMock, MagicMock

        client = MagicMock()
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock(return_value=False)
        client.get_players = AsyncMock(return_value=[])
        client.get_teams = AsyncMock(return_value=[])
        client.get_next_gameweek = AsyncMock(return_value={"id": 30})
        monkeypatch.setattr("fpl_cli.api.fpl.FPLClient", lambda *a, **k: client)

        result = CliRunner().invoke(main, ["player", "Zzzzzz", "--format", "json"])

        assert result.exit_code == 1
        assert "No players found matching" in json.loads(result.stdout)["error"]

    def test_sell_prices_reports_an_unwritable_cache(self, monkeypatch):
        from unittest.mock import AsyncMock, patch

        from fpl_cli.scraper.fpl_prices import PlayerSellPrice, TeamFinances

        squad = [
            PlayerSellPrice(name=f"P{i}", sell_price=5.0, position="MID", element_id=i)
            for i in range(15)
        ]
        finances = TeamFinances(squad=squad, bank=1.0, total_value=75.0, free_transfers=1)
        assert not finances.is_suspect

        def _unwritable(*args, **kwargs):
            raise PermissionError("read-only file system")

        with patch("fpl_cli.scraper.fpl_prices.FPLPriceScraper") as scraper_cls, \
             patch("fpl_cli.scraper.fpl_prices.save_cache", _unwritable), \
             patch("fpl_cli.scraper.fpl_prices.load_cache", return_value=None):
            scraper_cls.return_value.scrape = AsyncMock(return_value=finances)
            result = CliRunner().invoke(
                main, ["squad", "sell-prices", "--refresh", "--format", "json"],
            )

        assert result.exit_code == 1
        assert "Could not write the sell-price cache" in json.loads(result.stdout)["error"]

    def test_fixtures_exits_nonzero_when_the_fetch_fails_in_table_mode(self, monkeypatch):
        """The handler printed and fell through, so this exited 0 (#159 review)."""
        from unittest.mock import AsyncMock, MagicMock

        client = MagicMock()
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock(return_value=False)
        client.get_next_gameweek = AsyncMock(return_value={"id": 30})
        client.get_fixtures = AsyncMock(side_effect=ValueError("boom"))
        monkeypatch.setattr("fpl_cli.api.fpl.FPLClient", lambda *a, **k: client)

        result = CliRunner().invoke(main, ["fixtures"])

        assert result.exit_code == 1
        assert "Error fetching fixtures: boom" in result.output


def test_a_status_error_is_not_reported_as_an_outage(capsys):
    """A 404 that reaches the boundary says so, rather than blaming the network.

    The two are worth telling apart: an outage is worth a retry, a 404 means
    the command has a gap in its own handling (#159 review).
    """
    from fpl_cli.cli._json import api_failure_boundary

    request = httpx.Request("GET", "https://fantasy.premierleague.com/api/entry/1/event/5/picks/")
    response = httpx.Response(404, request=request)

    with pytest.raises(SystemExit):
        with api_failure_boundary("squad", "json"):
            raise httpx.HTTPStatusError("404", request=request, response=response)

    envelope = json.loads(capsys.readouterr().out)
    assert "returned 404" in envelope["error"]
    assert "Could not reach" not in envelope["error"]


# Two players in one position, so the same pair serves every script: the
# transfer-eval surfaces reject an IN candidate that does not match OUT.
_SQUAD = [
    make_player(id=1, web_name="Salah", first_name="Mohamed", second_name="Salah",
                team_id=1, position=PlayerPosition.MIDFIELDER),
    make_player(id=2, web_name="Saka", first_name="Bukayo", second_name="Saka",
                team_id=1, position=PlayerPosition.MIDFIELDER),
]
_CLUBS = [make_team(id=1, name="Arsenal", short_name="ARS")]


def _stub_client():
    client = MagicMock()
    client.get_players = AsyncMock(return_value=_SQUAD)
    client.get_teams = AsyncMock(return_value=_CLUBS)
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)
    return client


# Each entry names a helper script, the agent attribute to swap on it, and
# how its `_run` is called -- the three signatures differ.
AGENT_SCRIPTS = [
    pytest.param("bench_order.py", "BenchOrderAgent",
                 lambda run: run(["Salah"], ["Saka"]), id="bench_order.py"),
    pytest.param("starting_xi.py", "StartingXIAgent",
                 lambda run: run(["Salah", "Saka"]), id="starting_xi.py"),
    pytest.param("transfer_eval.py", "TransferEvalAgent",
                 lambda run: run("Salah", ["Saka"]), id="transfer_eval.py"),
]


class TestSuccessPathsThatRunAnAgent:
    """The half of the contract the tree walk above cannot reach (#226).

    Those tests drive every command down an outage, which is where #140,
    #141 and #144 lived. #226 was on the other side: the agent ran, logged
    two progress lines to stdout, and only then was the envelope printed --
    so `json.loads(stdout)` failed on success and passed on failure.

    There is no generic version of this. A success path needs data, and the
    data each command wants differs, so these are the surfaces that run an
    agent and are read by a machine: the one command that had the bug, and
    the three vendored scripts that had it too. The invariant behind all of
    them -- that `Agent.log` cannot reach stdout at all -- is enforced once
    in `tests/test_agents_base.py`.

    `make_logging_agent()` rather than `make_agent()` throughout: a MagicMock
    agent prints nothing, which is exactly why the existing suites for these
    four surfaces all passed while the bug shipped.
    """

    @pytest.fixture
    def custom_analysis(self, tmp_path):
        """`transfer-eval` is gated, so without this it exits 2 at the gate."""
        (tmp_path / "user-config" / "settings.yaml").write_text(
            yaml.safe_dump({"custom_analysis": True}), encoding="utf-8",
        )

    def test_transfer_eval_envelope_is_not_preceded_by_agent_prose(self, custom_analysis):
        agent = make_logging_agent({
            "out_player": {"price": 10.0},
            "in_players": [{"price": 8.0}],
        })

        with patch("fpl_cli.api.fpl.FPLClient", return_value=_stub_client()), \
             patch("fpl_cli.agents.analysis.transfer_eval.TransferEvalAgent",
                   return_value=agent), \
             patch("fpl_cli.scraper.fpl_prices.load_cache", return_value=None):
            result = CliRunner().invoke(
                main,
                ["transfer-eval", "--out", "Salah", "--in", "Saka", "--format", "json"],
            )

        assert result.exit_code == 0, result.output
        envelope = json.loads(result.stdout)
        assert envelope["command"] == "transfer-eval"
        assert envelope["data"]["in_players"] == [{"price": 8.0}]
        assert "LoggingTestAgent" in result.stderr, (
            "the agent's progress lines went missing rather than moving to stderr"
        )

    @pytest.mark.parametrize("filename,agent_attr,invoke", AGENT_SCRIPTS)
    async def test_helper_script_stdout_is_json_from_byte_zero(
        self, filename, agent_attr, invoke, capsys,
    ):
        """gw-prep's SKILL.md tells its reader to parse stdout as JSON.

        The scripts run in a user's vault against an orchestrator that will
        not always be lenient about leading prose, and they have no `--format`
        flag to gate on -- JSON is the only thing they emit.
        """
        script = load_gw_prep_script(filename)
        payload = {"result": "ok"}

        with patch.object(script, "FPLClient", return_value=_stub_client()), \
             patch.object(script, agent_attr, return_value=make_logging_agent(payload)):
            await invoke(script._run)

        captured = capsys.readouterr()
        assert captured.out.startswith("{"), (
            f"{filename} put {captured.out[:80]!r} ahead of its JSON"
        )
        assert json.loads(captured.out) == payload
        assert "LoggingTestAgent" in captured.err, (
            "the agent's progress lines went missing rather than moving to stderr"
        )
