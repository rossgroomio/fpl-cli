"""Tests for the `league-fines` season table (issue #136)."""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import patch

from click.testing import CliRunner

from fpl_cli.cli.league_fines import league_fines_command
from fpl_cli.models.league_history import FidelityTier, LedgerFine
from fpl_cli.services.league_history import LeagueHistoryStore
from tests.conftest import make_history_row

SEASON = "2026-27"
LEAGUE_ID = 42
ALL_RULES = ["last-place", "below-threshold", "red-card"]

_SETTINGS: dict[str, Any] = {
    "fpl": {"classic_league_id": LEAGUE_ID},
    "fines": {"classic": [
        {"type": "last-place", "penalty": "Pint"},
        {"type": "below-threshold", "threshold": 30, "penalty": "Pint"},
        {"type": "red-card", "penalty": "Round"},
    ]},
}


def _store(fpl_format: str = "classic", league_id: int = LEAGUE_ID) -> LeagueHistoryStore:
    return LeagueHistoryStore(SEASON, fpl_format, league_id)  # type: ignore[arg-type]


def _fine(manager_key: int, rule_type: str = "last-place") -> LedgerFine:
    return LedgerFine(manager_key=manager_key, rule_type=rule_type, message=f"{rule_type} fine.")


def _invoke(args: list[str] | None = None, *, settings: dict[str, Any] | None = None):
    with (
        patch("fpl_cli.cli.league_fines.load_settings", return_value=settings or _SETTINGS),
        patch("fpl_cli.cli.league_fines.season_label", return_value=SEASON),
    ):
        return CliRunner().invoke(league_fines_command, args or [])


def _seed_two_gameweeks(fpl_format: str = "classic") -> None:
    store = _store(fpl_format)
    for gw in (1, 2):
        store.append_rows(gw, [
            make_history_row(
                season=SEASON, fpl_format=fpl_format, league_id=LEAGUE_ID,
                gameweek=gw, manager_key=1, manager_name="Alice",
                fine_rules_evaluated=ALL_RULES,
            ),
            make_history_row(
                season=SEASON, fpl_format=fpl_format, league_id=LEAGUE_ID,
                gameweek=gw, manager_key=2, manager_name="Bob",
                fine_rules_evaluated=ALL_RULES, fines=[_fine(2)],
            ),
        ])


def _payload(result) -> dict[str, Any]:
    return json.loads(result.stdout)


class TestTableOutput:
    def test_the_table_ranks_by_total_and_names_every_recorded_manager(self):
        _seed_two_gameweeks()

        result = _invoke()

        assert result.exit_code == 0, result.output
        assert "Bob" in result.output
        assert "Alice" in result.output
        assert result.output.index("Bob") < result.output.index("Alice")

    def test_a_configured_rule_that_never_triggered_still_gets_a_column(self):
        _seed_two_gameweeks()

        output = _invoke().output.replace("\n", "")

        assert "Red card" in output

    def test_a_fully_ruled_span_says_so_rather_than_leaving_a_zero_unexplained(self):
        _seed_two_gameweeks()

        output = _invoke().output.replace("\n", "")

        assert "Every gameweek from GW1 through GW2 was ruled" in output

    def test_a_gap_is_named_and_the_manager_row_is_flagged(self):
        store = _store()
        for gw in (1, 3):
            store.append_rows(gw, [make_history_row(
                season=SEASON, league_id=LEAGUE_ID, gameweek=gw,
                manager_key=1, manager_name="Alice", fine_rules_evaluated=ALL_RULES,
            )])

        output = _invoke().output.replace("\n", "")

        assert "GW2 was never captured" in output
        assert "*" in output, "an incompletely ruled span is marked in the table too"

    def test_an_empty_partition_explains_where_fines_come_from(self):
        result = _invoke()

        assert result.exit_code == 0, result.output
        assert "No league history is recorded" in result.output.replace("\n", "")

    def test_an_explicit_gameweek_bounds_the_tally(self):
        _seed_two_gameweeks()

        output = _invoke(["-g", "1"]).output.replace("\n", "")

        assert "GW1-GW1" in output

    def test_a_manager_name_in_a_qualifier_line_is_not_interpreted_either(self):
        """The qualifier lines carry manager names too, so escaping only the
        table cells leaves half the surface exposed (#165 review)."""
        store = _store()
        store.append_rows(1, [make_history_row(
            season=SEASON, league_id=LEAGUE_ID, gameweek=1, manager_key=1,
            manager_name="Alice", fine_rules_evaluated=ALL_RULES,
        )])
        # A joiner, so their name reaches a qualifier line rather than only a
        # table cell.
        store.append_rows(2, [
            make_history_row(
                season=SEASON, league_id=LEAGUE_ID, gameweek=2, manager_key=1,
                manager_name="Alice", fine_rules_evaluated=ALL_RULES,
            ),
            make_history_row(
                season=SEASON, league_id=LEAGUE_ID, gameweek=2, manager_key=2,
                manager_name="[b]B[/b]", fine_rules_evaluated=ALL_RULES,
            ),
        ])

        output = _invoke().output.replace("\n", "")

        assert "recorded history begins at GW2" in output
        # Twice: once in their table cell, once in the joiner qualifier. One
        # occurrence means the qualifier swallowed the brackets as markup.
        assert output.count("[b]B[/b]") == 2

    def test_a_manager_name_with_markup_is_not_interpreted(self):
        _store().append_rows(1, [make_history_row(
            season=SEASON, league_id=LEAGUE_ID, gameweek=1, manager_key=1,
            manager_name="[b]A[/b]", fine_rules_evaluated=ALL_RULES,
        )])

        output = _invoke().output.replace("\n", "")

        assert "[b]A[/b]" in output


class TestJsonOutput:
    def test_the_envelope_carries_the_totals_and_the_qualifiers(self):
        _seed_two_gameweeks()

        result = _invoke(["--format", "json"])

        assert result.exit_code == 0, result.output
        payload = _payload(result)
        assert payload["command"] == "league-fines"
        assert payload["metadata"]["season"] == SEASON
        assert payload["metadata"]["gameweek"] == 2
        assert payload["metadata"]["rule_types"] == ALL_RULES
        assert payload["metadata"]["total_fines"] == 2
        assert payload["metadata"]["qualifiers"]

    def test_every_recorded_manager_is_emitted_fined_or_not(self):
        """"Recorded and not fined" is a fact a consumer needs; dropping the
        zero rows would make it indistinguishable from never recorded."""
        _seed_two_gameweeks()

        data = _payload(_invoke(["--format", "json"]))["data"]

        assert [m["manager_name"] for m in data] == ["Bob", "Alice"]
        assert data[0]["counts"] == {"last-place": 2, "below-threshold": 0, "red-card": 0}
        assert data[1]["total"] == 0
        assert data[1]["is_fully_ruled"] is True

    def test_stdout_stays_parseable_when_the_ledger_is_empty(self):
        result = _invoke(["--format", "json"])

        assert result.exit_code == 0, result.output
        assert _payload(result)["data"] == []

    def test_a_missing_league_id_is_an_error_envelope_and_exit_one(self):
        result = _invoke(["--format", "json"], settings={"fpl": {}})

        assert result.exit_code == 1
        payload = _payload(result)
        assert payload["command"] == "league-fines"
        assert "league id" in payload["error"]


class TestFailureContract:
    """#159's contract: stdout parses either way, and the prose channel never
    interprets user input as markup."""

    def test_a_malformed_season_label_is_not_read_as_rich_markup(self):
        """`--season` is user input on its way into a Rich console. Routed
        through `emit_failure`, which escapes it; a hand-rolled
        `console.print(f"[red]{message}[/red]")` would not."""
        result = _invoke(["--season", "[bold]2025[/bold]"])

        assert result.exit_code == 1
        assert "[bold]2025[/bold]" in result.output

    def test_the_command_completes_with_every_http_transport_broken(self):
        """It reads the ledger and nothing else, which is why it carries no
        `api_failure_boundary`. If that ever stops being true this fails
        rather than the omission going unnoticed."""
        import httpx

        def _boom(*args: Any, **kwargs: Any):
            raise httpx.ConnectError("connection refused")

        async def _async_boom(*args: Any, **kwargs: Any):
            raise httpx.ConnectError("connection refused")

        _seed_two_gameweeks()
        with (
            patch.object(httpx.AsyncClient, "send", _async_boom),
            patch.object(httpx.Client, "send", _boom),
        ):
            result = _invoke(["--format", "json"])

        assert result.exit_code == 0, result.output
        assert _payload(result)["metadata"]["total_fines"] == 2


class TestSelection:
    def test_a_bad_season_label_is_refused_rather_than_read_as_a_partition(self):
        result = _invoke(["--season", "not-a-season"])

        assert result.exit_code == 1
        assert "not a season label" in result.output.replace("\n", "")

    def test_an_earlier_season_is_readable_because_the_ledger_partitions_by_season(self):
        LeagueHistoryStore("2025-26", "classic", LEAGUE_ID).append_rows(1, [make_history_row(
            season="2025-26", league_id=LEAGUE_ID, gameweek=1, manager_key=1,
            manager_name="LastSeason", fine_rules_evaluated=ALL_RULES, fines=[_fine(1)],
        )])

        output = _invoke(["--season", "2025-26"]).output.replace("\n", "")

        assert "LastSeason" in output
        assert "2025-26" in output

    def test_draft_reads_the_draft_partition(self):
        _seed_two_gameweeks("draft")

        settings = {
            "fpl": {"draft_league_id": LEAGUE_ID},
            "fines": {"draft": [{"type": "last-place"}]},
        }
        output = _invoke(["--draft"], settings=settings).output.replace("\n", "")

        assert "Bob" in output
        assert "draft" in output

    def test_a_coarse_gameweek_names_the_rule_it_could_not_rule(self):
        _store().append_rows(1, [make_history_row(
            season=SEASON, league_id=LEAGUE_ID, gameweek=1, manager_key=1,
            manager_name="Alice", tier=FidelityTier.COARSE,
            fine_rules_evaluated=["last-place", "below-threshold"],
        )])

        output = _invoke().output.replace("\n", "")

        assert "'red-card' was not ruled in GW1" in output
        assert "carries no squad" in output
