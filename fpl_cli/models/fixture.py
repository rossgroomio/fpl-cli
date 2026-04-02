"""Fixture model for FPL data."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class Fixture(BaseModel):
    """Represents a Premier League fixture."""

    id: int
    gameweek: int | None = Field(alias="event")
    home_team_id: int = Field(alias="team_h")
    away_team_id: int = Field(alias="team_a")

    # Fixture difficulty ratings (1-5, higher = harder)
    home_difficulty: int = Field(alias="team_h_difficulty")
    away_difficulty: int = Field(alias="team_a_difficulty")

    # Timing
    kickoff_time: datetime | None = None
    finished: bool | None = False
    started: bool | None = False

    # Score (if finished or in progress)
    home_score: int | None = Field(default=None, alias="team_h_score")
    away_score: int | None = Field(default=None, alias="team_a_score")

    # Stats (goals, assists, bonus, etc. for finished fixtures)
    stats: list[dict[str, Any]] = Field(default_factory=list)

    model_config = ConfigDict(populate_by_name=True)

    @property
    def is_blank(self) -> bool:
        """Check if this is a blank fixture (no gameweek assigned)."""
        return self.gameweek is None

    def get_difficulty_for_team(self, team_id: int) -> int:
        """Get fixture difficulty for a specific team.

        Args:
            team_id: The team ID to get difficulty for.

        Returns:
            Difficulty rating (1-5).
        """
        if team_id == self.home_team_id:
            return self.home_difficulty
        elif team_id == self.away_team_id:
            return self.away_difficulty
        else:
            raise ValueError(f"Team {team_id} not in fixture")

    def is_home_for_team(self, team_id: int) -> bool:
        """Check if a team is playing at home.

        Args:
            team_id: The team ID to check.

        Returns:
            True if the team is playing at home.
        """
        return team_id == self.home_team_id

    def get_opponent_id(self, team_id: int) -> int:
        """Get the opponent team ID for a given team.

        Args:
            team_id: The team ID to get opponent for.

        Returns:
            Opponent team ID.
        """
        if team_id == self.home_team_id:
            return self.away_team_id
        elif team_id == self.away_team_id:
            return self.home_team_id
        else:
            raise ValueError(f"Team {team_id} not in fixture")

    def _get_stat(self, identifier: str) -> list[dict[str, Any]]:
        """Get a specific stat by identifier.

        Args:
            identifier: The stat identifier (e.g., 'goals_scored', 'assists', 'bonus').

        Returns:
            List of player entries with 'element' (player ID) and 'value'.
        """
        for stat in self.stats:
            if stat.get("identifier") == identifier:
                # Combine home and away entries
                home_entries = stat.get("h", [])
                away_entries = stat.get("a", [])
                return home_entries + away_entries
        return []

    def get_goal_scorers(self) -> list[dict[str, Any]]:
        """Get all goal scorers in this fixture.

        Returns:
            List of dicts with 'element' (player ID) and 'value' (goals scored).
        """
        return self._get_stat("goals_scored")

    def get_assists(self) -> list[dict[str, Any]]:
        """Get all assist providers in this fixture.

        Returns:
            List of dicts with 'element' (player ID) and 'value' (assists).
        """
        return self._get_stat("assists")

    def get_bonus(self) -> list[dict[str, Any]]:
        """Get all bonus point earners in this fixture.

        Returns:
            List of dicts with 'element' (player ID) and 'value' (bonus points).
            Sorted by bonus points descending (3, 2, 1).
        """
        bonus = self._get_stat("bonus")
        return sorted(bonus, key=lambda x: x.get("value", 0), reverse=True)

    def get_red_cards(self) -> list[dict[str, Any]]:
        """Get all red cards in this fixture.

        Returns:
            List of dicts with 'element' (player ID) and 'value' (1).
        """
        return self._get_stat("red_cards")

    def get_own_goals(self) -> list[dict[str, Any]]:
        """Get all own goals in this fixture.

        Returns:
            List of dicts with 'element' (player ID) and 'value' (own goals).
        """
        return self._get_stat("own_goals")
