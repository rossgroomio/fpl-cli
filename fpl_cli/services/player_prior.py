"""Player-level Bayesian prior for early-season confidence.

Uses previous-season pts/90 from vaastav data to determine per-player
confidence. Scores are shrunk toward position means with shrinkage
reduced for players with strong historical track records. A price-based
confidence floor handles new signings with no PL history.

Cache design follows team_ratings_prior.py: YAML with metadata,
atomic writes, season-change invalidation.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

import yaml

from fpl_cli.paths import user_data_file
from fpl_cli.season import get_season_year, season_label
from fpl_cli.utils.files import atomic_write_text

if TYPE_CHECKING:
    from fpl_cli.api.historical_types import PlayerProfile
    from fpl_cli.models.player import Player

logger = logging.getLogger(__name__)

def prior_config_path() -> Path:
    """Player prior cache location."""
    return user_data_file("player_prior.yaml")
REGRESSION_CONSTANT = 6
CUTOFF_GW = 10
PRICE_CONFIDENCE_FACTOR = 0.5
MIN_MINUTES = 450


@dataclass(frozen=True)
class PlayerPrior:
    """Per-player prior data for confidence-weighted shrinkage."""

    prior_strength: float  # 0.0-1.0, percentile rank of pts/90 within position
    confidence: float  # 0.0-1.0, how much to trust current-season data
    source: str  # "history", "price", "position-average"
    reliability: float | None = None  # recency-weighted historical availability (None = no history)


def _previous_season_label() -> str:
    """Get the vaastav season label for the previous season."""
    return season_label(get_season_year() - 1)


def _extract_prev_season_pts_per_90(
    profile: PlayerProfile, prev_season: str,
) -> float | None:
    """Extract pts/90 from the previous season's SeasonHistory.

    Looks up by season label and MIN_MINUTES threshold directly,
    rather than indexing into the pre-computed pts_per_90 list
    (which has no season labels).
    """
    for sh in profile.seasons:
        if sh.season == prev_season and sh.minutes >= MIN_MINUTES:
            return sh.total_points / sh.minutes * 90
    return None


def _percentile_rank(value: float, values: list[float]) -> float:
    """Compute percentile rank of value within values (0.0-1.0)."""
    if len(values) <= 1:
        return 0.5
    below = sum(1 for v in values if v < value)
    equal = sum(1 for v in values if v == value)
    return (below + equal * 0.5) / len(values)


def _compute_confidence(gw: int, prior_strength: float) -> float:
    """Confidence = min(1.0, base_confidence * (1 + prior_strength)).

    Hard cutoff: confidence = 1.0 when gw >= CUTOFF_GW.
    """
    if gw >= CUTOFF_GW:
        return 1.0
    effective_gw = max(gw, 1)  # pre-season (GW 0) treated as GW 1
    base_confidence = effective_gw / (effective_gw + REGRESSION_CONSTANT)
    return min(1.0, base_confidence * (1 + prior_strength))


def generate_player_prior(
    profiles: dict[int, PlayerProfile],
    players: list[Player],
    current_gw: int,
) -> dict[int, PlayerPrior]:
    """Generate per-player priors from vaastav history and current FPL data.

    Args:
        profiles: Vaastav PlayerProfile keyed by element_code.
        players: Current FPL players (needed for code->id mapping and prices).
        current_gw: Current gameweek number.

    Returns:
        Dict of player_id -> PlayerPrior.
    """
    from fpl_cli.services.scoring.constants import _position_from_element_type

    prev_season = _previous_season_label()

    # Build code->player mapping
    code_to_player: dict[int, Player] = {}
    for p in players:
        if p.code > 0:
            code_to_player[p.code] = p

    # Pass 1: collect pts/90 by position for percentile computation
    position_pts: dict[str, list[float]] = {"GK": [], "DEF": [], "MID": [], "FWD": []}
    player_pts_map: dict[int, float] = {}  # player_id -> pts/90

    for code, profile in profiles.items():
        fpl_player = code_to_player.get(code)
        if fpl_player is None:
            continue
        pts_90 = _extract_prev_season_pts_per_90(profile, prev_season)
        if pts_90 is None:
            continue
        position = _position_from_element_type(fpl_player.position.value)
        position_pts.setdefault(position, []).append(pts_90)
        player_pts_map[fpl_player.id] = pts_90

    # Build price percentiles by position for no-history fallback
    position_prices: dict[str, list[int]] = {"GK": [], "DEF": [], "MID": [], "FWD": []}
    for p in players:
        position = _position_from_element_type(p.position.value)
        position_prices.setdefault(position, []).append(p.now_cost)

    # Pass 2: compute priors
    result: dict[int, PlayerPrior] = {}
    for p in players:
        position = _position_from_element_type(p.position.value)
        profile = profiles.get(p.code)

        if p.id in player_pts_map:
            # Has qualifying history
            pts_90 = player_pts_map[p.id]
            pos_values = position_pts.get(position, [])
            prior_strength = _percentile_rank(pts_90, pos_values)
            source = "history"
        else:
            # No qualifying history (injured last season, new signing, no vaastav data)
            price_values = position_prices.get(position, [])
            price_pct = _percentile_rank(float(p.now_cost), [float(v) for v in price_values])
            prior_strength = price_pct * PRICE_CONFIDENCE_FACTOR
            source = "price"

        reliability = profile.reliability if profile is not None else None
        confidence = _compute_confidence(current_gw, prior_strength)
        result[p.id] = PlayerPrior(
            prior_strength=round(prior_strength, 4),
            confidence=round(confidence, 4),
            source=source,
            reliability=reliability,
        )

    return result


# ---------------------------------------------------------------------------
# Cache management
# ---------------------------------------------------------------------------


def _load_prior_cache() -> dict[str, Any] | None:
    """Load cached prior from disk, or None if missing/invalid."""
    path = prior_config_path()
    if not path.exists():
        return None
    with open(path, encoding="utf-8") as f:
        data = yaml.safe_load(f)
    if not data or "priors" not in data:
        return None
    return data


def _save_prior_cache(
    priors: dict[int, PlayerPrior],
    season: str,
    gw: int,
) -> None:
    """Save priors to disk (atomic write)."""
    path = prior_config_path()
    data: dict[str, Any] = {
        "metadata": {"season": season, "gameweek": gw},
        "priors": {},
    }
    for pid in sorted(priors):
        p = priors[pid]
        data["priors"][pid] = {
            "prior_strength": p.prior_strength,
            "confidence": p.confidence,
            "source": p.source,
            "reliability": p.reliability,
        }
    atomic_write_text(path, yaml.dump(data, default_flow_style=False, sort_keys=False))


def load_cached_priors(current_gw: int) -> dict[int, PlayerPrior] | None:
    """Load cached priors if valid for current season and gameweek.

    Returns None if cache is missing, stale (wrong season), or
    for a different gameweek.
    """
    data = _load_prior_cache()
    if data is None:
        return None

    meta = data.get("metadata", {})
    current_season = season_label()
    if meta.get("season") != current_season:
        logger.info("Player prior cache stale (season %s != %s)", meta.get("season"), current_season)
        return None
    if meta.get("gameweek") != current_gw:
        logger.info("Player prior cache stale (GW %s != %s)", meta.get("gameweek"), current_gw)
        return None

    result: dict[int, PlayerPrior] = {}
    for pid_str, vals in data["priors"].items():
        result[int(pid_str)] = PlayerPrior(
            prior_strength=vals["prior_strength"],
            confidence=vals["confidence"],
            source=vals["source"],
            reliability=vals.get("reliability"),
        )
    return result
