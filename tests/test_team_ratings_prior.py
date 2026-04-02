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
            patch("fpl_cli.services.team_ratings_prior.PRIOR_CONFIG_PATH", tmp_path / "prior.yaml"),
            patch("fpl_cli.services.team_ratings_prior._prior_from_understat", return_value=None),
            patch("fpl_cli.api.football_data.FootballDataClient", return_value=mock_fd),
        ):
            result = await generate_prior(mock_client)

        assert "ARS" in result
        assert "MCI" in result

    async def test_ultimate_fallback_to_default_4(self, mock_client, tmp_path):
        """When all sources fail, all teams get default rating 4."""
        with (
            patch("fpl_cli.services.team_ratings_prior.PRIOR_CONFIG_PATH", tmp_path / "prior.yaml"),
            patch("fpl_cli.services.team_ratings_prior._prior_from_understat", return_value=None),
            patch("fpl_cli.services.team_ratings_prior._prior_from_football_data", return_value=None),
            patch("fpl_cli.services.team_ratings_prior._championship_prior", new_callable=AsyncMock, return_value={}),
        ):
            result = await generate_prior(mock_client)

        assert result["ARS"].atk_home == 4
        assert result["MCI"].def_away == 4

    async def test_cache_reused_when_teams_match(self, mock_client, tmp_path):
        """Cached prior is returned if team list matches."""
        import yaml

        cache_path = tmp_path / "prior.yaml"
        cached = {
            "metadata": {"source": "prior_understat_xg", "teams": ["ARS", "MCI"]},
            "ratings": {
                "ARS": {"atk_home": 2, "atk_away": 2, "def_home": 2, "def_away": 2},
                "MCI": {"atk_home": 3, "atk_away": 3, "def_home": 3, "def_away": 3},
            },
        }
        with open(cache_path, "w", encoding="utf-8") as f:
            yaml.dump(cached, f)

        with patch("fpl_cli.services.team_ratings_prior.PRIOR_CONFIG_PATH", cache_path):
            result = await generate_prior(mock_client)

        assert result["ARS"].atk_home == 2  # From cache


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
