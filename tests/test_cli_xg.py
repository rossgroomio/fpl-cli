"""Tests for `fpl xg` JSON output."""
from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

from click.testing import CliRunner

from fpl_cli.cli import main


def _make_agent_result(success=True, data=None, message=""):
    """Create a mock AgentResult."""
    result = MagicMock()
    result.success = success
    result.data = data or {
        "top_xgi_per_90": [
            {
                "player_name": "Haaland",
                "team_short": "MCI",
                "xG": 12.5,
                "xA": 3.2,
                "xGI_per_90": 0.95,
                "goals": 15,
                "assists": 4,
            }
        ],
        "underperformers": [
            {
                "player_name": "Saka",
                "team_short": "ARS",
                "GI": 8,
                "xGI": 12.3,
                "difference": 4.3,
            }
        ],
        "value_picks": [
            {
                "player_name": "Mbeumo",
                "team_short": "BRE",
                "price": 6.5,
                "ownership": 8.2,
                "xGI_per_90": 0.72,
            }
        ],
        "window_label": "last 6 GWs",
    }
    result.message = message
    result.errors = ["Something went wrong"] if not success else []
    return result


def _run_xg(args=None, agent_result=None, custom_analysis=True):
    runner = CliRunner()
    if agent_result is None:
        agent_result = _make_agent_result()

    mock_agent = MagicMock()
    mock_agent.run = AsyncMock(return_value=agent_result)
    mock_agent.__aenter__ = AsyncMock(return_value=mock_agent)
    mock_agent.__aexit__ = AsyncMock(return_value=False)

    with (
        patch("fpl_cli.agents.analysis.stats.StatsAgent", return_value=mock_agent),
        patch("fpl_cli.cli.xg.custom_analysis_enabled", return_value=custom_analysis),
    ):
        return runner.invoke(main, ["xg"] + (args or []))


class TestXgJsonFormat:
    def test_json_output_is_valid(self):
        result = _run_xg(["--format", "json"])
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert data["command"] == "xg"
        assert isinstance(data["data"], dict)

    def test_json_contains_expected_keys(self):
        result = _run_xg(["--format", "json"])
        data = json.loads(result.output)
        assert "top_xgi_per_90" in data["data"]
        assert "underperformers" in data["data"]
        assert "value_picks" in data["data"]
        assert data["data"]["top_xgi_per_90"][0]["player_name"] == "Haaland"

    def test_json_metadata_has_window_default(self):
        result = _run_xg(["--format", "json"])
        data = json.loads(result.output)
        assert data["metadata"]["window"] == 6
        assert data["metadata"]["custom_analysis"] is True

    def test_json_metadata_window_all_season(self):
        result = _run_xg(["--format", "json", "--all"])
        data = json.loads(result.output)
        assert data["metadata"]["window"] == "all"

    def test_json_agent_failure_exits_nonzero(self):
        agent_result = _make_agent_result(success=False, message="API timeout")
        result = _run_xg(["--format", "json"], agent_result=agent_result)
        assert result.exit_code == 1

    def test_table_output_unchanged(self):
        result = _run_xg()
        assert result.exit_code == 0, result.output
        assert "Haaland" in result.output
        assert "Underlying Stats" in result.output

    def test_table_agent_failure_exits_nonzero(self):
        """Table-mode agent failure must exit nonzero, not just print and succeed (#47)."""
        agent_result = _make_agent_result(success=False, message="API timeout")
        result = _run_xg(agent_result=agent_result)
        assert result.exit_code == 1
        assert "Agent failed" in result.output


class TestXgCustomAnalysisToggle:
    """Tests for custom_analysis toggle gating experimental views."""

    def test_toggle_off_table_excludes_value_picks_view(self):
        """When toggle off, StatsAgent is configured without value_picks view."""
        runner = CliRunner()
        agent_result = _make_agent_result()
        mock_agent = MagicMock()
        mock_agent.run = AsyncMock(return_value=agent_result)
        mock_agent.__aenter__ = AsyncMock(return_value=mock_agent)
        mock_agent.__aexit__ = AsyncMock(return_value=False)

        with (
            patch("fpl_cli.agents.analysis.stats.StatsAgent", return_value=mock_agent) as mock_cls,
            patch("fpl_cli.cli.xg.custom_analysis_enabled", return_value=False),
        ):
            result = runner.invoke(main, ["xg"])

        assert result.exit_code == 0, result.output
        # Verify the views config passed to StatsAgent excludes value_picks
        call_kwargs = mock_cls.call_args[1]
        views = call_kwargs["config"]["views"]
        assert "value_picks" not in views
        assert "underperformers" in views
        assert "top_xgi_per_90" in views

    def test_toggle_on_table_has_value_picks(self):
        """When toggle on, value_picks section is present (no regression)."""
        result = _run_xg(custom_analysis=True)
        assert result.exit_code == 0, result.output
        assert "Mbeumo" in result.output  # value_picks player

    def test_toggle_off_json_excludes_experimental_views(self):
        """When toggle off, StatsAgent JSON config excludes value_picks and overperformers."""
        runner = CliRunner()
        agent_result = _make_agent_result()
        mock_agent = MagicMock()
        mock_agent.run = AsyncMock(return_value=agent_result)
        mock_agent.__aenter__ = AsyncMock(return_value=mock_agent)
        mock_agent.__aexit__ = AsyncMock(return_value=False)

        with (
            patch("fpl_cli.agents.analysis.stats.StatsAgent", return_value=mock_agent) as mock_cls,
            patch("fpl_cli.cli.xg.custom_analysis_enabled", return_value=False),
        ):
            result = runner.invoke(main, ["xg", "--format", "json"])

        assert result.exit_code == 0, result.output
        call_kwargs = mock_cls.call_args[1]
        views = call_kwargs["config"]["views"]
        assert "value_picks" not in views
        assert "overperformers" not in views
        assert "underperformers" in views
        assert "top_xgi_per_90" in views

    def test_toggle_on_json_includes_all_views(self):
        """When toggle on, JSON output has all views (no regression)."""
        result = _run_xg(["--format", "json"], custom_analysis=True)
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert "value_picks" in data["data"]
        assert "top_xgi_per_90" in data["data"]
        assert data["metadata"]["custom_analysis"] is True

    def test_toggle_off_json_metadata_signals_state(self):
        """JSON metadata includes custom_analysis: false when toggle off."""
        result = _run_xg(["--format", "json"], custom_analysis=False)
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert data["metadata"]["custom_analysis"] is False

    def test_toggle_off_table_renders_without_value_picks_key(self):
        """StatsAgent only emits keys for requested views, so with the toggle
        off `value_picks` is absent from `data` entirely, not just empty
        (#48). The renderer must not crash with a KeyError on the missing key.
        """
        data = _make_agent_result().data
        del data["value_picks"]
        result = _run_xg(agent_result=_make_agent_result(data=data), custom_analysis=False)
        assert result.exit_code == 0, result.output
        assert "Haaland" in result.output
        assert "Value Picks" not in result.output


class TestXgWindowAndFloorReporting:
    """`fpl xg` says which window and minutes floor it actually applied (#227).

    The floor is derived from the gameweeks played, so before GW6 it is not the
    one `--last-n` implies; a reader who cannot see it cannot tell a scaled bar
    from the default one, and an empty result reads the same either way.
    """

    @staticmethod
    def _early_season_result(*, empty: bool):
        data = _make_agent_result().data
        data |= {
            "window_label": "last 2 GWs (window of 6 clamped to gameweeks played)",
            "gameweeks": 2,
            "gameweeks_played": 2,
            "min_minutes": 90,
            "empty_reason": None,
            "warnings": [{"code": "early_season_minutes_floor", "message": "Floor scaled to 90."}],
        }
        if empty:
            data |= {
                "top_xgi_per_90": [], "underperformers": [], "value_picks": [],
                "qualified_players": 0,
                "empty_reason": {
                    "code": "below_minutes_floor",
                    "message": "No player clears the 90-minute floor.",
                },
            }
        return _make_agent_result(data=data)

    def test_json_metadata_reports_the_applied_window_and_floor(self):
        result = _run_xg(["--format", "json"], agent_result=self._early_season_result(empty=False))
        metadata = json.loads(result.output)["metadata"]
        assert metadata["window"] == 6  # what --last-n asked for
        assert metadata["gameweeks_played"] == 2
        assert metadata["min_minutes"] == 90
        assert "clamped" in metadata["window_label"]

    def test_json_warnings_live_in_metadata_not_data(self):
        result = _run_xg(["--format", "json"], agent_result=self._early_season_result(empty=False))
        payload = json.loads(result.output)
        assert [w["code"] for w in payload["metadata"]["warnings"]] == ["early_season_minutes_floor"]
        assert "warnings" not in payload["data"]

    def test_json_empty_result_carries_its_reason(self):
        result = _run_xg(["--format", "json"], agent_result=self._early_season_result(empty=True))
        data = json.loads(result.output)["data"]
        assert data["empty_reason"]["code"] == "below_minutes_floor"

    def test_table_header_names_the_applied_floor(self):
        result = _run_xg(agent_result=self._early_season_result(empty=False))
        assert result.exit_code == 0, result.output
        assert "90+ mins" in result.output

    def test_table_empty_result_explains_itself(self):
        result = _run_xg(agent_result=self._early_season_result(empty=True))
        assert result.exit_code == 0, result.output
        assert "No players to analyse" in result.output
        assert "No player clears the 90-minute floor." in result.output
        # The three empty tables are replaced by the explanation, not preceded by it.
        assert "Top xGI per 90" not in result.output

    def test_table_prints_the_scaled_floor_notice(self):
        result = _run_xg(agent_result=self._early_season_result(empty=False))
        assert "Floor scaled to 90." in result.output


class TestXgHeaderBeforeAnyGameweek:
    """Pre-season the panel must not name a floor there is no football behind."""

    @staticmethod
    def _pre_season_result():
        data = _make_agent_result().data
        data |= {
            "top_xgi_per_90": [], "underperformers": [], "value_picks": [],
            "window_label": "no gameweek played yet",
            "gameweeks": 0,
            "gameweeks_played": 0,
            "min_minutes": 1,
            "qualified_players": 0,
            "empty_reason": {
                "code": "no_minutes_played",
                "message": "No player has recorded any minutes yet.",
            },
        }
        return _make_agent_result(data=data)

    def test_header_omits_the_floor_when_nothing_has_been_played(self):
        result = _run_xg(agent_result=self._pre_season_result())
        assert result.exit_code == 0, result.output
        assert "no gameweek played yet" in result.output
        # "no gameweek played yet, 1+ mins" contradicts itself.
        assert "1+ mins" not in result.output
        assert "No player has recorded any minutes yet." in result.output

    def test_an_explicit_zero_floor_still_renders(self):
        """`is not None`, not truthiness: 0 is a floor, not the absence of one."""
        data = _make_agent_result().data
        data |= {"window_label": "whole season", "gameweeks_played": 3, "min_minutes": 0}
        result = _run_xg(agent_result=_make_agent_result(data=data))
        assert result.exit_code == 0, result.output
        assert "0+ mins" in result.output
