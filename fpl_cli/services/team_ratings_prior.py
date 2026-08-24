"""Prior generation and blending for early-season team ratings.

Uses previous season data to smooth ratings when current-season sample
is small. Bayesian shrinkage with REGRESSION_CONSTANT=6 and hard cutoff
at GW12.
"""

from __future__ import annotations

import logging
from pathlib import Path
from statistics import mean
from typing import TYPE_CHECKING, Any

import yaml

from fpl_cli.paths import user_data_file
from fpl_cli.services.team_ratings import TeamPerformance, TeamRating

if TYPE_CHECKING:
    from fpl_cli.api.fpl import FPLClient

logger = logging.getLogger(__name__)

def prior_config_path() -> Path:
    """Team ratings prior cache location."""
    return user_data_file("team_ratings_prior.yaml")


REGRESSION_CONSTANT = 6
BLENDING_CUTOFF_GW = 12

# Bump whenever the prior methodology changes: a cache written by an older
# version passes the team-set check below yet holds ratings the new code
# would never produce, so it must be discarded rather than served.
PRIOR_CACHE_VERSION = 2

# Championship-to-PL adjustment. A promoted side scores less in the PL against
# better defences, and concedes more against better attacks, so the two
# directions move opposite ways: applying one factor to both (or to raw match
# scores, where one team's goal is another's concession) deflates conceded when
# it should inflate it.
CHAMPIONSHIP_GOALS_SCORED_FACTOR = 0.665
CHAMPIONSHIP_GOALS_CONCEDED_FACTOR = 1 / CHAMPIONSHIP_GOALS_SCORED_FACTOR

# football-data.org TLA to FPL short name (only where they differ).
# Most TLAs match directly; add exceptions here as discovered.
TLA_TO_FPL: dict[str, str] = {}


def _matches_to_performances(
    matches: list[dict[str, Any]], team_tlas: set[str]
) -> dict[str, TeamPerformance]:
    """Aggregate match results into per-game scored/conceded rates by venue."""
    # Aggregate per-team stats
    stats: dict[str, dict[str, list[float]]] = {}
    for tla in team_tlas:
        fpl_name = TLA_TO_FPL.get(tla, tla)
        stats[fpl_name] = {
            "scored_home": [],
            "scored_away": [],
            "conceded_home": [],
            "conceded_away": [],
        }

    for m in matches:
        home = TLA_TO_FPL.get(m["home_team_tla"], m["home_team_tla"])
        away = TLA_TO_FPL.get(m["away_team_tla"], m["away_team_tla"])
        if home not in stats or away not in stats:
            continue
        stats[home]["scored_home"].append(m["home_score"])
        stats[home]["conceded_home"].append(m["away_score"])
        stats[away]["scored_away"].append(m["away_score"])
        stats[away]["conceded_away"].append(m["home_score"])

    performances: dict[str, TeamPerformance] = {}
    for team, data in stats.items():
        h = len(data["scored_home"])
        a = len(data["scored_away"])
        if h == 0 or a == 0:
            continue
        performances[team] = TeamPerformance(
            team=team,
            goals_scored_home=mean(data["scored_home"]),
            goals_scored_away=mean(data["scored_away"]),
            goals_conceded_home=mean(data["conceded_home"]),
            goals_conceded_away=mean(data["conceded_away"]),
            home_games=h,
            away_games=a,
        )

    return performances


def _load_prior_cache() -> dict[str, TeamRating] | None:
    """Load cached prior from disk, or None if missing or outdated."""
    path = prior_config_path()
    if not path.exists():
        return None
    with open(path, encoding="utf-8") as f:
        data = yaml.safe_load(f)
    if not data or "ratings" not in data or not isinstance(data["ratings"], dict):
        return None
    metadata = data.get("metadata")
    if not isinstance(metadata, dict) or metadata.get("version") != PRIOR_CACHE_VERSION:
        return None
    ratings = {}
    for team, r in data["ratings"].items():
        ratings[team] = TeamRating(
            atk_home=r.get("atk_home", 4),
            atk_away=r.get("atk_away", 4),
            def_home=r.get("def_home", 4),
            def_away=r.get("def_away", 4),
        )
    return ratings


def _save_prior_cache(
    ratings: dict[str, TeamRating], source: str, teams: list[str]
) -> None:
    """Save prior to disk for caching (atomic write)."""
    from fpl_cli.utils.files import atomic_write_text

    path = prior_config_path()
    data: dict[str, Any] = {
        "metadata": {
            "version": PRIOR_CACHE_VERSION,
            "source": source,
            "teams": sorted(teams),
        },
        "ratings": {},
    }
    for team in sorted(ratings):
        r = ratings[team]
        data["ratings"][team] = {
            "atk_home": r.atk_home,
            "atk_away": r.atk_away,
            "def_home": r.def_home,
            "def_away": r.def_away,
        }
    atomic_write_text(path, yaml.dump(data, default_flow_style=False, sort_keys=False))


async def generate_prior(client: FPLClient) -> dict[str, TeamRating]:
    """Generate prior ratings from previous season data.

    Fallback chain: Understat xG/xGA -> football-data.org.
    Promoted teams use Championship data rescaled to PL-equivalent rates.

    Returns:
        Ratings by team short name, or an empty dict when no source has
        previous-season data. Callers must treat the empty case as "no prior"
        rather than substituting a uniform table -- see the comment below.
    """
    teams = await client.get_teams()
    current_team_names = {t.short_name for t in teams}

    # Check cache validity
    cached = _load_prior_cache()
    if cached is not None:
        cached_teams = set(cached.keys())
        mismatches = len(current_team_names - cached_teams) + len(cached_teams - current_team_names)
        if mismatches <= 2:
            return cached

    from fpl_cli.season import get_season_year

    current_season_year = get_season_year()
    prev_season = str(current_season_year - 1)
    prev_season_int = current_season_year - 1

    # Raw per-game rates first: buckets are only assigned once every team,
    # continuing and promoted alike, sits in the same pool.
    performances = await _prior_from_understat(client, prev_season)
    source = "prior_understat_xg"

    if not performances:
        performances = await _prior_from_football_data(prev_season_int)
        source = "prior_football_data"

    if not performances:
        # No previous-season evidence from any source. Returning a uniform 4.0
        # table here would be indistinguishable from a real prior to every
        # caller: seed_from_prior() would save it and report "estimated
        # ratings", blend_with_prior() would shrink genuine current form
        # towards flat mid-table, and the cache would serve it all season.
        # An empty mapping is falsy, so each caller takes its no-prior branch
        # and says so. Deliberately not cached -- the sources may recover.
        logger.warning("No previous-season data available for a ratings prior")
        return {}

    # Last season's relegated sides would otherwise skew the percentiles and
    # then be dropped, so rank only the teams actually in the league.
    performances = {t: p for t, p in performances.items() if t in current_team_names}

    # Promoted teams join the same pool on PL-rescaled rates, so the 1-7 spread
    # is a ranking of the real 20 rather than two incomparable divisions. (On the
    # Understat path the pool mixes xG rates with rescaled actual goals — close
    # enough to nudge a promoted side by at most a bucket, not reorder the pool.)
    # A promoted team the Championship data doesn't cover gets the flat estimate
    # individually, so partial coverage never promotes it to mid-table by omission.
    promoted = current_team_names - set(performances)
    promoted_performances = (
        await _championship_performances(promoted, prev_season_int) or {} if promoted else {}
    )
    performances.update(promoted_performances)
    fallback = {team: _promoted_fallback() for team in promoted - set(promoted_performances)}

    from fpl_cli.services.team_ratings import TeamRatingsCalculator

    prior = TeamRatingsCalculator._convert_to_ratings(performances)
    prior.update(fallback)

    _save_prior_cache(prior, source, list(current_team_names))
    return prior


async def _prior_from_understat(
    client: FPLClient, prev_season: str
) -> dict[str, TeamPerformance] | None:
    """Per-game xG/xGA rates for last season's PL teams.

    Returns raw rates rather than 1-7 buckets so promoted teams can later be
    percentiled against this same distribution.
    """
    try:
        from fpl_cli.services.team_ratings import TeamRatingsCalculator

        calculator = TeamRatingsCalculator(client)
        _, performances = await calculator.calculate_from_xg(season=prev_season)
        return performances if len(performances) >= 10 else None

    except Exception:  # noqa: BLE001 — graceful degradation
        logger.warning("Failed to generate prior from Understat", exc_info=True)
        return None


async def _prior_from_football_data(prev_season: int) -> dict[str, TeamPerformance] | None:
    """Per-game scored/conceded rates from football-data.org PL results."""
    try:
        from fpl_cli.api.football_data import FootballDataClient

        async with FootballDataClient() as fd:
            if not fd.is_configured:
                return None
            matches = await fd.get_matches(competition="PL", season=prev_season)

        if not matches:
            return None

        tlas = {m["home_team_tla"] for m in matches} | {m["away_team_tla"] for m in matches}
        return _matches_to_performances(matches, tlas)

    except Exception:  # noqa: BLE001 — graceful degradation
        logger.warning("Failed to generate prior from football-data.org", exc_info=True)
        return None


def _default_rating() -> TeamRating:
    """Neutral mid-table rating for a team no source covers.

    A fresh instance per call: TeamRating is mutable and _apply_overrides
    assigns onto it, so a shared one would leak an override across teams.
    """
    return TeamRating(4, 4, 4, 4)


def _promoted_fallback() -> TeamRating:
    """Undifferentiated bottom-of-table estimate for a promoted team.

    A fresh instance per call, for the same reason as :func:`_default_rating`.
    """
    return TeamRating(5, 6, 5, 6)


async def _championship_performances(
    promoted_teams: set[str], prev_season: int
) -> dict[str, TeamPerformance] | None:
    """Per-game rates for promoted teams, rescaled onto the Premier League.

    Championship rates are converted to PL-equivalent ones here so the caller
    can rank a promoted side against the division it is joining rather than the
    one it just left. Ranking within the Championship hands its champion a
    rating of 1 -- nominally the best team in the Premier League -- because
    percentile bucketing is purely ordinal, so a uniform scale factor applied
    beforehand cannot shift the result at all.

    Returns None when Championship data is unavailable, leaving the caller to
    fall back to an undifferentiated estimate.
    """
    try:
        from fpl_cli.api.football_data import FootballDataClient

        async with FootballDataClient() as fd:
            if not fd.is_configured:
                logger.info(
                    "FOOTBALL_DATA_API_KEY not set - promoted teams fall back to a flat estimate"
                )
                return None
            matches = await fd.get_matches(competition="ELC", season=prev_season)

        if not matches:
            return None

        tlas = {m["home_team_tla"] for m in matches} | {m["away_team_tla"] for m in matches}
        championship = _matches_to_performances(matches, tlas)

        result: dict[str, TeamPerformance] = {}
        for team in promoted_teams:
            # _matches_to_performances already keys by FPL short name (it maps
            # TLAs through TLA_TO_FPL), so promoted teams are looked up directly.
            perf = championship.get(team)
            if perf is None:
                continue
            result[team] = TeamPerformance(
                team=team,
                goals_scored_home=perf.goals_scored_home * CHAMPIONSHIP_GOALS_SCORED_FACTOR,
                goals_scored_away=perf.goals_scored_away * CHAMPIONSHIP_GOALS_SCORED_FACTOR,
                goals_conceded_home=perf.goals_conceded_home * CHAMPIONSHIP_GOALS_CONCEDED_FACTOR,
                goals_conceded_away=perf.goals_conceded_away * CHAMPIONSHIP_GOALS_CONCEDED_FACTOR,
                home_games=perf.home_games,
                away_games=perf.away_games,
            )

        return result or None

    except Exception:  # noqa: BLE001 — graceful degradation
        logger.warning("Failed to fetch Championship data for promoted teams", exc_info=True)
        return None


def blend_with_prior(
    prior: dict[str, TeamRating],
    current: dict[str, TeamRating],
    current_gw: int,
) -> dict[str, TeamRating]:
    """Blend prior and current ratings using Bayesian shrinkage.

    current_weight = gw / (gw + REGRESSION_CONSTANT)
    Hard cutoff at BLENDING_CUTOFF_GW: returns current ratings unmodified.
    """
    if current_gw >= BLENDING_CUTOFF_GW:
        return current

    current_weight = current_gw / (current_gw + REGRESSION_CONSTANT)
    prior_weight = 1 - current_weight

    blended: dict[str, TeamRating] = {}
    all_teams = set(prior) | set(current)

    for team in all_teams:
        p = prior.get(team, _default_rating())
        c = current.get(team, p)

        blended[team] = TeamRating(
            atk_home=round(prior_weight * p.atk_home + current_weight * c.atk_home),
            atk_away=round(prior_weight * p.atk_away + current_weight * c.atk_away),
            def_home=round(prior_weight * p.def_home + current_weight * c.def_home),
            def_away=round(prior_weight * p.def_away + current_weight * c.def_away),
        )

    return blended
