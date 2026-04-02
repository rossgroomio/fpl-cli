"""FPL status dashboard: context-aware GW state, deadlines, and personalised info."""
# Pattern: direct-api

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any

import click
import httpx
from rich.panel import Panel

from fpl_cli.cli._context import Format, console, get_format, is_custom_analysis_enabled, load_settings
from fpl_cli.cli._fines import FinesLeagueData, FinesTeamPlayer, evaluate_fines
from fpl_cli.cli._fines_config import FinesConfig, parse_fines_config
from fpl_cli.cli._json import emit_json, output_format_option
from fpl_cli.cli.chips import CHIP_NAMES
from fpl_cli.models.chip_plan import ChipPlan, ChipType, UsedChip
from fpl_cli.models.player import Player, PlayerStatus

if TYPE_CHECKING:
    from fpl_cli.api.fpl import FPLClient
    from fpl_cli.api.fpl_draft import FPLDraftClient

logger = logging.getLogger(__name__)

_ORDINAL_SUFFIXES = {1: "st", 2: "nd", 3: "rd"}


def _countdown(deadline_str: str) -> str:
    """Format a deadline string into a human-readable countdown."""
    try:
        deadline = datetime.fromisoformat(deadline_str.replace("Z", "+00:00"))
        now = datetime.now(timezone.utc)
        delta = deadline - now
        if delta.total_seconds() < 0:
            return "passed"
        days = delta.days
        hours, remainder = divmod(delta.seconds, 3600)
        minutes = remainder // 60
        parts = []
        if days:
            parts.append(f"{days}d")
        if hours:
            parts.append(f"{hours}h")
        if not days:
            parts.append(f"{minutes}m")
        return " ".join(parts)
    except (ValueError, TypeError):
        return deadline_str



def _ordinal(n: int | str) -> str:
    """Convert number to ordinal string (1st, 2nd, 3rd, etc.)."""
    if isinstance(n, str):
        return n
    if 11 <= n % 100 <= 13:
        return f"{n}th"
    return f"{n}{_ORDINAL_SUFFIXES.get(n % 10, 'th')}"


def _gw_rank(standings: list[dict[str, Any]], user_event_total: int) -> int:
    """Derive GW rank by counting entries with higher event_total."""
    return sum(1 for s in standings if s.get("event_total", 0) > user_event_total) + 1




def _build_fines_context(
    standings_sorted_asc: list[dict[str, Any]],
    user_is_last: bool,
    user_gw_pts: int,
    pick_ids: list[int] | None,
    live_data: dict[str, Any] | None,
    player_names: dict[int, str] | None,
) -> tuple[FinesLeagueData, list[FinesTeamPlayer]]:
    """Build minimal league_data and team_data for evaluate_fines()."""
    bottom = standings_sorted_asc[0] if standings_sorted_asc else {}
    bottom_pts = bottom.get("event_total", 0)
    bottom_name = bottom.get("player_name", bottom.get("entry_name", "Unknown"))

    league_data: FinesLeagueData = {
        "user_gw_points": user_gw_pts,
        "worst_performers": [{
            "is_user": user_is_last,
            "points": bottom_pts,
            "gross_points": bottom_pts,
            "name": bottom_name,
        }],
    }

    team_data: list[FinesTeamPlayer] = []
    if pick_ids and live_data:
        live_map = {e["id"]: e.get("stats", {}) for e in live_data.get("elements", [])}
        names = player_names or {}
        for i, pid in enumerate(pick_ids):
            stats = live_map.get(pid, {})
            team_data.append({
                "name": names.get(pid, f"Player {pid}"),
                "red_cards": stats.get("red_cards", 0),
                "contributed": i < 11,
                "auto_sub_out": False,
            })

    return league_data, team_data




@click.command("status")
@output_format_option
@click.pass_context
def status_command(ctx: click.Context, output_format: str) -> None:
    """Show FPL gameweek status and upcoming deadlines."""
    fmt = get_format(ctx)
    show_classic = fmt != Format.DRAFT
    show_draft = fmt != Format.CLASSIC
    is_both = show_classic and show_draft

    async def _run() -> None:
        from fpl_cli.api.fpl import FPLClient as _FPLClient

        settings = load_settings()
        fpl_cfg = settings.get("fpl", {})
        entry_id = fpl_cfg.get("classic_entry_id")
        draft_entry_id = fpl_cfg.get("draft_entry_id")
        draft_league_id = fpl_cfg.get("draft_league_id")
        classic_league_id = fpl_cfg.get("classic_league_id")

        async with _FPLClient() as client:
            current_gw = await client.get_current_gameweek()
            next_gw = await client.get_next_gameweek()

            # --- JSON early return ---
            if output_format == "json":
                gw_info: dict[str, Any] = {
                    "current_gw": current_gw["id"] if current_gw else None,
                    "finished": current_gw.get("finished", False) if current_gw else None,
                    "next_gw": next_gw["id"] if next_gw else None,
                    "deadline": next_gw.get("deadline_time") if next_gw else None,
                }
                json_data: dict[str, Any] = {"gameweek_info": gw_info}

                if fmt is None:
                    emit_json("status", json_data, metadata={
                        "gameweek": current_gw["id"] if current_gw else None,
                        "format": None,
                    })
                    return

                fines_cfg = parse_fines_config(settings)

                if show_classic and entry_id:
                    try:
                        classic_data = await _fetch_classic_data(
                            client, entry_id, classic_league_id, current_gw, next_gw, fines_cfg,
                            use_net_points=settings.get("use_net_points", False),
                        )
                        json_data["classic"] = classic_data
                    except (httpx.HTTPError, KeyError, TypeError):
                        pass

                if show_draft and draft_entry_id:
                    try:
                        from fpl_cli.agents.common import get_draft_squad_players
                        from fpl_cli.api.fpl_draft import FPLDraftClient as _FPLDraftClient

                        async with _FPLDraftClient() as draft_client:
                            draft_data = await _fetch_draft_data(
                                client, draft_client, get_draft_squad_players,
                                draft_entry_id, draft_league_id, current_gw, next_gw, fines_cfg,
                            )
                        json_data["draft"] = draft_data
                    except (httpx.HTTPError, KeyError, TypeError):
                        pass

                has_classic = "classic" in json_data
                has_draft = "draft" in json_data
                format_str = (
                    "both" if has_classic and has_draft
                    else ("draft" if has_draft else ("classic" if has_classic else None))
                )
                emit_json("status", json_data, metadata={
                    "gameweek": current_gw["id"] if current_gw else None,
                    "format": format_str,
                })
                return

            # --- Table output ---
            console.print(Panel.fit("[bold blue]FPL Status[/bold blue]"))

            # --- Always: GW state + deadline ---
            if current_gw:
                gw_id = current_gw["id"]
                finished = current_gw.get("finished", False)
                state = "[green]Finished[/green]" if finished else "[yellow]In Progress[/yellow]"
                console.print(f"\n[bold]Gameweek {gw_id}[/bold] - {state}")

            if next_gw:
                deadline = next_gw.get("deadline_time", "")
                countdown = _countdown(deadline) if deadline else "Unknown"
                console.print(f"[bold]Next Deadline:[/bold] GW{next_gw['id']} in [cyan]{countdown}[/cyan]")
                if deadline:
                    console.print(f"  [dim]{deadline}[/dim]")

            # No format detected - no entry IDs configured
            if fmt is None:
                console.print("\n[dim]Configure entry ID in settings.yaml for personalised data[/dim]")
                return

            fines_cfg = parse_fines_config(settings)

            # --- Classic section ---
            if show_classic:
                if is_both:
                    console.print("\n[bold cyan]# Classic[/bold cyan]")
                if not entry_id:
                    console.print("\n[dim]Configure classic_entry_id in settings.yaml for personalised data[/dim]")
                else:
                    try:
                        await _classic_section(
                            client, entry_id, classic_league_id, current_gw, next_gw, fines_cfg,
                            use_net_points=settings.get("use_net_points", False),
                        )
                    except (httpx.HTTPError, KeyError, TypeError) as exc:
                        logger.debug("Failed to fetch classic data: %s", exc)
                        console.print("[dim]Could not load classic data[/dim]")

            # --- Separator ---
            if is_both:
                console.print("\n" + "-" * 50)

            # --- Draft section ---
            if show_draft:
                if is_both:
                    console.print("\n[bold cyan]# Draft[/bold cyan]")
                if not draft_entry_id:
                    console.print("\n[dim]Configure draft_entry_id in settings.yaml for personalised data[/dim]")
                else:
                    from fpl_cli.agents.common import get_draft_squad_players
                    from fpl_cli.api.fpl_draft import FPLDraftClient as _FPLDraftClient

                    try:
                        async with _FPLDraftClient() as draft_client:
                            await _draft_section(
                                client, draft_client, get_draft_squad_players,
                                draft_entry_id, draft_league_id, current_gw, next_gw,
                                fines_cfg,
                            )
                    except (httpx.HTTPError, KeyError, TypeError) as exc:
                        logger.debug("Failed to fetch draft data: %s", exc)
                        console.print("[dim]Could not load draft data[/dim]")

            # Discovery note for custom analysis (R10)
            if not is_custom_analysis_enabled(settings):
                console.print(
                    "\n[dim]Custom analysis features (captain, targets, etc.) "
                    "available — run `fpl init` to enable[/dim]"
                )

    asyncio.run(_run())


async def _fetch_classic_data(
    client: FPLClient,
    entry_id: int,
    classic_league_id: int | None,
    current_gw: dict[str, Any] | None,
    next_gw: dict[str, Any] | None,
    fines_config: FinesConfig | None = None,
    *,
    use_net_points: bool = False,
) -> dict[str, Any]:
    """Fetch and compute classic personalised data, returned as a structured dict.

    Keys present only when the relevant data is available:
    - gw_result, league_standing, fines, pre_deadline, flagged_players
    """
    data: dict[str, Any] = {}
    gw_for_picks = current_gw["id"] if current_gw else 1

    history_data, manager_data, picks_data, all_players, all_teams = await asyncio.gather(
        client.get_manager_history(entry_id),
        client.get_manager_entry(entry_id),
        client.get_manager_picks(entry_id, gw_for_picks),
        client.get_players(),
        client.get_teams(),
    )

    # --- Post-GW: points, rank movement, league standing ---
    if current_gw and current_gw.get("finished"):
        gw_history = history_data.get("current", [])
        completed_gw = next(
            (gw for gw in gw_history if gw["event"] == current_gw["id"]),
            None,
        )
        if completed_gw:
            pts = completed_gw.get("points", 0)
            overall_rank = completed_gw.get("overall_rank", 0)
            prev_gw = next(
                (gw for gw in gw_history if gw["event"] == current_gw["id"] - 1),
                None,
            )
            gw_result: dict[str, Any] = {
                "points": pts,
                "overall_rank": overall_rank,
            }
            if prev_gw:
                gw_result["rank_change"] = prev_gw.get("overall_rank", 0) - overall_rank
            data["gw_result"] = gw_result

        # Classic league standing (separate error boundary)
        classic_standings: list[dict[str, Any]] = []
        classic_user_gw_pts = completed_gw.get("points", 0) if completed_gw else 0
        if classic_league_id:
            try:
                standings_data = await client.get_classic_league_standings(classic_league_id)
                classic_standings = standings_data.get("standings", {}).get("results", [])
                user_entry = next(
                    (e for e in classic_standings if e.get("entry") == entry_id), None
                )
                if user_entry:
                    classic_user_gw_pts = user_entry.get("event_total", 0)
                    gw_pos = _gw_rank(classic_standings, classic_user_gw_pts)
                    data["league_standing"] = {
                        "rank": user_entry.get("rank", "?"),
                        "gw_pts": classic_user_gw_pts,
                        "gw_position": gw_pos,
                        "league_size": len(classic_standings),
                    }
                elif classic_standings:
                    data["league_standing"] = {"not_in_top_50": True}
            except (httpx.HTTPError, KeyError, TypeError) as exc:
                logger.debug("Failed to fetch classic league standings: %s", exc)

        # --- Fines evaluation (own error boundary) ---
        if fines_config and fines_config.classic:
            rules = fines_config.classic
            sorted_standings = sorted(classic_standings, key=lambda s: s.get("event_total", 0))
            user_is_last = bool(sorted_standings and sorted_standings[0].get("entry") == entry_id)

            live_data = None
            player_names: dict[int, str] | None = None
            if any(r.type == "red-card" for r in rules):
                try:
                    live_data = await client.get_gameweek_live(current_gw["id"])
                except (httpx.HTTPError, KeyError, TypeError):
                    logger.debug("Failed to fetch live GW data for fines", exc_info=True)
                player_names = {p.id: p.web_name for p in all_players}

            all_picks_list = picks_data.get("picks", [])
            pick_id_list = [p["element"] for p in all_picks_list] if all_picks_list else None

            league_data, team_data = _build_fines_context(
                sorted_standings, user_is_last, classic_user_gw_pts,
                pick_id_list, live_data, player_names,
            )

            close_margin = False
            if use_net_points and any(r.type == "last-place" for r in rules) and len(sorted_standings) >= 2:
                gap = sorted_standings[1].get("event_total", 0) - sorted_standings[0].get("event_total", 0)
                close_margin = gap <= 4

            try:
                results = evaluate_fines(fines_config, "classic", league_data, team_data, use_net_points=False)
                triggered = [r for r in results if r.triggered]
                if triggered:
                    fines_list = []
                    for r in triggered:
                        msg = r.message.removeprefix("FINE TRIGGERED: ")
                        suffix = "*" if close_margin and r.rule_type == "last-place" else ""
                        fines_list.append({
                            "rule_type": r.rule_type,
                            "message": msg,
                            "close_margin_asterisk": bool(suffix),
                        })
                    data["fines"] = fines_list
            except Exception:  # noqa: BLE001 — best-effort enrichment
                logger.debug("Fines evaluation failed in _fetch_classic_data", exc_info=True)

    # --- Pre-deadline: bank, chips ---
    if next_gw:
        bank = manager_data.get("last_deadline_bank", 0) / 10
        gw_now = current_gw["id"] if current_gw else 1
        valid_chips = {c.value for c in ChipType}
        used = [
            UsedChip(chip=ChipType(name), gameweek=event)
            for c in history_data.get("chips", [])
            if (name := c.get("name", "").lower()) in valid_chips
            and (event := c.get("event", 0))
        ]
        transient = ChipPlan(chips_used=used)
        remaining = [
            CHIP_NAMES.get(c.value, c.value)
            for c in transient.get_available_chips(gw_now)
        ]
        pre_deadline: dict[str, Any] = {"bank": bank}
        if remaining:
            pre_deadline["available_chips"] = remaining
        data["pre_deadline"] = pre_deadline

    # --- Flagged players (full 15, bench dimmed) ---
    all_picks = picks_data.get("picks", [])
    if all_picks:
        pick_ids = [p["element"] for p in all_picks]
        bench_ids = {p["element"] for p in all_picks[11:]}
        player_map = {p.id: p for p in all_players}
        teams = {t.id: t.short_name for t in all_teams}
        flagged = [
            player_map[pid] for pid in pick_ids
            if pid in player_map and player_map[pid].status != PlayerStatus.AVAILABLE
        ]
        if flagged:
            data["flagged_players"] = [
                {
                    "name": p.web_name,
                    "team": teams.get(p.team_id, "???"),
                    "chance_of_playing": p.chance_of_playing_next_round,
                    "news": p.news or None,
                    "on_bench": p.id in bench_ids,
                }
                for p in flagged
            ]

    return data


async def _classic_section(
    client: FPLClient,
    entry_id: int,
    classic_league_id: int | None,
    current_gw: dict[str, Any] | None,
    next_gw: dict[str, Any] | None,
    fines_config: FinesConfig | None = None,
    *,
    use_net_points: bool = False,
) -> None:
    """Render classic personalised data: post-GW results, pre-deadline info, flagged players."""
    data = await _fetch_classic_data(
        client, entry_id, classic_league_id, current_gw, next_gw, fines_config,
        use_net_points=use_net_points,
    )

    # --- Post-GW: points, rank movement, league standing ---
    if current_gw and current_gw.get("finished"):
        gw_result = data.get("gw_result")
        if gw_result:
            pts = gw_result["points"]
            overall_rank = gw_result["overall_rank"]
            console.print(f"\n[bold]GW{current_gw['id']} Result:[/bold]")
            console.print(f"  Points: [bold]{pts}[/bold]")
            if "rank_change" in gw_result:
                diff = gw_result["rank_change"]
                arrow = "[green]\u2191[/green]" if diff > 0 else "[red]\u2193[/red]" if diff < 0 else "-"
                console.print(f"  Overall Rank: {overall_rank:,} ({arrow} {abs(diff):,})")
            else:
                console.print(f"  Overall Rank: {overall_rank:,}")

        league = data.get("league_standing")
        if league:
            if league.get("not_in_top_50"):
                console.print("  [dim]Not in top 50 - run `fpl league` for full standings[/dim]")
            else:
                console.print(
                    f"  League: {league['gw_pts']} pts"
                    f" ({_ordinal(league['gw_position'])} of {league['league_size']} this week)"
                    f" | {_ordinal(league['rank'])} overall"
                )

        # --- Fines rendering ---
        fines = data.get("fines", [])
        for f in fines:
            suffix = "*" if f.get("close_margin_asterisk") else ""
            console.print(f"  [yellow]\u26a0 Fine: {f['message']}{suffix}[/yellow]")
        if any(f.get("close_margin_asterisk") for f in fines):
            console.print(
                "  [dim]* Based on gross pts; net pts may differ"
                " - run fpl review for authoritative result[/dim]"
            )

    # --- Pre-deadline: bank, chips ---
    if "pre_deadline" in data:
        bank = data["pre_deadline"]["bank"]
        console.print("\n[bold]Pre-Deadline Info:[/bold]")
        console.print(f"  Bank: \u00a3{bank:.1f}m")
        chips = data["pre_deadline"].get("available_chips")
        if chips:
            console.print(f"  Chips: {', '.join(chips)}")

    # --- Flagged players (full 15, bench dimmed) ---
    flagged_players = data.get("flagged_players", [])
    if flagged_players:
        console.print("\n[bold red]Flagged Players:[/bold red]")
        for p in flagged_players:
            chance = f"{p['chance_of_playing']}%" if p["chance_of_playing"] is not None else "?"
            on_bench = p["on_bench"]
            line = f"  - {p['name']} ({p['team']}): {chance}{' (bench)' if on_bench else ''}"
            console.print(f"  [dim]{line}[/dim]" if on_bench else line)
            if p["news"]:
                console.print(f"    [dim]{p['news']}[/dim]")


async def _fetch_draft_data(
    client: FPLClient,
    draft_client: FPLDraftClient,
    get_draft_squad_fn: Callable[..., Awaitable[list[Player]]],
    draft_entry_id: int,
    draft_league_id: int | None,
    current_gw: dict[str, Any] | None,
    next_gw: dict[str, Any] | None,
    fines_config: FinesConfig | None = None,
) -> dict[str, Any]:
    """Fetch and compute draft personalised data, returned as a structured dict.

    Keys present only when the relevant data is available:
    - league_standing, fines, waiver_info, flagged_players
    """
    data: dict[str, Any] = {}
    gw_for_picks = current_gw["id"] if current_gw else 1
    all_players = await client.get_players()

    # Gather all independent draft API calls
    coros: list[Awaitable[Any]] = []
    if draft_league_id:
        coros.append(draft_client.get_league_details(draft_league_id))
        coros.append(draft_client.get_game_state())
    coros.append(get_draft_squad_fn(
        draft_client, all_players, draft_entry_id, gw_for_picks,
    ))

    results = await asyncio.gather(*coros)
    if draft_league_id:
        league_details: dict[str, Any] = results[0]
        game_state: dict[str, Any] = results[1]
        squad_players: list[Player] = results[2]
    else:
        league_details = {}
        game_state = {}
        squad_players = results[0]

    # --- Draft post-GW standings ---
    if current_gw and current_gw.get("finished") and league_details:
        standings = league_details.get("standings", [])
        league_entries = league_details.get("league_entries", [])
        entry_map: dict[int, dict[str, Any]] = {e.get("id"): e for e in league_entries}

        user_standing = None
        for s in standings:
            entry_info = entry_map.get(s.get("league_entry"))
            if entry_info and entry_info.get("entry_id") == draft_entry_id:
                user_standing = s
                break

        if user_standing:
            user_gw_pts = user_standing.get("event_total", 0)
            gw_pos = _gw_rank(standings, user_gw_pts)
            data["league_standing"] = {
                "rank": user_standing.get("rank", "?"),
                "gw_pts": user_gw_pts,
                "gw_position": gw_pos,
                "league_size": len(standings),
            }

        # --- Draft fines evaluation ---
        if fines_config and fines_config.draft:
            rules = fines_config.draft
            sorted_standings = sorted(standings, key=lambda s: s.get("event_total", 0))

            bottom_entry_info = entry_map.get(sorted_standings[0].get("league_entry")) if sorted_standings else None
            user_is_last = bool(bottom_entry_info and bottom_entry_info.get("entry_id") == draft_entry_id)
            draft_user_gw_pts = user_standing.get("event_total", 0) if user_standing else 0

            live_data = None
            if any(r.type == "red-card" for r in rules):
                try:
                    live_data = await client.get_gameweek_live(current_gw["id"])
                except (httpx.HTTPError, KeyError, TypeError):
                    logger.debug("Failed to fetch live GW data for draft fines", exc_info=True)

            pick_id_list = [p.id for p in squad_players] if squad_players else None
            draft_player_names = {p.id: p.web_name for p in squad_players} if squad_players else None

            league_data, team_data = _build_fines_context(
                sorted_standings, user_is_last, draft_user_gw_pts,
                pick_id_list, live_data, draft_player_names,
            )

            try:
                fines_results = evaluate_fines(fines_config, "draft", league_data, team_data, use_net_points=False)
                triggered = [r for r in fines_results if r.triggered]
                if triggered:
                    data["fines"] = [
                        {"rule_type": r.rule_type, "message": r.message.removeprefix("FINE TRIGGERED: ")}
                        for r in triggered
                    ]
            except Exception:  # noqa: BLE001 — best-effort enrichment
                logger.debug("Fines evaluation failed in _fetch_draft_data", exc_info=True)

    # --- Waiver info ---
    if next_gw:
        waivers_processed = game_state.get("waivers_processed", False)
        deadline_str = next_gw.get("deadline_time", "")
        if waivers_processed:
            data["waiver_info"] = {"status": "processed"}
        elif deadline_str:
            try:
                gw_deadline = datetime.fromisoformat(deadline_str.replace("Z", "+00:00"))
                waiver_deadline = gw_deadline - timedelta(hours=24)
                data["waiver_info"] = {
                    "status": "pending",
                    "deadline": waiver_deadline.isoformat(),
                }
            except (ValueError, TypeError):
                pass

    # --- Draft flagged players ---
    teams = {t.id: t.short_name for t in await client.get_teams()}
    bench_player_ids = {p.id for p in squad_players[11:]}
    flagged = [p for p in squad_players if p.status != PlayerStatus.AVAILABLE]
    if flagged:
        data["flagged_players"] = [
            {
                "name": p.web_name,
                "team": teams.get(p.team_id, "???"),
                "chance_of_playing": p.chance_of_playing_next_round,
                "news": p.news or None,
                "on_bench": p.id in bench_player_ids,
            }
            for p in flagged
        ]

    return data


async def _draft_section(
    client: FPLClient,
    draft_client: FPLDraftClient,
    get_draft_squad_fn: Callable[..., Awaitable[list[Player]]],
    draft_entry_id: int,
    draft_league_id: int | None,
    current_gw: dict[str, Any] | None,
    next_gw: dict[str, Any] | None,
    fines_config: FinesConfig | None = None,
) -> None:
    """Render draft personalised data: league standings, waiver deadline, flagged players."""
    data = await _fetch_draft_data(
        client, draft_client, get_draft_squad_fn,
        draft_entry_id, draft_league_id, current_gw, next_gw, fines_config,
    )

    # --- Draft post-GW standings ---
    league = data.get("league_standing")
    if league and current_gw:
        console.print(f"\n[bold]GW{current_gw['id']} Result:[/bold]")
        console.print(
            f"  League: {league['gw_pts']} pts"
            f" ({_ordinal(league['gw_position'])} of {league['league_size']} this week)"
            f" | {_ordinal(league['rank'])} overall"
        )

    # --- Fines rendering ---
    for f in data.get("fines", []):
        console.print(f"  [yellow]\u26a0 Fine: {f['message']}[/yellow]")

    # --- Waiver deadline ---
    waiver = data.get("waiver_info")
    if waiver:
        if waiver["status"] == "processed":
            console.print("\n[bold]Waivers:[/bold] Processed - free agency until deadline")
        elif waiver["status"] == "pending":
            countdown = _countdown(waiver["deadline"])
            console.print(f"[bold]Waiver Deadline:[/bold] in [cyan]{countdown}[/cyan]")

    # --- Draft flagged players ---
    flagged_players = data.get("flagged_players", [])
    if flagged_players:
        console.print("")  # blank line before header, matching _show_flagged_players
        console.print("[bold red]Flagged Players:[/bold red]")  # no \n prefix - already have blank line
        for p in flagged_players:
            chance = f"{p['chance_of_playing']}%" if p["chance_of_playing"] is not None else "?"
            on_bench = p["on_bench"]
            line = f"  - {p['name']} ({p['team']}): {chance}{' (bench)' if on_bench else ''}"
            console.print(f"  [dim]{line}[/dim]" if on_bench else line)
            if p["news"]:
                console.print(f"    [dim]{p['news']}[/dim]")
