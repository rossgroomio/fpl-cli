"""Team ratings service with auto-refresh and staleness detection.

Provides 4-axis team ratings (attacking/defensive x home/away) on a 1-7 scale.
Auto-refreshes from FPL fixture results when a new gameweek completes.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from statistics import mean
from typing import ClassVar

import yaml

from fpl_cli.paths import user_config_file, user_data_file
from fpl_cli.season import get_season_year, season_label
from fpl_cli.utils.files import atomic_write_text
from fpl_cli.utils.teams import describe_team_set_mismatch

logger = logging.getLogger(__name__)


def overrides_path() -> Path:
    """Manual per-team override file."""
    return user_config_file("team_ratings_overrides.yaml")


def default_ratings_path() -> Path:
    """Default ratings file."""
    return user_data_file("team_ratings.yaml")


PRESEASON_SOURCE = "preseason_prior"
"""Marks ratings derived entirely from last season, with no current-season results behind them.

Written pre-season and again in the gap after GW1 kicks off but before its
results can rate anyone.
"""

FDR_MODE_GLOSS: dict[str, str] = {
    "difference": "opponent strength at the venue, blended with the team's own",
    "opponent": "opponent strength at the venue only",
}
"""One-line reading of each ``get_positional_fdr`` mode, for the footers of FDR tables."""


_FDR_SCALE = "FDR scale: 1 (easiest) - 7 (hardest)"
"""Opening clause of every FDR footer. One copy, so no renderer can state a different scale."""

_FDR_UNRATED_NOTE = "Fixtures involving an unrated club score a neutral 4.0."
"""Closing clause of every FDR footer, true on the general figure and both positional ones."""


def fdr_columns_footer(mode: str) -> str:
    """Footer for a table showing FDR beside ATK and DEF, all scored in ``mode``.

    One sentence shared by every renderer (terminal, saved report, inline
    fallback) so they describe the columns identically. It holds
    unconditionally: the general FDR is always the ATK/DEF mean, and a fixture
    involving an unrated club scores the neutral 4.0 on all three.
    """
    gloss = FDR_MODE_GLOSS.get(mode, mode)
    return (
        f"{_FDR_SCALE}. All three columns use {mode} mode ({gloss}); "
        "FDR is the mean of ATK and DEF. ATK = for attackers, DEF = for defenders/GKs. "
        f"{_FDR_UNRATED_NOTE}"
    )


def fdr_scale_footer(mode: str) -> str:
    """Footer for a table showing the general FDR alone, without the ATK/DEF pair.

    The `fdr_columns_footer` sentence describes columns `fpl fixtures` does not
    show, but the figure is the same one, so the reading it needs is the same
    minus the column glossary: which mode, and what an unrated club scores.
    """
    gloss = FDR_MODE_GLOSS.get(mode, mode)
    return (
        f"{_FDR_SCALE}, in {mode} mode ({gloss}); "
        "each figure is the mean of the attacking and defensive difficulty of the "
        f"fixture for that team. {_FDR_UNRATED_NOTE}"
    )


def general_fdr(positional_fdr: dict[str, float]) -> float:
    """General FDR for one fixture: the mean of its ATK and DEF positional FDRs.

    Takes the pair rather than the fixture so a caller that has already scored
    the positional columns derives the general figure from the very numbers it
    is about to display, instead of re-deriving them. Those two routes round in
    different orders -- `get_positional_fdr_pair` rounds each column to 1dp,
    this rounds their mean to 2 -- and a re-derivation from unrounded values
    lands elsewhere the moment a rating axis is not a whole number, which the
    primary `team_ratings.yaml` load path does not enforce. The FDR column
    would then disagree with the ATK/DEF pair beside it, which is the split
    #202 exists to close.
    """
    return round((positional_fdr["ATK"] + positional_fdr["DEF"]) / 2, 2)


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


@dataclass
class RatingsMetadata:
    """Metadata about the ratings."""

    last_updated: datetime | None
    source: str | None  # "auto_calculated", "calculated", "understat_xg"
    staleness_threshold_days: int
    based_on_gws: tuple[int, int] | None
    calculation_method: str | None  # "full_season", "recent_form"
    season: str | None = None  # e.g. "2026-27"; None on files written before the stamp


def _empty_metadata() -> RatingsMetadata:
    """Metadata for the no-usable-ratings state (file missing, or wrong season)."""
    return RatingsMetadata(
        last_updated=None,
        source=None,
        staleness_threshold_days=30,
        based_on_gws=None,
        calculation_method=None,
        season=None,
    )


def _season_of(declared: object, last_updated: datetime | None) -> str | None:
    """The season a ratings file describes.

    Prefers the explicit ``metadata.season`` stamp, falling back to whichever
    season ``last_updated`` lands in so files written before the stamp existed
    still invalidate themselves at the July cutover. None means the file says
    neither, which is treated as "cannot tell" rather than "stale".
    """
    if declared:
        return str(declared)
    if last_updated:
        return season_label(get_season_year(last_updated.date()))
    return None


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
        self._stale_season: str | None = None
        self._team_set_warning: str | None = None

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

        path = self.config_path
        if not path.exists():
            self._metadata = _empty_metadata()
            return

        with open(path, encoding="utf-8") as f:
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

        file_season = _season_of(meta.get("season"), last_updated)
        if file_season and file_season != season_label():
            # A file from a previous season describes a different league: it
            # still rates the relegated clubs and knows nothing about the
            # promoted ones. Serving those numbers is worse than serving none,
            # and keeping its based_on_gws would also convince ensure_fresh
            # that GW3 of the new season is covered by GW35 of the old one.
            logger.info(
                "Team ratings stale (season %s != %s) - ignoring",
                file_season,
                season_label(),
            )
            self._stale_season = file_season
            self._metadata = _empty_metadata()
            return

        self._metadata = RatingsMetadata(
            last_updated=last_updated,
            source=meta.get("source"),
            staleness_threshold_days=meta.get("staleness_threshold_days", 30),
            based_on_gws=based_on_gws,
            calculation_method=meta.get("calculation_method"),
            season=file_season,
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
        """Refresh ratings from FPL fixture data if stale, then check the team set.

        Compares the latest completed GW against based_on_gws metadata.
        On failure, keeps stale data and logs a warning.

        The team-set check runs whether or not a refresh happened: a file that
        is fresh by date can still describe last season's twenty clubs, and
        that is the mismatch a date can never catch.
        """
        if not TeamRatingsService._refreshed_this_session:
            try:
                await self._refresh(client)
            except Exception:  # noqa: BLE001 — graceful degradation
                logger.warning("Auto-refresh failed, using stale ratings", exc_info=True)

        try:
            teams = await client.get_teams()
            self.check_team_set(team.short_name for team in teams)
        except Exception:  # noqa: BLE001 — a drift warning must never break a command
            logger.debug("Team-set check skipped", exc_info=True)

    async def _refresh(self, client) -> None:
        """Recalculate ratings when completed gameweeks have moved past the file."""
        self._ensure_loaded()
        next_gw = await client.get_next_gameweek()
        if not next_gw:
            return

        max_completed_gw = next_gw["id"] - 1
        if max_completed_gw < 1:
            # Only mark the session refreshed on success: a failed attempt
            # (no prior available, e.g. a transient outage) must be retried
            # by the next ensure_fresh() call rather than locked out for the
            # rest of the process.
            if await self.seed_from_prior(client):
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

            blended = False
            if max_completed_gw < BLENDING_CUTOFF_GW:
                prior = await generate_prior(client)
                if prior:
                    ratings = blend_with_prior(prior, ratings, max_completed_gw)
                    blended = True

            # Tagged the same way `fpl ratings update` tags it. A blended file
            # is mostly last season early on, and get_staleness_warning() reads
            # this to say so -- an untagged one would present a GW1 blend as
            # ordinary current-season form.
            self.save_ratings(
                ratings,
                source="auto_calculated_blended" if blended else "auto_calculated",
                based_on_gws=(min_gw, max_completed_gw),
                calculation_method="recent_form_blended" if blended else "recent_form",
            )
            self._apply_overrides()
            TeamRatingsService._refreshed_this_session = True
        elif not self._ratings or await self._team_set_drifts(client):
            # A gameweek is under way but has produced nothing to rate teams on
            # yet — every fixture in the window is still in flight. The single
            # finished gameweek that used to land here is now rated (#138), so
            # this is the no-results-at-all case. The pre-season branch above has
            # already closed (next_gw moved on at GW1 kickoff), so without this
            # the function returns having done nothing and, with no usable file
            # on disk, get_positional_fdr serves a neutral 4.0 to every caller.
            # The previous-season prior is available the whole time and is a
            # strictly better answer than uniform difficulty. As above, only
            # mark refreshed on success so a transient failure can be retried.
            #
            # A file that rates last season's clubs counts as unusable too: it
            # is non-empty, so an emptiness test alone leaves the promoted
            # sides unrated (a neutral 4.0 each) for as long as the new season
            # produces no ratable results, which is exactly the rollover window
            # this branch exists to cover.
            if await self.seed_from_prior(client):
                TeamRatingsService._refreshed_this_session = True
        else:
            TeamRatingsService._refreshed_this_session = True

    async def _team_set_drifts(self, client) -> bool:
        """Whether the rated clubs no longer match the live league.

        Wraps check_team_set for the refresh path, where a lookup failure must
        read as "no drift known" rather than aborting the refresh.
        """
        try:
            teams = await client.get_teams()
        except Exception:  # noqa: BLE001 — a drift check must never break a refresh
            logger.debug("Team-set drift check skipped", exc_info=True)
            return False
        return self.check_team_set(team.short_name for team in teams) is not None

    def check_team_set(self, current_teams: Iterable[str]) -> str | None:
        """Compare the rated clubs against the live league and record any drift.

        A ratings file rebuilt in early August passes every date check while
        still rating three relegated clubs and knowing nothing about the three
        promoted ones -- get_rating returns None for those, so every fixture
        they are in scores a neutral 4.0 without saying that three teams are
        being handled differently from the other seventeen. Diffing the keys
        against bootstrap-static is what catches the rollover.

        Args:
            current_teams: Team short names in the league right now

        Returns:
            The warning message, also surfaced by get_staleness_warning(),
            or None when the sets agree.
        """
        self._ensure_loaded()
        self._team_set_warning = None

        if not self._ratings:
            return None

        mismatch = describe_team_set_mismatch(
            self.config_path.name, self._ratings, current_teams, verb="rates"
        )
        if mismatch:
            self._team_set_warning = f"⚠️ {mismatch} - run `fpl ratings update`"
        return self._team_set_warning

    async def seed_from_prior(self, client) -> bool:
        """Rebuild ratings from last season when the current one cannot rate teams.

        Pre-season there are no results to rate teams on, and whatever sits in
        team_ratings.yaml is last season's table: it still carries relegated
        sides and knows nothing about the promoted ones, which then fall
        through to the neutral 4.0 in get_positional_fdr. On a fresh install
        the file is absent entirely and every team gets that 4.0 — uniform
        difficulty, presented as analysis.

        The same hole reopens once GW1 kicks off but before any fixture in it
        finishes: results exist in principle but not yet in fact, so the
        current-season calculation returns nothing while the pre-season branch
        has already closed. Both windows are served from here.

        The previous-season prior (Understat xG, with Championship-adjusted
        ratings for promoted teams) is the better source, so use it and tag it
        so callers can label the output as an estimate.

        Returns:
            True if ratings were written, False if no prior was available.
        """
        from fpl_cli.services.team_ratings_prior import generate_prior

        prior = await generate_prior(client)
        if not prior:
            return False

        self.save_ratings(
            prior,
            source=PRESEASON_SOURCE,
            calculation_method="preseason_prior",
        )
        self._apply_overrides()
        return True

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
                "season": season_label(),
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
        atomic_write_text(
            self.config_path,
            header + yaml.dump(data, default_flow_style=False, sort_keys=False),
        )

        # Keep in-memory state current (don't discard and reload)
        self._ratings = dict(ratings)
        self._metadata = RatingsMetadata(
            last_updated=datetime.now(),
            source=source,
            staleness_threshold_days=30,
            based_on_gws=based_on_gws,
            calculation_method=calculation_method,
            season=season_label(),
        )
        self._loaded = True
        # Whatever was wrong with the old file has just been replaced.
        self._stale_season = None
        self._team_set_warning = None

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

    def get_positional_fdr_pair(
        self,
        team: str,
        opponent: str,
        venue: str,
        mode: str = "difference",
    ) -> dict[str, float]:
        """ATK (FWD/MID) and DEF (DEF/GK) FDR for one fixture, each rounded to 1dp.

        The single rounding boundary for a fixture's difficulty: every FDR a
        user sees is built from this pair, so the general figure beside the two
        columns is the mean of exactly the numbers printed, whatever the
        underlying axes are.

        Args:
            team: Short name of the team whose fixture this is (e.g. "LIV")
            opponent: Opponent team short name (e.g. "ARS")
            venue: "home" or "away", from *team*'s point of view
            mode: "difference" (default) or "opponent"
        """
        return {
            # ATK: for FWD/MID, off the opponent's defensive weakness
            "ATK": round(self.get_positional_fdr("FWD", team, opponent, venue, mode), 1),
            # DEF: for DEF/GK, off the opponent's attacking threat
            "DEF": round(self.get_positional_fdr("DEF", team, opponent, venue, mode), 1),
        }

    def get_fixture_fdr(
        self,
        team: str,
        opponent: str,
        venue: str,
        mode: str = "difference",
    ) -> float:
        """General FDR for one fixture: the mean of its ATK and DEF positional FDRs.

        The one definition of the general figure, so a fixture is scored once
        in the codebase and every surface showing an FDR for it -- `fpl fdr`,
        `fpl preview`, `fpl fixtures` -- prints the same number (#202). It is
        venue-aware and, in difference mode, blends the team's own strength
        with the opponent's, exactly like the positional pair it averages.

        It replaced `TeamRating.avg_overall_fdr`, the opponent's mean across
        all four axes: that had no input from the team whose fixture it was
        and no venue, so it answered "how strong is the opponent" where the
        column asks "how hard is this fixture" (#186).

        An unrated club scores the neutral 4.0 `get_positional_fdr` returns,
        not the FPL API's `home_difficulty`/`away_difficulty`: that fallback
        put one club on a 1-5 scale inside a 1-7 column.

        For a caller that already holds the positional pair, `general_fdr()`
        takes it directly -- this is that call with the pair fetched first, so
        the two cannot disagree.

        Args:
            team: Short name of the team whose fixture this is (e.g. "LIV")
            opponent: Opponent team short name (e.g. "ARS")
            venue: "home" or "away", from *team*'s point of view
            mode: "difference" (default) or "opponent"

        Returns:
            FDR value (1-7 scale, lower = easier fixture)
        """
        return general_fdr(self.get_positional_fdr_pair(team, opponent, venue, mode))

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
        """Whether ratings are last-season estimates rather than current-season form.

        True pre-season and in the gap after GW1 kicks off, before completed
        results can rate anyone.
        """
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

    def _prior_dominance(self) -> tuple[int, float] | None:
        """Window length and its blend weight, when last season still outweighs it.

        A blended file built on one gameweek is 86% previous season, but it is
        stamped with a real `based_on_gws` and a calculated source, so nothing
        else on the staleness path treats it as an estimate. Between GW1
        finishing and the sample reaching REGRESSION_CONSTANT gameweeks, the
        ratings are named after current-season results while being mostly the
        prior -- so say which.

        Returns None once current form carries at least half the weight, and
        for any file that was never blended.

        Detection is by the `_blended` source tag, which means a file written
        before that tag existed reads as unblended and stays silent here even
        when it is prior-dominated. That is a one-gameweek gap, not a lasting
        one: the next completed gameweek moves `based_on_gws` on, the refresh
        rewrites the file with the tag, and the warning starts firing. Inferring
        a blend from the window alone instead would claim "mostly last season's
        prior" over files that never had a prior blended into them (none was
        available), which is a worse failure than staying quiet for a week.
        """
        from fpl_cli.services.team_ratings_prior import REGRESSION_CONSTANT

        if not self._metadata or not (self._metadata.source or "").endswith("_blended"):
            return None
        if not self._metadata.based_on_gws:
            return None

        min_gw, max_gw = self._metadata.based_on_gws
        window = max_gw - min_gw + 1
        if window <= 0 or window >= REGRESSION_CONSTANT:
            return None
        return window, window / (window + REGRESSION_CONSTANT)

    def _prior_dominance_warning(self) -> str | None:
        """The prior-dominance note, or None when it does not apply."""
        share = self._prior_dominance()
        if not share:
            return None
        window, weight = share
        gws = "gameweek" if window == 1 else "gameweeks"
        return (
            f"⚠️ Ratings are mostly last season's prior — {window} {gws} of results "
            f"carries {weight:.0%} of the weight. Fixture difficulty is indicative "
            f"until more results land."
        )

    def advisory_warning(self) -> str | None:
        """The active warning, when it describes healthy ratings rather than a fault.

        An early-season blend that is still mostly last season is the correct
        answer, not a problem: no command clears it and GW`REGRESSION_CONSTANT`
        will. Callers that triage rather than just display -- `fpl doctor` --
        need to tell that note apart from a stale or drifted file, or they
        report a fault with no remedy, which is the dead-end #138 was about.

        Derived from get_staleness_warning() rather than from _prior_dominance()
        directly, so precedence is honoured in one place: a prior-dominated file
        that has ALSO drifted off the current team set reports the drift, and
        that is a real fault, so this returns None for it.
        """
        warning = self.get_staleness_warning()
        if warning and warning == self._prior_dominance_warning():
            return warning
        return None

    def get_staleness_warning(self) -> str | None:
        """Get a warning about the quality of the ratings backing fixture difficulty.

        Covers the cases where difficulty would otherwise be presented as
        ordinary analysis while resting on nothing: ratings from a previous
        season, no ratings at all, ratings describing a different set of clubs
        (see check_team_set), ratings that fail to separate any two teams, and
        pre-season estimates.

        Returns:
            Warning message, or None if ratings are usable and fresh
        """
        self._ensure_loaded()

        if self._stale_season:
            return (
                f"⚠️ Team ratings are from {self._stale_season}, not {season_label()} - "
                "they describe a different league, so they were ignored and every "
                "fixture will score a neutral 4.0. Run `fpl ratings update`."
            )

        if not self.has_ratings:
            return (
                "⚠️ No team ratings available - every fixture will score a neutral 4.0. "
                "Run `fpl ratings update` to seed estimates from last season's prior."
            )

        if self._team_set_warning:
            return self._team_set_warning

        if self.is_uniform:
            return (
                "⚠️ Team ratings do not separate any two teams - fixture difficulty is "
                "meaningless until real results land. Run `fpl ratings update`."
            )

        if self.is_preseason_estimate:
            return (
                "⚠️ Ratings are estimated from last season (promoted teams from "
                "Championship form) — no current-season results to rate teams on yet. "
                "Fixture difficulty is indicative until results land."
            )

        advisory = self._prior_dominance_warning()
        if advisory:
            return advisory

        days = self.days_since_update()

        if days < 0:
            return "⚠️ Team ratings have no last_updated date - run `fpl ratings update`"

        if self.is_stale():
            return f"⚠️ Team ratings are {days} days old - consider running `fpl ratings update`"

        return None


# The four rate axes a TeamPerformance carries, each paired with the axis that
# measures the same thing at the other venue.
_OTHER_VENUE: dict[str, str] = {
    "scored_home": "scored_away",
    "scored_away": "scored_home",
    "conceded_home": "conceded_away",
    "conceded_away": "conceded_home",
}


def performances_from_samples(
    samples: dict[str, dict[str, list[float]]],
) -> dict[str, TeamPerformance]:
    """Turn per-match samples into the per-venue rates ratings are built from.

    ``samples`` maps a team to four lists of per-match values, keyed
    ``scored_home`` / ``scored_away`` / ``conceded_home`` / ``conceded_away``.

    A team that has played only one venue is still rated: the venue it has not
    played is estimated from the one it has, rescaled by the gap between the two
    venues across the whole sample. Requiring both venues instead is what made
    `fpl ratings update` report nothing to calculate from once GW1 finished
    (#138) — every club has played exactly one match at that point, so every
    club is missing a venue and the entire league falls out of the result. The
    same hole reopens for any single-gameweek window, e.g. `--since-gw 15` on
    the day GW15 completes.

    home_games/away_games stay as observed, so an estimated axis is visible as a
    0 in the counts rather than presented as a played record.
    """
    # League-wide baseline per axis. Home and away goals per match are the only
    # two figures needed: a team's scoring at home and its opponents' conceding
    # away are the same goals, so the four axes share two baselines.
    home_goals = [v for data in samples.values() for v in data["scored_home"]]
    away_goals = [v for data in samples.values() for v in data["scored_away"]]
    home_rate = mean(home_goals) if home_goals else 0.0
    away_rate = mean(away_goals) if away_goals else 0.0
    baseline = {
        "scored_home": home_rate,
        "scored_away": away_rate,
        "conceded_home": away_rate,
        "conceded_away": home_rate,
    }

    performances: dict[str, TeamPerformance] = {}
    for team, data in samples.items():
        home_games = len(data["scored_home"])
        away_games = len(data["scored_away"])
        if home_games == 0 and away_games == 0:
            continue

        rates: dict[str, float] = {}
        for axis, other in _OTHER_VENUE.items():
            if data[axis]:
                rates[axis] = mean(data[axis])
            elif baseline[other]:
                # Same team, other venue, moved onto this venue's level.
                rates[axis] = mean(data[other]) * baseline[axis] / baseline[other]
            else:
                # No conversion ratio to measure: the counterpart venue
                # produced nothing across the entire window. That collapses to
                # zero rather than to an unscaled copy of the played venue --
                # this team's counterpart values are members of the very pool
                # whose mean is baseline[other], and goals and xG are both
                # non-negative, so a zero pooled mean means every one of them
                # is zero. (A pool assembled per-team rather than per-match can
                # in principle break that correspondence -- calculate_from_xg
                # skips a club Understat has no data for -- so this is written
                # as an explicit zero rather than left to the arithmetic.)
                rates[axis] = 0.0

        performances[team] = TeamPerformance(
            team=team,
            goals_scored_home=rates["scored_home"],
            goals_scored_away=rates["scored_away"],
            goals_conceded_home=rates["conceded_home"],
            goals_conceded_away=rates["conceded_away"],
            home_games=home_games,
            away_games=away_games,
        )

    return performances


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

        # Calculate per-game averages, estimating a venue a team has yet to play
        performances = performances_from_samples(stats)

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
        from fpl_cli.api.understat import UnderstatClient

        teams = await self.fpl.get_teams()
        async with UnderstatClient() as understat:
            raw: dict[str, dict[str, list[float]]] = {}

            for team in teams:
                data = await understat.get_team(team.name, season=season)
                if not data:
                    continue

                # Keyed on the shared axis names so performances_from_samples
                # can treat xG exactly as it treats goals.
                team_stats: dict[str, list[float]] = {
                    "scored_home": [],
                    "scored_away": [],
                    "conceded_home": [],
                    "conceded_away": [],
                }

                for match in data["matches"]:
                    if not match.get("isResult"):
                        continue

                    side = match.get("side")
                    xg = match.get("xG", {})

                    if side == "h":
                        team_stats["scored_home"].append(float(xg.get("h", 0)))
                        team_stats["conceded_home"].append(float(xg.get("a", 0)))
                    elif side == "a":
                        team_stats["scored_away"].append(float(xg.get("a", 0)))
                        team_stats["conceded_away"].append(float(xg.get("h", 0)))

                if any(team_stats.values()):
                    raw[team.short_name] = team_stats

        performances = performances_from_samples(raw)

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
