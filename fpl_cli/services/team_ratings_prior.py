"""Prior generation and blending for early-season team ratings.

Uses previous season data to smooth ratings when current-season sample
is small. Bayesian shrinkage with REGRESSION_CONSTANT=6 and hard cutoff
at GW12.
"""

from __future__ import annotations

import logging
from statistics import mean
from typing import TYPE_CHECKING, Any

import yaml

from fpl_cli.paths import user_data_dir
from fpl_cli.services.team_ratings import TeamRating

if TYPE_CHECKING:
    from fpl_cli.api.fpl import FPLClient

logger = logging.getLogger(__name__)

PRIOR_CONFIG_PATH = user_data_dir() / "team_ratings_prior.yaml"
REGRESSION_CONSTANT = 6
BLENDING_CUTOFF_GW = 12

# Championship-to-PL adjustment: goals scored x0.665 (harder to score in PL)
CHAMPIONSHIP_GOALS_SCORED_FACTOR = 0.665

# football-data.org TLA to FPL short name (only where they differ).
# Most TLAs match directly; add exceptions here as discovered.
TLA_TO_FPL: dict[str, str] = {}


def _matches_to_ratings(
    matches: list[dict[str, Any]], team_tlas: set[str]
) -> dict[str, TeamRating]:
    """Convert match results to 1-7 ratings via percentile bucketing."""
    from fpl_cli.services.team_ratings import TeamPerformance, TeamRatingsCalculator

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

    # Use the existing percentile bucketing
    return TeamRatingsCalculator._convert_to_ratings(performances)


def _load_prior_cache() -> dict[str, TeamRating] | None:
    """Load cached prior from disk, or None if missing."""
    if not PRIOR_CONFIG_PATH.exists():
        return None
    with open(PRIOR_CONFIG_PATH, encoding="utf-8") as f:
        data = yaml.safe_load(f)
    if not data or "ratings" not in data:
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
    import os
    import tempfile

    data: dict[str, Any] = {
        "metadata": {"source": source, "teams": sorted(teams)},
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
    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=PRIOR_CONFIG_PATH.parent, suffix=".yaml", delete=False
    ) as f:
        yaml.dump(data, f, default_flow_style=False, sort_keys=False)
        tmp_path = f.name
    os.replace(tmp_path, PRIOR_CONFIG_PATH)


async def generate_prior(client: FPLClient) -> dict[str, TeamRating]:
    """Generate prior ratings from previous season data.

    Fallback chain: Understat xG/xGA -> football-data.org -> default 4.
    Promoted teams use Championship data with league adjustment.
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

    # Try Understat first
    prior = await _prior_from_understat(client, prev_season)
    source = "prior_understat_xg"

    if not prior:
        # Fallback to football-data.org
        prior = await _prior_from_football_data(current_team_names, prev_season_int)
        source = "prior_football_data"

    if not prior:
        # Ultimate fallback
        prior = {name: TeamRating(4, 4, 4, 4) for name in current_team_names}
        source = "prior_default"

    # Handle promoted teams
    promoted = current_team_names - set(prior.keys())
    if promoted:
        championship_prior = await _championship_prior(promoted, prev_season_int)
        prior.update(championship_prior)

    # Ensure all current teams are covered
    for name in current_team_names:
        if name not in prior:
            prior[name] = TeamRating(4, 4, 4, 4)

    _save_prior_cache(prior, source, list(current_team_names))
    return prior


async def _prior_from_understat(client: FPLClient, prev_season: str) -> dict[str, TeamRating] | None:
    """Generate prior from Understat xG/xGA data for previous season."""
    try:
        from fpl_cli.services.team_ratings import TeamRatingsCalculator

        calculator = TeamRatingsCalculator(client)
        ratings, _ = await calculator.calculate_from_xg(season=prev_season)
        return ratings if len(ratings) >= 10 else None

    except Exception:  # noqa: BLE001 — graceful degradation
        logger.warning("Failed to generate prior from Understat", exc_info=True)
        return None


async def _prior_from_football_data(
    team_names: set[str], prev_season: int
) -> dict[str, TeamRating] | None:
    """Generate prior from football-data.org match results."""
    try:
        from fpl_cli.api.football_data import FootballDataClient

        async with FootballDataClient() as fd:
            if not fd.is_configured:
                return None
            matches = await fd.get_matches(competition="PL", season=prev_season)

        if not matches:
            return None

        tlas = {m["home_team_tla"] for m in matches} | {m["away_team_tla"] for m in matches}
        return _matches_to_ratings(matches, tlas)

    except Exception:  # noqa: BLE001 — graceful degradation
        logger.warning("Failed to generate prior from football-data.org", exc_info=True)
        return None


async def _championship_prior(
    promoted_teams: set[str], prev_season: int
) -> dict[str, TeamRating]:
    """Generate ratings for promoted teams from Championship data."""
    try:
        from fpl_cli.api.football_data import FootballDataClient

        async with FootballDataClient() as fd:
            if not fd.is_configured:
                return {t: TeamRating(5, 6, 5, 6) for t in promoted_teams}
            matches = await fd.get_matches(competition="ELC", season=prev_season)

        if not matches:
            return {t: TeamRating(5, 6, 5, 6) for t in promoted_teams}

        # Apply league adjustment to match scores
        adjusted = []
        for m in matches:
            adjusted.append({
                **m,
                "home_score": m["home_score"] * CHAMPIONSHIP_GOALS_SCORED_FACTOR,
                "away_score": m["away_score"] * CHAMPIONSHIP_GOALS_SCORED_FACTOR,
            })
            # Also adjust conceded perspective (opponents score more in PL)
            # This is handled by the fact that one team's scored is another's conceded

        tlas = {m["home_team_tla"] for m in matches} | {m["away_team_tla"] for m in matches}
        all_ratings = _matches_to_ratings(adjusted, tlas)

        # Only return ratings for the promoted teams
        reverse_map = {v: k for k, v in TLA_TO_FPL.items()} if TLA_TO_FPL else {}
        result = {}
        for team in promoted_teams:
            tla = reverse_map.get(team, team)
            result[team] = all_ratings.get(tla) or all_ratings.get(team) or TeamRating(5, 6, 5, 6)

        return result

    except Exception:  # noqa: BLE001 — graceful degradation
        logger.warning("Failed to fetch Championship data for promoted teams", exc_info=True)
        return {t: TeamRating(5, 6, 5, 6) for t in promoted_teams}


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
        p = prior.get(team, TeamRating(4, 4, 4, 4))
        c = current.get(team, p)

        blended[team] = TeamRating(
            atk_home=round(prior_weight * p.atk_home + current_weight * c.atk_home),
            atk_away=round(prior_weight * p.atk_away + current_weight * c.atk_away),
            def_home=round(prior_weight * p.def_home + current_weight * c.def_home),
            def_away=round(prior_weight * p.def_away + current_weight * c.def_away),
        )

    return blended
