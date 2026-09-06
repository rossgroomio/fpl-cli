"""League recap data collection: per-manager stats, awards, standings movement."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal, Protocol

from fpl_cli.cli._context import fpl_config
from fpl_cli.cli._fines import (
    FineResult,
    FinesLeagueData,
    FinesTeamPlayer,
    evaluate_fines,
    rules_for_format,
)
from fpl_cli.cli._fines_config import parse_fines_config
from fpl_cli.cli._helpers import _live_player_stats

if TYPE_CHECKING:
    from fpl_cli.models.player import Player
    from fpl_cli.models.team import Team

    class ManagerPicksClient(Protocol):
        """The subset of FPLClient that _fetch_all_manager_data calls.

        FPLClient satisfies this structurally; a test fake only needs to
        implement these two methods rather than the whole real client.
        """

        async def get_manager_picks(self, entry_id: int, gameweek: int, /) -> dict[str, Any]: ...
        async def get_manager_transfers(self, entry_id: int, /) -> list[dict[str, Any]]: ...

    class ManagerHistoryClient(Protocol):
        """The subset of FPLClient that _apply_league_start_offset calls."""

        async def get_manager_history(self, entry_id: int, /) -> dict[str, Any]: ...

    class ClassicRecapClient(ManagerPicksClient, ManagerHistoryClient, Protocol):
        """The subset of FPLClient that collect_classic_recap_data calls.

        Widens ManagerPicksClient with the league standings fetch and the
        manager-history baseline fetch _apply_league_start_offset makes for a
        league with start_event > 1.
        """

        async def get_classic_league_standings(
            self, league_id: int, page: int = 1, /,
        ) -> dict[str, Any]: ...
from fpl_cli.cli._league_recap_types import (
    LeagueRecapData,
    RecapAwardEntry,
    RecapAwards,
    RecapDraftTransaction,
    RecapFineResult,
    RecapManagerEntry,
    RecapManagerPlayer,
    RecapStandingsEntry,
    RecapTransfer,
)
from fpl_cli.services.fixture_predictions import had_fixture

logger = logging.getLogger(__name__)


def recap_manager_key(m: RecapManagerEntry) -> int:
    """The ledger key for a collected manager.

    Draft keys on the league-local `league_entry` id, which is always present;
    the site-wide `entry_id` is null for an unclaimed team and two of them
    would collide on 0 (KTD11). Classic has no league-local id and keys on
    `entry_id`, which the collector always populates.
    """
    return m.get("league_entry_id", m["entry_id"])


class RecapReconciliationError(RuntimeError):
    """A point-in-time headline number disagrees with a second, independent source.

    Raised only for the live/current gameweek, where both sources should
    always agree. A mismatch means the point-in-time field mapping is wrong,
    so every row a future capture would derive from it is wrong too -- this
    must stop the run rather than degrade silently.
    """


_CHIP_DISPLAY = {"wildcard": "WC", "freehit": "FH", "bboost": "BB", "3xc": "TC"}
_CHIP_RAW = {display: raw for raw, display in _CHIP_DISPLAY.items()}


def raw_chip_name(display: str | None) -> str | None:
    """Map a displayed chip abbreviation back to the raw API name.

    `RecapManagerEntry.active_chip` holds the display form, but the ledger
    stores what the API said so a recorded row never depends on a rendering
    choice. An unrecognised value passes through unchanged -- the collector
    already passes through a chip it has no abbreviation for.
    """
    if not display:
        return None
    return _CHIP_RAW.get(display, display)
_PICKS_CONCURRENCY = 10
# Most managers named in one award's detail before it is truncated. Shared by
# the transfer/waiver, captain, and bench-haul awards so a wide tie in a large
# league cannot sprawl.
_DETAIL_CAP = 3


def _omitted_suffix(omitted: int, noun: str | None = None) -> str:
    """Render the "; N more [noun(s)] omitted" tail once a _DETAIL_CAP list is
    truncated, shared by the transfer/waiver, captain, and bench-haul awards.
    """
    if not omitted:
        return ""
    if noun is None:
        return f"; {omitted} more omitted"
    plural = noun if omitted == 1 else f"{noun}s"
    return f"; {omitted} more {plural} omitted"


def _classic_pick_flags(
    *,
    pick: dict[str, Any],
    active_chip: str | None,
    player_id: int,
    auto_sub_in_ids: set[int],
) -> tuple[bool, bool, bool]:
    """Return (is_bench, is_bench_boost_player, contributed) for a classic pick.

    Bench Boost flips `contributed` true for bench slots (12-15) because those
    points actually count toward the manager's GW total.
    """
    is_bench = pick.get("position", 1) > 11
    is_bench_boost_player = active_chip == "bboost" and is_bench
    contributed = (not is_bench) and pick.get("multiplier", 0) > 0
    if player_id in auto_sub_in_ids or is_bench_boost_player:
        contributed = True
    return is_bench, is_bench_boost_player, contributed


# ---------------------------------------------------------------------------
# Classic data collection
# ---------------------------------------------------------------------------


async def collect_classic_recap_data(
    client: ClassicRecapClient,
    settings: dict[str, Any],
    gw: int,
    live_stats: dict[int, dict[str, Any]],
    player_map: dict[int, Player],
    teams: dict[int, Team],
    *,
    is_live_gw: bool = True,
    bgw_team_ids: frozenset[int] = frozenset(),
    players_with_fixture: frozenset[int] | None = None,
) -> LeagueRecapData:
    """Fetch all managers' picks and compute league-wide recap data.

    `is_live_gw` marks whether the league standings still describe `gw` (a
    live capture) rather than a later gameweek the season has moved on to (a
    replay). Being the most recently finished gameweek is not enough: the
    standings follow whatever gameweek the API calls current, so they move on
    at the next deadline while the recapped gameweek stays put (issue #262).
    It gates the headline-numbers reconciliation, which only holds for a live
    capture -- see RecapReconciliationError.

    `bgw_team_ids` is the set of clubs with no fixture this gameweek, so a
    recorded squad can tell a player who blanked apart from one who never
    kicked a ball (R20). `players_with_fixture` answers the same question
    from the gameweek's own live data and takes precedence where it can, so a
    replay does not read the blank off a club the player has since moved to
    (issue #169); `resolve_players_with_fixture` (`services/fixture_predictions.py`)
    builds it.

    Returns a LeagueRecapData dict ready for template rendering.
    """
    # `Any`, deliberately: the recap has never guarded a missing league id --
    # an unconfigured one 404s through `api_failure_boundary` -- and typing it
    # `int | None` here would only move that failure, not fix it.
    classic_league_id: Any = fpl_config(settings).get("classic_league_id")
    use_net_points = settings.get("use_net_points", False)

    standings_response = await client.get_classic_league_standings(classic_league_id)
    league = standings_response.get("league", {})
    league_name = league.get("name", "Unknown League")
    standings_block = standings_response.get("standings", {})
    standings = standings_block.get("results", [])

    managers = await _fetch_all_manager_data(
        client, standings, gw, live_stats, player_map, teams,
        use_net_points=use_net_points, is_live_gw=is_live_gw,
        bgw_team_ids=bgw_team_ids, players_with_fixture=players_with_fixture,
    )

    league_rows = [
        (e.get("entry", 0), e.get("total", 0), e.get("event_total", 0))
        for e in standings
    ]

    start_event = league.get("start_event")
    if start_event and start_event > 1 and gw < start_event:
        # The league did not exist yet at `gw`, so it has no table to place
        # anyone on and no baseline to subtract (subtracting a later one
        # would drive every total negative and invert the order). Drop the
        # season-wide totals rather than rank the cohort on numbers that
        # never belonged to this league.
        logger.warning(
            "Gameweek %s precedes the league's start (GW%s): cumulative totals and "
            "league positions are unavailable for this replay.", gw, start_event,
        )
        for m in managers:
            if "total_points" in m:
                del m["total_points"]
    elif start_event and start_event > 1:
        await _apply_league_start_offset(client, managers, start_event)

    _assign_point_in_time_positions(
        managers,
        [(entry_id, total) for entry_id, total, _ in league_rows],
        allow_standings_fallback=is_live_gw,
    )
    if _has_previous_gameweek(gw, start_event):
        _compute_standings_movement(
            managers, league_rows,
            use_net_points=use_net_points,
            allow_standings_fallback=is_live_gw,
        )

    awards = _compute_shared_awards(managers, format_name="classic", total_managers=len(standings))

    cohort: list[RecapStandingsEntry] = [
        RecapStandingsEntry(
            manager_key=e.get("entry", 0),
            manager_name=e.get("player_name", "Unknown"),
            entry_id=e.get("entry"),
            gw_points=e.get("event_total", 0),
            total_points=e.get("total", 0),
        )
        for e in standings
    ]

    data = LeagueRecapData(
        gameweek=gw,
        league_name=league_name,
        fpl_format="classic",
        managers=managers,
        awards=awards,
        league_id=classic_league_id,
        standings_cohort=cohort,
        # One page of standings is 50 entries; `has_next` says a larger league
        # is only partly in hand, which the ledger would otherwise inherit
        # silently (the same trap `status` guards against).
        standings_truncated=bool(standings_block.get("has_next", False)),
    )
    if start_event:
        data["league_start_event"] = start_event
    return data


# ---------------------------------------------------------------------------
# Per-manager fetch (bounded concurrency)
# ---------------------------------------------------------------------------


async def _fetch_all_manager_data(
    client: ManagerPicksClient,
    standings: list[dict[str, Any]],
    gw: int,
    live_stats: dict[int, dict[str, Any]],
    player_map: dict[int, Player],
    teams: dict[int, Team],
    *,
    use_net_points: bool = False,
    is_live_gw: bool = True,
    bgw_team_ids: frozenset[int] = frozenset(),
    players_with_fixture: frozenset[int] | None = None,
) -> list[RecapManagerEntry]:
    """Fetch picks for every manager in the league, extract recap data.

    Point-in-time headline numbers (gross points, cumulative total) are read
    from each manager's own `entry_history` -- present in the picks response
    for any gameweek, past or present -- rather than the standings row, which
    only ever reflects the *current* state. A picks response with no
    `entry_history` at all falls back to the standings row, the only source
    left for it. League position is left to the caller: it needs
    cross-manager context (and, for a league that started after GW1, a
    baseline offset) a per-manager fetch does not have.

    `is_live_gw` marks the standings as still describing `gw` rather than a
    later gameweek the season has moved on to, which gates the reconciliation
    against the standings row -- see RecapReconciliationError.
    """
    sem = asyncio.Semaphore(_PICKS_CONCURRENCY)

    async def _fetch_one(entry: dict, rank: int) -> RecapManagerEntry | None:
        league_entry_id: int = entry.get("entry", 0)
        manager_name: str = entry.get("player_name", "Unknown")
        standings_gross: int = entry.get("event_total", 0)
        standings_total: int = entry.get("total", 0)

        async with sem:
            try:
                picks_response = await client.get_manager_picks(league_entry_id, gw)
            except Exception as e:  # noqa: BLE001 — graceful degradation
                logger.warning("Failed to fetch picks for %s (entry %s): %s", manager_name, league_entry_id, e)
                return None

        picks = picks_response.get("picks", [])
        entry_history = picks_response.get("entry_history") or {}
        active_chip = picks_response.get("active_chip")
        automatic_subs = picks_response.get("automatic_subs", [])

        transfer_cost = entry_history.get("event_transfers_cost", 0)
        gross_points = entry_history.get("points", standings_gross)
        total_pts = entry_history.get("total_points", standings_total)
        gw_points = (gross_points - transfer_cost) if use_net_points else gross_points

        auto_sub_in_ids = {sub["element_in"] for sub in automatic_subs}
        auto_sub_out_ids = {sub["element_out"] for sub in automatic_subs}

        # Build squad
        squad: list[RecapManagerPlayer] = []
        captain_name = ""
        captain_points = 0
        captain_played = False
        vice_captain_name = ""
        vice_captain_points = 0

        bench_points = 0
        for pick in picks:
            player = player_map.get(pick["element"])
            if not player:
                continue

            pts, minutes, red_cards = _live_player_stats(live_stats, player.id)
            is_bench, is_bench_boost_player, contributed = _classic_pick_flags(
                pick=pick,
                active_chip=active_chip,
                player_id=player.id,
                auto_sub_in_ids=auto_sub_in_ids,
            )

            # Bench points: actual bench slot, not auto-subbed out, not BB (BB bench counts)
            if is_bench and player.id not in auto_sub_in_ids and not is_bench_boost_player:
                bench_points += pts

            player_team = teams.get(player.team_id)
            squad.append(RecapManagerPlayer(
                name=player.web_name,
                team=player_team.short_name if player_team else "???",
                team_name=player_team.name if player_team else None,
                position=player.position_name,
                code=player.code or None,
                points=pts,
                is_captain=pick.get("is_captain", False),
                is_vice_captain=pick.get("is_vice_captain", False),
                contributed=contributed,
                is_bench_boost_player=is_bench_boost_player,
                auto_sub_in=player.id in auto_sub_in_ids,
                auto_sub_out=player.id in auto_sub_out_ids,
                red_cards=red_cards,
                unmatched=False,
                had_fixture=had_fixture(
                    player.id, player.team_id,
                    players_with_fixture=players_with_fixture,
                    bgw_team_ids=bgw_team_ids,
                ),
            ))

            if pick.get("is_captain"):
                captain_name = player.web_name
                captain_points = pts
                captain_played = minutes > 0
            if pick.get("is_vice_captain"):
                vice_captain_name = player.web_name
                vice_captain_points = pts

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
                # Same permit as the picks fetch: this is a second network call
                # per manager, and without it a large league fans out unbounded.
                async with sem:
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
                        transfer = RecapTransfer(
                            player_in=pin.web_name,
                            player_in_team=pin_team.short_name if pin_team else "???",
                            player_in_team_name=pin_team.name if pin_team else None,
                            player_in_points=pin_pts,
                            player_out=pout.web_name,
                            player_out_team=pout_team.short_name if pout_team else "???",
                            player_out_team_name=pout_team.name if pout_team else None,
                            player_out_points=pout_pts,
                            net=pin_pts - pout_pts,
                            cost=transfer_cost,
                        )
                        if pin.code:
                            transfer["player_in_code"] = pin.code
                        if pout.code:
                            transfer["player_out_code"] = pout.code
                        transfers.append(transfer)
            except Exception as e:  # noqa: BLE001 — best-effort enrichment
                logger.debug("Could not fetch transfers for %s: %s", manager_name, e)

        result = RecapManagerEntry(
            manager_name=manager_name,
            entry_id=league_entry_id,
            gw_points=gw_points,
            gross_points=gross_points,
            total_points=total_pts,
            gw_rank=rank,
            captain=captain_name,
            captain_points=captain_points,
            captain_played=captain_played,
            vice_captain=vice_captain_name,
            vice_captain_points=vice_captain_points,
            active_chip=_CHIP_DISPLAY.get(active_chip, active_chip) if active_chip else None,
            squad=squad,
            bench_points=bench_points,
            transfer_cost=transfer_cost,
            auto_subs=auto_sub_descriptions,
            transfers=transfers,
            transfers_made=num_transfers,
        )
        # Four more figures the rollover destroys, already in hand on the
        # object the headline numbers came from (R2). Recorded only where the
        # response actually carried them, so a partial one leaves them absent
        # rather than zero. `global_rank` is the FPL-wide cumulative rank,
        # `global_gw_rank` the FPL-wide rank for this gameweek alone -- the
        # API's `overall_rank` and `rank` respectively -- and neither is a
        # league position (KTD12, issue #148).
        # `value` is bank-inclusive, hence `team_value` rather than
        # `squad_value` -- the squad alone is `team_value - bank`.
        team_value = entry_history.get("value")
        if isinstance(team_value, int):
            result["team_value"] = team_value
        bank = entry_history.get("bank")
        if isinstance(bank, int):
            result["bank"] = bank
        global_rank = entry_history.get("overall_rank")
        if isinstance(global_rank, int):
            result["global_rank"] = global_rank
        global_gw_rank = entry_history.get("rank")
        if isinstance(global_gw_rank, int):
            result["global_gw_rank"] = global_gw_rank
        return result

    tasks = [_fetch_one(entry, i + 1) for i, entry in enumerate(standings)]
    results = await asyncio.gather(*tasks)

    # Filter out failed fetches, sort by GW points descending
    managers = [m for m in results if m is not None]
    managers.sort(key=lambda m: -m["gw_points"])

    # Assign GW ranks
    for i, m in enumerate(managers):
        m["gw_rank"] = i + 1

    if is_live_gw:
        _reconcile_classic_headline_numbers(managers, standings)

    return managers


# ---------------------------------------------------------------------------
# Headline-number reconciliation (U1)
# ---------------------------------------------------------------------------


def _reconcile_classic_headline_numbers(
    managers: list[RecapManagerEntry],
    standings: Sequence[dict[str, Any]],
) -> None:
    """Cross-check entry_history against the standings row it replaced.

    Meaningful only for the live gameweek, where both sources describe the
    same point in time -- a replay is expected to diverge and must not call
    this. Gameweek points must agree exactly wherever a manager took a hit,
    since gross points cannot depend on gross/net semantics; a mismatch there
    means the point-in-time field mapping is wrong and is a stop condition.
    With no manager who took a hit this gameweek, the check is inconclusive
    rather than passing. Cumulative total is allowed to diverge -- a league
    that started after GW1 diverges there by design, see
    _apply_league_start_offset -- so a mismatch there is only a warning.

    One mismatch is expected rather than fatal: a standings `event_total`
    that is exactly the hit lower than `entry_history.points` is the same
    gameweek reported net of the hit, which is what the FPL league table
    displays. That is a gross/net difference between two sources describing
    the same points, not the wrong field -- it is logged and the run
    continues. Any other size of gap is the stop condition.
    """
    standings_by_entry = {e.get("entry"): e for e in standings}

    hit_mismatches: list[tuple[str, int, int]] = []
    total_mismatches: list[tuple[str, int, int]] = []
    net_rows: list[str] = []
    saw_a_hit = False

    for m in managers:
        std = standings_by_entry.get(m["entry_id"])
        if std is None:
            continue
        std_gross = std.get("event_total", 0)
        std_total = std.get("total", 0)

        if m["transfer_cost"] > 0:
            saw_a_hit = True
            if m["gross_points"] - m["transfer_cost"] == std_gross:
                net_rows.append(m["manager_name"])
            elif m["gross_points"] != std_gross:
                hit_mismatches.append((m["manager_name"], m["gross_points"], std_gross))

        if "total_points" in m and m["total_points"] != std_total:
            total_mismatches.append((m["manager_name"], m["total_points"], std_total))

    if hit_mismatches:
        detail = "; ".join(f"{name}: entry_history={eh} standings={st}" for name, eh, st in hit_mismatches)
        raise RecapReconciliationError(
            "Gameweek-points reconciliation failed for the live gameweek "
            f"({detail}). The point-in-time field mapping (gross vs net) is "
            "likely wrong -- every row derived from it would be wrong too."
        )
    if net_rows:
        logger.info(
            "Standings report gameweek points net of the hit for %d manager(s) (%s);"
            " entry_history reports them gross, as recorded.",
            len(net_rows), ", ".join(net_rows),
        )
    if not saw_a_hit:
        logger.debug("Gameweek-points reconciliation inconclusive: no manager took a hit this gameweek")

    if total_mismatches:
        detail = "; ".join(f"{name}: entry_history={eh} standings={st}" for name, eh, st in total_mismatches)
        logger.warning(
            "Cumulative-total divergence between entry_history and standings for %d manager(s)"
            " -- expected for a league that started after GW1: %s",
            len(total_mismatches), detail,
        )


def derive_point_in_time_positions(
    totals: Sequence[tuple[int, int]],
) -> dict[int, int]:
    """Rank entries by cumulative total, descending, into 1-based positions.

    `totals` is (entry_id, point-in-time cumulative total) for every member
    with a known total -- omit a member with none rather than passing a
    placeholder; they simply get no entry in the returned mapping.

    Competition ranking, not ordinal: entries level on points share the
    better position and the next distinct total skips the places they
    consumed (1, 2, 2, 4). Ordinal numbering handed tied managers distinct
    positions decided by the order `totals` happened to arrive in -- cohort
    standings order, which itself moves through the season -- so the same
    gameweek could rank a tie differently on a later backfill, and every
    consumer of a position inherited an ordering nothing in the data
    supported (issue #164 review; issue #163 fixed the same defect where
    `gw_rank` reached the streak predicates).

    Sharing the place is the honest reading rather than merely the safer
    one: classic breaks a points tie on fewest transfers season-to-date
    (`docs/fpl-rules.md`), which nothing in the ledger records, so two
    managers level on points are genuinely indistinguishable here. Where a
    tie *can* be broken authoritatively the API does it for us and this
    function is never consulted -- a live draft capture takes the league's
    own `rank`, which already applies the h2h points-for tie-break.

    Reusable across both collectors and, per KTD12, by the coarse-backfill
    path that has no collector to call.
    """
    ordered = sorted(totals, key=lambda kv: -kv[1])
    positions: dict[int, int] = {}
    previous_total: int | None = None
    shared_position = 0
    for index, (entry_id, total) in enumerate(ordered):
        if total != previous_total:
            # A new distinct total starts at the place this entry actually
            # occupies, so a shared place consumes the ones behind it.
            shared_position = index + 1
            previous_total = total
        positions[entry_id] = shared_position
    return positions


def _assign_point_in_time_positions(
    managers: list[RecapManagerEntry],
    league_totals: Sequence[tuple[int, int]],
    *,
    allow_standings_fallback: bool,
) -> None:
    """Set `overall_rank` from a ranking over the *whole* league cohort.

    `league_totals` is (entry_id, standings cumulative total) for every entry
    in league-standings order, including managers whose picks failed to
    fetch. A manager missing from `managers` still occupies a place in the
    real table, so ranking survivors alone would silently renumber everyone
    below them -- the same trap `_compute_standings_movement` documents.

    `allow_standings_fallback` says whether the standings total may stand in
    for a manager with no point-in-time total. It may only for a live
    capture, where the standings describe the same point in time as the
    collected data. On a replay they describe a later one, and mixing the
    two eras in a single ranking produces positions that are wrong for both:
    rather than that, no position is derived for anyone and the report
    renders them unavailable.

    Mutates managers in-place. Leaves `overall_rank` unset wherever no
    position could be derived.
    """
    by_entry = {m["entry_id"]: m for m in managers}
    totals: list[tuple[int, int]] = []
    for entry_id, standings_total in league_totals:
        fetched = by_entry.get(entry_id)
        if fetched is not None and "total_points" in fetched:
            totals.append((entry_id, fetched["total_points"]))
        elif allow_standings_fallback:
            totals.append((entry_id, standings_total))
        else:
            logger.warning(
                "League positions unavailable: no point-in-time cumulative total for "
                "entry %s, and the standings total belongs to a later gameweek.",
                entry_id,
            )
            return

    position_map = derive_point_in_time_positions(totals)
    for m in managers:
        rank = position_map.get(m["entry_id"])
        if rank is not None:
            m["overall_rank"] = rank


async def _apply_league_start_offset(
    client: ManagerHistoryClient,
    managers: list[RecapManagerEntry],
    start_event: int,
) -> None:
    """Rescope each manager's stored cumulative total to the league's own start.

    `entry_history.total_points` is the FPL-wide season total; a league
    created after GW1 scores its members only from `start_event` onward, so
    ranking or displaying the season total would diverge from the league's
    own standings (the divergence _reconcile_classic_headline_numbers warns
    about). The baseline is each manager's season total as of the gameweek
    before the league started, read from the manager-history endpoint -- the
    same call U7's coarse backfill makes (KTD12).

    A manager whose history fetch fails, or whose history does not reach
    back to the baseline gameweek, has their total dropped rather than kept:
    a season-wide total sitting among league-scoped ones is not merely less
    precise, it is on a different scale, and would rank that manager top and
    shift everyone else down. Without a total they are simply left out of
    the ranking (rendered "unavailable"), which costs one row instead of
    corrupting the table. Still best-effort -- this does not block the run,
    so it is not the correctness class RecapReconciliationError guards.
    """
    baseline_gw = start_event - 1
    sem = asyncio.Semaphore(_PICKS_CONCURRENCY)

    async def _offset_one(m: RecapManagerEntry) -> None:
        if "total_points" not in m:
            return
        async with sem:
            try:
                history = await client.get_manager_history(m["entry_id"])
            except Exception as e:  # noqa: BLE001 — best-effort offset; total dropped on failure
                logger.warning(
                    "Failed to fetch manager history for %s (entry %s); their cumulative "
                    "total is left unavailable rather than ranked on a season-wide "
                    "figure the rest of the league is not on: %s",
                    m["manager_name"], m["entry_id"], e,
                )
                del m["total_points"]
                return
        baseline_row = next(
            (row for row in history.get("current", []) if row.get("event") == baseline_gw),
            None,
        )
        if baseline_row is None:
            logger.warning(
                "Manager history for %s (entry %s) has no GW%s row to offset from; their "
                "cumulative total is left unavailable.",
                m["manager_name"], m["entry_id"], baseline_gw,
            )
            del m["total_points"]
            return
        m["total_points"] -= baseline_row.get("total_points", 0)

    await asyncio.gather(*(_offset_one(m) for m in managers))


# ---------------------------------------------------------------------------
# Standings movement
# ---------------------------------------------------------------------------


def _has_previous_gameweek(gw: int, start_event: int | None = None) -> bool:
    """Whether a gameweek this league scored comes before `gw`.

    Movement is a claim about a table that existed a week ago. On the
    league's first scored gameweek no such table exists, and every
    derivation quietly says "no movement" instead of "no previous
    gameweek": subtracting gameweek points from a cumulative total leaves
    every manager on zero, so the tie-break hands each of them back their
    current position. The two are indistinguishable downstream -- in the
    ledger row most of all, which outlives the API that could settle it --
    so no previous position is derived at all rather than a flat one
    fabricated (issue #147).

    `start_event` is the league's own first gameweek: a league created
    after GW1 scores its members only from there, so that gameweek has no
    predecessor either even though GW1 exists.
    """
    return gw > max(1, start_event or 1)


def _compute_standings_movement(
    managers: list[RecapManagerEntry],
    league_rows: Sequence[tuple[int, int, int]] | None = None,
    *,
    use_net_points: bool = False,
    allow_standings_fallback: bool = True,
) -> None:
    """Derive previous league positions from total_points - net_gw_points.

    `league_rows` is (entry_id, total_points, gw_points) for every entry in
    league-standings order, including managers whose picks failed to fetch.
    `overall_rank` is assigned over that same full cohort, so ranking the
    previous table over survivors alone renumbers everyone below a missing
    manager and reports the whole tail as having moved.

    Only ever called for a gameweek with a predecessor (`_has_previous_gameweek`):
    on the league's first one every previous total is zero and the ranking
    below would hand every manager back their current position as though
    they had held it.

    Fetched managers keep their own points; standings values only fill in
    entries that could not be fetched -- and only when
    `allow_standings_fallback` says the standings describe the same point in
    time as the collected data, i.e. a live capture. On a replay they do not,
    so an entry with no point-in-time total makes the whole previous table
    underivable (mixing the two eras would rank a current-state total against
    point-in-time ones) and no `previous_rank` is set at all, matching
    _assign_point_in_time_positions. `total_points` is FPL's cumulative
    total, which is always net of every hit ever taken, so what gets
    subtracted from it must be net too: a fetched manager's `gw_points` is
    only net-of-hit when `use_net_points` is on, so `transfer_cost` is
    subtracted back out here when it's off -- otherwise a manager who took a
    hit this GW has their previous total over-credited by the hit amount.
    Standings-only rows have no hit data to correct with and are used as-is
    (an existing, unavoidable approximation for failed fetches).

    A manager with no point-in-time `total_points` (an unreconstructable
    draft replay, see U2) cannot be placed on the previous table at all --
    they are left out of the ranking entirely, and get no `previous_rank`
    rather than a fabricated one. This does not shift anyone else's position:
    the ranking is still over whoever *does* have a total.

    Mutates managers in-place to set previous_rank.
    """
    by_entry = {m["entry_id"]: m for m in managers}

    def _net_gw_points(m: RecapManagerEntry) -> int:
        return m["gw_points"] if use_net_points else m["gw_points"] - m["transfer_cost"]

    if league_rows is None:
        rows = [
            (m["entry_id"], m["total_points"], _net_gw_points(m))
            for m in managers
            if "total_points" in m
        ]
    else:
        rows = []
        for entry_id, total, gw_pts in league_rows:
            fetched = by_entry.get(entry_id)
            if fetched is not None and "total_points" in fetched:
                rows.append((entry_id, fetched["total_points"], _net_gw_points(fetched)))
            elif allow_standings_fallback:
                rows.append((entry_id, total, gw_pts))
            else:
                logger.warning(
                    "Standings movement unavailable: no point-in-time cumulative total "
                    "for entry %s, and the standings row belongs to a later gameweek.",
                    entry_id,
                )
                return

    prev_totals = [(entry_id, total - gw_pts) for entry_id, total, gw_pts in rows]
    # The same ranking helper `overall_rank` is derived through, not a local
    # re-derivation: movement is the difference between two tables, so both
    # have to be built the same way or the difference reports a move nobody
    # made. Ordinal numbering here against competition ranking there gave
    # managers level on points distinct previous places and a shared current
    # one, arrowing a manager up or down for a tie they never left (issue
    # #164 review).
    prev_rank_map = derive_point_in_time_positions(prev_totals)

    for m in managers:
        rank = prev_rank_map.get(m["entry_id"])
        if rank is None:
            rank = m.get("overall_rank")
        if rank is not None:
            m["previous_rank"] = rank


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


@dataclass(frozen=True)
class LeagueFinesRuling:
    """What one gameweek's fine evaluation produced, and who it covered.

    `ruled_manager_keys` is deliberately not "every manager passed in": a
    manager whose evaluation raised is absent from it, which is what stops
    their ledger row from recording an acquittal nothing actually ruled
    (issue #136).
    """

    fines: list[RecapFineResult]
    ruled_manager_keys: frozenset[int]


def configured_fine_rule_types(settings: dict[str, Any], format_name: str) -> list[str]:
    """The rule types `evaluate_league_fines` would rule on, in config order.

    Stamped onto every row a capture writes (`fine_rules_evaluated`), so a
    season tally reading the ledger back can tell "nobody was fined" apart
    from "nothing was ruled" -- an empty `fines` list says both (issue #136).
    An empty list here is itself a ruling: nothing is configured for this
    format, so no rule covers the gameweek.

    Gracefully returns an empty list when fines are unconfigured, matching
    `evaluate_league_fines`, so the two can never disagree about whether a
    gameweek was ruled. A `fines:` block that cannot be parsed is a different
    thing and raises `ConfigError` through to the command's
    `config_failure_boundary`: returning an empty list there would record
    "no rule was configured" for a gameweek whose rules were simply
    unreadable, which is the false acquittal the field exists to prevent.
    """
    fines_config = parse_fines_config(settings)
    if fines_config is None:
        return []
    return [rule.type for rule in rules_for_format(fines_config, format_name)]


def evaluate_league_fines(
    managers: list[RecapManagerEntry],
    settings: dict[str, Any],
    format_name: str,
) -> LeagueFinesRuling:
    """Rule the configured fines against each manager.

    Returns the triggered fines *and* the managers whose evaluation actually
    completed. The two are separate facts: a manager whose evaluation raised
    is dropped from `fines` silently, and stamping the configured rule list
    onto their ledger row anyway would record "every rule was ruled, none
    triggered" -- a false acquittal for a manager nothing was ruled against
    (issue #136). `ruled_manager_keys` is what lets the caller leave that row
    unstamped instead.

    An unconfigured or failing evaluation degrades to an empty ruling. A
    malformed `fines:` block does not: `parse_fines_config` raises
    `ConfigError` and the command's `config_failure_boundary` reports it,
    for the reason `configured_fine_rule_types` gives above.
    """
    fines_config = parse_fines_config(settings)
    if fines_config is None:
        # Every manager counts as ruled: there was no rule to run and so
        # nothing that could fail, which is the "ruled, nothing configured"
        # state the ledger records as an empty list rather than as silence.
        return LeagueFinesRuling(
            fines=[],
            ruled_manager_keys=frozenset(recap_manager_key(m) for m in managers),
        )

    use_net_points = settings.get("use_net_points", False)

    # Find the worst performer (lowest GW points) for last-place rule
    worst = min(managers, key=lambda m: m["gw_points"]) if managers else None

    triggered: list[RecapFineResult] = []
    ruled: set[int] = set()

    from fpl_cli.cli._fines import WorstPerformer

    for m in managers:
        try:

            worst_list: list[WorstPerformer] = []
            if worst:
                worst_list = [WorstPerformer(
                    # Keyed rather than compared on `entry_id`: every unclaimed
                    # draft team carries entry_id 0, so comparing on it fines
                    # all of them for one team's last place (KTD11).
                    is_user=recap_manager_key(m) == recap_manager_key(worst),
                    points=worst["gw_points"],
                    # `gross_points` is already gross whatever `use_net_points`
                    # is set to; `gw_points` flips. Adding the hit back to
                    # `gw_points` only reaches gross on the net side of that
                    # flip -- on the gross side it added the hit to a figure
                    # that never had it deducted, inflating the score a
                    # below-threshold rule is measured against (issue #136).
                    gross_points=worst["gross_points"],
                    name=worst["manager_name"],
                )]

            league_data = FinesLeagueData(
                user_gw_points=m["gross_points"],
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
                        manager_key=recap_manager_key(m),
                        rule_type=r.rule_type,
                        message=msg,
                    ))
            ruled.add(recap_manager_key(m))

        except Exception:  # noqa: BLE001 — best-effort enrichment
            # Warned, not debugged: this is the one signal that a rule
            # handler is broken, and the row it produces records silence
            # rather than an acquittal, so nothing downstream will ever
            # surface it either (issue #136).
            logger.warning(
                "Fines evaluation failed for %s; nothing is recorded as ruled for them",
                m["manager_name"], exc_info=True,
            )

    return LeagueFinesRuling(fines=triggered, ruled_manager_keys=frozenset(ruled))


# ---------------------------------------------------------------------------
# Awards (pure functions)
# ---------------------------------------------------------------------------


def _captain_detail(
    caps: list[RecapManagerEntry],
    total_managers: int = 0,
    *,
    points: int | None = None,
) -> str:
    """Build a detail string for tied captain awards, grouping by player.

    `points` is the value the tie was struck on. Pass it for worst-captain,
    where the tie is on *effective* captain points (the vice's score when the
    captain did not play) and so need not equal any tied manager's raw
    `captain_points`. Groups are capped at _DETAIL_CAP, as are the managers
    named inside one group, so a wide tie in a large league does not sprawl;
    the "## Captains" prompt section remains the full per-manager roster.

    A tie the whole league is in collapses further, to a single line counting
    each captain: nobody stood out, so there is no manager worth naming and
    the grouped block would be the entire roster (issue #145).
    """
    if len(caps) == 1:
        m = caps[0]
        if not m.get("captain_played"):
            vc_pts = m.get("vice_captain_points", 0)
            return (
                f"{m['manager_name']} captained {m['captain']} (dnp); "
                f"vice {m['vice_captain']} also scored {vc_pts} pts"
            )
        return f"{m['manager_name']} captained {m['captain']} ({m['captain_points']} pts)"

    from collections import defaultdict

    by_player: dict[str, list[str]] = defaultdict(list)
    # Whether the captain played is a property of the player, so it is uniform
    # within a group: managers tied here on a captain who blanked all reached
    # the tie value through their vice, not through the captain.
    played_by_player: dict[str, bool] = {}
    for m in caps:
        by_player[m["captain"]].append(m["manager_name"])
        played_by_player[m["captain"]] = bool(m.get("captain_played"))

    pts = caps[0]["captain_points"] if points is None else points
    groups = sorted(by_player.items(), key=lambda kv: (-len(kv[1]), kv[0]))

    # A league-wide tie is one fact about the gameweek, not an award: grouped,
    # it names every manager, and since Best and Worst then hold the same set
    # it named them all twice (issue #145). Collapse only once the roster is
    # longer than a capped block would be, so a small league keeps the prose
    # that already fits.
    if total_managers > _DETAIL_CAP and len(caps) == total_managers:
        picks = ", ".join(
            f"{player} ×{len(names)}" + ("" if played_by_player[player] else " (dnp)")
            for player, names in groups[:_DETAIL_CAP]
        )
        dropped_players = max(0, len(groups) - _DETAIL_CAP)
        unit = "pt" if pts == 1 else "pts"
        # Only a tie every captain played for can say the captains scored it.
        # A tie can mix captains who scored the value with captains who blanked
        # and had a vice reach it, and either verb would misdescribe one half of
        # it, so the neutral phrasing leaves the per-pick (dnp) marks to say
        # which route each group took.
        lead = (
            f"all {total_managers} captains scored {pts} {unit}"
            if all(played_by_player.values())
            else f"all {total_managers} captaincies were worth {pts} {unit}"
        )
        return (
            f"Captaincy was a wash — {lead}"
            f" ({picks})" + _omitted_suffix(dropped_players, "player")
        )

    omitted = sum(len(names) for _, names in groups[_DETAIL_CAP:])

    parts = []
    for player, names in groups[:_DETAIL_CAP]:
        n = len(names)
        # Naming the managers is the point of a narrow tie, but the same list
        # is roster-length when most of the league shares a captain, so it is
        # capped like the groups themselves.
        shown = names[:_DETAIL_CAP]
        extra = n - len(shown)
        if extra:
            joined = ", ".join(shown) + f" and {extra} other{'' if extra == 1 else 's'}"
        elif n > 1:
            joined = ", ".join(shown[:-1]) + " and " + shown[-1]
        else:
            joined = shown[0]
        verb = "all captained" if n > 2 else "captained"
        fraction = f" [{n} of {total_managers} managers]" if total_managers > 0 and n < total_managers else ""
        scored = f"({pts} pts)" if played_by_player[player] else f"(dnp; vice scored {pts})"
        parts.append(f"{joined} {verb} {player} {scored}{fraction}")

    detail = ", ".join(parts)
    return detail + _omitted_suffix(omitted, "manager")


def _compute_shared_awards(
    managers: list[RecapManagerEntry],
    format_name: str = "classic",
    *,
    total_managers: int | None = None,
) -> RecapAwards:
    """Compute awards common to both classic and draft, plus format-specific ones.

    `total_managers` is the full league size (including managers whose picks
    failed to fetch), used as the denominator in captain-tie fractions like
    "[9 of 20 managers]". Falls back to len(managers) when the caller doesn't
    have the full league size on hand (e.g. existing unit tests).
    """
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

    # Biggest bench haul — excludes Bench Boost managers (their bench counted).
    # Detect via per-player flag to avoid coupling to the display-form chip string
    # stored on RecapManagerEntry.active_chip (e.g. "BB" vs raw "bboost").
    bench_candidates = [
        m for m in managers
        if not any(p.get("is_bench_boost_player") for p in m.get("squad", []))
    ]
    if bench_candidates:
        best_bench_pts = max(m["bench_points"] for m in bench_candidates)
        if best_bench_pts > 0:
            bench_kings = [m for m in bench_candidates if m["bench_points"] == best_bench_pts]
            # Each entry is verbose, so a wide tie is capped the same way the
            # transfer and captain awards are. Everyone tied here benched the
            # same points, so name the managers it actually cost: leaving 12 on
            # the bench stings more on a 40-point week than an 80-point one.
            # Name is the secondary key so the choice stays deterministic.
            bench_kings.sort(key=lambda m: (m["gw_points"], m["manager_name"]))
            omitted = max(0, len(bench_kings) - _DETAIL_CAP)
            detail_parts = []
            for m in bench_kings[:_DETAIL_CAP]:
                bench_players = [
                    p for p in m["squad"]
                    if not p["contributed"] and not p["auto_sub_out"] and p["points"] > 0
                ]
                player_detail = ", ".join(f"{p['name']} ({p['points']})" for p in bench_players)
                detail_parts.append(
                    f"{m['manager_name']} left {m['bench_points']} pts on the bench"
                    f" (team scored {m['gw_points']} pts): {player_detail}"
                )
            detail = "; ".join(detail_parts) + _omitted_suffix(omitted, "manager")
            awards["biggest_bench_haul"] = RecapAwardEntry(
                manager_name=" and ".join(m["manager_name"] for m in bench_kings),
                value=best_bench_pts,
                detail=detail,
            )

    # Captain awards (classic only - draft has no captaincy)
    if format_name == "classic":
        league_size = total_managers if total_managers is not None else len(managers)

        # Effective captain pts: use vice's score if captain didn't play (VC takeover).
        # This correctly ranks a blanking VC below a played captain who scored 0.
        def _effective_cap_pts(m: RecapManagerEntry) -> int:
            return m["captain_points"] if m.get("captain_played") else m.get("vice_captain_points", 0)

        best_cap_pts = max(m["captain_points"] for m in managers)
        # Held as positions in `managers`, not managers: two managers can share
        # a display name, and comparing the two sets is what decides whether
        # the awards are one fact printed twice.
        best_positions = (
            {i for i, m in enumerate(managers) if m["captain_points"] == best_cap_pts}
            if best_cap_pts > 0
            else set()
        )
        worst_cap_pts = min(_effective_cap_pts(m) for m in managers)
        worst_positions = {
            i for i, m in enumerate(managers) if _effective_cap_pts(m) == worst_cap_pts
        }

        if best_positions:
            best_caps = [managers[i] for i in sorted(best_positions)]
            awards["best_captain"] = RecapAwardEntry(
                manager_name=" and ".join(m["manager_name"] for m in best_caps),
                value=best_cap_pts,
                detail=_captain_detail(best_caps, league_size, points=best_cap_pts),
            )

        # Best and Worst over the same managers render as two identical blocks
        # under opposite headings (issue #145). Everyone scored the same, so
        # neither reading is more true -- Best keeps it and Worst is dropped
        # rather than repeated. Best is absent when every captain scored 0, and
        # the sets differ then too, so Worst still carries the gameweek.
        if worst_positions != best_positions:
            worst_caps = [managers[i] for i in sorted(worst_positions)]
            awards["worst_captain"] = RecapAwardEntry(
                manager_name=" and ".join(m["manager_name"] for m in worst_caps),
                value=worst_cap_pts,
                detail=_captain_detail(worst_caps, league_size, points=worst_cap_pts),
            )

    # Format-specific awards
    if format_name == "classic":
        _compute_transfer_awards(managers, awards)
    elif format_name == "draft":
        _compute_waiver_awards(managers, awards)

    return awards


def _fmt_award_move(m: RecapTransfer | RecapDraftTransaction) -> str:
    return f"{m['player_in']} for {m['player_out']} ({m['net']:+d})"


def _format_award_detail(
    *,
    manager_name: str,
    moves: list[RecapTransfer] | list[RecapDraftTransaction],
    transfer_cost: int,
    side: Literal["genius", "disaster"],
    label_singular: str,
) -> str:
    """Format the detail string for a transfer/waiver genius or disaster award.

    Surfaces the top _DETAIL_CAP moves by net (for the side of the award),
    aggregate context (raw, hit, overall true_net), and an omitted-count tail
    when more moves exist than the cap. Uniform across transfer and waiver.
    """
    if not moves:
        raise ValueError("_format_award_detail requires at least one move")

    n = len(moves)
    raw = sum(m["net"] for m in moves)
    true_net = raw - transfer_cost

    if side == "genius":
        sorted_moves = sorted(moves, key=lambda m: m["net"], reverse=True)
        verb = "gained"
        magnitude = true_net
        lead = "Best"
    else:
        sorted_moves = sorted(moves, key=lambda m: m["net"])
        verb = "lost"
        magnitude = abs(true_net)
        lead = "Worst"

    plural = label_singular if n == 1 else f"{label_singular}s"
    if transfer_cost > 0:
        context = f" ({raw:+d} raw across {n} {plural}, -{transfer_cost} hit)"
    elif n > 1:
        context = f" ({raw:+d} raw across {n} {plural})"
    else:
        context = ""

    headline = f"{manager_name} {verb} {magnitude} net pts overall{context}."

    shown = sorted_moves[:_DETAIL_CAP]
    omitted = max(0, n - _DETAIL_CAP)

    # Disaster fired but no individual move was negative: the loss came from
    # the hit, not from any swap. Reframe so "Worst: X (+3)" doesn't read as
    # contradictory to the headline.
    if side == "disaster" and shown[0]["net"] >= 0:
        moves_part = "All swaps were profitable; the hit cost produced the loss"
        if len(shown) == 1:
            moves_part += f". Move: {_fmt_award_move(shown[0])}"
        else:
            move_strs = "; ".join(_fmt_award_move(m) for m in shown)
            moves_part += f". Moves: {move_strs}"
        moves_part += _omitted_suffix(omitted)
        moves_part += "."
        return f"{headline} {moves_part}"

    first = _fmt_award_move(shown[0])
    if len(shown) == 1:
        moves_part = f"{lead}: {first}"
    else:
        rest = ", ".join(_fmt_award_move(m) for m in shown[1:])
        moves_part = f"{lead}: {first}; also {rest}"
    moves_part += _omitted_suffix(omitted)
    moves_part += "."

    return f"{headline} {moves_part}"


def _compute_transfer_awards(
    managers: list[RecapManagerEntry],
    awards: RecapAwards,
) -> None:
    """Compute transfer genius/disaster awards for classic format.

    Aggregate true_net = sum(per-transfer net) - transfer_cost, so the award
    reconciles with the standings figure (which is also post-hit) rather than
    presenting a pre-hit gross swing.
    """
    managers_with_transfers = [m for m in managers if m.get("transfers")]

    if not managers_with_transfers:
        return

    def _transfer_net(m: RecapManagerEntry) -> int:
        return sum(t["net"] for t in m.get("transfers", [])) - m.get("transfer_cost", 0)

    genius = max(managers_with_transfers, key=_transfer_net)
    genius_net = _transfer_net(genius)
    if genius_net > 0:
        awards["transfer_genius"] = RecapAwardEntry(
            manager_name=genius["manager_name"],
            value=genius_net,
            detail=_format_award_detail(
                manager_name=genius["manager_name"],
                moves=list(genius.get("transfers", [])),
                transfer_cost=genius.get("transfer_cost", 0),
                side="genius",
                label_singular="transfer",
            ),
        )

    disaster = min(managers_with_transfers, key=_transfer_net)
    disaster_net = _transfer_net(disaster)
    if disaster_net < 0:
        awards["transfer_disaster"] = RecapAwardEntry(
            manager_name=disaster["manager_name"],
            value=disaster_net,
            detail=_format_award_detail(
                manager_name=disaster["manager_name"],
                moves=list(disaster.get("transfers", [])),
                transfer_cost=disaster.get("transfer_cost", 0),
                side="disaster",
                label_singular="transfer",
            ),
        )


def _contract_draft_txn_chains(
    txns: list[RecapDraftTransaction],
) -> list[RecapDraftTransaction]:
    """Collapse chain rebuilds within a single manager-GW into endpoint pairs.

    If a manager swaps A→B then B→C in the same GW, B is an intermediate and
    the effective move is A→C. Contracting these prevents the best/worst-move
    detail from naming an intermediate the manager didn't end the GW with.

    Sum of effective nets equals sum of raw nets (intermediates cancel
    algebraically). Closed loops (A→B then B→A) contract to nothing.
    """
    if len(txns) < 2:
        return list(txns)

    by_out = {t["player_out"]: t for t in txns}
    in_names = {t["player_in"] for t in txns}
    out_names = {t["player_out"] for t in txns}
    intermediates = in_names & out_names

    if not intermediates:
        return list(txns)

    contracted: list[RecapDraftTransaction] = []
    for start in txns:
        # Chain starts at a txn whose dropped player isn't also picked up
        # elsewhere in the same GW.
        if start["player_out"] in in_names:
            continue
        current = start
        while current["player_in"] in intermediates:
            current = by_out[current["player_in"]]
        if current is start:
            contracted.append(start)
        else:
            contracted.append(RecapDraftTransaction(
                player_in=current["player_in"],
                player_in_team=current["player_in_team"],
                player_in_team_name=current.get("player_in_team_name"),
                player_in_points=current["player_in_points"],
                player_out=start["player_out"],
                player_out_team=start["player_out_team"],
                player_out_team_name=start.get("player_out_team_name"),
                player_out_points=start["player_out_points"],
                net=current["player_in_points"] - start["player_out_points"],
                kind=current["kind"],
            ))
    return contracted


def _compute_waiver_awards(
    managers: list[RecapManagerEntry],
    awards: RecapAwards,
) -> None:
    """Compute waiver genius/disaster awards for draft format."""
    managers_with_txns = [m for m in managers if m.get("transactions")]

    if not managers_with_txns:
        return

    # Contract once per manager so ranking and display read the same list.
    effective_by_manager: dict[int, list[RecapDraftTransaction]] = {
        id(m): _contract_draft_txn_chains(m.get("transactions", []))
        for m in managers_with_txns
    }

    def _txn_net(m: RecapManagerEntry) -> int:
        return sum(t["net"] for t in effective_by_manager[id(m)])

    genius = max(managers_with_txns, key=_txn_net)
    genius_effective = effective_by_manager[id(genius)]
    genius_net = _txn_net(genius)
    if genius_net > 0 and genius_effective:
        awards["waiver_genius"] = RecapAwardEntry(
            manager_name=genius["manager_name"],
            value=genius_net,
            detail=_format_award_detail(
                manager_name=genius["manager_name"],
                moves=list(genius_effective),
                transfer_cost=0,
                side="genius",
                label_singular="waiver",
            ),
        )

    disaster = min(managers_with_txns, key=_txn_net)
    disaster_effective = effective_by_manager[id(disaster)]
    disaster_net = _txn_net(disaster)
    if disaster_net < 0 and disaster_effective:
        awards["waiver_disaster"] = RecapAwardEntry(
            manager_name=disaster["manager_name"],
            value=disaster_net,
            detail=_format_award_detail(
                manager_name=disaster["manager_name"],
                moves=list(disaster_effective),
                transfer_cost=0,
                side="disaster",
                label_singular="waiver",
            ),
        )


# ---------------------------------------------------------------------------
# Draft data collection
# ---------------------------------------------------------------------------


def _draft_manager_name(entry_info: dict[str, Any]) -> str:
    """Display name for one draft league entry, falling back to the team name.

    An unclaimed team has no player name at all, so the entry name is the only
    thing left to call it.
    """
    name = f"{entry_info.get('player_first_name', '')} {entry_info.get('player_last_name', '')}".strip()
    return name or entry_info.get("entry_name", "Unknown")


def _bucket_draft_txns_by_league_entry(
    gw_txns: list[dict[str, Any]],
    league_entries: list[dict[str, Any]],
) -> dict[int, list[dict[str, Any]]]:
    """Bucket draft transactions by league_entry id.

    The draft txn payload's `entry` field is the FPL `entry_id` (the manager's
    site-wide id), not the league-local `id`. The rest of the recap pipeline
    keys on league_entry `id`, so remap before bucketing — otherwise managers
    whose `entry_id != id` have their transactions silently dropped.
    """
    entry_id_to_le_id: dict[int, int] = {
        e["entry_id"]: e["id"]
        for e in league_entries
        if e.get("entry_id") is not None and e.get("id") is not None
    }
    out: dict[int, list[dict[str, Any]]] = {}
    for txn in gw_txns:
        txn_entry_id = txn.get("entry")
        if txn_entry_id is None:
            continue
        le_id = entry_id_to_le_id.get(txn_entry_id)
        if le_id is None:
            continue
        out.setdefault(le_id, []).append(txn)
    return out


async def collect_draft_recap_data(
    settings: dict[str, Any],
    gw: int,
    live_stats: dict[int, dict[str, Any]],
    players: list[Player],
    teams: dict[int, Team],
    *,
    is_live_gw: bool = True,
    bgw_team_ids: frozenset[int] = frozenset(),
    players_with_fixture: frozenset[int] | None = None,
) -> LeagueRecapData:
    """Fetch all managers' draft picks and compute league-wide recap data.

    Draft has no per-manager history endpoint (KTD2), so GW points are always
    recomputed from the recorded squad against live stats rather than read
    off the league's always-current standings -- and, when `is_live_gw`,
    reconciled against those standings as the only independent check the
    reconstruction is right (RecapReconciliationError on divergence). The
    cumulative total can only be trusted from the standings for a live
    capture; a replayed gameweek leaves it unset (R10) until a ledger exists
    to sum it from (U6, not built yet).
    """
    from fpl_cli.api.fpl_draft import FPLDraftClient, match_draft_to_main
    from fpl_cli.models.player import POSITION_MAP

    draft_league_id: Any = fpl_config(settings).get("draft_league_id")

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

        matched_main_players = match_draft_to_main(draft_elements, players)
        draft_to_main_id = {
            draft_id: main_player.id
            for draft_id, main_player in matched_main_players.items()
        }
        # The stable cross-season code, resolved through the same match. A
        # draft id with no entry here is exactly the unmatched case, so the
        # recorded squad carries a null code alongside its unmatched marker
        # rather than a seasonal id that means nothing next year (R6).
        draft_to_main_code = {
            draft_id: main_player.code
            for draft_id, main_player in matched_main_players.items()
            if main_player.code
        }

        # Fetch all transactions for the league, filter to this GW
        txn_response = await draft_client.get_league_transactions(draft_league_id)
        all_txns: list[dict[str, Any]] = txn_response.get("transactions", [])
        gw_txns = [
            t for t in all_txns
            if t.get("event") == gw and t.get("result") == "a"
        ]
        txns_by_entry = _bucket_draft_txns_by_league_entry(gw_txns, league_entries)

        # Fetch picks for each manager
        sem = asyncio.Semaphore(_PICKS_CONCURRENCY)
        managers: list[RecapManagerEntry] = []

        async def _fetch_draft_manager(standing: dict[str, Any], rank: int) -> RecapManagerEntry | None:
            league_entry_id: int = standing.get("league_entry", 0)
            entry_info = entry_map.get(league_entry_id, {})
            entry_id = entry_info.get("entry_id")
            manager_name = _draft_manager_name(entry_info)

            standings_gw_pts: int = standing.get("event_total", 0)
            standings_total: int = standing.get("total", 0)

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
            computed_gw_points = 0

            for pick in picks:
                draft_elem_id = pick.get("element")
                draft_player = draft_player_map.get(draft_elem_id)
                if not draft_player:
                    continue

                main_id = draft_to_main_id.get(draft_elem_id)
                unmatched = main_id is None
                pts, _, red_cards = _live_player_stats(live_stats, main_id)
                pos_name = POSITION_MAP.get(draft_player.get("element_type"), "???")
                draft_team = teams.get(draft_player.get("team"))
                team_short = draft_team.short_name if draft_team else "???"
                squad_position = pick.get("position", 1)
                is_bench = squad_position > 11
                contributed = not is_bench
                if draft_elem_id in auto_sub_in_ids:
                    contributed = True

                if is_bench and draft_elem_id not in auto_sub_in_ids:
                    bench_points += pts
                if contributed:
                    computed_gw_points += pts

                squad.append(RecapManagerPlayer(
                    name=draft_player.get("web_name", "Unknown"),
                    team=team_short,
                    team_name=draft_team.name if draft_team else None,
                    position=pos_name,
                    code=draft_to_main_code.get(draft_elem_id),
                    points=pts,
                    is_captain=False,
                    is_vice_captain=False,
                    contributed=contributed,
                    is_bench_boost_player=False,
                    auto_sub_in=draft_elem_id in auto_sub_in_ids,
                    auto_sub_out=draft_elem_id in auto_sub_out_ids,
                    red_cards=red_cards,
                    unmatched=unmatched,
                    had_fixture=had_fixture(
                        main_id, draft_player.get("team"),
                        players_with_fixture=players_with_fixture,
                        bgw_team_ids=bgw_team_ids,
                    ),
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

            # Build transaction data for this manager. Both waivers and free
            # agents always carry an element_out — draft squads are fixed at 15,
            # so any add requires a simultaneous drop.
            manager_txns: list[RecapDraftTransaction] = []
            for txn in txns_by_entry.get(league_entry_id, []):
                pin_id: int | None = txn.get("element_in")
                pout_id: int | None = txn.get("element_out")
                dp_in = draft_player_map.get(pin_id) if pin_id else None
                dp_out = draft_player_map.get(pout_id) if pout_id else None

                if not dp_in or not dp_out:
                    logger.warning(
                        "Skipping malformed draft txn (entry=%s in=%s out=%s)",
                        txn.get("entry"), pin_id, pout_id,
                    )
                    continue

                main_in_id = draft_to_main_id.get(pin_id) if pin_id else None
                in_pts, _, _ = _live_player_stats(live_stats, main_in_id)
                main_out_id = draft_to_main_id.get(pout_id) if pout_id else None
                out_pts, _, _ = _live_player_stats(live_stats, main_out_id)

                in_club = teams.get(dp_in.get("team"))
                out_club = teams.get(dp_out.get("team"))

                transaction = RecapDraftTransaction(
                    player_in=dp_in.get("web_name", "Unknown"),
                    player_in_team=in_club.short_name if in_club else "???",
                    player_in_team_name=in_club.name if in_club else None,
                    player_in_points=in_pts,
                    player_out=dp_out.get("web_name", "Unknown"),
                    player_out_team=out_club.short_name if out_club else "???",
                    player_out_team_name=out_club.name if out_club else None,
                    player_out_points=out_pts,
                    net=in_pts - out_pts,
                    kind=txn.get("kind", "w"),
                )
                if pin_id is not None and (in_code := draft_to_main_code.get(pin_id)):
                    transaction["player_in_code"] = in_code
                if pout_id is not None and (out_code := draft_to_main_code.get(pout_id)):
                    transaction["player_out_code"] = out_code
                manager_txns.append(transaction)

            gw_points = computed_gw_points
            if is_live_gw and computed_gw_points != standings_gw_pts:
                unmatched_names = [p["name"] for p in squad if p["unmatched"]]
                if unmatched_names:
                    # A draft player whose (web_name, team) match to a main
                    # player failed scores zero, so the sum is short by
                    # construction -- a known gap in the mapping, not the
                    # wrong field. On a live gameweek the standings describe
                    # this same gameweek, so they are the better number to
                    # show; the squad rows keep their unmatched markers.
                    logger.warning(
                        "Draft gameweek points for %s could not be fully reconstructed "
                        "(%d unmatched player(s): %s); using the standings total (%s) "
                        "instead of the short computed sum (%s).",
                        manager_name, len(unmatched_names), ", ".join(unmatched_names),
                        standings_gw_pts, computed_gw_points,
                    )
                    gw_points = standings_gw_pts
                else:
                    raise RecapReconciliationError(
                        f"Gameweek-points reconciliation failed for the live gameweek for "
                        f"{manager_name}: computed={computed_gw_points} standings={standings_gw_pts}. "
                        "The draft point-in-time reconstruction is likely wrong -- every row "
                        "derived from it would be wrong too."
                    )

            result = RecapManagerEntry(
                manager_name=manager_name,
                entry_id=entry_id or 0,
                league_entry_id=league_entry_id,
                gw_points=gw_points,
                gross_points=gw_points,
                gw_rank=rank,
                captain=captain_name,
                captain_points=captain_points,
                captain_played=False,
                vice_captain=vice_captain_name,
                vice_captain_points=0,
                active_chip=None,
                squad=squad,
                bench_points=bench_points,
                transfer_cost=0,
                auto_subs=auto_sub_descs,
                transactions=manager_txns,
            )
            if is_live_gw:
                result["total_points"] = standings_total
                # On a live capture the standings *are* the point in time, so
                # the league's own rank is better than one re-derived from
                # `total`: draft h2h leagues score league points, where ties
                # are common and the API breaks them on points-for. Sorting
                # `total` alone would discard that and reorder tied managers
                # arbitrarily between runs.
                api_rank = standing.get("rank")
                if isinstance(api_rank, int):
                    result["overall_rank"] = api_rank
                # Only where a previous gameweek exists to have stood in:
                # the API reports `last_rank` from the league's first
                # gameweek onward, and a zero there is its "no previous
                # table" sentinel rather than a position anyone held.
                api_last_rank = standing.get("last_rank")
                if (
                    isinstance(api_last_rank, int)
                    and api_last_rank > 0
                    and _has_previous_gameweek(gw)
                ):
                    result["previous_rank"] = api_last_rank
            return result

        tasks = [_fetch_draft_manager(s, i + 1) for i, s in enumerate(standings)]
        results = await asyncio.gather(*tasks)

        managers = [m for m in results if m is not None]
        managers.sort(key=lambda m: -m["gw_points"])
        for i, m in enumerate(managers):
            m["gw_rank"] = i + 1

        # A live capture already carries the league's own rank (set above).
        # A replay has no cumulative total to rank on at all -- draft has no
        # ledger in Phase A -- so position stays unavailable rather than
        # derived from the always-current standings order.
        league_rows = [
            (
                entry_map.get(s.get("league_entry"), {}).get("entry_id") or 0,
                s.get("total", 0),
                s.get("event_total", 0),
            )
            for s in standings
        ]
        cohort: list[RecapStandingsEntry] = [
            RecapStandingsEntry(
                manager_key=s.get("league_entry", 0),
                manager_name=_draft_manager_name(entry_map.get(s.get("league_entry"), {})),
                entry_id=entry_map.get(s.get("league_entry"), {}).get("entry_id"),
                gw_points=s.get("event_total", 0),
                total_points=s.get("total", 0),
            )
            for s in standings
        ]
        # The league's own last_rank already states where everyone stood, and
        # states it correctly for h2h scoring, which subtracting gameweek
        # points from a league-points total cannot. Only re-derive movement
        # when the API did not supply it for the whole cohort.
        api_movement = is_live_gw and all(
            isinstance(s.get("rank"), int) and isinstance(s.get("last_rank"), int)
            for s in standings
        )

    if not api_movement and _has_previous_gameweek(gw):
        _compute_standings_movement(managers, league_rows, allow_standings_fallback=is_live_gw)
    awards = _compute_shared_awards(managers, format_name="draft", total_managers=len(standings))

    return LeagueRecapData(
        gameweek=gw,
        league_name=league_name,
        fpl_format="draft",
        managers=managers,
        awards=awards,
        league_id=draft_league_id,
        standings_cohort=cohort,
        # `league_entries` is the league's own member list, so a standings
        # response shorter than it is a real truncation rather than an
        # unknown-size page.
        standings_truncated=len(cohort) < len(league_entries),
        league_size=len(league_entries),
    )
