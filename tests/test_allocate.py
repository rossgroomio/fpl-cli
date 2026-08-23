"""Tests for the allocate CLI command."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest
from click.testing import CliRunner

from fpl_cli.cli.allocate import allocate_command, load_sell_prices


def _make_sell_prices_json(
    players: list[dict] | None = None,
    bank: float = 1.5,
) -> str:
    """Write a sell-prices JSON file and return the path."""
    if players is None:
        players = [
            {"id": 1, "name": "Salah", "position": "MID", "sell_price": 12.5},
            {"id": 2, "name": "Haaland", "position": "FWD", "sell_price": 14.0},
        ]
    envelope = {
        "command": "sell-prices",
        "data": players,
        "metadata": {"bank": bank, "total_sell_value": sum(p["sell_price"] for p in players)},
    }
    f = tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False)
    json.dump(envelope, f)
    f.close()
    return f.name


class TestAllocateSellPricesFlag:
    def test_malformed_json_exits_with_error(self):
        """Malformed JSON in sell-prices file exits with error."""
        f = tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False)
        f.write("not valid json{{{")
        f.close()

        runner = CliRunner()
        result = runner.invoke(allocate_command, ["--sell-prices", f.name])
        assert result.exit_code == 1

        Path(f.name).unlink()

    def test_missing_field_in_sell_prices_json(self):
        """Sell-prices JSON with missing 'id' field exits with clear error."""
        envelope = {
            "command": "sell-prices",
            "data": [{"name": "Salah", "sell_price": 13.0}],  # missing 'id'
            "metadata": {"bank": 1.0},
        }
        f = tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False)
        json.dump(envelope, f)
        f.close()

        runner = CliRunner()
        result = runner.invoke(allocate_command, ["--sell-prices", f.name])
        assert result.exit_code == 1

        Path(f.name).unlink()


class TestLoadSellPrices:
    """Budget resolution and price-override parsing behind --sell-prices."""

    def test_no_file_leaves_budget_untouched(self):
        """Without --sell-prices there are no overrides and the budget passes through."""
        overrides, budget = load_sell_prices(None, 100.0, budget_from_command_line=False)

        assert overrides is None
        assert budget == 100.0

    def test_budget_auto_computed_from_json(self):
        """Budget auto-computed as sum(sell_prices) + bank when --budget not set."""
        path = _make_sell_prices_json(
            players=[
                {"id": 1, "name": "A", "position": "MID", "sell_price": 10.0},
                {"id": 2, "name": "B", "position": "FWD", "sell_price": 5.0},
            ],
            bank=2.0,
        )

        overrides, budget = load_sell_prices(path, 100.0, budget_from_command_line=False)

        assert budget == pytest.approx(17.0)  # 10.0 + 5.0 + 2.0 bank
        assert overrides == {1: 10.0, 2: 5.0}

        Path(path).unlink()

    def test_explicit_budget_overrides_auto_compute(self):
        """Explicit --budget flag overrides auto-computed value."""
        path = _make_sell_prices_json(
            players=[{"id": 1, "name": "A", "position": "MID", "sell_price": 50.0}],
            bank=10.0,
        )

        overrides, budget = load_sell_prices(path, 80.0, budget_from_command_line=True)

        assert budget == pytest.approx(80.0)  # not the auto-computed 60.0
        assert overrides == {1: 50.0}

        Path(path).unlink()

    def test_missing_bank_defaults_to_zero(self):
        """Sell-prices JSON without a bank entry treats the bank as empty."""
        envelope = {
            "command": "sell-prices",
            "data": [{"id": 1, "name": "A", "position": "MID", "sell_price": 7.5}],
            "metadata": {},
        }
        f = tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False)
        json.dump(envelope, f)
        f.close()

        _, budget = load_sell_prices(f.name, 100.0, budget_from_command_line=False)

        assert budget == pytest.approx(7.5)

        Path(f.name).unlink()

    def test_malformed_json_raises_system_exit(self):
        """Malformed JSON surfaces as SystemExit(1) rather than a parse error."""
        f = tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False)
        f.write("not valid json{{{")
        f.close()

        with pytest.raises(SystemExit) as exc_info:
            load_sell_prices(f.name, 100.0, budget_from_command_line=False)

        assert exc_info.value.code == 1

        Path(f.name).unlink()

    def test_missing_required_field_raises_system_exit(self):
        """A player entry missing 'id' surfaces as SystemExit(1)."""
        envelope = {
            "command": "sell-prices",
            "data": [{"name": "Salah", "sell_price": 13.0}],  # missing 'id'
            "metadata": {"bank": 1.0},
        }
        f = tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False)
        json.dump(envelope, f)
        f.close()

        with pytest.raises(SystemExit) as exc_info:
            load_sell_prices(f.name, 100.0, budget_from_command_line=False)

        assert exc_info.value.code == 1

        Path(f.name).unlink()
