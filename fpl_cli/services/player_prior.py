"""Player-level Bayesian prior for early-season confidence.

Uses previous-season pts/90 from the historical datasets to determine
per-player confidence in this season's data. Two consumers read it before
``CUTOFF_GW``:

- The value family (``quality_score``) and the ownership family (target,
  differential, waiver) blend the observed raw score with the score the prior
  implies — a player's pts/90 percentile placed on the calibrated elite scale
  — weighted by confidence
  (``scoring.value_quality.blend_quality_with_prior``). Going into GW2 the
  observation is one gameweek, so a quiet-starting elite keeps most of
  their standing and a one-game wonder does not saturate the scale. The
  ownership family blends its quality baseline only, before the matchup,
  ownership, position-need and consistency terms the prior does not model.
- The single-GW family (captain, bench, lineup, starting XI) shrinks its
  scores toward the position mean instead, with shrinkage reduced for players
  with strong track records (``scoring.shrinkage``).

A price-based prior handles new signings with no PL history: the
within-position price percentile, halved, so it can never outrank a
mid-table history.

Cache design follows team_ratings_prior.py: YAML with metadata,
atomic writes, season-change invalidation.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

import yaml

from fpl_cli.paths import user_data_file
from fpl_cli.season import previous_season_label, season_label
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


def percentile_rank(value: float, values: list[float]) -> float:
    """Compute percentile rank of value within values (0.0-1.0).

    Public because the returnee radar's last-resort price bar ranks players by
    the same rule, and two copies of a tie-handling formula drift apart.
    """
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


def observation_weight_range(gw: int) -> tuple[float, float]:
    """Least and most weight this season's observation can carry at *gw*.

    The confidence curve spans ``prior_strength`` 0 (no history and the
    cheapest price in the position) to 1 (top of last season's pts/90
    percentile), so this is the band a blended quality score sits in — going
    into GW2 the observation carries 25-50% of it, going into GW5 45-91%.
    ``early_season_quality_warning`` quotes it so a reader knows how much of
    the score is measurement and how much pedigree.
    """
    return _compute_confidence(gw, 0.0), _compute_confidence(gw, 1.0)


def early_season_quality_warning(
    next_gw_id: int,
    *,
    blended: bool,
    score_names: Sequence[str] = ("quality_score",),
) -> dict[str, str] | None:
    """The ``metadata.warnings`` entry to carry beside a prior-blended score produced at *next_gw_id*.

    One helper for every command that shows a score the early-season prior
    blend reaches — the value family's ``quality_score`` (``fpl stats
    --value``, ``fpl player``, ``fpl allocate``) and the ownership family's
    ``target_score`` / ``differential_score`` / ``waiver_score`` (``fpl
    targets``, ``fpl differentials``, ``fpl waivers``), with ``fpl
    transfer-eval`` showing one of each — so a reader and an agent are told
    the same thing about the same kind of number whichever command produced
    it. Both codes are shared across all of them: the condition and the device
    are the same, so a consumer keying on the code needs one rule.

    *score_names* names the fields this caller actually shows, so the notice
    points at them rather than at a field the output does not contain. Its
    single-GW siblings (``lineup_score`` and the captain / bench / XI scores)
    are **not** blended — they still shrink toward the position mean — so a
    caller showing one of those does not name it here.

    *blended* is whether the prior actually reached the score — the priors
    loaded — so the entry also tells a successful blend from a degraded run:
    the loader swallows a failed history fetch and returns None, and without
    this the same command, player and gameweek could print 76 or 59 with
    identical metadata. Returns None once there is nothing to caveat: from the
    prior cutoff when blended, from GW6 when not.
    """
    from fpl_cli.services.scoring.constants import MINS_FACTOR_START_GW

    subject = " and ".join(score_names)
    plural = len(score_names) > 1

    if blended:
        if next_gw_id >= CUTOFF_GW:
            return None
        low, high = observation_weight_range(next_gw_id)
        reading = "prior-informed estimates" if plural else "a prior-informed estimate"
        return {
            "code": "early_season_prior_informed",
            "message": (
                f"Early-season notice: until GW{CUTOFF_GW}, {subject} "
                f"{'blend' if plural else 'blends'} "
                "this season's observation with last season's pts/90 pedigree "
                "(price for players without PL history), so a quiet-starting "
                "elite keeps most of their standing and one good game does not "
                f"saturate the scale. Going into GW{next_gw_id} the observation "
                f"carries {low:.0%}-{high:.0%} of the score depending on the "
                f"player's track record. Read {subject} as {reading}, "
                f"not {'measurements' if plural else 'a measurement'}; ep_next "
                "(fpl stats --sort ep_next) is FPL's own projection for the "
                "coming gameweek, but in the opening gameweeks it tracks form "
                "almost exactly, so before ~GW6 it is not a second opinion."
            ),
        }
    if next_gw_id > MINS_FACTOR_START_GW:
        return None
    return {
        "code": "early_season_small_sample",
        "message": (
            "Early-season notice: last season's history could not be loaded, "
            f"so {subject} {'are' if plural else 'is'} pure observation and "
            "small-sample dominated "
            "before GW6 — form and ppg reflect only the opening gameweek(s) and "
            "per-90 rates come from very few minutes, so hot starters saturate "
            "the scale while elite players with a quiet start read low. GK "
            f"ceilings scale with the sample the calendar has made possible, "
            f"reaching full scale at GW6. Treat {subject} as provisional "
            "until ~GW6-10 and weigh last season's pedigree, role and "
            "fixtures above the numbers; ep_next is no escape this early, "
            "since it tracks form and carries the same small sample."
        ),
    }


def generate_player_prior(
    profiles: dict[int, PlayerProfile],
    players: list[Player],
    current_gw: int,
) -> dict[int, PlayerPrior]:
    """Generate per-player priors from player history and current FPL data.

    Args:
        profiles: PlayerProfile keyed by element_code, as
            `HistoricalDataProvider.get_all_player_histories()` returns them.
        players: Current FPL players (needed for code->id mapping and prices).
        current_gw: Current gameweek number.

    Returns:
        Dict of player_id -> PlayerPrior.
    """
    from fpl_cli.services.scoring import _position_from_element_type

    prev_season = previous_season_label()

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
            prior_strength = percentile_rank(pts_90, pos_values)
            source = "history"
        else:
            # No qualifying history (injured last season, new signing, no historical data)
            price_values = position_prices.get(position, [])
            price_pct = percentile_rank(float(p.now_cost), [float(v) for v in price_values])
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


async def load_or_generate_player_priors(
    players: list[Player],
    next_gw_id: int,
) -> dict[int, PlayerPrior] | None:
    """Priors for *next_gw_id*: the cache when current, else generated and cached.

    The one entry point for every command that scores against a prior —
    ``prepare_scoring_data`` for the agents, and ``fpl stats --value`` /
    ``fpl player`` directly, since the value family blends the prior into
    ``quality_score`` before ``CUTOFF_GW``. Returns None when the historical
    datasets cannot be reached, so a caller degrades to pure-observation
    scores and says so, rather than failing the command over a dataset the
    score can do without.
    """
    try:
        cached = load_cached_priors(next_gw_id)
        if cached is not None:
            return cached

        from fpl_cli.api.historical import make_historical_provider

        async with make_historical_provider() as historical:
            profiles = await historical.get_all_player_histories()
        priors = generate_player_prior(profiles, players, next_gw_id)
        _save_prior_cache(priors, season_label(), next_gw_id)
        return priors
    except Exception as exc:  # noqa: BLE001 — graceful degradation: historical datasets unavailable
        # No traceback: fpl-cli configures no logging handlers, so a WARNING
        # with exc_info reaches logging's lastResort handler and dumps it raw
        # into stderr, including under `--format json` (issue #237/#239 review).
        logger.warning("Failed to generate player priors: %s", exc)
        return None
