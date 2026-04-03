"""FPL player listing with filters and sorting."""
# Pattern: direct-api

from __future__ import annotations

import asyncio
from collections.abc import Mapping

import click
from rich.table import Table

from fpl_cli.cli._context import CLIContext, Format, console, is_custom_analysis_enabled
from fpl_cli.cli._helpers import _format_sort_value, _validate_team_filter
from fpl_cli.cli._json import emit_json, json_output_mode, output_format_option

# Valid sort fields for `fpl stats` command
PLAYERS_SORT_FIELDS = [
    "total_points", "points_per_game", "form", "minutes",
    "goals_scored", "assists", "clean_sheets", "goals_conceded",
    "bonus", "bps",
    "expected_goals", "expected_assists", "expected_goal_involvements", "expected_goals_conceded",
    "influence", "creativity", "threat", "ict_index",
    "selected_by_percent", "now_cost", "transfers_in_event", "transfers_out_event",
    "defensive_contribution", "defensive_contribution_per_90",
    "value_form", "value_season",
    "quality_score", "value_score",
]

# Core columns shown for every `fpl stats` query.
# Maps sort field name -> core column name for dedup (sort field not appended if already a core col).
_PLAYERS_CORE_SORT_FIELDS = {"now_cost": "Price", "minutes": "Mins"}

# Sort fields that require --value flag
_VALUE_SORT_FIELDS = frozenset({"quality_score", "value_score"})


@click.command("stats")
@click.option("--position", "-p", type=click.Choice(["GK", "DEF", "MID", "FWD"], case_sensitive=False),
              default=None, help="Filter by position")
@click.option("--team", "-t", default=None, help="Filter by team short name (e.g. ARS)")
@click.option("--sort", "-s", "sort_field", type=click.Choice(PLAYERS_SORT_FIELDS, case_sensitive=False),
              default="total_points", help="Field to sort by")
@click.option("--limit", "-n", type=int, default=20, help="Number of results")
@click.option("--min-minutes", type=int, default=0, help="Minimum minutes played")
@click.option("--available-only", "-a", is_flag=True, help="Exclude injured/suspended/unavailable players")
@click.option("--reverse", "-r", is_flag=True, help="Sort ascending instead of descending")
@click.option("--value", "-v", is_flag=True, help="Add quality and value/£m columns (requires Understat data)")
@output_format_option
@click.pass_context
def stats_command(
    ctx: click.Context, position: str | None, team: str | None, sort_field: str,
    limit: int, min_minutes: int, available_only: bool, reverse: bool,
    value: bool, output_format: str,
):
    """List players with filtering and sorting."""
    from fpl_cli.api.fpl import FPLClient
    from fpl_cli.models.player import Player, PlayerPosition, PlayerStatus

    # Gate --value behind custom_analysis toggle
    settings = ctx.obj.settings if isinstance(ctx.obj, CLIContext) else {}
    custom_on = is_custom_analysis_enabled(settings)
    if not custom_on:
        if sort_field in _VALUE_SORT_FIELDS:
            console.print(
                f"[red]--sort {sort_field} requires custom analysis."
                " Enable it with: fpl init[/red]"
            )
            raise SystemExit(1)
        value = False

    # Validate: value sort fields require --value flag
    if sort_field in _VALUE_SORT_FIELDS and not value:
        console.print(f"[red]--sort {sort_field} requires the --value flag[/red]")
        raise SystemExit(1)

    # Override default sort to value_score when --value active and --sort not explicit
    explicit_value_sort = sort_field in _VALUE_SORT_FIELDS
    if value and ctx.get_parameter_source("sort_field") == click.core.ParameterSource.DEFAULT:
        sort_field = "value_score"
        explicit_value_sort = False

    fmt = ctx.obj.format if isinstance(ctx.obj, CLIContext) else None
    show_draft = fmt in (Format.DRAFT, Format.BOTH)

    position_map = {"GK": PlayerPosition.GOALKEEPER, "DEF": PlayerPosition.DEFENDER,
                    "MID": PlayerPosition.MIDFIELDER, "FWD": PlayerPosition.FORWARD}

    async def _run():
        async with FPLClient() as client:
            all_players = await client.get_players()
            all_teams = await client.get_teams()
            team_map = {t.id: t for t in all_teams}

            # Draft ownership lookup
            draft_owned: dict[int, int] = {}
            draft_entries: dict[int, str] = {}
            main_to_draft_id: dict[int, int] = {}

            if show_draft:
                from fpl_cli.cli._context import load_settings
                settings = load_settings()
                draft_league_id = settings.get("fpl", {}).get("draft_league_id")
                if not draft_league_id:
                    console.print("[yellow]No draft_league_id configured in settings.yaml[/yellow]")
                else:
                    try:
                        from fpl_cli.agents.common import get_draft_ownership_mapping
                        from fpl_cli.api.fpl_draft import FPLDraftClient
                        async with FPLDraftClient() as draft_client:
                            draft_owned, draft_entries, main_to_draft_id = (
                                await get_draft_ownership_mapping(
                                    draft_client, all_players, draft_league_id,
                                )
                            )
                    except Exception as e:  # noqa: BLE001 — best-effort enrichment
                        console.print(f"[yellow]Draft ownership lookup failed: {e}[/yellow]")

            # Filter
            team_upper = _validate_team_filter(team, all_teams)
            filtered = all_players
            if position:
                target_pos = position_map[position.upper()]
                filtered = [p for p in filtered if p.position == target_pos]
            if team_upper:
                filtered = [
                    p for p in filtered
                    if (t := team_map.get(p.team_id)) and t.short_name.upper() == team_upper
                ]
            if min_minutes > 0:
                filtered = [p for p in filtered if p.minutes >= min_minutes]
            if available_only:
                _unavailable = {PlayerStatus.INJURED, PlayerStatus.SUSPENDED,
                                PlayerStatus.NOT_AVAILABLE, PlayerStatus.UNAVAILABLE}
                filtered = [p for p in filtered if p.status not in _unavailable]

            # Value scoring pipeline (when --value active)
            quality_map: dict[int, int] = {}
            value_map: dict[int, float | None] = {}
            value_active = False

            if value and filtered:
                import httpx

                from fpl_cli.api.understat import UnderstatClient, match_fpl_to_understat
                from fpl_cli.services.player_scoring import compute_quality_value

                try:
                    async with UnderstatClient() as us_client:
                        understat_players = await us_client.get_league_players()
                except httpx.HTTPError:
                    understat_players = []
                    console.print("[yellow]Understat unavailable — skipping quality/value scores[/yellow]")

                if understat_players:
                    value_active = True
                    next_gw = await client.get_next_gameweek()
                    next_gw_id = next_gw["id"] if next_gw else 38

                    # Match filtered players to Understat
                    us_matches: dict[int, dict] = {}
                    for p in filtered:
                        t = team_map.get(p.team_id)
                        t_name = t.name if t else ""
                        us = match_fpl_to_understat(
                            p.web_name, t_name, understat_players,
                            fpl_position=p.position_name, fpl_minutes=p.minutes,
                        )
                        if us:
                            us_matches[p.id] = us

                    matched_players = [p for p in filtered if p.id in us_matches]

                    if len(matched_players) > 100:
                        console.print(
                            f"[yellow]Scoring {len(matched_players)} players, this may take a moment. "
                            "Use --position to narrow.[/yellow]"
                        )

                    # Batch-fetch get_player_detail() in groups of 50
                    player_histories: dict[int, list[dict]] = {}
                    batch_size = 50
                    for i in range(0, len(matched_players), batch_size):
                        batch = matched_players[i : i + batch_size]
                        tasks = [client.get_player_detail(p.id) for p in batch]
                        results = await asyncio.gather(*tasks, return_exceptions=True)
                        for p, result in zip(batch, results):
                            if isinstance(result, dict):
                                player_histories[p.id] = result.get("history", [])

                    # Compute quality and value scores
                    for p in matched_players:
                        t = team_map.get(p.team_id)
                        q, v = compute_quality_value(
                            p, us_matches[p.id], next_gw_id,
                            team_short=t.short_name if t else "???",
                            gw_history=player_histories.get(p.id) or None,
                        )
                        quality_map[p.id] = q
                        value_map[p.id] = v

            # Fall back from value sort if scoring failed
            effective_sort = sort_field
            if sort_field in _VALUE_SORT_FIELDS and not value_active:
                if explicit_value_sort:
                    console.print(
                        "[yellow]Understat unavailable — falling back to total_points sort[/yellow]"
                    )
                effective_sort = "total_points"

            # Sort
            if effective_sort in _VALUE_SORT_FIELDS:
                score_map: Mapping[int, int | float | None] = (
                    quality_map if effective_sort == "quality_score" else value_map
                )
                # Null-scored players sort to bottom regardless of direction
                bottom = float("-inf") if not reverse else float("inf")

                def _value_key(p: Player) -> float:
                    v = score_map.get(p.id)
                    return float(v) if v is not None else bottom

                filtered.sort(key=_value_key, reverse=not reverse)
            else:
                filtered.sort(key=lambda p: getattr(p, effective_sort), reverse=not reverse)

            # Limit
            filtered = filtered[:limit]

            metadata = {"gameweek": None, "format": str(fmt) if fmt else None,
                        "custom_analysis": custom_on,
                        "filters": {"position": position, "sort": sort_field,
                                    "limit": limit, "min_minutes": min_minutes}}

            if not filtered:
                if output_format == "json":
                    with json_output_mode() as stdout:
                        emit_json("stats", [], metadata=metadata, file=stdout)
                    return
                console.print("[yellow]No players match the given filters.[/yellow]")
                return

            if output_format == "json":
                with json_output_mode() as stdout:
                    records = [
                        {
                            "id": p.id,
                            "name": p.web_name,
                            "team": (t.short_name if (t := team_map.get(p.team_id)) else "???"),
                            "position": p.position_name,
                            "price": round(float(p.price), 1),
                            "total_points": p.total_points,
                            "points_per_game": float(p.points_per_game),
                            "form": float(p.form),
                            "minutes": p.minutes,
                            "goals_scored": p.goals_scored,
                            "assists": p.assists,
                            "clean_sheets": p.clean_sheets,
                            "goals_conceded": p.goals_conceded,
                            "bonus": p.bonus,
                            "bps": p.bps,
                            "expected_goals": float(p.expected_goals),
                            "expected_assists": float(p.expected_assists),
                            "expected_goal_involvements": float(p.expected_goal_involvements),
                            "expected_goals_conceded": float(p.expected_goals_conceded),
                            "influence": float(p.influence),
                            "creativity": float(p.creativity),
                            "threat": float(p.threat),
                            "ict_index": float(p.ict_index),
                            "selected_by_percent": float(p.selected_by_percent),
                            "transfers_in_event": p.transfers_in_event,
                            "transfers_out_event": p.transfers_out_event,
                            "defensive_contribution": p.defensive_contribution,
                            "defensive_contribution_per_90": float(p.defensive_contribution_per_90),
                            "value_form": float(p.value_form),
                            "value_season": float(p.value_season),
                            **(
                                {
                                    "quality_score": quality_map.get(p.id),
                                    "value_score": value_map.get(p.id),
                                }
                                if value_active
                                else {}
                            ),
                        }
                        for p in filtered
                    ]
                    emit_json("stats", records, metadata=metadata, file=stdout)
                return

            # Build table
            arrow = " \u25b2" if reverse else " \u25bc"
            table = Table(show_header=True, header_style="bold")

            # Core columns - track which sort field is already covered
            core_col_names = {"Name": None, "Team": None, "Pos": None, "Price": "now_cost", "Mins": "minutes"}
            sort_in_core = effective_sort in _PLAYERS_CORE_SORT_FIELDS
            sort_in_value = effective_sort in _VALUE_SORT_FIELDS

            for col_name, mapped_field in core_col_names.items():
                header = col_name
                if mapped_field == effective_sort:
                    header += arrow
                if col_name in ("Price", "Mins"):
                    justify = "right"
                elif col_name in ("Team", "Pos"):
                    justify = "center"
                else:
                    justify = "left"
                table.add_column(header, justify=justify)

            # Dynamic sort column (if not already a core column and not a value column)
            if not sort_in_core and not sort_in_value:
                table.add_column(effective_sort + arrow, justify="right")

            # Value columns (when --value active and scoring succeeded)
            if value_active:
                q_header = "Quality" + (arrow if effective_sort == "quality_score" else "")
                v_header = "Value/£m" + (arrow if effective_sort == "value_score" else "")
                table.add_column(q_header, justify="right")
                table.add_column(v_header, justify="right")

            # Draft ownership column
            has_draft_col = show_draft and main_to_draft_id
            if has_draft_col:
                table.add_column("Draft", justify="left")

            for p in filtered:
                row = [
                    p.web_name,
                    (t.short_name if (t := team_map.get(p.team_id)) else "???"),
                    p.position_name,
                    f"\u00a3{p.price:.1f}m",
                    str(p.minutes),
                ]
                if not sort_in_core and not sort_in_value:
                    row.append(_format_sort_value(effective_sort, getattr(p, effective_sort)))

                if value_active:
                    q = quality_map.get(p.id)
                    v = value_map.get(p.id)
                    row.append(str(q) if q is not None else "-")
                    row.append(str(v) if v is not None else "-")

                if has_draft_col:
                    draft_pid = main_to_draft_id.get(p.id)
                    if draft_pid is not None and draft_pid in draft_owned:
                        owner_id = draft_owned[draft_pid]
                        owner_name = draft_entries.get(owner_id, f"Team #{owner_id}")
                        row.append(f"[red]{owner_name}[/red]")
                    else:
                        row.append("[green]Available[/green]")
                table.add_row(*row)

            console.print(table)

    asyncio.run(_run())
