"""Capture orchestration: turn a collected recap into durable ledger rows.

Capture rides `league-recap` rather than living behind its own command (R1):
the recap already holds derived fields the raw API responses do not, and it
already pays for every call a row needs. It runs between fines evaluation and
LLM synthesis, because rendering happens after synthesis and a gameweek
captured at render time would be too late for anything the prompt reads.

A store failure never fails the run. The rows are built before the store is
touched, so a corrupt file costs the write and nothing else: the recap still
renders from live data and the command still exits 0 (R4).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Collection
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

from fpl_cli.cli._context import error_console
from fpl_cli.cli._fines import (
    COHORT_ONLY_RULE_TYPES,
    FinesLeagueData,
    WorstPerformer,
    evaluate_rules,
    rules_for_format,
)
from fpl_cli.cli._fines_config import FineRule, FinesConfig
from fpl_cli.cli._league_recap_data import (
    _PICKS_CONCURRENCY,
    _has_previous_gameweek,
    _recap_fine_message,
    derive_point_in_time_positions,
    raw_chip_name,
    recap_manager_key,
)
from fpl_cli.cli._league_recap_types import (
    LeagueRecapData,
    RecapManagerEntry,
    RecapStandingsEntry,
)
from fpl_cli.models.league_history import (
    CaptureStatus,
    FidelityTier,
    LeagueFormat,
    LeagueHistoryRow,
    LedgerCaptaincy,
    LedgerFine,
    LedgerPlayer,
    LedgerTransaction,
    LedgerTransfer,
)
from fpl_cli.season import season_label
from fpl_cli.services.league_history import (
    GameweekCoverage,
    LeagueHistoryError,
    LeagueHistoryStore,
)
from fpl_cli.services.league_history_counters import invalidate_if_repaired
from fpl_cli.services.league_history_fines import SeasonFinesTally, build_season_fines_tally
from fpl_cli.services.league_history_notes import NotesPack, build_notes_pack
from fpl_cli.utils.gameweek import format_gameweek_list

if TYPE_CHECKING:
    from fpl_cli.cli._league_recap_data import ManagerHistoryClient

# Replays one finished gameweek through the collectors Phase A corrected, and
# returns what they collected (or None when that gameweek cannot be rebuilt).
# Passed in rather than built here, so this module never needs to know about
# FPLClient, bootstrap data, or fixtures.
ReplayGameweek = Callable[[int], Awaitable["LeagueRecapData | None"]]

logger = logging.getLogger(__name__)

# Machine-readable warning codes, paired with the human line printed to stderr.
# Same shape the `stats` command already emits, so `--format json` can carry
# them without a second vocabulary.
HISTORY_WARNING_STORE_UNREADABLE = "league_history_store_unreadable"
HISTORY_WARNING_LEAGUE_ID_MISSING = "league_history_league_id_missing"
HISTORY_WARNING_UNMATCHED_PLAYERS = "league_history_unmatched_players"
HISTORY_WARNING_TRANSFER_DETAIL_SHORT = "league_history_transfer_detail_short"
HISTORY_WARNING_STANDINGS_TRUNCATED = "league_history_standings_truncated"
HISTORY_WARNING_COVERAGE = "league_history_coverage"
HISTORY_WARNING_BACKFILL_MANAGER_UNREACHABLE = "league_history_backfill_manager_unreachable"
HISTORY_WARNING_BACKFILL_REPLAY_FAILED = "league_history_backfill_replay_failed"
HISTORY_WARNING_BACKFILL_WRITE_FAILED = "league_history_backfill_write_failed"


@dataclass
class CaptureResult:
    """What one capture produced, whether or not the store accepted it."""

    rows: list[LeagueHistoryRow] = field(default_factory=list)
    written: list[LeagueHistoryRow] = field(default_factory=list)
    store_readable: bool = True
    warnings: list[dict[str, str]] = field(default_factory=list)
    coverage: list[GameweekCoverage] = field(default_factory=list)
    # None exactly when store_readable is False: a pack needs a readable
    # store to build from, and R4's degrade-gracefully contract means a
    # store failure costs the pack, not the rest of the recap.
    notes_pack: NotesPack | None = None
    # None for the same reason `notes_pack` is: the tally is a fold over the
    # store, so an unreadable store costs it rather than costing the recap.
    fines_tally: SeasonFinesTally | None = None
    # Set only on this partition's very first capture -- computed once here,
    # from the same `is_first_season_capture` check the stderr notice below
    # already makes, rather than a second, independent probe against a fresh
    # `LeagueHistoryStore` at the CLI layer (which would both duplicate the
    # check and widen the window between it and the write it precedes).
    first_capture_store_path: Path | None = None


def _warn(warnings: list[dict[str, str]], code: str, message: str) -> None:
    """Record a warning and show it on stderr.

    stdout carries the recap (and, later, the JSON payload), so every warning
    goes to `error_console` -- the split `fpl_cli/cli/_context.py` sets up.
    """
    warnings.append({"code": code, "message": message})
    error_console.print(f"[yellow]{message}[/yellow]")


# ---------------------------------------------------------------------------
# Row building
# ---------------------------------------------------------------------------


def _ledger_players(manager: RecapManagerEntry) -> list[LedgerPlayer]:
    return [
        LedgerPlayer(
            name=p["name"],
            team=p["team"],
            position=p["position"],
            code=p["code"],
            points=p["points"],
            is_captain=p["is_captain"],
            is_vice_captain=p["is_vice_captain"],
            contributed=p["contributed"],
            is_bench_boost_player=p["is_bench_boost_player"],
            auto_sub_in=p["auto_sub_in"],
            auto_sub_out=p["auto_sub_out"],
            red_cards=p["red_cards"],
            unmatched=p["unmatched"],
            had_fixture=p["had_fixture"],
        )
        for p in manager["squad"]
    ]


def _captaincy(
    squad: list[LedgerPlayer], name: str, points: int, *, played: bool | None,
) -> LedgerCaptaincy | None:
    """Build the captain or vice entry, enriched from the recorded squad.

    Draft has no captaincy at all, so an empty name means no entry rather than
    an entry naming nobody. `played` is set for the captain and left None for
    the vice, which the collector never records it for.
    """
    if not name:
        return None
    pick = next((p for p in squad if p.name == name), None)
    return LedgerCaptaincy(
        name=name,
        code=pick.code if pick else None,
        points=points,
        played=played,
        # R20 gates the captain-blank condition on this: a captain whose club
        # had no fixture never contributes to a run.
        had_fixture=pick.had_fixture if (pick and played is not None) else None,
    )


def _fines_for(
    data: LeagueRecapData, manager_key: int, manager_name: str,
) -> list[LedgerFine]:
    """Fines ruled against one manager, keyed rather than matched by name.

    A fine with no key falls back to the display name, which is what a caller
    building `RecapFineResult` by hand still produces -- two managers sharing a
    name is exactly why the key exists, so the fallback is the last resort.
    """
    out: list[LedgerFine] = []
    for fine in data.get("fines", []):
        key = fine.get("manager_key")
        matched = key == manager_key if key is not None else fine["manager_name"] == manager_name
        if matched:
            out.append(LedgerFine(
                manager_key=manager_key,
                rule_type=fine["rule_type"],
                message=fine["message"],
            ))
    return out


def _known_row(
    data: LeagueRecapData,
    manager: RecapManagerEntry,
    cohort_entry: RecapStandingsEntry | None,
    *,
    season: str,
    league_id: int,
    captured_at: datetime,
    tier: FidelityTier,
) -> LeagueHistoryRow:
    fpl_format: LeagueFormat = "draft" if data["fpl_format"] == "draft" else "classic"
    manager_key = recap_manager_key(manager)
    squad = _ledger_players(manager)

    transfers = [
        LedgerTransfer(
            player_in=t["player_in"],
            player_in_team=t["player_in_team"],
            player_in_points=t["player_in_points"],
            player_in_code=t.get("player_in_code"),
            player_out=t["player_out"],
            player_out_team=t["player_out_team"],
            player_out_points=t["player_out_points"],
            player_out_code=t.get("player_out_code"),
            net=t["net"],
            cost=t["cost"],
        )
        for t in manager.get("transfers", [])
    ]
    transactions = [
        LedgerTransaction(
            player_in=t["player_in"],
            player_in_team=t["player_in_team"],
            player_in_points=t["player_in_points"],
            player_in_code=t.get("player_in_code"),
            player_out=t["player_out"],
            player_out_team=t["player_out_team"],
            player_out_points=t["player_out_points"],
            player_out_code=t.get("player_out_code"),
            net=t["net"],
            kind=t["kind"],
        )
        for t in manager.get("transactions", [])
    ]

    transfers_made = manager.get("transfers_made")
    shortfall = None if transfers_made is None else max(0, transfers_made - len(transfers))

    return LeagueHistoryRow(
        season=season,
        fpl_format=fpl_format,
        league_id=league_id,
        gameweek=data["gameweek"],
        manager_key=manager_key,
        capture_status=CaptureStatus.OK,
        tier=tier,
        captured_at=captured_at,
        manager_name=manager["manager_name"],
        entry_id=cohort_entry["entry_id"] if cohort_entry else (manager["entry_id"] or None),
        gross_points=manager["gross_points"],
        # Draft charges nothing for a squad change, so there is no hit to
        # measure: the collector's structural zero would otherwise be stored
        # as a recorded "took no hit" (issue #147).
        transfer_cost=manager["transfer_cost"] if fpl_format == "classic" else None,
        total_points=manager.get("total_points"),
        gw_rank=manager["gw_rank"],
        league_position=manager.get("overall_rank"),
        # Null on the league's first scored gameweek whatever the collector
        # handed over: there was no table to move from, and a row saying
        # "same position as last week" is indistinguishable from a real flat
        # week once the API has collapsed the season (issue #147). Asked
        # through the same helper the collectors gate their derivations with,
        # so the two can never disagree about which gameweek that is -- a
        # league that started at GW12 has no predecessor at GW12 either.
        previous_league_position=(
            manager.get("previous_rank")
            if _has_previous_gameweek(data["gameweek"], data.get("league_start_event"))
            else None
        ),
        captain=_captaincy(
            squad, manager["captain"], manager["captain_points"],
            played=manager["captain_played"],
        ),
        vice_captain=_captaincy(
            squad, manager["vice_captain"], manager["vice_captain_points"], played=None,
        ),
        active_chip=raw_chip_name(manager["active_chip"]),
        bench_points=manager["bench_points"],
        squad=squad,
        transfers=transfers,
        transactions=transactions,
        gameweek_blank=data.get("is_bgw"),
        gameweek_double=data.get("is_dgw"),
        fines=_fines_for(data, manager_key, manager["manager_name"]),
        # What was ruled, not just what triggered. Absent from `data` means
        # the caller never evaluated fines at all, which stays `None` -- the
        # "nothing is recorded either way" state -- rather than collapsing
        # into the `[]` that means "ruled, nothing configured" (issue #136).
        fine_rules_evaluated=data.get("fine_rules_evaluated"),
        team_value=manager.get("team_value"),
        bank=manager.get("bank"),
        global_rank=manager.get("global_rank"),
        global_gw_rank=manager.get("global_gw_rank"),
        transfers_made=transfers_made,
        transfer_detail_shortfall=shortfall,
    )


def _unknown_row(
    data: LeagueRecapData,
    cohort_entry: RecapStandingsEntry,
    *,
    season: str,
    league_id: int,
    captured_at: datetime,
    tier: FidelityTier,
    is_live_gw: bool,
) -> LeagueHistoryRow:
    """A row for a member the capture could not reach (R19).

    It records what standings knew, and only when standings describe the same
    point in time -- on a replay they describe a later one, so the numbers are
    left unset rather than written as this gameweek's. The unknown status is
    what stops any streak condition reading them either way: a condition holds
    on an unknown row rather than extending or resetting.

    For classic, the standings figure is `event_total`, which is net of any
    transfer-cost hit rather than gross whenever this manager took one --
    unobservable here since the fetch that would show it is exactly what
    failed. `gross_points` is documented as always gross for a `capture_status
    == OK` row; on this UNKNOWN row it may not be. That is accepted rather
    than left unset, because `_assign_cohort_ranks` needs it net-of-the-hit to
    rank this manager against the rest of the cohort (KTD12) -- excluding them
    would risk handing someone else the week's best or worst instead.
    """
    fpl_format: LeagueFormat = "draft" if data["fpl_format"] == "draft" else "classic"
    return LeagueHistoryRow(
        season=season,
        fpl_format=fpl_format,
        league_id=league_id,
        gameweek=data["gameweek"],
        manager_key=cohort_entry["manager_key"],
        capture_status=CaptureStatus.UNKNOWN,
        tier=tier,
        captured_at=captured_at,
        manager_name=cohort_entry["manager_name"],
        entry_id=cohort_entry["entry_id"],
        gross_points=cohort_entry["gw_points"] if is_live_gw else None,
        total_points=cohort_entry["total_points"] if is_live_gw else None,
        gameweek_blank=data.get("is_bgw"),
        gameweek_double=data.get("is_dgw"),
    )


def _assign_cohort_ranks(rows: list[LeagueHistoryRow]) -> None:
    """Derive gameweek rank and league position across everyone recorded.

    Ranking only the managers whose fetch succeeded hands someone else the
    week's best -- or, worse, the week's worst. Every row for the gameweek is
    in the pool, including the unknown ones that carry standings numbers
    (KTD12). Gameweek points are always taken net of the hit, which is what the
    league table counts and is independent of the `use_net_points` display
    setting.

    A position the collector already supplied is left alone: draft's own
    standings rank breaks head-to-head ties on points-for, which re-deriving
    from a cumulative total cannot.
    """
    gw_points = [
        (row.manager_key, row.gross_points - (row.transfer_cost or 0))
        for row in rows
        if row.gross_points is not None
    ]
    gw_ranks = derive_point_in_time_positions(gw_points)

    totals = [(row.manager_key, row.total_points) for row in rows if row.total_points is not None]
    positions = derive_point_in_time_positions(totals)

    for row in rows:
        rank = gw_ranks.get(row.manager_key)
        if rank is not None:
            row.gw_rank = rank
        if row.league_position is None:
            row.league_position = positions.get(row.manager_key)


def build_history_rows(
    data: LeagueRecapData,
    *,
    season: str,
    captured_at: datetime,
    league_id: int | None = None,
    tier: FidelityTier = FidelityTier.DETAILED,
    is_live_gw: bool = True,
) -> list[LeagueHistoryRow]:
    """One row per league member, in standings order.

    Built before the store is touched, so a store failure costs only the write
    and every downstream surface still sees the full manager set.

    Raises:
        ValueError: no league id, so the rows could not be keyed to a
            partition. `capture_recap_history` reports that as a warning
            before it gets here.
    """
    resolved_league_id = league_id if league_id is not None else data.get("league_id")
    if resolved_league_id is None:
        raise ValueError(
            "Cannot build league history rows without a league id: set the league id "
            "for this format in settings.yaml.",
        )
    cohort = data.get("standings_cohort") or [
        RecapStandingsEntry(
            manager_key=recap_manager_key(m),
            manager_name=m["manager_name"],
            entry_id=m["entry_id"] or None,
            gw_points=m["gross_points"],
            total_points=m.get("total_points", 0),
        )
        for m in data["managers"]
    ]
    collected = {recap_manager_key(m): m for m in data["managers"]}

    rows: list[LeagueHistoryRow] = []
    for entry in cohort:
        manager = collected.get(entry["manager_key"])
        if manager is None:
            rows.append(_unknown_row(
                data, entry, season=season, league_id=resolved_league_id,
                captured_at=captured_at, tier=tier, is_live_gw=is_live_gw,
            ))
        else:
            rows.append(_known_row(
                data, manager, entry, season=season, league_id=resolved_league_id,
                captured_at=captured_at, tier=tier,
            ))
    _assign_cohort_ranks(rows)
    return rows


# ---------------------------------------------------------------------------
# Draft cumulative totals
# ---------------------------------------------------------------------------


def _fill_draft_cumulative_totals(
    store: LeagueHistoryStore,
    rows: list[LeagueHistoryRow],
    *,
    gameweek: int,
    start_gameweek: int,
) -> None:
    """Sum a replayed draft gameweek's cumulative total from captured rows.

    Draft exposes no per-manager history endpoint, so a replayed gameweek has
    no point-in-time total until the ledger can supply one. A manager whose run
    back to the league's start has any hole keeps their total unset: reporting
    a partial sum as a cumulative total would be worse than reporting it
    unavailable (R10).
    """
    needed = [r for r in rows if r.total_points is None and r.gross_points is not None]
    if not needed:
        return

    running: dict[int, int] = {r.manager_key: 0 for r in needed}
    unbroken = set(running)
    for earlier in range(start_gameweek, gameweek):
        try:
            resolved = store.resolved_gameweek(earlier)
        except LeagueHistoryError as exc:
            logger.debug("Cannot sum draft totals across GW%s: %s", earlier, exc)
            return
        for key in list(unbroken):
            prior = resolved.get(key)
            if prior is None or prior.capture_status is CaptureStatus.UNKNOWN or prior.gross_points is None:
                unbroken.discard(key)
                continue
            running[key] += prior.gross_points

    for row in needed:
        if row.manager_key in unbroken and row.gross_points is not None:
            row.total_points = running[row.manager_key] + row.gross_points


# ---------------------------------------------------------------------------
# Data-quality warnings
# ---------------------------------------------------------------------------


def _quality_warnings(
    data: LeagueRecapData, rows: list[LeagueHistoryRow], warnings: list[dict[str, str]],
) -> None:
    """Surface the three ways a row can record less than it appears to.

    Each is silent when there is nothing to act on, so a clean capture adds no
    noise to the recap.
    """
    unmatched = {
        row.manager_name: [p.name for p in row.squad if p.unmatched]
        for row in rows
        if any(p.unmatched for p in row.squad)
    }
    if unmatched:
        detail = "; ".join(f"{name}: {', '.join(players)}" for name, players in unmatched.items())
        total = sum(len(players) for players in unmatched.values())
        _warn(
            warnings, HISTORY_WARNING_UNMATCHED_PLAYERS,
            f"Captured {total} draft player(s) that could not be matched to a main-game "
            f"player, so their recorded points are zero rather than a real score ({detail}).",
        )

    short = [
        (row.manager_name, row.transfers_made, len(row.transfers))
        for row in rows
        if row.transfer_detail_shortfall
    ]
    if short:
        detail = "; ".join(f"{name}: {made} made, {got} captured" for name, made, got in short)
        _warn(
            warnings, HISTORY_WARNING_TRANSFER_DETAIL_SHORT,
            f"Transfer detail came back short for {len(short)} manager(s), so the recorded "
            f"list is incomplete rather than empty ({detail}).",
        )

    if data.get("standings_truncated"):
        fetched = len(data.get("standings_cohort") or data["managers"])
        size = data.get("league_size")
        scope = f"{fetched} of {size} members" if size else f"only the first {fetched} members"
        _warn(
            warnings, HISTORY_WARNING_STANDINGS_TRUNCATED,
            f"The league standings response covered {scope}, so this gameweek is recorded "
            f"for that subset only. Paginating past one standings page is not supported yet.",
        )


# ---------------------------------------------------------------------------
# Backfill (U7)
# ---------------------------------------------------------------------------

# Named rather than restated: the coarse tier carries headline numbers only, so
# these are the recap surfaces that stay dark until the detailed tier fills a
# gameweek. Reporting a gap the user cannot act on is not visibility (R9).
COARSE_HELD_BACK = (
    "captain and vice detail (captain-blank streaks)",
    "per-player squad contributions (bench and blank rendering)",
    "transfer and waiver detail (transfer/waiver streaks and awards)",
)

DETAIL_FLAG = "--backfill-detail"


def _target_gameweeks(data: LeagueRecapData, finished_gameweeks: Collection[int]) -> list[int]:
    """Finished gameweeks the league actually existed for, up to this one.

    A league created at GW12 has no GW1 to backfill, so its start gameweek --
    not GW1 -- is the floor. An unfinished gameweek is never a gap: its numbers
    are still moving.
    """
    start = data.get("league_start_event") or 1
    current = data["gameweek"]
    return sorted(gw for gw in set(finished_gameweeks) if start <= gw <= current)


@dataclass(frozen=True)
class _Gaps:
    """Target gameweeks split by what backfilling them would achieve."""

    missing: list[int] = field(default_factory=list)
    incomplete: list[int] = field(default_factory=list)
    coarse: list[int] = field(default_factory=list)


def _gaps(coverage: list[GameweekCoverage], targets: list[int]) -> _Gaps:
    """Classify the target gameweeks: never captured, holding unknown rows, coarse.

    An unreadable gameweek is in none of them: it is reported, not overwritten
    -- a repair that replaced it would destroy whatever it still holds (R4).
    """
    by_gameweek = {c.gameweek: c for c in coverage}
    missing: list[int] = []
    incomplete: list[int] = []
    coarse: list[int] = []
    for gameweek in targets:
        entry = by_gameweek.get(gameweek)
        if entry is None:
            missing.append(gameweek)
            continue
        if not entry.readable:
            continue
        if entry.manager_count == 0:
            missing.append(gameweek)
            continue
        if entry.unknown_count:
            incomplete.append(gameweek)
        if entry.lowest_tier is FidelityTier.COARSE:
            coarse.append(gameweek)
    return _Gaps(missing=missing, incomplete=incomplete, coarse=coarse)


def _coarse_row(
    history_row: dict[str, Any],
    cohort_entry: RecapStandingsEntry,
    *,
    season: str,
    league_id: int,
    captured_at: datetime,
    baseline_total: int,
) -> LeagueHistoryRow:
    """Map one manager-history row to a coarse ledger row.

    Only the fields the endpoint actually returned are recorded -- a condition
    whose fields this tier does not carry holds rather than evaluating (R8).
    Ranks are deliberately absent here: `overall_rank` is the FPL-wide ladder,
    and every rank the recap means is a position inside the mini-league, so
    both are derived across the cohort afterwards (KTD12).
    """
    def _int(key: str) -> int | None:
        value = history_row.get(key)
        return value if isinstance(value, int) else None

    total = _int("total_points")
    return LeagueHistoryRow(
        season=season,
        fpl_format="classic",
        league_id=league_id,
        gameweek=history_row["event"],
        manager_key=cohort_entry["manager_key"],
        capture_status=CaptureStatus.OK,
        tier=FidelityTier.COARSE,
        captured_at=captured_at,
        manager_name=cohort_entry["manager_name"],
        entry_id=cohort_entry["entry_id"],
        gross_points=_int("points"),
        transfer_cost=_int("event_transfers_cost"),
        # The endpoint's total is season-wide; a league that started after GW1
        # scores its members only from its own start, so the baseline comes off.
        total_points=None if total is None else total - baseline_total,
        bench_points=_int("points_on_bench"),
        team_value=_int("value"),
        bank=_int("bank"),
        global_rank=_int("overall_rank"),
        global_gw_rank=_int("rank"),
        transfers_made=_int("event_transfers"),
    )


def _coarse_fine_rules(fines_config: FinesConfig | None, fpl_format: LeagueFormat) -> list[FineRule]:
    """The configured rules the coarse tier can actually rule on.

    The manager-history endpoint carries headline numbers and no squad, so
    `red-card` is structurally unevaluable there -- and a red-card handler
    handed an empty squad answers "no red card fine", which would record a
    false acquittal rather than an abstention (issue #136). Narrowing here,
    and stamping the narrowed list onto the row as `fine_rules_evaluated`,
    is what makes the partial ruling honest rather than implied complete.
    """
    if fines_config is None:
        return []
    return [
        rule for rule in rules_for_format(fines_config, fpl_format)
        if rule.type in COHORT_ONLY_RULE_TYPES
    ]


def _apply_coarse_fines(
    rows: list[LeagueHistoryRow], *, rules: list[FineRule], use_net_points: bool,
) -> None:
    """Rule the cohort-only fines across one coarse gameweek, in place.

    Runs after `_assign_cohort_ranks` for the same reason that does: last
    place is a fact about the whole cohort, not about one row. Only
    `capture_status == OK` rows carrying points are ruled -- an unknown row
    reached nobody, so it records no ruling at all (R19) rather than an
    acquittal by default.

    Every ruled row is stamped even when nothing triggered, which is the
    point: `fines == []` with `fine_rules_evaluated == ["last-place"]` is a
    recorded acquittal on that rule, while `fine_rules_evaluated is None` is
    silence.
    """
    ruled = [
        row for row in rows
        if row.capture_status is CaptureStatus.OK and row.gross_points is not None
    ]
    if not ruled:
        return

    rule_types = [rule.type for rule in rules]
    if not rules:
        # Nothing configured this tier can rule: still a recorded "no rule
        # covers this row", not silence.
        for row in ruled:
            row.fine_rules_evaluated = rule_types
        return

    def _gameweek_points(row: LeagueHistoryRow) -> int:
        gross = row.gross_points or 0
        return gross - (row.transfer_cost or 0) if use_net_points else gross

    # Same measure and same single-winner tie-break the live path uses
    # (`evaluate_league_fines`): whichever manager `min` reaches first is the
    # one fined, so a tie for last does not fine everyone tied.
    worst = min(ruled, key=_gameweek_points)

    for row in ruled:
        league_data = FinesLeagueData(
            user_gw_points=row.gross_points or 0,
            worst_performers=[WorstPerformer(
                is_user=row.manager_key == worst.manager_key,
                points=_gameweek_points(worst),
                gross_points=worst.gross_points or 0,
                name=worst.manager_name,
            )],
        )
        if use_net_points:
            league_data["user_gw_net_points"] = _gameweek_points(row)

        try:
            results = evaluate_rules(rules, league_data, [], use_net_points=use_net_points)
        except Exception:  # noqa: BLE001 — one bad rule must not cost the backfill
            logger.debug("Coarse fines evaluation failed for %s", row.manager_name, exc_info=True)
            continue

        row.fines = [
            LedgerFine(
                manager_key=row.manager_key,
                rule_type=result.rule_type,
                message=_recap_fine_message(result, row.manager_name),
            )
            for result in results if result.triggered
        ]
        row.fine_rules_evaluated = rule_types


async def _coarse_backfill(
    data: LeagueRecapData,
    *,
    store: LeagueHistoryStore,
    season: str,
    league_id: int,
    history_client: ManagerHistoryClient,
    gameweeks: list[int],
    fine_rules: list[FineRule],
    use_net_points: bool,
    warnings: list[dict[str, str]],
) -> set[int]:
    """Fill classic gameweeks from the manager-history endpoint.

    One call per manager covers the whole season, which is what makes this
    tier cheap enough to run unconditionally (KTD6). Every call holds the same
    permit the picks fetch does, so a large league cannot fan out unbounded.

    Returns the gameweeks that actually gained a superseding row -- this
    tier runs unconditionally, so `capture_recap_history` uses it to know
    which cached counters projection needs invalidating rather than trusted
    stale (see `invalidate_if_repaired`).
    """
    cohort = data.get("standings_cohort") or []
    if not cohort or not gameweeks:
        return set()

    captured_at = datetime.now(tz=timezone.utc)
    start = data.get("league_start_event") or 1
    semaphore = asyncio.Semaphore(_PICKS_CONCURRENCY)

    async def _fetch(entry: RecapStandingsEntry) -> tuple[RecapStandingsEntry, dict[str, Any]]:
        entry_id = entry["entry_id"]
        if entry_id is None:
            raise ValueError(f"{entry['manager_name']} has no FPL entry id")
        async with semaphore:
            return entry, await history_client.get_manager_history(entry_id)

    results = await asyncio.gather(
        *(_fetch(entry) for entry in cohort), return_exceptions=True,
    )

    rows_by_gameweek: dict[int, list[LeagueHistoryRow]] = {gw: [] for gw in gameweeks}
    for entry, outcome in zip(cohort, results):
        if isinstance(outcome, BaseException):
            logger.warning(
                "Manager history unavailable for %s; their gameweeks stay unknown: %s",
                entry["manager_name"], outcome,
            )
            _warn(
                warnings, HISTORY_WARNING_BACKFILL_MANAGER_UNREACHABLE,
                f"Could not backfill {entry['manager_name']}: their manager history "
                f"could not be fetched, so GW{gameweeks[0]}-{gameweeks[-1]} stay recorded as "
                f"unknown for them and will be re-attempted next run.",
            )
            for gameweek in gameweeks:
                rows_by_gameweek[gameweek].append(_coarse_unknown_row(
                    entry, season=season, league_id=league_id,
                    gameweek=gameweek, captured_at=captured_at,
                ))
            continue

        _, history = outcome
        by_event = {
            row["event"]: row
            for row in history.get("current", [])
            if isinstance(row, dict) and isinstance(row.get("event"), int)
        }
        baseline_row = by_event.get(start - 1) if start > 1 else None
        baseline_total = baseline_row.get("total_points", 0) if baseline_row else 0
        for gameweek in gameweeks:
            history_row = by_event.get(gameweek)
            if history_row is None:
                rows_by_gameweek[gameweek].append(_coarse_unknown_row(
                    entry, season=season, league_id=league_id,
                    gameweek=gameweek, captured_at=captured_at,
                ))
                continue
            rows_by_gameweek[gameweek].append(_coarse_row(
                history_row, entry, season=season, league_id=league_id,
                captured_at=captured_at, baseline_total=baseline_total,
            ))

    # Committed per gameweek, ascending, so an interruption keeps everything
    # already written rather than losing the whole fetch.
    repaired: set[int] = set()
    for gameweek in gameweeks:
        gameweek_rows = rows_by_gameweek[gameweek]
        if not gameweek_rows:
            continue
        _assign_cohort_ranks(gameweek_rows)
        _apply_coarse_fines(gameweek_rows, rules=fine_rules, use_net_points=use_net_points)
        try:
            written = store.append_rows(gameweek, gameweek_rows)
        except LeagueHistoryError as exc:
            _warn(warnings, HISTORY_WARNING_BACKFILL_WRITE_FAILED, str(exc))
            continue
        if written:
            repaired.add(gameweek)
    return repaired


def _coarse_unknown_row(
    entry: RecapStandingsEntry,
    *,
    season: str,
    league_id: int,
    gameweek: int,
    captured_at: datetime,
) -> LeagueHistoryRow:
    """A manager the coarse tier could not reach for one gameweek (R19)."""
    return LeagueHistoryRow(
        season=season,
        fpl_format="classic",
        league_id=league_id,
        gameweek=gameweek,
        manager_key=entry["manager_key"],
        capture_status=CaptureStatus.UNKNOWN,
        tier=FidelityTier.COARSE,
        captured_at=captured_at,
        manager_name=entry["manager_name"],
        entry_id=entry["entry_id"],
    )


async def _detailed_backfill(
    *,
    store: LeagueHistoryStore,
    season: str,
    league_id: int,
    replay_gameweek: ReplayGameweek,
    gameweeks: list[int],
    warnings: list[dict[str, str]],
) -> set[int]:
    """Replay whole gameweeks through the collectors Phase A corrected.

    Serial by gameweek, committing each as it completes: the collector already
    bounds its own per-manager concurrency, and committing per gameweek is what
    lets an interrupted backfill keep everything it already fetched.

    Returns the gameweeks that actually gained a superseding row -- see
    `_coarse_backfill`, which returns the same thing for the same reason.
    """
    repaired: set[int] = set()
    for gameweek in gameweeks:
        try:
            replayed = await replay_gameweek(gameweek)
        except Exception as exc:  # noqa: BLE001 — one bad gameweek must not abort the rest
            logger.warning("Detailed backfill of GW%s failed: %s", gameweek, exc)
            _warn(
                warnings, HISTORY_WARNING_BACKFILL_REPLAY_FAILED,
                f"Could not replay GW{gameweek} in detail: {exc}. "
                f"Other gameweeks are unaffected; re-run to retry it.",
            )
            continue
        if replayed is None:
            continue
        rows = build_history_rows(
            replayed,
            season=season,
            league_id=league_id,
            captured_at=datetime.now(tz=timezone.utc),
            tier=FidelityTier.DETAILED,
            is_live_gw=False,
        )
        try:
            written = store.append_rows(gameweek, rows)
        except LeagueHistoryError as exc:
            _warn(warnings, HISTORY_WARNING_BACKFILL_WRITE_FAILED, str(exc))
            continue
        if written:
            repaired.add(gameweek)
    return repaired


async def _backfill(
    data: LeagueRecapData,
    *,
    store: LeagueHistoryStore,
    season: str,
    league_id: int,
    fpl_format: LeagueFormat,
    history_client: ManagerHistoryClient | None,
    finished_gameweeks: Collection[int],
    replay_gameweek: ReplayGameweek | None,
    backfill_detail: bool,
    fines_config: FinesConfig | None,
    use_net_points: bool,
    warnings: list[dict[str, str]],
) -> set[int]:
    """Fill what this run is allowed to fill, cheapest tier first.

    Returns every gameweek that gained a superseding row this call, across
    both tiers -- `capture_recap_history` passes this straight to
    `invalidate_if_repaired`, since a repair either tier makes can land on a
    gameweek the counters cache has already folded in.
    """
    targets = _target_gameweeks(data, finished_gameweeks)
    if not targets:
        return set()

    gaps = _gaps(store.coverage(), targets)
    repaired: set[int] = set()

    if fpl_format == "classic" and history_client is not None:
        coarse_targets = sorted(set(gaps.missing) | set(gaps.incomplete))
        if coarse_targets:
            repaired |= await _coarse_backfill(
                data, store=store, season=season, league_id=league_id,
                history_client=history_client, gameweeks=coarse_targets,
                fine_rules=_coarse_fine_rules(fines_config, fpl_format),
                use_net_points=use_net_points,
                warnings=warnings,
            )
            gaps = _gaps(store.coverage(), targets)

    if replay_gameweek is None:
        return repaired

    # A gameweek already holding unknown rows is repaired without the flag:
    # the cost is bounded by how many gameweeks actually failed, and without it
    # an unknown row is permanent. Filling a gap or upgrading a coarse gameweek
    # is the unbounded case the flag guards -- one call per manager per
    # gameweek, which a mid-season first run would otherwise pay across the
    # whole season before printing anything (KTD6).
    detail_targets = set(gaps.incomplete)
    if backfill_detail:
        detail_targets |= set(gaps.missing) | set(gaps.coarse)
    if detail_targets:
        repaired |= await _detailed_backfill(
            store=store, season=season, league_id=league_id,
            replay_gameweek=replay_gameweek, gameweeks=sorted(detail_targets),
            warnings=warnings,
        )

    return repaired


# ---------------------------------------------------------------------------
# Coverage report (R9, R10)
# ---------------------------------------------------------------------------


def _report_coverage(
    coverage: list[GameweekCoverage],
    *,
    fpl_format: LeagueFormat,
    targets: list[int],
    warnings: list[dict[str, str]],
) -> None:
    """Say what is captured, at what fidelity, and what can still be done.

    Silent on a fully detailed season: a report the user cannot act on is
    noise, not visibility.
    """
    by_gameweek = {c.gameweek: c for c in coverage}
    coarse = [gw for gw in targets if (c := by_gameweek.get(gw)) and c.lowest_tier is FidelityTier.COARSE]
    unreadable = [gw for gw in targets if (c := by_gameweek.get(gw)) and not c.readable]
    unknown = [gw for gw in targets if (c := by_gameweek.get(gw)) and c.readable and c.unknown_count]
    uncaptured = [gw for gw in targets if gw not in by_gameweek or by_gameweek[gw].manager_count == 0]

    lines: list[str] = []
    if coarse:
        lines.append(
            f"League history: {format_gameweek_list(coarse)} captured at the coarse tier "
            f"(headline numbers only). Held back until filled: "
            f"{'; '.join(COARSE_HELD_BACK)}. Re-run with {DETAIL_FLAG} to fill them.",
        )
    if uncaptured:
        if fpl_format == "draft":
            lines.append(
                f"League history: {format_gameweek_list(uncaptured)} has no recorded rows. Draft "
                f"exposes no per-manager history endpoint, so each of those gameweeks' "
                f"league position and cumulative total is unavailable by name, and becomes "
                f"permanently unrecoverable at the July season rollover. "
                f"Re-run with {DETAIL_FLAG} to reconstruct them now.",
            )
        else:
            lines.append(
                f"League history: {format_gameweek_list(uncaptured)} has no recorded rows. "
                f"Re-run with {DETAIL_FLAG} to fill them.",
            )
    if unknown:
        lines.append(
            f"League history: {format_gameweek_list(unknown)} holds managers whose data could not "
            f"be fetched. They are recorded as unknown -- no streak is extended or broken "
            f"across them -- and every later run re-attempts them.",
        )
    if unreadable:
        lines.append(
            f"League history: {format_gameweek_list(unreadable)} could not be read and is skipped. "
            f"Move the file aside to recapture it; the rest of the season is unaffected.",
        )

    for line in lines:
        _warn(warnings, HISTORY_WARNING_COVERAGE, line)


# ---------------------------------------------------------------------------
# Previous-position correction (R13)
# ---------------------------------------------------------------------------


def _apply_recorded_previous_positions(
    data: LeagueRecapData, store: LeagueHistoryStore, gw: int,
) -> None:
    """R13: override `previous_rank` with the ledger's recorded GW-1 position
    wherever a prior-gameweek row exists, leaving it untouched (U3's derived
    path, or unset) for any manager with none. A store problem degrades to
    that same untouched fallback rather than raising (R4).

    Must run before `build_history_rows`, on the same `store` instance the
    caller already holds: the correction has to land in `data` before rows
    are built from it, so it reaches every downstream surface built from
    `rows` -- the persisted ledger row and the `--format json` payload --
    not just `data` itself, which console/report read directly and which
    this also mutates in place for them.
    """
    if gw <= 1:
        return

    try:
        previous_rows = store.resolved_gameweek(gw - 1)
    except LeagueHistoryError:
        return

    for m in data["managers"]:
        row = previous_rows.get(recap_manager_key(m))
        if row is not None and row.league_position is not None:
            m["previous_rank"] = row.league_position


# ---------------------------------------------------------------------------
# Capture
# ---------------------------------------------------------------------------


async def capture_recap_history(
    data: LeagueRecapData,
    *,
    season: str | None = None,
    is_live_gw: bool = True,
    history_client: ManagerHistoryClient | None = None,
    finished_gameweeks: Collection[int] = (),
    replay_gameweek: ReplayGameweek | None = None,
    backfill_detail: bool = False,
    fines_config: FinesConfig | None = None,
    use_net_points: bool = False,
) -> CaptureResult:
    """Record this gameweek, fill what the API still allows, and report the rest.

    Never raises for a store problem: the message goes to stderr, the rows come
    back regardless, and the caller carries on rendering (R4).

    `history_client` enables the coarse classic tier, which runs
    unconditionally because it costs one call per manager for the whole season.
    `replay_gameweek` enables the detailed tier, which costs one call per
    manager *per gameweek* and therefore runs only behind `backfill_detail` --
    except to repair a gameweek already holding unknown rows, which is bounded
    and is the only way R19's unknown rows ever get fixed.

    `fines_config` lets the coarse tier rule the fines it structurally can --
    the ones derivable from cohort points alone. Without it a backfilled
    gameweek lands fine-free, and an empty list is indistinguishable from a
    week nobody was fined in, so missing a week would un-fine it permanently
    (issue #136). The detailed tier needs nothing here: its replay goes
    through the same collectors and the same `evaluate_league_fines` the live
    path does, and arrives with `data["fines"]` already ruled.
    """
    season = season or season_label()
    league_id = data.get("league_id")
    warnings: list[dict[str, str]] = []

    if league_id is None:
        _warn(
            warnings, HISTORY_WARNING_LEAGUE_ID_MISSING,
            "No league id was resolved for this recap, so the gameweek was not recorded. "
            "Set the league id for this format in settings.yaml and re-run to capture it.",
        )
        return CaptureResult(store_readable=False, warnings=warnings)

    fpl_format: LeagueFormat = "draft" if data["fpl_format"] == "draft" else "classic"
    store = LeagueHistoryStore(season, fpl_format, league_id)
    is_first_season_capture = not store.partition_exists()
    first_capture_store_path = store.partition_dir() if is_first_season_capture else None

    # R13: correct `previous_rank` from the ledger's actually-recorded GW-1
    # position before rows are built from `data` -- on the same `store`
    # instance just constructed above, not a fresh one. Mutates `data` in
    # place, so console/report (which read it directly, after this returns)
    # see the same corrected value the row below is built from.
    _apply_recorded_previous_positions(data, store, data["gameweek"])

    rows = build_history_rows(
        data,
        season=season,
        league_id=league_id,
        captured_at=datetime.now(tz=timezone.utc),
        is_live_gw=is_live_gw,
    )
    if fpl_format == "draft" and rows:
        _fill_draft_cumulative_totals(
            store, rows,
            gameweek=data["gameweek"],
            start_gameweek=data.get("league_start_event") or 1,
        )
    _quality_warnings(data, rows, warnings)

    try:
        written = store.append_rows(data["gameweek"], rows)
    except LeagueHistoryError as exc:
        _warn(warnings, HISTORY_WARNING_STORE_UNREADABLE, str(exc))
        return CaptureResult(
            rows=rows, store_readable=False, warnings=warnings,
            first_capture_store_path=first_capture_store_path,
        )

    if is_first_season_capture:
        # The one moment a container-local data directory is still cheap to
        # notice: after the season is under way, everything written into an
        # ephemeral one is already gone.
        error_console.print(
            f"[dim]Recording league history to {store.partition_dir()} "
            f"(set FPL_CLI_DATA_DIR to keep it somewhere persistent).[/dim]",
        )

    repaired_gameweeks = await _backfill(
        data,
        store=store,
        season=season,
        league_id=league_id,
        fpl_format=fpl_format,
        history_client=history_client,
        finished_gameweeks=finished_gameweeks,
        replay_gameweek=replay_gameweek,
        backfill_detail=backfill_detail,
        fines_config=fines_config,
        use_net_points=use_net_points,
        warnings=warnings,
    )
    # A repair `_backfill` just made can land on a gameweek the counters
    # cache already folded in; compute_counters_through's fast path would
    # otherwise trust that gameweek's old, unrepaired contribution. Must run
    # before build_notes_pack, which is what actually calls it.
    invalidate_if_repaired(store, repaired_gameweeks)

    coverage = store.coverage()
    _report_coverage(
        coverage,
        fpl_format=fpl_format,
        targets=_target_gameweeks(data, finished_gameweeks),
        warnings=warnings,
    )

    # Built from the store, not from `rows`: U9's pack reads the counters
    # projection and a trailing window of already-persisted rows, so it sees
    # this gameweek's just-written rows the same way it will on every later
    # run (U9 fails open internally -- a store problem here costs the pack,
    # never the recap).
    notes_pack = build_notes_pack(
        store, data["gameweek"], league_start_gameweek=data.get("league_start_event") or 1,
    )

    # Also built from the store rather than from `rows`: the season table is a
    # fold over every captured gameweek, and this run's rows are already in it
    # by the time this is reached. Shares the store's memoized gameweek reads
    # with the notes pack above, so the gameweeks they both touch are read
    # once (issue #136).
    fines_tally = build_season_fines_tally(
        store,
        data["gameweek"],
        league_start_gameweek=data.get("league_start_event") or 1,
        rule_types=data.get("fine_rules_evaluated") or [],
    )

    return CaptureResult(
        rows=rows,
        written=written,
        store_readable=True,
        warnings=warnings,
        coverage=coverage,
        notes_pack=notes_pack,
        fines_tally=fines_tally,
        first_capture_store_path=first_capture_store_path,
    )
