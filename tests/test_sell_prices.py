"""Tests for the sell-prices CLI command."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from click.testing import CliRunner

from fpl_cli.cli.sell_prices import sell_prices_command
from fpl_cli.models.player import PlayerPosition
from fpl_cli.scraper.fpl_prices import PlayerSellPrice, TeamFinances
from tests.conftest import assert_player_rows_carry_code, make_player


def _today(salah: int = 135, haaland: int = 145) -> list:
    """Salah and Haaland at today's price, by default the one they were scraped at."""
    return [
        make_player(id=253, code=118748, web_name="Salah", position=PlayerPosition.MIDFIELDER, now_cost=salah),
        make_player(id=355, code=223094, web_name="Haaland", position=PlayerPosition.FORWARD, now_cost=haaland),
    ]


@pytest.fixture(autouse=True)
def today(todays_players):
    """Today's player list, holding the scrape's prices until a test changes them."""
    todays_players.extend(_today())
    return todays_players


def _run_json(finances: TeamFinances):
    with patch("fpl_cli.scraper.fpl_prices.load_cache", return_value=finances):
        result = CliRunner().invoke(sell_prices_command, ["--format", "json"], catch_exceptions=False)
    assert result.exit_code == 0, result.stderr
    return json.loads(result.stdout)


def _make_finances(*, with_element_ids: bool = True) -> TeamFinances:
    squad = [
        PlayerSellPrice(
            name="Salah", sell_price=13.0, position="MID",
            purchase_price=12.5, element_id=253 if with_element_ids else None, code=118748,
            price_at_scrape=13.5,
        ),
        PlayerSellPrice(
            name="Haaland", sell_price=14.5, position="FWD",
            purchase_price=15.0, element_id=355 if with_element_ids else None, code=223094,
            price_at_scrape=14.5,
        ),
    ]
    return TeamFinances(
        bank=1.5, free_transfers=2, squad=squad,
        total_value=29.0, scraped_at="2026-03-30T12:00:00",
    )


class TestSellPricesJson:
    def test_json_output_structure(self):
        finances = _make_finances()
        runner = CliRunner()
        with patch("fpl_cli.scraper.fpl_prices.load_cache", return_value=finances):
            result = runner.invoke(sell_prices_command, ["--format", "json"], catch_exceptions=False)

        assert result.exit_code == 0
        envelope = json.loads(result.output)
        assert envelope["command"] == "sell-prices"
        assert len(envelope["data"]) == 2
        assert envelope["data"][0]["id"] == 253
        assert envelope["data"][0]["code"] == 118748
        assert_player_rows_carry_code(envelope["data"], command="squad sell-prices")
        assert envelope["metadata"]["warnings"] == []
        assert envelope["data"][0]["sell_price"] == 13.0
        assert envelope["metadata"]["bank"] == 1.5
        assert envelope["metadata"]["total_sell_value"] == 27.5
        assert envelope["metadata"]["free_transfers"] == 2

    def test_rows_without_code_are_named_in_a_warning_not_an_error(self):
        """A cache scraped before `code` was stored still budgets, so it is data with a warning (#419)."""
        finances = _make_finances()
        for player in finances.squad:
            player.code = None
        runner = CliRunner()
        with patch("fpl_cli.scraper.fpl_prices.load_cache", return_value=finances):
            result = runner.invoke(sell_prices_command, ["--format", "json"], catch_exceptions=False)

        assert result.exit_code == 0
        envelope = json.loads(result.output)
        assert [row["code"] for row in envelope["data"]] == [None, None]
        [warning] = envelope["metadata"]["warnings"]
        assert warning["code"] == "sell_prices_code_missing"
        assert "2 of 2" in warning["message"]
        assert "--refresh" in warning["message"]

    def test_json_error_when_element_ids_missing(self):
        finances = _make_finances(with_element_ids=False)
        runner = CliRunner()
        with patch("fpl_cli.scraper.fpl_prices.load_cache", return_value=finances):
            result = runner.invoke(sell_prices_command, ["--format", "json"])

        assert result.exit_code == 1
        output = result.output + (result.stderr_bytes or b"").decode()
        assert "DOM fallback" in output or "player IDs" in output.lower()

    def test_table_output_unchanged(self):
        finances = _make_finances()
        runner = CliRunner()
        with patch("fpl_cli.scraper.fpl_prices.load_cache", return_value=finances):
            result = runner.invoke(sell_prices_command, ["--format", "table"], catch_exceptions=False)

        assert result.exit_code == 0
        assert "Salah" in result.output
        assert "Haaland" in result.output

    def test_empty_squad_json(self):
        finances = TeamFinances(bank=5.0, free_transfers=1, total_value=5.0, scraped_at="2026-03-30T12:00:00")
        runner = CliRunner()
        with patch("fpl_cli.scraper.fpl_prices.load_cache", return_value=finances):
            result = runner.invoke(sell_prices_command, ["--format", "json"], catch_exceptions=False)

        assert result.exit_code == 0
        envelope = json.loads(result.output)
        assert envelope["data"] == []


class TestSellPricesAtTodaysPrices:
    """The cache's sell prices follow price changes made since the scrape (#426)."""

    def test_a_rise_since_the_scrape_moves_the_sell_price(self, today):
        # Bought at 12.5, scraped at 13.5 (sell 13.0), now 13.7: half of 1.2 is 0.6.
        today[:] = _today(salah=137)
        envelope = _run_json(_make_finances())

        salah, haaland = envelope["data"]
        assert (salah["sell_price"], salah["scraped_sell_price"]) == (13.1, 13.0)
        assert (haaland["sell_price"], haaland["scraped_sell_price"]) == (14.5, 14.5)
        assert envelope["metadata"]["total_sell_value"] == pytest.approx(27.6)
        assert envelope["metadata"]["warnings"] == []

    def test_a_row_the_rule_cannot_reproduce_keeps_its_scraped_price_and_is_named(self, today):
        finances = _make_finances()
        finances.squad[0].price_at_scrape = 13.7  # the rule gives 13.1, FPL said 13.0
        today[:] = _today(salah=139)
        envelope = _run_json(finances)

        assert envelope["data"][0]["sell_price"] == 13.0
        [warning] = envelope["metadata"]["warnings"]
        assert warning["code"] == "sell_prices_as_scraped"
        assert "1 of 2" in warning["message"]
        assert "Salah" in warning["message"]
        assert "--refresh" in warning["message"]

    @pytest.mark.parametrize(("failure", "detail"), [
        (httpx.ConnectError("connection refused"), "ConnectError"),
        (httpx.ReadTimeout(""), "ReadTimeout"),
        (
            httpx.HTTPStatusError(
                "503", request=httpx.Request("GET", "https://x/"), response=httpx.Response(503),
            ),
            "HTTP 503",
        ),
        # A 200 that is not the player list: a maintenance page, a body without `elements`.
        (json.JSONDecodeError("Expecting value", "<html>", 0), "JSONDecodeError"),
        (KeyError("elements"), "KeyError"),
    ])
    def test_a_failed_lookup_leaves_every_price_as_scraped_with_a_warning(self, failure, detail):
        client = MagicMock()
        client.get_players = AsyncMock(side_effect=failure)
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock(return_value=False)
        with patch("fpl_cli.api.fpl.FPLClient", return_value=client), \
             patch("fpl_cli.scraper.fpl_prices.load_cache", return_value=_make_finances()):
            result = CliRunner().invoke(sell_prices_command, ["--format", "json"], catch_exceptions=False)

        assert result.exit_code == 0, result.stderr
        envelope = json.loads(result.stdout)
        assert [row["sell_price"] for row in envelope["data"]] == [13.0, 14.5]
        [warning] = envelope["metadata"]["warnings"]
        assert warning["code"] == "sell_prices_as_scraped"
        assert warning["message"] == (
            f"Could not fetch today's prices ({detail}), so every sell price is as"
            " scraped and may be out of date."
        )

    def test_the_note_and_warning_reach_stderr_under_json_too(self, today):
        """The skills write the envelope to a file, so stderr is what they see."""
        finances = _make_finances()
        finances.squad[1].purchase_price = 0.0  # Haaland can no longer be checked
        today[:] = _today(salah=137, haaland=147)
        with patch("fpl_cli.scraper.fpl_prices.load_cache", return_value=finances):
            result = CliRunner().invoke(sell_prices_command, ["--format", "json"], catch_exceptions=False)

        stderr = result.stderr.replace("\n", " ")  # Rich wraps at the terminal width
        assert "1 sell price updated for price changes since the scrape: Salah" in stderr
        assert "1 of 2 sell prices could not be checked" in stderr
        assert json.loads(result.stdout)["command"] == "sell-prices"

    def test_a_cache_without_player_ids_is_refused_before_any_lookup(self):
        client = MagicMock()
        with patch("fpl_cli.api.fpl.FPLClient", return_value=client), \
             patch("fpl_cli.scraper.fpl_prices.load_cache", return_value=_make_finances(with_element_ids=False)):
            result = CliRunner().invoke(sell_prices_command, ["--format", "json"])

        assert result.exit_code == 1
        assert "player IDs" in json.loads(result.stdout)["error"]
        client.get_players.assert_not_called()
