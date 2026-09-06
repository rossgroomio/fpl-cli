"""Tests for team ratings prior system."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from fpl_cli.services.team_ratings import TeamPerformance, TeamRating
from fpl_cli.services.team_ratings_prior import (
    BLENDING_CUTOFF_GW,
    POOL_RELIABILITY_BY_SOURCE,
    PRIOR_BASIS_CHAMPIONSHIP,
    PRIOR_BASIS_FALLBACK,
    PRIOR_BASIS_INCOMPLETE,
    PRIOR_BASIS_PREMIER_LEAGUE,
    PRIOR_CACHE_VERSION,
    PRIOR_MIN_GAMES_PER_VENUE,
    PRIOR_SOURCE_UNDERSTAT,
    REGRESSION_CONSTANT,
    ChampionshipRecords,
    _full_season_records,
    _is_full_season,
    _prior_from_football_data,
    _prior_from_understat,
    blend_with_prior,
    describe_prior_inputs,
    generate_prior,
    load_prior_inputs,
    rebuild_prior,
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
        # A season's worth (10H/10A each) so the full-season bar does not
        # reject the pool: the fallback chain is what this test is about.
        mock_fd.get_matches = AsyncMock(return_value=[
            {"home_team_tla": "ARS", "away_team_tla": "MCI", "home_score": 2, "away_score": 1, "matchday": 1},
            {"home_team_tla": "MCI", "away_team_tla": "ARS", "home_score": 3, "away_score": 0, "matchday": 2},
        ] * 10)
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


# COV wins the division outright; XXX and YYY draw with each other. One round
# is 2H/2A per club; six rounds make it a season's worth against the
# full-season bar (12H/12A each, means and spread unchanged).
_DOMINANT_ROUND = [
    {"home_team_tla": "COV", "away_team_tla": "XXX", "home_score": 3, "away_score": 1},
    {"home_team_tla": "COV", "away_team_tla": "YYY", "home_score": 3, "away_score": 1},
    {"home_team_tla": "XXX", "away_team_tla": "COV", "home_score": 1, "away_score": 2},
    {"home_team_tla": "YYY", "away_team_tla": "COV", "home_score": 1, "away_score": 2},
    {"home_team_tla": "XXX", "away_team_tla": "YYY", "home_score": 1, "away_score": 1},
    {"home_team_tla": "YYY", "away_team_tla": "XXX", "home_score": 1, "away_score": 1},
]
DOMINANT_CHAMPIONSHIP = _DOMINANT_ROUND * 6


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
        played = result.played["COV"]
        assert (played.goals_scored_home, played.goals_scored_away) == (3.0, 2.0)
        assert (played.goals_conceded_home, played.goals_conceded_away) == (1.0, 1.0)
        ranked = result.ranked["COV"]
        assert ranked.goals_scored_home < 3.0
        assert ranked.goals_scored_away < 2.0
        assert ranked.goals_conceded_home > 1.0
        assert ranked.goals_conceded_away > 1.0

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
        championship = ChampionshipRecords(
            played={"COV": TeamPerformance("COV", 2.0, 1.6, 0.9, 1.1, 23, 23)},
            ranked={"COV": TeamPerformance("COV", 1.3, 1.1, 1.5, 1.7, 23, 23)},
        )

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
        assert "COV" in result.ranked
        assert "ZZZ" not in result.ranked
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


class TestPriorSourceFailureLogging:
    """Each source's failure warning must carry no traceback: fpl-cli
    configures no logging handlers, so a WARNING with exc_info reaches
    logging's lastResort handler and dumps it raw into stderr, including
    under `--format json` (issue #237/#239 review).
    """

    async def test_understat_source_failure_logs_no_traceback(self, caplog):
        import logging

        client = AsyncMock()
        with (
            patch(
                "fpl_cli.services.team_ratings.TeamRatingsCalculator.calculate_from_xg",
                new_callable=AsyncMock,
                side_effect=RuntimeError("Understat unreachable"),
            ),
            caplog.at_level(logging.WARNING),
        ):
            result = await _prior_from_understat(client, "2024-25")

        assert result is None
        records = [r for r in caplog.records if "Failed to generate prior from Understat" in r.message]
        assert len(records) == 1
        assert records[0].exc_info is None

    async def test_football_data_source_failure_logs_no_traceback(self, caplog):
        import logging

        with (
            patch(
                "fpl_cli.api.football_data.FootballDataClient",
                side_effect=RuntimeError("football-data.org unreachable"),
            ),
            caplog.at_level(logging.WARNING),
        ):
            result = await _prior_from_football_data(2024)

        assert result is None
        records = [
            r for r in caplog.records if "Failed to generate prior from football-data.org" in r.message
        ]
        assert len(records) == 1
        assert records[0].exc_info is None


# --- A club back after exactly one season away (#235) ---

def _understat_season(start_year: int, xg_for: float, xg_against: float) -> list[dict]:
    """A completed Understat season: 19 home and 19 away results, dated in it."""
    from datetime import date, timedelta

    matches = []
    for i in range(38):
        kickoff = date(start_year, 8, 16) + timedelta(days=7 * i)
        side = "h" if i % 2 == 0 else "a"
        xg = {"h": str(xg_for), "a": str(xg_against)} if side == "h" else {
            "h": str(xg_against), "a": str(xg_for)
        }
        matches.append({
            "id": str(i), "isResult": True, "side": side, "xG": xg,
            "datetime": f"{kickoff.isoformat()} 15:00:00",
        })
    return matches


def _understat_substituted_season(start_year: int) -> list[dict]:
    """What Understat serves for a season a club has no record of: its most
    recent season instead, here the one in progress -- a full fixture list
    with one result played (Ipswich's opener in #235: 1.58 xG for, 1.18
    against, at home)."""
    from datetime import date, timedelta

    matches = [{
        "id": "opener", "isResult": True, "side": "h",
        "xG": {"h": "1.58452", "a": "1.17743"},
        "datetime": f"{date(start_year, 8, 22).isoformat()} 14:00:00",
    }]
    for i in range(1, 38):
        kickoff = date(start_year, 8, 22) + timedelta(days=7 * i)
        matches.append({
            "id": str(i), "isResult": False, "side": "h" if i % 2 else "a",
            "xG": {"h": "0", "a": "0"},
            "datetime": f"{kickoff.isoformat()} 15:00:00",
        })
    return matches


# IPS tops a three-club Championship; the other two draw with each other.
IPSWICH_CHAMPIONSHIP = [
    {**m, "home_team_tla": m["home_team_tla"].replace("COV", "IPS"),
     "away_team_tla": m["away_team_tla"].replace("COV", "IPS")}
    for m in DOMINANT_CHAMPIONSHIP
]


class TestClubReturningAfterOneSeasonAway:
    """Ipswich in 2026-27: relegated in 2025, promoted again a year later.

    Understat holds their 2024-25 Premier League season and, once GW1 kicks
    off, their 2026-27 one -- and answers a request for 2025-26, which they
    spent in the Championship, with the 2026-27 fixture list. Their first
    home match then read as last season's Premier League xG and, with the
    away venue estimated from it, bucketed to def 2/2 against nineteen full
    seasons (#235). The prior must treat them as what they are: a promoted
    side, rated from their Championship record like the other two.
    """

    # (name, short, xG per game, xGA per game) -- a pool where 1.18 xGA, the
    # opener's, sits third of thirteen: exactly the def 2 the issue reported.
    PL_CLUBS = [
        ("Arsenal", "ARS", 2.4, 0.9), ("Man City", "MCI", 2.3, 1.0),
        ("Liverpool", "LIV", 2.2, 1.25), ("Chelsea", "CHE", 2.0, 1.3),
        ("Spurs", "TOT", 1.8, 1.35), ("Newcastle", "NEW", 1.7, 1.4),
        ("Aston Villa", "AVL", 1.6, 1.45), ("Brighton", "BHA", 1.5, 1.5),
        ("Everton", "EVE", 1.3, 1.6), ("Fulham", "FUL", 1.3, 1.7),
        ("Brentford", "BRE", 1.2, 1.8), ("Bournemouth", "BOU", 1.1, 1.9),
    ]

    @pytest.fixture
    def client(self):
        from tests.conftest import make_team

        client = AsyncMock()
        teams = [
            make_team(id=i + 1, name=name, short_name=short)
            for i, (name, short, _, _) in enumerate(self.PL_CLUBS)
        ]
        teams.append(make_team(id=99, name="Ipswich Town", short_name="IPS"))
        client.get_teams = AsyncMock(return_value=teams)
        return client

    @pytest.fixture
    def understat_payloads(self):
        """getTeamData by (url_name, season): last season for the continuing
        clubs, and the season in progress for Ipswich whatever was asked."""
        from fpl_cli.api.understat import TEAM_NAME_MAP
        from fpl_cli.season import get_season_year

        prev = get_season_year() - 1
        payloads = {}
        for name, _, xg_for, xg_against in self.PL_CLUBS:
            url_name = TEAM_NAME_MAP.get(name, name).replace(" ", "_")
            payloads[(url_name, str(prev))] = {
                "players": [], "dates": _understat_season(prev, xg_for, xg_against),
            }
        payloads[("Ipswich", str(prev))] = {
            "players": [], "dates": _understat_substituted_season(prev + 1),
        }
        return payloads

    async def _prior(self, client, understat_payloads, tmp_path):
        async def get_team_json(url_name, season):
            return understat_payloads.get((url_name, season))

        with (
            patch("fpl_cli.services.team_ratings_prior.prior_config_path",
                  return_value=tmp_path / "p.yaml"),
            patch("fpl_cli.api.understat.UnderstatClient._get_team_json",
                  side_effect=get_team_json),
            patch("fpl_cli.api.football_data.FootballDataClient",
                  return_value=_championship_fd(IPSWICH_CHAMPIONSHIP)),
        ):
            return await generate_prior(client)

    async def test_the_returning_club_is_rated_as_a_promoted_side(
        self, client, understat_payloads, tmp_path
    ):
        """Their basis is the Championship record, not one match of this season."""
        await self._prior(client, understat_payloads, tmp_path)

        with patch("fpl_cli.services.team_ratings_prior.prior_config_path",
                   return_value=tmp_path / "p.yaml"):
            inputs = load_prior_inputs()

        assert inputs is not None
        assert inputs["IPS"]["basis"] == PRIOR_BASIS_CHAMPIONSHIP
        # The three-club Championship season, not the single 2026-27 home match.
        assert (inputs["IPS"]["home_games"], inputs["IPS"]["away_games"]) == (12, 12)
        assert inputs["ARS"]["basis"] == PRIOR_BASIS_PREMIER_LEAGUE
        assert (inputs["ARS"]["home_games"], inputs["ARS"]["away_games"]) == (19, 19)

    async def test_one_home_match_does_not_make_a_top_tier_defence(
        self, client, understat_payloads, tmp_path
    ):
        """The symptom: def 2/2 from 1.18 xGA in one match, which this pool
        reproduces with the guards off. A promoted side damped onto the
        Premier League spread never sits inside the top third on any axis."""
        result = await self._prior(client, understat_payloads, tmp_path)

        assert len(result) == len(self.PL_CLUBS) + 1
        for axis in ("atk_home", "atk_away", "def_home", "def_away"):
            assert getattr(result["IPS"], axis) >= 3, axis

    async def test_the_substitution_is_logged_where_the_fetch_happens(
        self, client, understat_payloads, tmp_path, caplog
    ):
        import logging

        with caplog.at_level(logging.INFO, logger="fpl_cli.api.understat"):
            await self._prior(client, understat_payloads, tmp_path)

        assert "no" in caplog.text and "record for Ipswich" in caplog.text


def _record(team: str, home: int, away: int) -> TeamPerformance:
    return TeamPerformance(team, 1.5, 1.2, 1.2, 1.5, home, away)


class TestFullSeasonRecordsOnly:
    """The prior's pools are completed seasons; a fragment is split out."""

    def test_a_club_short_of_the_bar_on_either_venue_is_a_fragment(self):
        pool = {
            "ARS": _record("ARS", 19, 19),
            "IPS": _record("IPS", 1, 0),
            "HOM": _record("HOM", 19, PRIOR_MIN_GAMES_PER_VENUE - 1),
            "EDG": _record("EDG", PRIOR_MIN_GAMES_PER_VENUE, PRIOR_MIN_GAMES_PER_VENUE),
        }

        full, fragments = _full_season_records(pool, "2025-26", "Premier League")

        assert set(full) == {"ARS", "EDG"}
        assert set(fragments) == {"IPS", "HOM"}
        assert not _is_full_season(pool["HOM"])
        assert _is_full_season(pool["EDG"])

    def test_a_fragment_is_named_with_its_record_and_division(self, caplog):
        import logging

        pool = {"ARS": _record("ARS", 19, 19), "IPS": _record("IPS", 1, 0)}

        with caplog.at_level(logging.WARNING, logger="fpl_cli.services.team_ratings_prior"):
            _full_season_records(pool, "2025-26", "Premier League")
            _full_season_records({"FRG": _record("FRG", 1, 1)}, "2025-26", "Championship")

        assert "IPS shows 1H/0A matches in the 2025-26 Premier League record" in caplog.text
        assert "FRG shows 1H/1A matches in the 2025-26 Championship record" in caplog.text
        assert "ARS" not in caplog.text

    async def test_the_understat_prior_counts_full_seasons_towards_its_club_count(self):
        """Eleven full seasons and a fragment is a pool worth serving (the
        split happens in generate_prior); ten fragments is no pool at all,
        so the fallback source gets its turn."""
        full = {f"T{i:02d}": _record(f"T{i:02d}", 19, 19) for i in range(11)}
        client = AsyncMock()

        with patch(
            "fpl_cli.services.team_ratings.TeamRatingsCalculator.calculate_from_xg",
            new_callable=AsyncMock,
            return_value=({}, {**full, "IPS": _record("IPS", 1, 0)}),
        ):
            result = await _prior_from_understat(client, "2025")
        assert result is not None
        assert set(result) == set(full) | {"IPS"}

        fragments = {f"T{i:02d}": _record(f"T{i:02d}", 2, 1) for i in range(20)}
        with patch(
            "fpl_cli.services.team_ratings.TeamRatingsCalculator.calculate_from_xg",
            new_callable=AsyncMock,
            return_value=({}, fragments),
        ):
            assert await _prior_from_understat(client, "2025") is None


def _prev_label() -> str:
    from fpl_cli.season import get_season_year, season_label

    return season_label(get_season_year() - 1)


class TestIncompleteRecord:
    """A served-but-partial Premier League record is not a promoted side's.

    A continuing club whose fetch broke would otherwise fall out of the pool
    and, having no Championship record, land on the promoted side's
    bottom-of-table estimate -- Arsenal rated as relegation fodder over a
    data hiccup. A club with a Premier League page is far more likely
    continuing than promoted, so it takes the neutral rating instead, and
    says so.
    """

    @staticmethod
    def _client(*shorts: str):
        from tests.conftest import make_team

        client = AsyncMock()
        client.get_teams = AsyncMock(
            return_value=[
                make_team(id=i + 1, name=short, short_name=short) for i, short in enumerate(shorts)
            ]
        )
        return client

    async def test_a_continuing_club_with_a_fragment_is_rated_neutral(self, tmp_path, caplog):
        import logging

        pool = {
            "ARS": TeamPerformance("ARS", 2.5, 2.2, 0.6, 0.8, 3, 17),
            "MCI": TeamPerformance("MCI", 2.4, 2.1, 0.7, 0.9, 19, 19),
            "LIV": TeamPerformance("LIV", 2.0, 1.8, 1.0, 1.1, 19, 19),
        }
        championship = AsyncMock(return_value=None)

        with (
            caplog.at_level(logging.WARNING, logger="fpl_cli.services.team_ratings_prior"),
            patch("fpl_cli.services.team_ratings_prior.prior_config_path", return_value=tmp_path / "p.yaml"),
            patch("fpl_cli.services.team_ratings_prior._prior_from_understat", return_value=pool),
            patch("fpl_cli.services.team_ratings_prior._championship_performances", championship),
        ):
            result = await generate_prior(self._client("ARS", "MCI", "LIV"))
            inputs = load_prior_inputs()

        assert (result["ARS"].atk_home, result["ARS"].atk_away) == (4, 4)
        assert (result["ARS"].def_home, result["ARS"].def_away) == (4, 4)
        # Looked up in the Championship first, in case the season guard was inert.
        assert championship.await_args.args[0] == {"ARS"}
        assert inputs is not None
        assert inputs["ARS"]["basis"] == PRIOR_BASIS_INCOMPLETE
        assert (inputs["ARS"]["home_games"], inputs["ARS"]["away_games"]) == (3, 17)
        assert inputs["ARS"]["served"]["conceded_home"] == 0.6
        assert f"ARS: a {_prev_label()} Premier League record was served" in caplog.text
        assert "rated neutral mid-table" in caplog.text

    async def test_a_fragment_with_a_championship_record_is_ranked_from_it(self, tmp_path):
        """The season guard inert and a promoted side's own opener served as
        last season's: its Championship record still settles it."""
        pool = {
            "ARS": TeamPerformance("ARS", 2.5, 2.2, 0.6, 0.8, 19, 19),
            "MCI": TeamPerformance("MCI", 2.4, 2.1, 0.7, 0.9, 19, 19),
            "IPS": TeamPerformance("IPS", 1.585, 1.585, 1.177, 1.177, 1, 0),
        }
        championship = ChampionshipRecords(
            played={"IPS": TeamPerformance("IPS", 2.0, 1.6, 0.9, 1.1, 23, 23)},
            ranked={"IPS": TeamPerformance("IPS", 1.3, 1.1, 1.5, 1.7, 23, 23)},
        )

        with (
            patch("fpl_cli.services.team_ratings_prior.prior_config_path", return_value=tmp_path / "p.yaml"),
            patch("fpl_cli.services.team_ratings_prior._prior_from_understat", return_value=pool),
            patch("fpl_cli.services.team_ratings_prior._championship_performances",
                  new_callable=AsyncMock, return_value=championship),
        ):
            result = await generate_prior(self._client("ARS", "MCI", "IPS"))
            inputs = load_prior_inputs()

        assert inputs is not None
        assert inputs["IPS"]["basis"] == PRIOR_BASIS_CHAMPIONSHIP
        assert (inputs["IPS"]["home_games"], inputs["IPS"]["away_games"]) == (23, 23)
        assert result["IPS"].def_home > result["ARS"].def_home

    async def test_the_bar_holds_on_the_football_data_path_too(self, tmp_path):
        """The fallback source is held to the same bar as the primary one."""
        pool = {
            "ARS": TeamPerformance("ARS", 2.5, 2.2, 0.6, 0.8, 19, 19),
            "MCI": TeamPerformance("MCI", 2.4, 2.1, 0.7, 0.9, 19, 19),
            "COV": TeamPerformance("COV", 1.2, 1.0, 1.6, 1.8, 3, 17),
        }

        with (
            patch("fpl_cli.services.team_ratings_prior.prior_config_path", return_value=tmp_path / "p.yaml"),
            patch("fpl_cli.services.team_ratings_prior._prior_from_understat", return_value=None),
            patch("fpl_cli.services.team_ratings_prior._prior_from_football_data",
                  new_callable=AsyncMock, return_value=pool),
            patch("fpl_cli.services.team_ratings_prior._championship_performances",
                  new_callable=AsyncMock, return_value=None),
        ):
            result = await generate_prior(self._client("ARS", "MCI", "COV"))
            inputs = load_prior_inputs()

        assert inputs is not None
        assert inputs["COV"]["basis"] == PRIOR_BASIS_INCOMPLETE
        assert (result["COV"].atk_home, result["COV"].def_away) == (4, 4)
        # The two full seasons were ranked against each other alone.
        assert inputs["ARS"]["basis"] == inputs["MCI"]["basis"] == PRIOR_BASIS_PREMIER_LEAGUE

    async def test_a_pool_of_fragments_is_no_prior(self, tmp_path, caplog):
        import logging

        pool = {
            "ARS": TeamPerformance("ARS", 2.5, 2.2, 0.6, 0.8, 2, 1),
            "MCI": TeamPerformance("MCI", 2.4, 2.1, 0.7, 0.9, 1, 2),
        }
        cache_path = tmp_path / "p.yaml"

        with (
            caplog.at_level(logging.WARNING, logger="fpl_cli.services.team_ratings_prior"),
            patch("fpl_cli.services.team_ratings_prior.prior_config_path", return_value=cache_path),
            patch("fpl_cli.services.team_ratings_prior._prior_from_understat", return_value=None),
            patch("fpl_cli.services.team_ratings_prior._prior_from_football_data",
                  new_callable=AsyncMock, return_value=pool),
        ):
            result = await generate_prior(self._client("ARS", "MCI"))

        assert result == {}
        assert not cache_path.exists()
        assert "served no full-season record for any club" in caplog.text


class TestChampionshipDivisionGate:
    """A partial club must not skew the baseline every promoted side is measured against."""

    async def test_a_fragment_is_left_out_of_the_division(self, caplog):
        import logging

        from fpl_cli.services.team_ratings_prior import (
            _championship_performances,
            _rescale_to_pl,
        )

        # FRG played one match at each venue, both against XXX.
        with_fragment = [
            *DOMINANT_CHAMPIONSHIP,
            {"home_team_tla": "FRG", "away_team_tla": "XXX", "home_score": 0, "away_score": 9},
            {"home_team_tla": "XXX", "away_team_tla": "FRG", "home_score": 9, "away_score": 0},
        ]

        with (
            caplog.at_level(logging.WARNING, logger="fpl_cli.services.team_ratings_prior"),
            patch("fpl_cli.api.football_data.FootballDataClient",
                  return_value=_championship_fd(with_fragment)),
            patch("fpl_cli.services.team_ratings_prior._rescale_to_pl",
                  wraps=_rescale_to_pl) as rescale,
        ):
            result = await _championship_performances({"COV"}, 2025, PL_POOL, XG_POOL)

        assert result is not None and "COV" in result.ranked
        division = rescale.call_args.args[0]
        assert set(division) == {"COV", "XXX", "YYY"}
        assert "FRG shows 1H/1A matches in the 2025-26 Championship record" in caplog.text


class TestMoreAbsenteesThanPromotedClubs:
    """Three clubs come up each season; a fourth absentee is a continuing club that failed to join."""

    @staticmethod
    def _client(*shorts: str):
        from tests.conftest import make_team

        client = AsyncMock()
        client.get_teams = AsyncMock(
            return_value=[
                make_team(id=i + 1, name=short, short_name=short) for i, short in enumerate(shorts)
            ]
        )
        return client

    async def _absent(self, count: int, tmp_path, caplog) -> str:
        import logging

        absent = [f"A{i}" for i in range(count)]
        with (
            caplog.at_level(logging.WARNING, logger="fpl_cli.services.team_ratings_prior"),
            patch("fpl_cli.services.team_ratings_prior.prior_config_path", return_value=tmp_path / "p.yaml"),
            patch("fpl_cli.services.team_ratings_prior._prior_from_understat", return_value=dict(PL_POOL)),
            patch("fpl_cli.services.team_ratings_prior._championship_performances",
                  new_callable=AsyncMock, return_value=None),
        ):
            await generate_prior(self._client("ARS", "MCI", *absent))
        return caplog.text

    async def test_a_fourth_absentee_is_called_out(self, tmp_path, caplog):
        text = await self._absent(4, tmp_path, caplog)

        assert "4 clubs have no record in last season's Premier League pool (A0, A1, A2, A3)" in text
        assert "at least one continuing club failed to join" in text

    async def test_three_absentees_are_the_ordinary_case(self, tmp_path, caplog):
        text = await self._absent(3, tmp_path, caplog)

        assert "failed to join" not in text


class TestPriorInputsInTheCache:
    """The cache says what each rating was ranked on, so it can be traced."""

    @pytest.fixture
    def cache_path(self, tmp_path):
        return tmp_path / "prior.yaml"

    async def _generate(self, cache_path):
        from tests.conftest import make_team

        client = AsyncMock()
        client.get_teams = AsyncMock(return_value=[
            make_team(id=1, name="Arsenal", short_name="ARS"),
            make_team(id=2, name="Man City", short_name="MCI"),
            make_team(id=3, name="Coventry", short_name="COV"),
            make_team(id=4, name="Hull", short_name="HUL"),
        ])
        championship = ChampionshipRecords(
            played={"COV": TeamPerformance("COV", 2.0, 1.6, 0.9, 1.1, 23, 23)},
            ranked={"COV": TeamPerformance("COV", 1.3, 1.1, 1.5, 1.7, 23, 23)},
        )
        with (
            patch("fpl_cli.services.team_ratings_prior.prior_config_path", return_value=cache_path),
            patch("fpl_cli.services.team_ratings_prior._prior_from_understat",
                  return_value=dict(PL_POOL)),
            patch("fpl_cli.services.team_ratings_prior._championship_performances",
                  new_callable=AsyncMock, return_value=championship),
        ):
            return await generate_prior(client)

    async def test_every_club_records_its_basis(self, cache_path):
        import yaml

        await self._generate(cache_path)

        with open(cache_path, encoding="utf-8") as f:
            data = yaml.safe_load(f)

        inputs = data["inputs"]
        assert inputs["ARS"] == {
            "basis": PRIOR_BASIS_PREMIER_LEAGUE,
            "home_games": 19,
            "away_games": 19,
            "ranked": {"scored_home": 2.5, "scored_away": 2.2,
                       "conceded_home": 0.6, "conceded_away": 0.8},
        }
        assert inputs["COV"] == {
            "basis": PRIOR_BASIS_CHAMPIONSHIP,
            "home_games": 23,
            "away_games": 23,
            # Both sides of the damping: as played, and as ranked.
            "played": {"scored_home": 2.0, "scored_away": 1.6,
                       "conceded_home": 0.9, "conceded_away": 1.1},
            "ranked": {"scored_home": 1.3, "scored_away": 1.1,
                       "conceded_home": 1.5, "conceded_away": 1.7},
        }
        assert inputs["HUL"] == {"basis": PRIOR_BASIS_FALLBACK}

    async def test_metadata_names_the_promoted_clubs_and_the_season(self, cache_path):
        import yaml

        from fpl_cli.season import get_season_year, season_label

        await self._generate(cache_path)

        with open(cache_path, encoding="utf-8") as f:
            data = yaml.safe_load(f)

        assert data["metadata"]["version"] == PRIOR_CACHE_VERSION
        assert data["metadata"]["promoted"] == ["COV", "HUL"]
        assert data["metadata"]["incomplete"] == []
        assert data["metadata"]["based_on_season"] == season_label(get_season_year() - 1)
        # The ratings block is unchanged in shape: the trace sits beside it.
        assert set(data["ratings"]["ARS"]) == {"atk_home", "atk_away", "def_home", "def_away"}

    async def test_the_file_still_loads_as_a_cache(self, cache_path):
        """A header comment and an extra block must not break the reader."""
        from fpl_cli.services.team_ratings_prior import (
            _ratings_from_cache,
            _read_prior_cache,
        )

        generated = await self._generate(cache_path)

        with patch("fpl_cli.services.team_ratings_prior.prior_config_path", return_value=cache_path):
            data = _read_prior_cache()

        assert data is not None
        assert _ratings_from_cache(data) == generated

    async def test_inputs_read_back(self, cache_path):
        await self._generate(cache_path)

        with patch("fpl_cli.services.team_ratings_prior.prior_config_path", return_value=cache_path):
            inputs = load_prior_inputs()
            note = describe_prior_inputs()

        assert inputs is not None
        assert {team: entry["basis"] for team, entry in inputs.items()} == {
            "ARS": PRIOR_BASIS_PREMIER_LEAGUE,
            "MCI": PRIOR_BASIS_PREMIER_LEAGUE,
            "COV": PRIOR_BASIS_CHAMPIONSHIP,
            "HUL": PRIOR_BASIS_FALLBACK,
        }
        assert note is not None
        assert "COV from Championship results" in note
        assert "HUL on the flat promoted estimate" in note
        assert str(cache_path) in note

    def test_no_provenance_without_a_cache_or_on_an_old_one(self, cache_path):
        import yaml

        with patch("fpl_cli.services.team_ratings_prior.prior_config_path", return_value=cache_path):
            assert load_prior_inputs() is None
            assert describe_prior_inputs() is None

            # A pre-inputs cache: ratings only.
            ratings = {"ARS": {"atk_home": 1, "atk_away": 1, "def_home": 1, "def_away": 1}}
            with open(cache_path, "w", encoding="utf-8") as f:
                yaml.dump({
                    "metadata": {"version": PRIOR_CACHE_VERSION - 1, "teams": ["ARS"]},
                    "ratings": ratings,
                }, f)
            assert load_prior_inputs() is None
            assert describe_prior_inputs() is None

            # A stale-version cache that does carry inputs: refused by the same
            # version check that stops its ratings being served, so the trace
            # shown is never one for a prior the tool would not use.
            with open(cache_path, "w", encoding="utf-8") as f:
                yaml.dump({
                    "metadata": {"version": PRIOR_CACHE_VERSION - 1, "teams": ["ARS"]},
                    "ratings": ratings,
                    "inputs": {"ARS": {"basis": PRIOR_BASIS_PREMIER_LEAGUE}},
                }, f)
            assert load_prior_inputs() is None


class TestCacheInvalidationOnBetterInputs:
    """Configuring FOOTBALL_DATA_API_KEY must reach the prior on its own (#112).

    The cache had two validity tests -- the schema version and the league's
    club list -- and neither can see that *this install's inputs* changed.
    Setting the key mid-season left every promoted side pinned to the flat
    bottom-of-table estimate until the file was deleted by hand.

    The comparison has to stay one-directional: a rebuild happens only when
    this run can do better than the cached one, never merely differently, so a
    key that goes away cannot replace a good prior with a worse one.
    """

    TEAMS = ("ARS", "MCI", "COV")
    CACHED_RATING = 7

    @pytest.fixture
    def cache_path(self, tmp_path):
        return tmp_path / "prior.yaml"

    @pytest.fixture(autouse=True)
    def _no_key_by_default(self, monkeypatch):
        """Never inherit a real key -- these tests are about its presence."""
        monkeypatch.delenv("FOOTBALL_DATA_API_KEY", raising=False)

    @classmethod
    def _client(cls):
        from tests.conftest import make_team

        client = AsyncMock()
        client.get_teams = AsyncMock(return_value=[
            make_team(id=i, name=name, short_name=name)
            for i, name in enumerate(cls.TEAMS, start=1)
        ])
        return client

    @classmethod
    def _write_cache(cls, path, *, configured, cov_basis=PRIOR_BASIS_FALLBACK):
        """A valid current-version cache, flat at CACHED_RATING so a rebuild shows.

        ``configured`` is what the cached run recorded for its football-data
        key; None writes a file from before that field existed.
        """
        import yaml

        metadata = {
            "version": PRIOR_CACHE_VERSION,
            "source": PRIOR_SOURCE_UNDERSTAT,
            "teams": sorted(cls.TEAMS),
        }
        if configured is not None:
            metadata["football_data_configured"] = configured
        inputs = {team: {"basis": PRIOR_BASIS_PREMIER_LEAGUE} for team in cls.TEAMS}
        inputs["COV"] = {"basis": cov_basis}
        rating = dict.fromkeys(("atk_home", "atk_away", "def_home", "def_away"), cls.CACHED_RATING)
        with open(path, "w", encoding="utf-8") as f:
            yaml.dump({
                "metadata": metadata,
                "ratings": {team: dict(rating) for team in cls.TEAMS},
                "inputs": inputs,
            }, f)

    @staticmethod
    async def _generate(cache_path, *, sources=True, refresh=False, pl_pool=None):
        """Run the prior against a two-club PL pool plus a promoted COV.

        ``refresh`` takes the forced path (`rebuild_prior`) rather than the
        cache-gated one, and returns its ratings so both read alike here.
        """
        championship = ChampionshipRecords(
            played={"COV": TeamPerformance("COV", 2.0, 1.6, 0.9, 1.1, 23, 23)},
            ranked={"COV": TeamPerformance("COV", 1.3, 1.1, 1.5, 1.7, 23, 23)},
        )
        mock_championship = AsyncMock(return_value=championship if sources else None)
        with (
            patch("fpl_cli.services.team_ratings_prior.prior_config_path", return_value=cache_path),
            patch("fpl_cli.services.team_ratings_prior._prior_from_understat",
                  return_value=(pl_pool if pl_pool is not None else dict(PL_POOL))
                  if sources else None),
            patch("fpl_cli.services.team_ratings_prior._prior_from_football_data",
                  new_callable=AsyncMock, return_value=None),
            patch("fpl_cli.services.team_ratings_prior._championship_performances",
                  mock_championship),
        ):
            client = TestCacheInvalidationOnBetterInputs._client()
            result = (
                (await rebuild_prior(client)).prior if refresh else await generate_prior(client)
            )
        return result, mock_championship

    def _is_cached_copy(self, prior):
        return all(r.atk_home == self.CACHED_RATING for r in prior.values())

    async def test_a_key_configured_since_the_cache_was_written_rebuilds_it(
        self, cache_path, monkeypatch
    ):
        """The issue's case: the key appears, so the promoted side is re-rated."""
        self._write_cache(cache_path, configured=False)
        monkeypatch.setenv("FOOTBALL_DATA_API_KEY", "test-key")

        prior, mock_championship = await self._generate(cache_path)

        mock_championship.assert_awaited_once()
        assert not self._is_cached_copy(prior)
        # COV is ranked against the PL pool now, not sitting on the flat estimate.
        with patch("fpl_cli.services.team_ratings_prior.prior_config_path", return_value=cache_path):
            inputs = load_prior_inputs()
        assert inputs is not None
        assert inputs["COV"]["basis"] == PRIOR_BASIS_CHAMPIONSHIP

    async def test_a_cache_that_already_had_the_key_is_served(self, cache_path, monkeypatch):
        """Nothing new is available, so there is nothing to rebuild for."""
        self._write_cache(cache_path, configured=True)
        monkeypatch.setenv("FOOTBALL_DATA_API_KEY", "test-key")

        prior, mock_championship = await self._generate(cache_path)

        assert self._is_cached_copy(prior)
        mock_championship.assert_not_awaited()

    async def test_still_no_key_serves_the_cache(self, cache_path):
        """The input the cache went without is still missing."""
        self._write_cache(cache_path, configured=False)

        prior, mock_championship = await self._generate(cache_path)

        assert self._is_cached_copy(prior)
        mock_championship.assert_not_awaited()

    async def test_a_key_is_no_reason_to_rebuild_a_prior_with_nothing_to_gain(
        self, cache_path, monkeypatch
    ):
        """Every club on its own PL record is already the best this code does."""
        self._write_cache(cache_path, configured=False, cov_basis=PRIOR_BASIS_PREMIER_LEAGUE)
        monkeypatch.setenv("FOOTBALL_DATA_API_KEY", "test-key")

        prior, mock_championship = await self._generate(cache_path)

        assert self._is_cached_copy(prior)
        mock_championship.assert_not_awaited()

    async def test_a_key_going_away_never_invalidates(self, cache_path):
        """One-directional: a source lost is not a reason to build a worse prior.

        A transient football-data outage (or a key pulled) would otherwise
        discard a cache whose promoted sides carry real Championship evidence
        and replace them with the flat estimate.
        """
        self._write_cache(cache_path, configured=True, cov_basis=PRIOR_BASIS_CHAMPIONSHIP)

        prior, mock_championship = await self._generate(cache_path)

        assert self._is_cached_copy(prior)
        mock_championship.assert_not_awaited()

    async def test_a_cache_from_before_the_field_rebuilds_at_most_once(
        self, cache_path, monkeypatch
    ):
        """An existing file says nothing either way, so the clubs decide it.

        It must not rebuild every run: the rebuilt file records the answer, so
        the second call is served from the cache.
        """
        self._write_cache(cache_path, configured=None)
        monkeypatch.setenv("FOOTBALL_DATA_API_KEY", "test-key")

        first, first_championship = await self._generate(cache_path)
        second, second_championship = await self._generate(cache_path)

        first_championship.assert_awaited_once()
        assert not self._is_cached_copy(first)
        second_championship.assert_not_awaited()
        assert second == first

    async def test_a_rebuild_that_finds_nothing_keeps_the_cached_prior(
        self, cache_path, monkeypatch
    ):
        """A provider down must not cost the caller the prior it already had.

        Returning {} here would have every caller report "no prior" and fall
        back to flat mid-table, with a perfectly good file still on disk.
        """
        self._write_cache(cache_path, configured=False)
        monkeypatch.setenv("FOOTBALL_DATA_API_KEY", "test-key")

        prior, _ = await self._generate(cache_path, sources=False)

        assert self._is_cached_copy(prior)
        assert prior.keys() == set(self.TEAMS)

    async def test_refresh_rebuilds_a_cache_that_would_have_been_served(self, cache_path):
        """The escape hatch, for a reason provenance cannot infer."""
        self._write_cache(cache_path, configured=True)

        prior, mock_championship = await self._generate(cache_path, refresh=True)

        mock_championship.assert_awaited_once()
        assert not self._is_cached_copy(prior)

    async def test_a_forced_refresh_that_finds_nothing_keeps_the_cached_prior(self, cache_path):
        """Forcing a rebuild can never be worse than not forcing one."""
        self._write_cache(cache_path, configured=True)

        prior, _ = await self._generate(cache_path, sources=False, refresh=True)

        assert self._is_cached_copy(prior)

    async def test_a_cache_for_a_different_league_is_not_fallen_back_to(self, cache_path):
        """The team-set check is a different verdict: that cache is simply wrong.

        Holding it as a fallback would serve last season's clubs when the
        rebuild fails, which is the state #106/#109 exist to end.
        """
        import yaml

        self._write_cache(cache_path, configured=True)
        with open(cache_path, encoding="utf-8") as f:
            data = yaml.safe_load(f)
        data["ratings"] = {
            f"OLD{i}": dict.fromkeys(
                ("atk_home", "atk_away", "def_home", "def_away"), self.CACHED_RATING
            )
            for i in range(4)
        }
        with open(cache_path, "w", encoding="utf-8") as f:
            yaml.dump(data, f)

        prior, _ = await self._generate(cache_path, sources=False)

        assert prior == {}

    @pytest.mark.parametrize("key, expected", [("test-key", True), (None, False)])
    async def test_the_cache_records_whether_a_key_was_configured(
        self, cache_path, monkeypatch, key, expected
    ):
        """The field the comparison reads back, written on every save."""
        import yaml

        if key:
            monkeypatch.setenv("FOOTBALL_DATA_API_KEY", key)

        await self._generate(cache_path)

        with open(cache_path, encoding="utf-8") as f:
            metadata = yaml.safe_load(f)["metadata"]
        assert metadata["football_data_configured"] is expected

    async def test_the_flat_estimate_carries_its_remedy(self, cache_path, monkeypatch):
        """An undifferentiated promoted cohort is the only visible symptom.

        Without the remedy attached, `source: prior_understat_xg` reads the
        same whether the promoted sides came from Championship data or the
        flat estimate, so there is nothing to act on.
        """
        with (
            patch("fpl_cli.services.team_ratings_prior.prior_config_path", return_value=cache_path),
            patch("fpl_cli.services.team_ratings_prior._prior_from_understat",
                  return_value=dict(PL_POOL)),
            patch("fpl_cli.services.team_ratings_prior._championship_performances",
                  new_callable=AsyncMock, return_value=None),
        ):
            await generate_prior(self._client())
            without_key = describe_prior_inputs()
            monkeypatch.setenv("FOOTBALL_DATA_API_KEY", "test-key")
            with_key = describe_prior_inputs()

        assert "COV on the flat promoted estimate" in without_key
        assert "set FOOTBALL_DATA_API_KEY" in without_key
        # Already set: naming it again would be advice the user has taken.
        assert "COV on the flat promoted estimate" in with_key
        assert "FOOTBALL_DATA_API_KEY" not in with_key


class TestARebuildMayNotDowngradeThePrior:
    """A rebuild is a chance to improve the prior, never a licence to worsen it.

    Emptiness was the only gate, so a rebuild that came back full but degraded
    -- Understat failing mid-run and dropping the pool to football-data's
    noisier goals, football-data 429ing and dropping the promoted sides back to
    the flat estimate -- overwrote a better cached prior and then latched,
    because the file it wrote recorded that these inputs had been tried.
    """

    TEAMS = ("ARS", "MCI", "COV")
    CACHED_RATING = 2

    @pytest.fixture
    def cache_path(self, tmp_path):
        return tmp_path / "prior.yaml"

    @pytest.fixture(autouse=True)
    def _key_set(self, monkeypatch):
        monkeypatch.setenv("FOOTBALL_DATA_API_KEY", "test-key")

    @classmethod
    def _client(cls):
        from tests.conftest import make_team

        client = AsyncMock()
        client.get_teams = AsyncMock(return_value=[
            make_team(id=i, name=name, short_name=name)
            for i, name in enumerate(cls.TEAMS, start=1)
        ])
        return client

    @classmethod
    def _write_cache(cls, path, *, source, cov_basis):
        import yaml

        inputs = {team: {"basis": PRIOR_BASIS_PREMIER_LEAGUE} for team in cls.TEAMS}
        inputs["COV"] = {"basis": cov_basis}
        axes = dict.fromkeys(
            ("atk_home", "atk_away", "def_home", "def_away"), cls.CACHED_RATING
        )
        with open(path, "w", encoding="utf-8") as f:
            yaml.dump({
                "metadata": {
                    "version": PRIOR_CACHE_VERSION,
                    "source": source,
                    # Not yet tried with a key, so the automatic gate opens for
                    # a cache that has a club to gain.
                    "football_data_configured": False,
                    "teams": sorted(cls.TEAMS),
                },
                "ratings": {team: dict(axes) for team in cls.TEAMS},
                "inputs": inputs,
            }, f)

    @classmethod
    async def _run(cls, cache_path, *, pl_source=True, championship=True, refresh=False):
        """A rebuild whose PL source and Championship lookup can each be failed.

        ``pl_source`` False fails Understat and serves football-data instead --
        the real fallback, and the source regression the guard has to catch;
        ``None`` fails both, leaving nothing to build from.
        """
        records = ChampionshipRecords(
            played={"COV": TeamPerformance("COV", 2.0, 1.6, 0.9, 1.1, 23, 23)},
            ranked={"COV": TeamPerformance("COV", 1.3, 1.1, 1.5, 1.7, 23, 23)},
        )
        with (
            patch("fpl_cli.services.team_ratings_prior.prior_config_path", return_value=cache_path),
            patch("fpl_cli.services.team_ratings_prior._prior_from_understat",
                  return_value=dict(PL_POOL) if pl_source is True else None),
            patch("fpl_cli.services.team_ratings_prior._prior_from_football_data",
                  new_callable=AsyncMock,
                  return_value=dict(PL_POOL) if pl_source is False else None),
            patch("fpl_cli.services.team_ratings_prior._championship_performances",
                  new_callable=AsyncMock, return_value=records if championship else None),
        ):
            client = cls._client()
            return (await rebuild_prior(client)).prior if refresh else await generate_prior(client)

    @staticmethod
    def _cache(cache_path):
        import yaml

        with open(cache_path, encoding="utf-8") as f:
            return yaml.safe_load(f)

    def _is_cached_copy(self, prior):
        return bool(prior) and all(r.atk_home == self.CACHED_RATING for r in prior.values())

    async def test_a_rebuild_on_a_worse_source_is_refused(self, cache_path):
        """Understat failing mid-run must not swap an xG prior for a goals one."""
        self._write_cache(
            cache_path, source=PRIOR_SOURCE_UNDERSTAT, cov_basis=PRIOR_BASIS_FALLBACK
        )

        prior = await self._run(cache_path, pl_source=False)

        assert self._is_cached_copy(prior)
        assert self._cache(cache_path)["metadata"]["source"] == PRIOR_SOURCE_UNDERSTAT

    async def test_a_rebuild_that_loses_a_championship_club_is_refused(self, cache_path):
        """football-data 429ing mid-rebuild must not un-rate a promoted side.

        Forced, because a cache with no club left to gain never rebuilds on its
        own -- which is the shape `--refresh-prior` puts at risk.
        """
        self._write_cache(
            cache_path, source=PRIOR_SOURCE_UNDERSTAT, cov_basis=PRIOR_BASIS_CHAMPIONSHIP
        )

        prior = await self._run(cache_path, championship=False, refresh=True)

        assert self._is_cached_copy(prior)
        assert self._cache(cache_path)["inputs"]["COV"]["basis"] == PRIOR_BASIS_CHAMPIONSHIP

    async def test_a_rebuild_that_improves_a_club_is_kept(self, cache_path):
        """The case the whole feature exists for still goes through."""
        self._write_cache(
            cache_path, source=PRIOR_SOURCE_UNDERSTAT, cov_basis=PRIOR_BASIS_FALLBACK
        )

        prior = await self._run(cache_path)

        assert not self._is_cached_copy(prior)
        assert self._cache(cache_path)["inputs"]["COV"]["basis"] == PRIOR_BASIS_CHAMPIONSHIP

    async def test_a_refused_rebuild_still_records_the_attempt(self, cache_path):
        """Otherwise the trigger stays hot and every command re-runs the providers.

        The file keeps its ratings and its trace; only the record of which
        inputs have been tried moves on, which is what bounds the retry to one.
        """
        self._write_cache(
            cache_path, source=PRIOR_SOURCE_UNDERSTAT, cov_basis=PRIOR_BASIS_FALLBACK
        )

        await self._run(cache_path, pl_source=False)
        cached = self._cache(cache_path)

        assert cached["metadata"]["football_data_configured"] is True
        assert cached["metadata"]["source"] == PRIOR_SOURCE_UNDERSTAT
        assert cached["inputs"]["COV"]["basis"] == PRIOR_BASIS_FALLBACK

        # ...and the next call is served straight from it, providers untouched.
        with (
            patch("fpl_cli.services.team_ratings_prior.prior_config_path",
                  return_value=cache_path),
            patch("fpl_cli.services.team_ratings_prior._prior_from_understat",
                  side_effect=AssertionError("providers must not be consulted again")),
        ):
            again = await generate_prior(self._client())

        assert self._is_cached_copy(again)

    async def test_a_rebuild_that_finds_nothing_records_the_attempt_too(self, cache_path):
        """Both providers down is the same bounded retry, not one per command."""
        self._write_cache(
            cache_path, source=PRIOR_SOURCE_UNDERSTAT, cov_basis=PRIOR_BASIS_FALLBACK
        )

        prior = await self._run(cache_path, pl_source=None, championship=False)

        assert self._is_cached_copy(prior)
        assert self._cache(cache_path)["metadata"]["football_data_configured"] is True

    async def test_the_remedy_survives_the_key_being_set(self, cache_path):
        """A latched cache must not become undiscoverable from the output.

        With a key set and clubs still on the flat estimate, the "set the key"
        line no longer applies, and without a replacement the one way back to a
        healthy prior is nowhere in the tool's output.
        """
        self._write_cache(
            cache_path, source=PRIOR_SOURCE_UNDERSTAT, cov_basis=PRIOR_BASIS_FALLBACK
        )

        with patch(
            "fpl_cli.services.team_ratings_prior.prior_config_path", return_value=cache_path
        ):
            note = describe_prior_inputs()

        assert note is not None
        assert "COV on the flat promoted estimate" in note
        assert "--refresh-prior" in note


class TestMalformedCachedRatings:
    """A file documented as inspectable will be hand-edited, so it is validated.

    An unguarded `null` axis became `TeamRating(atk_home=None)` and survived to
    blend_with_prior's arithmetic, failing a long way from the cause.
    """

    @pytest.fixture
    def cache_path(self, tmp_path):
        return tmp_path / "prior.yaml"

    @staticmethod
    def _write(path, ratings):
        import yaml

        with open(path, "w", encoding="utf-8") as f:
            yaml.dump({
                "metadata": {"version": PRIOR_CACHE_VERSION, "teams": ["ARS"]},
                "ratings": ratings,
            }, f)

    @pytest.mark.parametrize(
        "ratings",
        [
            {"ARS": {"atk_home": None, "atk_away": 2, "def_home": 2, "def_away": 2}},
            {"ARS": {"atk_home": 70, "atk_away": 2, "def_home": 2, "def_away": 2}},
            {"ARS": {"atk_home": "2", "atk_away": 2, "def_home": 2, "def_away": 2}},
            {"ARS": "not a mapping"},
        ],
        ids=["null_axis", "out_of_range", "string_axis", "not_a_mapping"],
    )
    def test_a_malformed_rating_refuses_the_whole_cache(self, cache_path, ratings):
        from fpl_cli.services.team_ratings_prior import _read_prior_cache

        self._write(cache_path, ratings)
        with patch(
            "fpl_cli.services.team_ratings_prior.prior_config_path", return_value=cache_path
        ):
            assert _read_prior_cache() is None

    def test_a_missing_axis_still_defaults(self, cache_path):
        """Absent axes have always defaulted to a neutral 4 -- only bad ones are new."""
        from fpl_cli.services.team_ratings_prior import _ratings_from_cache, _read_prior_cache

        self._write(cache_path, {"ARS": {"atk_home": 2}})
        with patch(
            "fpl_cli.services.team_ratings_prior.prior_config_path", return_value=cache_path
        ):
            data = _read_prior_cache()

        assert data is not None
        assert _ratings_from_cache(data)["ARS"] == TeamRating(2, 4, 4, 4)
