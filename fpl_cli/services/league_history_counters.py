"""Streak-condition registry and rebuildable counters projection (U8).

Conditions are a declarative registry, not hand-written counters (KTD7):
each entry declares its key, the formats it applies to, a label, the
minimum run length worth reporting, the row fields it reads, and a
predicate that returns extend, reset, or hold for one manager's row that
gameweek. A predicate receives the row, the manager's row for the previous
gameweek (or None), and the full set of rows recorded for that gameweek --
so a cohort-relative condition (who's top, who's last) has its denominator
without a second query.

Hold is what makes R19 and R20 work: an unknown row, a fixture-less blank,
a condition that plainly does not apply that gameweek (a draft manager who
made no waiver moves) all hold, leaving the run untouched rather than
lying in either direction. R19 specifically -- an unknown row never
advances or breaks a streak -- is enforced centrally in :func:`_evaluate`
rather than trusted to every predicate.

The projection this registry drives is a rebuildable cache, never a second
source of truth (KTD10): it carries its own version and a
computed-through-gameweek stamp, advances by one gameweek only when the
gameweek folded in is exactly the stamp plus one, and rebuilds silently
from `LeagueHistoryStore`'s rows for anything else -- a stale stamp, a
version mismatch, a missing or unreadable cache file. Only the ledger
itself (`fpl_cli/services/league_history.py`) fails closed; this module
never raises past a caller.
"""

from __future__ import annotations

import logging
import math
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from pydantic import ValidationError

from fpl_cli.models.league_history import (
    LEAGUE_HISTORY_COUNTERS_VERSION,
    CaptureStatus,
    ConditionRunState,
    LeagueFormat,
    LeagueHistoryCountersProjection,
    LeagueHistoryRow,
)
from fpl_cli.models.player import BLANK_POINTS_THRESHOLD
from fpl_cli.paths import user_data_dir
from fpl_cli.services.league_history import LeagueHistoryError, LeagueHistoryStore
from fpl_cli.utils.files import atomic_write_text

logger = logging.getLogger(__name__)


class RunAction(str, Enum):
    """What a condition's predicate says to do with a manager's run this gameweek."""

    EXTEND = "extend"
    RESET = "reset"
    HOLD = "hold"


ConditionPredicate = Callable[
    [LeagueHistoryRow, "LeagueHistoryRow | None", "list[LeagueHistoryRow]"], RunAction,
]


@dataclass(frozen=True)
class ConditionDefinition:
    """One entry in the streak-condition registry (KTD7).

    `needs` documents the row fields the predicate reads -- descriptive
    metadata, not a generic gate: coarse-safety is not a fixed column (a
    coarse row lacking captain detail still evaluates `weeks_on_top` fine
    if `league_position` is present), so each predicate implements its own
    field-presence and applicability checks rather than this tuple driving
    them mechanically. Cohort- or previous-row-dependence (e.g.
    `bottom_half_run` needing the full cohort, `green_arrow_drought`
    needing the previous gameweek) lives in the predicate body, not here.
    """

    key: str
    formats: frozenset[LeagueFormat]
    label: str
    min_run: int
    needs: tuple[str, ...]
    predicate: ConditionPredicate


# ---------------------------------------------------------------------------
# Predicates
# ---------------------------------------------------------------------------
#
# Every predicate has the same signature so they can sit uniformly in the
# registry: (row, previous_row, cohort) -> RunAction. `row.capture_status ==
# UNKNOWN` is never checked here -- that is R19's job, enforced once in
# `_evaluate` before any predicate runs -- so a predicate is free to read
# whatever headline numbers an unknown row happens to carry when that row
# shows up inside *another* manager's cohort (KTD12).


def _weeks_on_top(
    row: LeagueHistoryRow, previous_row: LeagueHistoryRow | None, cohort: list[LeagueHistoryRow],
) -> RunAction:
    del previous_row, cohort
    if row.league_position is None:
        return RunAction.HOLD
    return RunAction.EXTEND if row.league_position == 1 else RunAction.RESET


def _bottom_half_run(
    row: LeagueHistoryRow, previous_row: LeagueHistoryRow | None, cohort: list[LeagueHistoryRow],
) -> RunAction:
    del previous_row
    if row.league_position is None or not cohort:
        return RunAction.HOLD
    half = math.ceil(len(cohort) / 2)
    return RunAction.EXTEND if row.league_position > half else RunAction.RESET


def _gw_win_streak(
    row: LeagueHistoryRow, previous_row: LeagueHistoryRow | None, cohort: list[LeagueHistoryRow],
) -> RunAction:
    del previous_row, cohort
    if row.gw_rank is None:
        return RunAction.HOLD
    return RunAction.EXTEND if row.gw_rank == 1 else RunAction.RESET


def _gw_loss_streak(
    row: LeagueHistoryRow, previous_row: LeagueHistoryRow | None, cohort: list[LeagueHistoryRow],
) -> RunAction:
    del previous_row
    if row.gw_rank is None:
        return RunAction.HOLD
    known_ranks = [member.gw_rank for member in cohort if member.gw_rank is not None]
    if not known_ranks:
        return RunAction.HOLD
    return RunAction.EXTEND if row.gw_rank == max(known_ranks) else RunAction.RESET


def _green_arrow_drought(
    row: LeagueHistoryRow, previous_row: LeagueHistoryRow | None, cohort: list[LeagueHistoryRow],
) -> RunAction:
    del cohort
    if row.league_position is None:
        return RunAction.HOLD
    # The previous *gameweek's* row, not `row.previous_league_position` (an
    # upstream API field with no hold semantics of its own): if we don't
    # know what happened to this manager last gameweek -- no row, or an
    # unknown one -- we cannot say whether their position improved.
    if previous_row is None or previous_row.capture_status is CaptureStatus.UNKNOWN:
        return RunAction.HOLD
    if previous_row.league_position is None:
        return RunAction.HOLD
    improved = row.league_position < previous_row.league_position
    return RunAction.RESET if improved else RunAction.EXTEND


def _captain_blank_run(
    row: LeagueHistoryRow, previous_row: LeagueHistoryRow | None, cohort: list[LeagueHistoryRow],
) -> RunAction:
    del previous_row, cohort
    if row.captain is None:
        return RunAction.HOLD
    # R20: the captain's own had_fixture flag gates this condition. Only
    # True proceeds -- False and None (not recorded) both hold, since
    # neither lets us say the blank was a real one.
    if row.captain.had_fixture is not True:
        return RunAction.HOLD
    return RunAction.EXTEND if row.captain.points <= BLANK_POINTS_THRESHOLD else RunAction.RESET


def _hit_run(
    row: LeagueHistoryRow, previous_row: LeagueHistoryRow | None, cohort: list[LeagueHistoryRow],
) -> RunAction:
    del previous_row, cohort
    if row.transfer_cost is None:
        return RunAction.HOLD
    return RunAction.EXTEND if row.transfer_cost > 0 else RunAction.RESET


def _waiver_win_run(
    row: LeagueHistoryRow, previous_row: LeagueHistoryRow | None, cohort: list[LeagueHistoryRow],
) -> RunAction:
    del previous_row, cohort
    if not row.transactions:
        return RunAction.HOLD
    net_total = sum(transaction.net for transaction in row.transactions)
    return RunAction.EXTEND if net_total > 0 else RunAction.RESET


def _waiver_burn_run(
    row: LeagueHistoryRow, previous_row: LeagueHistoryRow | None, cohort: list[LeagueHistoryRow],
) -> RunAction:
    del previous_row, cohort
    if not row.transactions:
        return RunAction.HOLD
    net_total = sum(transaction.net for transaction in row.transactions)
    return RunAction.EXTEND if net_total < 0 else RunAction.RESET


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

_BOTH: frozenset[LeagueFormat] = frozenset({"classic", "draft"})
_CLASSIC_ONLY: frozenset[LeagueFormat] = frozenset({"classic"})
_DRAFT_ONLY: frozenset[LeagueFormat] = frozenset({"draft"})

# Exactly these nine conditions (R12): shared, then classic-only, then
# draft-only. Order is display order only -- every condition is evaluated
# independently, and none reads another condition's state.
CONDITIONS: tuple[ConditionDefinition, ...] = (
    ConditionDefinition(
        key="weeks_on_top", formats=_BOTH, label="Weeks on top", min_run=2,
        needs=("league_position",), predicate=_weeks_on_top,
    ),
    ConditionDefinition(
        key="bottom_half_run", formats=_BOTH, label="Bottom-half run", min_run=3,
        needs=("league_position",), predicate=_bottom_half_run,
    ),
    ConditionDefinition(
        key="gw_win_streak", formats=_BOTH, label="Gameweek win streak", min_run=2,
        needs=("gw_rank",), predicate=_gw_win_streak,
    ),
    ConditionDefinition(
        key="gw_loss_streak", formats=_BOTH, label="Gameweek loss streak", min_run=2,
        needs=("gw_rank",), predicate=_gw_loss_streak,
    ),
    ConditionDefinition(
        key="green_arrow_drought", formats=_BOTH, label="Green arrow drought", min_run=4,
        needs=("league_position",), predicate=_green_arrow_drought,
    ),
    ConditionDefinition(
        key="captain_blank_run", formats=_CLASSIC_ONLY, label="Captain blank run", min_run=2,
        needs=("captain",), predicate=_captain_blank_run,
    ),
    ConditionDefinition(
        key="hit_run", formats=_CLASSIC_ONLY, label="Hit run", min_run=3,
        needs=("transfer_cost",), predicate=_hit_run,
    ),
    ConditionDefinition(
        key="waiver_win_run", formats=_DRAFT_ONLY, label="Waiver win run", min_run=2,
        needs=("transactions",), predicate=_waiver_win_run,
    ),
    ConditionDefinition(
        key="waiver_burn_run", formats=_DRAFT_ONLY, label="Waiver burn run", min_run=2,
        needs=("transactions",), predicate=_waiver_burn_run,
    ),
)


def conditions_for_format(fpl_format: LeagueFormat) -> tuple[ConditionDefinition, ...]:
    """Registry entries applicable to one format, in registry order."""
    return tuple(condition for condition in CONDITIONS if fpl_format in condition.formats)


def _evaluate(
    definition: ConditionDefinition,
    row: LeagueHistoryRow,
    previous_row: LeagueHistoryRow | None,
    cohort: list[LeagueHistoryRow],
) -> RunAction:
    """Evaluate one condition for one manager's row, enforcing R19 centrally.

    An unknown row never advances or breaks a streak (R19). Checked once,
    here, rather than trusted to every predicate author: a predicate is
    free to read whatever headline numbers an unknown row happens to carry
    when it appears inside *another* manager's cohort (KTD12 keeps them so
    gameweek-rank conditions still rank correctly), but must never see them
    for its own row's streak.
    """
    if row.capture_status is CaptureStatus.UNKNOWN:
        return RunAction.HOLD
    return definition.predicate(row, previous_row, cohort)


# ---------------------------------------------------------------------------
# Folding one gameweek into run state
# ---------------------------------------------------------------------------


def _next_state(current: ConditionRunState, gameweek: int, action: RunAction) -> ConditionRunState:
    """The run state after applying one gameweek's action to the current one."""
    if action is RunAction.EXTEND:
        if current.length == 0:
            return ConditionRunState(length=1, start_gameweek=gameweek, held_in_run=0)
        return ConditionRunState(
            length=current.length + 1,
            start_gameweek=current.start_gameweek,
            held_in_run=current.held_in_run,
        )
    if action is RunAction.RESET:
        return ConditionRunState()
    # HOLD: before any run has opened there is nothing to record. Once one
    # is open, a hold extends its held-gameweek count without touching
    # length or where it started (KTD7) -- the run is not "broken" by a
    # hold, only annotated as having crossed one.
    if current.length == 0:
        return current
    return ConditionRunState(
        length=current.length,
        start_gameweek=current.start_gameweek,
        held_in_run=current.held_in_run + 1,
    )


def _fold_gameweek(
    runs: dict[int, dict[str, ConditionRunState]],
    gameweek: int,
    resolved_rows: dict[int, LeagueHistoryRow],
    previous_rows: dict[int, LeagueHistoryRow],
    fpl_format: LeagueFormat,
) -> dict[int, dict[str, ConditionRunState]]:
    """Fold one gameweek's resolved rows into per-manager run state.

    Returns a new dict; `runs` is read, never mutated in place, so a caller
    walking several gameweeks during a rebuild never has two gameweeks'
    state accidentally aliased to the same mutable mapping. Only managers
    present in `resolved_rows` (an OK or an unknown row) are touched -- a
    manager with no row at all for this gameweek is out of scope for it,
    and their existing state, if any, carries forward completely
    untouched, not even counted towards a hold.
    """
    conditions = conditions_for_format(fpl_format)
    cohort = list(resolved_rows.values())
    updated: dict[int, dict[str, ConditionRunState]] = {
        manager_key: dict(states) for manager_key, states in runs.items()
    }

    for manager_key, row in resolved_rows.items():
        previous_row = previous_rows.get(manager_key)
        manager_states = dict(updated.get(manager_key, {}))
        for condition in conditions:
            current_state = manager_states.get(condition.key, ConditionRunState())
            action = _evaluate(condition, row, previous_row, cohort)
            manager_states[condition.key] = _next_state(current_state, gameweek, action)
        updated[manager_key] = manager_states

    return updated


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------


def counters_dir() -> Path:
    """Root of the counters projection cache, under the writable data dir.

    Resolved per call, never bound to a module-level constant: an
    `FPL_CLI_DATA_DIR` set after import (a late-loaded `.env`) must still be
    honoured, and `tests/test_paths.py` enforces this across the package
    via an AST check.
    """
    return user_data_dir() / "league_history_counters"


def counters_partition_dir(season: str, fpl_format: LeagueFormat, league_id: int) -> Path:
    """Directory holding one partition's projection file.

    Mirrors the ledger's own season/format/league_id partitioning
    (`fpl_cli.services.league_history.partition_dir`), but lives in a
    separate directory tree: unlike the ledger, this file is a disposable
    cache the projection engine may freely discard and rebuild (KTD10).
    """
    return counters_dir() / season / f"{fpl_format}-{league_id}"


def counters_file(season: str, fpl_format: LeagueFormat, league_id: int) -> Path:
    """Path of the single projection file for one partition."""
    return counters_partition_dir(season, fpl_format, league_id) / "counters.json"


def _load_projection(store: LeagueHistoryStore) -> LeagueHistoryCountersProjection | None:
    """The persisted projection for a partition, or None if it must be rebuilt.

    Fails open (KTD10): a missing file, one that fails to parse, one
    stamped with a version this code no longer produces, or one that (by
    corruption or tampering) claims a different partition than the one
    asked for -- all return None. The caller's response to None is always a
    full rebuild, never a raise.
    """
    path = counters_file(store.season, store.fpl_format, store.league_id)
    if not path.is_file():
        return None

    try:
        text = path.read_text(encoding="utf-8")
        projection = LeagueHistoryCountersProjection.model_validate_json(text)
    except (OSError, ValidationError) as exc:
        logger.warning("League history counters file %s is unreadable, rebuilding: %s", path, exc)
        return None

    if projection.version != LEAGUE_HISTORY_COUNTERS_VERSION:
        logger.info(
            "League history counters file %s is version %s (current %s); rebuilding.",
            path, projection.version, LEAGUE_HISTORY_COUNTERS_VERSION,
        )
        return None

    if (
        projection.season != store.season
        or projection.fpl_format != store.fpl_format
        or projection.league_id != store.league_id
    ):
        logger.warning(
            "League history counters file %s does not match partition %s/%s-%s; rebuilding.",
            path, store.season, store.fpl_format, store.league_id,
        )
        return None

    return projection


def _save_projection(projection: LeagueHistoryCountersProjection) -> None:
    """Persist the projection, best-effort.

    A write failure here must not block the recap that triggered it (KTD10)
    -- it just means the next call finds a stale or missing stamp and
    rebuilds, which is the same fallback a lost race would trigger.
    """
    path = counters_file(projection.season, projection.fpl_format, projection.league_id)
    try:
        atomic_write_text(path, projection.model_dump_json())
    except OSError as exc:
        logger.warning("Could not persist league history counters to %s: %s", path, exc)


# ---------------------------------------------------------------------------
# Public computation entry points
# ---------------------------------------------------------------------------


def rebuild_counters_through(
    store: LeagueHistoryStore, through_gameweek: int,
) -> LeagueHistoryCountersProjection:
    """Recompute the full projection from the ledger, gameweek 1..through_gameweek.

    Pure: never reads or writes the persisted cache. This is both the
    fallback a stale, missing, or version-mismatched cache rebuilds through
    (KTD10), and the point-in-time / season-finale read (R15) -- "what were
    the counters as of gameweek N" for any N, independent of whatever the
    cache currently holds.

    Gameweeks are walked one at a time in strict numeric order, rather than
    only the gameweeks the store happens to hold: a gap (a gameweek never
    captured at all) must not be silently skipped over when computing the
    *next* captured gameweek's previous-row comparison, or a two-week gap
    would read as an ordinary one-week transition.
    """
    runs: dict[int, dict[str, ConditionRunState]] = {}
    previous_rows: dict[int, LeagueHistoryRow] = {}
    for gameweek in range(1, through_gameweek + 1):
        try:
            resolved = store.resolved_gameweek(gameweek)
        except LeagueHistoryError as exc:
            # One corrupt gameweek must not block the rest of the rebuild,
            # the same per-gameweek scoping `LeagueHistoryStore.coverage()`
            # already applies. Treated as if nothing was captured that
            # week, so the *next* captured gameweek correctly finds no
            # previous_row to compare against rather than reaching further
            # back and mislabelling a multi-week gap as one week.
            logger.warning(
                "GW%s unreadable while rebuilding league history counters for %s/%s-%s; "
                "treated as uncaptured: %s",
                gameweek, store.season, store.fpl_format, store.league_id, exc,
            )
            resolved = {}
        runs = _fold_gameweek(runs, gameweek, resolved, previous_rows, store.fpl_format)
        previous_rows = resolved

    return LeagueHistoryCountersProjection(
        season=store.season,
        fpl_format=store.fpl_format,
        league_id=store.league_id,
        computed_through_gameweek=through_gameweek,
        runs=runs,
    )


def compute_counters_through(
    store: LeagueHistoryStore, through_gameweek: int,
) -> LeagueHistoryCountersProjection:
    """Counters for this partition after folding in `through_gameweek`.

    This is the weekly-path entry point: it always persists what it
    computes, so the next call can advance from it. The cached projection
    advances by exactly one gameweek when its stamp is
    `through_gameweek - 1`; anything else -- missing, unreadable, or
    wrong-version cache, a backfill behind the stamp, or a multi-gameweek
    catch-up ahead of it -- rebuilds fully (KTD10). A lost race on the
    cache file costs a rebuild, never wrong data.
    """
    existing = _load_projection(store)

    if existing is not None and existing.computed_through_gameweek == through_gameweek - 1:
        try:
            resolved = store.resolved_gameweek(through_gameweek)
            previous_resolved = (
                store.resolved_gameweek(through_gameweek - 1) if through_gameweek > 1 else {}
            )
        except LeagueHistoryError as exc:
            logger.warning(
                "Could not advance league history counters for %s/%s-%s to GW%s incrementally; "
                "rebuilding instead: %s",
                store.season, store.fpl_format, store.league_id, through_gameweek, exc,
            )
        else:
            runs = _fold_gameweek(
                existing.runs, through_gameweek, resolved, previous_resolved, store.fpl_format,
            )
            projection = LeagueHistoryCountersProjection(
                season=store.season,
                fpl_format=store.fpl_format,
                league_id=store.league_id,
                computed_through_gameweek=through_gameweek,
                runs=runs,
            )
            _save_projection(projection)
            return projection

    projection = rebuild_counters_through(store, through_gameweek)
    _save_projection(projection)
    return projection


# ---------------------------------------------------------------------------
# Public read views
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ConditionRunView:
    """One manager's run for one condition, combined with its registry threshold.

    The read-facing counterpart to `ConditionRunState` (what gets
    persisted): adds `label`, `is_reportable` (length >= the condition's own
    minimum), and `excess` (R12: how far a run has gone past its minimum, so
    a caller can rank surfaced entries by it) -- so a caller, U9's rendering,
    not built here, never has to cross-reference the registry itself.
    """

    condition_key: str
    label: str
    length: int
    start_gameweek: int | None
    held_in_run: int
    min_run: int

    @property
    def is_reportable(self) -> bool:
        return self.length >= self.min_run

    @property
    def excess(self) -> int:
        """How far `length` sits past `min_run`. Negative below the minimum;
        the ranking signal R12 asks for is only meaningful once
        `is_reportable` is true."""
        return self.length - self.min_run


def manager_condition_views(
    projection: LeagueHistoryCountersProjection, manager_key: int,
) -> dict[str, ConditionRunView]:
    """Every condition applicable to this projection's format, for one manager.

    Always one entry per applicable condition key -- a manager with no run
    open for a condition gets a fresh, non-reportable `ConditionRunView`
    (length 0) rather than a missing key, so a caller never has to guess
    whether "absent" means "no run" or "not computed yet".
    """
    manager_runs = projection.runs.get(manager_key, {})
    views: dict[str, ConditionRunView] = {}
    for condition in conditions_for_format(projection.fpl_format):
        state = manager_runs.get(condition.key, ConditionRunState())
        views[condition.key] = ConditionRunView(
            condition_key=condition.key,
            label=condition.label,
            length=state.length,
            start_gameweek=state.start_gameweek,
            held_in_run=state.held_in_run,
            min_run=condition.min_run,
        )
    return views


def all_condition_views(
    projection: LeagueHistoryCountersProjection,
) -> dict[int, dict[str, ConditionRunView]]:
    """`manager_condition_views` for every manager the projection has touched."""
    return {
        manager_key: manager_condition_views(projection, manager_key)
        for manager_key in projection.runs
    }
