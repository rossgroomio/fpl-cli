"""FPL historical player profiles."""
# Pattern: direct-api

from __future__ import annotations

import asyncio

import click

from fpl_cli.cli._context import console
from fpl_cli.cli._json import emit_json, emit_json_error, json_output_mode, output_format_option
from fpl_cli.cli.player import _build_history_json


@click.command("history")
@output_format_option
def history_command(output_format: str):
    """Show historical player performance across seasons."""

    async def _run():
        from fpl_cli.api.fpl import FPLClient
        from fpl_cli.api.vaastav import VaastavClient

        async with FPLClient() as fpl_client, VaastavClient() as vaastav:
            try:
                current_players = await fpl_client.get_players()
                current_codes = {p.code for p in current_players}
                all_profiles = await vaastav.get_all_player_histories()
                relevant = {
                    code: profile
                    for code, profile in all_profiles.items()
                    if code in current_codes
                }
                if not relevant:
                    if output_format == "json":
                        with json_output_mode() as stdout:
                            emit_json("history", [], file=stdout)
                        return
                    console.print("[yellow]No historical data found[/yellow]")
                    return
                if output_format == "json":
                    with json_output_mode() as stdout:
                        profiles_data = []
                        for profile in sorted(relevant.values(), key=lambda p: p.web_name):
                            if not profile.pts_per_90:
                                continue
                            profile_dict = {
                                "name": profile.web_name,
                                "code": profile.element_code,
                                "position": profile.current_position,
                            }
                            profile_dict.update(_build_history_json(profile))
                            profiles_data.append(profile_dict)
                        emit_json("history", profiles_data, file=stdout)
                    return
                for profile in sorted(relevant.values(), key=lambda p: p.web_name):
                    if not profile.pts_per_90:
                        continue
                    n_seasons = len(profile.pts_per_90)
                    pts_str = "/".join(f"{v:.1f}" for v in profile.pts_per_90)
                    xgi_str = "/".join(f"{v:.1f}" for v in profile.xgi_per_90)
                    trend_str = f"pts_trend={profile.pts_per_90_trend:+.2f}"
                    xgi_trend = (
                        f"xgi_trend={profile.xgi_per_90_trend:+.2f}"
                        if profile.xgi_per_90_trend is not None
                        else "xgi_trend=N/A"
                    )
                    cost_str = f"cost_trend={profile.cost_trajectory:+.1f}"
                    console.print(
                        f"{profile.web_name} ({profile.current_position}) "
                        f"[{n_seasons}s] pts/90={pts_str} {trend_str} "
                        f"xgi/90={xgi_str} {xgi_trend} {cost_str}"
                    )
            except Exception as e:  # noqa: BLE001 — display resilience
                if output_format == "json":
                    with json_output_mode() as stdout:
                        emit_json_error("history", "Failed to fetch historical data", file=stdout)
                    return
                console.print(f"[red]Error fetching historical data: {e}[/red]")

    asyncio.run(_run())
