"""League recap data collection: per-manager stats, awards, standings movement."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

from fpl_cli.cli._fines import FineResult, FinesLeagueData, FinesTeamPlayer, evaluate_fines
from fpl_cli.cli._fines_config import parse_fines_config
from fpl_cli.cli._helpers import _live_player_stats

if TYPE_CHECKING:
    from fpl_cli.api.fpl import FPLClient
    from fpl_cli.models.player import Player
    from fpl_cli.models.team import Team
from fpl_cli.cli._league_recap_types import (
    LeagueRecapData,
    RecapAwardEntry,
    RecapAwards,
    RecapDraftTransaction,
    RecapFineResult,
    RecapManagerEntry,
    RecapManagerPlayer,
    RecapTransfer,
)

logger = logging.getLogger(__name__)

_CHIP_DISPLAY = {"wildcard": "WC", "freehit": "FH", "bboost": "BB", "3xc": "TC"}
_PICKS_CONCURRENCY = 10


# ---------------------------------------------------------------------------
# Classic data collection
# ---------------------------------------------------------------------------


async def collect_classic_recap_data(
    client: FPLClient,
    settings: dict[str, Any],
    gw: int,
    live_stats: dict[int, dict[str, Any]],
    player_map: dict[int, Player],
    teams: dict[int, Team],
) -> LeagueRecapData:
    """Fetch all managers' picks and compute league-wide recap data.

    Returns a LeagueRecapData dict ready for template rendering.
    """
    classic_league_id = settings.get("fpl", {}).get("classic_league_id")
    use_net_points = settings.get("use_net_points", False)

    standings_response = await client.get_classic_league_standings(classic_league_id)
    league_name = standings_response.get("league", {}).get("name", "Unknown League")
    standings = standings_response.get("standings", {}).get("results", [])

    managers = await _fetch_all_manager_data(
        client, standings, gw, live_stats, player_map, teams,
        use_net_points=use_net_points,
    )

    _compute_standings_movement(managers)

    awards = _compute_shared_awards(managers, format_name="classic")

    return LeagueRecapData(
        gameweek=gw,
        league_name=league_name,
        fpl_format="classic",
        managers=managers,
        awards=awards,
    )


# ---------------------------------------------------------------------------
# Per-manager fetch (bounded concurrency)
# ---------------------------------------------------------------------------


async def _fetch_all_manager_data(
    client: FPLClient,
    standings: list[dict[str, Any]],
    gw: int,
    live_stats: dict[int, dict[str, Any]],
    player_map: dict[int, Player],
    teams: dict[int, Team],
    *,
    use_net_points: bool = False,
) -> list[RecapManagerEntry]:
    """Fetch picks for every manager in the league, extract recap data."""
    sem = asyncio.Semaphore(_PICKS_CONCURRENCY)

    async def _fetch_one(entry: dict, rank: int) -> RecapManagerEntry | None:
        league_entry_id: int = entry.get("entry", 0)
        manager_name: str = entry.get("player_name", "Unknown")
        gross_pts: int = entry.get("event_total", 0)
        total_pts: int = entry.get("total", 0)

        async with sem:
            try:
                picks_response = await client.get_manager_picks(league_entry_id, gw)
            except Exception as e:  # noqa: BLE001 — graceful degradation
                logger.warning("Failed to fetch picks for %s (entry %s): %s", manager_name, league_entry_id, e)
                return None

        picks = picks_response.get("picks", [])
        entry_history = picks_response.get("entry_history", {})
        active_chip = picks_response.get("active_chip")
        automatic_subs = picks_response.get("automatic_subs", [])

        transfer_cost = entry_history.get("event_transfers_cost", 0)
        gw_points = (gross_pts - transfer_cost) if use_net_points else gross_pts

        auto_sub_in_ids = {sub["element_in"] for sub in automatic_subs}
        auto_sub_out_ids = {sub["element_out"] for sub in automatic_subs}

        # Build squad
        squad: list[RecapManagerPlayer] = []
        captain_name = ""
        captain_points = 0
        captain_played = False
        vice_captain_name = ""

        bench_points = 0
        for pick in picks:
            player = player_map.get(pick["element"])
            if not player:
                continue

            pts, minutes, red_cards = _live_player_stats(live_stats, player.id)
            is_bench = pick.get("position", 1) > 11
            contributed = (not is_bench) and pick.get("multiplier", 0) > 0
            if player.id in auto_sub_in_ids:
                contributed = True

            # Bench points: actual bench slot, not auto-subbed out
            if is_bench and player.id not in auto_sub_in_ids:
                bench_points += pts

            squad.append(RecapManagerPlayer(
                name=player.web_name,
                team=t.short_name if (t := teams.get(player.team_id)) else "???",
                position=player.position_name,
                points=pts,
                is_captain=pick.get("is_captain", False),
                is_vice_captain=pick.get("is_vice_captain", False),
                contributed=contributed,
                auto_sub_in=player.id in auto_sub_in_ids,
                auto_sub_out=player.id in auto_sub_out_ids,
                red_cards=red_cards,
            ))

            if pick.get("is_captain"):
                captain_name = player.web_name
                captain_points = pts
                captain_played = minutes > 0
            if pick.get("is_vice_captain"):
                vice_captain_name = player.web_name

        # Human-readable auto-sub descriptions
        auto_sub_descriptions: list[str] = []
        for sub in automatic_subs:
            pin = player_map.get(sub["element_in"])
            pout = player_map.get(sub["element_out"])
            if pin and pout:
                pin_pts, _, _ = _live_player_stats(live_stats, pin.id)
                auto_sub_descriptions.append(f"{pin.web_name} on for {pout.web_name} ({pin_pts} pts)")

        # Fetch transfers for this GW
        transfers: list[RecapTransfer] = []
        num_transfers = entry_history.get("event_transfers", 0)
        if num_transfers > 0:
            try:
                all_transfers = await client.get_manager_transfers(league_entry_id)
                gw_transfers = [tr for tr in all_transfers if tr.get("event") == gw]
                for tr in gw_transfers:
                    elem_in: int | None = tr.get("element_in")
                    elem_out: int | None = tr.get("element_out")
                    pin = player_map.get(elem_in) if elem_in else None
                    pout = player_map.get(elem_out) if elem_out else None
                    if pin and pout:
                        pin_pts, _, _ = _live_player_stats(live_stats, pin.id)
                        pout_pts, _, _ = _live_player_stats(live_stats, pout.id)
                        pin_team = teams.get(pin.team_id)
                        pout_team = teams.get(pout.team_id)
                        transfers.append(RecapTransfer(
                            player_in=pin.web_name,
                            player_in_team=pin_team.short_name if pin_team else "???",
                            player_in_points=pin_pts,
                            player_out=pout.web_name,
                            player_out_team=pout_team.short_name if pout_team else "???",
                            player_out_points=pout_pts,
                            net=pin_pts - pout_pts,
                            cost=transfer_cost,
                        ))
            except Exception as e:  # noqa: BLE001 — best-effort enrichment
                logger.debug("Could not fetch transfers for %s: %s", manager_name, e)

        result = RecapManagerEntry(
            manager_name=manager_name,
            entry_id=league_entry_id,
            gw_points=gw_points,
            total_points=total_pts,
            gw_rank=rank,
            overall_rank=rank,
            previous_rank=rank,  # placeholder, computed after all managers fetched
            captain=captain_name,
            captain_points=captain_points,
            captain_played=captain_played,
            vice_captain=vice_captain_name,
            active_chip=_CHIP_DISPLAY.get(active_chip, active_chip) if active_chip else None,
            squad=squad,
            bench_points=bench_points,
            transfer_cost=transfer_cost,
            auto_subs=auto_sub_descriptions,
            transfers=transfers,
        )
        return result

    tasks = [_fetch_one(entry, i + 1) for i, entry in enumerate(standings)]
    results = await asyncio.gather(*tasks)

    # Filter out failed fetches, sort by GW points descending
    managers = [m for m in results if m is not None]
    managers.sort(key=lambda m: -m["gw_points"])

    # Assign GW ranks
    for i, m in enumerate(managers):
        m["gw_rank"] = i + 1

    # Assign overall ranks from original standings order
    standings_order = {e.get("entry"): i + 1 for i, e in enumerate(standings)}
    for m in managers:
        m["overall_rank"] = standings_order.get(m["entry_id"], 0)

    return managers


# ---------------------------------------------------------------------------
# Standings movement
# ---------------------------------------------------------------------------


def _compute_standings_movement(managers: list[RecapManagerEntry]) -> None:
    """Derive previous league positions from total_points - gw_points.

    Mutates managers in-place to set previous_rank.
    """
    prev_totals = [(m["entry_id"], m["total_points"] - m["gw_points"]) for m in managers]
    prev_totals.sort(key=lambda x: -x[1])
    prev_rank_map = {entry_id: rank + 1 for rank, (entry_id, _) in enumerate(prev_totals)}

    for m in managers:
        m["previous_rank"] = prev_rank_map.get(m["entry_id"], m["overall_rank"])


# ---------------------------------------------------------------------------
# Fines evaluation (per-manager)
# ---------------------------------------------------------------------------


def _recap_fine_message(result: FineResult, manager_name: str) -> str:
    """Generate a clean recap-specific fine message (no 'FINE TRIGGERED' prefix)."""
    # Extract the penalty text (after the last period-space in the original message)
    penalty = ""
    if ". " in result.message:
        parts = result.message.rsplit(". ", 1)
        if len(parts) == 2:
            penalty = parts[1].rstrip(".")

    if result.rule_type == "last-place":
        return f"Finished last in the gameweek. {penalty}" if penalty else "Finished last in the gameweek."
    if result.rule_type == "red-card":
        # Extract player names from original message
        red_names = ""
        if "(" in result.message and ")" in result.message:
            red_names = result.message.split("(")[1].split(")")[0]
        base = f"Red card in starting XI ({red_names})"
        return f"{base}. {penalty}" if penalty else f"{base}."
    if result.rule_type == "below-threshold":
        return f"Scored below threshold. {penalty}" if penalty else "Scored below threshold."
    return result.message


def evaluate_league_fines(
    managers: list[RecapManagerEntry],
    settings: dict[str, Any],
    format_name: str,
) -> list[RecapFineResult]:
    """Evaluate fines for each manager. Returns only triggered fines.

    Gracefully returns empty list if fines are unconfigured or evaluation fails.
    """
    fines_config = parse_fines_config(settings)
    if fines_config is None:
        return []

    use_net_points = settings.get("use_net_points", False)

    # Find the worst performer (lowest GW points) for last-place rule
    worst = min(managers, key=lambda m: m["gw_points"]) if managers else None

    triggered: list[RecapFineResult] = []

    from fpl_cli.cli._fines import WorstPerformer

    for m in managers:
        try:

            worst_list: list[WorstPerformer] = []
            if worst:
                worst_list = [WorstPerformer(
                    is_user=m["entry_id"] == worst["entry_id"],
                    points=worst["gw_points"],
                    gross_points=worst["gw_points"] + worst["transfer_cost"],
                    name=worst["manager_name"],
                )]

            league_data = FinesLeagueData(
                user_gw_points=m["gw_points"] + m["transfer_cost"],
                worst_performers=worst_list,
            )
            if use_net_points:
                league_data["user_gw_net_points"] = m["gw_points"]

            # Build FinesTeamPlayer list from squad
            team_data: list[FinesTeamPlayer] = [
                FinesTeamPlayer(
                    name=p["name"],
                    red_cards=p["red_cards"],
                    contributed=p["contributed"],
                    auto_sub_out=p["auto_sub_out"],
                )
                for p in m["squad"]
            ]

            results = evaluate_fines(
                fines_config, format_name, league_data, team_data,
                use_net_points=use_net_points,
            )

            for r in results:
                if r.triggered:
                    msg = _recap_fine_message(r, m["manager_name"])
                    triggered.append(RecapFineResult(
                        manager_name=m["manager_name"],
                        rule_type=r.rule_type,
                        message=msg,
                    ))

        except Exception:  # noqa: BLE001 — best-effort enrichment
            logger.debug("Fines evaluation failed for %s", m["manager_name"], exc_info=True)

    return triggered


# ---------------------------------------------------------------------------
# Awards (pure functions)
# ---------------------------------------------------------------------------


def _captain_detail(caps: list[RecapManagerEntry]) -> str:
    """Build a detail string for tied captain awards, grouping by player."""
    if len(caps) == 1:
        m = caps[0]
        return f"{m['manager_name']} captained {m['captain']} ({m['captain_points']} pts)"

    from collections import defaultdict

    by_player: dict[str, list[str]] = defaultdict(list)
    for m in caps:
        by_player[m["captain"]].append(m["manager_name"])

    pts = caps[0]["captain_points"]
    parts = []
    for player, names in by_player.items():
        joined = ", ".join(names[:-1]) + " and " + names[-1] if len(names) > 1 else names[0]
        verb = "all captained" if len(names) > 2 else "captained"
        parts.append(f"{joined} {verb} {player} ({pts} pts)")
    return ", ".join(parts)


def _compute_shared_awards(
    managers: list[RecapManagerEntry],
    format_name: str = "classic",
) -> RecapAwards:
    """Compute awards common to both classic and draft, plus format-specific ones."""
    awards = RecapAwards()

    if not managers:
        return awards

    # GW winner (highest points)
    best_gw_pts = max(m["gw_points"] for m in managers)
    winners = [m for m in managers if m["gw_points"] == best_gw_pts]
    awards["gw_winner"] = RecapAwardEntry(
        manager_name=" and ".join(m["manager_name"] for m in winners),
        value=best_gw_pts,
        detail=", ".join(f"{m['manager_name']} with {m['gw_points']} pts" for m in winners),
    )

    # GW loser (lowest points)
    worst_gw_pts = min(m["gw_points"] for m in managers)
    losers = [m for m in managers if m["gw_points"] == worst_gw_pts]
    awards["gw_loser"] = RecapAwardEntry(
        manager_name=" and ".join(m["manager_name"] for m in losers),
        value=worst_gw_pts,
        detail=", ".join(f"{m['manager_name']} with {m['gw_points']} pts" for m in losers),
    )

    # Biggest bench haul
    best_bench_pts = max(m["bench_points"] for m in managers)
    if best_bench_pts > 0:
        bench_kings = [m for m in managers if m["bench_points"] == best_bench_pts]
        detail_parts = []
        for m in bench_kings:
            bench_players = [
                p for p in m["squad"]
                if not p["contributed"] and not p["auto_sub_out"] and p["points"] > 0
            ]
            player_detail = ", ".join(f"{p['name']} ({p['points']})" for p in bench_players)
            detail_parts.append(
                f"{m['manager_name']} left {m['bench_points']} pts on the bench"
                f" (team scored {m['gw_points']} pts): {player_detail}"
            )
        awards["biggest_bench_haul"] = RecapAwardEntry(
            manager_name=" and ".join(m["manager_name"] for m in bench_kings),
            value=best_bench_pts,
            detail="; ".join(detail_parts),
        )

    # Captain awards (classic only - draft has no captaincy)
    if format_name == "classic":
        best_cap_pts = max(m["captain_points"] for m in managers)
        if best_cap_pts > 0:
            best_caps = [m for m in managers if m["captain_points"] == best_cap_pts]
            awards["best_captain"] = RecapAwardEntry(
                manager_name=" and ".join(m["manager_name"] for m in best_caps),
                value=best_cap_pts,
                detail=_captain_detail(best_caps),
            )

        played_caps = [m for m in managers if m.get("captain_played", True)]
        worst_pool = played_caps if played_caps else managers
        worst_cap_pts = min(m["captain_points"] for m in worst_pool)
        worst_caps = [m for m in worst_pool if m["captain_points"] == worst_cap_pts]
        awards["worst_captain"] = RecapAwardEntry(
            manager_name=" and ".join(m["manager_name"] for m in worst_caps),
            value=worst_cap_pts,
            detail=_captain_detail(worst_caps),
        )

    # Format-specific awards
    if format_name == "classic":
        _compute_transfer_awards(managers, awards)
    elif format_name == "draft":
        _compute_waiver_awards(managers, awards)

    return awards


def _compute_transfer_awards(
    managers: list[RecapManagerEntry],
    awards: RecapAwards,
) -> None:
    """Compute transfer genius/disaster awards for classic format."""
    managers_with_transfers = [m for m in managers if m.get("transfers")]

    if not managers_with_transfers:
        return

    # Transfer genius: best total net across all transfers
    def _transfer_net(m: RecapManagerEntry) -> int:
        return sum(t["net"] for t in m.get("transfers", []))

    genius = max(managers_with_transfers, key=_transfer_net)
    genius_net = _transfer_net(genius)
    if genius_net > 0:
        best_transfer = max(genius.get("transfers", []), key=lambda t: t["net"])
        awards["transfer_genius"] = RecapAwardEntry(
            manager_name=genius["manager_name"],
            value=genius_net,
            detail=(
                f"{genius['manager_name']} gained {genius_net} net pts from transfers"
                f" (best: {best_transfer['player_in']} for {best_transfer['player_out']},"
                f" +{best_transfer['net']})"
            ),
        )

    # Transfer disaster: worst total net
    disaster = min(managers_with_transfers, key=_transfer_net)
    disaster_net = _transfer_net(disaster)
    if disaster_net < 0:
        worst_transfer = min(disaster.get("transfers", []), key=lambda t: t["net"])
        awards["transfer_disaster"] = RecapAwardEntry(
            manager_name=disaster["manager_name"],
            value=disaster_net,
            detail=(
                f"{disaster['manager_name']} lost {abs(disaster_net)} net pts from transfers"
                f" (worst: {worst_transfer['player_in']} for {worst_transfer['player_out']},"
                f" {worst_transfer['net']})"
            ),
        )


def _compute_waiver_awards(
    managers: list[RecapManagerEntry],
    awards: RecapAwards,
) -> None:
    """Compute waiver genius/disaster awards for draft format."""
    managers_with_txns = [m for m in managers if m.get("transactions")]

    if not managers_with_txns:
        return

    def _txn_net(m: RecapManagerEntry) -> int:
        return sum(t["net"] for t in m.get("transactions", []))

    genius = max(managers_with_txns, key=_txn_net)
    genius_net = _txn_net(genius)
    if genius_net > 0:
        best_txn = max(genius.get("transactions", []), key=lambda t: t["net"])
        awards["waiver_genius"] = RecapAwardEntry(
            manager_name=genius["manager_name"],
            value=genius_net,
            detail=(
                f"{genius['manager_name']} gained {genius_net} net pts from waivers"
                f" (best: {best_txn['player_in']} for {best_txn.get('player_out', '?')},"
                f" +{best_txn['net']})"
            ),
        )

    disaster = min(managers_with_txns, key=_txn_net)
    disaster_net = _txn_net(disaster)
    if disaster_net < 0:
        worst_txn = min(disaster.get("transactions", []), key=lambda t: t["net"])
        awards["waiver_disaster"] = RecapAwardEntry(
            manager_name=disaster["manager_name"],
            value=disaster_net,
            detail=(
                f"{disaster['manager_name']} lost {abs(disaster_net)} net pts from waivers"
                f" (worst: {worst_txn['player_in']} for {worst_txn.get('player_out', '?')},"
                f" {worst_txn['net']})"
            ),
        )


# ---------------------------------------------------------------------------
# Draft data collection
# ---------------------------------------------------------------------------


async def collect_draft_recap_data(
    settings: dict[str, Any],
    gw: int,
    live_stats: dict[int, dict[str, Any]],
    players: list[Player],
    teams: dict[int, Team],
) -> LeagueRecapData:
    """Fetch all managers' draft picks and compute league-wide recap data."""
    from fpl_cli.api.fpl_draft import FPLDraftClient
    from fpl_cli.models.player import POSITION_MAP

    draft_league_id = settings.get("fpl", {}).get("draft_league_id")

    async with FPLDraftClient() as draft_client:
        league_details = await draft_client.get_league_details(draft_league_id)
        league_name = league_details.get("league", {}).get("name", "Draft League")
        standings = league_details.get("standings", [])
        league_entries = league_details.get("league_entries", [])
        entry_map = {e.get("id"): e for e in league_entries}

        # Build Draft-specific player map and ID mapping
        draft_bootstrap = await draft_client.get_bootstrap_static()
        draft_elements = draft_bootstrap.get("elements", [])
        draft_player_map = {p["id"]: p for p in draft_elements}

        main_player_by_name_team = {(p.web_name, p.team_id): p for p in players}
        draft_to_main_id: dict[int, int] = {}
        for dp in draft_elements:
            key = (dp.get("web_name"), dp.get("team"))
            main_player = main_player_by_name_team.get(key)
            if main_player:
                draft_to_main_id[dp["id"]] = main_player.id

        # Fetch all transactions for the league, filter to this GW
        txn_response = await draft_client.get_league_transactions(draft_league_id)
        all_txns: list[dict[str, Any]] = txn_response.get("transactions", [])
        gw_txns = [
            t for t in all_txns
            if t.get("event") == gw and t.get("result") == "a"
        ]
        txns_by_entry: dict[int, list[dict[str, Any]]] = {}
        for txn in gw_txns:
            txn_entry: int = txn.get("entry", 0)
            txns_by_entry.setdefault(txn_entry, []).append(txn)

        # Fetch picks for each manager
        sem = asyncio.Semaphore(_PICKS_CONCURRENCY)
        managers: list[RecapManagerEntry] = []

        async def _fetch_draft_manager(standing: dict[str, Any], rank: int) -> RecapManagerEntry | None:
            league_entry_id: int = standing.get("league_entry", 0)
            entry_info = entry_map.get(league_entry_id, {})
            entry_id = entry_info.get("entry_id")
            manager_name = f"{entry_info.get('player_first_name', '')} {entry_info.get('player_last_name', '')}".strip()
            if not manager_name:
                manager_name = entry_info.get("entry_name", "Unknown")

            gw_pts: int = standing.get("event_total", 0)
            total_pts: int = standing.get("total", 0)

            async with sem:
                try:
                    picks_data = await draft_client.get_entry_picks(entry_id, gw)
                except Exception as e:  # noqa: BLE001 — graceful degradation
                    logger.warning("Failed to fetch draft picks for %s: %s", manager_name, e)
                    return None

            picks = picks_data.get("picks", [])
            subs = picks_data.get("subs", [])
            auto_sub_in_ids = {s["element_in"] for s in subs}
            auto_sub_out_ids = {s["element_out"] for s in subs}

            squad: list[RecapManagerPlayer] = []
            captain_name = ""
            captain_points = 0
            vice_captain_name = ""
            bench_points = 0

            for pick in picks:
                draft_elem_id = pick.get("element")
                draft_player = draft_player_map.get(draft_elem_id)
                if not draft_player:
                    continue

                main_id = draft_to_main_id.get(draft_elem_id)
                pts, _, red_cards = _live_player_stats(live_stats, main_id)
                pos_name = POSITION_MAP.get(draft_player.get("element_type"), "???")
                team_short = t.short_name if (t := teams.get(draft_player.get("team"))) else "???"
                squad_position = pick.get("position", 1)
                is_bench = squad_position > 11
                contributed = not is_bench
                if draft_elem_id in auto_sub_in_ids:
                    contributed = True

                if is_bench and draft_elem_id not in auto_sub_in_ids:
                    bench_points += pts

                squad.append(RecapManagerPlayer(
                    name=draft_player.get("web_name", "Unknown"),
                    team=team_short,
                    position=pos_name,
                    points=pts,
                    is_captain=False,
                    is_vice_captain=False,
                    contributed=contributed,
                    auto_sub_in=draft_elem_id in auto_sub_in_ids,
                    auto_sub_out=draft_elem_id in auto_sub_out_ids,
                    red_cards=red_cards,
                ))

            # Build auto-sub descriptions
            auto_sub_descs: list[str] = []
            for s in subs:
                pin = draft_player_map.get(s["element_in"])
                pout = draft_player_map.get(s["element_out"])
                if pin and pout:
                    pin_main_id = draft_to_main_id.get(s["element_in"])
                    pin_pts, _, _ = _live_player_stats(live_stats, pin_main_id)
                    auto_sub_descs.append(
                        f"{pin.get('web_name', '?')} on for {pout.get('web_name', '?')} ({pin_pts} pts)"
                    )

            # Build transaction data for this manager
            manager_txns: list[RecapDraftTransaction] = []
            for txn in txns_by_entry.get(league_entry_id, []):
                pin_id: int = txn.get("element_in", 0)
                pout_id: int | None = txn.get("element_out")
                dp_in = draft_player_map.get(pin_id)
                dp_out = draft_player_map.get(pout_id) if pout_id else None

                if dp_in:
                    main_in_id = draft_to_main_id.get(pin_id)
                    in_pts, _, _ = _live_player_stats(live_stats, main_in_id)
                    out_pts = 0
                    if dp_out and pout_id is not None:
                        main_out_id = draft_to_main_id.get(pout_id)
                        out_pts, _, _ = _live_player_stats(live_stats, main_out_id)

                    in_team = t.short_name if (t := teams.get(dp_in.get("team"))) else "???"
                    out_team = t.short_name if dp_out and (t := teams.get(dp_out.get("team"))) else None

                    manager_txns.append(RecapDraftTransaction(
                        player_in=dp_in.get("web_name", "Unknown"),
                        player_in_team=in_team,
                        player_in_points=in_pts,
                        player_out=dp_out.get("web_name") if dp_out else None,
                        player_out_team=out_team,
                        player_out_points=out_pts if dp_out else None,
                        net=in_pts - out_pts,
                        kind=txn.get("kind", "w"),
                    ))

            result = RecapManagerEntry(
                manager_name=manager_name,
                entry_id=entry_id or 0,
                gw_points=gw_pts,
                total_points=total_pts,
                gw_rank=rank,
                overall_rank=rank,
                previous_rank=rank,
                captain=captain_name,
                captain_points=captain_points,
                captain_played=False,
                vice_captain=vice_captain_name,
                active_chip=None,
                squad=squad,
                bench_points=bench_points,
                transfer_cost=0,
                auto_subs=auto_sub_descs,
                transactions=manager_txns,
            )
            return result

        tasks = [_fetch_draft_manager(s, i + 1) for i, s in enumerate(standings)]
        results = await asyncio.gather(*tasks)

        managers = [m for m in results if m is not None]
        managers.sort(key=lambda m: -m["gw_points"])
        for i, m in enumerate(managers):
            m["gw_rank"] = i + 1

        standings_order = {
            entry_map.get(s.get("league_entry"), {}).get("entry_id"): i + 1
            for i, s in enumerate(standings)
        }
        for m in managers:
            m["overall_rank"] = standings_order.get(m["entry_id"], 0)

    _compute_standings_movement(managers)
    awards = _compute_shared_awards(managers, format_name="draft")

    return LeagueRecapData(
        gameweek=gw,
        league_name=league_name,
        fpl_format="draft",
        managers=managers,
        awards=awards,
    )
