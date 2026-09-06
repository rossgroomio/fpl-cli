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
    RED_CARD_RULE_TYPE,
    FinesLeagueData,
    FinesTeamPlayer,
    WorstPerformer,
    evaluate_rules,
    red_card_offenders,
    rules_for_format,
)
from fpl_cli.cli._fines_config import FineRule, FinesConfig
from fpl_cli.cli._league_recap_data import (
    _PICKS_CONCURRENCY,
    _has_previous_gameweek,
    _recap_fine_message,
    derive_point_in_time_positions,
    raw_chip_name,
    recap_fine_player_names,
    recap_manager_key,
    restate_recap_fine_players,
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
    LedgerFinePlayer,
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
HISTORY_WARNING_IDENTITY_CARRIED = "league_history_identity_carried"
HISTORY_WARNING_CLUB_REDERIVED = "league_history_club_rederived"
HISTORY_WARNING_STANDINGS_CARRIED = "league_history_standings_carried"
HISTORY_WARNING_STANDINGS_REPAIRED = "league_history_standings_repaired"


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


def _ruled_rules_for(data: LeagueRecapData, manager_key: int) -> list[str] | None:
    """The rule types this manager was actually ruled against, if any.

    `fine_rules_evaluated` is a fact about the gameweek's configuration;
    whether a given manager was measured against it is a fact about their
    own evaluation, which `evaluate_league_fines` drops silently when a rule
    handler raises. Stamping the configured list onto that manager anyway
    would record "all rules ruled, none triggered" for someone nothing was
    ruled against, so they get the same `None` a never-evaluated capture
    gets -- silence, which the season tally qualifies rather than counting
    as a clean week (issue #136).
    """
    rules = data.get("fine_rules_evaluated")
    if rules is None:
        return None
    ruled = data.get("fines_ruled_manager_keys")
    if ruled is not None and manager_key not in ruled:
        return None
    return rules


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
            # Absent stays `None` rather than collapsing into the `[]` that
            # means "this ruling names nobody" -- a hand-built ruling said
            # nothing either way, which is not the same answer (issue #176).
            players = fine.get("players")
            out.append(LedgerFine(
                manager_key=manager_key,
                rule_type=fine["rule_type"],
                message=fine["message"],
                # `code` is read through `.get`, like `_eval_red_card` reads
                # it: a hand-built entry that names a player and says nothing
                # about his reference records `None` rather than raising a
                # `KeyError` through `build_history_rows`, which has no guard
                # of its own and would lose the whole gameweek's capture.
                players=(
                    None if players is None
                    else [LedgerFinePlayer(name=p["name"], code=p.get("code")) for p in players]
                ),
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
        fine_rules_evaluated=_ruled_rules_for(data, manager_key),
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

    def _gameweek_points(row: LeagueHistoryRow) -> int:
        gross = row.gross_points or 0
        return gross - (row.transfer_cost or 0) if use_net_points else gross

    # Same measure and same single-winner tie-break the live path uses
    # (`evaluate_league_fines`): whichever manager `min` reaches first is the
    # one fined, so a tie for last does not fine everyone tied. With no
    # rules to run this is wasted but harmless -- `evaluate_rules([], ...)`
    # returns `[]`, so every row still lands on the recorded "no rule covers
    # this row" that a special case here would have written by hand.
    worst = min(ruled, key=_gameweek_points)
    worst_points = _gameweek_points(worst)

    for row in ruled:
        league_data = FinesLeagueData(
            user_gw_points=row.gross_points or 0,
            worst_performers=[WorstPerformer(
                is_user=row.manager_key == worst.manager_key,
                points=worst_points,
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
                # Always empty here and recorded as such: this tier can only
                # rule what cohort points alone decide, and no such rule names
                # a player.
                players=[
                    LedgerFinePlayer(name=p.name, code=p.code) for p in result.players
                ],
            )
            for result in results if result.triggered
        ]
        row.fine_rules_evaluated = rule_types


def _freeze_recorded_fines(
    store: LeagueHistoryStore, gameweek: int, rows: list[LeagueHistoryRow],
) -> None:
    """Keep a ruling already on disk instead of re-ruling it under today's config.

    A backfill re-fetches a past gameweek's whole cohort, and both tiers rule
    fines over all of it. Left alone that re-rules managers whose data never
    changed, under whatever `fines` config happens to be current at repair
    time -- so editing a `below-threshold` value in March silently rewrites
    every already-ruled gameweek a repair touches afterwards, because
    `append_rows` supersedes any same-tier row whose content differs
    (issue #136 review). Rulings are frozen at capture; this is what freezes
    them.

    The decision is per gameweek, not per row: if *anything* in the cohort
    genuinely needs ruling -- a manager with no recorded ruling (an unknown
    row now repaired), a tier upgrade that can rule more than the recorded
    ruling did, or points that changed under a manager -- the whole gameweek
    is re-ruled together, because a cohort-relative rule like `last-place`
    ruled half-and-half would record two managers as last in one gameweek.
    Otherwise every recorded ruling is carried forward verbatim, the rows
    come out byte-identical, and `append_rows` writes nothing at all.

    Only backfill uses this. The gameweek being recapped now is ruled live
    each run on purpose: it is still being scored, and a config fix made
    this week should apply to this week.
    """
    try:
        previous = store.resolved_gameweek(gameweek)
    except LeagueHistoryError:
        # Unreadable: `append_rows` raises on the same file moments later and
        # the caller warns there. Nothing to carry forward either way.
        return
    if not previous:
        return

    carried: dict[int, LeagueHistoryRow] = {}
    for row in rows:
        if row.capture_status is not CaptureStatus.OK:
            continue
        prior = previous.get(row.manager_key)
        if (
            prior is None
            or prior.capture_status is not CaptureStatus.OK
            or prior.fine_rules_evaluated is None
            or not set(prior.fine_rules_evaluated) >= set(row.fine_rules_evaluated or ())
            or (prior.gross_points, prior.transfer_cost)
            != (row.gross_points, row.transfer_cost)
        ):
            return
        carried[row.manager_key] = prior

    for row in rows:
        prior = carried.get(row.manager_key)
        if prior is not None:
            row.fines = list(prior.fines)
            row.fine_rules_evaluated = list(prior.fine_rules_evaluated or ())


def _reconstructed_fine_players(
    fine: LedgerFine, row: LeagueHistoryRow, recorded_names: list[str],
) -> list[LedgerFinePlayer] | None:
    """Who a ruling recorded before schema version 5 must have named.

    Those rows carry the names as prose and nothing else, so a repair has to
    re-derive the references -- from the row's own squad, through the same
    predicate that ruled them (`red_card_offenders`). The prose is read only
    to count what it names, never to decide who: parsing names back out of a
    message is exactly the coupling this replaces.

    Adopted only where the squad corroborates it, on two counts. It has to
    name as many players as the message does: a replay drops a pick today's
    bootstrap cannot resolve, so a squad that has lost one would otherwise
    quietly un-name a player the gameweek fined, and a ruling is frozen at
    capture (`_freeze_recorded_fines`), so narrowing one is not this pass's to
    do. And no name the message already uses may belong to a squad member the
    predicate does not select -- that is the difference between a rename and a
    changed ruling. A rename leaves the old name nowhere in the squad, because
    `_carry_recorded_identity` has just put the recorded one back in its place;
    a player still sitting in the squad under the name the message uses, no
    longer counted as sent off, means the facts moved rather than the name, and
    substituting whoever the predicate now selects would stamp a stranger's
    name and reference onto the ruling. Either way the row is left exactly as
    recorded.

    A player the replay lost *and* replaced with another offender still reads
    as a rename here, and there is no signal on a reference-less row that tells
    the two apart. That is the irreducible limit of repairing prose, and the
    reason a ruling now records who it named.
    """
    if fine.rule_type != RED_CARD_RULE_TYPE or not recorded_names:
        return None
    offenders = red_card_offenders([
        FinesTeamPlayer(
            name=p.name,
            red_cards=p.red_cards,
            contributed=p.contributed,
            auto_sub_out=p.auto_sub_out,
            code=p.code,
        )
        for p in row.squad
    ])
    if len(offenders) != len(recorded_names):
        return None
    offender_names = {p["name"] for p in offenders}
    squad_names = {p.name for p in row.squad}
    if any(
        name in squad_names and name not in offender_names for name in recorded_names
    ):
        return None
    return [LedgerFinePlayer(name=p["name"], code=p.get("code")) for p in offenders]


def _repair_fine_identity(rows: list[LeagueHistoryRow]) -> None:
    """Restate the players a recorded fine names from the row's own squad.

    A fine's message embeds player names as free text resolved against the
    bootstrap that ruled it. A replay rules against *today's* bootstrap, so a
    since-renamed player is stored under his current name in `fines` and,
    thanks to `_carry_recorded_identity`, under the name the gameweek actually
    recorded in `squad` -- the same row disagreeing with itself (issue #176).
    Nothing reads the message back today, but the ledger is the only surviving
    copy of a gameweek once the API collapses it in July, so a wrong record is
    the whole defect.

    Runs *after* `_freeze_recorded_fines`, which is what makes it a repair
    rather than a prevention. The freeze carries a recorded ruling forward
    verbatim, so a message written wrong once becomes the resolved winner and
    is frozen wrong on every replay afterwards; a correction applied before it
    would simply be overwritten by the mistake.

    Only who a ruling names ever moves. Which rule triggered, which manager it
    was ruled against and what it costs them are untouched -- the ruling stays
    frozen, and this restates the identity inside it exactly as the squad carry
    restates a pick's.

    Rebuilds rather than mutates: `_freeze_recorded_fines` carries the *store's*
    own `LedgerFine` objects onto a row by reference, and `resolved_gameweek`
    memoizes what it hands out, so editing one in place would rewrite the
    ledger's cached view of the gameweek it was read from.
    """
    for row in rows:
        if not row.fines:
            continue
        squad_names = {p.code: p.name for p in row.squad if p.code is not None}
        repaired: list[LedgerFine] = []
        for fine in row.fines:
            players = fine.players
            # What the message itself spells out. Known for certain on a row
            # that stored its references -- the message was written from them
            # -- and read back off the prose only for one that did not.
            in_message = (
                [p.name for p in players] if players is not None
                else recap_fine_player_names(fine.message)
            )
            if players is None:
                players = _reconstructed_fine_players(fine, row, in_message)
            if not players:
                # Either the ruling names nobody -- which `last-place` and
                # `below-threshold` genuinely do not -- or it predates the
                # references and could not be reconstructed safely.
                repaired.append(fine)
                continue
            named = [
                LedgerFinePlayer(
                    name=squad_names.get(p.code, p.name) if p.code is not None else p.name,
                    code=p.code,
                )
                for p in players
            ]
            names = [p.name for p in named]
            message = restate_recap_fine_players(fine.message, in_message, names)
            if names != in_message and message == fine.message:
                # The message did not spell the list out the way `in_message`
                # says it did, so it kept the old names while the references
                # took the new ones -- one ruling naming two different sets of
                # players. Nothing moves unless both do.
                repaired.append(fine)
                continue
            repaired.append(fine.model_copy(update={"players": named, "message": message}))
        row.fines = repaired


def _first_recorded(rows: list[LeagueHistoryRow]) -> dict[int, LeagueHistoryRow]:
    """Per manager, the earliest line that recorded a squad.

    Deliberately not `resolved_gameweek`'s winner. The ledger is append-only,
    so a row an earlier replay degraded sits on disk *above* the original
    capture and wins resolution -- carrying that forward would just re-copy
    the degraded value. The earliest line is the one written closest to the
    gameweek, and reading it is what turns "nothing is destroyed" into an
    actual repair rather than a preserved mistake (issue #169).
    """
    earliest: dict[int, LeagueHistoryRow] = {}
    for row in rows:
        if row.capture_status is CaptureStatus.OK and row.squad:
            earliest.setdefault(row.manager_key, row)
    return earliest


def _pair_squads(
    replayed: list[LedgerPlayer], recorded: list[LedgerPlayer],
) -> list[tuple[LedgerPlayer, LedgerPlayer]]:
    """Pair each replayed squad entry with the recorded entry it supersedes.

    Prefers slot alignment: the picks endpoint returns a manager's squad in a
    fixed order for a given gameweek, so two squads of equal length whose
    codes corroborate each other are the same players in the same slots. That
    reaches an entry the replay could not identify at all, which a code join
    by definition cannot. At least one slot has to carry a code on both sides
    for that corroboration to mean anything -- `all()` over no comparisons at
    all is vacuously true, and two squads full of unidentified players would
    otherwise align on nothing but hope.

    Otherwise pairs on `code`, which still holds when the squads differ in
    length: a player who has since left the game entirely drops out of a
    replay, because the collectors skip a pick today's bootstrap cannot
    resolve. A single entry left unpaired on each side is then paired too --
    with one candidate each way there is nothing to confuse it with, and it
    is the one case where a code the replay lost is still recoverable here.
    Only when the replay lost it, though: an entry that *has* a code the
    recorded squad does not is a different player, not an unidentified one,
    and pairing it with whatever is left would stamp a stranger's name on it.
    """
    slots = list(zip(replayed, recorded, strict=False)) if len(replayed) == len(recorded) else []
    corroborating = [
        (new, old) for new, old in slots
        if new.code is not None and old.code is not None
    ]
    if corroborating and all(new.code == old.code for new, old in corroborating):
        return slots

    recorded_by_code = {p.code: i for i, p in enumerate(recorded) if p.code is not None}
    pairs: list[tuple[LedgerPlayer, LedgerPlayer]] = []
    claimed: set[int] = set()
    unpaired: list[LedgerPlayer] = []
    for new in replayed:
        index = recorded_by_code.get(new.code) if new.code is not None else None
        if index is None:
            unpaired.append(new)
            continue
        pairs.append((new, recorded[index]))
        claimed.add(index)

    spare = [p for i, p in enumerate(recorded) if i not in claimed]
    if len(unpaired) == 1 and len(spare) == 1 and unpaired[0].code is None:
        pairs.append((unpaired[0], spare[0]))
    return pairs


# The standings figures a replay structurally cannot re-fetch, and so must
# never erase. Draft has no per-manager history endpoint to re-derive a
# point-in-time total from, and the standings it *can* reach describe a later
# gameweek, so its collector leaves all three unset on a replay
# (`_assign_point_in_time_positions`).
_CARRIED_STANDINGS_FIELDS = ("league_position", "previous_league_position", "total_points")

# Everything the carry, the draft reconstruction and the repair sweep between
# them can change on a row -- `_assign_cohort_ranks` restates `gw_rank` off the
# same pass that settles a position.
_STANDINGS_ROW_FIELDS = (*_CARRIED_STANDINGS_FIELDS, "gw_rank")


def _earliest_recorded_standings(
    rows: list[LeagueHistoryRow],
) -> dict[int, dict[str, int]]:
    """Per manager, the first value each standings field was ever recorded with.

    Earliest rather than `resolved_gameweek`'s winner, for the reason
    `_first_recorded` gives: the ledger is append-only, so a row an earlier
    replay already degraded sits *above* the original capture and wins
    resolution. Reading the winner would preserve the mistake rather than
    repair it.

    Per field rather than per row, because no single row need hold all three:
    a gameweek captured live records its position and total while GW-1 is
    still unrecorded, and a later run fills `previous_league_position` from
    the ledger (R13).

    Every row is read, not just the `OK` ones: an unknown row captured live
    carries the standings position and total for the same point in time
    (`_unknown_row`), and those are exactly as recorded as an OK row's.
    """
    earliest: dict[int, dict[str, int]] = {}
    for row in rows:
        known = earliest.setdefault(row.manager_key, {})
        for name in _CARRIED_STANDINGS_FIELDS:
            value = getattr(row, name)
            if value is not None:
                known.setdefault(name, value)
    return earliest


def _apply_recorded_standings(
    rows: list[LeagueHistoryRow], recorded: dict[int, dict[str, int]],
) -> int:
    """Fill each row's null standings fields from what was recorded before.

    Only nulls are filled, so a replay that derived its own position keeps it
    and a genuine correction still lands. Nothing is ever lowered to null,
    which is what lets a replay of an already-recorded gameweek reproduce it
    exactly and so write no line at all.

    Returns how many values were restored, so the caller can say so.
    """
    carried = 0
    for row in rows:
        known = recorded.get(row.manager_key)
        if not known:
            continue
        for name, value in known.items():
            if getattr(row, name) is None:
                setattr(row, name, value)
                carried += 1
    return carried


def _carry_recorded_standings(
    store: LeagueHistoryStore, gameweek: int, rows: list[LeagueHistoryRow],
    *, warnings: list[dict[str, str]],
) -> int:
    """Keep the league position and cumulative total the gameweek recorded.

    A draft replay cannot fetch either (see `_CARRIED_STANDINGS_FIELDS`), so
    written straight out its rows supersede a live capture that *did* record
    them and the ledger silently loses that gameweek's positions -- the one
    thing the streak and season-count projections are built on (issue #223).
    The damage is append-only and, without this, permanent.

    Same shape and same gate as `_carry_recorded_identity`: only a finished
    gameweek is carried into, because only a finished gameweek is done
    moving. While one is still live a fresh position genuinely supersedes an
    older one, and none of them are null anyway.
    """
    try:
        previous = store.load_gameweek(gameweek)
    except LeagueHistoryError:
        # Degrades silently for the reason `_freeze_recorded_fines` gives.
        return 0
    if not previous:
        return 0

    carried = _apply_recorded_standings(rows, _earliest_recorded_standings(previous))
    if carried:
        _warn(
            warnings, HISTORY_WARNING_STANDINGS_CARRIED,
            f"League history: GW{gameweek} kept {carried} league position or cumulative "
            f"total already recorded for it. This run could not re-derive them -- draft "
            f"exposes no per-manager history endpoint, and the standings describe a later "
            f"gameweek -- so the recorded figures stand rather than being overwritten "
            f"with nothing.",
        )
    return carried


def _fill_draft_standings(
    store: LeagueHistoryStore, rows: list[LeagueHistoryRow],
    *, gameweek: int, start_gameweek: int,
) -> None:
    """Reconstruct a replayed draft gameweek's totals, then rank from them.

    `_assign_cohort_ranks` runs inside `build_history_rows`, before any total
    is summed, so without this second pass the totals reconstructed here would
    sit on rows whose `league_position` stayed null. At the league's own start
    gameweek the sum is the whole derivation -- the cumulative total *is* that
    gameweek's score -- so its positions come out of `gross_points` alone,
    with no earlier gameweek and no endpoint needed.

    A whole table is derived or none of it is, on two counts. Every row needs
    a total, because ranking just the managers who summed cleanly would
    renumber everyone below whoever was left out. And no row may already carry
    a position, because `_assign_cohort_ranks` fills only the nulls: a cohort
    where some positions were carried and the rest re-derived is ranked
    against two different tables at once, and lands two managers on the same
    place with nobody on the next. That is not a tie-shaped edge case -- draft
    breaks a head-to-head tie on points-for, which a cumulative total cannot
    reproduce, so a carried position and a re-derived one disagree in the
    ordinary case. A null says "unknown", which every streak condition holds
    across; a wrong position is recorded as fact and, with both fields then
    non-null, never revisited.
    """
    _fill_draft_cumulative_totals(
        store, rows, gameweek=gameweek, start_gameweek=start_gameweek,
    )
    if rows and all(
        row.total_points is not None and row.league_position is None for row in rows
    ):
        _assign_cohort_ranks(rows)


def _overruled_codes(data: LeagueRecapData) -> frozenset[int]:
    """The codes whose club this capture derived from the gameweek's fixtures.

    Absent means the caller never resolved gameweek clubs, which is not the
    same as "resolved and nothing moved": either way there is nothing to
    exempt from the carry, so both collapse to the empty set here.
    """
    return frozenset(data.get("clubs_overruled_codes") or ())


def _carry_recorded_identity(
    store: LeagueHistoryStore, gameweek: int, rows: list[LeagueHistoryRow],
    *, overruled_codes: frozenset[int] = frozenset(),
    warnings: list[dict[str, str]],
) -> int:
    """Keep the identity the gameweek recorded for each player.

    A replay resolves every pick against *today's* bootstrap, so a player
    transferred or renamed since comes back wearing his current club and
    current name on a row describing a gameweek where neither was true
    (issue #169). Points, cards and the pick flags are all re-derived from the
    gameweek's own data and stay as the replay found them; only the fields
    that describe who a player *was* are carried.

    `overruled_codes` names the players whose club this replay derived from
    the gameweek's own fixtures rather than restamping from the bootstrap
    (`services/player_clubs.py`). Those clubs are *not* carried: the recorded
    one may itself be today's club, written by a first capture or a coarse
    upgrade that had nothing to carry, and carrying it forward would preserve
    that mistake permanently. The derived club supersedes it instead, so the
    repair reaches rows already on disk (issue #177). Name and position are
    not derivable from fixtures and stay carry-forward-only either way.

    Identity is never lowered either: where the recorded row resolved a code
    and this replay did not, the code is restored, so the ledger's
    cross-season join key survives a bootstrap that has drifted. The replay's
    `unmatched` marker and its zero stay put -- they describe what this replay
    could score, and a restored code does not make those points real.

    Returns the number of players whose recorded identity differed from the
    replay's, so the caller can say so.
    """
    try:
        previous = store.load_gameweek(gameweek)
    except LeagueHistoryError:
        # Degrades silently for the reason `_freeze_recorded_fines` gives.
        return 0
    if not previous:
        return 0

    recorded_rows = _first_recorded(previous)
    carried = 0
    rederived = 0
    for row in rows:
        recorded = recorded_rows.get(row.manager_key)
        if recorded is None or row.capture_status is not CaptureStatus.OK:
            continue

        pairs = _pair_squads(row.squad, recorded.squad)
        # `_captaincy` built the captain and vice by matching the *replayed*
        # name, so index on that -- before the loop below rewrites it.
        recorded_by_replayed_name = {new.name: old for new, old in pairs}

        for new, old in pairs:
            before = (new.name, new.team, new.position, new.code)
            new.name, new.position = old.name, old.position
            if new.code in overruled_codes:
                if new.team != old.team:
                    rederived += 1
            else:
                new.team = old.team
            if new.code is None and old.code is not None:
                new.code = old.code
            if (new.name, new.team, new.position, new.code) != before:
                carried += 1

        # A captaincy entry is a value copy rather than a reference into the
        # squad, so restoring a squad slot's code leaves the same player's
        # captaincy code untouched unless it is carried here too.
        for pick in (row.captain, row.vice_captain):
            old_pick = recorded_by_replayed_name.get(pick.name) if pick else None
            if pick is None or old_pick is None:
                continue
            pick.name = old_pick.name
            if pick.code is None and old_pick.code is not None:
                pick.code = old_pick.code

        rederived += _carry_move_identity(row, recorded, overruled_codes)

    if carried:
        _warn(
            warnings, HISTORY_WARNING_IDENTITY_CARRIED,
            f"League history: GW{gameweek} kept the name, club, position or player "
            f"reference already recorded for {carried} player(s) rather than the ones "
            f"they carry today. Picks are resolved against the current bootstrap, "
            f"which has moved on since the gameweek was played.",
        )
    if rederived:
        _warn(
            warnings, HISTORY_WARNING_CLUB_REDERIVED,
            f"League history: GW{gameweek} replaced the club recorded for {rederived} "
            f"player(s) with the one the gameweek's own fixtures place them at. The "
            f"recorded club was stamped from a bootstrap that had already moved on.",
        )
    return carried


# The three fields naming one side of a transfer or waiver move.
_MOVE_SIDES = (
    ("player_in", "player_in_team", "player_in_code"),
    ("player_out", "player_out_team", "player_out_code"),
)


def _carry_move_identity(
    row: LeagueHistoryRow, recorded: LeagueHistoryRow, overruled_codes: frozenset[int],
) -> int:
    """Apply the same rule to the row's transfers and waiver moves.

    Slot-aligned like the squad, and for the same reason: a manager's moves in
    one gameweek come back in a fixed order, so equal-length lists pair
    position for position -- which is what reaches the player moved *out*, who
    by definition need never appear in the squad and so cannot be recovered
    from it.

    Where the lists disagree in length there is no order to trust, and each
    side falls back to what the recorded row knows about that code. That
    carries a name and club but cannot restore a code, since a move whose code
    the replay lost is exactly the one this path cannot identify.

    `overruled_codes` holds the same club exemption the squad carry makes, and
    returns how many recorded clubs the derived one replaced.
    """
    known = {
        code: (name, team)
        for source in (recorded.transfers, recorded.transactions)
        for move in source
        for name, team, code in (
            (move.player_in, move.player_in_team, move.player_in_code),
            (move.player_out, move.player_out_team, move.player_out_code),
        )
        if code is not None
    } | {p.code: (p.name, p.team) for p in recorded.squad if p.code is not None}

    rederived = 0
    for replayed_moves, recorded_moves in (
        (row.transfers, recorded.transfers),
        (row.transactions, recorded.transactions),
    ):
        aligned = len(replayed_moves) == len(recorded_moves)
        for index, move in enumerate(replayed_moves):
            for name_attr, team_attr, code_attr in _MOVE_SIDES:
                if aligned:
                    old = recorded_moves[index]
                    name, team = getattr(old, name_attr), getattr(old, team_attr)
                    old_code = getattr(old, code_attr)
                else:
                    code = getattr(move, code_attr)
                    if code is None or code not in known:
                        continue
                    name, team = known[code]
                    old_code = None
                setattr(move, name_attr, name)
                if getattr(move, code_attr) in overruled_codes:
                    if getattr(move, team_attr) != team:
                        rederived += 1
                else:
                    setattr(move, team_attr, team)
                if getattr(move, code_attr) is None and old_code is not None:
                    setattr(move, code_attr, old_code)
    return rederived


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
        _freeze_recorded_fines(store, gameweek, gameweek_rows)
        # No `_repair_fine_identity` here, unlike the other two paths the
        # freeze runs on. A coarse row carries no squad to restate names from,
        # and the one ruling it can inherit that names anybody is a red-card
        # fine frozen forward from a *detailed* prior -- whose row outranks
        # this one, so `append_rows` drops this line before it is written at
        # all. There is nothing on disk for a repair here to reach.
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
    fpl_format: LeagueFormat,
    replay_gameweek: ReplayGameweek,
    gameweeks: list[int],
    start_gameweek: int,
    warnings: list[dict[str, str]],
) -> set[int]:
    """Replay whole gameweeks through the collectors Phase A corrected.

    Serial by gameweek, committing each as it completes: the collector already
    bounds its own per-manager concurrency, and committing per gameweek is what
    lets an interrupted backfill keep everything it already fetched. Ascending
    for a second reason on draft, where each replayed gameweek's cumulative
    total is summed from the ones before it.

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
        _carry_recorded_identity(
            store, gameweek, rows,
            overruled_codes=_overruled_codes(replayed), warnings=warnings,
        )
        # A replayed gameweek is finished by definition, so its standings are
        # done moving and a null here is a loss rather than an update.
        _carry_recorded_standings(store, gameweek, rows, warnings=warnings)
        if fpl_format == "draft" and rows:
            _fill_draft_standings(
                store, rows, gameweek=gameweek, start_gameweek=start_gameweek,
            )
        _freeze_recorded_fines(store, gameweek, rows)
        # After the freeze, never before it: a ruling carried forward verbatim
        # carries its player names too, and those are what this puts right.
        _repair_fine_identity(rows)
        try:
            written = store.append_rows(gameweek, rows)
        except LeagueHistoryError as exc:
            _warn(warnings, HISTORY_WARNING_BACKFILL_WRITE_FAILED, str(exc))
            continue
        if written:
            repaired.add(gameweek)
    return repaired


def _repair_recorded_standings(
    store: LeagueHistoryStore,
    *,
    fpl_format: LeagueFormat,
    gameweeks: list[int],
    start_gameweek: int,
    warnings: list[dict[str, str]],
) -> set[int]:
    """Restore positions an earlier replay nulled, re-fetching nothing.

    Every input is already on disk: the value a superseding row overwrote is
    still on a line below it, and a draft gameweek's cumulative total is a sum
    over the ledger's own earlier gameweeks. So this runs on every recap
    rather than behind `--backfill-detail` -- there is no per-manager call to
    ration, and it is the only path that heals a ledger already damaged by a
    replay that predates the carry above (issue #223).

    Ascending, so a gameweek repaired here is available to sum the next one
    from -- which is what lets a whole partition rebuilt by `--backfill-detail`
    recover its positions from GW1 upwards.

    Idempotent, and silent on a healthy ledger: once a position is restored
    the candidate reproduces the stored row and `append_rows` writes nothing.
    A gameweek that genuinely cannot be filled -- a draft run with a hole in
    it, or a manager nobody ever reached -- is re-attempted every run and each
    time writes nothing. That costs one file parse, against the per-manager
    fetch `_detailed_backfill` already re-attempts for the same gameweeks.

    Returns the gameweeks that gained a superseding row, like both backfill
    tiers, so the caller can invalidate their counters.
    """
    repaired: set[int] = set()
    for gameweek in gameweeks:
        try:
            winners = store.resolved_gameweek(gameweek)
        except LeagueHistoryError:
            # Reported by `_warn_unreadable`, and never overwritten: a repair
            # that rewrote it would destroy whatever it still holds (R4).
            continue
        # Asked of exactly the fields the repair below restores, so the two
        # cannot drift apart, and of every row rather than just the `OK` ones:
        # an unknown row captured live carries a real position and total
        # (`_unknown_row`), so a replay that superseded one with nulls is as
        # repairable as any other row.
        #
        # `previous_league_position` is the one field null does not mean damage
        # for: at the league's first scored gameweek there was no table to move
        # from (issue #147). Asking about it there would fire this sweep on
        # every run of every league forever, so it is asked through the same
        # helper the collectors gate that field with -- the two cannot disagree
        # about which gameweek that is.
        #
        # The read itself is memoized, so a healthy gameweek costs nothing
        # beyond the parse the coverage pass above already paid for.
        repairable = [
            name for name in _CARRIED_STANDINGS_FIELDS
            if name != "previous_league_position"
            or _has_previous_gameweek(gameweek, start_gameweek)
        ]
        if not any(
            getattr(row, name) is None
            for row in winners.values() for name in repairable
        ):
            continue
        try:
            stored = store.load_gameweek(gameweek)
        except LeagueHistoryError:
            continue

        # Shallow: the only scalars ever assigned on a candidate are those in
        # `_STANDINGS_ROW_FIELDS`, so the nested squad and transfers are shared
        # with the stored row rather than rebuilt. This sweep runs on every
        # recap for every target gameweek, and one league with a single
        # unrepairable gameweek would otherwise deep-copy every manager's whole
        # squad on every future run. `_repair_fine_identity` is safe alongside
        # that because it *rebinds* `fines` to a rebuilt list rather than
        # editing the models in it -- anything here that starts mutating a
        # nested model in place needs the deep copy back.
        candidates = [(winners[key], winners[key].model_copy()) for key in sorted(winners)]
        rows = [candidate for _, candidate in candidates]
        _apply_recorded_standings(rows, _earliest_recorded_standings(stored))
        if fpl_format == "draft":
            _fill_draft_standings(
                store, rows, gameweek=gameweek, start_gameweek=start_gameweek,
            )
        # These rows are being rewritten anyway, and this is the one pass that
        # reaches a finished gameweek without `--backfill-detail`. Without it
        # the sweep re-anchors a stale fine name as the new winner every time
        # it fires, and nothing short of a detailed replay ever puts it right
        # (issue #176).
        _repair_fine_identity(rows)

        captured_at = datetime.now(tz=timezone.utc)
        changed: list[LeagueHistoryRow] = []
        for winner, candidate in candidates:
            if candidate.content() == winner.content():
                continue
            # The copy inherits the damaged row's timestamp, and resolution
            # breaks a tier tie on the later capture -- so without this the
            # appended line loses to the very row it repairs.
            candidate.captured_at = captured_at
            changed.append(candidate)
        if not changed:
            continue

        try:
            written = store.append_rows(gameweek, changed)
        except LeagueHistoryError as exc:
            _warn(warnings, HISTORY_WARNING_BACKFILL_WRITE_FAILED, str(exc))
            continue
        if written:
            repaired.add(gameweek)
            # Counted over the standings fields alone, not over every row
            # written: a row this pass rewrote only to restate a fine's player
            # names has had no position restored, and saying otherwise would
            # report a repair that did not happen. The restatement itself stays
            # silent here, as it does everywhere else.
            written_keys = {row.manager_key for row in written}
            restored = sum(
                1 for winner, candidate in candidates
                if candidate.manager_key in written_keys
                and any(
                    getattr(candidate, name) != getattr(winner, name)
                    for name in _STANDINGS_ROW_FIELDS
                )
            )
            if restored:
                _warn(
                    warnings, HISTORY_WARNING_STANDINGS_REPAIRED,
                    f"League history: GW{gameweek} had its league position or cumulative "
                    f"total restored for {restored} manager(s) from what the ledger "
                    f"itself already recorded. An earlier replay of that gameweek wrote "
                    f"them away; nothing was re-fetched to put them back.",
                )
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

    start_gameweek = data.get("league_start_event") or 1
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

    if replay_gameweek is not None:
        # A gameweek already holding unknown rows is repaired without the flag:
        # the cost is bounded by how many gameweeks actually failed, and without
        # it an unknown row is permanent. Filling a gap or upgrading a coarse
        # gameweek is the unbounded case the flag guards -- one call per manager
        # per gameweek, which a mid-season first run would otherwise pay across
        # the whole season before printing anything (KTD6).
        detail_targets = set(gaps.incomplete)
        if backfill_detail:
            detail_targets |= set(gaps.missing) | set(gaps.coarse)
        if detail_targets:
            repaired |= await _detailed_backfill(
                store=store, season=season, league_id=league_id, fpl_format=fpl_format,
                replay_gameweek=replay_gameweek, gameweeks=sorted(detail_targets),
                start_gameweek=start_gameweek, warnings=warnings,
            )

    # Last, and unconditionally -- it re-fetches nothing, so there is no cost
    # to ration, and running it after the tiers above means it sweeps whatever
    # they just wrote. Both orderings need that: a gameweek this run rebuilt
    # from scratch is what lets the next one sum a cumulative total, and a
    # gameweek only this pass can repair is what lets a replayed later one do
    # the same. Ascending within the sweep settles both in one go.
    return repaired | _repair_recorded_standings(
        store, fpl_format=fpl_format, gameweeks=targets,
        start_gameweek=start_gameweek, warnings=warnings,
    )


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

    An unreadable gameweek is reported under `league_history_store_unreadable`
    rather than as a coverage gap, and never as an uncaptured one: its file
    holds rows this run simply could not parse, so the remedy is the `mv` the
    store's own message names, not the `--backfill-detail` a gap would invite
    -- and backfill refuses to touch it anyway (`_gaps`, issue #224).
    """
    by_gameweek = {c.gameweek: c for c in coverage}
    coarse = [gw for gw in targets if (c := by_gameweek.get(gw)) and c.lowest_tier is FidelityTier.COARSE]
    unknown = [gw for gw in targets if (c := by_gameweek.get(gw)) and c.readable and c.unknown_count]
    uncaptured = [
        gw for gw in targets
        if gw not in by_gameweek
        or (by_gameweek[gw].readable and by_gameweek[gw].manager_count == 0)
    ]

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

    for line in lines:
        _warn(warnings, HISTORY_WARNING_COVERAGE, line)

    _warn_unreadable(coverage, warnings=warnings)


def _warn_unreadable(
    coverage: list[GameweekCoverage],
    *,
    warnings: list[dict[str, str]],
    already_reported: int | None = None,
) -> None:
    """One `league_history_store_unreadable` warning per unreadable gameweek.

    One warning each, not one line for the set: every entry carries its own
    file path and `mv` remedy, straight from the store. Every unreadable
    gameweek, not just the targeted ones -- a file that will not parse is a
    store problem whatever window the coverage report spans, and a warning is
    the only place it is surfaced, `coverage()` handing the reason back here
    rather than logging it (issue #224).

    `already_reported` skips the one gameweek a caller has itself warned
    about. The write-failure path in `capture_recap_history` shows the store's
    raised message for the gameweek it was recapping and then calls this for
    the rest of the partition, so a second damaged file still names itself
    without the recapped one being warned about twice.

    `coverage()` always sets `error` alongside `readable=False`, so the
    fallback is only reached by an entry built by hand without one -- the
    dataclass allows it, and a warning naming no remedy still beats none.
    """
    for entry in coverage:
        if entry.readable or entry.gameweek == already_reported:
            continue
        detail = entry.error or (
            "Move the file aside to recapture it; the rest of the season is unaffected."
        )
        _warn(
            warnings, HISTORY_WARNING_STORE_UNREADABLE,
            f"League history: GW{entry.gameweek} could not be read and is skipped. {detail}",
        )


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
    # `_report_coverage` below shows the user the store's own unreadable
    # message in full, as a `league_history_store_unreadable` warning, for
    # every gameweek `coverage()` could not read -- and on the write-failure
    # path, where `_report_coverage` is skipped, the `_warn` beside that
    # failure carries the recapped gameweek's message and `_warn_unreadable`
    # carries the rest of the partition's. Claimed here rather than
    # left to whichever reader happens to touch the file first, so no
    # reordering of the readers below can put a near-identical log line
    # beside that warning again (issue #224).
    store.unreadable_reported_by_caller = True
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
    # A gameweek whose fixtures have all finished is done moving: its picks,
    # captain and transfers are fixed, and this run's rows are resolved
    # against *today's* bootstrap, which may have moved on since (issue #178).
    # `finished_gameweeks` -- not `is_live_gw` -- is the gate, because a
    # gameweek stays "current" (and so keeps landing on this path) for the
    # whole window between its last fixture and the next deadline, and that
    # window is exactly when a transfer can restamp a correct recorded row.
    # `_carry_recorded_identity` already no-ops when nothing is recorded yet,
    # so a genuine first capture is unaffected.
    if data["gameweek"] in finished_gameweeks:
        _carry_recorded_identity(
            store, data["gameweek"], rows,
            overruled_codes=_overruled_codes(data), warnings=warnings,
        )
        # Same gate, same reason: a finished gameweek's position is done
        # moving, so a run that cannot re-derive it must not write it away
        # (issue #223). A gameweek still live is skipped because a fresh
        # position there genuinely supersedes the last one.
        _carry_recorded_standings(store, data["gameweek"], rows, warnings=warnings)
        # And the same gate again: this run ruled the fines against the same
        # drifted bootstrap, so they name players by today's names on a row
        # whose squad the carry above has just put back (issue #176). While
        # the gameweek is still live there is no drift to correct -- the
        # squad and the ruling came from the same fetch minutes ago.
        _repair_fine_identity(rows)
    if fpl_format == "draft" and rows:
        _fill_draft_standings(
            store, rows,
            gameweek=data["gameweek"],
            start_gameweek=data.get("league_start_event") or 1,
        )
    _quality_warnings(data, rows, warnings)

    try:
        written = store.append_rows(data["gameweek"], rows)
    except LeagueHistoryError as exc:
        _warn(warnings, HISTORY_WARNING_STORE_UNREADABLE, str(exc))
        # Coverage is still read, even though this write never landed: the
        # block is the payload's answer to "which gameweeks can I trust", and
        # one damaged file is exactly when a consumer needs the answer.
        # `coverage()` scopes failure to the gameweek -- the bad file comes
        # back `readable: False`, its neighbours keep their real status -- so
        # returning the field's `[]` default here both hid the intact
        # gameweeks and was indistinguishable from a partition with nothing
        # captured at all (issue #264). Nothing was written, so this reads the
        # same disk state the write attempt found.
        coverage = store.coverage()
        # Reading the whole partition can turn up a *second* damaged file,
        # which the `_warn` above says nothing about -- it carries only the
        # gameweek `append_rows` raised on. Without this the payload would
        # surface that gameweek as `readable: False` with its reason nowhere:
        # `_report_coverage` is skipped on this path, `_serialize_coverage`
        # does not emit `error`, and `unreadable_reported_by_caller` has
        # dropped the store's own log line to debug. So warn for the rest of
        # the partition here, passing the recapped gameweek as
        # `already_reported` -- routing the whole set through would put a
        # near-identical second line beside the `_warn` above (issue #224).
        _warn_unreadable(coverage, warnings=warnings, already_reported=data["gameweek"])
        return CaptureResult(
            rows=rows, store_readable=False, warnings=warnings,
            coverage=coverage,
            first_capture_store_path=first_capture_store_path,
        )

    # `append_rows` skips a row whose content is unchanged from the resolved
    # current one (captured_at excluded from that comparison), so `rows` can
    # still carry this run's fresh timestamp on a manager nothing was written
    # for. Re-stamp every row from the store's post-write resolution so a
    # consumer of `CaptureResult.rows` (the JSON payload included) sees the
    # gameweek's actual capture time, not a re-read mislabelled as fresh
    # (issue #237).
    #
    # Gated on `rows` being non-empty: `append_rows` above short-circuits
    # without ever parsing the file when there is nothing to append, so an
    # empty cohort paired with a corrupt gameweek file never reaches the
    # `LeagueHistoryError` guard on that call. Calling `resolved_gameweek`
    # unconditionally here would make this the first parse attempt for that
    # case, raising past this function's "never raises for a store problem"
    # guarantee (R4) for a re-stamp that an empty `rows` has nothing to use
    # anyway.
    if rows:
        resolved_after_write = store.resolved_gameweek(data["gameweek"])
        for row in rows:
            winner = resolved_after_write.get(row.manager_key)
            if winner is not None:
                row.captured_at = winner.captured_at

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

    # The sweep inside `_backfill` can fill *this* gameweek's own positions --
    # when the earlier gameweek it had to sum a cumulative total from was
    # itself only repaired on this run, after the write above had already gone
    # in with nulls. `rows` is what the JSON payload and the gw-prep scripts
    # read, so it has to show what the ledger now holds rather than what the
    # write went in with (the same divergence issue #237 closed for
    # `captured_at`).
    if rows and data["gameweek"] in repaired_gameweeks:
        try:
            repaired_rows = store.resolved_gameweek(data["gameweek"])
        except LeagueHistoryError:
            repaired_rows = {}  # R4: a store problem costs the refresh, not the recap.
        for row in rows:
            winner = repaired_rows.get(row.manager_key)
            if winner is None:
                continue
            for name in _STANDINGS_ROW_FIELDS:
                setattr(row, name, getattr(winner, name))
            row.captured_at = winner.captured_at

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
