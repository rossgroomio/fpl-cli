"""Team model for FPL data."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class Team(BaseModel):
    """Represents a Premier League team."""

    id: int
    name: str
    short_name: str  # 3-letter code (e.g., "ARS")
    code: int  # Team code used in some API responses

    # Current season stats.
    #
    # None of these are published until a season is under way. Pre-season the
    # API returns null for `strength` and zeros for the four attack/defence
    # axes, so every field is optional to keep those payloads valid.
    #
    # Nothing reads these today. Fixture difficulty comes from
    # TeamRatingsService, off its own 1-7 ratings in team_ratings.yaml. If that
    # ever changes, note that the pre-season zeros validate cleanly and are
    # indistinguishable from real ratings, so a new consumer needs its own
    # check that a season is under way.
    strength: int | None = None  # Overall strength rating
    strength_overall_home: int | None = None
    strength_overall_away: int | None = None
    strength_attack_home: int | None = None
    strength_attack_away: int | None = None
    strength_defence_home: int | None = None
    strength_defence_away: int | None = None

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
