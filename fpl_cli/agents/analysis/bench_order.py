"""Bench order agent for optimizing substitute ordering."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from fpl_cli.agents.base import Agent, AgentResult, AgentStatus
from fpl_cli.api.fpl import FPLClient
from fpl_cli.models.player import FORMATION_LIMITS, PlayerStatus
from fpl_cli.services.player_scoring import (
    ScoringContext,
    apply_shrinkage,
    build_fixture_matchups,
    build_player_evaluation,
    calculate_bench_score,
    compute_form_trajectory,
    prepare_scoring_data,
)

if TYPE_CHECKING:
    from fpl_cli.models.player import Player


class BenchOrderAgent(Agent):
    """Agent for optimizing bench/substitute ordering.

    Responsibilities:
    - Order bench players by expected points contribution
    - Surface availability risks in the starting XI
    - Provide formation context (coverage gaps, sole coverage)
    - Account for set-piece duties and fixture difficulty
    """

    name = "BenchOrderAgent"
    description = "Optimizes substitute ordering for auto-sub"

    def __init__(self, config: dict[str, Any] | None = None):
        super().__init__(config)
        self.client = FPLClient()

    async def close(self) -> None:
        await self.client.close()

    async def run(self, context: dict[str, Any] | None = None) -> AgentResult:
        """Optimize bench ordering.

        Args:
            context: Should contain:
                - 'starting_xi': List of player IDs in starting lineup
                - 'bench': List of player IDs on bench

        Returns:
            AgentResult with optimal bench order, availability risks,
            formation context, and warnings.
        """
        self.log("Optimizing bench order...")

        if not context or "bench" not in context:
            return self._create_result(
                AgentStatus.FAILED,
                message="No bench players provided",
                errors=["Provide 'bench' list of player IDs in context"],
            )

        try:
            data = await prepare_scoring_data(
                self.client,
                include_players=True, include_understat=True,
                include_history=True, include_prior=True,
            )
            all_players = data.players or []
            player_map = {p.id: p for p in all_players}
            player_histories = data.player_histories or {}
            scoring_context = data.scoring_ctx
            team_map = data.team_map
            understat_by_id = data.understat_lookup

            bench_ids = context["bench"]
            bench_players = [player_map[pid] for pid in bench_ids if pid in player_map]

            starting_ids = context.get("starting_xi", [])
            starting_players = [player_map[pid] for pid in starting_ids if pid in player_map]

            availability_risks = self._analyze_availability_risk(starting_players, team_map)
            formation_context = self._analyze_formation_context(starting_players, bench_players)

            next_gw_id = data.next_gw_id

            scored_bench = []
            for player in bench_players:
                score_data = self._score_bench_player(
                    player, scoring_context, availability_risks,
                    next_gw_id=next_gw_id,
                    understat_by_id=understat_by_id,
                    player_histories=player_histories,
                    player_priors=data.player_priors,
                )
                scored_bench.append(score_data)

            # Apply early-season shrinkage
            apply_shrinkage(scored_bench, "priority_score", data.player_priors, next_gw_id)

            # Sort outfield by raw score (avoids ties from normalisation rounding), GK always last
            outfield = [p for p in scored_bench if p["position"] != "GK"]
            goalkeepers = [p for p in scored_bench if p["position"] == "GK"]
            outfield.sort(key=lambda x: x["priority_score_raw"], reverse=True)
            optimal_order = outfield[:3] + goalkeepers

            warnings = [
                f"No {pos} cover on bench; XI has {FORMATION_LIMITS[pos][0]} {pos} "
                f"so a missing {pos} forces a formation change"
                for pos in formation_context["coverage_gaps"]
            ]

            self.log_success("Bench order optimized")

            return self._create_result(
                AgentStatus.SUCCESS,
                data={
                    "optimal_order": [
                        {
                            "id": p["id"],
                            "name": p["name"],
                            "position": p["position"],
                            "priority_score": p["priority_score"],
                            "priority_score_raw": p["priority_score_raw"],
                            "reasons": p["reasons"],
                        }
                        for p in optimal_order
                    ],
                    "availability_risks": availability_risks,
                    "formation_context": formation_context,
                    "warnings": warnings,
                },
                message="Bench order optimized",
            )

        except Exception as e:  # noqa: BLE001 — agent top-level handler
            self.log_error(f"Failed to optimize bench: {e}")
            return self._create_result(
                AgentStatus.FAILED,
                message="Failed to optimize bench order",
                errors=[str(e)],
            )

    def _analyze_availability_risk(
        self,
        starting_players: list[Player],
        team_map: dict[int, Any],
    ) -> list[dict[str, Any]]:
        """Identify starters with availability risk (injury, illness, suspension, etc.)."""
        risks = []

        for player in starting_players:
            risk_level = 0
            risk_reasons = []

            if player.status != PlayerStatus.AVAILABLE:
                if player.chance_of_playing_next_round is not None:
                    if player.chance_of_playing_next_round <= 25:
                        risk_level = 3
                        risk_reasons.append(f"Only {player.chance_of_playing_next_round}% chance of playing")
                    elif player.chance_of_playing_next_round <= 50:
                        risk_level = 2
                        risk_reasons.append(f"{player.chance_of_playing_next_round}% chance of playing")
                    elif player.chance_of_playing_next_round <= 75:
                        risk_level = 1
                        risk_reasons.append(f"{player.chance_of_playing_next_round}% chance of playing")
                else:
                    risk_level = 2
                    risk_reasons.append(f"Status: {player.status.value}")

            if risk_level > 0:
                team = team_map.get(player.team_id)
                risks.append({
                    "id": player.id,
                    "name": player.web_name,
                    "team": team.short_name if team else "???",
                    "position": player.position_name,
                    "risk_level": risk_level,
                    "reasons": risk_reasons,
                })

        return sorted(risks, key=lambda x: x["risk_level"], reverse=True)

    def _analyze_formation_context(
        self,
        starting_players: list[Player],
        bench_players: list[Player],
    ) -> dict[str, Any]:
        """Analyze formation constraints for auto-sub context.

        Identifies which positions are at their minimum count in the XI
        and whether the bench provides coverage for those positions.
        This is informational - it does not affect bench ordering.
        """
        xi_counts: dict[str, int] = {}
        for player in starting_players:
            pos = player.position_name
            xi_counts[pos] = xi_counts.get(pos, 0) + 1

        bench_by_position: dict[str, list[dict[str, Any]]] = {}
        for player in bench_players:
            pos = player.position_name
            if pos == "GK":
                continue
            bench_by_position.setdefault(pos, []).append({
                "id": player.id,
                "name": player.web_name,
                "position": pos,
            })

        constrained_positions = []
        sole_coverage = []
        coverage_gaps = []

        for pos, (minimum, _) in FORMATION_LIMITS.items():
            if xi_counts.get(pos, 0) <= minimum:
                constrained_positions.append(pos)
                bench_at_pos = bench_by_position.get(pos, [])
                if len(bench_at_pos) == 0:
                    coverage_gaps.append(pos)
                elif len(bench_at_pos) == 1:
                    sole_coverage.extend(bench_at_pos)

        return {
            "constrained_positions": constrained_positions,
            "sole_coverage": sole_coverage,
            "coverage_gaps": coverage_gaps,
        }

    def _score_bench_player(
        self,
        player: Player,
        context: ScoringContext,
        availability_risks: list[dict[str, Any]],
        *,
        next_gw_id: int,
        understat_by_id: dict[int, dict[str, float]] | None = None,
        player_histories: dict[int, list[dict[str, Any]]] | None = None,
        player_priors: dict[int, Any] | None = None,
    ) -> dict[str, Any]:
        """Score a bench player via the scoring engine."""
        team = context.team_map.get(player.team_id)
        fixture_matchups = build_fixture_matchups(
            player.team_id, player.position_name, context,
        )

        enrichment: dict[str, Any] = {"team_short": team.short_name if team else "???"}
        if understat_by_id:
            us_data = understat_by_id.get(player.id)
            if us_data:
                enrichment.update(us_data)

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

        return calculate_bench_score(
            evaluation, identity,
            availability_risks=availability_risks,
            next_gw_id=next_gw_id,
        )
