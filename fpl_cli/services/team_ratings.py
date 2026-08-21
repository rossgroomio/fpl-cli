"""Team ratings service with auto-refresh and staleness detection.

Provides 4-axis team ratings (attacking/defensive x home/away) on a 1-7 scale.
Auto-refreshes from FPL fixture results when a new gameweek completes.
"""

from __future__ import annotations

import logging
import os
import tempfile
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from statistics import mean
from typing import ClassVar

import yaml

from fpl_cli.paths import user_config_dir, user_data_dir

logger = logging.getLogger(__name__)


def overrides_path() -> Path:
    """Manual per-team override file. Resolved per call so FPL_CLI_CONFIG_DIR is honoured."""
    return user_config_dir() / "team_ratings_overrides.yaml"


def default_ratings_path() -> Path:
    """Default ratings file. Resolved per call so FPL_CLI_DATA_DIR is honoured."""
    return user_data_dir() / "team_ratings.yaml"


PRESEASON_SOURCE = "preseason_prior"
"""Marks ratings derived entirely from last season, before any GW has been played."""


@dataclass
class TeamRating:
    """4-axis rating for a single team."""

    atk_home: int
    atk_away: int
    def_home: int
    def_away: int

    @property
    def avg_atk(self) -> float:
        """Average attacking rating."""
        return (self.atk_home + self.atk_away) / 2

    @property
    def avg_defensive(self) -> float:
        """Average defensive rating."""
        return (self.def_home + self.def_away) / 2

    @property
    def avg_overall(self) -> float:
        """Overall average rating (1=best, 7=worst)."""
        return (self.atk_home + self.atk_away + self.def_home + self.def_away) / 4

    @property
    def avg_overall_fdr(self) -> float:
        """Overall FDR (1=easy, 7=hard). Inverts avg_overall for fixture difficulty."""
        return 8 - self.avg_overall


@dataclass
class RatingsMetadata:
    """Metadata about the ratings."""

    last_updated: datetime | None
    source: str | None  # "auto_calculated", "calculated", "understat_xg"
    staleness_threshold_days: int
    based_on_gws: tuple[int, int] | None
    calculation_method: str | None  # "full_season", "recent_form"


@dataclass
class TeamPerformance:
    """Raw performance stats for rating calculation."""

    team: str
    goals_scored_home: float
    goals_scored_away: float
    goals_conceded_home: float
    goals_conceded_away: float
    home_games: int
    away_games: int


class TeamRatingsService:
    """Service for accessing and managing team ratings.

    Ratings are on a 1-7 scale (1 = best, 7 = worst). Auto-refreshes from FPL
    fixture results when stale. Manual overrides from team_ratings_overrides.yaml
    are applied in-memory only (never written to the main file).

    Usage:
        service = TeamRatingsService()
        await service.ensure_fresh(client)  # async contexts
        rating = service.get_rating("LIV")
    """

    _refreshed_this_session: ClassVar[bool] = False

    def __init__(self, config_path: Path | str | None = None):
        # Left unresolved when not supplied so the default follows FPL_CLI_DATA_DIR
        # even if it is set after this module is imported.
        self._config_path = Path(config_path) if config_path else None
        self._ratings: dict[str, TeamRating] = {}
        self._metadata: RatingsMetadata | None = None
        self._loaded = False

    @property
    def config_path(self) -> Path:
        """Path of the ratings YAML file this service reads and writes."""
        return self._config_path if self._config_path is not None else default_ratings_path()

    def _ensure_loaded(self) -> None:
        """Load ratings if not already loaded."""
        if not self._loaded:
            self._load_ratings()

    def _load_ratings(self) -> None:
        """Load ratings from YAML config."""
        self._loaded = True

        if not self.config_path.exists():
            self._metadata = RatingsMetadata(
                last_updated=None,
                source=None,
                staleness_threshold_days=30,
                based_on_gws=None,
                calculation_method=None,
            )
            return

        with open(self.config_path, encoding="utf-8") as f:
            data = yaml.safe_load(f)

        # Parse metadata
        meta = data.get("metadata", {})
        last_updated = meta.get("last_updated")
        if last_updated and isinstance(last_updated, str):
            last_updated = datetime.fromisoformat(last_updated)
        elif isinstance(last_updated, datetime):
            pass  # Already a datetime
        else:
            last_updated = None

        based_on_gws = meta.get("based_on_gws")
        if based_on_gws and isinstance(based_on_gws, list) and len(based_on_gws) == 2:
            based_on_gws = tuple(based_on_gws)
        else:
            based_on_gws = None

        self._metadata = RatingsMetadata(
            last_updated=last_updated,
            source=meta.get("source"),
            staleness_threshold_days=meta.get("staleness_threshold_days", 30),
            based_on_gws=based_on_gws,
            calculation_method=meta.get("calculation_method"),
        )

        # Parse ratings
        ratings_data = data.get("ratings", {})
        for team, rating in ratings_data.items():
            self._ratings[team] = TeamRating(
                atk_home=rating.get("atk_home", 4),
                atk_away=rating.get("atk_away", 4),
                def_home=rating.get("def_home", 4),
                def_away=rating.get("def_away", 4),
            )

        self._apply_overrides()

    def _apply_overrides(self) -> None:
        """Merge overrides from team_ratings_overrides.yaml into in-memory ratings."""
        overrides_file = overrides_path()
        if not overrides_file.exists():
            return

        with open(overrides_file, encoding="utf-8") as f:
            overrides = yaml.safe_load(f)

        if not overrides or not isinstance(overrides, dict):
            return

        valid_axes = {"atk_home", "atk_away", "def_home", "def_away"}
        for team, axes in overrides.items():
            if team not in self._ratings:
                logger.warning("Override for unknown team: %s", team)
                continue
            if not isinstance(axes, dict):
                continue
            rating = self._ratings[team]
            for axis, value in axes.items():
                if axis not in valid_axes:
                    logger.warning("Override for unknown axis: %s.%s", team, axis)
                    continue
                if not isinstance(value, int) or not (1 <= value <= 7):
                    logger.warning("Override must be int 1-7, got %r for %s.%s", value, team, axis)
                    continue
                setattr(rating, axis, value)

    async def ensure_fresh(self, client) -> None:
        """Refresh ratings from FPL fixture data if stale.

        Compares the latest completed GW against based_on_gws metadata.
        On failure, keeps stale data and logs a warning.
        """
        if TeamRatingsService._refreshed_this_session:
            return

        try:
            self._ensure_loaded()
            next_gw = await client.get_next_gameweek()
            if not next_gw:
                return

            max_completed_gw = next_gw["id"] - 1
            if max_completed_gw < 1:
                await self._seed_from_prior(client)
                TeamRatingsService._refreshed_this_session = True
                return

            # Check staleness against metadata
            if self._metadata and self._metadata.based_on_gws:
                if max_completed_gw <= self._metadata.based_on_gws[1]:
                    TeamRatingsService._refreshed_this_session = True
                    return

            # Recalculate from recent fixtures
            calculator = TeamRatingsCalculator(client)
            min_gw = max(1, max_completed_gw - 11)
            ratings, _ = await calculator.calculate_from_fixtures(
                min_gw=min_gw, max_gw=max_completed_gw
            )

            if ratings:
                # Blend with prior in early season
                from fpl_cli.services.team_ratings_prior import (
                    BLENDING_CUTOFF_GW,
                    blend_with_prior,
                    generate_prior,
                )

                if max_completed_gw < BLENDING_CUTOFF_GW:
                    prior = await generate_prior(client)
                    ratings = blend_with_prior(prior, ratings, max_completed_gw)

                self.save_ratings(
                    ratings,
                    source="auto_calculated",
                    based_on_gws=(min_gw, max_completed_gw),
                    calculation_method="recent_form",
                )
                self._apply_overrides()

            TeamRatingsService._refreshed_this_session = True

        except Exception:  # noqa: BLE001 — graceful degradation
            logger.warning("Auto-refresh failed, using stale ratings", exc_info=True)

    async def _seed_from_prior(self, client) -> None:
        """Rebuild ratings from last season when no gameweek has been played yet.

        Pre-season there are no results to rate teams on, and whatever sits in
        team_ratings.yaml is last season's table: it still carries relegated
        sides and knows nothing about the promoted ones, which then fall
        through to the neutral 4.0 in get_positional_fdr. On a fresh install
        the file is absent entirely and every team gets that 4.0 — uniform
        difficulty, presented as analysis.

        The previous-season prior (Understat xG, with Championship-adjusted
        ratings for promoted teams) is the better source, so use it and tag it
        so callers can label the output as an estimate.
        """
        from fpl_cli.services.team_ratings_prior import generate_prior

        prior = await generate_prior(client)
        if not prior:
            return

        self.save_ratings(
            prior,
            source=PRESEASON_SOURCE,
            calculation_method="preseason_prior",
        )
        self._apply_overrides()

    def save_ratings(
        self,
        ratings: dict[str, TeamRating],
        source: str,
        based_on_gws: tuple[int, int] | None = None,
        calculation_method: str | None = None,
    ) -> None:
        """Save ratings to YAML config.

        Args:
            ratings: Dict mapping team short name to TeamRating
            source: Source of ratings ("calculated", "manual", etc.)
            based_on_gws: Tuple of (start_gw, end_gw) if calculated
            calculation_method: Method used ("full_season", "recent_form")
        """
        data = {
            "metadata": {
                "last_updated": datetime.now().strftime("%Y-%m-%d"),
                "source": source,
                "staleness_threshold_days": 30,
                "based_on_gws": list(based_on_gws) if based_on_gws else None,
                "calculation_method": calculation_method,
            },
            "ratings": {},
        }

        for team in sorted(ratings.keys()):
            r = ratings[team]
            data["ratings"][team] = {
                "atk_home": r.atk_home,
                "atk_away": r.atk_away,
                "def_home": r.def_home,
                "def_away": r.def_away,
            }

        # Write with header comment
        header = """# Team Ratings Configuration
# Scale: 1 (best) to 7 (worst)
#
# Attacking ratings: Higher goals scored = lower (better) rating
# Defensive ratings: Fewer goals conceded = lower (better) rating
#
# Used for position-specific FDR calculations:
# - FWD/MID: Use opponent's defensive rating (attacking opportunity)
# - DEF/GK: Use opponent's offensive rating (clean sheet likelihood)

"""
        # Atomic write: tempfile + os.replace
        target = self.config_path
        dir_path = target.parent
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=dir_path, suffix=".yaml", delete=False
        ) as f:
            f.write(header)
            yaml.dump(data, f, default_flow_style=False, sort_keys=False)
            tmp_path = f.name
        os.replace(tmp_path, target)

        # Keep in-memory state current (don't discard and reload)
        self._ratings = dict(ratings)
        self._metadata = RatingsMetadata(
            last_updated=datetime.now(),
            source=source,
            staleness_threshold_days=30,
            based_on_gws=based_on_gws,
            calculation_method=calculation_method,
        )
        self._loaded = True

    def get_rating(self, team_short: str) -> TeamRating | None:
        """Get rating for a team.

        Args:
            team_short: Team short name (e.g., "ARS", "LIV")

        Returns:
            TeamRating or None if team not found
        """
        self._ensure_loaded()
        return self._ratings.get(team_short.upper())

    def get_all_ratings(self) -> dict[str, TeamRating]:
        """Get all team ratings."""
        self._ensure_loaded()
        return self._ratings.copy()

    def get_positional_fdr(
        self,
        position: str,
        team: str,
        opponent: str,
        venue: str,
        mode: str = "difference",
    ) -> float:
        """Calculate position-specific FDR.

        Args:
            position: Player position ("FWD", "MID", "DEF", "GK")
            team: Player's team short name
            opponent: Opponent team short name
            venue: "home" or "away"
            mode: "difference" (Ben Crellin's preferred) or "opponent"

        Returns:
            FDR value (lower = easier fixture)
        """
        self._ensure_loaded()

        team_rating = self._ratings.get(team.upper())
        opp_rating = self._ratings.get(opponent.upper())

        if not team_rating or not opp_rating:
            return 4.0  # Default to average

        opp_venue = "away" if venue == "home" else "home"

        if position.upper() in ["FWD", "MID"]:
            # Attackers care about opponent's defensive weakness
            opp_def = opp_rating.def_away if opp_venue == "away" else opp_rating.def_home
            # Invert opponent axis: rating 1 (best defence) → 7 (hardest for attacker)
            opp_fdr = 8 - opp_def
            if mode == "difference":
                team_off = team_rating.atk_home if venue == "home" else team_rating.atk_away
                return (opp_fdr + team_off) / 2
            return float(opp_fdr)
        else:
            # Defenders/GKs care about opponent's attacking threat
            opp_off = opp_rating.atk_away if opp_venue == "away" else opp_rating.atk_home
            # Invert opponent axis: rating 1 (best attack) → 7 (hardest for defender)
            opp_fdr = 8 - opp_off
            if mode == "difference":
                team_def = team_rating.def_home if venue == "home" else team_rating.def_away
                return (opp_fdr + team_def) / 2
            return float(opp_fdr)

    @property
    def metadata(self) -> RatingsMetadata | None:
        """Get ratings metadata."""
        self._ensure_loaded()
        return self._metadata

    @property
    def teams(self) -> list[str]:
        """Get list of teams with ratings."""
        self._ensure_loaded()
        return list(self._ratings.keys())

    @property
    def is_preseason_estimate(self) -> bool:
        """Whether ratings are last-season estimates rather than current-season form."""
        self._ensure_loaded()
        return bool(self._metadata and self._metadata.source == PRESEASON_SOURCE)

    @property
    def has_ratings(self) -> bool:
        """Whether any ratings are loaded at all.

        False means every get_positional_fdr call returns the neutral 4.0.
        """
        self._ensure_loaded()
        return bool(self._ratings)

    @property
    def is_uniform(self) -> bool:
        """Whether every team carries identical ratings.

        A degenerate rating set produces the same fixture difficulty for all
        20 teams while looking like ordinary output, so it needs surfacing
        rather than silently ranking teams that were never differentiated.
        """
        self._ensure_loaded()
        if len(self._ratings) < 2:
            return False
        axes = {
            (r.atk_home, r.atk_away, r.def_home, r.def_away) for r in self._ratings.values()
        }
        return len(axes) == 1

    def is_stale(self) -> bool:
        """Check if ratings are stale (older than threshold)."""
        self._ensure_loaded()

        if not self._metadata or not self._metadata.last_updated:
            return True

        threshold = timedelta(days=self._metadata.staleness_threshold_days)
        return datetime.now() - self._metadata.last_updated > threshold

    def days_since_update(self) -> int:
        """Get number of days since last update."""
        self._ensure_loaded()

        if not self._metadata or not self._metadata.last_updated:
            return -1

        return (datetime.now() - self._metadata.last_updated).days

    def get_staleness_warning(self) -> str | None:
        """Get a warning about the quality of the ratings backing fixture difficulty.

        Covers the cases where difficulty would otherwise be presented as
        ordinary analysis while resting on nothing: no ratings at all, ratings
        that fail to separate any two teams, and pre-season estimates.

        Returns:
            Warning message, or None if ratings are usable and fresh
        """
        if not self.has_ratings:
            return (
                "⚠️ No team ratings available - every fixture will score a neutral 4.0. "
                "Run `fpl ratings update` once results exist."
            )

        if self.is_uniform:
            return (
                "⚠️ Team ratings do not separate any two teams - fixture difficulty is "
                "meaningless until real results land. Run `fpl ratings update`."
            )

        if self.is_preseason_estimate:
            return (
                "⚠️ Pre-season: ratings are estimated from last season (promoted teams "
                "from Championship form). Fixture difficulty is indicative until GW1."
            )

        days = self.days_since_update()

        if days < 0:
            return "⚠️ Team ratings have no last_updated date - run `fpl ratings update`"

        if self.is_stale():
            return f"⚠️ Team ratings are {days} days old - consider running `fpl ratings update`"

        return None


class TeamRatingsCalculator:
    """Calculate team ratings from fixture results.

    Uses goals scored/conceded at home and away to derive ratings
    on a 1-7 scale using percentile-based bucketing.
    """

    def __init__(self, fpl_client):
        """Initialize calculator.

        Args:
            fpl_client: FPLClient instance for fetching fixture data
        """
        self.fpl = fpl_client

    async def calculate_from_fixtures(
        self,
        min_gw: int = 1,
        max_gw: int | None = None,
    ) -> tuple[dict[str, TeamRating], dict[str, TeamPerformance]]:
        """Calculate ratings from completed fixture results.

        Args:
            min_gw: Starting gameweek (inclusive)
            max_gw: Ending gameweek (inclusive), None for all completed

        Returns:
            Tuple of (ratings dict, performance stats dict)
        """
        fixtures = await self.fpl.get_fixtures()
        teams = await self.fpl.get_teams()
        team_map = {t.id: t.short_name for t in teams}

        # Determine max_gw from completed fixtures if not specified
        completed = [f for f in fixtures if f.finished and f.gameweek and f.gameweek >= min_gw]
        if not completed:
            return {}, {}

        if max_gw is None:
            max_gw = max(f.gameweek for f in completed)

        completed = [f for f in completed if f.gameweek <= max_gw]

        # Aggregate stats per team
        stats: dict[str, dict] = {
            abbr: {
                "scored_home": [],
                "scored_away": [],
                "conceded_home": [],
                "conceded_away": [],
            }
            for abbr in team_map.values()
        }

        for fixture in completed:
            home_team = team_map.get(fixture.home_team_id)
            away_team = team_map.get(fixture.away_team_id)

            if not home_team or not away_team:
                continue

            home_goals = fixture.home_score or 0
            away_goals = fixture.away_score or 0

            stats[home_team]["scored_home"].append(home_goals)
            stats[home_team]["conceded_home"].append(away_goals)
            stats[away_team]["scored_away"].append(away_goals)
            stats[away_team]["conceded_away"].append(home_goals)

        # Calculate per-game averages
        performances: dict[str, TeamPerformance] = {}
        for team, data in stats.items():
            home_games = len(data["scored_home"])
            away_games = len(data["scored_away"])

            if home_games == 0 or away_games == 0:
                continue

            performances[team] = TeamPerformance(
                team=team,
                goals_scored_home=mean(data["scored_home"]) if data["scored_home"] else 0,
                goals_scored_away=mean(data["scored_away"]) if data["scored_away"] else 0,
                goals_conceded_home=mean(data["conceded_home"]) if data["conceded_home"] else 0,
                goals_conceded_away=mean(data["conceded_away"]) if data["conceded_away"] else 0,
                home_games=home_games,
                away_games=away_games,
            )

        # Convert to 1-7 ratings
        ratings = self._convert_to_ratings(performances)

        return ratings, performances

    async def calculate_from_xg(
        self,
        season: str | None = None,
    ) -> tuple[dict[str, TeamRating], dict[str, TeamPerformance]]:
        """Calculate ratings from Understat xG data.

        Fetches match-level xG from Understat for every current FPL team.
        GW filtering is not available via Understat.

        Args:
            season: Understat season year (e.g. "2024" for 2024/25). None for current.

        Returns:
            Tuple of (ratings dict, performance stats dict).
            Performance stats hold xG/xGA values in the goals_scored/conceded fields.
        """
        from statistics import mean

        from fpl_cli.api.understat import UnderstatClient

        teams = await self.fpl.get_teams()
        async with UnderstatClient() as understat:
            raw: dict[str, dict[str, list[float]]] = {}

            for team in teams:
                data = await understat.get_team(team.name, season=season)
                if not data:
                    continue

                team_stats: dict[str, list[float]] = {
                    "xg_home": [],
                    "xg_away": [],
                    "xga_home": [],
                    "xga_away": [],
                }

                for match in data["matches"]:
                    if not match.get("isResult"):
                        continue

                    side = match.get("side")
                    xg = match.get("xG", {})

                    if side == "h":
                        team_stats["xg_home"].append(float(xg.get("h", 0)))
                        team_stats["xga_home"].append(float(xg.get("a", 0)))
                    elif side == "a":
                        team_stats["xg_away"].append(float(xg.get("a", 0)))
                        team_stats["xga_away"].append(float(xg.get("h", 0)))

                if team_stats["xg_home"] and team_stats["xg_away"]:
                    raw[team.short_name] = team_stats

        performances: dict[str, TeamPerformance] = {}
        for abbr, data in raw.items():
            home_games = len(data["xg_home"])
            away_games = len(data["xg_away"])

            if home_games == 0 or away_games == 0:
                continue

            performances[abbr] = TeamPerformance(
                team=abbr,
                goals_scored_home=mean(data["xg_home"]),
                goals_scored_away=mean(data["xg_away"]),
                goals_conceded_home=mean(data["xga_home"]),
                goals_conceded_away=mean(data["xga_away"]),
                home_games=home_games,
                away_games=away_games,
            )

        ratings = self._convert_to_ratings(performances)
        return ratings, performances

    @staticmethod
    def _convert_to_ratings(
        performances: dict[str, TeamPerformance],
    ) -> dict[str, TeamRating]:
        """Convert raw stats to 1-7 scale ratings.

        Offensive: More goals = better (lower rating number)
        Defensive: Fewer goals conceded = better (lower rating number)

        Uses percentile-based bucketing across all teams.
        """
        if not performances:
            return {}

        # Collect all values for each metric
        metrics = {
            "atk_home": [p.goals_scored_home for p in performances.values()],
            "atk_away": [p.goals_scored_away for p in performances.values()],
            "def_home": [p.goals_conceded_home for p in performances.values()],
            "def_away": [p.goals_conceded_away for p in performances.values()],
        }

        ratings = {}
        to_rating = TeamRatingsCalculator._to_rating
        for team, perf in performances.items():
            ratings[team] = TeamRating(
                atk_home=to_rating(
                    perf.goals_scored_home, metrics["atk_home"], higher_is_better=True
                ),
                atk_away=to_rating(
                    perf.goals_scored_away, metrics["atk_away"], higher_is_better=True
                ),
                def_home=to_rating(
                    perf.goals_conceded_home, metrics["def_home"], higher_is_better=False
                ),
                def_away=to_rating(
                    perf.goals_conceded_away, metrics["def_away"], higher_is_better=False
                ),
            )

        return ratings

    @staticmethod
    def _to_rating(
        value: float,
        all_values: list[float],
        higher_is_better: bool,
    ) -> int:
        """Convert a value to 1-7 rating based on percentile.

        Args:
            value: The value to convert
            all_values: All values in the dataset for comparison
            higher_is_better: True for goals scored, False for goals conceded

        Returns:
            Rating from 1 (best) to 7 (worst)
        """
        if not all_values:
            return 4  # Default to average

        # Sort values
        if higher_is_better:
            sorted_vals = sorted(all_values, reverse=True)  # Highest first
        else:
            sorted_vals = sorted(all_values)  # Lowest first

        n = len(sorted_vals)

        # Find position (0-indexed)
        # Handle ties by finding first occurrence
        try:
            position = sorted_vals.index(value)
        except ValueError:
            # Value not in list (shouldn't happen), find closest
            position = n // 2

        # Calculate percentile (0 = best, 1 = worst)
        percentile = position / max(n - 1, 1)

        # Map to 1-7 scale
        # 0-14% = 1, 14-29% = 2, 29-43% = 3, 43-57% = 4, 57-71% = 5, 71-86% = 6, 86-100% = 7
        boundaries = [0.143, 0.286, 0.429, 0.571, 0.714, 0.857]
        for i, boundary in enumerate(boundaries):
            if percentile < boundary:
                return i + 1
        return 7
