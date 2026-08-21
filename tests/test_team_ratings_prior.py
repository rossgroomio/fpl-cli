"""Tests for team ratings prior system."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from fpl_cli.services.team_ratings import TeamRating
from fpl_cli.services.team_ratings_prior import (
    BLENDING_CUTOFF_GW,
    REGRESSION_CONSTANT,
    blend_with_prior,
    generate_prior,
)


class TestBlendWithPrior:
    """Tests for Bayesian blending."""

    @pytest.fixture
    def prior(self):
        return {
            "ARS": TeamRating(atk_home=1, atk_away=2, def_home=1, def_away=2),
            "MCI": TeamRating(atk_home=2, atk_away=3, def_home=2, def_away=3),
        }

    @pytest.fixture
    def current(self):
        return {
            "ARS": TeamRating(atk_home=3, atk_away=4, def_home=3, def_away=4),
            "MCI": TeamRating(atk_home=4, atk_away=5, def_home=4, def_away=5),
        }

    def test_cutoff_returns_current(self, prior, current):
        """At or above cutoff GW, current ratings returned unmodified."""
        result = blend_with_prior(prior, current, BLENDING_CUTOFF_GW)

        assert result["ARS"].atk_home == 3
        assert result["MCI"].atk_home == 4

    def test_gw1_heavily_weighted_prior(self, prior, current):
        """At GW1, prior dominates (86% weight)."""
        result = blend_with_prior(prior, current, 1)

        # ARS atk_home: round(6/7 * 1 + 1/7 * 3) = round(1.29) = 1
        assert result["ARS"].atk_home == 1

    def test_gw5_balanced(self, prior, current):
        """At GW5, weights are 45% current / 55% prior."""
        result = blend_with_prior(prior, current, 5)

        # ARS atk_home: round(6/11 * 1 + 5/11 * 3) = round(1.91) = 2
        assert result["ARS"].atk_home == 2

    def test_regression_constant_is_6(self):
        """Verify the tuned constant."""
        assert REGRESSION_CONSTANT == 6

    def test_cutoff_is_12(self):
        """Verify the cutoff GW."""
        assert BLENDING_CUTOFF_GW == 12

    def test_missing_team_in_current_uses_prior(self, prior):
        """Team in prior but not current gets prior value."""
        current = {"ARS": TeamRating(3, 4, 3, 4)}  # MCI missing

        result = blend_with_prior(prior, current, 5)

        assert "MCI" in result
        assert result["MCI"].atk_home == 2  # Prior value unchanged (blended with itself)

    def test_missing_team_in_prior_uses_default(self, current):
        """Team in current but not prior gets blended with default 4."""
        prior = {"ARS": TeamRating(1, 2, 1, 2)}  # MCI missing

        result = blend_with_prior(prior, current, 5)

        # MCI atk_home: round(6/11 * 4 + 5/11 * 4) = 4
        assert result["MCI"].atk_home == 4


class TestGeneratePrior:
    """Tests for prior generation with fallback chain."""

    @pytest.fixture
    def mock_client(self):
        from tests.conftest import make_team

        client = AsyncMock()
        client.get_teams = AsyncMock(return_value=[
            make_team(id=1, name="Arsenal", short_name="ARS"),
            make_team(id=2, name="Man City", short_name="MCI"),
        ])
        return client

    async def test_understat_fallback_to_football_data(self, mock_client, tmp_path):
        """When Understat fails, falls back to football-data.org."""
        mock_fd = AsyncMock()
        mock_fd.is_configured = True
        mock_fd.get_matches = AsyncMock(return_value=[
            {"home_team_tla": "ARS", "away_team_tla": "MCI", "home_score": 2, "away_score": 1, "matchday": 1},
            {"home_team_tla": "MCI", "away_team_tla": "ARS", "home_score": 3, "away_score": 0, "matchday": 2},
        ])
        mock_fd.__aenter__ = AsyncMock(return_value=mock_fd)
        mock_fd.__aexit__ = AsyncMock(return_value=False)

        with (
            patch("fpl_cli.services.team_ratings_prior.prior_config_path", return_value=tmp_path / "prior.yaml"),
            patch("fpl_cli.services.team_ratings_prior._prior_from_understat", return_value=None),
            patch("fpl_cli.api.football_data.FootballDataClient", return_value=mock_fd),
        ):
            result = await generate_prior(mock_client)

        assert "ARS" in result
        assert "MCI" in result

    async def test_ultimate_fallback_to_default_4(self, mock_client, tmp_path):
        """When all sources fail, all teams get default rating 4."""
        with (
            patch("fpl_cli.services.team_ratings_prior.prior_config_path", return_value=tmp_path / "prior.yaml"),
            patch("fpl_cli.services.team_ratings_prior._prior_from_understat", return_value=None),
            patch("fpl_cli.services.team_ratings_prior._prior_from_football_data", return_value=None),
            patch("fpl_cli.services.team_ratings_prior._championship_performances", new_callable=AsyncMock, return_value=None),
        ):
            result = await generate_prior(mock_client)

        assert result["ARS"].atk_home == 4
        assert result["MCI"].def_away == 4

    async def test_cache_reused_when_teams_match(self, mock_client, tmp_path):
        """Cached prior is returned if team list matches."""
        import yaml

        from fpl_cli.services.team_ratings_prior import PRIOR_CACHE_VERSION

        cache_path = tmp_path / "prior.yaml"
        cached = {
            "metadata": {
                "version": PRIOR_CACHE_VERSION,
                "source": "prior_understat_xg",
                "teams": ["ARS", "MCI"],
            },
            "ratings": {
                "ARS": {"atk_home": 2, "atk_away": 2, "def_home": 2, "def_away": 2},
                "MCI": {"atk_home": 3, "atk_away": 3, "def_home": 3, "def_away": 3},
            },
        }
        with open(cache_path, "w", encoding="utf-8") as f:
            yaml.dump(cached, f)

        with patch("fpl_cli.services.team_ratings_prior.prior_config_path", return_value=cache_path):
            result = await generate_prior(mock_client)

        assert result["ARS"].atk_home == 2  # From cache

    async def test_cache_from_older_version_is_discarded(self, mock_client, tmp_path):
        """A cache written by an older methodology is regenerated, not served."""
        import yaml

        cache_path = tmp_path / "prior.yaml"
        cached = {
            # Pre-versioning cache: same team set, no version stamp.
            "metadata": {"source": "prior_understat_xg", "teams": ["ARS", "MCI"]},
            "ratings": {
                "ARS": {"atk_home": 1, "atk_away": 1, "def_home": 1, "def_away": 1},
                "MCI": {"atk_home": 1, "atk_away": 1, "def_home": 1, "def_away": 1},
            },
        }
        with open(cache_path, "w", encoding="utf-8") as f:
            yaml.dump(cached, f)

        with (
            patch("fpl_cli.services.team_ratings_prior.prior_config_path", return_value=cache_path),
            patch("fpl_cli.services.team_ratings_prior._prior_from_understat", return_value=None),
            patch("fpl_cli.services.team_ratings_prior._prior_from_football_data", return_value=None),
        ):
            result = await generate_prior(mock_client)

        # Regenerated from the fallback chain, not the stale cached 1s.
        assert result["ARS"].atk_home == 4


class TestFootballDataGetMatches:
    """Tests for FootballDataClient.get_matches()."""

    async def test_get_matches_returns_parsed(self):
        """Matches are parsed into standardised dicts."""
        from unittest.mock import MagicMock

        from fpl_cli.api.football_data import FootballDataClient

        mock_response_data = {
            "matches": [
                {
                    "homeTeam": {"tla": "ARS"},
                    "awayTeam": {"tla": "MCI"},
                    "score": {"fullTime": {"home": 2, "away": 1}},
                    "matchday": 10,
                },
            ],
        }

        async with FootballDataClient() as client:
            client.api_key = "test-key"
            mock_resp = MagicMock()
            mock_resp.json.return_value = mock_response_data
            mock_resp.raise_for_status = MagicMock()

            with patch.object(client._http, "get", new_callable=AsyncMock, return_value=mock_resp):
                result = await client.get_matches(competition="PL", season=2024)

        assert len(result) == 1
        assert result[0]["home_team_tla"] == "ARS"
        assert result[0]["home_score"] == 2
        assert result[0]["matchday"] == 10

    async def test_get_matches_no_api_key(self):
        """Returns empty list when API key not set."""
        from fpl_cli.api.football_data import FootballDataClient

        async with FootballDataClient() as client:
            client.api_key = None
            result = await client.get_matches()

        assert result == []


def _championship_fd(matches):
    """Mock FootballDataClient serving a fixed Championship match list."""
    fd = AsyncMock()
    fd.is_configured = True
    fd.get_matches = AsyncMock(return_value=matches)
    fd.__aenter__ = AsyncMock(return_value=fd)
    fd.__aexit__ = AsyncMock(return_value=False)
    return fd


# COV wins the division outright; XXX and YYY draw with each other.
DOMINANT_CHAMPIONSHIP = [
    {"home_team_tla": "COV", "away_team_tla": "XXX", "home_score": 3, "away_score": 1},
    {"home_team_tla": "COV", "away_team_tla": "YYY", "home_score": 3, "away_score": 1},
    {"home_team_tla": "XXX", "away_team_tla": "COV", "home_score": 1, "away_score": 2},
    {"home_team_tla": "YYY", "away_team_tla": "COV", "home_score": 1, "away_score": 2},
    {"home_team_tla": "XXX", "away_team_tla": "YYY", "home_score": 1, "away_score": 1},
    {"home_team_tla": "YYY", "away_team_tla": "XXX", "home_score": 1, "away_score": 1},
]


class TestPromotedTeamsRankedAgainstPL:
    """A promoted side must be placed on the PL scale, not the Championship's."""

    @pytest.fixture
    def mock_client(self):
        from tests.conftest import make_team

        client = AsyncMock()
        client.get_teams = AsyncMock(return_value=[
            make_team(id=1, name="Arsenal", short_name="ARS"),
            make_team(id=2, name="Man City", short_name="MCI"),
            make_team(id=3, name="Coventry", short_name="COV"),
        ])
        return client

    @pytest.fixture
    def pl_performances(self):
        from fpl_cli.services.team_ratings import TeamPerformance

        return {
            "ARS": TeamPerformance("ARS", 2.5, 2.2, 0.6, 0.8, 19, 19),
            "MCI": TeamPerformance("MCI", 2.4, 2.1, 0.7, 0.9, 19, 19),
        }

    async def _prior(self, mock_client, pl_performances, tmp_path):
        with (
            patch("fpl_cli.services.team_ratings_prior.prior_config_path", return_value=tmp_path / "p.yaml"),
            patch("fpl_cli.services.team_ratings_prior._prior_from_understat", return_value=pl_performances),
            patch("fpl_cli.api.football_data.FootballDataClient", return_value=_championship_fd(DOMINANT_CHAMPIONSHIP)),
        ):
            return await generate_prior(mock_client)

    async def test_championship_winner_is_not_rated_best_in_the_league(
        self, mock_client, pl_performances, tmp_path
    ):
        """Topping the Championship must not yield rating 1 on the PL scale.

        Percentile bucketing is ordinal, so ranking promoted teams among their
        own division handed its champion the same rating as the best team in
        the Premier League.
        """
        result = await self._prior(mock_client, pl_performances, tmp_path)

        assert result["COV"].atk_home > 1
        assert result["COV"].def_home > 1

    async def test_promoted_team_ranks_below_established_sides(
        self, mock_client, pl_performances, tmp_path
    ):
        """Adjusted Championship rates fall short of both PL teams here."""
        result = await self._prior(mock_client, pl_performances, tmp_path)

        for axis in ("atk_home", "atk_away", "def_home", "def_away"):
            assert getattr(result["COV"], axis) > getattr(result["ARS"], axis), axis
            assert getattr(result["COV"], axis) > getattr(result["MCI"], axis), axis

    async def test_relegated_teams_are_excluded_from_the_pool(
        self, mock_client, pl_performances, tmp_path
    ):
        """Last season's departed sides must not skew the percentiles."""
        from fpl_cli.services.team_ratings import TeamPerformance

        pl_performances["BUR"] = TeamPerformance("BUR", 0.9, 0.7, 2.4, 2.6, 19, 19)

        result = await self._prior(mock_client, pl_performances, tmp_path)

        assert "BUR" not in result


class TestChampionshipRescaling:
    """Scored and conceded must be adjusted in opposite directions."""

    def test_factors_move_opposite_ways(self):
        from fpl_cli.services.team_ratings_prior import (
            CHAMPIONSHIP_GOALS_CONCEDED_FACTOR,
            CHAMPIONSHIP_GOALS_SCORED_FACTOR,
        )

        assert CHAMPIONSHIP_GOALS_SCORED_FACTOR < 1 < CHAMPIONSHIP_GOALS_CONCEDED_FACTOR

    async def test_scored_deflated_and_conceded_inflated(self):
        """A promoted side scores less and concedes more once promoted."""
        from fpl_cli.services.team_ratings_prior import _championship_performances

        with patch(
            "fpl_cli.api.football_data.FootballDataClient",
            return_value=_championship_fd(DOMINANT_CHAMPIONSHIP),
        ):
            result = await _championship_performances({"COV"}, 2025)

        assert result is not None
        # COV's raw Championship rates: scored 3.0 home / 2.0 away, conceded 1.0 both.
        assert result["COV"].goals_scored_home < 3.0
        assert result["COV"].goals_scored_away < 2.0
        assert result["COV"].goals_conceded_home > 1.0
        assert result["COV"].goals_conceded_away > 1.0

    async def test_returns_none_without_api_key(self):
        """No Championship data means the caller uses the flat estimate."""
        from fpl_cli.services.team_ratings_prior import _championship_performances

        fd = _championship_fd([])
        fd.is_configured = False

        with patch("fpl_cli.api.football_data.FootballDataClient", return_value=fd):
            assert await _championship_performances({"COV"}, 2025) is None


class TestPromotedFallback:
    """The undifferentiated estimate used when Championship data is missing."""

    def test_each_call_returns_a_distinct_instance(self):
        """TeamRating is mutable and overrides assign onto it in place.

        Sharing one instance would leak a single team's override onto every
        other promoted side.
        """
        from fpl_cli.services.team_ratings_prior import _promoted_fallback

        first, second = _promoted_fallback(), _promoted_fallback()
        first.atk_home = 1

        assert second.atk_home == 5

    async def test_promoted_teams_get_flat_estimate_without_championship_data(self, tmp_path):
        from fpl_cli.services.team_ratings import TeamPerformance
        from tests.conftest import make_team

        client = AsyncMock()
        client.get_teams = AsyncMock(return_value=[
            make_team(id=1, name="Arsenal", short_name="ARS"),
            make_team(id=2, name="Man City", short_name="MCI"),
            make_team(id=3, name="Coventry", short_name="COV"),
        ])
        pl = {
            "ARS": TeamPerformance("ARS", 2.5, 2.2, 0.6, 0.8, 19, 19),
            "MCI": TeamPerformance("MCI", 2.4, 2.1, 0.7, 0.9, 19, 19),
        }

        with (
            patch("fpl_cli.services.team_ratings_prior.prior_config_path", return_value=tmp_path / "p.yaml"),
            patch("fpl_cli.services.team_ratings_prior._prior_from_understat", return_value=pl),
            patch("fpl_cli.services.team_ratings_prior._championship_performances",
                  new_callable=AsyncMock, return_value=None),
        ):
            result = await generate_prior(client)

        assert (result["COV"].atk_home, result["COV"].atk_away) == (5, 6)
        assert (result["COV"].def_home, result["COV"].def_away) == (5, 6)

    async def test_uncovered_promoted_team_gets_flat_estimate_on_partial_coverage(self, tmp_path):
        """Partial Championship coverage must not upgrade a missed team to mid-table.

        With data for only some promoted sides, the uncovered one previously
        fell through to the neutral default 4 — better than the bottom-of-table
        estimate every unmatched promoted team is supposed to get.
        """
        from fpl_cli.services.team_ratings import TeamPerformance
        from tests.conftest import make_team

        client = AsyncMock()
        client.get_teams = AsyncMock(return_value=[
            make_team(id=1, name="Arsenal", short_name="ARS"),
            make_team(id=2, name="Man City", short_name="MCI"),
            make_team(id=3, name="Coventry", short_name="COV"),
            make_team(id=4, name="Hull", short_name="HUL"),
        ])
        pl = {
            "ARS": TeamPerformance("ARS", 2.5, 2.2, 0.6, 0.8, 19, 19),
            "MCI": TeamPerformance("MCI", 2.4, 2.1, 0.7, 0.9, 19, 19),
        }
        championship = {
            "COV": TeamPerformance("COV", 1.3, 1.1, 1.5, 1.7, 23, 23),
        }

        with (
            patch("fpl_cli.services.team_ratings_prior.prior_config_path", return_value=tmp_path / "p.yaml"),
            patch("fpl_cli.services.team_ratings_prior._prior_from_understat", return_value=pl),
            patch("fpl_cli.services.team_ratings_prior._championship_performances",
                  new_callable=AsyncMock, return_value=championship),
        ):
            result = await generate_prior(client)

        # COV is ranked from its data; HUL gets the flat promoted estimate.
        assert (result["HUL"].atk_home, result["HUL"].atk_away) == (5, 6)
        assert (result["HUL"].def_home, result["HUL"].def_away) == (5, 6)
