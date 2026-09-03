"""Tests for the fixtures CLI command."""

import json
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import yaml
from click.testing import CliRunner

from fpl_cli.agents.data.fixture import FixtureAgent
from fpl_cli.cli import main
from fpl_cli.services.team_ratings import TeamRatingsService
from tests.conftest import make_fixture, make_team

# MCI strong, CHE mid; BOU deliberately unrated.
_RATINGS = {
    "MCI": {"atk_home": 1, "atk_away": 2, "def_home": 1, "def_away": 2},
    "CHE": {"atk_home": 3, "atk_away": 4, "def_home": 3, "def_away": 4},
}


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


def _ratings_service(tmp_path, ratings=None):
    """A real TeamRatingsService over a temp ratings file, with refresh disabled.

    Real rather than mocked so these tests exercise the same
    ``get_fixture_fdr`` the fixture agent calls - a mock would let the two
    surfaces drift apart again without failing anything (#202).
    """
    config = tmp_path / "team_ratings.yaml"
    with open(config, "w", encoding="utf-8") as f:
        yaml.dump({
            "metadata": {
                "last_updated": datetime.now().strftime("%Y-%m-%d"),
                "source": "test",
                "staleness_threshold_days": 30,
            },
            "ratings": _RATINGS if ratings is None else ratings,
        }, f)
    service = TeamRatingsService(config_path=config)
    service.ensure_fresh = AsyncMock()
    return service


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


def _run_fixtures(runner, tmp_path, *args, ratings=None, custom_analysis=True):
    """Invoke `fpl fixtures` against the temp ratings, returning the click result."""
    teams, fixtures = _standard_teams_and_fixtures()
    client = _mock_fpl_client(teams, fixtures)
    service = _ratings_service(tmp_path, ratings=ratings)

    with (
        patch("fpl_cli.cli.fixtures.is_custom_analysis_enabled", return_value=custom_analysis),
        patch("fpl_cli.api.fpl.FPLClient", return_value=client),
        patch("fpl_cli.services.team_ratings.TeamRatingsService", return_value=service),
    ):
        return runner.invoke(main, ["fixtures", "-g", "32", *args])


class TestFixturesCommandFDR:
    """The general FDR `fpl fixtures` shows is the one the fixture agent scores."""

    def test_matches_the_fixture_agent_on_the_same_match(self, runner, tmp_path):
        """The headline symptom of #202: two numbers for one match under one header."""
        service = _ratings_service(tmp_path)
        agent = FixtureAgent()
        agent.ratings_service = service

        result = _run_fixtures(runner, tmp_path, "--format", "json")

        assert result.exit_code == 0
        fixture_dict = json.loads(result.output)["data"][0]
        assert fixture_dict["home_fdr"] == agent.general_fdr("CHE", "MCI", is_home=True)
        assert fixture_dict["away_fdr"] == agent.general_fdr("MCI", "CHE", is_home=False)

    def test_fdr_is_venue_aware_and_sees_both_teams(self, runner, tmp_path):
        """CHE at home to MCI: ATK and DEF both (8 - 2 + 3) / 2 = 4.5, so FDR 4.5.

        The venue-blind opponent average this replaced said 6.5 for CHE and 4.5
        for MCI - MCI's and CHE's overall strength, with no read on the fixture.
        """
        result = _run_fixtures(runner, tmp_path, "--format", "json")

        fixture_dict = json.loads(result.output)["data"][0]
        assert fixture_dict["home_fdr"] == 4.5
        # MCI away at CHE: (8 - 3 + 2) / 2 = 3.5 on both axes
        assert fixture_dict["away_fdr"] == 3.5

    def test_opponent_mode_drops_the_team_s_own_strength(self, runner, tmp_path):
        """`-m opponent` scores the opponent at the venue only, as in `fpl fdr`."""
        result = _run_fixtures(runner, tmp_path, "-m", "opponent", "--format", "json")

        fixture_dict = json.loads(result.output)["data"][0]
        assert fixture_dict["home_fdr"] == 6.0  # 8 - MCI's away axes (2)
        assert fixture_dict["away_fdr"] == 5.0  # 8 - CHE's home axes (3)
        assert json.loads(result.output)["metadata"]["fdr_mode"] == "opponent"

    def test_defaults_to_difference_mode(self, runner, tmp_path):
        """Same default as the fixture agent, so the two agree without a flag."""
        payload = json.loads(_run_fixtures(runner, tmp_path, "--format", "json").output)

        assert payload["metadata"]["fdr_mode"] == "difference"

    def test_unrated_club_scores_the_neutral_four(self, runner, tmp_path):
        """Not the FPL API's 1-5 difficulty, which would sit on a second scale."""
        # The fixture carries home_difficulty=2 / away_difficulty=4 from the API
        result = _run_fixtures(runner, tmp_path, "--format", "json", ratings={
            "CHE": _RATINGS["CHE"],
        })

        fixture_dict = json.loads(result.output)["data"][0]
        assert fixture_dict["home_fdr"] == 4.0
        assert fixture_dict["away_fdr"] == 4.0

    def test_table_names_the_scale_and_mode(self, runner, tmp_path):
        """Nothing on screen said which model the column was on (#202)."""
        result = _run_fixtures(runner, tmp_path)

        assert result.exit_code == 0
        assert "1 (easiest) - 7 (hardest)" in result.output
        assert "difference mode" in result.output


class TestFixturesRatingsWarning:
    """A flat table of neutral 4.0s has to say why, as `fpl fdr` and `fpl preview` do."""

    def test_table_mode_warns_on_stderr(self, runner, tmp_path):
        """No usable ratings: every FDR is 4.0 and the note explains it."""
        result = _run_fixtures(runner, tmp_path, ratings={})

        assert result.exit_code == 0
        assert "No team ratings available" in result.stderr

    def test_json_mode_carries_the_warning(self, runner, tmp_path):
        """JSON consumers get the same note as a coded `metadata.warnings` entry."""
        result = _run_fixtures(runner, tmp_path, "--format", "json", ratings={})

        warnings = json.loads(result.output)["metadata"]["warnings"]
        assert [w["code"] for w in warnings] == ["team_ratings_unusable"]
        assert "No team ratings available" in warnings[0]["message"]

    def test_warnings_key_present_when_ratings_are_healthy(self, runner, tmp_path):
        """Always present, empty or not, so a consumer indexes it directly."""
        payload = json.loads(_run_fixtures(runner, tmp_path, "--format", "json").output)

        assert payload["metadata"]["warnings"] == []


class TestFixturesJsonFormat:
    """Test --format json output for the fixtures command."""

    def test_json_happy_path(self, runner, tmp_path):
        """--format json produces valid JSON with correct envelope."""
        teams, fixtures = _standard_teams_and_fixtures()
        client = _mock_fpl_client(teams, fixtures)
        mock_svc = _ratings_service(tmp_path)

        with (
            patch("fpl_cli.cli.fixtures.is_custom_analysis_enabled", return_value=True),
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

    def test_json_fixture_dict_keys(self, runner, tmp_path):
        """Fixture dict contains expected keys."""
        teams, fixtures = _standard_teams_and_fixtures()
        client = _mock_fpl_client(teams, fixtures)
        mock_svc = _ratings_service(tmp_path)

        with (
            patch("fpl_cli.cli.fixtures.is_custom_analysis_enabled", return_value=True),
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

    def test_json_finished_fixture_has_scores(self, runner, tmp_path):
        """Finished fixture includes home_score and away_score."""
        teams, fixtures = _standard_teams_and_fixtures(finished=True)
        client = _mock_fpl_client(teams, fixtures)
        mock_svc = _ratings_service(tmp_path)

        with (
            patch("fpl_cli.cli.fixtures.is_custom_analysis_enabled", return_value=True),
            patch("fpl_cli.api.fpl.FPLClient", return_value=client),
            patch("fpl_cli.services.team_ratings.TeamRatingsService", return_value=mock_svc),
        ):
            result = runner.invoke(main, ["fixtures", "-g", "32", "--format", "json"])

        fixture_dict = json.loads(result.output)["data"][0]
        assert fixture_dict["finished"] is True
        assert fixture_dict["home_score"] == 2
        assert fixture_dict["away_score"] == 1

    def test_table_format_unchanged(self, runner, tmp_path):
        """Default --format table output contains no JSON."""
        teams, fixtures = _standard_teams_and_fixtures()
        client = _mock_fpl_client(teams, fixtures)
        mock_svc = _ratings_service(tmp_path)

        with (
            patch("fpl_cli.cli.fixtures.is_custom_analysis_enabled", return_value=True),
            patch("fpl_cli.api.fpl.FPLClient", return_value=client),
            patch("fpl_cli.services.team_ratings.TeamRatingsService", return_value=mock_svc),
        ):
            result = runner.invoke(main, ["fixtures", "-g", "32"])

        assert result.exit_code == 0
        # Should contain table content, not JSON
        assert "CHE" in result.output
        assert '"command"' not in result.output

    def test_json_error_on_api_failure(self, runner, tmp_path):
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
            patch("fpl_cli.cli.fixtures.is_custom_analysis_enabled", return_value=True),
            patch("fpl_cli.api.fpl.FPLClient", return_value=client),
            patch("fpl_cli.services.team_ratings.TeamRatingsService", return_value=mock_svc),
        ):
            result = runner.invoke(main, ["fixtures", "-g", "32", "--format", "json"])

        assert result.exit_code == 1
        # #141: the error envelope rides stdout, same as the success envelope.
        error_payload = json.loads(result.stdout)
        assert error_payload["command"] == "fixtures"
        assert "API down" in error_payload["error"]
        assert "{" not in result.stderr


class TestFixturesCustomAnalysisGate:
    """With custom analysis off, the canonical FPL API difficulty, as in `fpl fdr`."""

    def test_falls_back_to_api_difficulty(self, runner, tmp_path):
        """The 1-5 figures the API ships, not the 1-7 team-ratings FDR."""
        result = _run_fixtures(runner, tmp_path, "--format", "json", custom_analysis=False)

        assert result.exit_code == 0
        payload = json.loads(result.output)
        fixture_dict = payload["data"][0]
        # make_fixture built this one with home_difficulty=2, away_difficulty=4
        assert fixture_dict["home_fdr"] == 2
        assert fixture_dict["away_fdr"] == 4
        assert payload["metadata"]["custom_analysis"] is False
        assert payload["metadata"]["fdr_scale"] == "fpl_api_1_5"

    def test_metadata_names_the_scale_when_custom_is_on(self, runner, tmp_path):
        """A consumer reads the scale off the envelope, never off the numbers."""
        payload = json.loads(_run_fixtures(runner, tmp_path, "--format", "json").output)

        assert payload["metadata"]["custom_analysis"] is True
        assert payload["metadata"]["fdr_scale"] == "team_ratings_1_7"

    def test_no_ratings_work_when_opted_out(self, runner, tmp_path):
        """An opted-out user should not be refreshing Bayesian ratings at all."""
        teams, fixtures = _standard_teams_and_fixtures()
        client = _mock_fpl_client(teams, fixtures)
        service = _ratings_service(tmp_path)

        with (
            patch("fpl_cli.cli.fixtures.is_custom_analysis_enabled", return_value=False),
            patch("fpl_cli.api.fpl.FPLClient", return_value=client),
            patch("fpl_cli.services.team_ratings.TeamRatingsService", return_value=service),
        ):
            result = runner.invoke(main, ["fixtures", "-g", "32"])

        assert result.exit_code == 0
        service.ensure_fresh.assert_not_called()

    def test_table_names_the_api_scale(self, runner, tmp_path):
        """The panel and footer say which scale the column is on."""
        result = _run_fixtures(runner, tmp_path, custom_analysis=False)

        assert "FPL API Ratings" in result.output
        assert "1 (easiest) - 5 (hardest)" in result.output

    def test_mode_flag_reports_that_it_does_not_apply(self, runner, tmp_path):
        """`-m` against the API scale would otherwise be silently inert."""
        result = _run_fixtures(runner, tmp_path, "-m", "opponent", custom_analysis=False)

        assert result.exit_code == 0
        assert "--mode applies to the team-ratings FDR" in result.stderr

    def test_no_mode_note_when_the_flag_was_not_typed(self, runner, tmp_path):
        """The default never triggers the note - only an explicit flag does."""
        result = _run_fixtures(runner, tmp_path, custom_analysis=False)

        assert "--mode applies" not in result.stderr

    def test_no_ratings_warning_on_the_api_path(self, runner, tmp_path):
        """The ratings note is about ratings; the API path does not use them."""
        result = _run_fixtures(runner, tmp_path, "--format", "json", ratings={}, custom_analysis=False)

        assert json.loads(result.output)["metadata"]["warnings"] == []

    def test_mode_is_null_in_metadata_when_it_does_not_apply(self, runner, tmp_path):
        """`fdr_mode` describes a figure that is not there, so it is null."""
        payload = json.loads(
            _run_fixtures(runner, tmp_path, "--format", "json", custom_analysis=False).output
        )

        assert payload["metadata"]["fdr_mode"] is None
