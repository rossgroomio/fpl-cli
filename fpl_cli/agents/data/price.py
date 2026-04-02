"""Price agent for tracking FPL price changes and predictions."""

from __future__ import annotations

from typing import Any

from fpl_cli.agents.base import Agent, AgentResult, AgentStatus
from fpl_cli.api.fpl import FPLClient


class PriceAgent(Agent):
    """Agent for tracking player price changes.

    Responsibilities:
    - Track current gameweek price changes
    - Track season-long price changes
    - Identify players with high transfer activity (likely to change)
    - Alert on players about to rise/fall
    """

    name = "PriceAgent"
    description = "Tracks FPL player price changes and transfer activity"

    def __init__(self, config: dict[str, Any] | None = None):
        super().__init__(config)
        self.client = FPLClient()
        # Threshold for "high" transfer activity (percentage of ownership)
        self.transfer_threshold = config.get("transfer_threshold", 5.0) if config else 5.0

    async def close(self) -> None:
        await self.client.close()

    async def run(self, context: dict[str, Any] | None = None) -> AgentResult:
        """Analyze price changes and transfer activity.

        Returns:
            AgentResult with:
            - risers: Players who rose this gameweek
            - fallers: Players who fell this gameweek
            - hot_transfers_in: Players with high transfers in
            - hot_transfers_out: Players with high transfers out
            - season_value_gains: Best value gainers this season
        """
        self.log("Analyzing price changes and transfers...")

        try:
            players = await self.client.get_players()
            teams = await self.client.get_teams()
            team_map = {t.id: t for t in teams}

            # Analyze price changes
            risers = self._find_risers(players, team_map)
            fallers = self._find_fallers(players, team_map)
            hot_in = self._find_hot_transfers_in(players, team_map)
            hot_out = self._find_hot_transfers_out(players, team_map)
            value_gains = self._find_season_value_gains(players, team_map)
            value_losses = self._find_season_value_losses(players, team_map)

            self.log_success(
                f"Found {len(risers)} risers, {len(fallers)} fallers this GW"
            )

            return self._create_result(
                AgentStatus.SUCCESS,
                data={
                    "risers_this_gw": risers,
                    "fallers_this_gw": fallers,
                    "hot_transfers_in": hot_in,
                    "hot_transfers_out": hot_out,
                    "season_value_gains": value_gains,
                    "season_value_losses": value_losses,
                    "summary": {
                        "total_risers": len(risers),
                        "total_fallers": len(fallers),
                        "most_transferred_in": hot_in[0]["name"] if hot_in else None,
                        "most_transferred_out": hot_out[0]["name"] if hot_out else None,
                    },
                },
                message=f"Price analysis complete: {len(risers)} rises, {len(fallers)} falls",
            )

        except Exception as e:  # noqa: BLE001 — agent top-level handler
            self.log_error(f"Failed to analyze prices: {e}")
            return self._create_result(
                AgentStatus.FAILED,
                message="Failed to analyze price changes",
                errors=[str(e)],
            )

    def _find_risers(
        self,
        players: list,
        team_map: dict[int, Any],
    ) -> list[dict[str, Any]]:
        """Find players whose price rose this gameweek."""
        risers = [
            self._player_price_data(p, team_map)
            for p in players
            if p.cost_change_event > 0
        ]
        risers.sort(key=lambda x: x["change_this_gw"], reverse=True)
        return risers

    def _find_fallers(
        self,
        players: list,
        team_map: dict[int, Any],
    ) -> list[dict[str, Any]]:
        """Find players whose price fell this gameweek."""
        fallers = [
            self._player_price_data(p, team_map)
            for p in players
            if p.cost_change_event < 0
        ]
        fallers.sort(key=lambda x: x["change_this_gw"])
        return fallers

    def _find_hot_transfers_in(
        self,
        players: list,
        team_map: dict[int, Any],
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        """Find players with highest transfers in this gameweek."""
        sorted_players = sorted(
            players,
            key=lambda p: p.transfers_in_event,
            reverse=True,
        )

        return [
            {
                **self._player_price_data(p, team_map),
                "transfers_in": p.transfers_in_event,
                "transfers_out": p.transfers_out_event,
                "net_transfers": p.transfers_in_event - p.transfers_out_event,
            }
            for p in sorted_players[:limit]
        ]

    def _find_hot_transfers_out(
        self,
        players: list,
        team_map: dict[int, Any],
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        """Find players with highest transfers out this gameweek."""
        sorted_players = sorted(
            players,
            key=lambda p: p.transfers_out_event,
            reverse=True,
        )

        return [
            {
                **self._player_price_data(p, team_map),
                "transfers_in": p.transfers_in_event,
                "transfers_out": p.transfers_out_event,
                "net_transfers": p.transfers_in_event - p.transfers_out_event,
            }
            for p in sorted_players[:limit]
        ]

    def _find_season_value_gains(
        self,
        players: list,
        team_map: dict[int, Any],
        limit: int = 15,
    ) -> list[dict[str, Any]]:
        """Find players with biggest price increases this season."""
        sorted_players = sorted(
            players,
            key=lambda p: p.cost_change_start,
            reverse=True,
        )

        return [
            self._player_price_data(p, team_map)
            for p in sorted_players[:limit]
            if p.cost_change_start > 0
        ]

    def _find_season_value_losses(
        self,
        players: list,
        team_map: dict[int, Any],
        limit: int = 15,
    ) -> list[dict[str, Any]]:
        """Find players with biggest price decreases this season."""
        sorted_players = sorted(
            players,
            key=lambda p: p.cost_change_start,
        )

        return [
            self._player_price_data(p, team_map)
            for p in sorted_players[:limit]
            if p.cost_change_start < 0
        ]

    def _player_price_data(self, player, team_map: dict[int, Any]) -> dict[str, Any]:
        """Extract price-related data for a player."""
        team = team_map.get(player.team_id)

        return {
            "id": player.id,
            "name": player.web_name,
            "team": team.short_name if team else "???",
            "position": player.position_name,
            "current_price": player.price,
            "change_this_gw": player.cost_change_event / 10,  # Convert to millions
            "change_this_season": player.cost_change_start / 10,
            "ownership": player.selected_by_percent,
            "form": player.form,
            "total_points": player.total_points,
        }
