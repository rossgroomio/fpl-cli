"""Tests for `fpl stats` command."""
from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

from click.testing import CliRunner

from fpl_cli.cli import main
from fpl_cli.cli._context import CLIContext, Format
from fpl_cli.models.player import PlayerPosition, PlayerStatus
from tests.conftest import make_player, make_team


def _make_client(players=None, teams=None):
    """Create a mock FPLClient with given players and teams."""
    client = MagicMock()
    client.get_players = AsyncMock(return_value=players or [])
    client.get_teams = AsyncMock(return_value=teams or [])
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)
    return client


def _run(args=None, client=None, custom_analysis=None):
    """Invoke `fpl stats` with optional args and mock client."""
    if client is None:
        client = _make_client()
    runner = CliRunner()
    patches = [patch("fpl_cli.api.fpl.FPLClient", return_value=client)]
    if custom_analysis is not None:
        patches.append(
            patch("fpl_cli.cli.stats.is_custom_analysis_enabled", return_value=custom_analysis)
        )
    from contextlib import ExitStack
    with ExitStack() as stack:
        for p in patches:
            stack.enter_context(p)
        return runner.invoke(main, ["stats"] + (args or []))


def _sample_players():
    """Three players across positions with distinct stats."""
    return [
        make_player(id=1, web_name="Salah", team_id=1, position=PlayerPosition.MIDFIELDER,
                    total_points=200, minutes=2000, now_cost=130, goals_scored=15),
        make_player(id=2, web_name="Haaland", team_id=2, position=PlayerPosition.FORWARD,
                    total_points=180, minutes=1800, now_cost=145, goals_scored=20),
        make_player(id=3, web_name="Alexander-Arnold", team_id=1, position=PlayerPosition.DEFENDER,
                    total_points=150, minutes=1600, now_cost=85, goals_scored=3),
    ]


def _sample_teams():
    return [
        make_team(id=1, name="Liverpool", short_name="LIV"),
        make_team(id=2, name="Manchester City", short_name="MCI"),
    ]


class TestPlayersDefault:
    def test_default_output_shows_all_players_sorted_by_total_points(self):
        client = _make_client(_sample_players(), _sample_teams())
        result = _run(client=client)
        assert result.exit_code == 0, result.output
        # Players should appear in total_points descending order
        salah_pos = result.output.index("Salah")
        haaland_pos = result.output.index("Haaland")
        taa_pos = result.output.index("Alexander-Arnold")
        assert salah_pos < haaland_pos < taa_pos

    def test_default_output_contains_core_columns(self):
        client = _make_client(_sample_players(), _sample_teams())
        result = _run(client=client)
        assert result.exit_code == 0, result.output
        assert "LIV" in result.output
        assert "MCI" in result.output
        assert "MID" in result.output
        assert "DEF" in result.output


class TestPlayersFilters:
    def test_position_filter_shows_only_matching_position(self):
        client = _make_client(_sample_players(), _sample_teams())
        result = _run(["--position", "DEF"], client=client)
        assert result.exit_code == 0, result.output
        assert "Alexander-Arnold" in result.output
        assert "Salah" not in result.output
        assert "Haaland" not in result.output

    def test_team_filter_shows_only_matching_team(self):
        client = _make_client(_sample_players(), _sample_teams())
        result = _run(["--team", "LIV"], client=client)
        assert result.exit_code == 0, result.output
        assert "Salah" in result.output
        assert "Alexander-Arnold" in result.output
        assert "Haaland" not in result.output

    def test_team_filter_case_insensitive(self):
        client = _make_client(_sample_players(), _sample_teams())
        result = _run(["--team", "liv"], client=client)
        assert result.exit_code == 0, result.output
        assert "Salah" in result.output

    def test_min_minutes_filter(self):
        client = _make_client(_sample_players(), _sample_teams())
        result = _run(["--min-minutes", "1900"], client=client)
        assert result.exit_code == 0, result.output
        assert "Salah" in result.output
        assert "Haaland" not in result.output

    def test_limit_option(self):
        client = _make_client(_sample_players(), _sample_teams())
        result = _run(["--limit", "1"], client=client)
        assert result.exit_code == 0, result.output
        assert "Salah" in result.output
        assert "Haaland" not in result.output

    def test_available_only_excludes_injured_suspended_unavailable(self):
        players = [
            make_player(id=1, web_name="Fit", position=PlayerPosition.MIDFIELDER, status=PlayerStatus.AVAILABLE),
            make_player(id=2, web_name="Doubt", position=PlayerPosition.MIDFIELDER, status=PlayerStatus.DOUBTFUL),
            make_player(id=3, web_name="Hurt", position=PlayerPosition.MIDFIELDER, status=PlayerStatus.INJURED),
            make_player(id=4, web_name="Banned", position=PlayerPosition.MIDFIELDER, status=PlayerStatus.SUSPENDED),
            make_player(id=5, web_name="Out", position=PlayerPosition.MIDFIELDER, status=PlayerStatus.NOT_AVAILABLE),
            make_player(id=6, web_name="Gone", position=PlayerPosition.MIDFIELDER, status=PlayerStatus.UNAVAILABLE),
        ]
        client = _make_client(players, _sample_teams())
        result = _run(["--available-only"], client=client)
        assert result.exit_code == 0, result.output
        assert "Fit" in result.output
        assert "Doubt" in result.output
        assert "Hurt" not in result.output
        assert "Banned" not in result.output
        assert "Out" not in result.output
        assert "Gone" not in result.output

    def test_available_only_combined_with_position(self):
        players = [
            make_player(id=1, web_name="FitMid", position=PlayerPosition.MIDFIELDER, status=PlayerStatus.AVAILABLE),
            make_player(id=2, web_name="InjMid", position=PlayerPosition.MIDFIELDER, status=PlayerStatus.INJURED),
            make_player(id=3, web_name="FitFwd", position=PlayerPosition.FORWARD, status=PlayerStatus.AVAILABLE),
        ]
        client = _make_client(players, _sample_teams())
        result = _run(["--available-only", "--position", "MID"], client=client)
        assert result.exit_code == 0, result.output
        assert "FitMid" in result.output
        assert "InjMid" not in result.output
        assert "FitFwd" not in result.output

    def test_available_only_with_all_unavailable_shows_no_match(self):
        players = [
            make_player(id=1, web_name="Hurt", position=PlayerPosition.MIDFIELDER, status=PlayerStatus.INJURED),
            make_player(id=2, web_name="Banned", position=PlayerPosition.FORWARD, status=PlayerStatus.SUSPENDED),
        ]
        client = _make_client(players, _sample_teams())
        result = _run(["--available-only"], client=client)
        assert result.exit_code == 0
        assert "No players match" in result.output


class TestPlayersSort:
    def test_sort_by_goals_scored(self):
        client = _make_client(_sample_players(), _sample_teams())
        result = _run(["--sort", "goals_scored"], client=client)
        assert result.exit_code == 0, result.output
        # Haaland (20 goals) should appear before Salah (15)
        haaland_pos = result.output.index("Haaland")
        salah_pos = result.output.index("Salah")
        assert haaland_pos < salah_pos

    def test_sort_by_defensive_contribution(self):
        players = [
            make_player(id=1, web_name="Rice", team_id=1, defensive_contribution=50),
            make_player(id=2, web_name="Salah", team_id=1, defensive_contribution=10),
        ]
        client = _make_client(players, _sample_teams())
        result = _run(["--sort", "defensive_contribution"], client=client)
        assert result.exit_code == 0, result.output
        assert result.output.index("Rice") < result.output.index("Salah")

    def test_sort_by_value_form(self):
        players = [
            make_player(id=1, web_name="Bargain", team_id=1, value_form=2.5),
            make_player(id=2, web_name="Pricey", team_id=1, value_form=0.5),
        ]
        client = _make_client(players, _sample_teams())
        result = _run(["--sort", "value_form"], client=client)
        assert result.exit_code == 0, result.output
        assert result.output.index("Bargain") < result.output.index("Pricey")

    def test_reverse_flag_sorts_ascending(self):
        client = _make_client(_sample_players(), _sample_teams())
        result = _run(["--sort", "total_points", "--reverse"], client=client)
        assert result.exit_code == 0, result.output
        # TAA (150) should appear before Haaland (180) before Salah (200)
        taa_pos = result.output.index("Alexander-Arnold")
        haaland_pos = result.output.index("Haaland")
        salah_pos = result.output.index("Salah")
        assert taa_pos < haaland_pos < salah_pos

    def test_sort_column_appended_when_not_core(self):
        client = _make_client(_sample_players(), _sample_teams())
        result = _run(["--sort", "goals_scored"], client=client)
        assert result.exit_code == 0, result.output
        assert "goals_scored" in result.output


class TestPlayersErrors:
    def test_invalid_team_shows_valid_options(self):
        client = _make_client(_sample_players(), _sample_teams())
        result = _run(["--team", "XYZ"], client=client)
        assert result.exit_code != 0
        assert "LIV" in result.output
        assert "MCI" in result.output

    def test_no_results_shows_message(self):
        client = _make_client(_sample_players(), _sample_teams())
        result = _run(["--position", "GK"], client=client)
        assert result.exit_code == 0
        assert "No players match" in result.output


class TestPlayersJsonFormat:
    def test_json_output_is_valid_json(self):
        client = _make_client(_sample_players(), _sample_teams())
        result = _run(["--format", "json"], client=client)
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert data["command"] == "stats"
        assert isinstance(data["data"], list)

    def test_json_contains_expected_fields(self):
        client = _make_client(_sample_players(), _sample_teams())
        result = _run(["--format", "json"], client=client)
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        required = {"id", "name", "team", "position", "price", "total_points", "minutes",
                    "goals_scored", "assists", "expected_goal_involvements", "form",
                    "defensive_contribution", "defensive_contribution_per_90",
                    "value_form", "value_season"}
        assert required.issubset(data["data"][0].keys())

    def test_json_position_filter(self):
        client = _make_client(_sample_players(), _sample_teams())
        result = _run(["--position", "DEF", "--format", "json"], client=client)
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert all(p["position"] == "DEF" for p in data["data"])
        assert len(data["data"]) == 1

    def test_json_limit(self):
        client = _make_client(_sample_players(), _sample_teams())
        result = _run(["--limit", "2", "--format", "json"], client=client)
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert len(data["data"]) == 2

    def test_explicit_table_format(self):
        client = _make_client(_sample_players(), _sample_teams())
        result = _run(["--format", "table"], client=client)
        assert result.exit_code == 0, result.output
        # Table output contains player names as text, not JSON
        assert "Salah" in result.output
        assert result.output.strip()[0] != "["


class TestStatsDraftOwnership:
    """Tests for auto-enabled draft ownership column based on format."""

    def test_both_mode_no_league_id_warns(self):
        """BOTH format without draft_league_id shows warning."""
        client = _make_client(_sample_players(), _sample_teams())
        runner = CliRunner()
        ctx_obj = CLIContext(format=Format.BOTH, settings={})
        with (
            patch("fpl_cli.api.fpl.FPLClient", return_value=client),
            patch("fpl_cli.cli._context.load_settings", return_value={"fpl": {}}),
        ):
            result = runner.invoke(main, ["stats"], obj=ctx_obj)
        assert "draft_league_id" in result.output

    def test_classic_mode_no_draft_column(self):
        """CLASSIC format never shows Draft column."""
        client = _make_client(_sample_players(), _sample_teams())
        runner = CliRunner()
        ctx_obj = CLIContext(format=Format.CLASSIC, settings={})
        with patch("fpl_cli.api.fpl.FPLClient", return_value=client):
            result = runner.invoke(main, ["stats"], obj=ctx_obj)
        assert result.exit_code == 0
        assert "Draft" not in result.output

    def test_draft_flag_removed(self):
        """--draft flag no longer exists on stats command."""
        client = _make_client(_sample_players(), _sample_teams())
        result = _run(["--draft"], client=client)
        assert result.exit_code != 0
        assert "No such option" in result.output or "no such option" in result.output


# ---------------------------------------------------------------------------
# --value flag helpers
# ---------------------------------------------------------------------------

def _make_us_match():
    """Minimal Understat match dict for scoring."""
    return {
        "id": 100,
        "npxG_per_90": 0.45, "xGChain_per_90": 0.55,
        "xGI_per_90": 0.5, "penalty_xG_per_90": 0.10,
        "xGBuildup_per_90": 0.3,
    }


def _make_value_client(players=None, teams=None):
    """FPLClient mock with get_next_gameweek and get_player_detail for --value tests."""
    client = _make_client(players, teams)
    client.get_next_gameweek = AsyncMock(return_value={"id": 20})
    client.get_player_detail = AsyncMock(return_value={"history": []})
    return client


def _run_with_value(args=None, client=None, us_match=None):
    """Invoke `fpl stats --value` with mocked Understat scoring pipeline.

    Always enables custom_analysis since --value requires it.
    """
    if client is None:
        client = _make_value_client(_sample_players(), _sample_teams())
    if us_match is None:
        us_match = _make_us_match()

    mock_understat = MagicMock()
    mock_understat.get_league_players = AsyncMock(return_value=[
        {"id": 100, "player_name": "Mohamed Salah", "team_title": "Liverpool"},
    ])
    mock_understat.__aenter__ = AsyncMock(return_value=mock_understat)
    mock_understat.__aexit__ = AsyncMock(return_value=False)

    runner = CliRunner()
    with (
        patch("fpl_cli.api.fpl.FPLClient", return_value=client),
        patch("fpl_cli.api.understat.UnderstatClient", return_value=mock_understat),
        patch("fpl_cli.api.understat.match_fpl_to_understat", return_value=us_match),
        patch("fpl_cli.cli.stats.is_custom_analysis_enabled", return_value=True),
    ):
        return runner.invoke(main, ["stats", "--value"] + (args or []))


class TestStatsValueFlag:
    """Tests for --value flag: quality and value scoring columns."""

    def test_value_flag_shows_quality_and_value_columns(self):
        result = _run_with_value()
        assert result.exit_code == 0, result.output
        assert "Quality" in result.output
        assert "Value/£m" in result.output

    def test_value_flag_default_sort_is_value_score(self):
        """When --value active and no --sort, default sort is value_score descending."""
        result = _run_with_value()
        assert result.exit_code == 0, result.output
        # Sort arrow should be on Value/£m column
        assert "Value/£m" in result.output
        assert "▼" in result.output

    def test_value_flag_explicit_sort_overrides_default(self):
        result = _run_with_value(["--sort", "total_points", "--format", "json"])
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        # Sort by total_points descending: Salah(200) > Haaland(180) > TAA(150)
        points = [r["total_points"] for r in data["data"]]
        assert points == sorted(points, reverse=True)

    def test_value_flag_sort_by_quality_score(self):
        result = _run_with_value(["--sort", "quality_score"])
        assert result.exit_code == 0, result.output
        assert "Quality" in result.output

    def test_value_flag_json_includes_scores(self):
        result = _run_with_value(["--format", "json"])
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        record = data["data"][0]
        assert "quality_score" in record
        assert "value_score" in record

    def test_no_value_flag_json_excludes_scores(self):
        client = _make_value_client(_sample_players(), _sample_teams())
        result = _run(["--format", "json"], client=client)
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        record = data["data"][0]
        assert "quality_score" not in record
        assert "value_score" not in record

    def test_no_value_flag_table_has_no_quality_column(self):
        client = _make_client(_sample_players(), _sample_teams())
        result = _run(client=client)
        assert result.exit_code == 0, result.output
        assert "Quality" not in result.output
        assert "Value/£m" not in result.output


class TestStatsValueNullScores:
    """Tests for null quality/value scores."""

    def test_no_understat_match_shows_dash(self):
        """Player without Understat match displays '-' for quality and value."""
        # Force match_fpl_to_understat to return None for all players
        client = _make_value_client(_sample_players(), _sample_teams())
        mock_understat = MagicMock()
        mock_understat.get_league_players = AsyncMock(return_value=[{"id": 100}])
        mock_understat.__aenter__ = AsyncMock(return_value=mock_understat)
        mock_understat.__aexit__ = AsyncMock(return_value=False)
        runner = CliRunner()
        with (
            patch("fpl_cli.api.fpl.FPLClient", return_value=client),
            patch("fpl_cli.api.understat.UnderstatClient", return_value=mock_understat),
            patch("fpl_cli.api.understat.match_fpl_to_understat", return_value=None),
            patch("fpl_cli.cli.stats.is_custom_analysis_enabled", return_value=True),
        ):
            result = runner.invoke(main, ["stats", "--value"])
        assert result.exit_code == 0, result.output
        assert "-" in result.output

    def test_no_understat_match_json_has_null_scores(self):
        client = _make_value_client(_sample_players(), _sample_teams())
        mock_understat = MagicMock()
        mock_understat.get_league_players = AsyncMock(return_value=[{"id": 100}])
        mock_understat.__aenter__ = AsyncMock(return_value=mock_understat)
        mock_understat.__aexit__ = AsyncMock(return_value=False)
        runner = CliRunner()
        with (
            patch("fpl_cli.api.fpl.FPLClient", return_value=client),
            patch("fpl_cli.api.understat.UnderstatClient", return_value=mock_understat),
            patch("fpl_cli.api.understat.match_fpl_to_understat", return_value=None),
            patch("fpl_cli.cli.stats.is_custom_analysis_enabled", return_value=True),
        ):
            result = runner.invoke(main, ["stats", "--value", "--format", "json"])
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        record = data["data"][0]
        assert record["quality_score"] is None
        assert record["value_score"] is None

    def test_null_scored_players_sort_to_bottom(self):
        """When sorting by value_score, null-scored players appear last."""
        # Create one matched and one unmatched player
        players = [
            make_player(id=1, web_name="Scored", team_id=1, position=PlayerPosition.MIDFIELDER,
                        total_points=100, minutes=1000, now_cost=70),
            make_player(id=2, web_name="Unscored", team_id=2, position=PlayerPosition.MIDFIELDER,
                        total_points=200, minutes=2000, now_cost=100),
        ]
        teams = _sample_teams()
        client = _make_value_client(players, teams)

        # match_fpl_to_understat returns match only for id=1
        def _selective_match(fpl_name, *_args, **_kwargs):
            if fpl_name == "Scored":
                return _make_us_match()
            return None

        mock_understat = MagicMock()
        mock_understat.get_league_players = AsyncMock(return_value=[{"id": 100}])
        mock_understat.__aenter__ = AsyncMock(return_value=mock_understat)
        mock_understat.__aexit__ = AsyncMock(return_value=False)

        runner = CliRunner()
        with (
            patch("fpl_cli.api.fpl.FPLClient", return_value=client),
            patch("fpl_cli.api.understat.UnderstatClient", return_value=mock_understat),
            patch("fpl_cli.api.understat.match_fpl_to_understat", side_effect=_selective_match),
            patch("fpl_cli.cli.stats.is_custom_analysis_enabled", return_value=True),
        ):
            result = runner.invoke(main, ["stats", "--value"])

        assert result.exit_code == 0, result.output
        scored_pos = result.output.index("Scored")
        unscored_pos = result.output.index("Unscored")
        assert scored_pos < unscored_pos

    def test_price_zero_gives_null_value_score(self):
        players = [
            make_player(id=1, web_name="Free", team_id=1, position=PlayerPosition.MIDFIELDER,
                        total_points=100, minutes=1000, now_cost=0),
        ]
        client = _make_value_client(players, _sample_teams())
        result = _run_with_value(["--format", "json"], client=client)
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        record = data["data"][0]
        assert record["quality_score"] is not None
        assert record["value_score"] is None


class TestStatsValuePositionWeights:
    """Tests for position-based weight selection."""

    def test_gk_def_uses_without_xgi_weights(self):
        """GK/DEF should use VALUE_QUALITY_WEIGHTS.without_xgi()."""
        gk = make_player(id=1, web_name="Raya", team_id=1,
                         position=PlayerPosition.GOALKEEPER,
                         total_points=100, minutes=2000, now_cost=55)
        mid = make_player(id=2, web_name="Salah", team_id=1,
                          position=PlayerPosition.MIDFIELDER,
                          total_points=200, minutes=2000, now_cost=130)
        client = _make_value_client([gk, mid], _sample_teams())
        result = _run_with_value(["--format", "json"], client=client)
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        # Both should have quality scores (different weights, but both scored)
        for record in data["data"]:
            assert record["quality_score"] is not None


class TestStatsValueSortReverse:
    """Tests for --reverse with value sort fields."""

    def test_value_score_reverse_sorts_ascending(self):
        """--sort value_score --reverse puts lowest value first."""
        result = _run_with_value(["--sort", "value_score", "--reverse", "--format", "json"])
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        scores = [r["value_score"] for r in data["data"] if r["value_score"] is not None]
        assert scores == sorted(scores)

    def test_null_scored_players_sort_to_bottom_with_reverse(self):
        """Null-scored players at bottom even in ascending sort."""
        players = [
            make_player(id=1, web_name="Scored", team_id=1, position=PlayerPosition.MIDFIELDER,
                        total_points=100, minutes=1000, now_cost=70),
            make_player(id=2, web_name="Unscored", team_id=2, position=PlayerPosition.MIDFIELDER,
                        total_points=200, minutes=2000, now_cost=100),
        ]
        client = _make_value_client(players, _sample_teams())

        def _selective_match(fpl_name, *_args, **_kwargs):
            return _make_us_match() if fpl_name == "Scored" else None

        mock_understat = MagicMock()
        mock_understat.get_league_players = AsyncMock(return_value=[{"id": 100}])
        mock_understat.__aenter__ = AsyncMock(return_value=mock_understat)
        mock_understat.__aexit__ = AsyncMock(return_value=False)

        runner = CliRunner()
        with (
            patch("fpl_cli.api.fpl.FPLClient", return_value=client),
            patch("fpl_cli.api.understat.UnderstatClient", return_value=mock_understat),
            patch("fpl_cli.api.understat.match_fpl_to_understat", side_effect=_selective_match),
            patch("fpl_cli.cli.stats.is_custom_analysis_enabled", return_value=True),
        ):
            result = runner.invoke(main, ["stats", "--value", "--reverse"])

        assert result.exit_code == 0, result.output
        scored_pos = result.output.index("Scored")
        unscored_pos = result.output.index("Unscored")
        assert scored_pos < unscored_pos


class TestStatsValueSortValidation:
    """Tests for --sort value fields requiring --value flag."""

    def test_sort_value_score_without_value_flag_errors(self):
        client = _make_client(_sample_players(), _sample_teams())
        result = _run(["--sort", "value_score"], client=client, custom_analysis=True)
        assert result.exit_code != 0
        assert "--value" in result.output

    def test_sort_quality_score_without_value_flag_errors(self):
        client = _make_client(_sample_players(), _sample_teams())
        result = _run(["--sort", "quality_score"], client=client, custom_analysis=True)
        assert result.exit_code != 0
        assert "--value" in result.output


class TestStatsValueErrorPaths:
    """Tests for error handling in scoring pipeline."""

    def test_understat_failure_shows_table_without_scores(self):
        """Understat API failure shows table without quality/value columns."""
        import httpx

        mock_understat = MagicMock()
        mock_understat.get_league_players = AsyncMock(
            side_effect=httpx.HTTPError("connection failed")
        )
        mock_understat.__aenter__ = AsyncMock(return_value=mock_understat)
        mock_understat.__aexit__ = AsyncMock(return_value=False)

        client = _make_value_client(_sample_players(), _sample_teams())
        runner = CliRunner()
        with (
            patch("fpl_cli.api.fpl.FPLClient", return_value=client),
            patch("fpl_cli.api.understat.UnderstatClient", return_value=mock_understat),
            patch("fpl_cli.cli.stats.is_custom_analysis_enabled", return_value=True),
        ):
            result = runner.invoke(main, ["stats", "--value"])

        assert result.exit_code == 0, result.output
        # Table still displays but without scoring columns
        assert "Salah" in result.output
        assert "Understat unavailable" in result.output
        assert "Quality" not in result.output

    def test_explicit_value_sort_with_understat_failure_falls_back(self):
        """--sort quality_score + Understat failure falls back to total_points."""
        import httpx

        mock_understat = MagicMock()
        mock_understat.get_league_players = AsyncMock(
            side_effect=httpx.HTTPError("connection failed")
        )
        mock_understat.__aenter__ = AsyncMock(return_value=mock_understat)
        mock_understat.__aexit__ = AsyncMock(return_value=False)

        client = _make_value_client(_sample_players(), _sample_teams())
        runner = CliRunner()
        with (
            patch("fpl_cli.api.fpl.FPLClient", return_value=client),
            patch("fpl_cli.api.understat.UnderstatClient", return_value=mock_understat),
            patch("fpl_cli.cli.stats.is_custom_analysis_enabled", return_value=True),
        ):
            result = runner.invoke(main, ["stats", "--value", "--sort", "quality_score"])

        assert result.exit_code == 0, result.output
        assert "falling back to total_points" in result.output
        # Should still show players sorted by total_points
        assert "Salah" in result.output

    def test_individual_detail_failure_gives_null_scores(self):
        """get_player_detail failure for one player still scores others."""
        players = [
            make_player(id=1, web_name="Good", team_id=1, position=PlayerPosition.MIDFIELDER,
                        total_points=100, minutes=1000, now_cost=70),
            make_player(id=2, web_name="Bad", team_id=1, position=PlayerPosition.MIDFIELDER,
                        total_points=80, minutes=800, now_cost=60),
        ]
        client = _make_value_client(players, _sample_teams())
        # First call succeeds, second raises
        client.get_player_detail = AsyncMock(
            side_effect=[{"history": []}, Exception("API error")]
        )

        result = _run_with_value(["--format", "json"], client=client)
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        # Both should still appear with scores (failed detail means no trajectory, not no score)
        assert len(data["data"]) == 2
        assert all(r["quality_score"] is not None for r in data["data"])


# ---------------------------------------------------------------------------
# custom_analysis toggle tests
# ---------------------------------------------------------------------------


class TestStatsCustomAnalysisToggle:
    """Tests for custom_analysis toggle gating --value and value sort fields."""

    def test_toggle_off_value_flag_silently_ignored(self):
        """When toggle off, --value flag is silently ignored: no quality/value columns."""
        client = _make_value_client(_sample_players(), _sample_teams())
        result = _run(["--value"], client=client, custom_analysis=False)
        assert result.exit_code == 0, result.output
        assert "Quality" not in result.output
        assert "Value/£m" not in result.output
        # Players still appear
        assert "Salah" in result.output

    def test_toggle_on_value_flag_works(self):
        """When toggle on, --value flag shows quality/value columns (no regression)."""
        result = _run_with_value()
        assert result.exit_code == 0, result.output
        assert "Quality" in result.output or "Value/£m" in result.output

    def test_toggle_off_sort_quality_score_shows_custom_analysis_message(self):
        """When toggle off, --sort quality_score shows custom analysis required message."""
        client = _make_client(_sample_players(), _sample_teams())
        result = _run(["--sort", "quality_score"], client=client, custom_analysis=False)
        assert result.exit_code != 0
        assert "custom analysis" in result.output.lower()
        assert "fpl init" in result.output

    def test_toggle_off_sort_value_score_shows_custom_analysis_message(self):
        """When toggle off, --sort value_score shows custom analysis required message."""
        client = _make_client(_sample_players(), _sample_teams())
        result = _run(["--sort", "value_score"], client=client, custom_analysis=False)
        assert result.exit_code != 0
        assert "custom analysis" in result.output.lower()
        assert "fpl init" in result.output

    def test_toggle_off_json_excludes_quality_value(self):
        """When toggle off + --value flag, JSON output has no quality/value fields."""
        client = _make_value_client(_sample_players(), _sample_teams())
        result = _run(["--value", "--format", "json"], client=client, custom_analysis=False)
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        record = data["data"][0]
        assert "quality_score" not in record
        assert "value_score" not in record
        assert data["metadata"]["custom_analysis"] is False
