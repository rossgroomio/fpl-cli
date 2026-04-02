"""Team model for FPL data."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class Team(BaseModel):
    """Represents a Premier League team."""

    id: int
    name: str
    short_name: str  # 3-letter code (e.g., "ARS")
    code: int  # Team code used in some API responses

    # Current season stats
    strength: int  # Overall strength rating
    strength_overall_home: int
    strength_overall_away: int
    strength_attack_home: int
    strength_attack_away: int
    strength_defence_home: int
    strength_defence_away: int

    # Form (last 5 games: W=win, D=draw, L=loss)
    form: str | None = None

    # Position in league
    position: int | None = None
    played: int = 0
    win: int = 0
    draw: int = 0
    loss: int = 0
    points: int = 0

    model_config = ConfigDict(populate_by_name=True)

    @property
    def form_list(self) -> list[str]:
        """Get form as a list of results."""
        if not self.form:
            return []
        return list(self.form)

    @property
    def form_points(self) -> int:
        """Calculate points from recent form (W=3, D=1, L=0)."""
        points_map = {"W": 3, "D": 1, "L": 0}
        return sum(points_map.get(r, 0) for r in self.form_list)
