"""Tests for `fpl price-changes` command exit-code behaviour (#47)."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

from click.testing import CliRunner

from fpl_cli.cli import main


def _make_agent_result(success=True, message=""):
    result = MagicMock()
    result.success = success
    result.data = {
        "risers_this_gw": [
            {"name": "Haaland", "team": "MCI", "current_price": 15.1, "change_this_gw": 0.1},
        ],
        "fallers_this_gw": [],
        "hot_transfers_in": [
            {"name": "Haaland", "team": "MCI", "transfers_in": 100000, "net_transfers": 90000},
        ],
        "hot_transfers_out": [
            {"name": "Wilson", "team": "NEW", "transfers_out": 50000, "net_transfers": -40000},
        ],
    }
    result.message = message
    result.errors = ["API timeout"] if not success else []
    return result


def _run_price_changes(agent_result=None):
    runner = CliRunner()
    if agent_result is None:
        agent_result = _make_agent_result()

    mock_agent = MagicMock()
    mock_agent.run = AsyncMock(return_value=agent_result)
    mock_agent.__aenter__ = AsyncMock(return_value=mock_agent)
    mock_agent.__aexit__ = AsyncMock(return_value=False)

    with patch("fpl_cli.agents.data.price.PriceAgent", return_value=mock_agent):
        return runner.invoke(main, ["price-changes"])


class TestPriceChanges:
    def test_success_exits_zero(self):
        result = _run_price_changes()
        assert result.exit_code == 0, result.output
        assert "Price Change Analysis" in result.output
        assert "Haaland" in result.output

    def test_agent_failure_exits_nonzero(self):
        """Agent failure must exit nonzero, not just print and succeed (#47)."""
        agent_result = _make_agent_result(success=False, message="API timeout")
        result = _run_price_changes(agent_result=agent_result)
        assert result.exit_code == 1
        assert "Agent failed" in result.output
