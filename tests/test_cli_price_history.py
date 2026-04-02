"""Tests for `fpl price-history` command."""
from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
from click.testing import CliRunner

from fpl_cli.api.vaastav import GwTrendProfile
from fpl_cli.cli import main
from fpl_cli.models.player import PlayerPosition
from tests.conftest import make_player, make_team


def _make_trend(
    *,
    element=100, web_name="Salah", position="MID", team_name="Liverpool",
    price_start=130, price_current=135, price_change=5,
    price_slope=1.0, price_acceleration=0.1,
    transfer_momentum=50000,
    gw_count=6, latest_gw=6, first_gw=1,
) -> GwTrendProfile:
    return GwTrendProfile(
        element=element, web_name=web_name, position=position,
        team_name=team_name, price_start=price_start,
        price_current=price_current, price_change=price_change,
        price_slope=price_slope, price_acceleration=price_acceleration,
        transfer_momentum=transfer_momentum,
        gw_count=gw_count, latest_gw=latest_gw, first_gw=first_gw,
    )


def _sample_trends():
    return {
        100: _make_trend(
            element=100, web_name="Salah", position="MID", team_name="Liverpool",
            price_change=5, price_slope=1.0,
        ),
        200: _make_trend(
            element=200, web_name="Haaland", position="FWD", team_name="Manchester City",
            price_start=150, price_current=147, price_change=-3,
            price_slope=-0.5, transfer_momentum=-20000,
        ),
        300: _make_trend(
            element=300, web_name="Alexander-Arnold", position="DEF", team_name="Liverpool",
            price_start=85, price_current=87, price_change=2,
            price_slope=0.3, transfer_momentum=10000,
        ),
    }


def _sample_players():
    return [
        make_player(
            id=100, web_name="Salah", team_id=14,
            position=PlayerPosition.MIDFIELDER, now_cost=135,
            cost_change_start=5,
        ),
        make_player(
            id=200, web_name="Haaland", team_id=13,
            position=PlayerPosition.FORWARD, now_cost=147,
            cost_change_start=-3,
        ),
        make_player(
            id=300, web_name="Alexander-Arnold", team_id=14,
            position=PlayerPosition.DEFENDER, now_cost=87,
            cost_change_start=2,
        ),
    ]


def _sample_teams():
    return [
        make_team(id=14, name="Liverpool", short_name="LIV"),
        make_team(id=13, name="Manchester City", short_name="MCI"),
    ]


def _make_clients(trends=None, players=None, teams=None, current_gw=6):
    fpl = MagicMock()
    fpl.get_players = AsyncMock(return_value=players or [])
    fpl.get_teams = AsyncMock(return_value=teams or [])
    fpl.get_current_gameweek = AsyncMock(return_value={"id": current_gw})
    fpl.__aenter__ = AsyncMock(return_value=fpl)
    fpl.__aexit__ = AsyncMock(return_value=False)

    vaastav = MagicMock()
    vaastav.get_gw_trends = AsyncMock(return_value=trends or {})
    vaastav.__aenter__ = AsyncMock(return_value=vaastav)
    vaastav.__aexit__ = AsyncMock(return_value=False)

    return fpl, vaastav


def _run(args=None, trends=None, players=None, teams=None, current_gw=6):
    fpl, vaastav = _make_clients(
        trends=_sample_trends() if trends is None else trends,
        players=players or _sample_players(),
        teams=teams or _sample_teams(),
        current_gw=current_gw,
    )
    runner = CliRunner()
    with (
        patch("fpl_cli.api.fpl.FPLClient", return_value=fpl),
        patch("fpl_cli.api.vaastav.VaastavClient", return_value=vaastav),
    ):
        return runner.invoke(main, ["price-history"] + (args or []))


class TestPriceHistoryDefault:
    def test_default_output_shows_all_players(self):
        result = _run()
        assert result.exit_code == 0, result.output
        assert "Salah" in result.output
        assert "Haaland" in result.output
        # Rich may truncate long names; check for prefix
        assert "Alexander" in result.output

    def test_default_sorted_by_price_change_descending(self):
        result = _run()
        assert result.exit_code == 0, result.output
        salah_pos = result.output.index("Salah")
        taa_pos = result.output.index("Alexander")
        haaland_pos = result.output.index("Haaland")
        assert salah_pos < taa_pos < haaland_pos

    def test_table_shows_core_columns(self):
        result = _run()
        assert result.exit_code == 0, result.output
        assert "Pos" in result.output
        assert "Team" in result.output
        assert "GW1" in result.output
        assert "Now" in result.output
        assert "Trend" in result.output
        assert "Momentum" in result.output


class TestPriceHistoryFilters:
    def test_position_filter(self):
        result = _run(["--position", "MID"])
        assert result.exit_code == 0, result.output
        assert "Salah" in result.output
        assert "Haaland" not in result.output
        assert "Alexander-Arnold" not in result.output

    def test_team_filter(self):
        result = _run(["--team", "LIV"])
        assert result.exit_code == 0, result.output
        assert "Salah" in result.output
        assert "Alexander" in result.output
        assert "Haaland" not in result.output

    def test_team_filter_case_insensitive(self):
        result = _run(["--team", "liv"])
        assert result.exit_code == 0, result.output
        assert "Salah" in result.output

    def test_invalid_team_shows_valid_options(self):
        result = _run(["--team", "XYZ"])
        assert result.exit_code != 0
        assert "LIV" in result.output
        assert "MCI" in result.output

    def test_limit_option(self):
        result = _run(["--limit", "1"])
        assert result.exit_code == 0, result.output
        assert "Salah" in result.output
        assert "Haaland" not in result.output

    def test_no_results_shows_message(self):
        result = _run(["--position", "GK"])
        assert result.exit_code == 0
        assert "No players match" in result.output


class TestPriceHistorySort:
    def test_sort_by_transfer_momentum(self):
        result = _run(["--sort", "transfer_momentum"])
        assert result.exit_code == 0, result.output
        salah_pos = result.output.index("Salah")
        haaland_pos = result.output.index("Haaland")
        assert salah_pos < haaland_pos

    def test_sort_by_price_current(self):
        result = _run(["--sort", "price_current"])
        assert result.exit_code == 0, result.output
        # Haaland (147) > Salah (135) > TAA (87) descending
        haaland_pos = result.output.index("Haaland")
        salah_pos = result.output.index("Salah")
        assert haaland_pos < salah_pos

    def test_reverse_flag(self):
        result = _run(["--sort", "price_change", "--reverse"])
        assert result.exit_code == 0, result.output
        haaland_pos = result.output.index("Haaland")
        salah_pos = result.output.index("Salah")
        assert haaland_pos < salah_pos


class TestPriceHistoryStaleness:
    def test_stale_data_shows_warning(self):
        result = _run(current_gw=20)
        assert result.exit_code == 0, result.output
        assert "Warning" in result.output
        assert "GW1-6" in result.output
        assert "GW20" in result.output

    def test_stale_data_hides_trend_columns(self):
        result = _run(current_gw=20)
        assert result.exit_code == 0, result.output
        # Table header row should not contain trend columns
        # (the word "Trend" may appear in the warning banner, so check table structure)
        assert "Accel" not in result.output
        assert "Momentum" not in result.output

    def test_fresh_data_no_warning(self):
        result = _run(current_gw=7)
        assert result.exit_code == 0, result.output
        assert "Warning" not in result.output


class TestPriceHistoryJson:
    def test_json_output_is_valid(self):
        result = _run(["--format", "json"])
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert data["command"] == "price-history"
        assert "metadata" in data
        assert "data" in data

    def test_json_metadata_fields(self):
        result = _run(["--format", "json"])
        data = json.loads(result.output)
        meta = data["metadata"]
        assert meta["latest_gw"] == 6
        assert meta["current_gw"] == 6
        assert meta["is_stale"] is False

    def test_json_stale_metadata(self):
        result = _run(["--format", "json"], current_gw=20)
        data = json.loads(result.output)
        meta = data["metadata"]
        assert meta["is_stale"] is True

    def test_json_stale_nulls_trend_fields(self):
        result = _run(["--format", "json"], current_gw=20)
        data = json.loads(result.output)
        player = data["data"][0]
        assert player["price_slope"] is None
        assert player["price_acceleration"] is None
        assert player["transfer_momentum"] is None

    def test_json_player_fields(self):
        result = _run(["--format", "json"])
        data = json.loads(result.output)
        player = data["data"][0]
        required = {
            "element", "web_name", "position", "team",
            "price_start", "price_current", "price_change",
            "price_slope", "price_acceleration", "transfer_momentum",
            "gw_count", "latest_gw",
        }
        assert required.issubset(player.keys())

    def test_json_position_filter(self):
        result = _run(["--position", "FWD", "--format", "json"])
        data = json.loads(result.output)
        assert all(p["position"] == "FWD" for p in data["data"])
        assert len(data["data"]) == 1

    def test_json_limit(self):
        result = _run(["--limit", "2", "--format", "json"])
        data = json.loads(result.output)
        assert len(data["data"]) == 2


class TestPriceHistoryErrors:
    def test_empty_trends_table(self):
        result = _run(trends={})
        assert result.exit_code == 0
        assert "No price history data" in result.output

    def test_empty_trends_json(self):
        result = _run(["--format", "json"], trends={})
        data = json.loads(result.output)
        assert data["data"] == []
        assert data["metadata"]["is_stale"] is True

    def test_fetch_failure_table(self):
        fpl, vaastav = _make_clients()
        vaastav.get_gw_trends = AsyncMock(side_effect=httpx.HTTPError("Network error"))
        runner = CliRunner()
        with (
            patch("fpl_cli.api.fpl.FPLClient", return_value=fpl),
            patch("fpl_cli.api.vaastav.VaastavClient", return_value=vaastav),
        ):
            result = runner.invoke(main, ["price-history"])
        assert result.exit_code != 0
        assert "Failed to fetch" in result.output

    def test_fetch_failure_json(self):
        fpl, vaastav = _make_clients()
        vaastav.get_gw_trends = AsyncMock(side_effect=httpx.HTTPError("Network error"))
        runner = CliRunner()
        with (
            patch("fpl_cli.api.fpl.FPLClient", return_value=fpl),
            patch("fpl_cli.api.vaastav.VaastavClient", return_value=vaastav),
        ):
            result = runner.invoke(main, ["price-history", "--format", "json"])
        assert result.exit_code != 0
        data = json.loads(result.output)
        assert data["command"] == "price-history"
        assert "error" in data


class TestPriceHistoryLastN:
    def test_last_n_passes_to_get_gw_trends(self):
        fpl, vaastav = _make_clients(
            trends=_sample_trends(),
            players=_sample_players(),
            teams=_sample_teams(),
        )
        runner = CliRunner()
        with (
            patch("fpl_cli.api.fpl.FPLClient", return_value=fpl),
            patch("fpl_cli.api.vaastav.VaastavClient", return_value=vaastav),
        ):
            result = runner.invoke(main, ["price-history", "--last-n", "4"])
        assert result.exit_code == 0, result.output
        vaastav.get_gw_trends.assert_called_once_with(last_n=4)

    def test_default_passes_none_to_get_gw_trends(self):
        fpl, vaastav = _make_clients(
            trends=_sample_trends(),
            players=_sample_players(),
            teams=_sample_teams(),
        )
        runner = CliRunner()
        with (
            patch("fpl_cli.api.fpl.FPLClient", return_value=fpl),
            patch("fpl_cli.api.vaastav.VaastavClient", return_value=vaastav),
        ):
            result = runner.invoke(main, ["price-history"])
        assert result.exit_code == 0, result.output
        vaastav.get_gw_trends.assert_called_once_with(last_n=None)

    def test_json_window_used_with_last_n(self):
        result = _run(["--last-n", "4", "--format", "json"])
        data = json.loads(result.output)
        assert data["metadata"]["window_used"] == 4

    def test_json_window_used_without_last_n(self):
        result = _run(["--format", "json"])
        data = json.loads(result.output)
        assert data["metadata"]["window_used"] is None

    def test_table_gw_label_default(self):
        result = _run()
        assert result.exit_code == 0, result.output
        assert "GW1" in result.output

    def test_momentum_column_net_transfers_when_windowed(self):
        result = _run(["--last-n", "4"])
        assert result.exit_code == 0, result.output
        # Rich may wrap "Net Transfers" across lines in narrow terminals
        assert "Transfers" in result.output
        assert "Momentum" not in result.output

    def test_momentum_column_momentum_when_not_windowed(self):
        result = _run()
        assert result.exit_code == 0, result.output
        assert "Momentum" in result.output

    def test_scoped_note_when_windowed(self):
        result = _run(["--last-n", "4"])
        assert result.exit_code == 0, result.output
        assert "Metrics scoped to last 4 GWs" in result.output

    def test_last_n_minimum_enforced(self):
        result = _run(["--last-n", "2"])
        assert result.exit_code != 0
        assert "2" in result.output or "Invalid" in result.output or "Range" in result.output
