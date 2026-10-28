"""FPL's own division of the season into months (issue #425)."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from fpl_cli.utils.gameweek import format_gameweek_list


class SeasonMonth(BaseModel):
    """One month of the season, as FPL divides the gameweeks between them.

    FPL publishes these as bootstrap-static's `phases`, and the month selector
    on a league's page reads the same ids: `leagues-classic/{id}/standings/
    ?phase=N` is the league's table for that month alone. FPL calls them
    phases; here they are months, because "phase" already means the season's
    arc (opener, midpoint, run-in) in `derive_season_phase`.

    The boundaries are gameweek ids, never dates: a fixture postponed into a
    later calendar month still counts in its gameweek's month, which is how
    FPL's own month table counts it.
    """

    id: int
    name: str
    first_gameweek: int = Field(alias="start_event")
    last_gameweek: int = Field(alias="stop_event")

    model_config = ConfigDict(populate_by_name=True)

    @property
    def gameweeks(self) -> range:
        """Every gameweek in the month, in order."""
        return range(self.first_gameweek, self.last_gameweek + 1)

    @property
    def label(self) -> str:
        """The month as every surface names it: "October (GW6-9)"."""
        return f"{self.name} ({format_gameweek_list(self.gameweeks)})"


def season_months(phases: Iterable[Mapping[str, Any]]) -> list[SeasonMonth]:
    """The months among bootstrap-static's `phases`, in gameweek order.

    Every phase but the season-long one, which FPL lists first as "Overall"
    (GW1-38). It is told apart by its span, covering every other phase's,
    rather than by its id or name, so a payload that reorders or renames it
    still leaves the months alone. A single phase is that season-long one,
    so it gives no months.

    Raises:
        pydantic.ValidationError: a phase lacks an id, name or either
            boundary, so the months cannot be read.
    """
    parsed = [SeasonMonth.model_validate(phase) for phase in phases]
    if len(parsed) < 2:
        return []
    first = min(phase.first_gameweek for phase in parsed)
    last = max(phase.last_gameweek for phase in parsed)
    months = [
        phase for phase in parsed
        if not (phase.first_gameweek == first and phase.last_gameweek == last)
    ]
    return sorted(months, key=lambda month: month.first_gameweek)
