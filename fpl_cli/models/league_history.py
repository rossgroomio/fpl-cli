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

from collections.abc import Iterable
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

# Bump whenever the row shape changes in a way older code cannot read. A line
# carrying a *higher* version than this is skipped (with a warning) and
# preserved byte-for-byte, so two installs can share one store.
#
# 2: `squad_value` renamed to `team_value`, the name the number always
#    deserved -- it is the API's bank-inclusive `value` (issue #147).
# 3: `global_gw_rank` added -- the FPL-wide rank for this gameweek alone,
#    distinct from `global_rank`'s season-cumulative one. Purely additive, so
#    a version-2 row still validates with it defaulting to None (issue #148).
# 4: `fine_rules_evaluated` added, so an empty `fines` list can be told apart
#    from a capture that never ruled any fine at all. Purely additive too, so
#    an older row still validates with it defaulting to None (issue #136).
LEAGUE_HISTORY_VERSION = 4

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


def partition_segment(season: str, fpl_format: LeagueFormat, league_id: int) -> Path:
    """The `<season>/<format>-<league_id>` naming both ledger and cache trees
    use to identify one partition, kept in one place so the two can never
    silently drift onto different naming schemes. Each tree roots it under
    its own directory (`fpl_cli/services/league_history.py`'s durable store,
    `fpl_cli/services/league_history_counters.py`'s disposable cache) --
    only the segment itself is shared.
    """
    return Path(season) / f"{fpl_format}-{league_id}"


def weakest_tier(tiers: Iterable[FidelityTier]) -> FidelityTier | None:
    """The weakest tier among a set of rows, by `_TIER_RANK`.

    Shared by every "what's the tier of this group as a whole" question: a
    gameweek's coverage (`GameweekCoverage.lowest_tier`) and a notes-pack
    entry's provenance (`league_history_notes._entry_tier`) both mean the
    same thing by it -- a condition needing detail is unavailable for the
    whole group as soon as one row in it is only coarse.
    """
    present = set(tiers)
    if not present:
        return None
    return min(present, key=lambda tier: _TIER_RANK[tier])


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
    # Always gross for `capture_status == OK`: `RecapManagerEntry.gw_points`
    # flips with `use_net_points`, and that setting can change mid-season,
    # which would make rows written months apart incomparable with no way to
    # detect it (KTD3). A `capture_status == UNKNOWN` row is the one exception:
    # it records the classic league-standings figure, which is net of any
    # transfer-cost hit rather than gross whenever the unreached manager took
    # one (see `_unknown_row` in `fpl_cli/cli/_league_recap_history.py`) --
    # accepted so a failed fetch still counts towards gameweek rank (KTD12)
    # rather than handing someone else the week's best or worst. Any reader
    # that needs a verified-gross figure must check `capture_status` first.
    gross_points: int | None = None
    # Classic only: the points hit taken for extra transfers. Null on a draft
    # row, which has no transfers to be charged for -- a zero there reads as a
    # measured "took no hit" for a mechanic the format does not have.
    transfer_cost: int | None = None
    total_points: int | None = None
    # Rank within the league for this gameweek's points, and position on the
    # league table. Never a global FPL rank -- see `global_rank` (KTD12).
    gw_rank: int | None = None
    league_position: int | None = None
    # Where they stood after the previous gameweek. Null on the league's first
    # scored gameweek, where no previous table existed: a value equal to
    # `league_position` there would claim a flat week rather than an absent
    # one, and nothing downstream could tell the two apart (issue #147).
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
    # Which fine rule types this capture actually ruled on, whether or not any
    # of them triggered. Without it an empty `fines` list means three different
    # things at once -- nobody was fined, no rules were configured, or the
    # capture never evaluated any -- and a season tally reading it back would
    # score all three as innocence (issue #136). Three states, all distinct:
    #
    # - a list of rule types: exactly those were ruled here. Shorter than the
    #   configured set on a coarse row, which carries no squad and so cannot
    #   rule anything needing one (`COHORT_ONLY_RULE_TYPES` in
    #   `fpl_cli/cli/_fines.py`).
    # - `[]`: this capture ruled nothing, because nothing was configured for
    #   this format -- a real "no ruling covers this row", not an absence.
    # - `None`: nothing is recorded either way. An unknown-status row (R19),
    #   which never reached the manager, or a row written before schema
    #   version 4 existed.
    fine_rules_evaluated: list[str] | None = None

    # -- classic-only (R2) ---------------------------------------------------
    # All five sit in the picks response's `entry_history` object, all five are
    # destroyed by the season rollover, and none of them exists in draft (no
    # budget, no global rank, no transfers). A draft row omits all five.
    # Prices are in the repo's £0.1m units (1000 = £100.0m).
    #
    # `team_value` is the API's `value` verbatim, which is the squad's selling
    # value *plus* whatever sits in the bank -- the two are stored as the API
    # reports them, so squad-only value is `team_value - bank`. Named
    # `squad_value` until schema version 2, where the name asserted an
    # exclusion the number never made (issue #147).
    team_value: int | None = None
    bank: int | None = None
    # The manager's FPL-wide rank, cumulative across the season to date. Named
    # so it can never be read as a league position, which is what every
    # condition and every recap surface means by "rank" (KTD12).
    global_rank: int | None = None
    # The manager's FPL-wide rank for this gameweek's points alone, not the
    # season-cumulative figure `global_rank` carries -- the API's `rank`,
    # distinct from its `overall_rank`. The rollover destroys this exactly
    # like `global_rank`, and it is the one field of the five worth a
    # dedicated capture push: unlike the other four, nothing else in the row
    # can reconstruct a season's world-rank trajectory once it is gone
    # (issue #148).
    global_gw_rank: int | None = None
    transfers_made: int | None = None
    # R21: how many transfers the recorded count claims that the captured
    # detail does not carry. The transfer fetch is best-effort, so without this
    # an empty list is indistinguishable from a manager who made none.
    transfer_detail_shortfall: int | None = None

    def content(self) -> dict[str, Any]:
        """The row's values excluding when it was captured and its own schema version.

        R3's no-op condition is same-*content*, not same-timestamp: a re-run
        that reproduces a row exactly must write nothing, and every re-run has
        a later `captured_at`. `version` is excluded for the same reason: a
        schema bump alone must not make an unchanged gameweek's re-run look
        like new content and append a superseding duplicate -- an install
        upgrade is not a fact about the gameweek.
        """
        return self.model_dump(mode="json", exclude={"captured_at", "version"})

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


# ---------------------------------------------------------------------------
# U8: streak-counter projection
# ---------------------------------------------------------------------------
#
# A rebuildable cache derived entirely from ledger rows (KTD10), never a
# second source of truth. Persistence and the condition registry that
# produces this state live in `fpl_cli/services/league_history_counters.py`;
# these are just the shapes that get serialised.

# Bump whenever the projection's shape changes in a way older code cannot
# read, or whenever a predicate's *meaning* changes such that a cache built
# under the old logic would report different runs today (a fix to a
# predicate is exactly this: same shape, different runs). Unlike
# LEAGUE_HISTORY_VERSION, a mismatch here is never fatal: the projection is
# a rebuildable cache, so a stale version rebuilds silently from the
# ledger's rows rather than blocking anything (KTD10).
#
# 2: gw_win_streak/gw_loss_streak now extend for every manager tied on
# net-of-hit gameweek points, not just whichever the ordinal gw_rank landed
# on first (issue #163).
# 3: season-wide occurrence fields added to `ConditionRunState`
# (`occurrences`, `held_total`, `last_occurrence_gameweek`,
# `first_evaluated_gameweek`), so a rebuild counts every time a condition
# has occurred this season rather than only the currently-open run
# (issue #164). Because the counts derive from rows already on disk, the
# rebuild this bump forces reports correct season totals back to the
# partition's first captured gameweek.
# 4: two changes to what a position-reading predicate sees. League
# positions are competition-ranked, so managers level on points share a
# place rather than being split by whichever order the cohort arrived in;
# and green_arrow_drought holds instead of extending for a gameweek that
# began at first place, where climbing was impossible and the absent green
# arrow is therefore no failure (issue #164 review).
LEAGUE_HISTORY_COUNTERS_VERSION = 4


class ConditionRunState(BaseModel):
    """One manager's running state for one streak condition.

    Persisted so the weekly path can fold in one new gameweek without
    rescanning the whole ledger. `length` and `start_gameweek` describe the
    run currently open (both reset together); `held_in_run` counts
    gameweeks that held -- R19's unknown rows, R20's fixture-less blanks,
    a condition that did not apply that gameweek -- while this run stayed
    open. A run does not have to hold on *consecutive* gameweeks to
    accumulate this count: three non-held extends that held eight
    gameweeks somewhere in between is still reported as length 3, held 8,
    not silently rounded down to "3, consecutive" (KTD7, consumed by U9).

    The remaining four fields are season-wide and survive a reset, which
    is what makes a season total representable at all (issue #164):
    `occurrences` counts every gameweek that ever extended this condition
    -- a manager who won GW2, GW7, GW11 and GW19 shows 4 here while
    `length` is back to 1. `held_total` counts every held gameweek across
    the whole season, open run or not: it is the count's honesty
    qualifier, the same "un-ruled is not innocent" problem the fines
    tally documents, so a consumer can say "with N not judged" beside the
    number rather than presenting it bare. `last_occurrence_gameweek` is
    the most recent extending gameweek, which is how a consumer tells "the
    count grew this gameweek" from stale colour. `first_evaluated_gameweek`
    is the first gameweek any action at all was folded for this manager --
    the span the count was actually computed over, set once and never
    moved.
    """

    model_config = ConfigDict(extra="forbid")

    length: int = 0
    start_gameweek: int | None = None
    held_in_run: int = 0
    occurrences: int = 0
    held_total: int = 0
    last_occurrence_gameweek: int | None = None
    first_evaluated_gameweek: int | None = None


class LeagueHistoryCountersProjection(BaseModel):
    """Rebuildable per-manager, per-condition streak state for one partition.

    Scoped to one (season, format, league_id) partition, the same
    partitioning the ledger store uses -- so a new season starts every
    counter fresh rather than carrying a run across the boundary.
    `computed_through_gameweek` is the stamp KTD10 advances only when the
    gameweek folded in is exactly one past it; anything else (a backfill
    behind it, a multi-gameweek catch-up ahead of it) means the caller must
    rebuild rather than trust this file, which is exactly why loading it is
    fail-open -- unlike the ledger, this is a disposable cache.
    """

    model_config = ConfigDict(extra="forbid")

    version: int = LEAGUE_HISTORY_COUNTERS_VERSION
    season: str
    fpl_format: LeagueFormat
    league_id: int
    computed_through_gameweek: int
    # Keyed [manager_key][condition_key]. A manager or condition absent from
    # this dict simply has no run open -- equivalent to a fresh
    # ConditionRunState() -- rather than every combination being written out.
    runs: dict[int, dict[str, ConditionRunState]] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# U9: per-manager earliest-captured-gameweek cache (R17)
# ---------------------------------------------------------------------------
#
# A rebuildable cache, never a second source of truth (KTD10), kept and
# persisted by `fpl_cli/services/league_history_notes.py` -- deliberately
# separate from `LeagueHistoryCountersProjection` above rather than a new
# field on it, so this cache's own read/rebuild logic stays scoped to the
# one module that needs it. Each manager's earliest gameweek with any row
# (OK or unknown) at all, keyed by manager_key: once a manager is found,
# the notes pack's R17 joiner qualifier need not rescan every gameweek from
# GW1 to re-derive it on every later weekly `league-recap` call -- only a
# manager never seen before costs a scan, and it costs one exactly once per
# manager, not once per week for the rest of the season.

EARLIEST_GAMEWEEK_CACHE_VERSION = 1


class ManagerEarliestGameweekCache(BaseModel):
    """Per-manager earliest-captured-gameweek cache for one partition (R17).

    Scoped to one (season, format, league_id) partition, the same
    partitioning every other league-history cache uses. Like
    `LeagueHistoryCountersProjection`, a version or partition mismatch on
    load means "rebuild", never "raise" -- this is a disposable cache
    derived entirely from ledger rows, not the ledger itself.
    """

    model_config = ConfigDict(extra="forbid")

    version: int = EARLIEST_GAMEWEEK_CACHE_VERSION
    season: str
    fpl_format: LeagueFormat
    league_id: int
    # manager_key -> earliest gameweek with any row (OK or unknown status)
    # ever discovered for them while scanning this partition. A manager
    # absent from this dict simply has not been looked up yet -- never
    # implies "joined at gameweek 0".
    earliest_gameweek: dict[int, int] = Field(default_factory=dict)
