"""Tests for season and team-set awareness of per-team config files.

Two failures a date check cannot catch, both silent before this: ratings
carried across a season boundary keep rating the relegated clubs and hand the
promoted ones nothing, and a manager map refreshed in early August still
describes last season's twenty clubs.
"""

from datetime import datetime, timedelta
from unittest.mock import AsyncMock, patch

import pytest
import yaml

from fpl_cli.season import get_season_year, season_label
from fpl_cli.services.team_ratings import TeamRating, TeamRatingsCalculator, TeamRatingsService
from fpl_cli.utils.teams import describe_team_set_mismatch
from tests.conftest import make_team

PREVIOUS_SEASON = season_label(get_season_year() - 1)

CURRENT_TEAMS = ("ARS", "COV", "HUL", "IPS")
LAST_SEASON_RATINGS = {
    "ARS": {"atk_home": 1, "atk_away": 2, "def_home": 1, "def_away": 2},
    "BUR": {"atk_home": 6, "atk_away": 7, "def_home": 6, "def_away": 7},
    "WHU": {"atk_home": 5, "atk_away": 6, "def_home": 5, "def_away": 5},
    "WOL": {"atk_home": 6, "atk_away": 6, "def_home": 6, "def_away": 7},
}


def write_ratings(path, ratings: dict, **metadata) -> None:
    """Write a ratings file, defaulting to a fresh current-season stamp."""
    meta = {
        "season": season_label(),
        "last_updated": datetime.now().strftime("%Y-%m-%d"),
        "source": "auto_calculated",
        "staleness_threshold_days": 30,
        "based_on_gws": None,
        "calculation_method": "recent_form",
    }
    meta.update(metadata)
    with open(path, "w", encoding="utf-8") as f:
        yaml.dump({"metadata": meta, "ratings": ratings}, f)


class TestDescribeTeamSetMismatch:
    """The shared diff that names the specific clubs on each side."""

    def test_names_both_halves_of_the_mismatch(self):
        message = describe_team_set_mismatch(
            "team_ratings.yaml",
            ["ARS", "BUR", "WHU", "WOL"],
            ["ARS", "COV", "HUL", "IPS"],
            verb="rates",
        )

        assert message == (
            "team_ratings.yaml is missing COV, HUL, IPS and still rates BUR, WHU, WOL"
        )

    def test_missing_only(self):
        message = describe_team_set_mismatch(
            "team_managers.yaml", ["ARS"], ["ARS", "COV"], verb="lists"
        )

        assert message == "team_managers.yaml is missing COV"

    def test_extra_only(self):
        message = describe_team_set_mismatch(
            "team_managers.yaml", ["ARS", "BUR"], ["ARS"], verb="lists"
        )

        assert message == "team_managers.yaml still lists BUR"

    def test_matching_sets_are_silent(self):
        assert (
            describe_team_set_mismatch("f.yaml", ["ARS", "COV"], ["COV", "ARS"], verb="rates")
            is None
        )

    def test_case_is_normalised(self):
        assert describe_team_set_mismatch("f.yaml", ["ars"], ["ARS"], verb="rates") is None

    def test_empty_live_list_is_silent(self):
        """An API that returned nothing is not evidence the file is wrong."""
        assert describe_team_set_mismatch("f.yaml", ["ARS"], [], verb="rates") is None


class TestRatingsSeasonInvalidation:
    """A ratings file from a previous season must not serve its numbers."""

    def test_previous_season_ratings_are_not_served(self, tmp_path):
        path = tmp_path / "team_ratings.yaml"
        write_ratings(path, LAST_SEASON_RATINGS, season=PREVIOUS_SEASON)

        service = TeamRatingsService(config_path=path)

        assert service.get_rating("ARS") is None
        assert service.get_all_ratings() == {}
        assert service.has_ratings is False

    def test_previous_season_ratings_warn_by_season_not_by_date(self, tmp_path):
        path = tmp_path / "team_ratings.yaml"
        write_ratings(path, LAST_SEASON_RATINGS, season=PREVIOUS_SEASON)

        warning = TeamRatingsService(config_path=path).get_staleness_warning()

        assert PREVIOUS_SEASON in warning
        assert "different league" in warning
        assert "days old" not in warning

    def test_season_derived_from_last_updated_when_unstamped(self, tmp_path):
        """Files written before the season stamp existed still invalidate."""
        path = tmp_path / "team_ratings.yaml"
        meta = {
            "last_updated": f"{get_season_year() - 1}-09-01",
            "source": "auto_calculated",
            "staleness_threshold_days": 30,
            "based_on_gws": [24, 35],
            "calculation_method": "recent_form",
        }
        with open(path, "w", encoding="utf-8") as f:
            yaml.dump({"metadata": meta, "ratings": LAST_SEASON_RATINGS}, f)

        service = TeamRatingsService(config_path=path)

        assert service.has_ratings is False
        assert PREVIOUS_SEASON in service.get_staleness_warning()

    def test_current_season_ratings_load_normally(self, tmp_path):
        path = tmp_path / "team_ratings.yaml"
        write_ratings(path, LAST_SEASON_RATINGS)

        service = TeamRatingsService(config_path=path)

        assert service.get_rating("ARS") is not None
        assert service.metadata.season == season_label()

    def test_stamp_wins_over_a_date_from_the_previous_season(self, tmp_path):
        """An explicit stamp is authoritative; only unstamped files fall back."""
        path = tmp_path / "team_ratings.yaml"
        write_ratings(
            path,
            LAST_SEASON_RATINGS,
            last_updated=(datetime.now() - timedelta(days=60)).strftime("%Y-%m-%d"),
        )

        service = TeamRatingsService(config_path=path)

        assert service.get_rating("ARS") is not None
        assert "days old" in service.get_staleness_warning()

    def test_undated_unstamped_file_is_not_treated_as_stale(self, tmp_path):
        """No season and no date means "cannot tell", not "wrong season"."""
        path = tmp_path / "team_ratings.yaml"
        with open(path, "w", encoding="utf-8") as f:
            yaml.dump({"metadata": {}, "ratings": LAST_SEASON_RATINGS}, f)

        service = TeamRatingsService(config_path=path)

        assert service.get_rating("ARS") is not None
        assert "no last_updated date" in service.get_staleness_warning()

    def test_save_stamps_the_current_season(self, tmp_path):
        path = tmp_path / "team_ratings.yaml"
        service = TeamRatingsService(config_path=path)

        service.save_ratings(
            {"ARS": TeamRating(1, 2, 1, 2)}, source="calculated", based_on_gws=(1, 3)
        )

        written = yaml.safe_load(path.read_text(encoding="utf-8"))
        assert written["metadata"]["season"] == season_label()
        assert TeamRatingsService(config_path=path).get_rating("ARS") is not None

    def test_save_clears_a_previous_season_warning(self, tmp_path):
        path = tmp_path / "team_ratings.yaml"
        write_ratings(path, LAST_SEASON_RATINGS, season=PREVIOUS_SEASON)
        service = TeamRatingsService(config_path=path)
        assert service.has_ratings is False

        service.save_ratings({"ARS": TeamRating(1, 2, 1, 2)}, source="calculated")

        assert service.get_staleness_warning() is None


class TestRatingsTeamSetCheck:
    """Promotion and relegation is the mismatch a date can never catch."""

    def test_names_missing_and_extra_clubs(self, tmp_path):
        path = tmp_path / "team_ratings.yaml"
        write_ratings(path, LAST_SEASON_RATINGS)
        service = TeamRatingsService(config_path=path)

        warning = service.check_team_set(CURRENT_TEAMS)

        assert "is missing COV, HUL, IPS" in warning
        assert "still rates BUR, WHU, WOL" in warning
        assert service.get_staleness_warning() == warning

    def test_matching_team_set_is_silent(self, tmp_path):
        path = tmp_path / "team_ratings.yaml"
        write_ratings(path, {t: dict(LAST_SEASON_RATINGS["ARS"]) for t in CURRENT_TEAMS})
        service = TeamRatingsService(config_path=path)

        assert service.check_team_set(CURRENT_TEAMS) is None

    def test_empty_ratings_defer_to_the_no_ratings_warning(self, tmp_path):
        service = TeamRatingsService(config_path=tmp_path / "missing.yaml")

        assert service.check_team_set(CURRENT_TEAMS) is None
        assert "No team ratings available" in service.get_staleness_warning()

    def test_recheck_clears_a_resolved_mismatch(self, tmp_path):
        path = tmp_path / "team_ratings.yaml"
        write_ratings(path, LAST_SEASON_RATINGS)
        service = TeamRatingsService(config_path=path)
        assert service.check_team_set(CURRENT_TEAMS) is not None

        assert service.check_team_set(LAST_SEASON_RATINGS.keys()) is None


class TestEnsureFreshSeasonRollover:
    """ensure_fresh must not trust a previous season's gameweek range."""

    @pytest.fixture(autouse=True)
    def reset_session_guard(self):
        TeamRatingsService._refreshed_this_session = False
        yield
        TeamRatingsService._refreshed_this_session = False

    @pytest.fixture
    def client(self):
        client = AsyncMock()
        client.get_next_gameweek = AsyncMock(return_value={"id": 4})
        client.get_fixtures = AsyncMock(return_value=[])
        client.get_teams = AsyncMock(
            return_value=[make_team(id=i, short_name=t) for i, t in enumerate(CURRENT_TEAMS, 1)]
        )
        return client

    async def test_previous_season_gw_range_does_not_suppress_recalculation(
        self, tmp_path, client
    ):
        """GW3 of the new season is not covered by GW35 of the old one."""
        path = tmp_path / "team_ratings.yaml"
        write_ratings(
            path, LAST_SEASON_RATINGS, season=PREVIOUS_SEASON, based_on_gws=[24, 35]
        )
        service = TeamRatingsService(config_path=path)

        with (
            patch.object(
                TeamRatingsCalculator, "calculate_from_fixtures", new_callable=AsyncMock
            ) as mock_calc,
            patch(
                "fpl_cli.services.team_ratings_prior.generate_prior", new_callable=AsyncMock
            ) as mock_prior,
        ):
            mock_calc.return_value = ({"ARS": TeamRating(2, 3, 2, 3)}, {})
            mock_prior.return_value = {}
            await service.ensure_fresh(client)

        mock_calc.assert_awaited_once()
        assert service.metadata.based_on_gws == (1, 3)
        assert service.get_rating("ARS") is not None

    async def test_ensure_fresh_records_the_team_set_mismatch(self, tmp_path, client):
        path = tmp_path / "team_ratings.yaml"
        write_ratings(path, LAST_SEASON_RATINGS, based_on_gws=[1, 3])

        service = TeamRatingsService(config_path=path)
        await service.ensure_fresh(client)

        assert "is missing COV, HUL, IPS" in service.get_staleness_warning()

    async def test_team_set_check_survives_a_failed_refresh(self, tmp_path, client):
        """A dead refresh must not swallow the mismatch the file still has."""
        path = tmp_path / "team_ratings.yaml"
        write_ratings(path, LAST_SEASON_RATINGS, based_on_gws=[1, 3])
        client.get_next_gameweek.side_effect = Exception("API down")

        service = TeamRatingsService(config_path=path)
        await service.ensure_fresh(client)

        assert "still rates BUR, WHU, WOL" in service.get_staleness_warning()

    async def test_unreachable_team_list_does_not_break_the_command(self, tmp_path, client):
        path = tmp_path / "team_ratings.yaml"
        write_ratings(path, LAST_SEASON_RATINGS, based_on_gws=[1, 3])
        client.get_teams.side_effect = Exception("API down")

        service = TeamRatingsService(config_path=path)
        await service.ensure_fresh(client)

        assert service.get_rating("ARS") is not None


class TestManagerMapTeamSet:
    """The manager map is handed to the recap prompt as current fact."""

    def _teams(self, *short_names: str) -> dict[int, object]:
        return {
            i: make_team(id=i, short_name=name)
            for i, name in enumerate(short_names, start=1)
        }

    def test_warns_naming_relegated_and_promoted_clubs(self, capsys):
        from fpl_cli.cli._review_summarisation import _warn_manager_team_drift

        _warn_manager_team_drift(
            {"ARS": "Arteta", "BUR": "Parker", "WHU": "Nuno"},
            self._teams("ARS", "COV", "HUL"),
        )

        err = capsys.readouterr().err
        assert "team_managers.yaml" in err
        assert "is missing COV, HUL" in err
        assert "still lists BUR, WHU" in err

    def test_silent_when_the_map_matches_the_league(self, capsys):
        from fpl_cli.cli._review_summarisation import _warn_manager_team_drift

        _warn_manager_team_drift({"ARS": "Arteta", "COV": "Lampard"}, self._teams("ARS", "COV"))

        assert capsys.readouterr().err == ""

    def test_silent_when_the_team_list_is_unavailable(self, capsys):
        from fpl_cli.cli._review_summarisation import _warn_manager_team_drift

        _warn_manager_team_drift({"ARS": "Arteta"}, None)

        assert capsys.readouterr().err == ""

    def test_shipped_manager_map_covers_the_current_league(self):
        """The committed map is the fallback for every user, so it must be complete."""
        from fpl_cli.cli._review_summarisation import _load_team_managers

        managers = _load_team_managers()

        assert len(managers) == 20
        assert all(len(code) == 3 and code.isupper() for code in managers)
