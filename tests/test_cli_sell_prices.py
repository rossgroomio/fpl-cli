"""Tests for fpl sell-prices command display logic."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

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
        # Nothing to show is a failure, not a silent success (#144).
        assert result.exit_code == 1
        assert "--refresh" in result.output


class TestRefreshFlag:
    def test_refresh_triggers_scrape(self):
        runner = CliRunner()
        finances = _make_finances()

        with patch("fpl_cli.scraper.fpl_prices.FPLPriceScraper") as mock_scraper_cls, \
             patch("fpl_cli.scraper.fpl_prices.save_cache"), \
             patch("fpl_cli.scraper.fpl_prices.load_cache"):
            mock_scraper = mock_scraper_cls.return_value
            # AsyncMock, not a bare return_value: awaiting a MagicMock raises,
            # which the command used to swallow into an exit-0 error message,
            # so this passed without a scrape ever succeeding.
            mock_scraper.scrape = AsyncMock(return_value=finances)
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
        assert result.exit_code == 1
        assert "not available in draft format" in result.output


class TestSummarySection:
    def test_shows_available_total(self):
        finances = _make_finances(bank=1.5)
        runner = CliRunner()
        with patch("fpl_cli.scraper.fpl_prices.load_cache", return_value=finances):
            result = runner.invoke(sell_prices_command)
        assert "Available:" in result.output
        assert "Free transfers:" in result.output


class TestProxyTroubleshootingHint:
    def test_shows_hint_on_connection_reset(self):
        runner = CliRunner()
        with patch("fpl_cli.scraper.fpl_prices.FPLPriceScraper") as mock_scraper_cls, \
             patch("fpl_cli.scraper.fpl_prices.load_cache"):
            mock_scraper_cls.return_value.scrape = AsyncMock(
                side_effect=Exception("net::ERR_CONNECTION_RESET at https://fantasy.premierleague.com/")
            )
            result = runner.invoke(sell_prices_command, ["--refresh"])
        assert result.exit_code == 1
        assert "TLS-inspecting proxy" in result.output
        assert "FPL_BROWSER_EXECUTABLE" in result.output

    def test_shows_hint_on_tunnel_connection_failed(self):
        runner = CliRunner()
        with patch("fpl_cli.scraper.fpl_prices.FPLPriceScraper") as mock_scraper_cls, \
             patch("fpl_cli.scraper.fpl_prices.load_cache"):
            mock_scraper_cls.return_value.scrape = AsyncMock(
                side_effect=Exception("net::ERR_TUNNEL_CONNECTION_FAILED")
            )
            result = runner.invoke(sell_prices_command, ["--refresh"])
        assert result.exit_code == 1
        assert "TLS-inspecting proxy" in result.output

    def test_shows_hint_on_ssl_protocol_error(self):
        """Broadened beyond the original two markers - a proxy can also surface this."""
        runner = CliRunner()
        with patch("fpl_cli.scraper.fpl_prices.FPLPriceScraper") as mock_scraper_cls, \
             patch("fpl_cli.scraper.fpl_prices.load_cache"):
            mock_scraper_cls.return_value.scrape = AsyncMock(
                side_effect=Exception("net::ERR_SSL_PROTOCOL_ERROR")
            )
            result = runner.invoke(sell_prices_command, ["--refresh"])
        assert result.exit_code == 1
        assert "TLS-inspecting proxy" in result.output

    def test_omits_hint_for_unrelated_error(self):
        runner = CliRunner()
        with patch("fpl_cli.scraper.fpl_prices.FPLPriceScraper") as mock_scraper_cls, \
             patch("fpl_cli.scraper.fpl_prices.load_cache"):
            mock_scraper_cls.return_value.scrape = AsyncMock(
                side_effect=Exception("FPL credentials required.")
            )
            result = runner.invoke(sell_prices_command, ["--refresh"])
        assert result.exit_code == 1
        assert "Troubleshooting" in result.output
        assert "TLS-inspecting proxy" not in result.output


class TestSaveMessage:
    def test_save_message_shows_actual_path(self, tmp_path, monkeypatch):
        """The save confirmation prints the real cache location, not a hardcoded path."""
        from fpl_cli.paths import user_data_dir

        data_dir = tmp_path / "fake-data-dir"
        monkeypatch.setenv("FPL_CLI_DATA_DIR", str(data_dir))
        user_data_dir.cache_clear()
        runner = CliRunner()
        finances = _make_finances()

        with patch("fpl_cli.scraper.fpl_prices.FPLPriceScraper") as mock_scraper_cls, \
             patch("fpl_cli.scraper.fpl_prices.save_cache"), \
             patch("fpl_cli.scraper.fpl_prices.load_cache"):
            mock_scraper_cls.return_value.scrape = AsyncMock(return_value=finances)
            result = runner.invoke(sell_prices_command, ["--refresh"], catch_exceptions=False)

        assert result.exit_code == 0
        # Rich soft-wraps long paths; compare against the unwrapped output.
        assert str(data_dir / "team_finances.json") in result.output.replace("\n", "")

    def test_save_message_keeps_bracketed_path_intact(self, tmp_path, monkeypatch):
        """A data dir containing [brackets] must not be eaten as rich markup."""
        from fpl_cli.paths import user_data_dir

        data_dir = tmp_path / "[vault]" / "fpl-data"
        monkeypatch.setenv("FPL_CLI_DATA_DIR", str(data_dir))
        user_data_dir.cache_clear()
        runner = CliRunner()
        finances = _make_finances()

        with patch("fpl_cli.scraper.fpl_prices.FPLPriceScraper") as mock_scraper_cls, \
             patch("fpl_cli.scraper.fpl_prices.save_cache"), \
             patch("fpl_cli.scraper.fpl_prices.load_cache"):
            mock_scraper_cls.return_value.scrape = AsyncMock(return_value=finances)
            result = runner.invoke(sell_prices_command, ["--refresh"], catch_exceptions=False)

        assert result.exit_code == 0
        assert "[vault]" in result.output.replace("\n", "")


class TestJsonFailurePaths:
    """A --format json run reports on stdout or not at all (#140, #144)."""

    def test_no_cache_is_an_error_envelope(self):
        with patch("fpl_cli.scraper.fpl_prices.load_cache", return_value=None):
            result = CliRunner().invoke(sell_prices_command, ["--format", "json"])

        assert result.exit_code == 1
        assert json.loads(result.stdout)["command"] == "sell-prices"
        assert "--refresh" in json.loads(result.stdout)["error"]

    def test_a_failed_scrape_is_an_error_envelope_with_hints_on_stderr(self):
        with patch("fpl_cli.scraper.fpl_prices.FPLPriceScraper") as mock_scraper_cls, \
             patch("fpl_cli.scraper.fpl_prices.load_cache"):
            mock_scraper_cls.return_value.scrape = AsyncMock(
                side_effect=Exception("net::ERR_CONNECTION_RESET")
            )
            result = CliRunner().invoke(sell_prices_command, ["--refresh", "--format", "json"])

        assert result.exit_code == 1
        assert "ERR_CONNECTION_RESET" in json.loads(result.stdout)["error"]
        # The hints are for a human, so they belong on the other stream.
        assert "Troubleshooting" in result.stderr
        assert "Troubleshooting" not in result.stdout

    def _scrape(self, finances, args):
        for player in finances.squad:
            player.element_id = 1  # past the DOM-fallback check, to the suspect one
        with patch("fpl_cli.scraper.fpl_prices.FPLPriceScraper") as mock_scraper_cls, \
             patch("fpl_cli.scraper.fpl_prices.save_cache"), \
             patch("fpl_cli.scraper.fpl_prices.load_cache", return_value=None):
            mock_scraper_cls.return_value.scrape = AsyncMock(return_value=finances)
            return CliRunner().invoke(sell_prices_command, args)

    def test_suspect_data_is_refused_rather_than_handed_over(self):
        """`fpl allocate --sell-prices` budgets off `data` and never reads
        metadata, so a warning would not reach the one consumer this flag
        exists for. It gets the refusal instead."""
        finances = _make_finances()  # 3 players: the extraction heuristics distrust it
        assert finances.is_suspect

        result = self._scrape(finances, ["--refresh", "--format", "json"])

        assert result.exit_code == 1
        envelope = json.loads(result.stdout)
        assert "data" not in envelope
        assert "suspect data" in envelope["error"]
        assert "3 players" in envelope["error"]

    def test_suspect_data_still_reaches_a_terminal(self):
        """A human can see the label and count the rows, so the table keeps it."""
        finances = _make_finances()
        result = self._scrape(finances, ["--refresh"])

        assert result.exit_code == 0, result.output
        assert "Squad Budget (Suspect)" in result.output
        assert "Haaland" in result.output

    def test_a_trustworthy_scrape_comes_back_as_data(self):
        finances = _make_finances()
        finances.squad = finances.squad * 5  # 15 players clears the heuristic
        assert not finances.is_suspect

        result = self._scrape(finances, ["--refresh", "--format", "json"])

        assert result.exit_code == 0, result.stderr
        envelope = json.loads(result.stdout)
        assert len(envelope["data"]) == 15
        assert "warnings" not in envelope["metadata"]
