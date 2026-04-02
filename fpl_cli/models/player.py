"""Player model for FPL data."""

from __future__ import annotations

import re
from enum import Enum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from fpl_cli.models.team import Team

from pydantic import BaseModel, ConfigDict, Field, computed_field

from fpl_cli.utils.text import strip_diacritics


class PlayerStatus(str, Enum):
    """Player availability status."""

    AVAILABLE = "a"
    DOUBTFUL = "d"
    INJURED = "i"
    SUSPENDED = "s"
    NOT_AVAILABLE = "n"
    UNAVAILABLE = "u"


class PlayerPosition(int, Enum):
    """Player position types."""

    GOALKEEPER = 1
    DEFENDER = 2
    MIDFIELDER = 3
    FORWARD = 4


POSITION_MAP: dict[int, str] = {1: "GK", 2: "DEF", 3: "MID", 4: "FWD"}

# FPL formation limits: (minimum, maximum) players per outfield position
FORMATION_LIMITS: dict[str, tuple[int, int]] = {"DEF": (3, 5), "MID": (2, 5), "FWD": (1, 3)}


class Player(BaseModel):
    """Represents a Premier League player."""

    id: int
    code: int = 0  # Stable cross-season identifier (element_code)
    web_name: str  # Display name (e.g., "Salah")
    first_name: str
    second_name: str
    team_id: int = Field(alias="team")
    position: PlayerPosition = Field(alias="element_type")

    # Pricing (in £0.1m units, so 100 = £10.0m)
    now_cost: int
    cost_change_event: int = 0  # Price change this gameweek
    cost_change_start: int = 0  # Price change since season start

    # Selection
    selected_by_percent: float = 0.0
    transfers_in_event: int = 0
    transfers_out_event: int = 0

    # Status
    status: PlayerStatus = PlayerStatus.AVAILABLE
    chance_of_playing_next_round: int | None = None
    news: str = ""
    news_added: str | None = None

    # Season stats
    total_points: int = 0
    points_per_game: float = 0.0
    form: float = 0.0
    minutes: int = 0
    goals_scored: int = 0
    assists: int = 0
    clean_sheets: int = 0
    goals_conceded: int = 0
    bonus: int = 0
    bps: int = 0  # Bonus points system score
    influence: float = 0.0
    creativity: float = 0.0
    threat: float = 0.0
    ict_index: float = 0.0

    # Expected stats (from FPL API)
    expected_goals: float = Field(default=0.0, alias="expected_goals")
    expected_assists: float = Field(default=0.0, alias="expected_assists")
    expected_goal_involvements: float = Field(default=0.0, alias="expected_goal_involvements")
    expected_goals_conceded: float = Field(default=0.0, alias="expected_goals_conceded")

    # Defensive
    defensive_contribution: int = 0
    defensive_contribution_per_90: float = 0.0
    penalties_saved: int = 0

    # Value metrics
    value_form: float = 0.0
    value_season: float = 0.0

    # Availability
    starts: int = 0
    team_join_date: str | None = None

    # Set pieces (None = no duty assigned)
    penalties_order: int | None = None
    corners_and_indirect_freekicks_order: int | None = None
    direct_freekicks_order: int | None = None

    model_config = ConfigDict(populate_by_name=True)

    @property
    def price(self) -> float:
        """Get price in millions (e.g., 10.5)."""
        return self.now_cost / 10

    @property
    def full_name(self) -> str:
        """Get full player name."""
        return f"{self.first_name} {self.second_name}"

    @property
    def is_available(self) -> bool:
        """Check if player is available to play."""
        return self.status == PlayerStatus.AVAILABLE

    @property
    def position_name(self) -> str:
        """Get human-readable position name."""
        return POSITION_MAP.get(self.position.value, "???")

    @computed_field  # type: ignore[prop-decorator]
    @property
    def appearances(self) -> int:
        """GWs where the player featured (starts + sub appearances).

        Derived from total_points / ppg (FPL's own inverse). Capped at 38
        to guard against near-zero ppg inflating the count.
        """
        if self.points_per_game > 0:
            return min(round(self.total_points / self.points_per_game), 38)
        return 0


def resolve_players(
    query: str,
    players: list[Player],
    teams: list[Team] | None = None,
) -> list[Player]:
    """Resolve players by name, ID, or ``Name (TEAM)`` syntax.

    Resolution order:
    1. Numeric query - match by player ID (returns single-element list).
    2. ``Name (TEAM)`` pattern - filter to team, then exact/substring on name.
    3. Plain name - exact match on web_name/full_name, then substring.

    Strips diacritics so ASCII input matches accented names.
    *teams* is a list of Team models (need ``.id`` and ``.short_name``);
    required only when using the ``(TEAM)`` disambiguator.
    """
    raw = query.strip()
    if not raw:
        return []

    # Numeric ID
    if raw.isdigit():
        pid = int(raw)
        match = next((p for p in players if p.id == pid), None)
        return [match] if match else []

    # Name (TEAM) disambiguation
    team_filter: int | None = None
    m = re.match(r"^(.+?)\s*\(([A-Za-z]{3})\)\s*$", raw)
    if m and teams is not None:
        raw = m.group(1).strip()
        team_code = m.group(2).upper()
        team_map = {t.short_name.upper(): t.id for t in teams}
        team_filter = team_map.get(team_code)
        if team_filter is None:
            return []

    candidates = (
        players if team_filter is None
        else [p for p in players if p.team_id == team_filter]
    )

    q = strip_diacritics(raw.lower())
    if not q:
        return []

    exact = [
        p for p in candidates
        if q == strip_diacritics(p.web_name.lower())
        or q == strip_diacritics(p.full_name.lower())
    ]
    if exact:
        return exact

    return [
        p for p in candidates
        if q in strip_diacritics(p.web_name.lower())
        or q in strip_diacritics(p.full_name.lower())
    ]


def resolve_player(
    query: str,
    players: list[Player],
    teams: list[Team] | None = None,
) -> Player | None:
    """Resolve a single player. Returns first match or None.

    See :func:`resolve_players` for full resolution logic.
    """
    matches = resolve_players(query, players, teams=teams)
    return matches[0] if matches else None
