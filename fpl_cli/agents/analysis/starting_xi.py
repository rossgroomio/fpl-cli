"""Starting XI agent for optimizing lineup selection."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from fpl_cli.agents.base import Agent, AgentResult, AgentStatus
from fpl_cli.api.fpl import FPLClient
from fpl_cli.services.player_scoring import (
    ScoringContext,
    apply_shrinkage,
    build_fixture_matchups,
    build_player_evaluation,
    calculate_lineup_score,
    compute_aggregate_matchup,
    compute_form_trajectory,
    prepare_scoring_data,
    select_starting_xi,
)

if TYPE_CHECKING:
    from fpl_cli.models.player import Player


class StartingXIAgent(Agent):
    """Agent for selecting the optimal starting XI from a 15-player squad.

    Responsibilities:
    - Score each squad player for single-GW expected output
    - Optimise formation to maximise total score
    - Apply team exposure penalties for tough fixtures
    - Surface excluded players (low availability)
    """

    name = "StartingXIAgent"
    description = "Selects optimal starting XI from squad using formation optimisation"

    def __init__(self, config: dict[str, Any] | None = None):
        super().__init__(config)
        self.client = FPLClient()

    async def close(self) -> None:
        await self.client.close()

    async def run(self, context: dict[str, Any] | None = None) -> AgentResult:
        """Select optimal starting XI.

        Args:
            context: Should contain:
                - 'squad': List of 15 player IDs

        Returns:
            AgentResult with starting XI, bench, formation, excluded
            players, and total score.
        """
        self.log("Selecting starting XI...")

        if not context or "squad" not in context:
            return self._create_result(
                AgentStatus.FAILED,
                message="No squad provided",
                errors=["Provide 'squad' list of player IDs in context"],
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
            understat_by_id = data.understat_lookup

            squad_ids = context["squad"]
            squad_players = [player_map[pid] for pid in squad_ids if pid in player_map]

            if len(squad_players) != len(squad_ids):
                missing = set(squad_ids) - set(player_map.keys())
                return self._create_result(
                    AgentStatus.FAILED,
                    message=f"Could not resolve {len(missing)} player ID(s)",
                    errors=[f"Missing player IDs: {missing}"],
                )

            next_gw_id = data.next_gw_id

            scored: list[dict[str, Any]] = []
            for player in squad_players:
                score_data = self._score_squad_player(
                    player, scoring_context,
                    next_gw_id=next_gw_id,
                    understat_by_id=understat_by_id,
                    player_histories=player_histories,
                    player_priors=data.player_priors,
                )
                scored.append(score_data)

            # Apply early-season shrinkage
            apply_shrinkage(scored, "lineup_score", data.player_priors, next_gw_id)

            # Build team_fixtures from scored players' positional FDR
            team_fixtures: dict[str, dict[str, float]] = {}
            for p in scored:
                team = p["team"]
                if team not in team_fixtures:
                    team_fixtures[team] = {}
                fdr = p.get("positional_fdr")
                if fdr is not None:
                    if p["position"] in ("MID", "FWD"):
                        team_fixtures[team]["atk_fdr"] = fdr
                    else:
                        team_fixtures[team]["def_fdr"] = fdr

            result = select_starting_xi(scored, team_fixtures=team_fixtures)

            excluded = [p for p in scored if p["excluded"]]

            self.log_success("Starting XI selected")

            return self._create_result(
                AgentStatus.SUCCESS,
                data={
                    "starting_xi": result["starting_xi"],
                    "bench": result["bench"],
                    "formation": result["formation"],
                    "excluded_players": excluded,
                    "total_score": result["total_score"],
                    "team_exposure_penalties": result["team_exposure_penalties"],
                },
                message="Starting XI selected",
            )

        except Exception as e:  # noqa: BLE001 — agent top-level handler
            self.log_error(f"Failed to select starting XI: {e}")
            return self._create_result(
                AgentStatus.FAILED,
                message="Failed to select starting XI",
                errors=[str(e)],
            )

    def _score_squad_player(
        self,
        player: Player,
        context: ScoringContext,
        *,
        next_gw_id: int,
        understat_by_id: dict[int, dict[str, float]] | None = None,
        player_histories: dict[int, list[dict[str, Any]]] | None = None,
        player_priors: dict[int, Any] | None = None,
    ) -> dict[str, Any]:
        """Score a squad player via the scoring engine."""
        team = context.team_map.get(player.team_id)
        fixture_matchups = build_fixture_matchups(
            player.team_id, player.position_name, context,
        )

        # Positional FDR for team exposure penalties
        _, positional_fdr = compute_aggregate_matchup(
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
            positional_fdr=positional_fdr,
        )

        return calculate_lineup_score(
            evaluation, identity,
            next_gw_id=next_gw_id,
        )
