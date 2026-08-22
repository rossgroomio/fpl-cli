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

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone

from fpl_cli.cli._context import error_console
from fpl_cli.cli._league_recap_data import raw_chip_name, recap_manager_key
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

logger = logging.getLogger(__name__)

# Machine-readable warning codes, paired with the human line printed to stderr.
# Same shape the `stats` command already emits, so `--format json` can carry
# them without a second vocabulary.
HISTORY_WARNING_STORE_UNREADABLE = "league_history_store_unreadable"
HISTORY_WARNING_LEAGUE_ID_MISSING = "league_history_league_id_missing"
HISTORY_WARNING_UNMATCHED_PLAYERS = "league_history_unmatched_players"
HISTORY_WARNING_TRANSFER_DETAIL_SHORT = "league_history_transfer_detail_short"
HISTORY_WARNING_STANDINGS_TRUNCATED = "league_history_standings_truncated"


@dataclass
class CaptureResult:
    """What one capture produced, whether or not the store accepted it."""

    rows: list[LeagueHistoryRow] = field(default_factory=list)
    written: list[LeagueHistoryRow] = field(default_factory=list)
    store_readable: bool = True
    warnings: list[dict[str, str]] = field(default_factory=list)
    coverage: list[GameweekCoverage] = field(default_factory=list)


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
        transfer_cost=manager["transfer_cost"],
        total_points=manager.get("total_points"),
        gw_rank=manager["gw_rank"],
        league_position=manager.get("overall_rank"),
        previous_league_position=manager.get("previous_rank"),
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
        squad_value=manager.get("squad_value"),
        bank=manager.get("bank"),
        global_rank=manager.get("global_rank"),
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
# Capture
# ---------------------------------------------------------------------------


async def capture_recap_history(
    data: LeagueRecapData,
    *,
    season: str | None = None,
    is_live_gw: bool = True,
) -> CaptureResult:
    """Record this gameweek, and report anything the user should act on.

    Never raises for a store problem: the message goes to stderr, the rows come
    back regardless, and the caller carries on rendering (R4).
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
        return CaptureResult(rows=rows, store_readable=False, warnings=warnings)

    if is_first_season_capture:
        # The one moment a container-local data directory is still cheap to
        # notice: after the season is under way, everything written into an
        # ephemeral one is already gone.
        error_console.print(
            f"[dim]Recording league history to {store.partition_dir()} "
            f"(set FPL_CLI_DATA_DIR to keep it somewhere persistent).[/dim]",
        )

    return CaptureResult(
        rows=rows,
        written=written,
        store_readable=True,
        warnings=warnings,
        coverage=store.coverage(),
    )
