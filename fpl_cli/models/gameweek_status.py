"""FPL's post-gameweek processing feed: bonus confirmation and the league-table refresh."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

# The two `leagues` values the feed's processing state is read from: the one
# seen once every league table has been refreshed, and the one community
# usage reports while the refresh runs. Any other value (the empty string
# reported between gameweeks among them) says neither, so it is unknown.
LEAGUES_UPDATED = "Updated"
LEAGUES_UPDATING = "Updating"


class MatchDayStatus(BaseModel):
    """One match day the feed reports on.

    `event` and `bonus_added` are required, so a feed that renames either
    fails validation and reads as unavailable rather than as a negative.
    """

    gameweek: int = Field(alias="event")
    bonus_added: bool
    # Draft only: whether that day's results have reached the league tables.
    leagues_updated: bool | None = None

    model_config = ConfigDict(populate_by_name=True)


class GameweekStatus(BaseModel):
    """Where FPL has got to in processing a gameweek after its matches.

    Both games publish the same shape (`event-status/` for classic,
    `pl/event-status` for draft), normally covering the gameweek most
    recently played until the next deadline moves it on. FPL does not
    document the order, but the common understanding is that the gameweek's
    `finished` flag in bootstrap is set once bonus is confirmed and before
    the league tables are refreshed, so a gameweek can be finished while its
    league tables have not yet caught up with its last match day.

    Every question is asked of one gameweek and answered from that
    gameweek's own match days, so a day belonging to another gameweek
    cannot silence or settle it. None means the feed cannot say: it holds no
    day for that gameweek, or reports a state this model does not recognise.
    """

    match_days: list[MatchDayStatus] = Field(alias="status")
    # Required, though null is allowed: a feed that drops or renames the key
    # fails validation rather than reading as "not updated".
    leagues: str | None

    model_config = ConfigDict(populate_by_name=True)

    def _days(self, gameweek: int) -> list[MatchDayStatus]:
        return [day for day in self.match_days if day.gameweek == gameweek]

    def bonus_confirmed(self, gameweek: int) -> bool | None:
        """Whether bonus is confirmed for every one of the gameweek's match days."""
        days = self._days(gameweek)
        if not days:
            return None
        return all(day.bonus_added for day in days)

    def league_tables_updated(self, gameweek: int) -> bool | None:
        """Whether every league table has caught up with the gameweek.

        False on an explicit "Updating", or on a draft match day marked not
        yet in the tables; True only on "Updated" with no day held back.
        """
        days = self._days(gameweek)
        if not days:
            return None
        if self.leagues == LEAGUES_UPDATING or any(day.leagues_updated is False for day in days):
            return False
        if self.leagues == LEAGUES_UPDATED:
            return True
        return None

    def still_processing(self, gameweek: int) -> str | None:
        """Why FPL has not finished processing the gameweek, or None.

        None also wherever the feed cannot say, since only a definite
        signal is evidence that a league table has not caught up.
        """
        if self.bonus_confirmed(gameweek) is False:
            return "bonus is not yet confirmed for every match day"
        if self.league_tables_updated(gameweek) is False:
            return "league tables are still updating"
        return None
