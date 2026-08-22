"""Ledger row model for the durable league-history store.

One row records what a single `league-recap` capture knew about one manager in
one gameweek -- including that it knew nothing (R19). The store that persists
these rows lives in `fpl_cli/services/league_history.py`.

Two deliberate inversions of house convention (KTD1, R4, R5):

- `fpl_cli/models/chip_plan.py` swallows a `ValidationError` and resets to an
  empty plan. This model does the opposite: parsing is fail-closed, so a
  malformed row aborts capture for that gameweek rather than silently
  discarding recorded history that cannot be rebuilt.
- `fpl_cli/services/team_ratings.py` discards a file stamped with a previous
  season. Here season is a partition key: prior-season rows stay readable
  forever, because the API destroys per-gameweek granularity at the July
  rollover and the ledger is then the only copy.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

# Bump whenever the row shape changes in a way older code cannot read. A line
# carrying a *higher* version than this is skipped (with a warning) and
# preserved byte-for-byte, so two installs can share one store.
LEAGUE_HISTORY_VERSION = 1

# The oldest version this code can still parse. Raising this floor bricks every
# store holding older lines, so it moves only alongside a one-time rewrite that
# upgrades them (see `fpl_cli/services/league_history.py`).
MIN_READABLE_LEAGUE_HISTORY_VERSION = 1

LeagueFormat = Literal["classic", "draft"]


class CaptureStatus(str, Enum):
    """Whether the capture that wrote this row actually reached the manager.

    `UNKNOWN` exists because absence is read as non-membership (R18): a manager
    whose fetch failed must still get a row, or every streak crossing that
    gameweek is silently falsified.
    """

    OK = "ok"
    UNKNOWN = "unknown"


class FidelityTier(str, Enum):
    """Which source produced the row's numbers (R8).

    `COARSE` is the classic manager-history endpoint: headline numbers only, no
    captain, squad, or transfer detail. `DETAILED` is a live capture or a
    past-gameweek picks replay. An unknown-status row still records the tier
    its capture attempt ran at, so U7 knows how to re-attempt it.
    """

    COARSE = "coarse"
    DETAILED = "detailed"


# Resolution order for duplicate keys (R3): highest tier wins, then the latest
# capture timestamp. An unknown row ranks below every tier, so any real capture
# supersedes it.
_TIER_RANK: dict[FidelityTier, int] = {FidelityTier.COARSE: 1, FidelityTier.DETAILED: 2}
_UNKNOWN_RANK = 0


class LedgerPlayer(BaseModel):
    """One player in a manager's recorded squad."""

    model_config = ConfigDict(extra="forbid")

    name: str
    team: str
    position: str
    # Stable cross-season element_code (R6). None when the reference could not
    # be resolved -- always paired with `unmatched` on the draft side.
    code: int | None = None
    points: int = 0
    is_captain: bool = False
    is_vice_captain: bool = False
    contributed: bool = False
    is_bench_boost_player: bool = False
    auto_sub_in: bool = False
    auto_sub_out: bool = False
    red_cards: int = 0
    # Draft only: the draft-to-main-player match failed, so `points` is a false
    # zero rather than a real score.
    unmatched: bool = False
    # R20: their club had a fixture this gameweek. A bench zero from a blank
    # gameweek is not a choice that failed.
    had_fixture: bool = True


class LedgerCaptaincy(BaseModel):
    """A captain or vice-captain pick with the points it actually scored.

    Points are the player's own, undoubled, exactly as the collector records
    them -- the shared blank threshold in `fpl_cli/models/player.py` is applied
    against this number, not against a multiplied one.
    """

    model_config = ConfigDict(extra="forbid")

    name: str
    code: int | None = None
    points: int = 0
    # Set for the captain, left unset for the vice: the collector records
    # whether the captain played (it drives the VC-takeover rules) but never
    # asks the same of the vice.
    played: bool | None = None
    had_fixture: bool | None = None


class LedgerTransfer(BaseModel):
    """One classic transfer, as recorded at capture time."""

    model_config = ConfigDict(extra="forbid")

    player_in: str
    player_in_team: str
    player_in_points: int = 0
    player_in_code: int | None = None
    player_out: str
    player_out_team: str
    player_out_points: int = 0
    player_out_code: int | None = None
    net: int = 0
    cost: int = 0


class LedgerTransaction(BaseModel):
    """One draft waiver or free-agent move, as recorded at capture time."""

    model_config = ConfigDict(extra="forbid")

    player_in: str
    player_in_team: str
    player_in_points: int = 0
    player_in_code: int | None = None
    player_out: str
    player_out_team: str
    player_out_points: int = 0
    player_out_code: int | None = None
    net: int = 0
    kind: str = "w"


class LedgerFine(BaseModel):
    """A fine ruled at capture time, keyed by manager rather than display name.

    `RecapFineResult` carries only a display name, and two managers can share
    one -- so the key is carried through rather than matched on.
    """

    model_config = ConfigDict(extra="forbid")

    manager_key: int
    rule_type: str
    message: str


class LeagueHistoryRow(BaseModel):
    """One manager's record for one gameweek of one league.

    Every detail field is nullable so an unknown-status row (R19) validates
    with nothing but its key and provenance. The key fields carry no defaults,
    so a malformed row raises rather than quietly becoming zero.
    """

    model_config = ConfigDict(extra="forbid")

    version: int = LEAGUE_HISTORY_VERSION

    # -- key (R6) ------------------------------------------------------------
    season: str
    fpl_format: LeagueFormat
    league_id: int
    gameweek: int
    # Classic: the FPL `entry` id. Draft: the league-local `league_entry` id,
    # which is always present -- the site-wide entry id is null for an
    # unclaimed team, and two of them would collide on 0 (KTD11).
    manager_key: int

    # -- provenance (R3, R8) -------------------------------------------------
    capture_status: CaptureStatus
    tier: FidelityTier
    captured_at: datetime

    # -- identity ------------------------------------------------------------
    manager_name: str
    entry_id: int | None = None

    # -- headline numbers ----------------------------------------------------
    # Always gross: `RecapManagerEntry.gw_points` flips with `use_net_points`,
    # and that setting can change mid-season, which would make rows written
    # months apart incomparable with no way to detect it (KTD3).
    gross_points: int | None = None
    transfer_cost: int | None = None
    total_points: int | None = None
    # Rank within the league for this gameweek's points, and position on the
    # league table. Never a global FPL rank -- see `global_rank` (KTD12).
    gw_rank: int | None = None
    league_position: int | None = None
    previous_league_position: int | None = None

    # -- per-manager detail --------------------------------------------------
    captain: LedgerCaptaincy | None = None
    vice_captain: LedgerCaptaincy | None = None
    # The raw API chip name ("bboost", "3xc"), not the display abbreviation.
    # Mapping to display belongs at render.
    active_chip: str | None = None
    bench_points: int | None = None
    squad: list[LedgerPlayer] = Field(default_factory=list)
    transfers: list[LedgerTransfer] = Field(default_factory=list)
    transactions: list[LedgerTransaction] = Field(default_factory=list)

    # -- gameweek shape (R20) ------------------------------------------------
    gameweek_blank: bool | None = None
    gameweek_double: bool | None = None

    # -- fines ---------------------------------------------------------------
    fines: list[LedgerFine] = Field(default_factory=list)

    # -- classic-only (R2) ---------------------------------------------------
    # All four sit in the picks response's `entry_history` object, all four are
    # destroyed by the season rollover, and none of them exists in draft (no
    # budget, no global rank, no transfers). A draft row omits all four.
    # Prices are in the repo's £0.1m units (1000 = £100.0m).
    squad_value: int | None = None
    bank: int | None = None
    # The manager's FPL-wide rank. Named so it can never be read as a league
    # position, which is what every condition and every recap surface means by
    # "rank" (KTD12).
    global_rank: int | None = None
    transfers_made: int | None = None
    # R21: how many transfers the recorded count claims that the captured
    # detail does not carry. The transfer fetch is best-effort, so without this
    # an empty list is indistinguishable from a manager who made none.
    transfer_detail_shortfall: int | None = None

    def content(self) -> dict[str, Any]:
        """The row's values excluding when it was captured.

        R3's no-op condition is same-*content*, not same-timestamp: a re-run
        that reproduces a row exactly must write nothing, and every re-run has
        a later `captured_at`.
        """
        return self.model_dump(mode="json", exclude={"captured_at"})

    def resolution_sort_key(self) -> tuple[int, datetime]:
        """Sort key for picking the winning row among duplicates of one key.

        Highest fidelity tier first, then latest capture; an unknown row ranks
        below every tier so any real capture supersedes it (R3).
        """
        rank = (
            _UNKNOWN_RANK
            if self.capture_status is CaptureStatus.UNKNOWN
            else _TIER_RANK[self.tier]
        )
        return (rank, self.captured_at)


def resolve_rows(rows: list[LeagueHistoryRow]) -> dict[int, LeagueHistoryRow]:
    """Collapse an append-only line list to one winning row per manager key."""
    winners: dict[int, LeagueHistoryRow] = {}
    for row in rows:
        current = winners.get(row.manager_key)
        if current is None or row.resolution_sort_key() > current.resolution_sort_key():
            winners[row.manager_key] = row
    return winners
