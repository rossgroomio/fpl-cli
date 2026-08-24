"""Tests for team ratings prior system."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from fpl_cli.services.team_ratings import TeamPerformance, TeamRating
from fpl_cli.services.team_ratings_prior import (
    BLENDING_CUTOFF_GW,
    POOL_RELIABILITY_BY_SOURCE,
    PRIOR_CACHE_VERSION,
    PRIOR_SOURCE_UNDERSTAT,
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

    async def test_no_source_data_returns_no_prior(self, mock_client, tmp_path):
        """When every source fails there is no prior -- not a uniform 4.0 table.

        A flat table would be indistinguishable from a real prior: callers
        would save it as "estimated ratings" and blend genuine current form
        towards it. An empty mapping is falsy, so each caller reports the
        no-data case instead.
        """
        cache_path = tmp_path / "prior.yaml"
        with (
            patch("fpl_cli.services.team_ratings_prior.prior_config_path", return_value=cache_path),
            patch("fpl_cli.services.team_ratings_prior._prior_from_understat", return_value=None),
            patch("fpl_cli.services.team_ratings_prior._prior_from_football_data", return_value=None),
            patch("fpl_cli.services.team_ratings_prior._championship_performances", new_callable=AsyncMock, return_value=None),
        ):
            result = await generate_prior(mock_client)

        assert result == {}
        # Not cached either: the sources may recover on the next run.
        assert not cache_path.exists()

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

    @pytest.mark.parametrize(
        "stale_metadata",
        [
            # Pre-versioning cache: same team set, no version stamp.
            {"source": "prior_understat_xg", "teams": ["ARS", "MCI"]},
            # Stamped with the previous version. This is the case a version
            # bump exists for: the cache holds ratings the current code would
            # never produce, yet keys on the same team names, so the team-set
            # check alone would serve it for the rest of the season.
            {
                "version": PRIOR_CACHE_VERSION - 1,
                "source": "prior_understat_xg",
                "teams": ["ARS", "MCI"],
            },
            # The uniform 4.0 table the old code cached under this source. No
            # longer written, but existing copies must not be served either.
            {
                "version": PRIOR_CACHE_VERSION - 1,
                "source": "prior_default",
                "teams": ["ARS", "MCI"],
            },
        ],
        ids=["unversioned", "previous_version", "previous_version_flat_table"],
    )
    async def test_cache_from_older_version_is_discarded(
        self, mock_client, tmp_path, stale_metadata
    ):
        """A cache written by an older methodology is regenerated, not served."""
        import yaml

        cache_path = tmp_path / "prior.yaml"
        cached = {
            "metadata": stale_metadata,
            "ratings": {
                "ARS": {"atk_home": 1, "atk_away": 1, "def_home": 1, "def_away": 1},
                "MCI": {"atk_home": 1, "atk_away": 1, "def_home": 1, "def_away": 1},
            },
        }
        with open(cache_path, "w", encoding="utf-8") as f:
            yaml.dump(cached, f)

        from fpl_cli.services.team_ratings import TeamPerformance

        fd_performances = {
            "ARS": TeamPerformance(
                team="ARS", goals_scored_home=2.0, goals_scored_away=1.5,
                goals_conceded_home=0.5, goals_conceded_away=1.0, home_games=19, away_games=19,
            ),
            "MCI": TeamPerformance(
                team="MCI", goals_scored_home=1.0, goals_scored_away=0.5,
                goals_conceded_home=2.0, goals_conceded_away=2.5, home_games=19, away_games=19,
            ),
        }

        with (
            patch("fpl_cli.services.team_ratings_prior.prior_config_path", return_value=cache_path),
            patch("fpl_cli.services.team_ratings_prior._prior_from_understat", return_value=None),
            patch(
                "fpl_cli.services.team_ratings_prior._prior_from_football_data",
                new_callable=AsyncMock,
                return_value=fd_performances,
            ),
        ):
            result = await generate_prior(mock_client)

        # Regenerated from the fallback chain, not the stale cache -- which
        # rated the two clubs identically, where the fresh data separates them.
        assert result["MCI"].atk_home != 1
        assert result["ARS"].atk_home < result["MCI"].atk_home


class TestFootballDataGetMatches:
    """Tests for FootballDataClient.get_matches()."""

    async def test_get_matches_returns_parsed(self):
        """Matches are parsed into standardised dicts."""
        from unittest.mock import MagicMock

        from fpl_cli.api.football_data import FootballDataClient

        mock_response_data = {
            "matches": [
                {
                    "homeTeam": {"id": 57, "tla": "ARS"},
                    "awayTeam": {"id": 65, "tla": "MCI"},
                    "score": {"fullTime": {"home": 2, "away": 1}},
                    "matchday": 10,
                    "stage": "REGULAR_SEASON",
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
        assert result[0]["home_team_id"] == 57
        assert result[0]["home_team_tla"] == "ARS"
        assert result[0]["away_team_id"] == 65
        assert result[0]["home_score"] == 2
        assert result[0]["matchday"] == 10
        # Carried through so the prior can drop cup and playoff rounds, which
        # football-data serves in the same batch as the league season.
        assert result[0]["stage"] == "REGULAR_SEASON"

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


# A two-team stand-in for the Premier League pool. It supplies the units the
# promoted cohort's spread is expressed in, for tests that care about the
# Championship side of the conversion rather than the resulting buckets.
PL_POOL = {
    "ARS": TeamPerformance("ARS", 2.5, 2.2, 0.6, 0.8, 19, 19),
    "MCI": TeamPerformance("MCI", 2.4, 2.1, 0.7, 0.9, 19, 19),
}

# Reliability of the Understat xG pool -- the tests' PL pools stand in for the
# primary path, so the spread damping is measured in that path's units.
XG_POOL = POOL_RELIABILITY_BY_SOURCE[PRIOR_SOURCE_UNDERSTAT]


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
        return dict(PL_POOL)

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
            result = await _championship_performances({"COV"}, 2025, PL_POOL, XG_POOL)

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
            assert await _championship_performances({"COV"}, 2025, PL_POOL, XG_POOL) is None


def _evenly_spread(low: float, high: float, count: int) -> list[float]:
    """`count` values from `low` to `high` inclusive, evenly spaced."""
    return [low + (high - low) * i / (count - 1) for i in range(count)]


def _pool(prefix: str, conceded_home: list[float]) -> dict[str, TeamPerformance]:
    """A set of teams differing only on the conceded-at-home axis."""
    return {
        f"{prefix}{i:02d}": TeamPerformance(f"{prefix}{i:02d}", 1.4, 1.2, c, c + 0.2, 19, 19)
        for i, c in enumerate(conceded_home)
    }


# The 2025-26 live distributions from #111, to the sd quoted there: 17 continuing
# PL sides conceding 0.78-1.62 at home (mean 1.20, sd 0.24) and a 24-team
# Championship conceding 0.74-1.62 (mean 1.18, sd 0.25).
LIVE_PL = _pool("P", _evenly_spread(0.78, 1.62, 17))
LIVE_ELC = _pool("C", _evenly_spread(0.74, 1.62, 24))
# Best, mid-table and worst defence in the division.
LIVE_PROMOTED = {"C00", "C11", "C23"}


class TestChampionshipSpread:
    """A promoted cohort must fit inside the distribution it is ranked in (#111)."""

    def test_division_best_defence_is_not_a_top_premier_league_defence(self):
        """The headline case: Ipswich's league-best defence rated 3rd in the PL.

        Multiplying every promoted rate by 1.504 inflated the cohort's spread by
        the same 1.504 as its level, so the division's best defence undershot the
        Premier League mean by more than any actual Premier League team did.
        """
        from fpl_cli.services.team_ratings import TeamRatingsCalculator
        from fpl_cli.services.team_ratings_prior import _rescale_to_pl

        pool = dict(LIVE_PL)
        pool.update(_rescale_to_pl(LIVE_ELC, LIVE_PL, LIVE_PROMOTED, XG_POOL))

        ratings = TeamRatingsCalculator._convert_to_ratings(pool)

        # Bottom third, as a promoted side finishes. The flat factor put this
        # team at 3 -- third-best home defence in the Premier League.
        assert ratings["C00"].def_home >= 5

    def test_no_promoted_side_is_projected_above_the_premier_league_average(self):
        """A calibration guard on the level and spread terms together.

        The cohort sits ~0.5 goals a game worse than the Premier League mean, and
        `k` lets a team claw back only 0.6 of a PL sd per sd of Championship edge,
        so no plausible Championship record reaches average. Under the flat factor
        the division's best defence cleared the PL mean outright.
        """
        from statistics import mean

        from fpl_cli.services.team_ratings_prior import _rescale_to_pl

        rescaled = _rescale_to_pl(LIVE_ELC, LIVE_PL, set(LIVE_ELC), XG_POOL)

        pl_mean = mean(p.goals_conceded_home for p in LIVE_PL.values())
        assert min(p.goals_conceded_home for p in rescaled.values()) > pl_mean

    def test_cohort_spread_is_the_measured_fraction_of_the_pl_spread(self):
        """Standardising then re-expressing puts the cohort in PL units.

        Rescaling the whole division leaves z-scores with sd 1 by construction,
        so the cohort's sd lands on exactly `k` PL standard deviations -- the
        property a multiplicative factor cannot provide, since it scales the
        cohort's own sd instead of adopting the target's.
        """
        from statistics import pstdev

        from fpl_cli.services.team_ratings_prior import (
            CHAMPIONSHIP_TRANSFER_COEFFICIENT,
            _axis_reliability,
            _rescale_to_pl,
        )

        rescaled = _rescale_to_pl(LIVE_ELC, LIVE_PL, set(LIVE_ELC), XG_POOL)

        rho = _axis_reliability(LIVE_ELC.values(), "goals_conceded_home")
        expected_k = CHAMPIONSHIP_TRANSFER_COEFFICIENT * (rho * XG_POOL) ** 0.5
        pl_sd = pstdev([p.goals_conceded_home for p in LIVE_PL.values()])
        cohort_sd = pstdev([p.goals_conceded_home for p in rescaled.values()])

        assert cohort_sd == pytest.approx(expected_k * pl_sd)
        assert cohort_sd < pl_sd

    def test_level_is_exactly_what_the_factors_imply(self):
        """A spread-only change: the cohort mean is unmoved from the old scaling.

        Where each team was multiplied by the factor, the cohort mean was the
        Championship mean times that factor. It still is -- only the distance of
        each team from it has changed.
        """
        from statistics import mean

        from fpl_cli.services.team_ratings_prior import (
            CHAMPIONSHIP_AXES,
            _rescale_to_pl,
        )

        rescaled = _rescale_to_pl(LIVE_ELC, LIVE_PL, set(LIVE_ELC), XG_POOL)

        for axis, factor in CHAMPIONSHIP_AXES:
            elc_mean = mean(getattr(p, axis) for p in LIVE_ELC.values())
            cohort_mean = mean(getattr(p, axis) for p in rescaled.values())
            assert cohort_mean == pytest.approx(elc_mean * factor), axis

    def test_ordering_within_the_division_survives(self):
        """Damping the spread must not scramble who was better than whom."""
        from fpl_cli.services.team_ratings_prior import _rescale_to_pl

        rescaled = _rescale_to_pl(LIVE_ELC, LIVE_PL, set(LIVE_ELC), XG_POOL)

        by_raw = sorted(LIVE_ELC, key=lambda t: LIVE_ELC[t].goals_conceded_home)
        by_rescaled = sorted(rescaled, key=lambda t: rescaled[t].goals_conceded_home)
        assert by_rescaled == by_raw

    def test_edge_is_measured_against_the_whole_division(self):
        """A promoted side's standing comes from the teams it played.

        Standardising over the promoted teams alone would make the best of the
        three the best in the division by definition, however mid-table it was.
        """
        from fpl_cli.services.team_ratings_prior import _rescale_to_pl

        mid_table_only = _rescale_to_pl(LIVE_ELC, LIVE_PL, {"C10", "C11", "C12"}, XG_POOL)
        whole_division = _rescale_to_pl(LIVE_ELC, LIVE_PL, set(LIVE_ELC), XG_POOL)

        for team in mid_table_only:
            assert mid_table_only[team].goals_conceded_home == pytest.approx(
                whole_division[team].goals_conceded_home
            )


def _division(conceded_home: list[float], games: int = 23) -> dict[str, TeamPerformance]:
    """A division whose teams differ only on conceded-at-home, over `games` games."""
    return {
        f"T{i:02d}": TeamPerformance(f"T{i:02d}", 1.4, 1.2, c, 1.4, games, games)
        for i, c in enumerate(conceded_home)
    }


class TestAxisReliability:
    """How much of a division's observed spread is signal rather than sampling noise."""

    def test_spread_below_the_sampling_floor_reports_no_signal(self):
        """24 records indistinguishable from 24 draws of the same number.

        This is goals_conceded_away on live 2025-26 Championship results: the
        observed spread is smaller than Poisson noise over 23 games alone
        produces, so the ordering carries nothing to rank on.
        """
        from fpl_cli.services.team_ratings_prior import _axis_reliability

        # mean 1.2 over 23 games has a noise sd of sqrt(1.2/23) = 0.23; this
        # division's teams differ by a hundredth of that.
        division = _division(_evenly_spread(1.199, 1.201, 24))

        assert _axis_reliability(division.values(), "goals_conceded_home") == 0.0

    def test_spread_well_clear_of_the_floor_reports_signal(self):
        """A division that really does separate its teams keeps most of its spread."""
        from fpl_cli.services.team_ratings_prior import _axis_reliability

        division = _division(_evenly_spread(0.4, 2.4, 24))

        assert _axis_reliability(division.values(), "goals_conceded_home") > 0.7

    def test_more_games_lowers_the_noise_floor(self):
        """The same spread is better evidence over a longer season."""
        from fpl_cli.services.team_ratings_prior import _axis_reliability

        rates = _evenly_spread(0.9, 1.5, 24)
        short = _axis_reliability(_division(rates, games=6).values(), "goals_conceded_home")
        long = _axis_reliability(_division(rates, games=46).values(), "goals_conceded_home")

        assert long > short

    def test_a_single_team_has_no_measurable_spread(self):
        from fpl_cli.services.team_ratings_prior import _axis_reliability

        assert _axis_reliability(_division([1.2]).values(), "goals_conceded_home") == 0.0

    def test_an_axis_with_no_signal_gets_no_ordering(self):
        """A zero-reliability axis must place every promoted side identically.

        Ranking on it would be ranking noise, so the honest output is the level
        term for everyone -- the same answer as having no Championship data.
        """
        from fpl_cli.services.team_ratings_prior import (
            CHAMPIONSHIP_GOALS_CONCEDED_FACTOR,
            _rescale_to_pl,
        )

        division = _division(_evenly_spread(1.199, 1.201, 24))

        rescaled = _rescale_to_pl(division, LIVE_PL, {"T00", "T11", "T23"}, XG_POOL)

        expected = 1.2 * CHAMPIONSHIP_GOALS_CONCEDED_FACTOR
        for perf in rescaled.values():
            assert perf.goals_conceded_home == pytest.approx(expected, abs=1e-3)

    def test_a_noisier_axis_is_damped_harder_than_a_cleaner_one(self):
        """Two axes in one division must not share a single damping factor."""
        from statistics import pstdev

        from fpl_cli.services.team_ratings_prior import _rescale_to_pl

        # conceded_home separates the teams; conceded_away does not.
        division = {
            f"T{i:02d}": TeamPerformance(f"T{i:02d}", 1.4, 1.2, c, 1.4, 23, 23)
            for i, c in enumerate(_evenly_spread(0.4, 2.4, 24))
        }

        rescaled = _rescale_to_pl(division, LIVE_PL, set(division), XG_POOL)

        clean = pstdev([p.goals_conceded_home for p in rescaled.values()])
        noisy = pstdev([p.goals_conceded_away for p in rescaled.values()])
        assert clean > 0
        assert noisy == pytest.approx(0.0, abs=1e-9)


class TestPlayoffMatchesExcluded:
    """Championship playoffs must not count towards a promoted side's rates."""

    @staticmethod
    def _match(home, away, hs, a_s, stage="REGULAR_SEASON"):
        return {
            "home_team_tla": home, "away_team_tla": away,
            "home_team_id": hash(home) % 1000, "away_team_id": hash(away) % 1000,
            "home_score": hs, "away_score": a_s, "stage": stage,
        }

    def test_playoff_results_do_not_reach_performances(self):
        """The playoff winner is a promoted side, so this lands where it hurts.

        football-data serves the playoffs in the same batch as the league
        season, and the final is at Wembley yet carries a nominal home team.
        """
        from fpl_cli.services.team_ratings_prior import _matches_to_performances

        league = [
            self._match("AAA", "BBB", 1, 0),
            self._match("BBB", "AAA", 1, 0),
        ]
        with_playoff = [*league, self._match("AAA", "BBB", 5, 0, stage="PLAYOFFS")]

        clean = _matches_to_performances(league)
        contaminated = _matches_to_performances(with_playoff)

        assert clean["AAA"].home_games == 1
        assert contaminated["AAA"].home_games == 1
        assert contaminated["AAA"].goals_scored_home == clean["AAA"].goals_scored_home

    def test_matches_without_a_stage_are_kept(self):
        """An unrecognised payload must degrade, not silently empty the prior."""
        from fpl_cli.services.team_ratings_prior import _matches_to_performances

        matches = [
            {"home_team_tla": "AAA", "away_team_tla": "BBB", "home_team_id": 1,
             "away_team_id": 2, "home_score": 1, "away_score": 0},
            {"home_team_tla": "BBB", "away_team_tla": "AAA", "home_team_id": 2,
             "away_team_id": 1, "home_score": 1, "away_score": 0},
        ]

        assert set(_matches_to_performances(matches)) == {"AAA", "BBB"}


class TestPoolReliabilityBySource:
    """How noisily the pool is measured decides how hard promoted sides are damped."""

    def test_both_prior_sources_are_covered(self):
        """A source with no entry would raise a KeyError in generate_prior."""
        from fpl_cli.services.team_ratings_prior import (
            POOL_RELIABILITY_BY_SOURCE,
            PRIOR_SOURCE_FOOTBALL_DATA,
            PRIOR_SOURCE_UNDERSTAT,
        )

        assert set(POOL_RELIABILITY_BY_SOURCE) == {
            PRIOR_SOURCE_UNDERSTAT,
            PRIOR_SOURCE_FOOTBALL_DATA,
        }

    def test_the_goals_pool_is_read_as_noisier_than_the_xg_pool(self):
        """Actual goals over 19 games say less about a team than xG does."""
        from fpl_cli.services.team_ratings_prior import (
            POOL_RELIABILITY_BY_SOURCE,
            PRIOR_SOURCE_FOOTBALL_DATA,
            PRIOR_SOURCE_UNDERSTAT,
        )

        assert (
            POOL_RELIABILITY_BY_SOURCE[PRIOR_SOURCE_FOOTBALL_DATA]
            < POOL_RELIABILITY_BY_SOURCE[PRIOR_SOURCE_UNDERSTAT]
        )

    def test_a_noisier_pool_damps_the_promoted_cohort_harder(self):
        """The same Championship evidence is worth less against a noisier pool.

        Reusing the xG figure on the raw-goals fallback path would overstate
        every promoted rating there, since a real gap is a smaller share of a
        noisier pool's observed spread.
        """
        from statistics import pstdev

        from fpl_cli.services.team_ratings_prior import (
            POOL_RELIABILITY_BY_SOURCE,
            PRIOR_SOURCE_FOOTBALL_DATA,
            _rescale_to_pl,
        )

        goals_pool = POOL_RELIABILITY_BY_SOURCE[PRIOR_SOURCE_FOOTBALL_DATA]
        against_xg = _rescale_to_pl(LIVE_ELC, LIVE_PL, set(LIVE_ELC), XG_POOL)
        against_goals = _rescale_to_pl(LIVE_ELC, LIVE_PL, set(LIVE_ELC), goals_pool)

        spread_xg = pstdev([p.goals_conceded_home for p in against_xg.values()])
        spread_goals = pstdev([p.goals_conceded_home for p in against_goals.values()])
        assert spread_goals < spread_xg

    async def test_the_football_data_path_uses_the_goals_reliability(self, tmp_path):
        """The source that produced the pool picks the figure, not a fixed constant."""
        from unittest.mock import ANY

        from fpl_cli.services.team_ratings_prior import (
            POOL_RELIABILITY_BY_SOURCE,
            PRIOR_SOURCE_FOOTBALL_DATA,
        )
        from tests.conftest import make_team

        client = AsyncMock()
        client.get_teams = AsyncMock(return_value=[
            make_team(id=1, name="Arsenal", short_name="ARS"),
            make_team(id=2, name="Man City", short_name="MCI"),
            make_team(id=3, name="Coventry", short_name="COV"),
        ])
        championship = AsyncMock(return_value=None)

        with (
            patch("fpl_cli.services.team_ratings_prior.prior_config_path",
                  return_value=tmp_path / "p.yaml"),
            patch("fpl_cli.services.team_ratings_prior._prior_from_understat",
                  new_callable=AsyncMock, return_value=None),
            patch("fpl_cli.services.team_ratings_prior._prior_from_football_data",
                  new_callable=AsyncMock, return_value=dict(PL_POOL)),
            patch("fpl_cli.services.team_ratings_prior._championship_performances",
                  championship),
        ):
            await generate_prior(client)

        championship.assert_awaited_once_with(
            {"COV"}, ANY, ANY, POOL_RELIABILITY_BY_SOURCE[PRIOR_SOURCE_FOOTBALL_DATA]
        )


class TestChampionshipSpreadDegradation:
    """Neither distribution having any spread leaves only the level to apply."""

    def test_identical_championship_rates_collapse_onto_the_level(self):
        """No evidence of a difference between promoted sides means no difference."""
        from fpl_cli.services.team_ratings_prior import (
            CHAMPIONSHIP_GOALS_CONCEDED_FACTOR,
            _rescale_to_pl,
        )

        flat = _pool("C", [1.2, 1.2, 1.2])

        rescaled = _rescale_to_pl(flat, LIVE_PL, set(flat), XG_POOL)

        for perf in rescaled.values():
            assert perf.goals_conceded_home == pytest.approx(
                1.2 * CHAMPIONSHIP_GOALS_CONCEDED_FACTOR
            )

    def test_a_single_premier_league_team_leaves_no_units_to_scale_by(self):
        """A one-team pool has no sd, so there is no PL spread to place teams on."""
        from fpl_cli.services.team_ratings_prior import _rescale_to_pl

        rescaled = _rescale_to_pl(LIVE_ELC, _pool("P", [1.2]), LIVE_PROMOTED, XG_POOL)

        values = {round(p.goals_conceded_home, 9) for p in rescaled.values()}
        assert len(values) == 1

    def test_an_empty_premier_league_pool_does_not_raise(self):
        """The caller can reach here with no continuing-team records at all."""
        from fpl_cli.services.team_ratings_prior import _rescale_to_pl

        rescaled = _rescale_to_pl(LIVE_ELC, {}, LIVE_PROMOTED, XG_POOL)

        assert set(rescaled) == LIVE_PROMOTED

    def test_rates_never_go_negative(self):
        """A per-game goal rate below zero is not a thing, however far out a team sits."""
        from fpl_cli.services.team_ratings_prior import _rescale_to_pl

        # A Championship where one side is far clear, against a pool whose own
        # spread dwarfs the level the cohort is placed at.
        wild_pl = _pool("P", [0.1, 40.0])

        rescaled = _rescale_to_pl(LIVE_ELC, wild_pl, LIVE_PROMOTED, XG_POOL)

        for perf in rescaled.values():
            for axis in ("goals_scored_home", "goals_scored_away",
                         "goals_conceded_home", "goals_conceded_away"):
                assert getattr(perf, axis) >= 0.0, axis


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


class TestTlaToFplMapping:
    """Regression coverage for the NOT/NFO naming mismatch (#110).

    football-data uses "NOT" for Nottingham Forest; FPL uses "NFO". Without
    the mapping, Forest's matches never join, and generate_prior reclassifies
    an established side as promoted -- see TestTlaToFplMapping below and
    _promoted_fallback().
    """

    def test_not_maps_to_nfo(self):
        from fpl_cli.services.team_ratings_prior import TLA_TO_FPL

        assert TLA_TO_FPL["NOT"] == "NFO"

    def test_forest_matches_join_under_nfo_not_left_unresolved(self):
        """A "NOT"-labelled match must resolve to NFO, not fall through as if unseen."""
        from fpl_cli.services.team_ratings_prior import _matches_to_performances

        matches = [
            {"home_team_tla": "NOT", "away_team_tla": "ARS", "home_score": 2, "away_score": 1},
            {"home_team_tla": "ARS", "away_team_tla": "NOT", "home_score": 1, "away_score": 1},
        ]

        performances = _matches_to_performances(matches)

        assert "NFO" in performances
        assert "NOT" not in performances


class TestAmbiguousTla:
    """A tla backed by two distinct football-data ids must not pool their results (#110).

    football-data's 2025-26 Championship serves both Sheffield United and
    Sheffield Wednesday as "SHE". Pooling them would average two different
    clubs' seasons into one record.
    """

    # Two distinct team ids (1 and 2) both appear under tla "SHE"; id 3 (COV)
    # is unambiguous throughout.
    AMBIGUOUS_MATCHES = [
        {
            "home_team_id": 1, "home_team_tla": "SHE",
            "away_team_id": 3, "away_team_tla": "COV",
            "home_score": 2, "away_score": 0,
        },
        {
            "home_team_id": 3, "home_team_tla": "COV",
            "away_team_id": 2, "away_team_tla": "SHE",
            "home_score": 1, "away_score": 1,
        },
        {
            "home_team_id": 2, "home_team_tla": "SHE",
            "away_team_id": 3, "away_team_tla": "COV",
            "home_score": 0, "away_score": 3,
        },
    ]

    def test_ambiguous_team_gets_no_performance_record(self):
        from fpl_cli.services.team_ratings_prior import _matches_to_performances

        performances = _matches_to_performances(self.AMBIGUOUS_MATCHES)

        assert "SHE" not in performances

    def test_unambiguous_opponent_is_still_counted(self):
        """COV's own record must survive even though its opponent's tla collides."""
        from fpl_cli.services.team_ratings_prior import _matches_to_performances

        performances = _matches_to_performances(self.AMBIGUOUS_MATCHES)

        assert "COV" in performances
        assert performances["COV"].home_games == 1
        assert performances["COV"].away_games == 2

    def test_collision_logs_a_warning(self, caplog):
        import logging

        from fpl_cli.services.team_ratings_prior import _matches_to_performances

        with caplog.at_level(logging.WARNING, logger="fpl_cli.services.team_ratings_prior"):
            _matches_to_performances(self.AMBIGUOUS_MATCHES)

        assert "SHE" in caplog.text


class TestMissingPerformanceRecordWarnings:
    """A team with no performance record must be surfaced, not silently reclassified (#110)."""

    async def test_championship_lookup_warns_for_an_uncovered_promoted_team(self, caplog):
        import logging

        from fpl_cli.services.team_ratings_prior import _championship_performances

        with (
            caplog.at_level(logging.WARNING, logger="fpl_cli.services.team_ratings_prior"),
            patch(
                "fpl_cli.api.football_data.FootballDataClient",
                return_value=_championship_fd(DOMINANT_CHAMPIONSHIP),
            ),
        ):
            result = await _championship_performances({"COV", "ZZZ"}, 2025, PL_POOL, XG_POOL)

        assert result is not None
        assert "COV" in result
        assert "ZZZ" not in result
        assert "ZZZ" in caplog.text

    async def test_generate_prior_logs_when_a_team_gets_no_performance_record(self, caplog, tmp_path):
        import logging

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
            caplog.at_level(logging.INFO, logger="fpl_cli.services.team_ratings_prior"),
            patch("fpl_cli.services.team_ratings_prior.prior_config_path", return_value=tmp_path / "p.yaml"),
            patch("fpl_cli.services.team_ratings_prior._prior_from_understat", return_value=pl),
            patch("fpl_cli.services.team_ratings_prior._championship_performances",
                  new_callable=AsyncMock, return_value=None),
        ):
            await generate_prior(client)

        assert "COV" in caplog.text
