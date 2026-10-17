"""Tests for `fpl history` command."""
from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

from click.testing import CliRunner

from fpl_cli.cli import main


def _mock_fpl(codes: list[int]):
    """Create a mock FPLClient with players having given codes."""
    mock = AsyncMock()
    mock.get_players = AsyncMock(
        return_value=[MagicMock(code=c) for c in codes]
    )
    mock.__aenter__ = AsyncMock(return_value=mock)
    mock.__aexit__ = AsyncMock(return_value=False)
    return mock


def _mock_historical(profiles: dict):
    """Create a mock HistoricalDataProvider returning given profiles."""
    mock = AsyncMock()
    mock.get_all_player_histories = AsyncMock(return_value=profiles)
    mock.__aenter__ = AsyncMock(return_value=mock)
    mock.__aexit__ = AsyncMock(return_value=False)
    return mock


def _make_profile(
    web_name="Salah",
    element_code=80201,
    position="MID",
    *,
    pts_per_90=None,
    pts_per_90_trend=0.56,
    xgi_per_90=None,
    xgi_per_90_trend=0.94,
    cost_trajectory=5.0,
):
    """Create a mock PlayerProfile."""
    season = MagicMock()
    season.season = "2024-25"
    season.team_id = 14
    season.total_points = 200
    season.minutes = 2800
    season.starts = 30
    season.goals = 15
    season.assists = 10
    season.expected_goal_involvements = 18.5
    season.start_cost = 125
    season.end_cost = 130

    profile = MagicMock()
    profile.web_name = web_name
    profile.element_code = element_code
    profile.current_position = position
    profile.pts_per_90 = pts_per_90 if pts_per_90 is not None else [7.96, 8.52]
    profile.pts_per_90_trend = pts_per_90_trend
    profile.xgi_per_90 = xgi_per_90 if xgi_per_90 is not None else [7.96, 8.9]
    profile.xgi_per_90_trend = xgi_per_90_trend
    profile.cost_trajectory = cost_trajectory
    profile.minutes_per_start = [89.7, 90.3]
    profile.seasons = [season]
    return profile


def _run_history(args=None, codes=None, profiles=None):
    """Helper to invoke the history command with mocked providers."""
    fpl = _mock_fpl([80201] if codes is None else codes)
    hist = _mock_historical({80201: _make_profile()} if profiles is None else profiles)
    runner = CliRunner()
    with (
        patch("fpl_cli.api.fpl.FPLClient", return_value=fpl),
        patch("fpl_cli.api.historical.make_historical_provider", return_value=hist),
    ):
        return runner.invoke(main, ["history"] + (args or []))


def _run_failing_history(args=None):
    """Invoke `history` with an upstream that refuses, in either format."""
    fpl = _mock_fpl([80201])
    fpl.get_players = AsyncMock(side_effect=RuntimeError("API down"))
    hist = _mock_historical({})
    runner = CliRunner()
    with (
        patch("fpl_cli.api.fpl.FPLClient", return_value=fpl),
        patch("fpl_cli.api.historical.make_historical_provider", return_value=hist),
    ):
        return runner.invoke(main, ["history"] + (args or []))


class TestHistory:
    def test_outputs_current_season_players_only(self):
        result = _run_history(codes=[80201, 99999])
        assert result.exit_code == 0, result.output
        assert "Salah" in result.output
        assert "NewPlayer" not in result.output

    def test_handles_no_data(self):
        result = _run_history(codes=[], profiles={})
        assert result.exit_code == 0, result.output


class TestHistoryJson:
    def test_json_output_envelope(self):
        result = _run_history(["--format", "json"])
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert data["command"] == "history"
        assert isinstance(data["data"], list)
        assert len(data["data"]) == 1

    def test_json_profile_fields(self):
        result = _run_history(["--format", "json"])
        player = json.loads(result.output)["data"][0]
        assert player["name"] == "Salah"
        assert player["code"] == 80201
        assert player["position"] == "MID"

    def test_json_season_data(self):
        result = _run_history(["--format", "json"])
        season = json.loads(result.output)["data"][0]["seasons"][0]
        assert season["season"] == "2024-25"
        assert season["team"] == 14
        assert season["total_points"] == 200
        assert season["minutes"] == 2800
        assert season["starts"] == 30
        assert season["goals"] == 15
        assert season["assists"] == 10
        assert season["expected_goal_involvements"] == 18.5
        assert season["start_cost"] == 125
        assert season["end_cost"] == 130

    def test_json_trend_data(self):
        result = _run_history(["--format", "json"])
        trends = json.loads(result.output)["data"][0]["trends"]
        assert trends["pts_per_90"] == [7.96, 8.52]
        assert trends["pts_per_90_trend"] == 0.56
        assert trends["xgi_per_90"] == [7.96, 8.9]
        assert trends["xgi_per_90_trend"] == 0.94
        assert trends["cost_trajectory"] == 5.0

    def test_json_empty_when_no_data(self):
        result = _run_history(["--format", "json"], codes=[], profiles={})
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert data["command"] == "history"
        assert data["data"] == []

    def test_json_skips_profiles_without_pts_per_90(self):
        profile_with = _make_profile(web_name="Salah", element_code=80201)
        profile_without = _make_profile(
            web_name="Bench", element_code=12345, pts_per_90=[]
        )
        result = _run_history(
            ["--format", "json"],
            codes=[80201, 12345],
            profiles={80201: profile_with, 12345: profile_without},
        )
        data = json.loads(result.output)["data"]
        assert len(data) == 1
        assert data[0]["name"] == "Salah"

    def test_json_error_on_exception(self):
        result = _run_failing_history(["--format", "json"])
        assert result.exit_code == 1
        payload = json.loads(result.stdout)
        assert payload["command"] == "history"
        # The cause, not a stand-in for it: the envelope used to say only
        # "Failed to fetch historical data" while "API down" went to the
        # stream a `--format json` consumer is told not to read (#286).
        assert "API down" in payload["error"]

    def test_json_sorted_by_name(self):
        profile_z = _make_profile(web_name="Zaha", element_code=111)
        profile_a = _make_profile(web_name="Alexander-Arnold", element_code=222)
        result = _run_history(
            ["--format", "json"],
            codes=[111, 222],
            profiles={111: profile_z, 222: profile_a},
        )
        names = [p["name"] for p in json.loads(result.output)["data"]]
        assert names == ["Alexander-Arnold", "Zaha"]


class TestHistoryFailure:
    """A provider that refuses is one answer in both formats (#286).

    Table mode printed the cause on stdout and exited 0, so `fpl history >
    seasons.txt` filed the failure as though it were the profiles and
    `fpl history && rebuild-priors` rebuilt off nothing; `--format json`
    exited 1 with the cause dropped. Same refusal, same exit code, and the
    reason on the stream the caller is reading.
    """

    def test_table_exits_one_with_the_reason_on_stderr(self):
        result = _run_failing_history()

        assert result.exit_code == 1
        assert "Failed to fetch historical data: API down" in " ".join(result.stderr.split())
        # stdout carries the profiles, and there are none.
        assert result.stdout == ""

    def test_both_formats_agree_on_the_exit_code(self):
        assert (
            _run_failing_history().exit_code
            == _run_failing_history(["--format", "json"]).exit_code
            == 1
        )
