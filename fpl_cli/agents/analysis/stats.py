"""Stats agent for fetching underlying statistics (xG, xA, etc.)."""

from __future__ import annotations

from typing import Any

from fpl_cli.agents.base import Agent, AgentResult, AgentStatus
from fpl_cli.agents.common import fetch_understat_lookup
from fpl_cli.api.fpl import FPLClient
from fpl_cli.models.types import PlayerStats
from fpl_cli.services.matchup import calculate_matchup_score
from fpl_cli.services.player_scoring import (
    apply_shrinkage,
    build_player_evaluation,
    calculate_differential_score,
    calculate_target_score,
    compute_aggregate_matchup,
    compute_form_trajectory,
    prepare_scoring_data,
)

RECOGNISED_VIEWS: frozenset[str] = frozenset({
    "underperformers",
    "overperformers",
    "value_picks",
    "top_xgi_per_90",
    "differentials",
    "targets",
})


class StatsAgent(Agent):
    """Agent for fetching underlying player statistics.

    Responsibilities:
    - Fetch xG, xA data from FPL API
    - Identify over/underperforming players
    - Calculate per-90 metrics
    - Find value picks based on underlying stats
    """

    name = "StatsAgent"
    description = "Fetches underlying statistics (xG, xA) from FPL API"

    def __init__(self, config: dict[str, Any] | None = None):
        super().__init__(config)
        self.client = FPLClient()

        # Gameweek window (0 or None = whole season)
        self.gameweeks = config.get("gameweeks", 6) if config else 6

        # Minimum minutes scales with gameweek window (60 min/GW)
        if self.gameweeks and self.gameweeks > 0:
            self.min_minutes = config.get("min_minutes", 60 * self.gameweeks) if config else 60 * self.gameweeks
        else:
            self.min_minutes = config.get("min_minutes", 450) if config else 450

        # Differential thresholds (configurable)
        self.differential_threshold = config.get("differential_threshold", 5.0) if config else 5.0  # < 5% owned
        # < 15% owned
        self.semi_differential_threshold = config.get("semi_differential_threshold", 15.0) if config else 15.0

        # View selection: which analysis views to compute
        raw_views = config.get("views") if config else None
        if raw_views:
            unknown = set(raw_views) - RECOGNISED_VIEWS
            if unknown:
                msg = f"Unrecognised view(s): {sorted(unknown)}. Valid: {sorted(RECOGNISED_VIEWS)}"
                raise ValueError(msg)
            self.views: frozenset[str] = frozenset(raw_views)
        else:
            self.views = RECOGNISED_VIEWS

        # Set by run(); used by scoring methods for early-season mins_factor guard
        self._next_gw_id: int = 38

    async def close(self) -> None:
        await self.client.close()

    async def run(self, context: dict[str, Any] | None = None) -> AgentResult:
        """Fetch and analyze underlying statistics.

        Returns:
            AgentResult with:
            - players: All players with xG/xA data
            - underperformers: Players with goals < xG (due a rise)
            - overperformers: Players with goals > xG (regression candidates)
            - value_picks: High xGI per 90 at low ownership
        """
        import asyncio

        # Determine analysis window
        window_label = f"last {self.gameweeks} GWs" if self.gameweeks else "whole season"
        self.log(f"Fetching underlying statistics from FPL API ({window_label})...")

        try:
            players = await self.client.get_players()
            teams = await self.client.get_teams()
            team_map = {t.id: t for t in teams}

            # Get current gameweek for window calculation
            current_gw = await self.client.get_current_gameweek()
            current_gw_id = current_gw["id"] if current_gw else 1

            # Use gameweek window or whole season
            use_window = self.gameweeks and self.gameweeks > 0 and current_gw_id > self.gameweeks

            if use_window:
                # Calculate gameweek range for window
                start_gw = current_gw_id - self.gameweeks + 1
                end_gw = current_gw_id
                self.log(f"Analyzing GW{start_gw}-{end_gw} ({self.gameweeks} gameweeks)")

                # First pass: filter to players with any significant minutes this season
                candidates = [p for p in players if p.minutes >= 90]
                self.log(f"Fetching history for {len(candidates)} players...")

                # Fetch history for candidates in batches
                player_histories = {}
                batch_size = 50
                for i in range(0, len(candidates), batch_size):
                    batch = candidates[i:i + batch_size]
                    tasks = [self.client.get_player_detail(p.id) for p in batch]
                    results = await asyncio.gather(*tasks, return_exceptions=True)
                    for p, result in zip(batch, results):
                        if isinstance(result, dict):
                            player_histories[p.id] = result.get("history", [])

                # Reuse already-fetched histories for trajectory pre-computation
                self._player_histories = player_histories

                # Calculate windowed stats for each player
                player_stats = []
                for p in candidates:
                    history = player_histories.get(p.id, [])
                    windowed = self._calculate_windowed_stats(p, history, start_gw, end_gw, team_map)
                    if windowed and windowed["minutes"] >= self.min_minutes:
                        player_stats.append(windowed)

                self.log(f"Found {len(player_stats)} players with {self.min_minutes}+ minutes in window")
            else:
                # Whole season: use cumulative stats from bootstrap
                self.log("Using whole season cumulative stats")
                qualified_players = [p for p in players if p.minutes >= self.min_minutes]
                self.log(f"Found {len(qualified_players)} players with {self.min_minutes}+ minutes")
                player_stats = [self._calculate_player_stats(p, team_map) for p in qualified_players]

            # Enrich with Understat data
            player_obj_map = {p.id: p for p in players}
            us_adapter = []
            for ps in player_stats:
                obj = player_obj_map.get(ps["id"])
                if obj:
                    us_adapter.append({
                        "player_name": obj.web_name,
                        "position": ps.get("position"),
                        "minutes": ps.get("minutes"),
                        "_team_id": obj.team_id,
                    })
                else:
                    us_adapter.append({"player_name": "", "position": None, "minutes": None, "_team_id": 0})
            us_lookup = await fetch_understat_lookup(
                us_adapter,
                lambda p: (team_map.get(p["_team_id"]).name  # type: ignore[union-attr]
                           if team_map.get(p["_team_id"]) else ""),
                log=self.log,
            )
            for i, ps in enumerate(player_stats):
                self._merge_understat_data(ps, us_lookup.get(i))

            # Fetch fixture data and build shared scoring context
            needs_history = not hasattr(self, "_player_histories")
            scoring_data = await prepare_scoring_data(
                self.client, include_players=True,
                include_history=needs_history, include_prior=True,
            )
            self._next_gw_id = scoring_data.next_gw_id
            self._player_priors = scoring_data.player_priors
            if needs_history:
                self._player_histories = scoring_data.player_histories or {}
            scoring_ctx = scoring_data.scoring_ctx
            team_fixtures = scoring_ctx.team_fixture_map
            team_form_by_id = scoring_ctx.team_form_by_id or {}
            matchup_cache: dict[tuple[int, str], float] = {}

            # Enrich player_stats with matchup data
            player_team_map = {}
            for p in players:
                player_team_map[p.id] = p.team_id

            for ps in player_stats:
                player_id = ps.get("id")
                team_id = player_team_map.get(player_id)

                if not team_id:
                    ps["matchup_score"] = 5.0
                    ps["next_opponent"] = None
                    continue

                fixtures = team_fixtures.get(team_id, [])

                if not fixtures:
                    ps["matchup_score"] = 5.0
                    ps["next_opponent"] = None
                    continue

                # Use first fixture for per-fixture matchup breakdown
                f_data = fixtures[0]
                fixture = f_data["fixture"]
                is_home = f_data["is_home"]

                opponent_id = fixture.away_team_id if is_home else fixture.home_team_id
                opponent_team = team_map.get(opponent_id)

                player_team_form = team_form_by_id.get(team_id, {})
                opponent_form = team_form_by_id.get(opponent_id, {})

                if player_team_form and opponent_form:
                    matchup = calculate_matchup_score(
                        player_team_form,
                        opponent_form,
                        ps.get("position", "MID"),
                        is_home,
                    )
                    ps["matchup_score"] = matchup["matchup_score"]
                    ps["attack_matchup"] = matchup["attack_matchup"]
                    ps["defence_matchup"] = matchup["defence_matchup"]
                    ps["form_differential"] = matchup["form_differential"]
                    ps["position_differential"] = matchup["position_differential"]
                else:
                    ps["matchup_score"] = 5.0

                ps["next_opponent"] = (
                    opponent_team.short_name.upper() if is_home else opponent_team.short_name.lower()
                ) if opponent_team else None

                # Aggregate matchup (3-GW weighted + positional FDR) via shared helper
                position = ps.get("position", "MID")
                matchup_avg_3gw, positional_fdr = compute_aggregate_matchup(
                    team_id, position, scoring_ctx, matchup_cache=matchup_cache,
                )
                if matchup_avg_3gw is not None:
                    ps["matchup_avg_3gw"] = matchup_avg_3gw
                if positional_fdr is not None:
                    ps["positional_fdr"] = positional_fdr

            # Pre-compute form trajectory for all players (avoid recomputing per scorer)
            for ps in player_stats:
                history = self._player_histories.get(ps.get("id", 0), [])
                if history:
                    ps["form_trajectory"] = compute_form_trajectory(history, self._next_gw_id)

            # Analyze the data (only compute requested views)
            data: dict[str, Any] = {
                "total_players": len(players),
                "qualified_players": len(player_stats),
                "players": player_stats,
                "gameweeks": self.gameweeks if use_window else None,
                "window_label": window_label,
            }
            if "underperformers" in self.views:
                data["underperformers"] = self._find_underperformers(player_stats)
            if "overperformers" in self.views:
                data["overperformers"] = self._find_overperformers(player_stats)
            if "value_picks" in self.views:
                data["value_picks"] = self._find_value_picks(player_stats)
            if "top_xgi_per_90" in self.views:
                data["top_xgi_per_90"] = self._get_top_xgi(player_stats)
            if "differentials" in self.views:
                data["differentials"] = self._find_differentials(player_stats)
            if "targets" in self.views:
                data["targets"] = self._find_targets(player_stats)

            self.log_success(f"Analyzed {len(player_stats)} players ({window_label})")

            return self._create_result(
                AgentStatus.SUCCESS,
                data=data,
                message=f"Analyzed {len(player_stats)} players ({window_label})",
            )

        except Exception as e:  # noqa: BLE001 — agent top-level handler
            self.log_error(f"Failed to fetch stats: {e}")
            return self._create_result(
                AgentStatus.FAILED,
                message="Failed to fetch underlying statistics",
                errors=[str(e)],
            )

    def _calculate_player_stats(
        self,
        player,
        team_map: dict[int, Any],
    ) -> PlayerStats:  # enrichment fields added by run()
        """Calculate underlying stats for a player."""
        team = team_map.get(player.team_id)
        minutes = player.minutes

        # Calculate per 90 metrics
        xg_per_90 = (player.expected_goals / minutes) * 90 if minutes > 0 else 0
        xa_per_90 = (player.expected_assists / minutes) * 90 if minutes > 0 else 0
        xgi_per_90 = xg_per_90 + xa_per_90

        # Calculate over/underperformance
        goals_minus_xg = player.goals_scored - player.expected_goals
        assists_minus_xa = player.assists - player.expected_assists
        gi = player.goals_scored + player.assists
        xgi = player.expected_goals + player.expected_assists
        gi_minus_xgi = gi - xgi

        return {  # type: ignore[return-value]  # enrichment fields added by run()
            "id": player.id,
            "player_name": player.web_name,
            "team_short": team.short_name if team else "???",
            "position": player.position_name,
            "price": player.price,
            "ownership": player.selected_by_percent,
            "minutes": minutes,
            "goals": player.goals_scored,
            "assists": player.assists,
            "GI": gi,
            "xG": round(player.expected_goals, 2),
            "xA": round(player.expected_assists, 2),
            "xGI": round(player.expected_goals + player.expected_assists, 2),
            "xG_per_90": round(xg_per_90, 2),
            "xA_per_90": round(xa_per_90, 2),
            "xGI_per_90": round(xgi_per_90, 2),
            "goals_minus_xG": round(goals_minus_xg, 2),
            "assists_minus_xA": round(assists_minus_xa, 2),
            "GI_minus_xGI": round(gi_minus_xgi, 2),
            "form": player.form,
            "total_points": player.total_points,
            "ppg": player.points_per_game,
            "dc_per_90": player.defensive_contribution_per_90,
            "appearances": player.appearances,
        }

    def _calculate_windowed_stats(
        self,
        player,
        history: list[dict[str, Any]],
        start_gw: int,
        end_gw: int,
        team_map: dict[int, Any],
    ) -> PlayerStats | None:  # enrichment fields added by run()
        """Calculate stats for a specific gameweek window from player history."""
        team = team_map.get(player.team_id)

        # Filter history to the specified gameweek range
        window_data = [
            h for h in history
            if start_gw <= h.get("round", 0) <= end_gw
        ]

        if not window_data:
            return None

        # Sum up stats from the window
        minutes = sum(h.get("minutes", 0) for h in window_data)
        goals = sum(h.get("goals_scored", 0) for h in window_data)
        assists = sum(h.get("assists", 0) for h in window_data)
        xg = sum(float(h.get("expected_goals", 0)) for h in window_data)
        xa = sum(float(h.get("expected_assists", 0)) for h in window_data)
        total_points = sum(h.get("total_points", 0) for h in window_data)

        if minutes == 0:
            return None

        # Calculate per 90 metrics
        xg_per_90 = (xg / minutes) * 90
        xa_per_90 = (xa / minutes) * 90
        xgi_per_90 = xg_per_90 + xa_per_90

        # Calculate over/underperformance
        gi = goals + assists
        xgi = xg + xa
        gi_minus_xgi = gi - xgi

        # Calculate PPG for window
        games_played = len([h for h in window_data if h.get("minutes", 0) > 0])
        ppg = total_points / games_played if games_played > 0 else 0

        return {  # type: ignore[return-value]  # enrichment fields added by run()
            "id": player.id,
            "player_name": player.web_name,
            "team_short": team.short_name if team else "???",
            "position": player.position_name,
            "price": player.price,
            "ownership": player.selected_by_percent,
            "minutes": minutes,
            "goals": goals,
            "assists": assists,
            "GI": gi,
            "xG": round(xg, 2),
            "xA": round(xa, 2),
            "xGI": round(xgi, 2),
            "xG_per_90": round(xg_per_90, 2),
            "xA_per_90": round(xa_per_90, 2),
            "xGI_per_90": round(xgi_per_90, 2),
            "goals_minus_xG": round(goals - xg, 2),
            "assists_minus_xA": round(assists - xa, 2),
            "GI_minus_xGI": round(gi_minus_xgi, 2),
            "form": player.form,  # Use current form from bootstrap
            "total_points": total_points,
            "ppg": round(ppg, 2),
            "dc_per_90": player.defensive_contribution_per_90,
            "appearances": player.appearances,
        }

    def _merge_understat_data(
        self,
        player_stats: PlayerStats,
        us_match: dict[str, Any] | None,
    ) -> PlayerStats:
        """Merge Understat metrics into player stats dict.

        Falls back to None values if no Understat match provided.
        """
        if us_match:
            player_stats["npxG_per_90"] = us_match.get("npxG_per_90")
            player_stats["xGChain_per_90"] = us_match.get("xGChain_per_90")
            player_stats["xGBuildup_per_90"] = us_match.get("xGBuildup_per_90")
            player_stats["penalty_xG"] = us_match.get("penalty_xG")
            player_stats["penalty_xG_per_90"] = us_match.get("penalty_xG_per_90")
        else:
            player_stats["npxG_per_90"] = None
            player_stats["xGChain_per_90"] = None
            player_stats["xGBuildup_per_90"] = None
            player_stats["penalty_xG"] = None
            player_stats["penalty_xG_per_90"] = None

        return player_stats

    def _find_underperformers(
        self,
        players: list[PlayerStats],
        threshold: float = -2.0,
    ) -> list[dict[str, Any]]:
        """Find players underperforming their xGI (due a positive regression).

        These are players who have had bad luck converting chances and
        creating assists, and are likely to improve going forward.
        """
        underperformers = [
            {
                "player_name": p["player_name"],
                "team_short": p["team_short"],
                "position": p["position"],
                "price": p["price"],
                "GI": p["GI"],
                "xGI": p["xGI"],
                "difference": p["GI_minus_xGI"],
                "xGI_per_90": p["xGI_per_90"],
                "minutes": p["minutes"],
            }
            for p in players
            if p["GI_minus_xGI"] <= threshold
        ]

        # Sort by biggest underperformance
        underperformers.sort(key=lambda x: x["difference"])
        return underperformers[:15]

    def _find_overperformers(
        self,
        players: list[PlayerStats],
        threshold: float = 3.0,
    ) -> list[dict[str, Any]]:
        """Find players overperforming their xGI (regression candidates).

        These are players who have been lucky and may see reduced
        output going forward.
        """
        overperformers = [
            {
                "player_name": p["player_name"],
                "team_short": p["team_short"],
                "position": p["position"],
                "price": p["price"],
                "GI": p["GI"],
                "xGI": p["xGI"],
                "difference": p["GI_minus_xGI"],
                "xGI_per_90": p["xGI_per_90"],
                "minutes": p["minutes"],
            }
            for p in players
            if p["GI_minus_xGI"] >= threshold
        ]

        # Sort by biggest overperformance
        overperformers.sort(key=lambda x: x["difference"], reverse=True)
        return overperformers[:15]

    def _find_value_picks(
        self,
        players: list[PlayerStats],
    ) -> list[dict[str, Any]]:
        """Find value picks: high xGI per 90 at low ownership/price."""
        # Filter to under-owned players with good xGI
        value_candidates = [
            p for p in players
            if p["ownership"] < 15 and p["xGI_per_90"] > 0.3
        ]

        # Sort by xGI per 90
        value_candidates.sort(key=lambda x: x["xGI_per_90"], reverse=True)

        return [
            {
                "player_name": p["player_name"],
                "team_short": p["team_short"],
                "position": p["position"],
                "price": p["price"],
                "ownership": p["ownership"],
                "xG": p["xG"],
                "xA": p["xA"],
                "xGI_per_90": p["xGI_per_90"],
                "goals": p["goals"],
                "assists": p["assists"],
                "minutes": p["minutes"],
            }
            for p in value_candidates[:10]
        ]

    def _get_top_xgi(
        self,
        players: list[PlayerStats],
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        """Get top players by xGI (xG + xA) per 90 minutes."""
        sorted_players = sorted(
            players,
            key=lambda x: x["xGI_per_90"],
            reverse=True,
        )

        return [
            {
                "player_name": p["player_name"],
                "team_short": p["team_short"],
                "position": p["position"],
                "price": p["price"],
                "xG": p["xG"],
                "xA": p["xA"],
                "xGI_per_90": p["xGI_per_90"],
                "goals": p["goals"],
                "assists": p["assists"],
                "minutes": p["minutes"],
            }
            for p in sorted_players[:limit]
        ]

    def _find_differentials(
        self,
        players: list[PlayerStats],
    ) -> dict[str, Any]:
        """Find differential picks by position.

        Differentials are low-ownership players with strong underlying stats.
        Calculates a 'differential_score' based on:
        - xGI per 90 (attacking output)
        - Form (recent performance)
        - Points per game (FPL returns)
        - Inverse ownership (lower = better differential)
        """
        differentials = []

        for p in players:
            ownership = p["ownership"]

            # Skip highly-owned players
            if ownership >= self.semi_differential_threshold:
                continue

            # Calculate differential score
            # Higher score = better differential pick
            score = self._calculate_differential_score(p)

            # Determine differential tier
            if ownership < self.differential_threshold:
                tier = "elite"  # True differentials < 5%
            else:
                tier = "value"  # Semi-differentials 5-15%

            differentials.append({
                "id": p["id"],
                "player_name": p["player_name"],
                "team_short": p["team_short"],
                "position": p["position"],
                "price": p["price"],
                "ownership": ownership,
                "form": p["form"],
                "xGI_per_90": p["xGI_per_90"],
                "ppg": p["ppg"],
                "total_points": p["total_points"],
                "goals": p["goals"],
                "assists": p["assists"],
                "differential_score": score,
                "tier": tier,
                "minutes": p["minutes"],
                # Matchup data
                "matchup_score": p.get("matchup_score", 5.0),
                "positional_fdr": p.get("positional_fdr"),
                "next_opponent": p.get("next_opponent"),
            })

        # Apply early-season shrinkage
        apply_shrinkage(
            differentials, "differential_score",
            getattr(self, "_player_priors", None), self._next_gw_id,
        )

        # Sort by differential score
        differentials.sort(key=lambda x: x["differential_score"], reverse=True)

        # Group by position
        by_position = {"GK": [], "DEF": [], "MID": [], "FWD": []}
        for p in differentials:
            pos = p["position"]
            if pos in by_position and len(by_position[pos]) < 10:
                by_position[pos].append(p)

        return {
            "all": differentials[:20],
            "by_position": by_position,
            "elite": [p for p in differentials if p["tier"] == "elite"][:15],
            "thresholds": {
                "differential": self.differential_threshold,
                "semi_differential": self.semi_differential_threshold,
            },
        }

    def _calculate_differential_score(self, player: PlayerStats) -> int:
        """Calculate a differential score via the player scoring engine."""
        enrichment = self._prior_enrichment(player.get("id"))
        evaluation, _ = build_player_evaluation(
            player,
            enrichment=enrichment or None,
            matchup_avg_3gw=player.get("matchup_avg_3gw"),
            positional_fdr=player.get("positional_fdr"),
        )
        return calculate_differential_score(
            evaluation,
            semi_differential_threshold=self.semi_differential_threshold,
            next_gw_id=self._next_gw_id,
        )

    def _find_targets(
        self,
        players: list[PlayerStats],
        min_ownership: float = 0,
    ) -> dict[str, Any]:
        """Find transfer targets across all ownership levels.

        Unlike differentials, this ranks players purely by performance
        without penalizing high ownership. Useful for identifying
        template/consensus picks.
        """
        targets = []

        for p in players:
            ownership = p["ownership"]

            # Apply minimum ownership filter if specified
            if ownership < min_ownership:
                continue

            # Calculate target score (no ownership penalty)
            score = self._calculate_target_score(p)

            # Determine ownership tier
            if ownership >= 30:
                tier = "template"
            elif ownership >= 15:
                tier = "popular"
            else:
                tier = "differential"

            targets.append({
                "id": p["id"],
                "player_name": p["player_name"],
                "team_short": p["team_short"],
                "position": p["position"],
                "price": p["price"],
                "ownership": ownership,
                "form": p["form"],
                "xGI_per_90": p["xGI_per_90"],
                "ppg": p["ppg"],
                "total_points": p["total_points"],
                "goals": p["goals"],
                "assists": p["assists"],
                "target_score": score,
                "tier": tier,
                "minutes": p["minutes"],
                # Matchup data
                "matchup_score": p.get("matchup_score", 5.0),
                "positional_fdr": p.get("positional_fdr"),
                "next_opponent": p.get("next_opponent"),
            })

        # Apply early-season shrinkage
        apply_shrinkage(
            targets, "target_score",
            getattr(self, "_player_priors", None), self._next_gw_id,
        )

        # Sort by target score
        targets.sort(key=lambda x: x["target_score"], reverse=True)

        # Group by ownership tier
        by_tier = {"template": [], "popular": [], "differential": []}
        for p in targets:
            tier = p["tier"]
            if len(by_tier[tier]) < 20:
                by_tier[tier].append(p)

        # Group by position
        by_position = {"GK": [], "DEF": [], "MID": [], "FWD": []}
        for p in targets:
            pos = p["position"]
            if pos in by_position and len(by_position[pos]) < 10:
                by_position[pos].append(p)

        return {
            "all": targets[:20],
            "by_tier": by_tier,
            "by_position": by_position,
        }

    def _calculate_target_score(self, player: PlayerStats) -> int:
        """Calculate a target score via the player scoring engine."""
        enrichment = self._prior_enrichment(player.get("id"))
        evaluation, _ = build_player_evaluation(
            player,
            enrichment=enrichment or None,
            matchup_avg_3gw=player.get("matchup_avg_3gw"),
            positional_fdr=player.get("positional_fdr"),
        )
        return calculate_target_score(
            evaluation,
            next_gw_id=self._next_gw_id,
        )

    def _prior_enrichment(self, player_id: int | None) -> dict[str, Any] | None:
        """Build enrichment dict with prior_confidence if available."""
        priors = getattr(self, "_player_priors", None)
        if not priors or player_id is None:
            return None
        prior = priors.get(player_id)
        if not prior:
            return None
        return {"prior_confidence": prior.confidence}
