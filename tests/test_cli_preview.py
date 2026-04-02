"""Tests for custom_analysis toggle on preview command."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

from click.testing import CliRunner

from fpl_cli.cli.preview import preview_command


def _make_fpl_client(gw=25):
    """Minimal FPLClient mock for preview tests."""
    client = MagicMock()
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)
    client.get_next_gameweek = AsyncMock(return_value={"id": gw, "deadline_time": "2026-04-05T11:00:00Z"})
    client.get_players = AsyncMock(return_value=[])
    client.get_teams = AsyncMock(return_value=[])
    client.get_fixtures = AsyncMock(return_value=[])
    return client


def _make_agent(success=True, data=None):
    """Create a mock agent with context-manager support."""
    agent = MagicMock()
    agent.__aenter__ = AsyncMock(return_value=agent)
    agent.__aexit__ = AsyncMock(return_value=False)
    result = MagicMock()
    result.success = success
    result.data = data or {}
    result.message = "" if success else "failed"
    agent.run = AsyncMock(return_value=result)
    return agent


def _run_preview(custom_analysis=True, fixture_data=None, stats_data=None):
    """Invoke preview with mocked agents and toggle control."""
    fpl_client = _make_fpl_client()

    fixture_agent = _make_agent(data=fixture_data or {
        "easy_fixture_runs": {
            "overall": [
                {
                    "short_name": "ARS",
                    "average_fdr": 2.1,
                    "average_fdr_atk": 1.9,
                    "average_fdr_def": 2.3,
                    "fixtures_summary": "bou(H), MCI(A), new(H)",
                },
            ],
        },
        "team_form": [],
    })

    stats_agent = _make_agent(data=stats_data or {
        "top_xgi_per_90": [
            {"player_name": "Haaland", "team_short": "MCI", "xG": 12.5, "xA": 3.2,
             "xGI_per_90": 0.95, "goals": 15, "assists": 4},
        ],
        "underperformers": [],
        "value_picks": [
            {"player_name": "Mbeumo", "team_short": "BRE", "price": 6.5,
             "ownership": 8.2, "xGI_per_90": 0.72},
        ],
        "window_label": "last 6 GWs",
    })

    price_agent = _make_agent(data={})

    settings = {"custom_analysis": custom_analysis}

    runner = CliRunner()
    with (
        patch("fpl_cli.cli.preview.is_custom_analysis_enabled", return_value=custom_analysis),
        patch("fpl_cli.cli.preview.load_settings", return_value=settings),
        patch("fpl_cli.api.fpl.FPLClient", return_value=fpl_client),
        patch("fpl_cli.agents.data.fixture.FixtureAgent", return_value=fixture_agent),
        patch("fpl_cli.agents.analysis.stats.StatsAgent", return_value=stats_agent),
        patch("fpl_cli.agents.data.price.PriceAgent", return_value=price_agent),
    ):
        return runner.invoke(preview_command, [])


class TestPreviewCustomAnalysisToggle:
    """Tests for custom_analysis toggle on preview display sections."""

    def test_toggle_off_no_atk_def_columns(self):
        """When toggle off, ATK/DEF columns absent from easy fixtures table."""
        result = _run_preview(custom_analysis=False)
        assert result.exit_code == 0, result.output
        # ATK/DEF should not appear as column headers
        lines = result.output.split("\n")
        header_lines = [line for line in lines if "ATK" in line or "DEF" in line]
        assert len(header_lines) == 0
        # Team name and Avg FDR should still appear
        assert "ARS" in result.output

    def test_toggle_on_has_atk_def_columns(self):
        """When toggle on, ATK/DEF columns present (no regression)."""
        result = _run_preview(custom_analysis=True)
        assert result.exit_code == 0, result.output
        assert "ATK" in result.output
        assert "DEF" in result.output

    def test_toggle_off_no_value_picks(self):
        """When toggle off, Value Picks section absent from performance stats."""
        result = _run_preview(custom_analysis=False)
        assert result.exit_code == 0, result.output
        assert "Value Picks" not in result.output
        assert "Mbeumo" not in result.output

    def test_toggle_on_has_value_picks(self):
        """When toggle on, Value Picks section present (no regression)."""
        result = _run_preview(custom_analysis=True)
        assert result.exit_code == 0, result.output
        assert "Value Picks" in result.output or "Mbeumo" in result.output

    def test_toggle_off_raw_data_still_shown(self):
        """When toggle off, raw data sections (xGI/90, underperformers) still shown."""
        result = _run_preview(custom_analysis=False)
        assert result.exit_code == 0, result.output
        assert "Haaland" in result.output
        assert "xGI/90" in result.output
