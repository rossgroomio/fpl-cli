"""Prior generation and blending for early-season team ratings.

Uses previous season data to smooth ratings when current-season sample
is small. Bayesian shrinkage with REGRESSION_CONSTANT=6 and hard cutoff
at GW12.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from pathlib import Path
from statistics import mean, pstdev
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
# 3: the tla join fixes (NOT -> NFO via TLA_TO_FPL, per-side exclusion on a
# colliding tla), plus the removal of the uniform 4.0 fallback. A v2 cache is
# the exact shape those changes exist to stop being used -- Forest rated as a
# promoted side, two Sheffield clubs pooled, or a flat table saved under
# `prior_default`. All three key on the current team names, so the team-set
# check below sees zero mismatches and would serve them for the rest of the
# season.
# 4: promoted sides are damped onto the PL spread rather than scaled by a flat
# factor. A v3 cache is where the division's best defence is saved as a top-3
# Premier League one -- the exact rating this change exists to stop producing.
PRIOR_CACHE_VERSION = 4

# Championship-to-PL adjustment, level. A promoted side scores less in the PL
# against better defences, and concedes more against better attacks, so the two
# directions move opposite ways: applying one factor to both (or to raw match
# scores, where one team's goal is another's concession) deflates conceded when
# it should inflate it.
#
# These set where the promoted cohort's *mean* sits, and nothing else. They used
# to multiply each team's own rates, which scaled the cohort's spread by the same
# amount as its level -- so the conceded axes came out ~1.5x wider than the PL
# distribution the teams were about to be ranked in, and the division's best and
# worst defences both landed outside the PL's real range (#111).
CHAMPIONSHIP_GOALS_SCORED_FACTOR = 0.665
CHAMPIONSHIP_GOALS_CONCEDED_FACTOR = 1 / CHAMPIONSHIP_GOALS_SCORED_FACTOR

# Championship-to-PL adjustment, spread: how much of a team's edge over its own
# division survives promotion. Each side is standardised within the Championship
# and re-expressed in Premier League standard deviations scaled by this, so the
# cohort can only ever occupy this fraction of the PL's spread.
#
# 1.0 asserts a side that was 2 sd clear of the Championship will be 2 sd clear
# of the Premier League; 0.0 collapses every promoted team onto identical rates,
# i.e. _promoted_fallback() with a source of truth. At 0.6 promoted sides sit in
# the bottom third with their ordering intact -- which is where they finish --
# while a genuinely good one can still reach a mid-table bucket. Held at the low
# end of the defensible range because the pool mixes Understat xG (continuing
# teams) with actual goals (promoted, no Championship xG exists), and actual
# goals carry more noise per game than xG does.
CHAMPIONSHIP_EDGE_RETENTION = 0.6

# The four TeamPerformance rate axes with the level factor each takes.
CHAMPIONSHIP_AXES: tuple[tuple[str, float], ...] = (
    ("goals_scored_home", CHAMPIONSHIP_GOALS_SCORED_FACTOR),
    ("goals_scored_away", CHAMPIONSHIP_GOALS_SCORED_FACTOR),
    ("goals_conceded_home", CHAMPIONSHIP_GOALS_CONCEDED_FACTOR),
    ("goals_conceded_away", CHAMPIONSHIP_GOALS_CONCEDED_FACTOR),
)

# football-data.org TLA to FPL short name (only where they differ).
# Most TLAs match directly; add exceptions here as discovered.
TLA_TO_FPL: dict[str, str] = {"NOT": "NFO"}


def _tla_collisions(matches: list[dict[str, Any]]) -> dict[str, set[int]]:
    """Map each football-data tla to the distinct team ids seen under it.

    football-data's own tla is not a unique key -- the 2025-26 Championship
    serves both Sheffield clubs as "SHE" (#110). A tla backed by more than
    one id in this batch cannot be safely joined to a single FPL team, so
    the caller must exclude it rather than pool two clubs' results together.
    Ids are opaque here -- only distinctness matters, not their values -- so
    this needs no football-data id -> FPL mapping to detect the collision.
    """
    ids_by_tla: dict[str, set[int]] = {}
    for m in matches:
        for tla_key, id_key in (("home_team_tla", "home_team_id"), ("away_team_tla", "away_team_id")):
            tla = m[tla_key]
            team_id = m.get(id_key)
            if team_id is not None:
                ids_by_tla.setdefault(tla, set()).add(team_id)
    return {tla: ids for tla, ids in ids_by_tla.items() if len(ids) > 1}


def _empty_bucket() -> dict[str, list[float]]:
    return {"scored_home": [], "scored_away": [], "conceded_home": [], "conceded_away": []}


def _matches_to_performances(matches: list[dict[str, Any]]) -> dict[str, TeamPerformance]:
    """Aggregate match results into per-game scored/conceded rates by venue.

    A match whose opponent has an ambiguous tla (see _tla_collisions) still
    counts for the unambiguous side -- only the ambiguous team's own record
    is withheld, rather than discarding the whole fixture.
    """
    collisions = _tla_collisions(matches)
    if collisions:
        logger.warning(
            "football-data tla is ambiguous for %s (multiple team ids share it) - "
            "excluding from prior generation rather than pooling different clubs' results",
            ", ".join(f"{tla} (ids {sorted(ids)})" for tla, ids in sorted(collisions.items())),
        )

    # Aggregate per-team stats
    stats: dict[str, dict[str, list[float]]] = {}
    for m in matches:
        home_tla: str = m["home_team_tla"]
        away_tla: str = m["away_team_tla"]

        if home_tla not in collisions:
            home = TLA_TO_FPL.get(home_tla, home_tla)
            bucket = stats.setdefault(home, _empty_bucket())
            bucket["scored_home"].append(m["home_score"])
            bucket["conceded_home"].append(m["away_score"])

        if away_tla not in collisions:
            away = TLA_TO_FPL.get(away_tla, away_tla)
            bucket = stats.setdefault(away, _empty_bucket())
            bucket["scored_away"].append(m["away_score"])
            bucket["conceded_away"].append(m["home_score"])

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
    # Understat path the pool mixes xG rates with rescaled actual goals, which are
    # noisier per game; that extra variance is one of the reasons the promoted
    # cohort's spread is damped rather than trusted — see CHAMPIONSHIP_EDGE_RETENTION.)
    # A promoted team the Championship data doesn't cover gets the flat estimate
    # individually, so partial coverage never promotes it to mid-table by omission.
    promoted = current_team_names - set(performances)
    if promoted:
        logger.info(
            "No %s performance record for %s - treating as promoted and looking up "
            "Championship data (a continuing team lands here if it fails to join, e.g. "
            "a naming mismatch, rather than because it was actually promoted)",
            source,
            ", ".join(sorted(promoted)),
        )
    promoted_performances = (
        await _championship_performances(promoted, prev_season_int, performances) or {}
        if promoted
        else {}
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

        return _matches_to_performances(matches)

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


def _axis_spread(performances: Iterable[TeamPerformance], axis: str) -> tuple[float, float]:
    """Mean and standard deviation of one rate axis across a set of teams.

    Population rather than sample sd: a division's teams are the whole
    population of that division, not a draw from a larger one. It also degrades
    where sample sd raises -- a single team gives 0.0 spread, which the callers
    below already handle as "no spread to work with".
    """
    values = [getattr(p, axis) for p in performances]
    if not values:
        return 0.0, 0.0
    return mean(values), pstdev(values)


def _rescale_to_pl(
    championship: dict[str, TeamPerformance],
    pl_performances: dict[str, TeamPerformance],
    teams: set[str],
) -> dict[str, TeamPerformance]:
    """Re-express Championship rates in Premier League units.

    Level and spread are set separately, because they are separate claims:

        z  = (x - elc_mean) / elc_sd
        x' = elc_mean * factor + k * z * pl_sd

    The level term carries "promoted teams are worse than the league they are
    joining" and comes from the factors alone, so it is unchanged from when
    those factors were applied to each team directly. The spread term carries
    "this much of a Championship edge survives promotion", which a single
    multiplicative factor cannot express at all: multiplying scales spread and
    level together, so pushing the cohort's conceded mean up 50% also spread it
    50% wider than the distribution it was about to be ranked against.

    `championship` supplies the distribution and must be the whole division,
    not just `teams` -- a promoted side's edge is only meaningful against the
    teams it beat. `pl_performances` supplies the units and must be the pool
    the result will be bucketed in.

    Either distribution collapsing (one team, or every team identical on an
    axis) leaves no spread to carry, so every promoted side lands on the level
    term. That is the flat estimate, which is the right answer when the data
    holds no evidence of a difference between them.
    """
    rescaled: dict[str, dict[str, float]] = {team: {} for team in teams}
    for axis, factor in CHAMPIONSHIP_AXES:
        elc_mean, elc_sd = _axis_spread(championship.values(), axis)
        _, pl_sd = _axis_spread(pl_performances.values(), axis)
        target_level = elc_mean * factor
        for team, values in rescaled.items():
            z = (getattr(championship[team], axis) - elc_mean) / elc_sd if elc_sd else 0.0
            # A per-game goal rate below zero is not a thing, however far out a
            # team sits. Bucketing is ordinal so this only ever ties teams that
            # are already both off the bottom of the scale.
            values[axis] = max(0.0, target_level + CHAMPIONSHIP_EDGE_RETENTION * z * pl_sd)

    return {
        team: TeamPerformance(
            team=team,
            home_games=championship[team].home_games,
            away_games=championship[team].away_games,
            **values,
        )
        for team, values in rescaled.items()
    }


async def _championship_performances(
    promoted_teams: set[str],
    prev_season: int,
    pl_performances: dict[str, TeamPerformance],
) -> dict[str, TeamPerformance] | None:
    """Per-game rates for promoted teams, re-expressed in Premier League units.

    Championship rates are converted to PL-equivalent ones here so the caller
    can rank a promoted side against the division it is joining rather than the
    one it just left. Ranking within the Championship hands its champion a
    rating of 1 -- nominally the best team in the Premier League -- because
    percentile bucketing is purely ordinal, so a uniform scale factor applied
    beforehand cannot shift the result at all.

    `pl_performances` is the pool the caller will bucket the result against; it
    sets the units the promoted cohort's spread is expressed in. See
    :func:`_rescale_to_pl`.

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

        championship = _matches_to_performances(matches)

        # _matches_to_performances already keys by FPL short name (it maps TLAs
        # through TLA_TO_FPL), so promoted teams are looked up directly.
        covered = {team for team in promoted_teams if team in championship}
        for team in sorted(promoted_teams - covered):
            logger.warning(
                "No Championship performance record for promoted team %s - "
                "falling back to the undifferentiated bottom-of-table estimate",
                team,
            )
        if not covered:
            return None

        return _rescale_to_pl(championship, pl_performances, covered) or None

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
