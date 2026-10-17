"""Tests for fpl sell-prices command display logic."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import pytest
from click.testing import CliRunner

from fpl_cli.cli import main
from fpl_cli.cli._context import CLIContext, Format
from fpl_cli.cli.sell_prices import sell_prices_command
from fpl_cli.cli.squad import squad_group
from fpl_cli.models.player import PlayerPosition
from fpl_cli.scraper.fpl_prices import PlayerSellPrice, TeamFinances, ordered_squad
from tests.conftest import make_player

# Each player at the price `_make_finances` records them scraped at.
_TODAY = [
    make_player(id=355, code=223094, web_name="Haaland", position=PlayerPosition.FORWARD, now_cost=145),
    make_player(id=1, code=154561, web_name="Raya", position=PlayerPosition.GOALKEEPER, now_cost=59),
    make_player(id=253, code=118748, web_name="Salah", position=PlayerPosition.MIDFIELDER, now_cost=132),
]


@pytest.fixture(autouse=True)
def _prices_unchanged_since_the_scrape(todays_players):
    todays_players.extend(_TODAY)


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
            element_id=355,
            code=223094,
            price_at_scrape=14.5,
        ),
        PlayerSellPrice(
            name="Raya",
            sell_price=5.8,
            position="GKP",
            purchase_price=5.7 if with_purchase else 0.0,
            element_id=1,
            code=154561,
            price_at_scrape=5.9,
        ),
        PlayerSellPrice(
            name="Salah",
            sell_price=13.0,
            position="MID",
            purchase_price=12.8 if with_purchase else 0.0,
            element_id=253,
            code=118748,
            price_at_scrape=13.2,
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


class TestPriceChangesSinceTheScrape:
    """A cached sell price follows the player's price after the scrape (#426)."""

    def test_the_table_shows_todays_sell_price_and_stderr_says_which_moved(self, todays_players):
        # Raya: bought 5.7, scraped at 5.9 (sell 5.8), now 6.1, so half of 0.4.
        todays_players[1] = todays_players[1].model_copy(update={"now_cost": 61})
        with patch("fpl_cli.scraper.fpl_prices.load_cache", return_value=_make_finances(with_purchase=True)):
            result = CliRunner().invoke(sell_prices_command)

        assert result.exit_code == 0, result.output
        assert "\u00a35.9m" in result.stdout
        note = result.stderr.replace("\n", " ")  # Rich wraps at the terminal width
        assert "1 sell price updated for price changes since the scrape" in note
        assert "Raya \u00a35.8m \u2192 \u00a35.9m" in note
        assert "updated for price changes" not in result.stdout

    def test_a_price_it_cannot_check_is_warned_about_on_stderr(self, todays_players):
        # Haaland is missing from today's list, so his price cannot be looked up.
        del todays_players[0]
        with patch("fpl_cli.scraper.fpl_prices.load_cache", return_value=_make_finances()):
            result = CliRunner().invoke(sell_prices_command)

        assert result.exit_code == 0, result.output
        warning = result.stderr.replace("\n", " ")  # Rich wraps at the terminal width
        assert "1 of 3 sell prices could not be checked" in warning
        assert "Haaland" in warning
        assert "could not be checked" not in result.stdout


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
        for i, player in enumerate(finances.squad, start=1):
            player.code = 10_000 + i
        finances.squad = finances.squad * 5  # 15 players clears the heuristic
        assert not finances.is_suspect

        result = self._scrape(finances, ["--refresh", "--format", "json"])

        assert result.exit_code == 0, result.stderr
        envelope = json.loads(result.stdout)
        assert len(envelope["data"]) == 15
        assert envelope["metadata"]["warnings"] == []
        # Scraped just now, so the figure to budget on is FPL's own.
        assert all(row["scraped_sell_price"] == row["sell_price"] for row in envelope["data"])


class TestJsonSquadOrder:
    """A --refresh run and a later cached run must agree on squad order, or
    a consumer diffing successive JSON runs sees a spurious reshuffle on the
    first cached run after a refresh (#244 review)."""

    def test_refresh_json_order_matches_ordered_squad(self):
        squad = [
            PlayerSellPrice(name="Haaland", sell_price=14.5, position="FWD", element_id=355),
            PlayerSellPrice(name="Raya", sell_price=5.8, position="GKP", element_id=1),
            PlayerSellPrice(name="Salah", sell_price=13.0, position="MID", element_id=253),
        ] * 4  # 12 players clears the <11 suspect heuristic without changing composition
        finances = TeamFinances(bank=1.0, free_transfers=1, squad=squad, total_value=100.0)
        assert not finances.is_suspect

        with patch("fpl_cli.scraper.fpl_prices.FPLPriceScraper") as mock_scraper_cls, \
             patch("fpl_cli.scraper.fpl_prices.save_cache"), \
             patch("fpl_cli.scraper.fpl_prices.load_cache", return_value=None):
            mock_scraper_cls.return_value.scrape = AsyncMock(return_value=finances)
            result = CliRunner().invoke(sell_prices_command, ["--refresh", "--format", "json"])

        assert result.exit_code == 0, result.stderr
        live_order = [p["name"] for p in json.loads(result.stdout)["data"]]
        assert live_order == [p.name for p in ordered_squad(finances.squad)]
        # Position order (GK, DEF, MID, FWD), not the pick-slot order the squad was built in.
        assert live_order == ["Raya"] * 4 + ["Salah"] * 4 + ["Haaland"] * 4
