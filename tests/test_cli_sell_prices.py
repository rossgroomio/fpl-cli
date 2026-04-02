"""Tests for fpl sell-prices command display logic."""

from __future__ import annotations

from unittest.mock import patch

from click.testing import CliRunner

from fpl_cli.cli import main
from fpl_cli.cli._context import CLIContext, Format
from fpl_cli.cli.sell_prices import sell_prices_command
from fpl_cli.cli.squad import squad_group
from fpl_cli.scraper.fpl_prices import PlayerSellPrice, TeamFinances


def _make_finances(
    with_purchase: bool = False,
    bank: float = 1.5,
    free_transfers: int = 2,
    scraped_at: str = "2026-03-24T10:00:00",
) -> TeamFinances:
    squad = [
        PlayerSellPrice(
            name="Haaland",
            sell_price=14.5,
            position="FWD",
            purchase_price=14.6 if with_purchase else 0.0,
        ),
        PlayerSellPrice(
            name="Raya",
            sell_price=5.8,
            position="GKP",
            purchase_price=5.7 if with_purchase else 0.0,
        ),
        PlayerSellPrice(
            name="Salah",
            sell_price=13.0,
            position="MID",
            purchase_price=12.8 if with_purchase else 0.0,
        ),
    ]
    return TeamFinances(
        bank=bank,
        free_transfers=free_transfers,
        squad=squad,
        total_value=sum(p.sell_price for p in squad) + bank,
        scraped_at=scraped_at,
    )


class TestCachedDisplay:
    def test_shows_budget_table_from_cache(self):
        finances = _make_finances()
        runner = CliRunner()
        with patch("fpl_cli.scraper.fpl_prices.load_cache", return_value=finances):
            result = runner.invoke(sell_prices_command)
        assert result.exit_code == 0
        assert "Squad Budget" in result.output
        assert "Haaland" in result.output
        assert "Raya" in result.output
        assert "Selling value:" in result.output
        assert "In the bank:" in result.output

    def test_shows_pl_columns_when_purchase_data(self):
        finances = _make_finances(with_purchase=True)
        runner = CliRunner()
        with patch("fpl_cli.scraper.fpl_prices.load_cache", return_value=finances):
            result = runner.invoke(sell_prices_command)
        assert result.exit_code == 0
        assert "Buy" in result.output
        assert "P/L" in result.output

    def test_no_pl_columns_without_purchase_data(self):
        finances = _make_finances(with_purchase=False)
        runner = CliRunner()
        with patch("fpl_cli.scraper.fpl_prices.load_cache", return_value=finances):
            result = runner.invoke(sell_prices_command)
        assert result.exit_code == 0
        assert "P/L" not in result.output

    def test_shows_cache_timestamp(self):
        finances = _make_finances()
        runner = CliRunner()
        with patch("fpl_cli.scraper.fpl_prices.load_cache", return_value=finances):
            result = runner.invoke(sell_prices_command)
        assert result.exit_code == 0
        assert "Data from" in result.output

    def test_sorted_by_position_then_name(self):
        finances = _make_finances()
        runner = CliRunner()
        with patch("fpl_cli.scraper.fpl_prices.load_cache", return_value=finances):
            result = runner.invoke(sell_prices_command)
        lines = result.output.split("\n")
        player_lines = [l for l in lines if any(p in l for p in ["Raya", "Salah", "Haaland"])]
        assert len(player_lines) == 3
        # GKP before MID before FWD
        raya_idx = next(i for i, l in enumerate(player_lines) if "Raya" in l)
        salah_idx = next(i for i, l in enumerate(player_lines) if "Salah" in l)
        haaland_idx = next(i for i, l in enumerate(player_lines) if "Haaland" in l)
        assert raya_idx < salah_idx < haaland_idx


class TestNoCache:
    def test_no_cache_prompts_refresh(self):
        runner = CliRunner()
        with patch("fpl_cli.scraper.fpl_prices.load_cache", return_value=None):
            result = runner.invoke(sell_prices_command)
        assert result.exit_code == 0
        assert "--refresh" in result.output


class TestRefreshFlag:
    def test_refresh_triggers_scrape(self):
        runner = CliRunner()
        finances = _make_finances()

        with patch("fpl_cli.scraper.fpl_prices.FPLPriceScraper") as mock_scraper_cls, \
             patch("fpl_cli.scraper.fpl_prices.save_cache"), \
             patch("fpl_cli.scraper.fpl_prices.load_cache"):
            mock_scraper = mock_scraper_cls.return_value
            mock_scraper.scrape.return_value = finances
            result = runner.invoke(sell_prices_command, ["--refresh"])

        assert result.exit_code == 0
        assert "Scraping FPL transfers page" in result.output
        mock_scraper.scrape.assert_called_once()


class TestRouting:
    def test_accessible_via_squad_group(self):
        finances = _make_finances()
        runner = CliRunner()
        with patch("fpl_cli.scraper.fpl_prices.load_cache", return_value=finances):
            result = runner.invoke(squad_group, ["sell-prices"])
        assert result.exit_code == 0
        assert "Squad Budget" in result.output

    def test_not_accessible_at_top_level(self):
        runner = CliRunner()
        result = runner.invoke(main, ["sell-prices"])
        assert result.exit_code != 0
        assert "No such command" in result.output

    def test_not_available_in_draft_mode(self):
        runner = CliRunner()
        ctx_obj = CLIContext(format=Format.DRAFT, settings={})
        result = runner.invoke(squad_group, ["sell-prices"], obj=ctx_obj)
        assert result.exit_code == 0
        assert "not available in draft format" in result.output


class TestSummarySection:
    def test_shows_available_total(self):
        finances = _make_finances(bank=1.5)
        runner = CliRunner()
        with patch("fpl_cli.scraper.fpl_prices.load_cache", return_value=finances):
            result = runner.invoke(sell_prices_command)
        assert "Available:" in result.output
        assert "Free transfers:" in result.output
