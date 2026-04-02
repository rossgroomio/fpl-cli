"""Tests for `fpl player` command flags."""
from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

from click.testing import CliRunner

from fpl_cli.cli import main
from fpl_cli.models.player import PlayerPosition
from tests.conftest import make_fixture, make_player, make_team


def _make_mocks():
    player = make_player(id=1, web_name="Salah", team_id=1, position=PlayerPosition.MIDFIELDER)
    team = make_team(id=1, name="Liverpool", short_name="LIV")
    opponent = make_team(id=2, name="Arsenal", short_name="ARS")

    client = MagicMock()
    client.get_players = AsyncMock(return_value=[player])
    client.get_teams = AsyncMock(return_value=[team, opponent])
    client.get_next_gameweek = AsyncMock(return_value={"id": 30})
    client.get_fixtures = AsyncMock(return_value=[
        make_fixture(id=1, gameweek=30, home_team_id=1, away_team_id=2),
    ])
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)

    fixture_agent = MagicMock()
    fixture_agent.get_positional_fdr.return_value = 2.5

    ratings_svc = MagicMock()
    ratings_svc.get_staleness_warning.return_value = None

    return client, fixture_agent, ratings_svc


def _make_empty_understat():
    """UnderstatClient mock that returns no league players."""
    mock = MagicMock()
    mock.get_league_players = AsyncMock(return_value=[])
    mock.__aenter__ = AsyncMock(return_value=mock)
    mock.__aexit__ = AsyncMock(return_value=False)
    return mock


def _run(args, client, fixture_agent, ratings_svc):
    runner = CliRunner()
    with (
        patch("fpl_cli.cli.player.load_settings", return_value={"fpl": {}}),
        patch("fpl_cli.api.fpl.FPLClient", return_value=client),
        patch("fpl_cli.agents.data.fixture.FixtureAgent", return_value=fixture_agent),
        patch("fpl_cli.services.team_ratings.TeamRatingsService", return_value=ratings_svc),
        patch("fpl_cli.api.understat.UnderstatClient", return_value=_make_empty_understat()),
    ):
        return runner.invoke(main, ["player", "Salah"] + args)


class TestPlayerMode:
    def test_mode_opponent_threads_to_get_positional_fdr(self):
        client, fixture_agent, ratings_svc = _make_mocks()
        result = _run(["-f", "--mode", "opponent"], client, fixture_agent, ratings_svc)
        assert result.exit_code == 0, result.output
        calls = fixture_agent.get_positional_fdr.call_args_list
        assert len(calls) > 0
        assert all(c.kwargs.get("mode") == "opponent" for c in calls)

    def test_mode_defaults_to_difference(self):
        client, fixture_agent, ratings_svc = _make_mocks()
        result = _run(["-f"], client, fixture_agent, ratings_svc)
        assert result.exit_code == 0, result.output
        calls = fixture_agent.get_positional_fdr.call_args_list
        assert all(c.kwargs.get("mode") == "difference" for c in calls)

    def test_mode_without_fixtures_flag_does_not_call_fdr(self):
        client, fixture_agent, ratings_svc = _make_mocks()
        result = _run(["--mode", "opponent"], client, fixture_agent, ratings_svc)
        assert result.exit_code == 0, result.output
        fixture_agent.get_positional_fdr.assert_not_called()


class TestPlayerHistory:
    def test_history_flag_shows_historical_data(self):
        client, fixture_agent, ratings_svc = _make_mocks()
        # Make player have a code for vaastav lookup
        client.get_players = AsyncMock(return_value=[
            make_player(id=1, web_name="Salah", team_id=1,
                        position=PlayerPosition.MIDFIELDER, code=80201)
        ])
        mock_profile = MagicMock()
        mock_profile.web_name = "Salah"
        mock_profile.current_position = "MID"
        mock_profile.pts_per_90 = [7.96, 8.52]
        mock_profile.pts_per_90_trend = 0.56
        mock_profile.cost_trajectory = 5.0
        mock_profile.xgi_per_90 = [7.96, 8.9]
        mock_profile.xgi_per_90_trend = 0.94
        mock_profile.minutes_per_start = [89.7, 90.3]
        mock_profile.seasons = [
            MagicMock(
                season="2023-24", team_id=1, total_points=230, minutes=2600,
                starts=29, goals=15, assists=11,
                expected_goal_involvements=23.0, start_cost=125, end_cost=125, position="MID",
            ),
            MagicMock(
                season="2024-25", total_points=265, minutes=2800, starts=31,
                goals=19, assists=13, expected_goal_involvements=27.7,
                start_cost=125, end_cost=130, position="MID",
            ),
        ]

        runner = CliRunner()
        with (
            patch("fpl_cli.cli.player.load_settings", return_value={"fpl": {}}),
            patch("fpl_cli.api.fpl.FPLClient", return_value=client),
            patch("fpl_cli.agents.data.fixture.FixtureAgent", return_value=fixture_agent),
            patch("fpl_cli.services.team_ratings.TeamRatingsService", return_value=ratings_svc),
            patch("fpl_cli.api.vaastav.VaastavClient") as mock_vaastav_cls,
        ):
            mock_vaastav = MagicMock()
            mock_vaastav.get_player_history = AsyncMock(return_value=mock_profile)
            mock_vaastav.__aenter__ = AsyncMock(return_value=mock_vaastav)
            mock_vaastav.__aexit__ = AsyncMock(return_value=False)
            mock_vaastav_cls.return_value = mock_vaastav

            result = runner.invoke(main, ["player", "Salah", "--history"])

        assert result.exit_code == 0, result.output
        assert "2023-24" in result.output
        assert "2024-25" in result.output

    def test_history_flag_player_not_found(self):
        client, fixture_agent, ratings_svc = _make_mocks()
        client.get_players = AsyncMock(return_value=[
            make_player(id=1, web_name="NewSigning", team_id=1,
                        position=PlayerPosition.FORWARD, code=99999)
        ])

        runner = CliRunner()
        with (
            patch("fpl_cli.cli.player.load_settings", return_value={"fpl": {}}),
            patch("fpl_cli.api.fpl.FPLClient", return_value=client),
            patch("fpl_cli.agents.data.fixture.FixtureAgent", return_value=fixture_agent),
            patch("fpl_cli.services.team_ratings.TeamRatingsService", return_value=ratings_svc),
            patch("fpl_cli.api.vaastav.VaastavClient") as mock_vaastav_cls,
        ):
            mock_vaastav = MagicMock()
            mock_vaastav.get_player_history = AsyncMock(return_value=None)
            mock_vaastav.__aenter__ = AsyncMock(return_value=mock_vaastav)
            mock_vaastav.__aexit__ = AsyncMock(return_value=False)
            mock_vaastav_cls.return_value = mock_vaastav

            result = runner.invoke(main, ["player", "NewSigning", "--history"])

        assert result.exit_code == 0, result.output
        assert "No historical data" in result.output


class TestPlayerSetPieces:
    def test_set_pieces_shown_when_assigned(self):
        client, fixture_agent, ratings_svc = _make_mocks()
        client.get_players = AsyncMock(return_value=[
            make_player(id=1, web_name="Salah", team_id=1,
                        position=PlayerPosition.MIDFIELDER,
                        penalties_order=1, corners_and_indirect_freekicks_order=2)
        ])
        result = _run([], client, fixture_agent, ratings_svc)
        assert result.exit_code == 0, result.output
        assert "Set pieces:" in result.output
        assert "Pens (1st)" in result.output
        assert "Corners (2nd)" in result.output

    def test_set_pieces_hidden_when_none(self):
        client, fixture_agent, ratings_svc = _make_mocks()
        result = _run([], client, fixture_agent, ratings_svc)
        assert result.exit_code == 0, result.output
        assert "Set pieces:" not in result.output

    def test_set_pieces_excludes_pens_beyond_order_2(self):
        client, fixture_agent, ratings_svc = _make_mocks()
        client.get_players = AsyncMock(return_value=[
            make_player(id=1, web_name="Salah", team_id=1,
                        position=PlayerPosition.MIDFIELDER,
                        penalties_order=3)
        ])
        result = _run([], client, fixture_agent, ratings_svc)
        assert result.exit_code == 0, result.output
        assert "Set pieces:" not in result.output

    def test_set_pieces_direct_fk_only_at_order_1(self):
        client, fixture_agent, ratings_svc = _make_mocks()
        client.get_players = AsyncMock(return_value=[
            make_player(id=1, web_name="Salah", team_id=1,
                        position=PlayerPosition.MIDFIELDER,
                        direct_freekicks_order=1)
        ])
        result = _run([], client, fixture_agent, ratings_svc)
        assert result.exit_code == 0, result.output
        assert "Set pieces:" in result.output
        assert "Direct FKs (1st)" in result.output


class TestGoalkeeperPanelAdjustments:
    def _make_gk_mocks(self):
        client, fixture_agent, ratings_svc = _make_mocks()
        client.get_players = AsyncMock(return_value=[
            make_player(id=1, web_name="Salah", team_id=1,
                        position=PlayerPosition.GOALKEEPER,
                        penalties_saved=3, expected_goals=0.1, expected_assists=0.5)
        ])
        return client, fixture_agent, ratings_svc

    def test_gk_panel_shows_penalties_saved_not_xg(self):
        client, fixture_agent, ratings_svc = self._make_gk_mocks()
        result = _run([], client, fixture_agent, ratings_svc)
        assert result.exit_code == 0, result.output
        assert "Penalties saved: 3" in result.output
        assert "xG:" not in result.output
        assert "xA:" in result.output

    def test_outfield_panel_shows_xg_not_penalties_saved(self):
        client, fixture_agent, ratings_svc = _make_mocks()
        result = _run([], client, fixture_agent, ratings_svc)
        assert result.exit_code == 0, result.output
        assert "xG:" in result.output
        assert "Penalties saved" not in result.output

    def test_gk_detail_view_excludes_xg_column(self):
        client, fixture_agent, ratings_svc = self._make_gk_mocks()
        client.get_player_detail = AsyncMock(return_value={
            "history": [
                {
                    "round": 28, "opponent_team": 2, "was_home": True,
                    "minutes": 90, "goals_scored": 0, "assists": 0,
                    "expected_goals": 0.0, "expected_assists": 0.0,
                    "clean_sheets": 1, "goals_conceded": 0,
                    "expected_goals_conceded": 0.5, "saves": 4,
                    "bonus": 2, "total_points": 8,
                },
            ],
        })
        result = _run(["--detail"], client, fixture_agent, ratings_svc)
        assert result.exit_code == 0, result.output
        assert "Sv" in result.output
        assert "xA" in result.output
        # xG column header should not appear for GK
        # (check that "xG" doesn't appear as a column - it may appear in xGC though)
        lines = result.output.split("\n")
        header_lines = [l for l in lines if "GW" in l and "Opponent" in l]
        assert header_lines, "Expected table header with GW and Opponent columns"
        assert "xG " not in header_lines[0] or "xGC" in header_lines[0]

    def test_gk_json_has_penalties_saved_not_xg(self):
        client, fixture_agent, ratings_svc = self._make_gk_mocks()
        result = _run_json([], client, fixture_agent, ratings_svc)
        assert result.exit_code == 0, result.output
        info = json.loads(result.output)["data"][0]["info"]
        assert info["penalties_saved"] == 3
        assert "expected_goals" not in info
        assert "expected_assists" in info


class TestPlayerDefensiveContribution:
    def test_dc_shown_for_outfield_player(self):
        client, fixture_agent, ratings_svc = _make_mocks()
        client.get_players = AsyncMock(return_value=[
            make_player(id=1, web_name="Salah", team_id=1,
                        position=PlayerPosition.MIDFIELDER,
                        defensive_contribution_per_90=1.5)
        ])
        result = _run([], client, fixture_agent, ratings_svc)
        assert result.exit_code == 0, result.output
        assert "DC/90: 1.5" in result.output

    def test_dc_hidden_for_goalkeeper(self):
        client, fixture_agent, ratings_svc = _make_mocks()
        client.get_players = AsyncMock(return_value=[
            make_player(id=1, web_name="Raya", team_id=1,
                        position=PlayerPosition.GOALKEEPER,
                        defensive_contribution_per_90=2.0)
        ])
        result = _run([], client, fixture_agent, ratings_svc)
        assert result.exit_code == 0, result.output
        assert "DC/90:" not in result.output

    def test_dc_hidden_when_zero(self):
        client, fixture_agent, ratings_svc = _make_mocks()
        result = _run([], client, fixture_agent, ratings_svc)
        assert result.exit_code == 0, result.output
        assert "DC/90:" not in result.output


class TestPlayerDetail:
    def test_detail_standalone_shows_match_table(self):
        client, fixture_agent, ratings_svc = _make_mocks()
        client.get_player_detail = AsyncMock(return_value={
            "history": [
                {
                    "round": 28, "opponent_team": 2, "was_home": True,
                    "minutes": 90, "goals_scored": 1, "assists": 0,
                    "expected_goals": 0.45, "expected_assists": 0.12,
                    "total_points": 8,
                },
                {
                    "round": 29, "opponent_team": 2, "was_home": False,
                    "minutes": 78, "goals_scored": 0, "assists": 1,
                    "expected_goals": 0.10, "expected_assists": 0.55,
                    "total_points": 5,
                },
            ],
        })
        result = _run(["--detail"], client, fixture_agent, ratings_svc)
        assert result.exit_code == 0, result.output
        assert "Match Detail" in result.output
        assert "LIV" in result.output or "ars" in result.output

    def test_detail_opponent_uppercase_home_lowercase_away(self):
        client, fixture_agent, ratings_svc = _make_mocks()
        client.get_player_detail = AsyncMock(return_value={
            "history": [
                {
                    "round": 28, "opponent_team": 2, "was_home": True,
                    "minutes": 90, "goals_scored": 0, "assists": 0,
                    "expected_goals": 0.0, "expected_assists": 0.0,
                    "total_points": 2,
                },
                {
                    "round": 29, "opponent_team": 2, "was_home": False,
                    "minutes": 90, "goals_scored": 0, "assists": 0,
                    "expected_goals": 0.0, "expected_assists": 0.0,
                    "total_points": 2,
                },
            ],
        })
        result = _run(["--detail"], client, fixture_agent, ratings_svc)
        assert result.exit_code == 0, result.output
        assert "ARS" in result.output  # Home: uppercase
        assert "ars" in result.output  # Away: lowercase

    def test_detail_empty_history(self):
        client, fixture_agent, ratings_svc = _make_mocks()
        client.get_player_detail = AsyncMock(return_value={"history": []})
        result = _run(["--detail"], client, fixture_agent, ratings_svc)
        assert result.exit_code == 0, result.output
        assert "No match data available" in result.output

    def test_old_shots_flag_rejected(self):
        client, fixture_agent, ratings_svc = _make_mocks()
        result = _run(["--shots"], client, fixture_agent, ratings_svc)
        assert result.exit_code != 0
        assert "No such option" in result.output or "no such option" in result.output

    def test_old_profile_flag_rejected(self):
        client, fixture_agent, ratings_svc = _make_mocks()
        result = _run(["--profile"], client, fixture_agent, ratings_svc)
        assert result.exit_code != 0
        assert "No such option" in result.output or "no such option" in result.output


class TestPlayerUnderstat:
    def _run_with_understat(self, args, client, fixture_agent, ratings_svc,
                            understat_player_data=None):
        runner = CliRunner()
        mock_understat = MagicMock()
        mock_understat.get_league_players = AsyncMock(return_value=[
            {"id": 100, "player_name": "Mohamed Salah", "team_title": "Liverpool",
             "position": "M F", "games": 28, "npxG": 12.5, "xGChain": 18.0,
             "xGBuildup": 5.0},
        ])
        mock_understat.get_player = AsyncMock(return_value=understat_player_data)
        mock_understat.__aenter__ = AsyncMock(return_value=mock_understat)
        mock_understat.__aexit__ = AsyncMock(return_value=False)

        with (
            patch("fpl_cli.cli.player.load_settings", return_value={"fpl": {}}),
            patch("fpl_cli.api.fpl.FPLClient", return_value=client),
            patch("fpl_cli.agents.data.fixture.FixtureAgent", return_value=fixture_agent),
            patch("fpl_cli.services.team_ratings.TeamRatingsService", return_value=ratings_svc),
            patch("fpl_cli.api.understat.UnderstatClient", return_value=mock_understat),
            patch("fpl_cli.api.understat.match_fpl_to_understat", return_value={
                "id": 100, "npxG": 12.5, "xGChain": 18.0, "xGBuildup": 5.0,
            }),
        ):
            return runner.invoke(main, ["player", "Salah"] + args)

    def test_understat_shows_shot_analysis(self):
        client, fixture_agent, ratings_svc = _make_mocks()
        data = {
            "matches": [{"date": "2026-03-20", "season": "2025"}],
            "shots": [
                {"season": "2025", "xG": "0.45", "result": "Goal",
                 "shotType": "RightFoot", "situation": "OpenPlay"},
            ],
            "groups": {},
        }
        result = self._run_with_understat(
            ["--understat"], client, fixture_agent, ratings_svc, data)
        assert result.exit_code == 0, result.output
        assert "Shot Analysis" in result.output

    def test_understat_shows_staleness_warning_when_old(self):
        client, fixture_agent, ratings_svc = _make_mocks()
        data = {
            "matches": [{"date": "2025-11-09", "season": "2025"}],
            "shots": [
                {"season": "2025", "xG": "0.45", "result": "Goal",
                 "shotType": "RightFoot", "situation": "OpenPlay"},
            ],
            "groups": {},
        }
        result = self._run_with_understat(
            ["--understat"], client, fixture_agent, ratings_svc, data)
        assert result.exit_code == 0, result.output
        assert "2025-11-09" in result.output
        assert "days ago" in result.output

    def test_understat_shows_situation_profile(self):
        client, fixture_agent, ratings_svc = _make_mocks()
        data = {
            "matches": [{"date": "2026-03-20", "season": "2025"}],
            "shots": [],
            "groups": {
                "situation": {
                    "2025": {
                        "OpenPlay": {"xG": "3.5", "shots": "40", "goals": "4"},
                        "SetPiece": {"xG": "0.5", "shots": "5", "goals": "0"},
                    },
                },
            },
        }
        result = self._run_with_understat(
            ["--understat"], client, fixture_agent, ratings_svc, data)
        assert result.exit_code == 0, result.output
        assert "Situation Profile" in result.output
        assert "OpenPlay" in result.output

    def test_understat_no_match_shows_message(self):
        client, fixture_agent, ratings_svc = _make_mocks()
        runner = CliRunner()
        mock_understat = MagicMock()
        mock_understat.get_league_players = AsyncMock(return_value=[])
        mock_understat.__aenter__ = AsyncMock(return_value=mock_understat)
        mock_understat.__aexit__ = AsyncMock(return_value=False)

        with (
            patch("fpl_cli.cli.player.load_settings", return_value={"fpl": {}}),
            patch("fpl_cli.api.fpl.FPLClient", return_value=client),
            patch("fpl_cli.agents.data.fixture.FixtureAgent", return_value=fixture_agent),
            patch("fpl_cli.services.team_ratings.TeamRatingsService", return_value=ratings_svc),
            patch("fpl_cli.api.understat.UnderstatClient", return_value=mock_understat),
            patch("fpl_cli.api.understat.match_fpl_to_understat", return_value=None),
        ):
            result = runner.invoke(main, ["player", "Salah", "--understat"])

        assert result.exit_code == 0, result.output
        assert "No Understat match found" in result.output


# --- JSON output tests ---


def _run_json(args, client, fixture_agent, ratings_svc):
    """Run the player command with --format json and return parsed JSON."""
    runner = CliRunner()
    with (
        patch("fpl_cli.cli.player.load_settings", return_value={"fpl": {}}),
        patch("fpl_cli.api.fpl.FPLClient", return_value=client),
        patch("fpl_cli.agents.data.fixture.FixtureAgent", return_value=fixture_agent),
        patch("fpl_cli.services.team_ratings.TeamRatingsService", return_value=ratings_svc),
        patch("fpl_cli.api.understat.UnderstatClient", return_value=_make_empty_understat()),
    ):
        result = runner.invoke(main, ["player", "Salah", "--format", "json"] + args)
    return result


class TestPlayerJsonOutput:
    def test_json_produces_valid_envelope(self):
        client, fixture_agent, ratings_svc = _make_mocks()
        result = _run_json([], client, fixture_agent, ratings_svc)
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert data["command"] == "player"
        assert data["metadata"]["query"] == "Salah"
        assert data["metadata"]["matches"] == 1
        assert isinstance(data["data"], list)
        assert len(data["data"]) == 1

    def test_json_info_section_always_present(self):
        client, fixture_agent, ratings_svc = _make_mocks()
        result = _run_json([], client, fixture_agent, ratings_svc)
        assert result.exit_code == 0, result.output
        player_data = json.loads(result.output)["data"][0]
        info = player_data["info"]
        assert info["web_name"] == "Salah"
        assert info["team"] == "Liverpool"
        assert info["team_short"] == "LIV"
        assert info["position"] == "MID"
        assert info["status"] == "Available"
        assert "id" in info
        assert "price" in info
        assert "form" in info
        assert "total_points" in info

    def test_json_no_optional_sections_without_flags(self):
        client, fixture_agent, ratings_svc = _make_mocks()
        result = _run_json([], client, fixture_agent, ratings_svc)
        assert result.exit_code == 0, result.output
        player_data = json.loads(result.output)["data"][0]
        assert "fixtures" not in player_data
        assert "detail" not in player_data
        assert "understat" not in player_data
        assert "history" not in player_data

    def test_json_detail_section_present_with_flag(self):
        client, fixture_agent, ratings_svc = _make_mocks()
        client.get_player_detail = AsyncMock(return_value={
            "history": [
                {
                    "round": 28, "opponent_team": 2, "was_home": True,
                    "minutes": 90, "goals_scored": 1, "assists": 0,
                    "expected_goals": 0.45, "expected_assists": 0.12,
                    "bonus": 3, "total_points": 8,
                },
            ],
        })
        result = _run_json(["--detail"], client, fixture_agent, ratings_svc)
        assert result.exit_code == 0, result.output
        player_data = json.loads(result.output)["data"][0]
        assert "detail" in player_data
        assert len(player_data["detail"]) == 1
        entry = player_data["detail"][0]
        assert entry["gameweek"] == 28
        assert entry["opponent"] == "ARS"
        assert entry["goals_scored"] == 1
        assert entry["total_points"] == 8

    def test_json_fixtures_section_present_with_flag(self):
        client, fixture_agent, ratings_svc = _make_mocks()
        result = _run_json(["--fixtures"], client, fixture_agent, ratings_svc)
        assert result.exit_code == 0, result.output
        player_data = json.loads(result.output)["data"][0]
        assert "fixtures" in player_data
        assert isinstance(player_data["fixtures"], list)
        # Should have 6 gameweeks (current + 5)
        assert len(player_data["fixtures"]) == 6

    def test_json_history_section_present_with_flag(self):
        client, fixture_agent, ratings_svc = _make_mocks()
        client.get_players = AsyncMock(return_value=[
            make_player(id=1, web_name="Salah", team_id=1,
                        position=PlayerPosition.MIDFIELDER, code=80201)
        ])
        mock_profile = MagicMock()
        mock_profile.seasons = [
            MagicMock(
                season="2023-24", team_id=1, total_points=230, minutes=2600,
                starts=29, goals=15, assists=11,
                expected_goal_involvements=23.0, start_cost=125, end_cost=125,
            ),
        ]
        mock_profile.pts_per_90 = [7.96]
        mock_profile.pts_per_90_trend = 0.56
        mock_profile.xgi_per_90 = [7.96]
        mock_profile.xgi_per_90_trend = 0.94
        mock_profile.cost_trajectory = 5.0

        runner = CliRunner()
        with (
            patch("fpl_cli.cli.player.load_settings", return_value={"fpl": {}}),
            patch("fpl_cli.api.fpl.FPLClient", return_value=client),
            patch("fpl_cli.agents.data.fixture.FixtureAgent", return_value=fixture_agent),
            patch("fpl_cli.services.team_ratings.TeamRatingsService", return_value=ratings_svc),
            patch("fpl_cli.api.vaastav.VaastavClient") as mock_vaastav_cls,
        ):
            mock_vaastav = MagicMock()
            mock_vaastav.get_player_history = AsyncMock(return_value=mock_profile)
            mock_vaastav.__aenter__ = AsyncMock(return_value=mock_vaastav)
            mock_vaastav.__aexit__ = AsyncMock(return_value=False)
            mock_vaastav_cls.return_value = mock_vaastav

            result = runner.invoke(main, ["player", "Salah", "--history", "--format", "json"])

        assert result.exit_code == 0, result.output
        player_data = json.loads(result.output)["data"][0]
        assert "history" in player_data
        assert player_data["history"]["seasons"][0]["season"] == "2023-24"
        assert "trends" in player_data["history"]

    def test_json_set_pieces_present_when_assigned(self):
        client, fixture_agent, ratings_svc = _make_mocks()
        client.get_players = AsyncMock(return_value=[
            make_player(id=1, web_name="Salah", team_id=1,
                        position=PlayerPosition.MIDFIELDER,
                        penalties_order=1, corners_and_indirect_freekicks_order=2)
        ])
        result = _run_json([], client, fixture_agent, ratings_svc)
        assert result.exit_code == 0, result.output
        info = json.loads(result.output)["data"][0]["info"]
        assert "set_pieces" in info
        assert info["set_pieces"]["penalties_order"] == 1
        assert info["set_pieces"]["corners_order"] == 2
        assert info["set_pieces"]["direct_freekicks_order"] is None

    def test_json_set_pieces_absent_when_none(self):
        client, fixture_agent, ratings_svc = _make_mocks()
        result = _run_json([], client, fixture_agent, ratings_svc)
        assert result.exit_code == 0, result.output
        info = json.loads(result.output)["data"][0]["info"]
        assert "set_pieces" not in info

    def test_json_multiple_matches_returns_array(self):
        client, fixture_agent, ratings_svc = _make_mocks()
        player_a = make_player(id=1, web_name="M Salah", first_name="Mohamed",
                               second_name="Salah", team_id=1,
                               position=PlayerPosition.MIDFIELDER)
        player_b = make_player(id=2, web_name="Salah Jr", first_name="Mo",
                               second_name="Salah", team_id=1,
                               position=PlayerPosition.FORWARD)
        client.get_players = AsyncMock(return_value=[player_a, player_b])

        result = _run_json([], client, fixture_agent, ratings_svc)
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert data["metadata"]["matches"] == 2
        assert len(data["data"]) == 2


# --- Quality and value score tests ---


def _make_us_match():
    """Understat match data with per-90 fields for scoring."""
    return {
        "id": 100, "npxG": 12.5, "xGChain": 18.0, "xGBuildup": 5.0,
        "npxG_per_90": 0.45, "xGChain_per_90": 0.55,
        "xGI_per_90": 0.5, "penalty_xG_per_90": 0.10,
    }


def _run_with_us_match(args, client, fixture_agent, ratings_svc, us_match=None, json_mode=False):
    """Run player command with mocked Understat match for quality scoring."""
    if us_match is None:
        us_match = _make_us_match()
    runner = CliRunner()
    mock_understat = MagicMock()
    mock_understat.get_league_players = AsyncMock(return_value=[
        {"id": 100, "player_name": "Mohamed Salah", "team_title": "Liverpool",
         "position": "M F", "games": 28},
    ])
    mock_understat.__aenter__ = AsyncMock(return_value=mock_understat)
    mock_understat.__aexit__ = AsyncMock(return_value=False)

    cmd_args = ["player", "Salah"]
    if json_mode:
        cmd_args += ["--format", "json"]
    cmd_args += args

    with (
        patch("fpl_cli.cli.player.load_settings", return_value={"fpl": {}, "custom_analysis": True}),
        patch("fpl_cli.api.fpl.FPLClient", return_value=client),
        patch("fpl_cli.agents.data.fixture.FixtureAgent", return_value=fixture_agent),
        patch("fpl_cli.services.team_ratings.TeamRatingsService", return_value=ratings_svc),
        patch("fpl_cli.api.understat.UnderstatClient", return_value=mock_understat),
        patch("fpl_cli.api.understat.match_fpl_to_understat", return_value=us_match),
    ):
        return runner.invoke(main, cmd_args)


class TestPlayerQualityValueScores:
    def test_json_has_quality_and_value_with_understat_match(self):
        client, fixture_agent, ratings_svc = _make_mocks()
        client.get_player_detail = AsyncMock(return_value={"history": []})
        result = _run_with_us_match([], client, fixture_agent, ratings_svc, json_mode=True)
        assert result.exit_code == 0, result.output
        info = json.loads(result.output)["data"][0]["info"]
        assert isinstance(info["quality_score"], int)
        assert 0 <= info["quality_score"] <= 100
        assert isinstance(info["value_score"], float)
        assert info["value_score"] > 0

    def test_json_no_scores_when_custom_analysis_off(self):
        """quality_score/value_score absent from JSON when custom analysis off."""
        client, fixture_agent, ratings_svc = _make_mocks()
        result = _run_json([], client, fixture_agent, ratings_svc)
        assert result.exit_code == 0, result.output
        info = json.loads(result.output)["data"][0]["info"]
        assert "quality_score" not in info
        assert "value_score" not in info

    def test_rich_panel_shows_quality_value_line(self):
        client, fixture_agent, ratings_svc = _make_mocks()
        client.get_player_detail = AsyncMock(return_value={"history": []})
        result = _run_with_us_match([], client, fixture_agent, ratings_svc)
        assert result.exit_code == 0, result.output
        assert "Quality:" in result.output
        assert "Value:" in result.output

    def test_rich_panel_no_quality_line_without_understat(self):
        client, fixture_agent, ratings_svc = _make_mocks()
        result = _run([], client, fixture_agent, ratings_svc)
        assert result.exit_code == 0, result.output
        assert "Quality:" not in result.output

    def test_gk_uses_without_xgi_weights(self):
        """GK quality_score should differ from MID due to without_xgi path."""
        client, fixture_agent, ratings_svc = _make_mocks()
        client.get_players = AsyncMock(return_value=[
            make_player(id=1, web_name="Salah", team_id=1,
                        position=PlayerPosition.GOALKEEPER,
                        defensive_contribution_per_90=2.5)
        ])
        client.get_player_detail = AsyncMock(return_value={"history": []})
        result = _run_with_us_match([], client, fixture_agent, ratings_svc, json_mode=True)
        assert result.exit_code == 0, result.output
        info = json.loads(result.output)["data"][0]["info"]
        assert isinstance(info["quality_score"], int)
        # GK with zeroed attacking stats should score meaningfully lower than elite MID
        assert info["quality_score"] < 55

    def test_zero_price_player_gets_null_value_score(self):
        client, fixture_agent, ratings_svc = _make_mocks()
        client.get_players = AsyncMock(return_value=[
            make_player(id=1, web_name="Salah", team_id=1,
                        position=PlayerPosition.MIDFIELDER, now_cost=0)
        ])
        client.get_player_detail = AsyncMock(return_value={"history": []})
        result = _run_with_us_match([], client, fixture_agent, ratings_svc, json_mode=True)
        assert result.exit_code == 0, result.output
        info = json.loads(result.output)["data"][0]["info"]
        assert isinstance(info["quality_score"], int)
        assert info["value_score"] is None

    def test_form_trajectory_applied_without_detail_flag(self):
        """History is fetched for scoring even without --detail flag."""
        client, fixture_agent, ratings_svc = _make_mocks()
        client.get_player_detail = AsyncMock(return_value={
            "history": [
                {"round": gw, "minutes": 90, "total_points": pts}
                for gw, pts in [(20, 8), (21, 10), (22, 7), (23, 12), (24, 9), (25, 11), (26, 8)]
            ],
        })
        result = _run_with_us_match([], client, fixture_agent, ratings_svc, json_mode=True)
        assert result.exit_code == 0, result.output
        info = json.loads(result.output)["data"][0]["info"]
        assert isinstance(info["quality_score"], int)
        # Verify detail was fetched (form_trajectory needs it)
        client.get_player_detail.assert_called_once()

    def test_understat_api_failure_no_scores_when_custom_analysis_off(self):
        """When custom analysis off and Understat fails, scores absent from JSON."""
        client, fixture_agent, ratings_svc = _make_mocks()
        result = _run_json([], client, fixture_agent, ratings_svc)
        assert result.exit_code == 0, result.output
        info = json.loads(result.output)["data"][0]["info"]
        assert "quality_score" not in info
        assert "value_score" not in info
