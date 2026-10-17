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

The player-identifier tests at the end hold `docs/command-reference.md`'s
*Player identifiers* table to the code (#422): every JSON command is in it or
declared playerless, and every payload it says emits a player `id` has a test
running real output through `assert_player_rows_carry_code()`.
"""

from __future__ import annotations

import ast
import json
import re
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import click
import httpx
import pytest
import yaml
from click.testing import CliRunner

from fpl_cli.cli import main
from fpl_cli.models.player import PlayerPosition
from tests.conftest import (
    assert_player_rows_carry_code,
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


@pytest.mark.parametrize(
    "command", JSON_COMMANDS, ids=[" ".join(c) or "(root)" for c in JSON_COMMANDS],
)
def test_stdout_is_json_or_empty_on_failure(command, offline):
    """stdout parses as JSON from byte 0, whatever the command did.

    Exit 2 is click's own parameter handling, which writes its usage message
    to stderr and never reaches the command body -- that is click's contract,
    not ours, so the only thing to assert is that it left stdout alone.
    """
    _assert_stdout_is_an_envelope(_args_for(command))


def _assert_stdout_is_an_envelope(args: list[str]) -> None:
    """Run `fpl <args>` and hold its stdout to the contract, however it ended.

    Shared by the two walks below -- one drives the commands down an outage,
    the other down a malformed settings block -- because the contract is the
    same either way and the assertions are the fiddly part: a traceback and a
    missing envelope are separate failures with separate messages, and click's
    own exit 2 is its contract rather than ours.
    """
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
def test_stdout_is_json_when_the_settings_are_malformed(command, malformed_fines):
    """The other way a command fails before it has anything to print (#170).

    An outage is caught at a seam every command shares; a config parser is
    not. `parse_fines_config` raised a `ValueError` no caller caught, so one
    `below-threshold` rule missing its `threshold:` printed a traceback and
    left stdout empty on `league-fines`, `status` and `league-recap` alike --
    the same zero-byte stdout #159 closed for every other failure, reopened
    by a command reading its own config.

    The walk holds the whole tree to the envelope, so a command that starts
    parsing the block inherits the rule instead of choosing again.
    `test_the_malformed_block_is_actually_reached` below keeps it honest: the
    API is down here too, so most of these commands fail before the parse and
    would pass this vacuously.
    """
    _assert_stdout_is_an_envelope(_args_for(command))


def test_the_malformed_block_is_actually_reached(malformed_fines):
    """`league-fines` touches no network, so it gets as far as the parse."""
    result = CliRunner().invoke(main, ["league-fines", "--format", "json"])

    assert result.exit_code == 1
    envelope = json.loads(result.stdout)
    assert envelope["command"] == "league-fines"
    assert "requires a 'threshold' value" in envelope["error"]


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


# --- Player identifiers (#422) ---
#
# `docs/command-reference.md` (*Player identifiers*) lists every payload with
# player rows, which namespace its `id` is in, and whether it carries `code`.
# Two sweeps (#418, #419) brought the emitters into line with it by hand; the
# tests below make the table a checked invariant instead. The check itself,
# `assert_player_rows_carry_code()`, runs inside each emitter's own test on
# real output -- these tests only make sure every surface the table says has
# a player `id` is one of them, and that the table names every command.

COMMAND_REFERENCE = Path(__file__).resolve().parents[1] / "docs" / "command-reference.md"
TESTS_DIR = Path(__file__).resolve().parent
CHECK_NAME = "assert_player_rows_carry_code"

# `--format json` commands with no player row in their payload, and so no row
# in the table. Anything that lists players belongs in the table instead.
NO_PLAYER_ROWS: dict[str, str] = {
    "doctor": "setup checks",
    "fixtures": "fixtures and clubs",
    "league-fines": "one tally per manager",
    "chips": "chips used, planned and available",
}

# The gw-prep scripts emit their agent's data whole, so each is checked where
# that agent's real output is, under the name below. `transfer_eval.py` runs
# the same `TransferEvalAgent` as `fpl transfer-eval`, so one check covers both.
SCRIPT_CHECKED_AS: dict[str, str] = {
    "bench_order.py": "bench_order.py",
    "starting_xi.py": "starting_xi.py",
    "transfer_eval.py": "transfer-eval",
}


def _identifier_table() -> list[tuple[set[str], str, str]]:
    """The table's rows as (commands named, `id is` cell, `code` cell).

    A command cell can name several commands and carry flags beside them
    (`fdr --my-squad`, `squad grid` (classic and `--draft`)); the flags are
    dropped, so each backticked span reduces to the command path it names.
    """
    text = COMMAND_REFERENCE.read_text(encoding="utf-8")
    section = text.split("### Player identifiers", 1)[1].split("\n## ", 1)[0]
    rows: list[tuple[set[str], str, str]] = []
    for line in section.splitlines():
        if not line.startswith("| `"):
            continue
        command_cell, _rows_cell, id_cell, code_cell = (
            cell.strip() for cell in line.strip().strip("|").split("|")
        )
        commands = {
            " ".join(word for word in span.split() if not word.startswith("--"))
            for span in re.findall(r"`([^`]+)`", command_cell)
        } - {""}
        rows.append((commands, id_cell, code_cell))
    return rows


IDENTIFIER_TABLE = _identifier_table()
TABLED_COMMANDS = set().union(*(commands for commands, _, _ in IDENTIFIER_TABLE))
COMMANDS_WITH_A_PLAYER_ID = set().union(*(
    commands for commands, id_cell, _ in IDENTIFIER_TABLE if not id_cell.startswith("no id")
))


def _checked_surfaces() -> tuple[dict[str, list[str]], list[str]]:
    """Every `command=` the test suite passes the check, with where it does.

    Read from source rather than collected at run time, so the answer does not
    depend on which tests a run selected. A `command` that is not a string
    literal cannot be read this way, so its call site is returned beside the
    rest for `test_every_check_names_its_command_literally` to report: raising
    here, at import, would error every test in this module at collection.
    This module is left out: its calls test the check, not a surface.
    """
    found: dict[str, list[str]] = {}
    unreadable: list[str] = []
    for path in sorted(TESTS_DIR.rglob("*.py")):
        if path == Path(__file__).resolve():
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)
            if name != CHECK_NAME:
                continue
            command = next((kw.value for kw in node.keywords if kw.arg == "command"), None)
            where = f"{path.relative_to(TESTS_DIR)}:{node.lineno}"
            if isinstance(command, ast.Constant) and isinstance(command.value, str):
                found.setdefault(command.value, []).append(where)
            else:
                unreadable.append(where)
    return found, unreadable


CHECKED_SURFACES, UNREADABLE_CHECKS = _checked_surfaces()


def test_every_check_names_its_command_literally():
    """A check this contract cannot read counts for no surface."""
    assert not UNREADABLE_CHECKS, (
        f"{CHECK_NAME}() called without a string-literal `command=` at "
        f"{', '.join(UNREADABLE_CHECKS)}; pass the command name literally so this "
        f"contract can find the check"
    )


def test_the_identifier_table_was_read():
    """Guard the parser: an empty table would make every test below vacuous."""
    assert len(IDENTIFIER_TABLE) > 15
    assert "waivers" in COMMANDS_WITH_A_PLAYER_ID
    assert "squad sell-prices" in COMMANDS_WITH_A_PLAYER_ID
    assert "squad grid" in TABLED_COMMANDS
    assert "squad grid" not in COMMANDS_WITH_A_PLAYER_ID
    assert "fdr" in TABLED_COMMANDS, "flags must be dropped from a command cell"


@pytest.mark.parametrize(
    "command", JSON_COMMANDS, ids=[" ".join(c) or "(root)" for c in JSON_COMMANDS],
)
def test_every_json_command_is_in_the_identifier_table(command):
    """A new command, or a JSON mode on one that had none, is classified first.

    The table is how a consumer knows whether a payload's `id` is draft or
    main and whether it can join on `code`; a command missing from it leaves
    them to guess, and leaves the check below with nothing to require.
    """
    name = " ".join(command)
    assert name in TABLED_COMMANDS or name in NO_PLAYER_ROWS, (
        f"`fpl {name} --format json` is not in the Player identifiers table in "
        f"docs/command-reference.md. Add a row for each player list it emits, or add it "
        f"to NO_PLAYER_ROWS here if it has none"
    )


def test_no_player_rows_allowlist_is_current():
    """An entry that has been tabled, or whose command has gone, is stale."""
    json_names = {" ".join(c) for c in JSON_COMMANDS}
    assert set(NO_PLAYER_ROWS) <= json_names, set(NO_PLAYER_ROWS) - json_names
    assert not set(NO_PLAYER_ROWS) & TABLED_COMMANDS, set(NO_PLAYER_ROWS) & TABLED_COMMANDS


def test_the_table_names_only_real_commands():
    json_names = {" ".join(c) for c in JSON_COMMANDS}
    assert TABLED_COMMANDS <= json_names, TABLED_COMMANDS - json_names


@pytest.mark.parametrize(
    "commands,id_cell,code_cell", IDENTIFIER_TABLE,
    ids=[", ".join(sorted(c)) for c, _, _ in IDENTIFIER_TABLE],
)
def test_a_documented_player_id_comes_with_code(commands, id_cell, code_cell):
    """The rule itself, as the table states it: no `id` without `code` (#418)."""
    if id_cell.startswith("no id"):
        return
    assert code_cell.startswith("yes"), (
        f"{sorted(commands)} emit a player `id` ({id_cell}) but the table says `code` is "
        f"{code_cell!r}"
    )


@pytest.mark.parametrize(
    "surface",
    sorted(COMMANDS_WITH_A_PLAYER_ID | set(SCRIPT_CHECKED_AS.values())),
)
def test_every_payload_with_a_player_id_has_a_check(surface):
    """The table says these rows carry `code`; a test has to hold them to it.

    The check runs on each surface's real emitter (the agent for a command
    that runs one, the command for one that builds its own rows), so a new
    field added there is held to the rule as soon as that test fills it.
    """
    assert surface in CHECKED_SURFACES, (
        f"no test runs {surface!r} output through {CHECK_NAME}(..., command={surface!r}). "
        f"Add the call to the test that drives its real emitter"
    )


def test_every_check_names_a_surface_the_table_knows():
    """A typo in `command=` would leave the real surface unchecked."""
    known = COMMANDS_WITH_A_PLAYER_ID | set(SCRIPT_CHECKED_AS.values())
    unknown = {name: where for name, where in CHECKED_SURFACES.items() if name not in known}
    assert not unknown, unknown


def test_every_gw_prep_script_is_mapped_to_a_check():
    """The scripts the success-path test above runs are the ones checked here."""
    assert set(SCRIPT_CHECKED_AS) == {param.id for param in AGENT_SCRIPTS}


class TestThePlayerRowCheck:
    """`assert_player_rows_carry_code()` itself: what counts as a player row."""

    def test_rows_that_are_not_players_are_left_alone(self):
        """Gameweeks, clubs, fixtures and managers all carry an `id` too."""
        data = {
            "gameweek": {"id": 30, "name": "Gameweek 30"},
            "teams": [{"id": 1, "name": "Arsenal", "short_name": "ARS"}],
            "fixtures": [{"id": 9, "team_h": 1, "team_a": 2}],
            "standings": [{"id": 5, "entry": 100, "player_name": "A Manager", "rank": 1}],
            "warnings": [{"code": "early_season_small_sample", "message": "..."}],
            "players": [{"id": 1, "code": 10_001, "web_name": "Salah"}],
        }
        rows = assert_player_rows_carry_code(data, command="test")
        assert [row["web_name"] for row in rows] == ["Salah"]

    @pytest.mark.parametrize("row", [
        {"id": 1, "web_name": "Salah"},
        {"id": 1, "player_name": "Salah", "position": "MID"},
        {"id": 1, "name": "Salah", "position": "MID"},
        {"element": 1, "web_name": "Salah"},
    ], ids=["web_name", "player_name+position", "name+position", "element"])
    def test_a_player_row_without_code_fails(self, row):
        with pytest.raises(AssertionError, match="no `code`"):
            assert_player_rows_carry_code({"nested": [row]}, command="test")

    @pytest.mark.parametrize("code", [0, False, "118748", 1.0])
    def test_a_code_that_is_not_a_positive_int_fails(self, code):
        with pytest.raises(AssertionError, match="never 0 or a guess"):
            assert_player_rows_carry_code([{"id": 1, "code": code, "web_name": "S"}], command="test")

    def test_a_code_equal_to_the_id_fails(self):
        with pytest.raises(AssertionError, match="code == id"):
            assert_player_rows_carry_code([{"id": 7, "code": 7, "web_name": "S"}], command="test")

    def test_null_code_is_allowed(self):
        assert_player_rows_carry_code([{"id": 1, "code": None, "web_name": "S"}], command="test")

    def test_a_payload_with_no_player_rows_fails(self):
        """Otherwise a call on a payload that never reached its rows would pass."""
        with pytest.raises(AssertionError, match="nothing to check"):
            assert_player_rows_carry_code({"players": []}, command="test")
