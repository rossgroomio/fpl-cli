"""Optimal squad allocation via ILP solver."""
# Pattern: direct-api

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

import click
from rich.panel import Panel
from rich.table import Table

from fpl_cli.cli._context import console
from fpl_cli.cli._json import emit_json, emit_json_error, json_output_mode, output_format_option
from fpl_cli.services.player_scoring import STARTING_XI_CEILING, VALUE_CEILING, normalise_score

if TYPE_CHECKING:
    from fpl_cli.services.squad_allocator import ScoredPlayer, SquadResult


@click.command("allocate")
@click.option("--budget", type=click.FloatRange(min=0.1), default=100.0,
              help="Total budget in GBP millions (default 100.0)")
@click.option("--horizon", type=click.IntRange(min=1, max=38), default=6, help="Gameweeks to look ahead (default 6)")
@click.option("--bench-discount", type=click.FloatRange(min=0.0, max=1.0), default=None,
              help="Bench player discount factor, applied uniformly to all positions (default: 0.15 outfield, 0.05 GK)")
@click.option("--bench-boost-gw", type=click.IntRange(min=1, max=38), default=None,
              help="Gameweek to play Bench Boost (bench discount overridden to 1.0 for this GW)")
@click.option("--free-transfers", type=click.IntRange(min=0, max=5), default=1,
              help="Banked free transfers (0-5). More FTs = near-term GWs weighted more heavily")
@click.option("--sell-prices", "sell_prices_path", type=click.Path(exists=True, dir_okay=False), default=None,
              help="JSON file with player sell prices (from 'fpl squad sell-prices --format json')")
@output_format_option
@click.pass_context
def allocate_command(
    ctx: click.Context, budget: float, horizon: int, bench_discount: float | None,
    bench_boost_gw: int | None, free_transfers: int, sell_prices_path: str | None,
    output_format: str,
) -> None:
    """Select the mathematically optimal 15-player squad within budget."""

    import json as json_mod

    from click.core import ParameterSource

    # Load sell-price overrides
    price_overrides: dict[int, float] | None = None
    if sell_prices_path is not None:
        try:
            with open(sell_prices_path, encoding="utf-8") as f:
                sp_data = json_mod.load(f)
        except (json_mod.JSONDecodeError, OSError) as exc:
            console.print(f"[red]Error reading sell-prices file: {exc}[/red]")
            raise SystemExit(1) from exc

        players_list = sp_data.get("data", [])
        try:
            price_overrides = {p["id"]: p["sell_price"] for p in players_list}
        except KeyError as exc:
            console.print(f"[red]Sell-prices JSON missing required field: {exc}[/red]")
            raise SystemExit(1) from exc

        # Auto-compute budget from JSON when --budget not explicitly set
        if ctx.get_parameter_source("budget") != ParameterSource.COMMANDLINE:
            sp_bank = sp_data.get("metadata", {}).get("bank", 0.0)
            budget = sum(p["sell_price"] for p in players_list) + sp_bank

    async def _run() -> None:
        from fpl_cli.api.fpl import FPLClient
        from fpl_cli.services.player_scoring import prepare_scoring_data
        from fpl_cli.services.squad_allocator import (
            compute_fixture_coefficients,
            score_all_players,
            score_all_players_sgw,
            solve_squad,
        )

        is_json = output_format == "json"

        with console.status("Fetching player data..."):
            async with FPLClient() as client:
                scoring_data = await prepare_scoring_data(
                    client,
                    include_players=True,
                    include_understat=True,
                    include_history=True,
                    include_prior=True,
                )

        start_gw = scoring_data.next_gw_id

        if horizon == 1:
            with console.status("Scoring players (single-GW)..."):
                scored_players = score_all_players_sgw(scoring_data)
            coefficients = {
                sp.player.id: [0.0 if sp.suspended_gw1 else sp.raw_quality]
                for sp in scored_players
            }
        else:
            with console.status("Scoring players..."):
                scored_players = score_all_players(scoring_data)

            with console.status("Computing fixture coefficients..."):
                coefficients = compute_fixture_coefficients(
                    scored_players, scoring_data, horizon, start_gw,
                )

        bd = (
            {pos: bench_discount for pos in ("GK", "DEF", "MID", "FWD")}
            if bench_discount is not None
            else None
        )

        bb_gw_idx: int | None = None
        if bench_boost_gw is not None:
            effective_end_gw = min(start_gw + horizon, 39)
            if not (start_gw <= bench_boost_gw < effective_end_gw):
                msg = f"Bench Boost GW {bench_boost_gw} is outside horizon GW{start_gw}-{effective_end_gw - 1}"
                if is_json:
                    with json_output_mode() as stdout:
                        emit_json_error("allocate", msg, file=stdout)
                else:
                    console.print(Panel(msg, title="Allocation Failed", border_style="red"))
                    raise SystemExit(1)
            bb_gw_idx = bench_boost_gw - start_gw

        with console.status("Solving (7 formations)..."):
            result = solve_squad(
                scored_players, coefficients, budget,
                bench_discount=bd, bench_boost_gw_idx=bb_gw_idx,
                free_transfers=free_transfers,
                price_overrides=price_overrides,
            )

        if result.status == "infeasible":
            msg = f"Infeasible: could not select 15 players within {budget}m budget and constraints"
            if is_json:
                with json_output_mode() as stdout:
                    emit_json_error("allocate", msg, file=stdout)
            else:
                console.print(Panel(msg, title="Allocation Failed", border_style="red"))
                raise SystemExit(1)

        ceiling = STARTING_XI_CEILING if horizon == 1 else VALUE_CEILING
        _emit_result(
            result, scoring_data, scored_players,
            budget, horizon, start_gw, is_json,
            bench_discount=bd, bench_boost_gw=bench_boost_gw,
            free_transfers=free_transfers, ceiling=ceiling,
        )

    asyncio.run(_run())


def _emit_result(
    result: SquadResult,
    scoring_data: Any,
    scored_players: list[ScoredPlayer],
    budget: float,
    horizon: int,
    start_gw: int,
    is_json: bool,
    *,
    bench_discount: dict[str, float] | None = None,
    bench_boost_gw: int | None = None,
    free_transfers: int = 1,
    ceiling: float = VALUE_CEILING,
) -> None:
    """Format and output the solver result."""
    player_lookup = {sp.player.id: sp for sp in result.selected_players}
    team_map = scoring_data.team_map

    captain_gws_by_player: dict[int, list[int]] = {}
    for gw_idx, pid in result.captain_schedule.items():
        captain_gws_by_player.setdefault(pid, []).append(start_gw + gw_idx)

    players_data = []
    for sp in sorted(
        result.selected_players,
        key=lambda s: (0 if s.player.id in result.starter_ids else 1, -s.raw_quality),
    ):
        team = team_map.get(sp.player.team_id)
        team_short = team.short_name if team else "???"
        q_score = normalise_score(sp.raw_quality, ceiling)
        role = "starter" if sp.player.id in result.starter_ids else "bench"
        captain_gws = captain_gws_by_player.get(sp.player.id, [])

        is_owned = bool(result.owned_ids) and sp.player.id in result.owned_ids
        saving = result.player_savings.get(sp.player.id, 0.0) if is_owned else 0.0

        entry: dict[str, Any] = {
            "id": sp.player.id,
            "web_name": sp.player.web_name,
            "team": team_short,
            "position": sp.position,
            "price": sp.player.price,
            "effective_price": round(sp.player.price - saving, 1),
            "quality_score": q_score,
            "raw_quality": round(sp.raw_quality, 3),
            "role": role,
            "captain_gws": captain_gws,
        }
        if result.owned_ids:
            entry["owned"] = is_owned
            entry["saving"] = saving
        players_data.append(entry)

    captain_schedule_named = {}
    for gw_idx, pid in result.captain_schedule.items():
        sp = player_lookup[pid]
        captain_schedule_named[str(start_gw + gw_idx)] = sp.player.web_name

    if is_json:
        metadata: dict[str, Any] = {
            "budget": budget,
            "budget_used": result.budget_used,
            "budget_remaining": result.budget_remaining,
            "horizon": horizon,
            "start_gw": start_gw,
            "formation": list(result.formation),
            "solver_status": result.status,
            "objective_value": result.objective_value,
            "captain_schedule": captain_schedule_named,
            "player_count": len(scored_players),
            "bench_discount": bench_discount,
            "bench_boost_gw": bench_boost_gw,
            "free_transfers": free_transfers,
        }
        if result.owned_ids:
            metadata["total_savings"] = round(sum(result.player_savings.values()), 1)
        with json_output_mode() as stdout:
            emit_json("allocate", players_data, metadata=metadata, file=stdout)
    else:
        _render_table(
            result, players_data, budget, horizon, start_gw, len(scored_players),
            bench_boost_gw=bench_boost_gw, free_transfers=free_transfers,
        )


def _render_table(
    result: SquadResult,
    players_data: list[dict[str, Any]],
    budget: float,
    horizon: int,
    start_gw: int,
    player_count: int,
    *,
    bench_boost_gw: int | None = None,
    free_transfers: int = 1,
) -> None:
    """Render squad as Rich table."""
    has_owned = any(p.get("owned") for p in players_data)

    table = Table(title=f"Optimal Squad (GW {start_gw}-{start_gw + horizon - 1})")
    table.add_column("", width=1)
    table.add_column("Player", style="bold")
    table.add_column("Pos", width=3)
    table.add_column("Team", width=3)
    table.add_column("Price", justify="right")
    table.add_column("Quality", justify="right")
    table.add_column("Captain", width=12)
    if has_owned:
        table.add_column("Status", width=20)

    for p in players_data:
        role_icon = ">" if p["role"] == "starter" else " "
        captain_str = ",".join(f"GW{gw}" for gw in p["captain_gws"]) if p["captain_gws"] else ""
        q_style = "bold green" if p["quality_score"] >= 75 else "green" if p["quality_score"] >= 50 else ""

        row = [
            role_icon,
            p["web_name"],
            p["position"],
            p["team"],
            f"£{p['price']:.1f}m",
            f"[{q_style}]{p['quality_score']}[/{q_style}]" if q_style else str(p["quality_score"]),
            captain_str,
        ]
        if has_owned:
            if p.get("owned"):
                saving = p.get("saving", 0.0)
                if saving > 0:
                    row.append(f"[cyan]Owned (saves £{saving:.1f}m)[/cyan]")
                elif saving < 0:
                    row.append(f"[cyan]Owned (costs £{abs(saving):.1f}m extra)[/cyan]")
                else:
                    row.append("[cyan]Owned[/cyan]")
            else:
                row.append("")
        table.add_row(*row)

    console.print(table)

    def_n, mid_n, fwd_n = result.formation
    summary = (
        f"Formation: {def_n}-{mid_n}-{fwd_n}  |  "
        f"Budget: £{result.budget_used:.1f}m / £{budget:.1f}m (£{result.budget_remaining:.1f}m remaining)  |  "
        f"Players scored: {player_count}"
    )
    if bench_boost_gw is not None:
        summary += f"  |  BB: GW{bench_boost_gw}"
    if free_transfers != 1:
        summary += f"  |  FTs: {free_transfers}"
    console.print(Panel(summary, border_style="blue"))
