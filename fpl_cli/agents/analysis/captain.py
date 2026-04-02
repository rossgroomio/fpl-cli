"""Captain agent for ranking captain options."""

from __future__ import annotations

from typing import Any

import httpx

from fpl_cli.agents.base import Agent, AgentResult, AgentStatus
from fpl_cli.agents.common import get_actual_squad_picks
from fpl_cli.api.fpl import FPLClient
from fpl_cli.models.player import Player
from fpl_cli.models.types import CaptainCandidate
from fpl_cli.services.player_scoring import (
    ScoringContext,
    apply_shrinkage,
    build_fixture_matchups,
    build_player_evaluation,
    calculate_captain_score,
    compute_form_trajectory,
    prepare_scoring_data,
)


def _candidate_sort_key(p: Player) -> float:
    return p.form + (p.expected_goals * 2) + p.expected_assists


class CaptainAgent(Agent):
    """Agent for analyzing and ranking captain options.

    Responsibilities:
    - Rank captain candidates by expected output
    - Consider fixture difficulty
    - Factor in form and underlying stats
    - Account for home/away performance
    - Provide reasoning for recommendations
    """

    name = "CaptainAgent"
    description = "Ranks captain options based on fixtures, form, and xG"

    def __init__(self, config: dict[str, Any] | None = None):
        super().__init__(config)
        self.client = FPLClient()

        # Differential thresholds
        self.differential_threshold = config.get("differential_threshold", 10.0) if config else 10.0

    async def close(self) -> None:
        await self.client.close()

    async def _fetch_captain_data(
        self,
    ) -> tuple[
        dict[int, Player],   # player_map
        list[Player],        # all_players
        dict[str, Any] | None,  # next_gw
        dict[int, dict[str, float]],  # understat_by_id
        ScoringContext,       # scoring_context
    ]:
        data = await prepare_scoring_data(
            self.client,
            include_players=True, include_understat=True,
            include_history=True, include_prior=True,
        )
        all_players = data.players or []
        player_map = {p.id: p for p in all_players}
        self._player_histories = data.player_histories or {}
        self._player_priors = data.player_priors

        return (
            player_map,
            all_players,
            data.next_gw,
            data.understat_lookup or {},
            data.scoring_ctx,
        )

    async def _resolve_candidates(
        self,
        context: dict[str, Any] | None,
        player_map: dict[int, Player],
        all_players: list[Player],
        next_gw: dict[str, Any] | None,
    ) -> tuple[list[Player], bool]:
        my_squad_mode = False
        if context and context.get("picks"):
            candidates = [player_map[pid] for pid in context["picks"] if pid in player_map]
            my_squad_mode = True
        elif context and context.get("entry_id"):
            # Fetch user's current squad
            entry_id = context["entry_id"]
            self.log(f"Fetching squad for entry {entry_id}...")
            try:
                # Get picks from latest completed gameweek, checking for Free Hit chip
                last_gw = next_gw["id"] - 1 if next_gw else None
                if last_gw and last_gw > 0:
                    picks_data, last_gw = await get_actual_squad_picks(
                        self.client, entry_id, last_gw, log=self.log
                    )

                    pick_ids = [p["element"] for p in picks_data.get("picks", [])]
                    candidates = [player_map[pid] for pid in pick_ids if pid in player_map]
                    my_squad_mode = True
                    self.log(f"Found {len(candidates)} players in your squad from GW{last_gw}")
                else:
                    # Fallback to global if no previous gameweek
                    candidates = sorted(all_players, key=_candidate_sort_key, reverse=True)[:30]
            except httpx.HTTPError as e:
                self.log_error(f"Could not fetch team picks: {e}")
                # Fallback to global
                candidates = sorted(all_players, key=_candidate_sort_key, reverse=True)[:30]
        else:
            # Default: analyze top players by form/xG (global mode)
            candidates = sorted(all_players, key=_candidate_sort_key, reverse=True)[:30]

        return candidates, my_squad_mode

    async def run(self, context: dict[str, Any] | None = None) -> AgentResult:
        """Analyze and rank captain options.

        Args:
            context: Can contain 'picks' (list of player IDs to consider)
                    or 'entry_id' to fetch picks automatically.

        Returns:
            AgentResult with ranked captain recommendations.
        """
        self.log("Analyzing captain options...")

        try:
            player_map, all_players, next_gw, understat_by_id, scoring_context = (
                await self._fetch_captain_data()
            )
            candidates, my_squad_mode = await self._resolve_candidates(
                context, player_map, all_players, next_gw,
            )

            self.log(f"Analyzing {len(candidates)} captain candidates")

            # Score each candidate
            next_gw_id = next_gw["id"] if next_gw else 38
            scored_candidates = []
            for player in candidates:
                score_data = self._score_captain_candidate(
                    player, scoring_context,
                    understat_by_id=understat_by_id,
                    next_gw_id=next_gw_id,
                )
                if score_data:
                    scored_candidates.append(score_data)

            # Apply early-season shrinkage
            apply_shrinkage(scored_candidates, "captain_score", self._player_priors, next_gw_id)

            # Sort by captain score
            scored_candidates.sort(key=lambda x: x["captain_score"], reverse=True)

            # Generate top picks with reasoning
            top_picks = scored_candidates[:10]

            # Find differential captain picks (low ownership but high potential)
            differential_picks = [
                p for p in scored_candidates
                if p.get("ownership", 100) < self.differential_threshold
                and p["captain_score"] >= 15  # Minimum viable captain score
            ]
            differential_picks.sort(key=lambda x: x["captain_score"], reverse=True)

            self.log_success(f"Ranked {len(scored_candidates)} captain options")

            return self._create_result(
                AgentStatus.SUCCESS,
                data={
                    "gameweek": next_gw["id"] if next_gw else None,
                    "deadline": next_gw.get("deadline_time") if next_gw else None,
                    "top_picks": top_picks,
                    "differential_picks": differential_picks[:5],
                    "all_candidates": scored_candidates,
                    "recommendation": top_picks[0] if top_picks else None,
                    "my_squad_mode": my_squad_mode,
                },
                message=f"Top captain pick: {top_picks[0]['player_name'] if top_picks else 'None'}",
            )

        except Exception as e:  # noqa: BLE001 — agent top-level handler
            self.log_error(f"Failed to analyze captains: {e}")
            return self._create_result(
                AgentStatus.FAILED,
                message="Failed to analyze captain options",
                errors=[str(e)],
            )

    def _score_captain_candidate(
        self,
        player: Player,
        context: ScoringContext,
        *,
        understat_by_id: dict[int, dict[str, float]] | None = None,
        next_gw_id: int = 38,
    ) -> CaptainCandidate | None:
        """Score a player as a captain candidate via the scoring engine."""
        team = context.team_map.get(player.team_id)
        if not team:
            return None

        fixture_matchups = build_fixture_matchups(
            player.team_id, player.position_name, context,
        )
        if not fixture_matchups:
            return None  # Team has blank gameweek

        enrichment: dict[str, Any] = {"team_short": team.short_name}
        if understat_by_id:
            us_data = understat_by_id.get(player.id)
            if us_data:
                enrichment.update(us_data)

        # xGI_per_90 fallback for players without Understat data
        minutes_safe = max(player.minutes, 1)
        enrichment["xGI_per_90"] = (player.expected_goals + player.expected_assists) / minutes_safe * 90

        # Form trajectory from per-GW history
        histories = getattr(self, "_player_histories", {})
        history = histories.get(player.id, [])
        if history:
            enrichment["form_trajectory"] = compute_form_trajectory(history, next_gw_id)

        # Bayesian prior confidence
        priors = getattr(self, "_player_priors", None)
        if priors:
            prior = priors.get(player.id)
            if prior:
                enrichment["prior_confidence"] = prior.confidence

        evaluation, identity = build_player_evaluation(
            player,
            enrichment=enrichment,
            fixture_matchups=fixture_matchups,
        )

        return calculate_captain_score(evaluation, identity, next_gw_id=next_gw_id)
