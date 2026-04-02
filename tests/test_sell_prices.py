"""Tests for the sell-prices CLI command."""

from __future__ import annotations

import json
from unittest.mock import patch

from click.testing import CliRunner

from fpl_cli.cli.sell_prices import sell_prices_command
from fpl_cli.scraper.fpl_prices import PlayerSellPrice, TeamFinances


def _make_finances(*, with_element_ids: bool = True) -> TeamFinances:
    squad = [
        PlayerSellPrice(
            name="Salah", sell_price=13.0, position="MID",
            purchase_price=12.5, element_id=253 if with_element_ids else None,
        ),
        PlayerSellPrice(
            name="Haaland", sell_price=14.5, position="FWD",
            purchase_price=15.0, element_id=355 if with_element_ids else None,
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
        assert envelope["data"][0]["sell_price"] == 13.0
        assert envelope["metadata"]["bank"] == 1.5
        assert envelope["metadata"]["total_sell_value"] == 27.5
        assert envelope["metadata"]["free_transfers"] == 2

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
