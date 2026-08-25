"""Player evaluation inputs: enrichment assembly and evaluation types.

build_scoring_enrichment merges Understat data, derived per-90 rates, GK
signals, and per-player scoring signals into the enrichment dict;
build_player_evaluation normalises a Player model or enriched dict into
the immutable PlayerEvaluation / PlayerIdentity pair that every scoring
formula consumes.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from fpl_cli.services.scoring.constants import (
    GK_SAMPLE_RAMP_MINUTES,
    GK_XGC_QUALITY_ANCHOR,
    Position,
    _as_position,
    _position_from_element_type,
)
from fpl_cli.services.scoring.signals import (
    apply_consistency,
    compute_form_trajectory,
    compute_xgi_sustainability,
)

if TYPE_CHECKING:
    from fpl_cli.models.player import Player
    from fpl_cli.services.scoring.signals import ConsistencySignals


def build_scoring_enrichment(
    player: Any,
    us_match: dict[str, Any],
    team_short: str,
    gw_history: list[dict[str, Any]] | None,
    next_gw_id: int,
    *,
    consistency_lookup: dict[int, ConsistencySignals] | None = None,
) -> dict[str, Any]:
    """Build the enrichment dict shared by quality and single-GW scoring paths."""
    # Strip understat's "position" (e.g. "F M S") — its taxonomy differs from FPL's
    # and would otherwise shadow Player.position in build_player_evaluation.
    enrichment: dict[str, Any] = {"team_short": team_short, **us_match}
    enrichment.pop("position", None)
    minutes_safe = max(player.minutes, 1)
    enrichment["xGI_per_90"] = (
        (player.expected_goals + player.expected_assists) / minutes_safe * 90
    )
    enrichment["dc_per_90"] = player.defensive_contribution_per_90
    if player.position_name == "GK":
        # Sample-size ramp: per-90 rates are noisy below ~5 full games.
        # Consistent with waiver availability ramp (minutes / 450).
        sample_ramp = min(player.minutes / GK_SAMPLE_RAMP_MINUTES, 1.0)
        enrichment["gk_saves_per_90"] = player.saves_per_90 * sample_ramp
        if player.minutes > 0:
            xgc_per_90 = (player.expected_goals_conceded / player.minutes) * 90
            enrichment["gk_xgc_quality"] = (
                max(0.0, GK_XGC_QUALITY_ANCHOR - xgc_per_90) * sample_ramp
            )
        else:
            enrichment["gk_xgc_quality"] = 0.0
        enrichment["gk_cs_rate"] = (player.clean_sheets / max(player.appearances, 1)) * sample_ramp

    if gw_history:
        enrichment["form_trajectory"] = compute_form_trajectory(gw_history, next_gw_id)
        sustainability, divergence = compute_xgi_sustainability(
            gw_history, next_gw_id, player.position_name
        )
        enrichment["xgi_sustainability"] = sustainability
        enrichment["xgi_divergence"] = divergence

    if consistency_lookup:
        apply_consistency(enrichment, int(getattr(player, "id", 0)), consistency_lookup)

    return enrichment


# ---------------------------------------------------------------------------
# Evaluation types
# ---------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class FixtureMatchup:
    """Pre-resolved fixture data for a single fixture within a gameweek."""

    opponent_short: str
    is_home: bool
    opponent_fdr: float
    matchup_score: float
    matchup_breakdown: dict[str, Any] | None = None


@dataclasses.dataclass(frozen=True)
class PlayerEvaluation:
    """Scoring-relevant data for a single player. Immutable.

    All fields that scoring functions read for arithmetic live here.
    Display-only fields live in PlayerIdentity.
    """

    # Quality baseline inputs (keys match calculate_player_quality_score dict interface)
    form: float
    ppg: float
    xgi_per_90: float
    npxg_per_90: float | None
    xg_chain_per_90: float | None
    dc_per_90: float
    penalty_xg_per_90: float | None

    # Minutes risk (scorers derive mins_factor from these via calculate_mins_factor)
    minutes: int
    appearances: int

    # Position (for without_xgi gate and position multiplier)
    position: Position

    # Fixture data
    fixture_matchups: list[FixtureMatchup]
    matchup_avg_3gw: float | None = None
    positional_fdr: float | None = None

    # Regression inputs
    gi_minus_xgi: float = 0.0

    # Ownership (differential scoring)
    ownership: float = 0.0

    # Availability
    status: str = "a"
    chance_of_playing: int | None = None

    # Team context (waiver stacking)
    team_id: int = 0
    team_short: str = ""

    # Set pieces
    penalties_order: int | None = None
    corners_and_indirect_freekicks_order: int | None = None
    direct_freekicks_order: int | None = None

    # Form trajectory (multiplier on form contribution: 0.8=falling, 1.0=stable, 1.2=rising)
    form_trajectory: float = 1.0

    # Bayesian prior confidence (1.0=trust current data fully, <1.0=shrink toward position mean)
    prior_confidence: float = 1.0

    # xGI sustainability (multiplier on form contribution: <1.0=overperforming regression risk,
    # 1.0=at rate, >1.0=underperforming regression upside). ATK only; DEF/GK default to 1.0.
    xgi_sustainability: float = 1.0

    # GK-specific signals (zero for non-GKs; weight gate ensures zero contribution)
    gk_saves_per_90: float = 0.0
    gk_xgc_quality: float = 0.0
    gk_cs_rate: float = 0.0

    # Original Understat npxG/90 before fixture adjustment (None when no Understat data)
    raw_npxg_per_90: float | None = None

    # Consistency signals
    cv_xgi_percentile: float = 0.5
    blank_rate: float | None = None
    floor_percentile: float = 0.5
    involvement_rate: float | None = None
    gk_consistency_percentile: float = 0.5

    def as_quality_dict(self) -> dict[str, Any]:
        """Return a dict of evaluation fields for quality scoring and display."""
        return {
            "npxG_per_90": self.npxg_per_90,
            "xGChain_per_90": self.xg_chain_per_90,
            "xGI_per_90": self.xgi_per_90,
            "form": self.form,
            "ppg": self.ppg,
            "dc_per_90": self.dc_per_90,
            "penalty_xG_per_90": self.penalty_xg_per_90,
            "form_trajectory": self.form_trajectory,
            "prior_confidence": self.prior_confidence,
            "xgi_sustainability": self.xgi_sustainability,
            "gk_saves_per_90": self.gk_saves_per_90,
            "gk_xgc_quality": self.gk_xgc_quality,
            "gk_cs_rate": self.gk_cs_rate,
            "cv_xgi_percentile": self.cv_xgi_percentile,
            "floor_percentile": self.floor_percentile,
            "involvement_rate": self.involvement_rate,
            "gk_consistency_percentile": self.gk_consistency_percentile,
        }


@dataclasses.dataclass(frozen=True)
class PlayerIdentity:
    """Display-only fields passed through to scoring output dicts.

    No scoring function reads these for arithmetic.
    """

    id: int
    web_name: str
    team_short: str
    position_name: str
    price: float
    ownership: float
    expected_goals: float = 0.0
    expected_assists: float = 0.0
    points_per_game: float = 0.0


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


def _extract_status(val: Any) -> str:
    """Convert a PlayerStatus enum or string to its single-char status code."""
    from fpl_cli.models.player import PlayerStatus

    if isinstance(val, PlayerStatus):
        return val.value
    return str(val)


def read_player_field(source: Any, name: str, default: Any = None) -> Any:
    """Read one field from a ``Player`` model or a player-shaped mapping.

    The single place that knows a player can arrive as either shape. Attribute
    first, then mapping key, then *default* — ``build_player_evaluation`` and
    the shrinkage hold-out both read through this so their notion of "does this
    player have field X" cannot drift apart.
    """
    if hasattr(source, name):
        return getattr(source, name)
    if isinstance(source, Mapping):
        return source.get(name, default)
    return default


def build_player_evaluation(
    player: Player | Mapping[str, Any],
    *,
    enrichment: dict[str, Any] | None = None,
    fixture_matchups: list[FixtureMatchup] | None = None,
    matchup_avg_3gw: float | None = None,
    positional_fdr: float | None = None,
) -> tuple[PlayerEvaluation, PlayerIdentity]:
    """Build evaluation and identity from a Player model or enriched dict.

    Normalises both input shapes to the same field set. When *enrichment*
    is provided its keys overlay the base player data.
    """
    # Unified accessor: enrichment overlay, then the shared field reader
    def _get(key: str, default: Any = None) -> Any:
        if enrichment and key in enrichment:
            return enrichment[key]
        return read_player_field(player, key, default)

    minutes = _get("minutes", 0)
    appearances = _get("appearances", 0)

    # Availability: 0 is a real value ("ruled out of the next round"), not a
    # missing one, so the fallback to the model's field name tests for None
    # rather than truthiness. An ``or`` here read every 0% player as "no
    # availability information" — exactly inverting the signal, since the
    # 25/50/75 doubts survived it and the definitely-out players did not.
    chance_of_playing = _get("chance_of_playing")
    if chance_of_playing is None:
        chance_of_playing = _get("chance_of_playing_next_round")

    # Position: Player model stores as enum, dicts store as string
    position_raw = _get("position")
    position: Position
    if hasattr(position_raw, "value"):
        position = _position_from_element_type(position_raw.value)
    else:
        position = _as_position(str(position_raw) if position_raw else "")

    # Position name for identity (same as position for dicts, computed for model)
    position_name = _get("position_name") or position

    # Build evaluation
    evaluation = PlayerEvaluation(
        form=float(_get("form", 0)),
        ppg=float(_get("ppg") if _get("ppg") is not None else _get("points_per_game", 0)),
        xgi_per_90=float(_get("xGI_per_90", 0) or 0),
        npxg_per_90=_get("npxG_per_90"),
        xg_chain_per_90=_get("xGChain_per_90"),
        dc_per_90=float(_get("dc_per_90", 0) or 0),
        penalty_xg_per_90=_get("penalty_xG_per_90"),
        minutes=minutes,
        appearances=appearances,
        position=position,
        fixture_matchups=fixture_matchups or [],
        matchup_avg_3gw=matchup_avg_3gw,
        positional_fdr=positional_fdr,
        gi_minus_xgi=float(_get("GI_minus_xGI", 0) or 0),
        ownership=float(_get("ownership", 0) or _get("selected_by_percent", 0) or 0),
        status=_extract_status(_get("status", "a")),
        chance_of_playing=chance_of_playing,
        team_id=int(_get("team_id", 0)),
        team_short=str(_get("team_short", "")),
        penalties_order=_get("penalties_order"),
        corners_and_indirect_freekicks_order=_get("corners_and_indirect_freekicks_order"),
        direct_freekicks_order=_get("direct_freekicks_order"),
        form_trajectory=float(_get("form_trajectory", 1.0) or 1.0),
        prior_confidence=float(_get("prior_confidence", 1.0) or 1.0),
        xgi_sustainability=float(_get("xgi_sustainability", 1.0) or 1.0),
        gk_saves_per_90=float(_get("gk_saves_per_90", 0.0) or 0.0),
        gk_xgc_quality=float(_get("gk_xgc_quality", 0.0) or 0.0),
        gk_cs_rate=float(_get("gk_cs_rate", 0.0) or 0.0),
        raw_npxg_per_90=_get("raw_npxG_per_90"),
        cv_xgi_percentile=float(_get("cv_xgi_percentile", 0.5) or 0.5),
        blank_rate=_get("blank_rate"),
        floor_percentile=float(_get("floor_percentile", 0.5) or 0.5),
        involvement_rate=_get("involvement_rate"),
        gk_consistency_percentile=float(_get("gk_consistency_percentile", 0.5) or 0.5),
    )

    # Build identity
    identity = PlayerIdentity(
        id=int(_get("id", 0)),
        web_name=str(_get("web_name", "")),
        team_short=str(_get("team_short", "")),
        position_name=position_name,
        price=float(_get("price", 0)),
        ownership=float(_get("ownership", 0) or _get("selected_by_percent", 0) or 0),
        expected_goals=float(_get("expected_goals", 0)),
        expected_assists=float(_get("expected_assists", 0)),
        points_per_game=float(_get("ppg", 0) or _get("points_per_game", 0)),
    )

    return evaluation, identity
