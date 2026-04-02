"""Transfer evaluation agent for comparing OUT vs IN candidates."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, TypedDict

from fpl_cli.agents.base import Agent, AgentResult, AgentStatus
from fpl_cli.api.fpl import FPLClient
from fpl_cli.services.player_scoring import (
    VALUE_CEILING,
    VALUE_QUALITY_WEIGHTS,
    ScoringContext,
    apply_shrinkage,
    build_fixture_matchups,
    build_player_evaluation,
    calculate_lineup_score,
    calculate_mins_factor,
    calculate_player_quality_score,
    calculate_target_score,
    compute_form_trajectory,
    normalise_score,
    prepare_scoring_data,
)

if TYPE_CHECKING:
    from fpl_cli.models.player import Player


class TransferEvalContext(TypedDict):
    """Context dict for TransferEvalAgent.run()."""

    out_player_id: int
    in_player_ids: list[int]


class TransferEvalAgent(Agent):
    """Agent for evaluating transfer OUT/IN candidates across two scoring horizons.

    Scores each player on:
    - Outlook (target score): multi-GW quality via calculate_target_score
    - This GW (lineup score): single-GW lineup impact via calculate_lineup_score

    Returns structured comparison with deltas for each IN candidate.
    """

    name = "TransferEvalAgent"
    description = "Evaluates transfer candidates across outlook and lineup horizons"

    def __init__(self, config: dict[str, Any] | None = None):
        super().__init__(config)
        self.client = FPLClient()

    async def close(self) -> None:
        await self.client.close()

    async def run(self, context: dict[str, Any] | None = None) -> AgentResult:
        """Evaluate transfer candidates.

        Args:
            context: See TransferEvalContext for expected keys.

        Returns:
            AgentResult with out_player scores, in_players with deltas,
            sorted by outlook_delta descending.
        """
        self.log("Evaluating transfer candidates...")

        if not context or "out_player_id" not in context or "in_player_ids" not in context:
            return self._create_result(
                AgentStatus.FAILED,
                message="Missing required context",
                errors=["Provide 'out_player_id' and 'in_player_ids' in context"],
            )

        in_player_ids = context["in_player_ids"]
        if not in_player_ids:
            return self._create_result(
                AgentStatus.FAILED,
                message="No IN candidates provided",
                errors=["Provide at least one player ID in 'in_player_ids'"],
            )

        try:
            data = await prepare_scoring_data(
                self.client,
                include_players=True, include_understat=True,
                include_history=True, include_prior=True,
            )
            all_players = data.players or []
            player_map = {p.id: p for p in all_players}

            out_id = context["out_player_id"]
            all_ids = [out_id, *in_player_ids]
            missing = [pid for pid in all_ids if pid not in player_map]
            if missing:
                return self._create_result(
                    AgentStatus.FAILED,
                    message=f"Could not resolve {len(missing)} player ID(s)",
                    errors=[f"Missing player IDs: {missing}"],
                )

            next_gw_id = data.next_gw_id

            # Score all players on both horizons
            target_scored: list[dict[str, Any]] = []
            lineup_scored: list[dict[str, Any]] = []

            for pid in all_ids:
                player = player_map[pid]
                target_entry, lineup_entry = self._score_player(
                    player, data.scoring_ctx,
                    next_gw_id=next_gw_id,
                    understat_by_id=data.understat_lookup,
                    player_histories=data.player_histories,
                    player_priors=data.player_priors,
                )
                target_scored.append(target_entry)
                lineup_scored.append(lineup_entry)

            # Apply shrinkage to both score types
            apply_shrinkage(target_scored, "target_score", data.player_priors, next_gw_id)
            apply_shrinkage(lineup_scored, "lineup_score", data.player_priors, next_gw_id)

            # Build result dicts
            out_target = target_scored[0]["target_score"]
            out_lineup = lineup_scored[0]["lineup_score"]

            out_player_data = self._build_player_dict(
                target_scored[0], lineup_scored[0],
                outlook_delta=None, gw_delta=None,
            )

            in_players_data = []
            for i, pid in enumerate(in_player_ids):
                idx = i + 1  # offset past out player
                outlook_delta = target_scored[idx]["target_score"] - out_target
                gw_delta = lineup_scored[idx]["lineup_score"] - out_lineup
                in_players_data.append(self._build_player_dict(
                    target_scored[idx], lineup_scored[idx],
                    outlook_delta=outlook_delta, gw_delta=gw_delta,
                ))

            in_players_data.sort(key=lambda x: x["outlook_delta"], reverse=True)

            self.log_success("Transfer evaluation complete")

            return self._create_result(
                AgentStatus.SUCCESS,
                data={
                    "out_player": out_player_data,
                    "in_players": in_players_data,
                    "sorted_by": "outlook_delta",
                },
                message="Transfer evaluation complete",
            )

        except Exception as e:  # noqa: BLE001 — agent top-level handler
            self.log_error(f"Failed to evaluate transfers: {e}")
            return self._create_result(
                AgentStatus.FAILED,
                message="Failed to evaluate transfers",
                errors=[str(e)],
            )

    def _score_player(
        self,
        player: Player,
        context: ScoringContext,
        *,
        next_gw_id: int,
        understat_by_id: dict[int, dict[str, float]] | None = None,
        player_histories: dict[int, list[dict[str, Any]]] | None = None,
        player_priors: dict[int, Any] | None = None,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """Score a player on both horizons. Returns (target_entry, lineup_entry)."""
        team = context.team_map.get(player.team_id)
        fixture_matchups = build_fixture_matchups(
            player.team_id, player.position_name, context,
        )

        enrichment: dict[str, Any] = {"team_short": team.short_name if team else "???"}

        # Enrichment parity with fpl player (R5): xGI_per_90 fallback + dc_per_90
        minutes_safe = max(player.minutes, 1)
        enrichment["xGI_per_90"] = (player.expected_goals + player.expected_assists) / minutes_safe * 90
        enrichment["dc_per_90"] = player.defensive_contribution_per_90

        has_understat = False
        if understat_by_id:
            us_data = understat_by_id.get(player.id)
            if us_data:
                enrichment.update(us_data)
                has_understat = True

        if player_histories:
            history = player_histories.get(player.id, [])
            if history:
                enrichment["form_trajectory"] = compute_form_trajectory(history, next_gw_id)

        if player_priors:
            prior = player_priors.get(player.id)
            if prior:
                enrichment["prior_confidence"] = prior.confidence

        evaluation, identity = build_player_evaluation(
            player,
            enrichment=enrichment,
            fixture_matchups=fixture_matchups,
        )

        # Quality score (value dimension): gated on Understat match
        quality_score: int | None = None
        value_score: float | None = None
        if has_understat:
            q_dict = evaluation.as_quality_dict()
            is_defensive = player.position_name in ("GK", "DEF")
            weights = VALUE_QUALITY_WEIGHTS.without_xgi() if is_defensive else VALUE_QUALITY_WEIGHTS
            mins_factor = calculate_mins_factor(player.minutes, player.appearances, next_gw_id)
            raw = calculate_player_quality_score(q_dict, weights, mins_factor)
            quality_score = normalise_score(raw, VALUE_CEILING)
            if identity.price > 0:
                value_score = round(quality_score / identity.price, 1)

        # Target score (outlook): returns int
        target = calculate_target_score(evaluation, next_gw_id=next_gw_id)
        target_entry = {
            "id": identity.id,
            "position": identity.position_name,
            "target_score": target,
        }

        # Lineup score (this GW): returns rich dict
        lineup_entry = calculate_lineup_score(
            evaluation, identity,
            next_gw_id=next_gw_id,
        )

        # Attach metadata for display
        fdr_display = [
            {"opponent": m.opponent_short, "fdr": m.opponent_fdr}
            for m in fixture_matchups[:3]
        ]
        target_entry["fixture_matchups"] = fdr_display
        target_entry["form"] = evaluation.form
        target_entry["status"] = evaluation.status
        target_entry["chance_of_playing"] = evaluation.chance_of_playing
        target_entry["web_name"] = identity.web_name
        target_entry["team_short"] = identity.team_short
        target_entry["price"] = identity.price
        target_entry["quality_score"] = quality_score
        target_entry["value_score"] = value_score

        return target_entry, lineup_entry

    @staticmethod
    def _build_player_dict(
        target_entry: dict[str, Any],
        lineup_entry: dict[str, Any],
        *,
        outlook_delta: int | None,
        gw_delta: int | None,
    ) -> dict[str, Any]:
        """Build a unified player dict from target and lineup scoring results."""
        return {
            "id": target_entry["id"],
            "web_name": target_entry["web_name"],
            "team_short": target_entry["team_short"],
            "position": target_entry["position"],
            "outlook": target_entry["target_score"],
            "this_gw": lineup_entry["lineup_score"],
            "outlook_delta": outlook_delta,
            "gw_delta": gw_delta,
            "fixture_matchups": target_entry["fixture_matchups"],
            "form": target_entry["form"],
            "status": target_entry["status"],
            "chance_of_playing": target_entry["chance_of_playing"],
            "price": target_entry["price"],
            "excluded": lineup_entry.get("excluded", False),
            "quality_score": target_entry["quality_score"],
            "value_score": target_entry["value_score"],
        }
