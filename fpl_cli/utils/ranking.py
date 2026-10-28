"""Ranking utilities: the one definition of a shared place."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterable


def competition_rank(value: int, values: Iterable[int]) -> int:
    """`value`'s rank among `values` when higher is better: one more than the
    number above it, so level values share a rank and the next one down skips
    it (1, 2, 2, 4).

    The one definition the recap's own ranks are counted with (#486): the GW
    Rank column and the report's GW Standings block through
    `gameweek_rank()`, the editorial checks' readings of a gameweek placing
    and a season total, and each month's table behind Manager and Dud of the
    Month (`league_history_months`, #425), so a change to how a tie or a
    missing value ranks reaches all of them at once. League positions --
    Pos, Prev and the ledger's `gw_rank` -- come from
    `derive_point_in_time_positions()` (`_league_recap_data.py`), the same
    competition ranking over a sorted walk, and a change here does not reach
    them.
    """
    return 1 + sum(other > value for other in values)
