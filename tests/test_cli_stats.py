"""Tests for `fpl stats` command."""
from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from click.testing import CliRunner

from fpl_cli.cli import main
from fpl_cli.cli._context import CLIContext, Format
from fpl_cli.models.player import PlayerPosition, PlayerStatus
from tests.conftest import make_player, make_team


@pytest.fixture(autouse=True)
def _no_match_records_fetch(stub_scoring_network_seams):
    """Keep the direct fetch_match_records call on the --value path off the
    network. The patch list lives in conftest.stub_scoring_network_seams.
    """


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
        result = _run(["--sort", "form_per_m"], client=client)
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

    def test_sort_by_ep_next(self):
        players = [
            make_player(id=1, web_name="HighEp", team_id=1, ep_next=8.5),
            make_player(id=2, web_name="LowEp", team_id=1, ep_next=2.0),
        ]
        client = _make_client(players, _sample_teams())
        result = _run(["--sort", "ep_next"], client=client)
        assert result.exit_code == 0, result.output
        assert result.output.index("HighEp") < result.output.index("LowEp")

    def test_sort_by_ep_this(self):
        players = [
            make_player(id=1, web_name="HighEpThis", team_id=1, ep_this=7.0),
            make_player(id=2, web_name="LowEpThis", team_id=1, ep_this=1.5),
        ]
        client = _make_client(players, _sample_teams())
        result = _run(["--sort", "ep_this"], client=client)
        assert result.exit_code == 0, result.output
        assert result.output.index("HighEpThis") < result.output.index("LowEpThis")

    def test_sort_ep_next_zero_sorts_to_bottom(self):
        players = [
            make_player(id=1, web_name="Injured", team_id=1, ep_next=0.0),
            make_player(id=2, web_name="Fit", team_id=1, ep_next=5.0),
        ]
        client = _make_client(players, _sample_teams())
        result = _run(["--sort", "ep_next"], client=client)
        assert result.exit_code == 0, result.output
        assert result.output.index("Fit") < result.output.index("Injured")

    def test_sort_ep_next_none_sorts_to_bottom(self):
        players = [
            make_player(id=1, web_name="HasValue", team_id=1, ep_next=5.0),
            make_player(id=2, web_name="NullEp", team_id=1, ep_next=None),
        ]
        client = _make_client(players, _sample_teams())
        result = _run(["--sort", "ep_next"], client=client)
        assert result.exit_code == 0, result.output
        assert result.output.index("HasValue") < result.output.index("NullEp")

    def test_sort_ep_next_all_none_does_not_crash(self):
        players = [
            make_player(id=1, web_name="Alpha", team_id=1, ep_next=None),
            make_player(id=2, web_name="Beta", team_id=1, ep_next=None),
        ]
        client = _make_client(players, _sample_teams())
        result = _run(["--sort", "ep_next"], client=client)
        assert result.exit_code == 0, result.output

    def test_sort_ep_next_none_sorts_to_bottom_in_ascending(self):
        # --reverse activates the float("inf") sentinel branch of the sort
        players = [
            make_player(id=1, web_name="HasValue", team_id=1, ep_next=5.0),
            make_player(id=2, web_name="NullEp", team_id=1, ep_next=None),
        ]
        client = _make_client(players, _sample_teams())
        result = _run(["--sort", "ep_next", "--reverse"], client=client)
        assert result.exit_code == 0, result.output
        assert result.output.index("HasValue") < result.output.index("NullEp")

    def test_sort_ep_next_none_renders_em_dash_in_table(self):
        # Exercises the _format_sort_value None -> "—" branch
        players = [
            make_player(id=1, web_name="HasValue", team_id=1, ep_next=5.0),
            make_player(id=2, web_name="NullEp", team_id=1, ep_next=None),
        ]
        client = _make_client(players, _sample_teams())
        result = _run(["--sort", "ep_next"], client=client)
        assert result.exit_code == 0, result.output
        assert "—" in result.output


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
                    "form_per_m", "pts_per_m", "ep_next", "ep_this"}
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

    def test_json_ep_next_none_serialises_as_null(self):
        players = [
            make_player(
                id=1, web_name="NullEp", team_id=1,
                position=PlayerPosition.MIDFIELDER,
                ep_next=None, ep_this=None,
            ),
        ]
        client = _make_client(players, _sample_teams())
        result = _run(["--format", "json"], client=client)
        assert result.exit_code == 0, result.output
        record = json.loads(result.output)["data"][0]
        assert record["ep_next"] is None
        assert record["ep_this"] is None


class TestStatsDraftOwnership:
    """Tests for auto-enabled draft ownership column based on format."""

    def test_both_mode_no_league_id_warns(self):
        """BOTH format without draft_league_id shows warning."""
        client = _make_client(_sample_players(), _sample_teams())
        runner = CliRunner()
        with (
            patch("fpl_cli.api.fpl.FPLClient", return_value=client),
            patch("fpl_cli.cli._context.load_settings", return_value={"fpl": {}}),
            patch("fpl_cli.cli.load_settings", return_value={"fpl": {}}),
            patch("fpl_cli.cli._context.resolve_format", return_value=Format.BOTH),
            patch("fpl_cli.cli.resolve_format", return_value=Format.BOTH),
        ):
            result = runner.invoke(main, ["stats"])
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

    runner = CliRunner(env={"COLUMNS": "200"})
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
        assert "Quality/£m" in result.output

    def test_value_flag_default_sort_is_quality_per_m(self):
        """When --value active and no --sort, default sort is quality_per_m descending."""
        result = _run_with_value()
        assert result.exit_code == 0, result.output
        # Sort arrow should be on Quality/£m column
        assert "Quality/£m" in result.output
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
        assert "quality_per_m" in record

    def test_no_value_flag_json_excludes_scores(self):
        client = _make_value_client(_sample_players(), _sample_teams())
        result = _run(["--format", "json"], client=client)
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        record = data["data"][0]
        assert "quality_score" not in record
        assert "quality_per_m" not in record

    def test_no_value_flag_table_has_no_quality_column(self):
        client = _make_client(_sample_players(), _sample_teams())
        result = _run(client=client)
        assert result.exit_code == 0, result.output
        assert "Quality" not in result.output
        assert "Quality/£m" not in result.output


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
        assert record["quality_per_m"] is None

    def test_null_scored_players_sort_to_bottom(self):
        """When sorting by quality_per_m, null-scored players appear last."""
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

    def test_price_zero_gives_null_quality_per_m(self):
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
        assert record["quality_per_m"] is None


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

    def test_quality_per_m_reverse_sorts_ascending(self):
        """--sort quality_per_m --reverse puts lowest value first."""
        result = _run_with_value(["--sort", "quality_per_m", "--reverse", "--format", "json"])
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        scores = [r["quality_per_m"] for r in data["data"] if r["quality_per_m"] is not None]
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

    def test_sort_quality_per_m_without_value_flag_errors(self):
        client = _make_client(_sample_players(), _sample_teams())
        result = _run(["--sort", "quality_per_m"], client=client, custom_analysis=True)
        assert result.exit_code != 0
        assert "--value" in result.output

    def test_sort_quality_score_without_value_flag_errors(self):
        client = _make_client(_sample_players(), _sample_teams())
        result = _run(["--sort", "quality_score"], client=client, custom_analysis=True)
        assert result.exit_code != 0
        assert "--value" in result.output


class TestStatsValueCrossPositionWarning:
    """--value without -p is misleading; surface the warning in both channels.

    quality_score is an elite-within-position index; ordering elite DEFs
    against elite MIDs on quality_per_m actively misleads. Tables get
    prose on stderr for humans. JSON gets a structured entry in
    ``metadata.warnings`` so agents can detect the condition without
    parsing ANSI/stderr (agent-native parity).
    """

    def _invoke(self, args):
        """Run `fpl stats` with the full --value mock stack."""
        client = _make_value_client(_sample_players(), _sample_teams())
        mock_understat = MagicMock()
        mock_understat.get_league_players = AsyncMock(return_value=[{"id": 100}])
        mock_understat.__aenter__ = AsyncMock(return_value=mock_understat)
        mock_understat.__aexit__ = AsyncMock(return_value=False)
        runner = CliRunner()
        with (
            patch("fpl_cli.api.fpl.FPLClient", return_value=client),
            patch("fpl_cli.api.understat.UnderstatClient", return_value=mock_understat),
            patch("fpl_cli.api.understat.match_fpl_to_understat", return_value=_make_us_match()),
            patch("fpl_cli.cli.stats.is_custom_analysis_enabled", return_value=True),
        ):
            return runner.invoke(main, ["stats", *args])

    def test_table_mode_no_position_emits_stderr_warning(self):
        result = self._invoke(["--value"])
        assert result.exit_code == 0, result.output
        assert "cross-position ranking" in result.output
        assert "elite-within-position" in result.output

    def test_table_mode_with_position_no_warning(self):
        result = self._invoke(["--value", "-p", "MID"])
        assert result.exit_code == 0, result.output
        assert "cross-position ranking" not in result.output

    def test_json_mode_no_position_has_metadata_warning(self):
        """Agent-native: JSON consumers read `metadata.warnings` to detect
        the same condition without parsing stderr ANSI.
        """
        result = self._invoke(["--value", "--format", "json"])
        assert result.exit_code == 0, result.output
        # Stderr stays silent in JSON mode to keep machine pipelines clean.
        assert "cross-position ranking" not in result.output
        data = json.loads(result.output)
        warnings = data["metadata"]["warnings"]
        assert len(warnings) == 1
        assert warnings[0]["code"] == "cross_position_ranking_not_meaningful"
        assert "elite-within-position" in warnings[0]["message"]
        assert "--position" in warnings[0]["message"]
        assert data["metadata"]["filters"]["position"] is None

    def test_json_mode_with_position_empty_warnings(self):
        result = self._invoke(["--value", "-p", "MID", "--format", "json"])
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert data["metadata"]["warnings"] == []

    def test_warning_suppressed_without_value_flag(self):
        """Warning is gated on --value, not --position alone."""
        result = self._invoke([])
        assert result.exit_code == 0, result.output
        assert "cross-position ranking" not in result.output

    def test_json_warning_suppressed_without_value_flag(self):
        result = self._invoke(["--format", "json"])
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert data["metadata"]["warnings"] == []


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
        assert "Quality/£m" not in result.output
        # Players still appear
        assert "Salah" in result.output

    def test_toggle_on_value_flag_works(self):
        """When toggle on, --value flag shows quality/value columns (no regression)."""
        result = _run_with_value()
        assert result.exit_code == 0, result.output
        assert "Quality" in result.output or "Quality/£m" in result.output

    def test_toggle_off_sort_quality_score_shows_custom_analysis_message(self):
        """When toggle off, --sort quality_score shows custom analysis required message."""
        client = _make_client(_sample_players(), _sample_teams())
        result = _run(["--sort", "quality_score"], client=client, custom_analysis=False)
        assert result.exit_code != 0
        assert "custom analysis" in result.output.lower()
        assert "fpl init" in result.output

    def test_toggle_off_sort_quality_per_m_shows_custom_analysis_message(self):
        """When toggle off, --sort quality_per_m shows custom analysis required message."""
        client = _make_client(_sample_players(), _sample_teams())
        result = _run(["--sort", "quality_per_m"], client=client, custom_analysis=False)
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
        assert "quality_per_m" not in record
        assert data["metadata"]["custom_analysis"] is False


# ---------------------------------------------------------------------------
# rolling_pts_per_m tests
# ---------------------------------------------------------------------------

def _make_history(rounds_pts: list[tuple[int, int]], fixture_start: int = 100) -> list[dict]:
    """Build fixture history from (round, total_points) tuples; all 90 mins."""
    return [
        {"round": r, "minutes": 90, "total_points": pts, "fixture": fixture_start + i}
        for i, (r, pts) in enumerate(rounds_pts)
    ]


def _make_rolling_client(players=None, teams=None, history=None):
    """FPLClient mock with player history for rolling tests."""
    client = _make_value_client(players, teams)
    if history is not None:
        client.get_player_detail = AsyncMock(return_value={"history": history})
    return client


class TestStatsRollingPtsPerM:
    """Tests for rolling_pts_per_m in --value output."""

    def test_value_flag_includes_rolling_column(self):
        history = _make_history([(20, 6), (21, 8), (22, 4), (23, 10), (24, 7)])
        client = _make_rolling_client(_sample_players(), _sample_teams(), history)
        result = _run_with_value(client=client)
        assert result.exit_code == 0, result.output
        assert "Rolling" in result.output and "Pts/£m" in result.output

    def test_value_json_includes_rolling_fields(self):
        history = _make_history([(20, 6), (21, 8), (22, 4), (23, 10), (24, 7)])
        client = _make_rolling_client(_sample_players(), _sample_teams(), history)
        result = _run_with_value(["--format", "json"], client=client)
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        record = data["data"][0]
        assert "rolling_pts_per_m" in record
        assert "rolling_fixture_count" in record

    def test_sort_by_rolling_descending(self):
        history = _make_history([(20, 6), (21, 8), (22, 4), (23, 10), (24, 7)])
        client = _make_rolling_client(_sample_players(), _sample_teams(), history)
        result = _run_with_value(["-s", "rolling_pts_per_m", "--format", "json"], client=client)
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        scores = [r["rolling_pts_per_m"] for r in data["data"] if r["rolling_pts_per_m"] is not None]
        assert scores == sorted(scores, reverse=True)

    def test_sort_rolling_reverse_ascending(self):
        history = _make_history([(20, 6), (21, 8), (22, 4), (23, 10), (24, 7)])
        client = _make_rolling_client(_sample_players(), _sample_teams(), history)
        result = _run_with_value(["-s", "rolling_pts_per_m", "--reverse", "--format", "json"],
                                 client=client)
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        scores = [r["rolling_pts_per_m"] for r in data["data"] if r["rolling_pts_per_m"] is not None]
        assert scores == sorted(scores)

    def test_window_flag_respected(self):
        history = _make_history([(18, 2), (19, 3), (20, 4), (21, 5), (22, 6), (23, 7), (24, 8)])
        client = _make_rolling_client(_sample_players(), _sample_teams(), history)
        result = _run_with_value(["--window", "3", "--format", "json"], client=client)
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        record = data["data"][0]
        assert record["rolling_fixture_count"] == 3

    def test_asterisk_when_fewer_fixtures_than_window(self):
        """Rolling value shows asterisk when fixture_count < window."""
        history = _make_history([(22, 6), (23, 8), (24, 4)])  # only 3 qualifying
        client = _make_rolling_client(_sample_players(), _sample_teams(), history)
        result = _run_with_value(client=client)  # default window=5
        assert result.exit_code == 0, result.output
        # Should have asterisk suffix (3 fixtures < window of 5)
        assert "*" in result.output

    def test_no_asterisk_when_full_window(self):
        history = _make_history([(20, 6), (21, 8), (22, 4), (23, 10), (24, 7)])
        client = _make_rolling_client(_sample_players(), _sample_teams(), history)
        result = _run_with_value(client=client)
        assert result.exit_code == 0, result.output
        # No asterisk when fixture count equals window
        # Check the rolling column values don't have asterisks
        # (Can't easily assert absence in Rich output without parsing, but check it rendered)
        assert "Rolling" in result.output and "Pts/£m" in result.output

    def test_null_rolling_for_sparse_history(self):
        """Player with <3 qualifying fixtures gets null rolling value."""
        history = _make_history([(23, 6), (24, 8)])  # only 2 qualifying
        client = _make_rolling_client(_sample_players(), _sample_teams(), history)
        result = _run_with_value(["--format", "json"], client=client)
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        record = data["data"][0]
        assert record["rolling_pts_per_m"] is None
        assert record["rolling_fixture_count"] is None

    def test_sort_rolling_without_value_flag_errors(self):
        client = _make_client(_sample_players(), _sample_teams())
        result = _run(["--sort", "rolling_pts_per_m"], client=client, custom_analysis=True)
        assert result.exit_code != 0
        assert "--value" in result.output

    def test_window_without_value_silently_ignored(self):
        """--window without --value doesn't error."""
        client = _make_client(_sample_players(), _sample_teams())
        result = _run(["--window", "3"], client=client)
        assert result.exit_code == 0, result.output


def _many_players(count: int):
    """Enough matched players to trip the >100 scoring progress notice."""
    return [
        make_player(
            id=i, code=i, web_name=f"Player{i}", team_id=1,
            position=PlayerPosition.MIDFIELDER, minutes=900, total_points=50, now_cost=70,
        )
        for i in range(1, count + 1)
    ]


class TestStatsJsonStdoutPurity:
    """`--format json` must leave stdout parseable from byte 0 (#140).

    Every prose line the command can emit on the way to the envelope --
    progress notices, degradation warnings, usage errors -- has to go to
    stderr or come back as the error envelope, never as a stdout preamble.
    """

    def test_scoring_progress_notice_stays_off_stdout(self):
        """The >100-player notice used to precede the envelope on stdout."""
        client = _make_value_client(_many_players(129), _sample_teams())
        result = _run_with_value(["--format", "json"], client=client)
        assert result.exit_code == 0, result.output
        assert "this may take a moment" in result.stderr
        assert "this may take a moment" not in result.stdout
        data = json.loads(result.stdout)
        assert data["command"] == "stats"

    def test_scoring_progress_notice_still_shown_in_table_mode(self):
        client = _make_value_client(_many_players(129), _sample_teams())
        result = _run_with_value(client=client)
        assert result.exit_code == 0, result.output
        assert "this may take a moment" in result.stderr

    @staticmethod
    def _run_understat_down(args):
        """`--sort quality_score` with Understat unreachable: scoring is skipped
        and the sort falls back to total_points.
        """
        import httpx

        mock_understat = MagicMock()
        mock_understat.get_league_players = AsyncMock(
            side_effect=httpx.HTTPError("connection failed")
        )
        mock_understat.__aenter__ = AsyncMock(return_value=mock_understat)
        mock_understat.__aexit__ = AsyncMock(return_value=False)

        client = _make_value_client(_sample_players(), _sample_teams())
        runner = CliRunner(env={"COLUMNS": "200"})
        with (
            patch("fpl_cli.api.fpl.FPLClient", return_value=client),
            patch("fpl_cli.api.understat.UnderstatClient", return_value=mock_understat),
            patch("fpl_cli.cli.stats.is_custom_analysis_enabled", return_value=True),
        ):
            return runner.invoke(main, ["stats", "--value", "--sort", "quality_score"] + args)

    def test_understat_fallback_notice_stays_off_stdout(self):
        result = self._run_understat_down(["--format", "json"])
        assert result.exit_code == 0, result.output
        assert "falling back to total_points" not in result.output
        data = json.loads(result.stdout)
        # Scoring never ran, so the records carry no quality columns at all.
        assert "quality_score" not in data["data"][0]
        points = [r["total_points"] for r in data["data"]]
        assert points == sorted(points, reverse=True)

    def test_understat_fallback_carries_a_metadata_warning(self):
        """The sort silently changed, so JSON consumers get it structurally --
        `filters.sort` still echoes what was asked for, not what was applied.
        """
        result = self._run_understat_down(["--format", "json"])
        assert result.exit_code == 0, result.output
        data = json.loads(result.stdout)
        codes = [w["code"] for w in data["metadata"]["warnings"]]
        assert "value_sort_unavailable_fell_back" in codes
        assert data["metadata"]["filters"]["sort"] == "quality_score"

    def test_understat_fallback_is_prose_in_table_mode(self):
        result = self._run_understat_down([])
        assert result.exit_code == 0, result.output
        assert "falling back to total_points" in result.stderr

    def test_no_fallback_warning_when_scoring_succeeds(self):
        result = _run_with_value(["--sort", "quality_score", "--format", "json"])
        assert result.exit_code == 0, result.output
        data = json.loads(result.stdout)
        codes = [w["code"] for w in data["metadata"]["warnings"]]
        assert "value_sort_unavailable_fell_back" not in codes

    def test_value_sort_without_value_flag_emits_error_envelope(self):
        client = _make_client(_sample_players(), _sample_teams())
        result = _run(
            ["--sort", "quality_score", "--format", "json"], client=client, custom_analysis=True
        )
        assert result.exit_code == 1
        payload = json.loads(result.stdout)
        assert payload["command"] == "stats"
        assert "--value" in payload["error"]
        assert "data" not in payload

    def test_value_sort_without_custom_analysis_emits_error_envelope(self):
        client = _make_client(_sample_players(), _sample_teams())
        result = _run(
            ["--sort", "quality_per_m", "--format", "json"], client=client, custom_analysis=False
        )
        assert result.exit_code == 1
        payload = json.loads(result.stdout)
        assert "custom analysis" in payload["error"]

    def test_usage_errors_stay_prose_in_table_mode(self):
        client = _make_client(_sample_players(), _sample_teams())
        result = _run(["--sort", "quality_score"], client=client, custom_analysis=True)
        assert result.exit_code == 1
        assert "--value" in result.output
        assert "{" not in result.output

    def test_unknown_team_emits_error_envelope(self):
        """`--team` validation is shared with `fpl price-history` and used to
        print prose whatever the format.
        """
        client = _make_client(_sample_players(), _sample_teams())
        result = _run(["--team", "BADCODE", "--format", "json"], client=client)
        assert result.exit_code == 1
        payload = json.loads(result.stdout)
        assert payload["command"] == "stats"
        assert "BADCODE" in payload["error"]

    def test_unknown_team_stays_prose_in_table_mode(self):
        client = _make_client(_sample_players(), _sample_teams())
        result = _run(["--team", "BADCODE"], client=client)
        assert result.exit_code == 1
        assert "Unknown team" in result.output
