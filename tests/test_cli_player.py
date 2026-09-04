"""Tests for `fpl player` command flags."""
from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
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
    ratings_svc.ensure_fresh = AsyncMock(return_value=None)
    ratings_svc.get_staleness_warning.return_value = None

    return client, fixture_agent, ratings_svc


def _make_empty_understat():
    """UnderstatClient mock that returns no league players."""
    mock = MagicMock()
    mock.get_league_players = AsyncMock(return_value=[])
    mock.__aenter__ = AsyncMock(return_value=mock)
    mock.__aexit__ = AsyncMock(return_value=False)
    return mock


@pytest.fixture(autouse=True)
def _no_third_party_fetches():
    """Module-wide stubs for the fetches `fpl player` makes past FPLClient.

    Every invocation scrapes understat.com for league players, and the
    custom-analysis path fetches the Core-Insights match CSVs — both real
    network calls even when a test patches FPLClient. Both degrade silently
    when unreachable, so a test that forgets to patch them stays green while
    depending on the network. Stub them for the whole module; tests that
    assert on Understat enrichment override with their own patch.
    """
    with (
        patch(
            "fpl_cli.api.understat.UnderstatClient",
            side_effect=lambda *a, **kw: _make_empty_understat(),
        ),
        patch(
            "fpl_cli.cli.player.fetch_match_records",
            new_callable=AsyncMock,
            return_value=None,
        ),
        # The pre-GW10 quality blend loads player priors; an empty cache hit
        # keeps the historical datasets off the network.
        patch("fpl_cli.services.player_prior.load_cached_priors", return_value={}),
    ):
        yield


def _run(args, client, fixture_agent, ratings_svc, settings=None):
    runner = CliRunner()
    with (
        patch("fpl_cli.cli.player.get_settings", return_value=settings or {"fpl": {}}),
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
            patch("fpl_cli.cli.player.get_settings", return_value={"fpl": {}}),
            patch("fpl_cli.api.fpl.FPLClient", return_value=client),
            patch("fpl_cli.agents.data.fixture.FixtureAgent", return_value=fixture_agent),
            patch("fpl_cli.services.team_ratings.TeamRatingsService", return_value=ratings_svc),
            patch("fpl_cli.api.historical.make_historical_provider") as mock_hist,
        ):
            mock_provider = MagicMock()
            mock_provider.get_player_history = AsyncMock(return_value=mock_profile)
            mock_provider.__aenter__ = AsyncMock(return_value=mock_provider)
            mock_provider.__aexit__ = AsyncMock(return_value=False)
            mock_hist.return_value = mock_provider

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
            patch("fpl_cli.cli.player.get_settings", return_value={"fpl": {}}),
            patch("fpl_cli.api.fpl.FPLClient", return_value=client),
            patch("fpl_cli.agents.data.fixture.FixtureAgent", return_value=fixture_agent),
            patch("fpl_cli.services.team_ratings.TeamRatingsService", return_value=ratings_svc),
            patch("fpl_cli.api.historical.make_historical_provider") as mock_hist,
        ):
            mock_provider = MagicMock()
            mock_provider.get_player_history = AsyncMock(return_value=None)
            mock_provider.__aenter__ = AsyncMock(return_value=mock_provider)
            mock_provider.__aexit__ = AsyncMock(return_value=False)
            mock_hist.return_value = mock_provider

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
        # Named Salah because `_run` searches for that name -- seeded as "Raya"
        # the lookup found nothing and the assertion below passed against an
        # empty report rather than a goalkeeper's (#159 review).
        client, fixture_agent, ratings_svc = _make_mocks()
        client.get_players = AsyncMock(return_value=[
            make_player(id=1, web_name="Salah", team_id=1,
                        position=PlayerPosition.GOALKEEPER,
                        defensive_contribution_per_90=2.0)
        ])
        result = _run([], client, fixture_agent, ratings_svc)
        assert result.exit_code == 0, result.output
        assert "Salah" in result.output
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


class TestPlayerUnderstatJoinWarning:
    """A club Understat carries no rows for has to reach the JSON envelope (#229).

    The tripwire is a stderr log line; a `--format json` consumer parses
    stdout, so without the `metadata.warnings` entry it has no way to know
    this player's npxG and quality score are missing for a whole-club reason
    rather than a per-player one.
    """

    @staticmethod
    def _run_json(understat_players):
        client, fixture_agent, ratings_svc = _make_mocks()
        mock_understat = MagicMock()
        mock_understat.get_league_players = AsyncMock(return_value=understat_players)
        mock_understat.get_player = AsyncMock(return_value=None)
        mock_understat.__aenter__ = AsyncMock(return_value=mock_understat)
        mock_understat.__aexit__ = AsyncMock(return_value=False)
        runner = CliRunner()
        # No `match_fpl_to_understat` patch: the miss has to come from the
        # payload, so the real gate is what decides.
        with (
            patch("fpl_cli.cli.player.get_settings", return_value={"fpl": {}}),
            patch("fpl_cli.api.fpl.FPLClient", return_value=client),
            patch("fpl_cli.agents.data.fixture.FixtureAgent", return_value=fixture_agent),
            patch("fpl_cli.services.team_ratings.TeamRatingsService", return_value=ratings_svc),
            patch("fpl_cli.api.understat.UnderstatClient", return_value=mock_understat),
        ):
            return runner.invoke(main, ["player", "Salah", "--format", "json"])

    def test_club_with_no_rows_warns_in_json_metadata(self):
        result = self._run_json(
            [{"name": "Saka", "team": "Arsenal", "position": "M S", "minutes": 900}],
        )
        assert result.exit_code == 0, result.output
        warnings = json.loads(result.output)["metadata"]["warnings"]
        unmatched = [w for w in warnings if w["code"] == "understat_team_unmatched"]
        assert len(unmatched) == 1
        assert "Liverpool" in unmatched[0]["message"]

    def test_resolved_club_adds_no_join_warning(self):
        result = self._run_json(
            [{
                "id": 100, "name": "Salah", "team": "Liverpool",
                "position": "M S", "minutes": 900, "games": 10,
                "npxG": 5.0, "xGChain": 8.0, "xGBuildup": 2.0,
            }],
        )
        assert result.exit_code == 0, result.output
        warnings = json.loads(result.output)["metadata"]["warnings"]
        assert [w for w in warnings if w["code"] == "understat_team_unmatched"] == []


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
            patch("fpl_cli.cli.player.get_settings", return_value={"fpl": {}}),
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
            patch("fpl_cli.cli.player.get_settings", return_value={"fpl": {}}),
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


def _run_json(args, client, fixture_agent, ratings_svc, settings=None):
    """Run the player command with --format json and return parsed JSON."""
    runner = CliRunner()
    with (
        patch("fpl_cli.cli.player.get_settings", return_value=settings or {"fpl": {}}),
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
        assert info["minutes"] == 900

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
            patch("fpl_cli.cli.player.get_settings", return_value={"fpl": {}}),
            patch("fpl_cli.api.fpl.FPLClient", return_value=client),
            patch("fpl_cli.agents.data.fixture.FixtureAgent", return_value=fixture_agent),
            patch("fpl_cli.services.team_ratings.TeamRatingsService", return_value=ratings_svc),
            patch("fpl_cli.api.historical.make_historical_provider") as mock_hist,
        ):
            mock_provider = MagicMock()
            mock_provider.get_player_history = AsyncMock(return_value=mock_profile)
            mock_provider.__aenter__ = AsyncMock(return_value=mock_provider)
            mock_provider.__aexit__ = AsyncMock(return_value=False)
            mock_hist.return_value = mock_provider

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

    def test_json_contains_ep_next_and_ep_this(self):
        client, fixture_agent, ratings_svc = _make_mocks()
        client.get_players = AsyncMock(return_value=[
            make_player(id=308, web_name="Salah", first_name="Mohamed",
                        second_name="Salah", team_id=1,
                        position=PlayerPosition.MIDFIELDER,
                        ep_next=7.5, ep_this=3.2),
        ])
        result = _run_json([], client, fixture_agent, ratings_svc)
        assert result.exit_code == 0, result.output
        info = json.loads(result.output)["data"][0]["info"]
        assert info["ep_next"] == 7.5
        assert info["ep_this"] == 3.2

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
        patch("fpl_cli.cli.player.get_settings", return_value={"fpl": {}, "custom_analysis": True}),
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
        assert isinstance(info["quality_per_m"], float)
        assert info["quality_per_m"] > 0

    def test_json_no_scores_when_custom_analysis_off(self):
        """quality_score/quality_per_m absent from JSON when custom analysis off."""
        client, fixture_agent, ratings_svc = _make_mocks()
        result = _run_json([], client, fixture_agent, ratings_svc)
        assert result.exit_code == 0, result.output
        info = json.loads(result.output)["data"][0]["info"]
        assert "quality_score" not in info
        assert "quality_per_m" not in info

    def test_rich_panel_shows_quality_value_line(self):
        client, fixture_agent, ratings_svc = _make_mocks()
        client.get_player_detail = AsyncMock(return_value={"history": []})
        result = _run_with_us_match([], client, fixture_agent, ratings_svc)
        assert result.exit_code == 0, result.output
        assert "Quality:" in result.output
        assert "Quality/£m:" in result.output

    def test_rich_panel_no_quality_line_without_understat(self):
        client, fixture_agent, ratings_svc = _make_mocks()
        result = _run([], client, fixture_agent, ratings_svc)
        assert result.exit_code == 0, result.output
        assert "Quality:" not in result.output

    def test_rich_panel_shows_ep_next_on_points_line(self):
        client, fixture_agent, ratings_svc = _make_mocks()
        client.get_players = AsyncMock(return_value=[
            make_player(id=308, web_name="Salah", first_name="Mohamed",
                        second_name="Salah", team_id=1,
                        position=PlayerPosition.MIDFIELDER,
                        ep_next=8.5),
        ])
        result = _run([], client, fixture_agent, ratings_svc)
        assert result.exit_code == 0, result.output
        assert "xPts: 8.5" in result.output
        assert "ep_this" not in result.output

    def test_rich_panel_omits_ep_next_when_none(self):
        client, fixture_agent, ratings_svc = _make_mocks()
        client.get_players = AsyncMock(return_value=[
            make_player(id=308, web_name="Salah", first_name="Mohamed",
                        second_name="Salah", team_id=1,
                        position=PlayerPosition.MIDFIELDER,
                        ep_next=None),
        ])
        result = _run([], client, fixture_agent, ratings_svc)
        assert result.exit_code == 0, result.output
        assert "xPts" not in result.output
        # Rest of the Points line must still render — confirms only the xPts
        # segment was dropped, not the entire row.
        assert "Points:" in result.output
        assert "PPG:" in result.output

    def test_rich_panel_shows_ep_next_when_zero(self):
        # Guards against a future `if p.ep_next:` regression that would
        # swallow genuine 0.0 projections (distinct from None).
        client, fixture_agent, ratings_svc = _make_mocks()
        client.get_players = AsyncMock(return_value=[
            make_player(id=308, web_name="Salah", first_name="Mohamed",
                        second_name="Salah", team_id=1,
                        position=PlayerPosition.MIDFIELDER,
                        ep_next=0.0),
        ])
        result = _run([], client, fixture_agent, ratings_svc)
        assert result.exit_code == 0, result.output
        assert "xPts: 0.0" in result.output

    def test_json_ep_next_none_serialises_as_null(self):
        client, fixture_agent, ratings_svc = _make_mocks()
        client.get_players = AsyncMock(return_value=[
            make_player(id=308, web_name="Salah", first_name="Mohamed",
                        second_name="Salah", team_id=1,
                        position=PlayerPosition.MIDFIELDER,
                        ep_next=None, ep_this=None),
        ])
        result = _run_json([], client, fixture_agent, ratings_svc)
        assert result.exit_code == 0, result.output
        info = json.loads(result.output)["data"][0]["info"]
        assert info["ep_next"] is None
        assert info["ep_this"] is None

    def test_quality_score_is_prior_informed_before_the_cutoff(self):
        """Same blend as fpl stats --value: at GW2 the score moves with the
        player's prior, and the priors are loaded over the whole pool (#143).
        """
        from fpl_cli.services.player_prior import PlayerPrior, _compute_confidence

        def _score(priors):
            client, fixture_agent, ratings_svc = _make_mocks()
            client.get_next_gameweek = AsyncMock(return_value={"id": 2})
            client.get_player_detail = AsyncMock(return_value={"history": []})
            loader = AsyncMock(return_value=priors)
            with patch("fpl_cli.cli.player.load_or_generate_player_priors", loader):
                result = _run_with_us_match([], client, fixture_agent, ratings_svc, json_mode=True)
            assert result.exit_code == 0, result.output
            loader.assert_awaited_once()
            return json.loads(result.output)["data"][0]["info"]["quality_score"]

        observed = _score(None)
        elite = _score({1: PlayerPrior(1.0, _compute_confidence(2, 1.0), "history")})
        assert elite != observed

    def test_json_metadata_says_the_score_is_prior_informed(self):
        """PR #208 review: the blend must announce itself in the same slot
        fpl stats --value uses, keyed on whether the priors actually loaded.
        """
        from fpl_cli.services.player_prior import PlayerPrior, _compute_confidence

        def _warnings(priors, next_gw_id=2):
            client, fixture_agent, ratings_svc = _make_mocks()
            client.get_next_gameweek = AsyncMock(return_value={"id": next_gw_id})
            client.get_player_detail = AsyncMock(return_value={"history": []})
            with patch(
                "fpl_cli.cli.player.load_or_generate_player_priors",
                AsyncMock(return_value=priors),
            ):
                result = _run_with_us_match([], client, fixture_agent, ratings_svc, json_mode=True)
            assert result.exit_code == 0, result.output
            return json.loads(result.output)["metadata"]["warnings"]

        blended = _warnings({1: PlayerPrior(1.0, _compute_confidence(2, 1.0), "history")})
        assert [w["code"] for w in blended] == ["early_season_prior_informed"]
        assert "25%-50%" in blended[0]["message"]

        degraded = _warnings(None)
        assert [w["code"] for w in degraded] == ["early_season_small_sample"]

        assert _warnings(None, next_gw_id=30) == []

    def test_table_mode_prints_the_early_season_notice(self):
        from fpl_cli.services.player_prior import PlayerPrior, _compute_confidence

        client, fixture_agent, ratings_svc = _make_mocks()
        client.get_next_gameweek = AsyncMock(return_value={"id": 2})
        client.get_player_detail = AsyncMock(return_value={"history": []})
        priors = {1: PlayerPrior(1.0, _compute_confidence(2, 1.0), "history")}
        with patch(
            "fpl_cli.cli.player.load_or_generate_player_priors",
            AsyncMock(return_value=priors),
        ):
            result = _run_with_us_match([], client, fixture_agent, ratings_svc)
        assert result.exit_code == 0, result.output
        assert "Early-season notice" in result.output
        assert "Quality:" in result.output

    def test_no_notice_without_a_quality_score(self):
        """Nothing to caveat when no Understat match produced a score."""
        client, fixture_agent, ratings_svc = _make_mocks()
        client.get_next_gameweek = AsyncMock(return_value={"id": 2})
        result = _run_json([], client, fixture_agent, ratings_svc,
                           settings={"fpl": {}, "custom_analysis": True})
        assert result.exit_code == 0, result.output
        assert json.loads(result.output)["metadata"]["warnings"] == []

    def test_priors_are_not_loaded_from_the_cutoff(self):
        client, fixture_agent, ratings_svc = _make_mocks()  # next GW 30
        client.get_player_detail = AsyncMock(return_value={"history": []})
        loader = AsyncMock(return_value=None)
        with patch("fpl_cli.cli.player.load_or_generate_player_priors", loader):
            result = _run_with_us_match([], client, fixture_agent, ratings_svc, json_mode=True)
        assert result.exit_code == 0, result.output
        loader.assert_not_awaited()

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
        # GK path (GK weights, GK_VALUE_CEILING 14.42) yields 68 here vs 96 via
        # the MID path on identical inputs — assert meaningfully below the MID
        # path's score without pinning the exact GK value.
        assert info["quality_score"] < 80

    def test_zero_price_player_gets_null_quality_per_m(self):
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
        assert info["quality_per_m"] is None

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
        assert "quality_per_m" not in info


class TestPlayerRollingPtsPerM:
    """Tests for rolling_pts_per_m in fpl player output."""

    def test_json_includes_rolling_with_history(self):
        """rolling_pts_per_m and rolling_fixture_count in JSON when history available."""
        client, fixture_agent, ratings_svc = _make_mocks()
        client.get_player_detail = AsyncMock(return_value={
            "history": [
                {"round": gw, "minutes": 90, "total_points": 6, "fixture": 100 + gw}
                for gw in range(20, 25)
            ],
        })
        result = _run_with_us_match([], client, fixture_agent, ratings_svc, json_mode=True)
        assert result.exit_code == 0, result.output
        info = json.loads(result.output)["data"][0]["info"]
        assert "rolling_pts_per_m" in info
        assert "rolling_fixture_count" in info
        assert info["rolling_fixture_count"] == 5

    def test_json_null_rolling_with_sparse_history(self):
        """rolling_pts_per_m null when <3 qualifying fixtures."""
        client, fixture_agent, ratings_svc = _make_mocks()
        client.get_player_detail = AsyncMock(return_value={
            "history": [
                {"round": 23, "minutes": 90, "total_points": 6, "fixture": 200},
                {"round": 24, "minutes": 90, "total_points": 8, "fixture": 201},
            ],
        })
        result = _run_with_us_match([], client, fixture_agent, ratings_svc, json_mode=True)
        assert result.exit_code == 0, result.output
        info = json.loads(result.output)["data"][0]["info"]
        assert info["rolling_pts_per_m"] is None
        assert info["rolling_fixture_count"] is None

    def test_rich_panel_shows_rolling_with_history(self):
        client, fixture_agent, ratings_svc = _make_mocks()
        client.get_player_detail = AsyncMock(return_value={
            "history": [
                {"round": gw, "minutes": 90, "total_points": 6, "fixture": 100 + gw}
                for gw in range(20, 25)
            ],
        })
        result = _run_with_us_match([], client, fixture_agent, ratings_svc)
        assert result.exit_code == 0, result.output
        assert "Rolling:" in result.output
        assert "/£m" in result.output

    def test_rich_panel_asterisk_when_fewer_fixtures(self):
        """Asterisk shown when fixture_count < window."""
        client, fixture_agent, ratings_svc = _make_mocks()
        client.get_player_detail = AsyncMock(return_value={
            "history": [
                {"round": gw, "minutes": 90, "total_points": 6, "fixture": 100 + gw}
                for gw in range(22, 25)  # 3 fixtures, window default is 5
            ],
        })
        result = _run_with_us_match([], client, fixture_agent, ratings_svc)
        assert result.exit_code == 0, result.output
        assert "*" in result.output

    def test_json_null_rolling_when_price_zero(self):
        client, fixture_agent, ratings_svc = _make_mocks()
        client.get_players = AsyncMock(return_value=[
            make_player(id=1, web_name="Salah", team_id=1,
                        position=PlayerPosition.MIDFIELDER, now_cost=0)
        ])
        client.get_player_detail = AsyncMock(return_value={
            "history": [
                {"round": gw, "minutes": 90, "total_points": 6, "fixture": 100 + gw}
                for gw in range(20, 25)
            ],
        })
        result = _run_with_us_match([], client, fixture_agent, ratings_svc, json_mode=True)
        assert result.exit_code == 0, result.output
        info = json.loads(result.output)["data"][0]["info"]
        assert info["rolling_pts_per_m"] is None


# --- xGI sustainability display tests ---


def _make_overperforming_history(current_gw: int = 30, n: int = 7) -> list[dict]:
    """7 GWs where GI exceeds xGI by 0.3/match -> divergence +0.3 -> multiplier 0.85."""
    return [
        {
            "round": current_gw - n + i, "minutes": 90,
            "goals_scored": 1, "assists": 0,
            "expected_goals": "0.50", "expected_assists": "0.20",
        }
        for i in range(n)
    ]


def _make_underperforming_history(current_gw: int = 30, n: int = 7) -> list[dict]:
    """7 GWs where xGI exceeds GI by 0.3/match -> divergence -0.3 -> multiplier 1.15."""
    return [
        {
            "round": current_gw - n + i, "minutes": 90,
            "goals_scored": 0, "assists": 0,
            "expected_goals": "0.25", "expected_assists": "0.05",
        }
        for i in range(n)
    ]


_CUSTOM_SETTINGS = {"fpl": {}, "custom_analysis": True}


class TestXgiSustainabilityDisplay:
    def test_atk_overperformer_shows_sustainability_line(self):
        client, fixture_agent, ratings_svc = _make_mocks()
        client.get_player_detail = AsyncMock(return_value={
            "history": _make_overperforming_history(),
        })
        result = _run(["--detail"], client, fixture_agent, ratings_svc, settings=_CUSTOM_SETTINGS)
        assert result.exit_code == 0, result.output
        assert "xGI Sustainability" in result.output
        assert "0.85x form" in result.output

    def test_atk_underperformer_shows_sustainability_line(self):
        client, fixture_agent, ratings_svc = _make_mocks()
        client.get_player_detail = AsyncMock(return_value={
            "history": _make_underperforming_history(),
        })
        result = _run(["--detail"], client, fixture_agent, ratings_svc, settings=_CUSTOM_SETTINGS)
        assert result.exit_code == 0, result.output
        assert "xGI Sustainability" in result.output
        assert "1.15x form" in result.output

    def test_def_player_shows_no_sustainability_line(self):
        client, fixture_agent, ratings_svc = _make_mocks()
        client.get_players = AsyncMock(return_value=[
            make_player(id=1, web_name="Salah", team_id=1,
                        position=PlayerPosition.DEFENDER)
        ])
        client.get_player_detail = AsyncMock(return_value={
            "history": _make_overperforming_history(),
        })
        result = _run(["--detail"], client, fixture_agent, ratings_svc, settings=_CUSTOM_SETTINGS)
        assert result.exit_code == 0, result.output
        assert "xGI Sustainability" not in result.output

    def test_insufficient_history_shows_no_sustainability_line(self):
        client, fixture_agent, ratings_svc = _make_mocks()
        # Only 3 qualifying GWs (below 4-GW minimum)
        client.get_player_detail = AsyncMock(return_value={
            "history": _make_overperforming_history(n=3),
        })
        result = _run(["--detail"], client, fixture_agent, ratings_svc, settings=_CUSTOM_SETTINGS)
        assert result.exit_code == 0, result.output
        assert "xGI Sustainability" not in result.output

    def test_json_with_detail_includes_sustainability_fields(self):
        client, fixture_agent, ratings_svc = _make_mocks()
        client.get_player_detail = AsyncMock(return_value={
            "history": _make_overperforming_history(),
        })
        result = _run_json(["--detail"], client, fixture_agent, ratings_svc, settings=_CUSTOM_SETTINGS)
        assert result.exit_code == 0, result.output
        info = json.loads(result.output)["data"][0]["info"]
        assert "xgi_sustainability" in info
        assert "xgi_divergence" in info
        assert info["xgi_sustainability"] == 0.85
        assert info["xgi_divergence"] == pytest.approx(0.3, abs=0.01)

    def test_json_without_detail_omits_sustainability_fields(self):
        client, fixture_agent, ratings_svc = _make_mocks()
        result = _run_json([], client, fixture_agent, ratings_svc)
        assert result.exit_code == 0, result.output
        info = json.loads(result.output)["data"][0]["info"]
        assert "xgi_sustainability" not in info
        assert "xgi_divergence" not in info


# ---------------------------------------------------------------------------
# Adjusted npxG display tests (Unit 4)
# ---------------------------------------------------------------------------

def _run_with_adjusted_npxg(args, client, fixture_agent, ratings_svc, npxg_lookup, json_mode=False):
    """Run player command with mocked fetch_match_records and Understat."""
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

    # Return sentinel match records so the npxg branch activates,
    # then mock build_npxg_lookup_from_records to return the desired lookup.
    mock_records = {"_sentinel": []} if npxg_lookup else None

    with (
        patch("fpl_cli.cli.player.get_settings", return_value=_CUSTOM_SETTINGS),
        patch("fpl_cli.api.fpl.FPLClient", return_value=client),
        patch("fpl_cli.agents.data.fixture.FixtureAgent", return_value=fixture_agent),
        patch("fpl_cli.services.team_ratings.TeamRatingsService", return_value=ratings_svc),
        patch("fpl_cli.api.understat.UnderstatClient", return_value=mock_understat),
        patch("fpl_cli.api.understat.match_fpl_to_understat", return_value=us_match),
        patch("fpl_cli.cli.player.fetch_match_records", new_callable=AsyncMock, return_value=mock_records),
        patch("fpl_cli.cli.player.build_npxg_lookup_from_records", return_value=npxg_lookup or {}),
    ):
        return runner.invoke(main, cmd_args)


class TestAdjustedNpxgDisplay:
    """Unit 4: adjusted npxG display in Rich panel and JSON."""

    def test_json_includes_adjusted_and_raw_when_custom_on(self):
        client, fixture_agent, ratings_svc = _make_mocks()
        client.get_player_detail = AsyncMock(return_value={"history": []})
        lookup = {1: 0.22}  # player ID 1 -> adjusted npxG 0.22
        result = _run_with_adjusted_npxg([], client, fixture_agent, ratings_svc, lookup, json_mode=True)
        assert result.exit_code == 0, result.output
        info = json.loads(result.output)["data"][0]["info"]
        assert "adjusted_npxg_per_90" in info
        assert "raw_npxg_per_90" in info
        assert info["raw_npxg_per_90"] == pytest.approx(0.45)  # from _make_us_match

    def test_json_excludes_adjusted_when_custom_off(self):
        client, fixture_agent, ratings_svc = _make_mocks()
        result = _run_json([], client, fixture_agent, ratings_svc, settings={"fpl": {}})
        assert result.exit_code == 0, result.output
        info = json.loads(result.output)["data"][0]["info"]
        assert "adjusted_npxg_per_90" not in info
        assert "raw_npxg_per_90" not in info

    def test_json_omits_adjusted_when_no_ci_data(self):
        """No adjusted field when player absent from lookup (fallback to raw)."""
        client, fixture_agent, ratings_svc = _make_mocks()
        client.get_player_detail = AsyncMock(return_value={"history": []})
        lookup = {999: 0.22}  # wrong player ID - player 1 not in lookup
        result = _run_with_adjusted_npxg([], client, fixture_agent, ratings_svc, lookup, json_mode=True)
        assert result.exit_code == 0, result.output
        info = json.loads(result.output)["data"][0]["info"]
        assert "adjusted_npxg_per_90" not in info
        assert info["raw_npxg_per_90"] == pytest.approx(0.45)

    def test_rich_panel_shows_adj_npxg_line_when_custom_on(self):
        client, fixture_agent, ratings_svc = _make_mocks()
        client.get_player_detail = AsyncMock(return_value={"history": []})
        lookup = {1: 0.22}
        result = _run_with_adjusted_npxg([], client, fixture_agent, ratings_svc, lookup)
        assert result.exit_code == 0, result.output
        assert "adj. npxG/90:" in result.output
        assert "(raw:" in result.output

    def test_rich_panel_no_adj_npxg_when_no_ci_data(self):
        """adj. npxG line absent when no match records for this player."""
        client, fixture_agent, ratings_svc = _make_mocks()
        client.get_player_detail = AsyncMock(return_value={"history": []})
        lookup = {999: 0.22}  # wrong player ID
        result = _run_with_adjusted_npxg([], client, fixture_agent, ratings_svc, lookup)
        assert result.exit_code == 0, result.output
        assert "adj. npxG/90:" not in result.output


class TestPlayerErrorHandling:
    """A raised exception must be reported AND fail the process (#47)."""

    def test_exception_exits_nonzero(self):
        client, fixture_agent, ratings_svc = _make_mocks()
        client.get_teams = AsyncMock(side_effect=ValueError("boom"))
        result = _run([], client, fixture_agent, ratings_svc)
        assert result.exit_code != 0
        assert "Could not load player data: boom" in result.output
        assert "adj. npxG/90:" not in result.output


class TestUnderstatFetchDeferral:
    """The league-wide Understat scrape waits until a name resolves (#83)."""

    def _invoke(self, name, client, fixture_agent, ratings_svc):
        mock_understat = _make_empty_understat()
        runner = CliRunner()
        with (
            patch("fpl_cli.cli.player.get_settings", return_value={"fpl": {}}),
            patch("fpl_cli.api.fpl.FPLClient", return_value=client),
            patch("fpl_cli.agents.data.fixture.FixtureAgent", return_value=fixture_agent),
            patch("fpl_cli.services.team_ratings.TeamRatingsService", return_value=ratings_svc),
            patch("fpl_cli.api.understat.UnderstatClient", return_value=mock_understat),
        ):
            result = runner.invoke(main, ["player", name])
        return result, mock_understat

    def test_unmatched_name_skips_league_scrape(self):
        client, fixture_agent, ratings_svc = _make_mocks()
        result, mock_understat = self._invoke("Zzzzzz", client, fixture_agent, ratings_svc)
        assert result.exit_code == 1
        assert "No players found matching 'Zzzzzz'" in result.stderr
        mock_understat.get_league_players.assert_not_awaited()

    def test_matched_name_still_scrapes_league(self):
        client, fixture_agent, ratings_svc = _make_mocks()
        result, mock_understat = self._invoke("Salah", client, fixture_agent, ratings_svc)
        assert result.exit_code == 0, result.output
        mock_understat.get_league_players.assert_awaited_once()


def _make_draft_client():
    """FPLDraftClient mock owning Salah (draft id 99) via entry 7."""
    mock = MagicMock()
    mock.get_league_details = AsyncMock(return_value={
        "league_entries": [
            {"entry_id": 7, "player_first_name": "Ross", "player_last_name": "Groom"},
        ],
    })
    mock.get_bootstrap_static = AsyncMock(return_value={
        "elements": [{"id": 99, "web_name": "Salah", "team": 1}],
    })
    mock.get_league_ownership = AsyncMock(return_value={99: 7})
    mock.__aenter__ = AsyncMock(return_value=mock)
    mock.__aexit__ = AsyncMock(return_value=False)
    return mock


class TestDraftFetchDeferral:
    """Draft ownership fetches wait until a name resolves (#83).

    The block costs a league-details fetch, a draft bootstrap, game state and
    one squad fetch per league entry -- 4 + N requests, all of it discarded
    when the name matches nothing.
    """

    def _invoke(self, name, client, fixture_agent, ratings_svc):
        from fpl_cli.cli._context import Format

        draft_client = _make_draft_client()
        runner = CliRunner()
        with (
            patch("fpl_cli.cli.resolve_format", return_value=Format.DRAFT),
            patch("fpl_cli.cli.player.get_settings",
                  return_value={"fpl": {"draft_league_id": 12345}}),
            patch("fpl_cli.api.fpl.FPLClient", return_value=client),
            patch("fpl_cli.api.fpl_draft.FPLDraftClient", return_value=draft_client),
            patch("fpl_cli.agents.data.fixture.FixtureAgent", return_value=fixture_agent),
            patch("fpl_cli.services.team_ratings.TeamRatingsService", return_value=ratings_svc),
        ):
            result = runner.invoke(main, ["player", name])
        return result, draft_client

    def test_unmatched_name_skips_draft_fetches(self):
        client, fixture_agent, ratings_svc = _make_mocks()
        result, draft = self._invoke("Zzzzzz", client, fixture_agent, ratings_svc)
        assert result.exit_code == 1
        assert "No players found matching 'Zzzzzz'" in result.stderr
        draft.get_league_details.assert_not_awaited()
        draft.get_bootstrap_static.assert_not_awaited()
        draft.get_league_ownership.assert_not_awaited()

    def test_matched_name_still_shows_draft_ownership(self):
        client, fixture_agent, ratings_svc = _make_mocks()
        result, draft = self._invoke("Salah", client, fixture_agent, ratings_svc)
        assert result.exit_code == 0, result.output
        assert "Ross Groom" in result.output
        draft.get_league_ownership.assert_awaited_once()

    def test_league_details_fetched_once_and_reused(self):
        """get_league_details is not memoised on the client, so it is passed through."""
        client, fixture_agent, ratings_svc = _make_mocks()
        result, draft = self._invoke("Salah", client, fixture_agent, ratings_svc)
        assert result.exit_code == 0, result.output
        draft.get_league_details.assert_awaited_once()
        args = draft.get_league_ownership.await_args.args
        assert args[2] == draft.get_league_details.return_value
