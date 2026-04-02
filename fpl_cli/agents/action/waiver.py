"""Waiver agent for recommending and managing draft waiver claims."""

from __future__ import annotations

from collections import defaultdict
from typing import Any, cast

from fpl_cli.agents.base import Agent, AgentResult, AgentStatus
from fpl_cli.agents.common import (
    enrich_player,
    fetch_understat_lookup,
)
from fpl_cli.api.fpl import FPLClient
from fpl_cli.api.fpl_draft import FPLDraftClient
from fpl_cli.models.types import EnrichedPlayer, WaiverTarget
from fpl_cli.services.player_scoring import (
    apply_shrinkage,
    build_player_evaluation,
    calculate_waiver_score,
    compute_aggregate_matchup,
    compute_form_trajectory,
    prepare_scoring_data,
)


class WaiverAgent(Agent):
    """Agent for analyzing and recommending waiver claims.

    Responsibilities:
    - Analyze available players for waiver potential
    - Compare against current team needs
    - Rank waiver targets with reasoning
    - Track waiver deadline
    """

    name = "WaiverAgent"
    description = "Recommends waiver claims for draft leagues"

    def __init__(self, config: dict[str, Any] | None = None):
        super().__init__(config)
        self.client = FPLDraftClient()
        self.fpl_client = FPLClient()
        self.league_id = config.get("draft_league_id") if config else None
        self.entry_id = config.get("draft_entry_id") if config else None

    async def close(self) -> None:
        await self.client.close()
        await self.fpl_client.close()

    async def run(self, context: dict[str, Any] | None = None) -> AgentResult:
        """Analyze and recommend waiver targets.

        Args:
            context: Can contain:
                - 'league_id': Draft league ID
                - 'entry_id': Your draft entry ID
                - 'available_players': Pre-fetched available players
                - 'current_team': Your current squad

        Returns:
            AgentResult with waiver recommendations.
        """
        league_id = (context or {}).get("league_id") or self.league_id
        entry_id = (context or {}).get("entry_id") or self.entry_id

        if not league_id:
            return self._create_result(
                AgentStatus.FAILED,
                message="No draft league ID provided",
                errors=["Set draft_league_id in config or provide in context"],
            )

        self.log(f"Analyzing waiver options for league {league_id}...")

        try:
            # Fetch data
            bootstrap = await self.client.get_bootstrap_static()
            teams_data = bootstrap.get("teams", [])
            team_map = {t["id"]: t for t in teams_data}

            # Get available players
            available = await self.client.get_available_players(league_id, bootstrap)

            # Get recently released players (independent of entry_id)
            recent_releases: list[dict[str, Any]] = []
            league_details: dict[str, Any] = {}
            try:
                releases = await self.client.get_recent_releases(league_id, bootstrap)
                # Build entry->name map to resolve who dropped each player
                league_details = await self.client.get_league_details(league_id)
                league_entries = league_details.get("league_entries", [])
                entry_name_map = {
                    e.get("entry_id"): f"{e.get('player_first_name', '')} {e.get('player_last_name', '')}".strip()
                    for e in league_entries
                }
                recent_releases = [
                    {
                        **enrich_player(self.client.parse_player(r["player"]), team_map),
                        "dropped_by": entry_name_map.get(r["dropped_by"], str(r["dropped_by"])),
                        "gameweek": r["gameweek"],
                    }
                    for r in releases[:20]
                ]
            except Exception as e:  # noqa: BLE001 — best-effort enrichment
                self.log_warning(f"Could not fetch recent releases: {e}")

            # Parse players
            parsed_available = [
                enrich_player(self.client.parse_player(p), team_map, include_availability=False)
                for p in available
            ]

            # Enrich with Understat data
            us_lookup = await fetch_understat_lookup(
                cast(list[dict[str, Any]], parsed_available),
                lambda p: p.get("team_name", ""),
            )
            for i, us_match in us_lookup.items():
                parsed_available[i]["npxG_per_90"] = us_match.get("npxG_per_90")
                parsed_available[i]["xGChain_per_90"] = us_match.get("xGChain_per_90")
                parsed_available[i]["penalty_xG_per_90"] = us_match.get("penalty_xG_per_90")

            # Get current squad if entry_id provided
            current_squad = []
            squad_by_position = {"GK": [], "DEF": [], "MID": [], "FWD": []}

            if entry_id:
                try:
                    # Reuse league_details from recent releases fetch, or fetch fresh
                    if not league_details:
                        league_details = await self.client.get_league_details(league_id)
                    league_entries = league_details.get("league_entries", [])

                    # Find matching entry - entry_id could be entry_id or id
                    # The API uses entry_id for fetching picks
                    draft_entry_id = None
                    for entry in league_entries:
                        if entry.get("entry_id") == entry_id or entry.get("id") == entry_id:
                            draft_entry_id = entry.get("entry_id")  # Use entry_id for API calls
                            self.log(f"Found team: {entry.get('entry_name')}")
                            break

                    if draft_entry_id:
                        # Get current gameweek
                        game_data = await self.client.get_game_state()
                        current_gw = game_data.get("current_event", 1)

                        # Fetch picks for current gameweek
                        picks_data = await self.client.get_entry_picks(draft_entry_id, current_gw)
                        player_ids = [p.get("element") for p in picks_data.get("picks", [])]

                        player_map = {p["id"]: p for p in bootstrap.get("elements", [])}
                        for pid in player_ids:
                            if pid in player_map:
                                player = enrich_player(
                                    self.client.parse_player(player_map[pid]),
                                    team_map,
                                    include_availability=False,
                                )
                                current_squad.append(player)
                                pos = player.get("position", "???")
                                if pos in squad_by_position:
                                    squad_by_position[pos].append(player)
                    else:
                        self.log_warning(f"Entry ID {entry_id} not found in league entries")
                except Exception as e:  # noqa: BLE001 — best-effort enrichment
                    self.log_warning(f"Could not fetch current squad: {e}")

            # Fetch fixture data and build shared scoring context
            data = await prepare_scoring_data(
                self.fpl_client, include_players=True,
                include_history=True, include_prior=True,
            )
            next_gw_id = data.next_gw_id
            self._player_histories = data.player_histories or {}
            self._player_priors = data.player_priors
            scoring_ctx = data.scoring_ctx

            # Enrich available players with matchup and FDR
            matchup_cache: dict[tuple[int, str], float] = {}
            for player in parsed_available:
                tid = player.get("team_id", 0)
                pos = player.get("position", "MID")
                matchup_avg_3gw, positional_fdr = compute_aggregate_matchup(
                    tid, pos, scoring_ctx, matchup_cache=matchup_cache,
                )
                if matchup_avg_3gw is not None:
                    player["matchup_avg_3gw"] = matchup_avg_3gw
                if positional_fdr is not None:
                    player["positional_fdr"] = positional_fdr

            # Score and rank waiver targets
            waiver_targets = self._rank_waiver_targets(
                parsed_available,
                current_squad,
                squad_by_position,
                next_gw_id=next_gw_id,
            )

            # Get waiver priority
            waiver_order = await self.client.get_waiver_order(league_id)

            # Find our position in waiver order
            our_waiver_position = None
            if entry_id:
                for i, team in enumerate(waiver_order, 1):
                    if team.get("entry_id") == entry_id:
                        our_waiver_position = i
                        break

            self.log_success(f"Found {len(waiver_targets)} potential waiver targets")

            return self._create_result(
                AgentStatus.SUCCESS,
                data={
                    "league_id": league_id,
                    "entry_id": entry_id,
                    "waiver_position": our_waiver_position,
                    "total_waiver_teams": len(waiver_order),
                    "top_targets": waiver_targets[:15],
                    "targets_by_position": self._group_by_position(waiver_targets[:30]),
                    "current_squad": current_squad,
                    "squad_weaknesses": self._identify_weaknesses(squad_by_position),
                    "recommendations": self._generate_recommendations(
                        waiver_targets,
                        squad_by_position,
                    ),
                    "recent_releases": recent_releases,
                },
                message=f"Top waiver target: {waiver_targets[0]['player_name'] if waiver_targets else 'None'}",
            )

        except Exception as e:  # noqa: BLE001 — agent top-level handler
            self.log_error(f"Failed to analyze waivers: {e}")
            return self._create_result(
                AgentStatus.FAILED,
                message="Failed to analyze waiver options",
                errors=[str(e)],
            )

    def _get_team_exposure(
        self,
        squad_by_position: dict[str, list],
    ) -> dict[str, int]:
        """Count players per team in current squad."""
        team_counts: dict[str, int] = defaultdict(int)
        for players in squad_by_position.values():
            for p in players:
                team_short = p.get("team_short", "???")
                team_counts[team_short] += 1
        return dict(team_counts)

    def _check_team_exposure(
        self,
        target: WaiverTarget,
        drop_candidate: dict[str, Any] | None,
        team_counts: dict[str, int],
    ) -> tuple[int, str | None]:
        """Check resulting team exposure after a waiver.

        Returns:
            (new_count, warning_message or None)
        """
        target_team = target.get("team_short", "???")
        drop_team = drop_candidate.get("team_short") if drop_candidate else None

        current = team_counts.get(target_team, 0)

        # If dropping from same team, net change is 0
        if drop_team == target_team:
            return current, None

        new_count = current + 1

        if new_count >= 4:
            return new_count, f"Heavy exposure: {new_count} {target_team} players"
        elif new_count == 3:
            return new_count, f"Triple-up: 3 {target_team} players"

        return new_count, None

    def _rank_waiver_targets(
        self,
        available: list[EnrichedPlayer],
        current_squad: list[dict[str, Any]],
        squad_by_position: dict[str, list],
        next_gw_id: int = 38,
    ) -> list[WaiverTarget]:
        """Rank available players as waiver targets."""
        scored_players = []
        team_counts = self._get_team_exposure(squad_by_position)

        for player in available:
            score = self._calculate_waiver_score(
                player, squad_by_position, team_counts, next_gw_id=next_gw_id,
            )
            reasons = self._generate_target_reasons(player, squad_by_position)

            scored_players.append({
                **player,  # superset of WaiverTarget keys via EnrichedPlayer
                "waiver_score": score,
                "reasons": reasons,
            })

        # Apply early-season shrinkage
        apply_shrinkage(scored_players, "waiver_score", self._player_priors, next_gw_id)

        # Sort by waiver score
        scored_players.sort(key=lambda p: p["waiver_score"], reverse=True)
        return scored_players

    def _calculate_waiver_score(
        self,
        player: EnrichedPlayer,
        squad_by_position: dict[str, list],
        team_counts: dict[str, int] | None = None,
        next_gw_id: int = 38,
    ) -> int:
        """Calculate a waiver priority score via the player scoring engine."""
        enrichment: dict[str, Any] = {}
        histories = getattr(self, "_player_histories", {})
        history = histories.get(player.get("id", 0), [])
        if history:
            enrichment["form_trajectory"] = compute_form_trajectory(history, next_gw_id)
        priors = getattr(self, "_player_priors", None)
        if priors:
            prior = priors.get(player.get("id", 0))
            if prior:
                enrichment["prior_confidence"] = prior.confidence
        evaluation, _ = build_player_evaluation(
            player,
            enrichment=enrichment,
            matchup_avg_3gw=player.get("matchup_avg_3gw"),
            positional_fdr=player.get("positional_fdr"),
        )
        return calculate_waiver_score(
            evaluation,
            squad_by_position=squad_by_position,
            team_counts=team_counts,
            next_gw_id=next_gw_id,
        )

    def _generate_target_reasons(
        self,
        player: EnrichedPlayer,
        squad_by_position: dict[str, list],
    ) -> list[str]:
        """Generate reasons why a player is a good waiver target."""
        reasons = []

        form = player.get("form", 0)
        if form >= 6:
            reasons.append(f"Excellent form ({form})")
        elif form >= 4:
            reasons.append(f"Good form ({form})")

        ppg = player.get("ppg", 0)
        if ppg >= 5:
            reasons.append(f"Strong PPG ({ppg:.1f})")

        xgi = player.get("xGI_per_90", 0)
        if xgi >= 0.4:
            reasons.append(f"High xGI ({xgi:.2f}/90)")

        minutes = player.get("minutes", 0)
        if minutes >= 1500:
            reasons.append("Regular starter")

        # Check if fills a need
        pos_name = player.get("position", "???")
        if pos_name in squad_by_position:
            position_players = squad_by_position[pos_name]
            if position_players:
                avg_form = sum(p.get("form", 0) for p in position_players) / len(position_players)
                if form > avg_form + 1:
                    reasons.append(f"Better than current {pos_name} options")

        if not reasons:
            reasons.append("Depth option")

        return reasons

    def _group_by_position(
        self,
        players: list[WaiverTarget],
    ) -> dict[str, list[WaiverTarget]]:
        """Group players by position."""
        by_position = {"GK": [], "DEF": [], "MID": [], "FWD": []}

        for player in players:
            pos = player.get("position", "???")
            if pos in by_position:
                by_position[pos].append(player)

        return by_position

    def _identify_weaknesses(
        self,
        squad_by_position: dict[str, list],
    ) -> list[dict[str, Any]]:
        """Identify weak positions in the squad."""
        weaknesses = []

        for pos, players in squad_by_position.items():
            if not players:
                weaknesses.append({
                    "position": pos,
                    "severity": "high",
                    "reason": "No players at this position",
                })
            else:
                avg_form = sum(p.get("form", 0) for p in players) / len(players)
                if avg_form < 3:
                    weaknesses.append({
                        "position": pos,
                        "severity": "medium",
                        "reason": f"Low average form ({avg_form:.1f})",
                        "current_players": [p.get("player_name") for p in players],
                    })

        return weaknesses

    def _calculate_drop_priority(self, player: dict[str, Any]) -> float:
        """Calculate drop priority score (higher = more droppable).

        Priority order:
        1. Suspended players (status="s") - highest priority to drop
        2. Unavailable with 0% chance of playing
        3. Injured with low chance (<50%)
        4. Injured with medium chance (50-75%)
        5. Available players sorted by form (lowest form = higher priority)
        """
        status = player.get("status", "a")
        chance = player.get("chance_of_playing_next_round")
        form = player.get("form", 0)

        # Suspended players are highest priority to drop
        if status == "s":
            return 1000

        # Unavailable with 0% chance
        if status != "a" and chance == 0:
            return 500

        # Injured with low chance (<50%)
        if status != "a" and chance is not None and chance < 50:
            return 200 + (50 - chance)

        # Injured with medium chance (50-75%)
        if status != "a" and chance is not None and chance < 75:
            return 100 + (75 - chance)

        # Available players - inverse form (lower form = higher drop score)
        return 10 - min(form, 10)

    def _generate_recommendations(
        self,
        waiver_targets: list[WaiverTarget],
        squad_by_position: dict[str, list],
    ) -> list[dict[str, Any]]:
        """Generate specific waiver recommendations."""
        recommendations = []
        team_counts = self._get_team_exposure(squad_by_position)

        # Find best target for each position
        seen_positions = set()

        for target in waiver_targets[:20]:
            pos = target.get("position", "???")

            if pos not in seen_positions and pos in squad_by_position:
                squad_players = squad_by_position[pos]

                # Find worst player to drop (highest drop priority)
                drop_candidate = None
                if squad_players:
                    drop_candidate = max(
                        squad_players, key=lambda p: self._calculate_drop_priority(p)
                    )

                # Determine why this player is being dropped
                drop_reason = None
                if drop_candidate:
                    drop_status = drop_candidate.get("status", "a")
                    drop_chance = drop_candidate.get("chance_of_playing_next_round")
                    if drop_status == "s":
                        drop_reason = "Suspended"
                    elif drop_status != "a" and drop_chance == 0:
                        drop_reason = "Unavailable (0%)"
                    elif drop_status != "a" and drop_chance is not None and drop_chance < 75:
                        drop_reason = f"Doubtful ({drop_chance}%)"
                    else:
                        drop_reason = f"Low form ({drop_candidate.get('form', 0)})"

                # Check team exposure
                new_count, exposure_warning = self._check_team_exposure(
                    target, drop_candidate, team_counts
                )

                rec: dict[str, Any] = {
                    "priority": len(recommendations) + 1,
                    "target": {
                        "name": target.get("player_name"),
                        "team": target.get("team_short"),
                        "position": pos,
                        "form": target.get("form"),
                        "waiver_score": target.get("waiver_score"),
                    },
                    "drop": {
                        "name": drop_candidate.get("player_name") if drop_candidate else None,
                        "form": drop_candidate.get("form") if drop_candidate else None,
                        "reason": drop_reason,
                    } if drop_candidate else None,
                    "reasons": target.get("reasons", []),
                }

                if exposure_warning:
                    rec["exposure"] = {
                        "team": target.get("team_short"),
                        "count_after": new_count,
                        "warning": exposure_warning,
                    }

                recommendations.append(rec)
                seen_positions.add(pos)

            if len(recommendations) >= 5:
                break

        return recommendations
