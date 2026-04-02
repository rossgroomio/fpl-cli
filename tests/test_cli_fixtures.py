"""Tests for the fixtures CLI command."""

import json
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from click.testing import CliRunner

from fpl_cli.cli import main
from fpl_cli.services.team_ratings import TeamRating
from tests.conftest import make_fixture, make_team


@pytest.fixture
def runner():
    return CliRunner()


def _mock_fpl_client(teams, fixtures):
    client = MagicMock()
    client.get_next_gameweek = AsyncMock(return_value={"id": 32})
    client.get_fixtures = AsyncMock(return_value=fixtures)
    client.get_teams = AsyncMock(return_value=teams)
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)
    return client


def _mock_ratings_service():
    """Build a mock TeamRatingsService with MCI strong, CHE mid."""
    def _get_rating(short):
        if short == "MCI":
            return TeamRating(atk_home=1, atk_away=2, def_home=1, def_away=2)
        if short == "CHE":
            return TeamRating(atk_home=3, atk_away=4, def_home=3, def_away=4)
        return None

    mock_svc = MagicMock()
    mock_svc.get_rating.side_effect = _get_rating
    mock_svc.ensure_fresh = AsyncMock()
    return mock_svc


def _standard_teams_and_fixtures(*, finished=False, kickoff_time=None):
    """Return standard CHE vs MCI teams and fixture for reuse."""
    teams = [
        make_team(id=1, name="Chelsea", short_name="CHE", position=4),
        make_team(id=2, name="Man City", short_name="MCI", position=1),
    ]
    kt = kickoff_time or datetime(2026, 3, 29, 15, 30)
    fixtures = [make_fixture(
        id=1, gameweek=32, home_team_id=1, away_team_id=2,
        home_difficulty=2, away_difficulty=4,
        kickoff_time=kt,
        finished=finished,
        home_score=2 if finished else None,
        away_score=1 if finished else None,
    )]
    return teams, fixtures


class TestFixturesCommandFDR:
    """Test FDR values in fixtures output use inverted team ratings."""

    def test_strong_opponent_shows_high_fdr(self, runner):
        """Strong opponent (low avg_overall) should display high FDR (hard)."""
        teams, fixtures = _standard_teams_and_fixtures()
        client = _mock_fpl_client(teams, fixtures)
        mock_svc = _mock_ratings_service()

        with (
            patch("fpl_cli.api.fpl.FPLClient", return_value=client),
            patch("fpl_cli.services.team_ratings.TeamRatingsService", return_value=mock_svc),
        ):
            result = runner.invoke(main, ["fixtures", "-g", "32"])

        assert result.exit_code == 0
        # CHE home FDR = MCI avg_overall_fdr = 8 - 1.5 = 6.5 (hard)
        assert "6.5" in result.output
        # MCI away FDR = CHE avg_overall_fdr = 8 - 3.5 = 4.5 (medium-hard)
        assert "4.5" in result.output


class TestFixturesJsonFormat:
    """Test --format json output for the fixtures command."""

    def test_json_happy_path(self, runner):
        """--format json produces valid JSON with correct envelope."""
        teams, fixtures = _standard_teams_and_fixtures()
        client = _mock_fpl_client(teams, fixtures)
        mock_svc = _mock_ratings_service()

        with (
            patch("fpl_cli.api.fpl.FPLClient", return_value=client),
            patch("fpl_cli.services.team_ratings.TeamRatingsService", return_value=mock_svc),
        ):
            result = runner.invoke(main, ["fixtures", "-g", "32", "--format", "json"])

        assert result.exit_code == 0
        payload = json.loads(result.output)
        assert payload["command"] == "fixtures"
        assert isinstance(payload["data"], list)
        assert len(payload["data"]) == 1
        assert payload["metadata"]["gameweek"] == 32

    def test_json_fixture_dict_keys(self, runner):
        """Fixture dict contains expected keys."""
        teams, fixtures = _standard_teams_and_fixtures()
        client = _mock_fpl_client(teams, fixtures)
        mock_svc = _mock_ratings_service()

        with (
            patch("fpl_cli.api.fpl.FPLClient", return_value=client),
            patch("fpl_cli.services.team_ratings.TeamRatingsService", return_value=mock_svc),
        ):
            result = runner.invoke(main, ["fixtures", "-g", "32", "--format", "json"])

        fixture_dict = json.loads(result.output)["data"][0]
        expected_keys = {"home", "away", "home_fdr", "away_fdr", "kickoff", "finished", "home_score", "away_score"}
        assert expected_keys == set(fixture_dict.keys())
        assert fixture_dict["home"] == "CHE"
        assert fixture_dict["away"] == "MCI"
        assert fixture_dict["kickoff"] == "2026-03-29T15:30:00"

    def test_json_finished_fixture_has_scores(self, runner):
        """Finished fixture includes home_score and away_score."""
        teams, fixtures = _standard_teams_and_fixtures(finished=True)
        client = _mock_fpl_client(teams, fixtures)
        mock_svc = _mock_ratings_service()

        with (
            patch("fpl_cli.api.fpl.FPLClient", return_value=client),
            patch("fpl_cli.services.team_ratings.TeamRatingsService", return_value=mock_svc),
        ):
            result = runner.invoke(main, ["fixtures", "-g", "32", "--format", "json"])

        fixture_dict = json.loads(result.output)["data"][0]
        assert fixture_dict["finished"] is True
        assert fixture_dict["home_score"] == 2
        assert fixture_dict["away_score"] == 1

    def test_table_format_unchanged(self, runner):
        """Default --format table output contains no JSON."""
        teams, fixtures = _standard_teams_and_fixtures()
        client = _mock_fpl_client(teams, fixtures)
        mock_svc = _mock_ratings_service()

        with (
            patch("fpl_cli.api.fpl.FPLClient", return_value=client),
            patch("fpl_cli.services.team_ratings.TeamRatingsService", return_value=mock_svc),
        ):
            result = runner.invoke(main, ["fixtures", "-g", "32"])

        assert result.exit_code == 0
        # Should contain table content, not JSON
        assert "CHE" in result.output
        assert '"command"' not in result.output

    def test_json_error_on_api_failure(self, runner):
        """API failure returns error JSON on stderr and exit code 1."""
        client = MagicMock()
        client.get_next_gameweek = AsyncMock(return_value={"id": 32})
        client.get_fixtures = AsyncMock(side_effect=RuntimeError("API down"))
        client.get_teams = AsyncMock(return_value=[])
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock(return_value=False)

        mock_svc = MagicMock()
        mock_svc.ensure_fresh = AsyncMock()

        with (
            patch("fpl_cli.api.fpl.FPLClient", return_value=client),
            patch("fpl_cli.services.team_ratings.TeamRatingsService", return_value=mock_svc),
        ):
            result = runner.invoke(main, ["fixtures", "-g", "32", "--format", "json"])

        assert result.exit_code == 1
        # emit_json_error writes to stderr; CliRunner mixes streams by default
        error_payload = json.loads(result.output)
        assert error_payload["command"] == "fixtures"
        assert "API down" in error_payload["error"]
