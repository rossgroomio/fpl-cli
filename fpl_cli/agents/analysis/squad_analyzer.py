"""Squad analyzer agent for assessing squad strength and identifying gaps."""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from fpl_cli.agents.base import Agent, AgentResult, AgentStatus
from fpl_cli.agents.common import get_actual_squad_picks
from fpl_cli.api.fpl import FPLClient


class SquadAnalyzerAgent(Agent):
    """Agent for analyzing FPL squad composition and strength.

    Responsibilities:
    - Assess overall squad strength
    - Identify weak positions
    - Check fixture coverage
    - Flag injury/suspension risks
    - Suggest areas for improvement
    """

    name = "SquadAnalyzerAgent"
    description = "Analyzes squad composition and identifies weaknesses"

    def __init__(self, config: dict[str, Any] | None = None):
        super().__init__(config)
        self.client = FPLClient()
        self.entry_id = config.get("entry_id") if config else None

    async def close(self) -> None:
        await self.client.close()

    async def run(self, context: dict[str, Any] | None = None) -> AgentResult:
        """Analyze team composition and strength.

        Args:
            context: Should contain 'entry_id' or 'picks' (list of player IDs).
                     Optional 'format' key: "classic" (default) or "draft".

        Returns:
            AgentResult with team analysis.
        """
        self.log("Analyzing team composition...")

        # Get team ID from context or config
        entry_id = None
        picks = None
        fmt = "classic"

        if context:
            entry_id = context.get("entry_id") or self.entry_id
            picks = context.get("picks")  # Direct list of player IDs
            fmt = context.get("format", "classic")

        if not entry_id and not picks:
            return self._create_result(
                AgentStatus.FAILED,
                message="No entry_id or picks provided",
                errors=["Set classic_entry_id in config/settings.yaml or provide picks in context"],
            )

        try:
            # Fetch all player and team data
            all_players = await self.client.get_players()
            all_teams = await self.client.get_teams()
            player_map = {p.id: p for p in all_players}
            team_map = {t.id: t for t in all_teams}

            # Get manager entry data for correct metrics (classic only)
            manager_entry = None
            if entry_id and fmt == "classic":
                manager_entry = await self.client.get_manager_entry(entry_id)

            # Get the team's players
            if picks:
                team_players = [player_map[pid] for pid in picks if pid in player_map]
            else:
                # Fetch from gameweek picks, checking for Free Hit chip
                gw_data = await self.client.get_next_gameweek()
                gw = gw_data["id"] if gw_data else 1
                target_gw = gw - 1

                assert entry_id is not None  # already validated above
                picks_data, target_gw = await get_actual_squad_picks(
                    self.client, entry_id, target_gw, log=self.log
                )

                pick_ids = [p["element"] for p in picks_data.get("picks", [])]
                team_players = [player_map[pid] for pid in pick_ids if pid in player_map]
                self.log(f"Using squad from GW{target_gw}")

            if not team_players:
                return self._create_result(
                    AgentStatus.FAILED,
                    message="Could not fetch team players",
                    errors=["Team may be empty or API returned no data"],
                )

            self.log(f"Analyzing squad of {len(team_players)} players")

            # Run analysis
            squad_overview = self._analyze_squad_overview(team_players, team_map, manager_entry, fmt)
            position_analysis = self._analyze_positions(team_players, team_map)
            injury_risks = self._analyze_injury_risks(team_players, team_map)
            form_analysis = self._analyze_form(team_players, team_map)
            recommendations = self._generate_recommendations(
                team_players, position_analysis, injury_risks, squad_overview, fmt,
            )

            self.log_success("Squad analysis complete")

            return self._create_result(
                AgentStatus.SUCCESS,
                data={
                    "squad_overview": squad_overview,
                    "position_analysis": position_analysis,
                    "injury_risks": injury_risks,
                    "form_analysis": form_analysis,
                    "recommendations": recommendations,
                },
                message=f"Analyzed squad of {len(team_players)} players",
            )

        except Exception as e:  # noqa: BLE001 — agent top-level handler
            self.log_error(f"Failed to analyze squad: {e}")
            return self._create_result(
                AgentStatus.FAILED,
                message="Failed to analyze squad",
                errors=[str(e)],
            )

    def _analyze_squad_overview(
        self,
        players: list,
        team_map: dict[int, Any],
        manager_entry: dict[str, Any] | None = None,
        fmt: str = "classic",
    ) -> dict[str, Any]:
        """Get overall squad statistics."""
        total_points = sum(p.total_points for p in players)
        avg_form = sum(p.form for p in players) / len(players) if players else 0

        result: dict[str, Any] = {
            "total_points": total_points,
            "average_form": round(avg_form, 2),
        }

        # Value/bank only relevant for classic format
        if fmt == "classic":
            if manager_entry:
                result["total_points"] = manager_entry.get("summary_overall_points", 0)
                result["team_value"] = round(manager_entry.get("last_deadline_value", 0) / 10, 1)
                result["bank"] = round(manager_entry.get("last_deadline_bank", 0) / 10, 1)
            else:
                result["team_value"] = round(sum(p.now_cost for p in players) / 10, 1)
                result["bank"] = 0.0

        # Count by position
        positions = defaultdict(int)
        for p in players:
            positions[p.position_name] += 1

        # Count by team
        teams = defaultdict(list)
        for p in players:
            team = team_map.get(p.team_id)
            team_name = team.short_name if team else "???"
            teams[team_name].append(p.web_name)

        result["position_counts"] = dict(positions)
        result["team_coverage"] = {k: len(v) for k, v in teams.items()}
        result["players_by_team"] = dict(teams)
        return result

    def _analyze_positions(
        self,
        players: list,
        team_map: dict[int, Any],
    ) -> dict[str, Any]:
        """Analyze strength by position."""
        by_position: dict[str, list] = defaultdict(list)

        for p in players:
            by_position[p.position_name].append({
                "name": p.web_name,
                "team": team_map[p.team_id].short_name if p.team_id in team_map else "???",
                "price": p.price,
                "form": p.form,
                "points": p.total_points,
                "ppg": p.points_per_game,
                "xG": round(p.expected_goals, 2),
                "xA": round(p.expected_assists, 2),
            })

        # Sort each position by form
        for pos in by_position:
            by_position[pos].sort(key=lambda x: x["form"], reverse=True)

        # Identify weak positions (low average form)
        position_strength = {}
        for pos, players_list in by_position.items():
            avg_form = sum(p["form"] for p in players_list) / len(players_list)
            avg_ppg = sum(p["ppg"] for p in players_list) / len(players_list)
            position_strength[pos] = {
                "count": len(players_list),
                "average_form": round(avg_form, 2),
                "average_ppg": round(avg_ppg, 2),
                "players": players_list,
            }

        return position_strength

    def _analyze_injury_risks(
        self,
        players: list,
        team_map: dict[int, Any],
    ) -> list[dict[str, Any]]:
        """Identify players with injury/availability concerns."""
        from fpl_cli.models.player import PlayerStatus

        risks = []
        for p in players:
            if p.status != PlayerStatus.AVAILABLE:
                team = team_map.get(p.team_id)
                risks.append({
                    "name": p.web_name,
                    "team": team.short_name if team else "???",
                    "status": p.status.value,
                    "chance_of_playing": p.chance_of_playing_next_round,
                    "news": p.news,
                })

        return risks

    def _analyze_form(
        self,
        players: list,
        team_map: dict[int, Any],
    ) -> dict[str, Any]:
        """Analyze player form."""
        sorted_by_form = sorted(players, key=lambda p: p.form, reverse=True)

        in_form = [
            {
                "name": p.web_name,
                "team": team_map[p.team_id].short_name if p.team_id in team_map else "???",
                "position": p.position_name,
                "form": p.form,
            }
            for p in sorted_by_form[:5]
        ]

        out_of_form = [
            {
                "name": p.web_name,
                "team": team_map[p.team_id].short_name if p.team_id in team_map else "???",
                "position": p.position_name,
                "form": p.form,
            }
            for p in sorted_by_form[-5:]
        ]

        return {
            "in_form": in_form,
            "out_of_form": out_of_form,
        }

    def _generate_recommendations(
        self,
        team_players: list,
        position_analysis: dict[str, Any],
        injury_risks: list[dict[str, Any]],
        squad_overview: dict[str, Any] | None = None,
        fmt: str = "classic",
    ) -> list[dict[str, Any]]:
        """Generate recommendations for team improvement."""
        recommendations = []

        # Flag teams at the 3-player limit (Classic FPL hard limit - not relevant for draft)
        if squad_overview and fmt == "classic":
            team_coverage = squad_overview.get("team_coverage", {})
            players_by_team = squad_overview.get("players_by_team", {})
            for team_name, count in team_coverage.items():
                if count >= 3:
                    players = players_by_team.get(team_name, [])
                    player_list = ", ".join(players)
                    recommendations.append({
                        "priority": "low",
                        "type": "team_limit",
                        "message": f"At team limit: {count} {team_name} players ({player_list})",
                        "suggestion": f"Cannot add more {team_name} players without selling",
                    })

        # Check for availability concerns - prioritize by severity
        if injury_risks:
            for risk in injury_risks:
                status = risk.get("status", "")
                chance = risk.get("chance_of_playing")

                # Suspended players - highest priority
                if status == "s":
                    recommendations.append({
                        "priority": "critical",
                        "type": "suspended",
                        "message": f"{risk['name']} ({risk['team']}) is SUSPENDED",
                        "suggestion": f"Transfer out {risk['name']} immediately - cannot play",
                        "news": risk.get("news", ""),
                    })
                # Unavailable with 0% chance
                elif chance == 0:
                    recommendations.append({
                        "priority": "critical",
                        "type": "unavailable",
                        "message": f"{risk['name']} ({risk['team']}) has 0% chance of playing",
                        "suggestion": f"Transfer out {risk['name']} - confirmed unavailable",
                        "news": risk.get("news", ""),
                    })
                # Injured/doubtful with low chance (<50%)
                elif chance is not None and chance < 50:
                    recommendations.append({
                        "priority": "high",
                        "type": "injury_risk",
                        "message": f"{risk['name']} ({risk['team']}) has {chance}% chance of playing",
                        "suggestion": f"Consider replacing {risk['name']}",
                        "news": risk.get("news", ""),
                    })
                # Doubtful (50-75% chance)
                elif chance is not None and chance <= 75:
                    recommendations.append({
                        "priority": "medium",
                        "type": "doubtful",
                        "message": f"{risk['name']} ({risk['team']}) has {chance}% chance of playing",
                        "suggestion": f"Monitor {risk['name']} - have backup ready",
                        "news": risk.get("news", ""),
                    })

        # Check for weak positions (form < 3.0)
        for pos, data in position_analysis.items():
            if data["average_form"] < 3.0 and pos != "GK":
                recommendations.append({
                    "priority": "medium",
                    "type": "weak_position",
                    "message": f"{pos} position has low average form ({data['average_form']})",
                    "suggestion": f"Consider strengthening {pos} options",
                })

        # Check for underperforming players (classic only - price irrelevant in draft)
        for p in team_players:
            if fmt == "classic" and p.price >= 8.0 and p.form < 3.0:
                recommendations.append({
                    "priority": "medium",
                    "type": "premium_underperforming",
                    "message": f"Premium player {p.web_name} (£{p.price}m) has form of {p.form}",
                    "suggestion": "Consider downgrading to fund upgrades elsewhere",
                })
            elif fmt == "classic" and 5.0 <= p.price < 8.0 and p.minutes >= 450 and p.value_season < 3.0:
                recommendations.append({
                    "priority": "low",
                    "type": "mid_price_underperforming",
                    "message": f"Mid-price underperformer: {p.web_name} (£{p.price}m) — {p.value_season:.1f} pts/£m",
                    "suggestion": "Consider replacing with a better-value option in this price range",
                })

        # Sort by priority
        priority_order = {"critical": 0, "high": 1, "medium": 2, "low": 3}
        recommendations.sort(key=lambda x: priority_order.get(x["priority"], 99))

        return recommendations[:10]
