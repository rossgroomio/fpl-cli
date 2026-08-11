"""Tests for pre-season API payloads, where strength ratings are unpublished.

Before a season starts the FPL API returns ``"strength": null`` and zeroed
attack/defence axes for all 20 teams. The null used to fail Team validation
outright; the zeros validate cleanly and read as genuine ratings, so both the
crash and the silent-uniformity path are covered here.
"""

from unittest.mock import AsyncMock, patch

import pytest
import yaml

from fpl_cli.api.fpl import FPLClient
from fpl_cli.models.team import Team
from fpl_cli.services.team_ratings import (
    PRESEASON_SOURCE,
    TeamRating,
    TeamRatingsCalculator,
    TeamRatingsService,
)


def preseason_team(id: int, name: str, short_name: str, code: int) -> dict:
    """A team as bootstrap-static publishes it before the season starts.

    Mirrors the live pre-season shape: strength null, attack/defence zeroed,
    overall home/away genuinely populated.
    """
    return {
        "id": id,
        "name": name,
        "short_name": short_name,
        "code": code,
        "strength": None,
        "strength_overall_home": 4,
        "strength_overall_away": 5,
        "strength_attack_home": 0,
        "strength_attack_away": 0,
        "strength_defence_home": 0,
        "strength_defence_away": 0,
        "form": None,
        "played": 0,
        "win": 0,
        "draw": 0,
        "loss": 0,
        "points": 0,
    }


@pytest.fixture
def preseason_bootstrap():
    """bootstrap-static as served pre-season: full squad data, no strength ratings."""
    return {
        "elements": [],
        "teams": [
            preseason_team(1, "Arsenal", "ARS", 3),
            preseason_team(7, "Coventry", "COV", 45),
            preseason_team(13, "Manchester City", "MCI", 43),
            preseason_team(14, "Liverpool", "LIV", 14),
        ],
        "events": [
            {"id": 1, "is_current": False, "is_next": True, "deadline_time": "2026-08-14T17:30:00Z"},
        ],
    }


class TestPreseasonTeamValidation:
    """Team must validate against the pre-season payload (issue #43)."""

    async def test_get_teams_accepts_null_strength(self, preseason_bootstrap):
        """get_teams() no longer raises on the pre-season payload."""
        client = FPLClient()
        with patch.object(client, "_get", new_callable=AsyncMock) as mock_get:
            mock_get.return_value = preseason_bootstrap

            teams = await client.get_teams()

        assert len(teams) == 4
        assert {t.short_name for t in teams} == {"ARS", "COV", "MCI", "LIV"}

    async def test_null_strength_stays_none(self, preseason_bootstrap):
        """A null strength is preserved as None, not coerced to a number."""
        client = FPLClient()
        with patch.object(client, "_get", new_callable=AsyncMock) as mock_get:
            mock_get.return_value = preseason_bootstrap

            teams = await client.get_teams()

        assert all(t.strength is None for t in teams)

    def test_strength_defaults_to_none_when_absent(self):
        """A payload omitting strength entirely still validates."""
        team = Team(id=1, name="Arsenal", short_name="ARS", code=3)

        assert team.strength is None
        assert team.strength_attack_home is None


class TestHasStrengthData:
    """The availability guard for zeroed ratings (issue #44)."""

    def test_false_when_attack_defence_zeroed(self):
        """Zeros validate cleanly, so the guard is the only signal they are absent."""
        team = Team.model_validate(preseason_team(1, "Arsenal", "ARS", 3))

        assert team.has_strength_data is False

    def test_false_when_fields_absent(self):
        team = Team(id=1, name="Arsenal", short_name="ARS", code=3)

        assert team.has_strength_data is False

    def test_true_once_ratings_are_published(self):
        payload = preseason_team(1, "Arsenal", "ARS", 3) | {
            "strength": 4,
            "strength_attack_home": 1200,
            "strength_attack_away": 1150,
            "strength_defence_home": 1180,
            "strength_defence_away": 1130,
        }
        team = Team.model_validate(payload)

        assert team.has_strength_data is True

    def test_true_when_only_one_axis_populated(self):
        """Any populated axis means the API has started publishing."""
        payload = preseason_team(1, "Arsenal", "ARS", 3) | {"strength_defence_away": 1130}
        team = Team.model_validate(payload)

        assert team.has_strength_data is True

    async def test_whole_league_reads_as_unpublished(self, preseason_bootstrap):
        client = FPLClient()
        with patch.object(client, "_get", new_callable=AsyncMock) as mock_get:
            mock_get.return_value = preseason_bootstrap

            teams = await client.get_teams()

        assert not any(t.has_strength_data for t in teams)


class TestPreseasonRatings:
    """Pre-season pFDR must not silently fall back to one value for every team."""

    @pytest.fixture(autouse=True)
    def reset_session_guard(self):
        TeamRatingsService._refreshed_this_session = False
        yield
        TeamRatingsService._refreshed_this_session = False

    @pytest.fixture
    def last_season_config(self, tmp_path):
        """Last season's ratings: still carries a relegated side, misses the promoted one."""
        path = tmp_path / "team_ratings.yaml"
        with open(path, "w", encoding="utf-8") as f:
            yaml.dump(
                {
                    "metadata": {
                        "last_updated": "2026-03-25",
                        "source": "auto_calculated",
                        "staleness_threshold_days": 30,
                        "based_on_gws": [20, 31],
                        "calculation_method": "recent_form",
                    },
                    "ratings": {
                        "ARS": {"atk_home": 2, "atk_away": 1, "def_home": 1, "def_away": 1},
                        "IPS": {"atk_home": 6, "atk_away": 7, "def_home": 6, "def_away": 7},
                    },
                },
                f,
            )
        return path

    @pytest.fixture
    def preseason_client(self):
        """A client reporting GW1 as next, i.e. nothing has been played."""
        client = AsyncMock()
        client.get_next_gameweek = AsyncMock(return_value={"id": 1})
        client.get_fixtures = AsyncMock(return_value=[])
        client.get_teams = AsyncMock(return_value=[])
        return client

    async def test_seeds_from_prior_pre_season(self, last_season_config, preseason_client):
        """With no completed GW, ratings are rebuilt from the previous-season prior."""
        service = TeamRatingsService(config_path=last_season_config)
        prior = {
            "ARS": TeamRating(atk_home=2, atk_away=1, def_home=1, def_away=1),
            "COV": TeamRating(atk_home=5, atk_away=6, def_home=5, def_away=6),
        }

        with patch(
            "fpl_cli.services.team_ratings_prior.generate_prior",
            new_callable=AsyncMock,
        ) as mock_prior:
            mock_prior.return_value = prior
            await service.ensure_fresh(preseason_client)

        mock_prior.assert_awaited_once()
        assert service.metadata.source == PRESEASON_SOURCE
        assert service.is_preseason_estimate is True
        # The promoted side is now rated; the relegated one is gone.
        assert service.get_rating("COV") is not None
        assert service.get_rating("IPS") is None

    async def test_does_not_recalculate_from_empty_fixtures(
        self, last_season_config, preseason_client
    ):
        """Pre-season skips the current-season calculation, which has nothing to rate."""
        with (
            patch(
                "fpl_cli.services.team_ratings_prior.generate_prior",
                new_callable=AsyncMock,
            ) as mock_prior,
            patch.object(
                TeamRatingsCalculator, "calculate_from_fixtures", new_callable=AsyncMock
            ) as mock_calc,
        ):
            mock_prior.return_value = {"ARS": TeamRating(2, 1, 1, 1)}
            await TeamRatingsService(config_path=last_season_config).ensure_fresh(preseason_client)

        mock_calc.assert_not_called()

    async def test_pre_season_pfdr_differentiates_teams(
        self, last_season_config, preseason_client
    ):
        """The seeded prior must not hand every team the same fixture difficulty."""
        service = TeamRatingsService(config_path=last_season_config)
        prior = {
            "ARS": TeamRating(atk_home=1, atk_away=2, def_home=1, def_away=2),
            "MCI": TeamRating(atk_home=2, atk_away=1, def_home=2, def_away=1),
            "COV": TeamRating(atk_home=6, atk_away=7, def_home=6, def_away=7),
            "LIV": TeamRating(atk_home=3, atk_away=3, def_home=4, def_away=3),
        }

        with patch(
            "fpl_cli.services.team_ratings_prior.generate_prior",
            new_callable=AsyncMock,
        ) as mock_prior:
            mock_prior.return_value = prior
            await service.ensure_fresh(preseason_client)

        fdrs = {
            team: service.get_positional_fdr("MID", team, "ARS", "home")
            for team in ("MCI", "COV", "LIV")
        }

        assert len(set(fdrs.values())) > 1, f"pFDR identical for every team: {fdrs}"
        assert service.is_uniform is False

    async def test_keeps_last_season_ratings_when_prior_unavailable(
        self, last_season_config, preseason_client
    ):
        """An empty prior leaves existing ratings in place rather than wiping them."""
        service = TeamRatingsService(config_path=last_season_config)

        with patch(
            "fpl_cli.services.team_ratings_prior.generate_prior",
            new_callable=AsyncMock,
        ) as mock_prior:
            mock_prior.return_value = {}
            await service.ensure_fresh(preseason_client)

        assert service.get_rating("ARS") is not None
        assert service.is_preseason_estimate is False


class TestRatingsQualityWarnings:
    """Degenerate rating sets must announce themselves rather than rank silently."""

    def _service(self, tmp_path, ratings: dict, source: str = "auto_calculated"):
        path = tmp_path / "team_ratings.yaml"
        with open(path, "w", encoding="utf-8") as f:
            yaml.dump(
                {
                    "metadata": {
                        "last_updated": "2026-08-11",
                        "source": source,
                        "staleness_threshold_days": 30,
                        "based_on_gws": None,
                        "calculation_method": None,
                    },
                    "ratings": ratings,
                },
                f,
            )
        return TeamRatingsService(config_path=path)

    def test_warns_when_no_ratings_exist(self, tmp_path):
        """A fresh install has no ratings, so every fixture scores a neutral 4.0."""
        service = TeamRatingsService(config_path=tmp_path / "missing.yaml")

        assert service.has_ratings is False
        assert "neutral 4.0" in service.get_staleness_warning()

    def test_warns_when_every_team_rated_identically(self, tmp_path):
        """Twenty identical ratings produce difficulty that separates nothing."""
        identical = {"atk_home": 4, "atk_away": 4, "def_home": 4, "def_away": 4}
        service = self._service(
            tmp_path, {t: dict(identical) for t in ("ARS", "MCI", "LIV", "COV")}
        )

        assert service.is_uniform is True
        assert "do not separate" in service.get_staleness_warning()

    def test_warns_that_pre_season_ratings_are_estimates(self, tmp_path):
        service = self._service(
            tmp_path,
            {
                "ARS": {"atk_home": 1, "atk_away": 2, "def_home": 1, "def_away": 2},
                "COV": {"atk_home": 6, "atk_away": 7, "def_home": 6, "def_away": 7},
            },
            source=PRESEASON_SOURCE,
        )

        assert "Pre-season" in service.get_staleness_warning()

    def test_no_warning_for_fresh_differentiated_ratings(self, tmp_path):
        service = self._service(
            tmp_path,
            {
                "ARS": {"atk_home": 1, "atk_away": 2, "def_home": 1, "def_away": 2},
                "COV": {"atk_home": 6, "atk_away": 7, "def_home": 6, "def_away": 7},
            },
        )

        assert service.is_uniform is False
        assert service.get_staleness_warning() is None

    def test_single_team_is_not_treated_as_uniform(self, tmp_path):
        """One team cannot be compared against another, so it is not a degenerate set."""
        service = self._service(
            tmp_path, {"ARS": {"atk_home": 1, "atk_away": 2, "def_home": 1, "def_away": 2}}
        )

        assert service.is_uniform is False
