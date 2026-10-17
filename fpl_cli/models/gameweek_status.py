"""FPL's post-gameweek processing feed: bonus confirmation and the league-table refresh."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

# The `leagues` value FPL reports once every league table has been refreshed
# for the gameweek the feed covers. The other values seen in community usage
# are "Updating" while the refresh runs and an empty string between
# gameweeks; anything but this one is read as "not yet".
LEAGUES_UPDATED = "Updated"


class MatchDayStatus(BaseModel):
    """One match day of the gameweek the feed covers."""

    gameweek: int | None = Field(default=None, alias="event")
    bonus_added: bool | None = None
    # Draft only: whether that day's results have reached the league tables.
    leagues_updated: bool | None = None

    model_config = ConfigDict(populate_by_name=True)


class GameweekStatus(BaseModel):
    """Where FPL has got to in processing one gameweek after its matches.

    Both games publish the same shape (`event-status/` for classic,
    `pl/event-status` for draft), and both cover a single gameweek at a time:
    the one most recently played, until the next deadline moves it on. FPL
    does not document the order, but the common understanding is that the
    gameweek's `finished` flag in bootstrap is set once bonus is confirmed and
    before the league tables are refreshed, so a gameweek can be finished
    while its league tables have not yet caught up with its last match day.
    """

    match_days: list[MatchDayStatus] = Field(default_factory=list, alias="status")
    leagues: str | None = None

    model_config = ConfigDict(populate_by_name=True)

    @property
    def gameweek(self) -> int | None:
        """The gameweek the feed covers, or None when it names none or several."""
        gameweeks = {day.gameweek for day in self.match_days if day.gameweek is not None}
        return gameweeks.pop() if len(gameweeks) == 1 else None

    @property
    def bonus_confirmed(self) -> bool:
        """Whether bonus points are confirmed for every match day."""
        return bool(self.match_days) and all(day.bonus_added is True for day in self.match_days)

    @property
    def league_tables_updated(self) -> bool:
        """Whether every league table has caught up with the gameweek.

        Draft also reports this per match day; a day marked not updated
        holds the answer back even when the headline says otherwise.
        """
        return self.leagues == LEAGUES_UPDATED and all(
            day.leagues_updated is not False for day in self.match_days
        )
