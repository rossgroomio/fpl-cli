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

from fpl_cli.paths import user_config_dir
from fpl_cli.season import get_season_year

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from fpl_cli.models.fixture import Fixture
    from fpl_cli.models.team import Team

CONFIG_FILE = user_config_dir() / "fixture_predictions.yaml"


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
    """Read-only service for blank/double gameweek predictions from YAML."""

    def __init__(self, config_path: Path | None = None):
        self.config_path = config_path or CONFIG_FILE
        self._data: dict[str, Any] | None = None
        self._stale: bool = False

    def _load(self) -> dict[str, Any]:
        """Load predictions from config file."""
        if self._data is not None:
            return self._data

        if not self.config_path.exists():
            self._data = self._empty_data()
            return self._data

        with open(self.config_path, encoding="utf-8") as f:
            data: dict[str, Any] = yaml.safe_load(f) or {}

        # Suppress stale predictions from a previous season.
        if self._is_stale(data):
            self._stale = True
            self._data = self._empty_data()
            return self._data

        self._data = data
        return data

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
        last_updated = data.get("metadata", {}).get("last_updated", "")
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
        return data.get("metadata", {})


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
