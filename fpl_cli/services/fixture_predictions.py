"""Service for reading blank/double gameweek predictions."""

from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass
from datetime import date
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Any, TypedDict

import yaml

from fpl_cli.paths import SHIPPED_CONFIG_DIR, user_config_file
from fpl_cli.season import get_season_year

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from fpl_cli.models.fixture import Fixture
    from fpl_cli.models.team import Team

CONFIG_FILENAME = "fixture_predictions.yaml"
CONFIG_FILE = SHIPPED_CONFIG_DIR / CONFIG_FILENAME


class Confidence(str, Enum):
    """Confidence level for predictions."""

    CONFIRMED = "confirmed"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


@dataclass
class BlankPrediction:
    """A predicted blank gameweek for specific teams."""

    gameweek: int
    teams: list[str]
    confidence: Confidence

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> BlankPrediction:
        """Create from dictionary, tolerating legacy keys (status, source, reason)."""
        return cls(
            gameweek=data["gameweek"],
            teams=data["teams"],
            confidence=Confidence(data.get("confidence", "medium")),
        )


@dataclass
class DoublePrediction:
    """A predicted double gameweek for specific teams."""

    gameweek: int
    teams: list[str]
    confidence: Confidence

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> DoublePrediction:
        """Create from dictionary, tolerating legacy keys (status, source, reason)."""
        return cls(
            gameweek=data["gameweek"],
            teams=data["teams"],
            confidence=Confidence(data.get("confidence", "medium")),
        )


class FixturePredictionsService:
    """Read-only service for blank/double gameweek predictions from YAML.

    Without an explicit config_path, a fixture_predictions.yaml in the user
    config dir takes precedence over the copy shipped in the package, so
    predictions can be updated mid-season without a package release. A user
    copy that is unreadable, malformed, empty, or from a previous season falls
    through to the shipped copy, and the reason is recorded in
    :attr:`load_warnings` for the CLI to surface.

    An explicitly supplied config_path is not layered: read errors propagate,
    because a caller that named a file wants to know it could not be read.
    """

    def __init__(self, config_path: Path | None = None):
        # No filesystem work here: user_config_dir() resolves on first load so
        # constructing the service stays total, and an FPL_CLI_CONFIG_DIR set
        # after import is still honoured.
        self._explicit_path = Path(config_path) if config_path is not None else None
        self._config_path: Path | None = None
        self._data: dict[str, Any] | None = None
        self._stale: bool = False
        self._warnings: list[str] = []

    def _candidates(self) -> list[Path]:
        if self._explicit_path is not None:
            return [self._explicit_path]
        return [user_config_file(CONFIG_FILENAME), CONFIG_FILE]

    def _read(self, candidate: Path) -> dict[str, Any] | None:
        """Parse one candidate, or None when it is unusable (reason recorded).

        Read failures propagate for an explicitly supplied path -- there is no
        second candidate to fall through to, so degrading silently would hide
        the problem from the caller.
        """
        try:
            with open(candidate, encoding="utf-8") as f:
                data = yaml.safe_load(f)
        except (OSError, yaml.YAMLError) as exc:
            if self._explicit_path is not None:
                raise
            self._warn(f"Ignoring unreadable predictions file {candidate}: {exc}")
            return None

        if data is None:
            data = {}
        if not isinstance(data, dict):
            self._warn(
                f"Ignoring predictions file {candidate}: expected a mapping,"
                f" got {type(data).__name__}"
            )
            return None
        return data

    def _warn(self, message: str) -> None:
        """Record a skip reason for :attr:`load_warnings` to surface.

        Deliberately not logger.warning: fpl-cli configures no logging
        handlers, so a warning record reaches logging's lastResort handler and
        prints raw to stderr on top of whatever the CLI already displayed.
        """
        self._warnings.append(message)
        logger.debug("%s", message)

    def _load(self) -> dict[str, Any]:
        """Load predictions from the first usable, current-season candidate."""
        if self._data is not None:
            return self._data

        candidates = self._candidates()
        saw_stale = False
        for index, candidate in enumerate(candidates):
            if not candidate.exists():
                continue

            data = self._read(candidate)
            if data is None:
                continue

            # Suppress stale predictions from a previous season. Falling
            # through to the shipped copy must not be silent for a user
            # override -- the file's owner needs to know it was ignored. The
            # final candidate needs no warning here: with nothing to fall
            # through to, the is_stale banner reports it instead.
            if self._is_stale(data):
                saw_stale = True
                if index < len(candidates) - 1:
                    self._warn(
                        f"Ignoring predictions file {candidate}:"
                        " its metadata is from a previous season"
                    )
                continue

            # An empty or half-written file must not mask a later candidate. With
            # no later candidate an empty file is simply "no predictions yet".
            has_predictions = bool(data.get("predicted_blanks") or data.get("predicted_doubles"))
            if not has_predictions and index < len(candidates) - 1:
                self._warn(f"Ignoring predictions file {candidate}: it holds no predictions")
                continue

            self._config_path = candidate
            self._data = data
            return data

        self._stale = saw_stale
        self._data = self._empty_data()
        return self._data

    @property
    def config_path(self) -> Path | None:
        """File the predictions came from, or None when no candidate was usable."""
        self._load()
        return self._config_path

    @property
    def load_warnings(self) -> list[str]:
        """Reasons candidate files were skipped (unreadable, malformed, empty, stale)."""
        self._load()
        return list(self._warnings)

    @staticmethod
    def _empty_data() -> dict[str, Any]:
        return {
            "metadata": {"last_updated": "", "notes": ""},
            "predicted_blanks": [],
            "predicted_doubles": [],
        }

    @staticmethod
    def _is_stale(data: dict[str, Any]) -> bool:
        """Check if predictions are from a previous season."""
        metadata = data.get("metadata")
        last_updated = metadata.get("last_updated", "") if isinstance(metadata, dict) else ""
        if not last_updated:
            return False
        try:
            updated_date = date.fromisoformat(str(last_updated))
        except ValueError:
            return False
        return get_season_year(updated_date) < get_season_year()

    @property
    def is_stale(self) -> bool:
        """Whether predictions are from a previous season (for CLI warning)."""
        self._load()  # populates self._stale as side effect
        return self._stale

    def get_predicted_blanks(
        self, gw: int | None = None, *, min_gw: int | None = None,
    ) -> list[BlankPrediction]:
        """Get predicted blank gameweeks.

        Args:
            gw: Filter to exact gameweek, or None for all.
            min_gw: Exclude predictions before this gameweek.
        """
        data = self._load()
        predictions = [BlankPrediction.from_dict(b) for b in data.get("predicted_blanks") or []]

        if gw is not None:
            predictions = [p for p in predictions if p.gameweek == gw]
        if min_gw is not None:
            predictions = [p for p in predictions if p.gameweek >= min_gw]

        return sorted(predictions, key=lambda p: p.gameweek)

    def get_predicted_doubles(
        self, gw: int | None = None, *, min_gw: int | None = None,
    ) -> list[DoublePrediction]:
        """Get predicted double gameweeks.

        Args:
            gw: Filter to exact gameweek, or None for all.
            min_gw: Exclude predictions before this gameweek.
        """
        data = self._load()
        predictions = [DoublePrediction.from_dict(d) for d in data.get("predicted_doubles") or []]

        if gw is not None:
            predictions = [p for p in predictions if p.gameweek == gw]
        if min_gw is not None:
            predictions = [p for p in predictions if p.gameweek >= min_gw]

        return sorted(predictions, key=lambda p: p.gameweek)

    def get_metadata(self) -> dict[str, Any]:
        """Get metadata about predictions."""
        data = self._load()
        metadata = data.get("metadata")
        return metadata if isinstance(metadata, dict) else {}


# -- Extracted detection functions (pure, no agent dependency) --


class BlankTeamInfo(TypedDict):
    team_id: int
    team_name: str
    short_name: str


class DoubleTeamInfo(TypedDict):
    team_id: int
    team_name: str
    short_name: str
    fixtures: int


def find_blank_gameweeks(
    fixtures_by_gw: dict[int, list[Fixture]],
    teams: list[Team],
    start_gw: int,
    end_gw: int,
) -> dict[int, list[BlankTeamInfo]]:
    """Find teams with blank gameweeks (not playing).

    Args:
        fixtures_by_gw: Fixtures grouped by gameweek.
        teams: Team objects with id, name, short_name.
        start_gw: First gameweek to check.
        end_gw: Last gameweek to check (inclusive).

    Returns:
        Dict mapping GW number to list of team info dicts.
    """
    blank_gws: dict[int, list[BlankTeamInfo]] = {}

    for gw in range(start_gw, end_gw + 1):
        gw_fixtures = fixtures_by_gw.get(gw, [])
        teams_playing: set[int] = set()

        for f in gw_fixtures:
            teams_playing.add(f.home_team_id)
            teams_playing.add(f.away_team_id)

        teams_not_playing: list[BlankTeamInfo] = [
            {"team_id": t.id, "team_name": t.name, "short_name": t.short_name}
            for t in teams
            if t.id not in teams_playing
        ]

        if teams_not_playing:
            blank_gws[gw] = teams_not_playing

    return blank_gws


def find_double_gameweeks(
    fixtures_by_gw: dict[int, list[Fixture]],
    teams: list[Team],
    start_gw: int | None = None,
    end_gw: int | None = None,
) -> dict[int, list[DoubleTeamInfo]]:
    """Find teams with double gameweeks (playing twice).

    Args:
        fixtures_by_gw: Fixtures grouped by gameweek.
        teams: Team objects with id, name, short_name.
        start_gw: First gameweek to check (inclusive). None = no lower bound.
        end_gw: Last gameweek to check (inclusive). None = no upper bound.

    Returns:
        Dict mapping GW number to list of team info dicts.
    """
    double_gws: dict[int, list[DoubleTeamInfo]] = {}
    team_map = {t.id: t for t in teams}

    for gw, fixtures in fixtures_by_gw.items():
        if start_gw is not None and gw < start_gw:
            continue
        if end_gw is not None and gw > end_gw:
            continue
        team_fixture_count: dict[int, int] = defaultdict(int)

        for f in fixtures:
            team_fixture_count[f.home_team_id] += 1
            team_fixture_count[f.away_team_id] += 1

        teams_with_doubles: list[DoubleTeamInfo] = [
            {
                "team_id": tid,
                "team_name": team_map[tid].name,
                "short_name": team_map[tid].short_name,
                "fixtures": count,
            }
            for tid, count in team_fixture_count.items()
            if count > 1
        ]

        if teams_with_doubles:
            double_gws[gw] = teams_with_doubles

    return double_gws


# -- Prediction lookup for matchup scoring --

PredictionLookup = dict[int, dict[int, tuple[str, float]]]
"""gw -> team_id -> (prediction_type, confidence_multiplier)."""

CONFIDENCE_MULTIPLIERS: dict[Confidence, float] = {
    Confidence.CONFIRMED: 1.0,
    Confidence.HIGH: 0.8,
    Confidence.MEDIUM: 0.5,
    Confidence.LOW: 0.25,
}


def build_prediction_lookup(
    service: FixturePredictionsService,
    team_map: dict[int, Any],
    min_gw: int,
) -> PredictionLookup:
    """Build a gw -> team_id -> (prediction_type, confidence_multiplier) lookup.

    Resolves short_name team identifiers from the YAML to team IDs using
    *team_map* (team_id -> Team model).  Returns an empty dict when the
    service has no data (missing / stale / empty YAML), satisfying R9
    graceful fallback.

    Conflict rules:
    - If a team appears in both blanks and doubles for the same GW,
      double takes precedence (more informative signal).
    - If a team appears in multiple entries of the same type for the
      same GW, the highest confidence wins.
    """
    short_to_id: dict[str, int] = {
        t.short_name: tid for tid, t in team_map.items()
    }

    blanks = service.get_predicted_blanks(min_gw=min_gw)
    doubles = service.get_predicted_doubles(min_gw=min_gw)

    if not blanks and not doubles:
        return {}

    lookup: PredictionLookup = {}

    # Process blanks first so doubles can overwrite (precedence rule).
    for pred in blanks:
        multiplier = CONFIDENCE_MULTIPLIERS[pred.confidence]
        gw_entry = lookup.setdefault(pred.gameweek, {})
        for short_name in pred.teams:
            tid = short_to_id.get(short_name)
            if tid is None:
                logger.warning(
                    "Prediction team %s (GW%d) not in team_map, skipping",
                    short_name, pred.gameweek,
                )
                continue
            existing = gw_entry.get(tid)
            if existing is not None and existing[0] == "blank" and existing[1] >= multiplier:
                continue  # Keep higher confidence
            gw_entry[tid] = ("blank", multiplier)

    for pred in doubles:
        multiplier = CONFIDENCE_MULTIPLIERS[pred.confidence]
        gw_entry = lookup.setdefault(pred.gameweek, {})
        for short_name in pred.teams:
            tid = short_to_id.get(short_name)
            if tid is None:
                logger.warning(
                    "Prediction team %s (GW%d) not in team_map, skipping",
                    short_name, pred.gameweek,
                )
                continue
            existing = gw_entry.get(tid)
            # Doubles always override blanks; within doubles keep highest confidence
            if existing is not None and existing[0] == "double" and existing[1] >= multiplier:
                continue
            gw_entry[tid] = ("double", multiplier)

    return lookup
