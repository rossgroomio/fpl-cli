"""Injury returnee radar: who is flagged, when they are due back, what moved."""
# Pattern: direct-api

from __future__ import annotations

import asyncio
import logging
from contextlib import nullcontext
from dataclasses import replace
from datetime import date
from typing import TYPE_CHECKING, Any

import click
from rich.panel import Panel
from rich.table import Table

from fpl_cli.cli._context import console, error_console, load_settings
from fpl_cli.cli._json import emit_json, emit_json_error, output_format_option

if TYPE_CHECKING:
    from fpl_cli.api.historical_types import PlayerProfile
    from fpl_cli.services.player_prior import PlayerPrior
    from fpl_cli.services.returnee_radar import RadarEntry, RadarResult

logger = logging.getLogger(__name__)

COMMAND = "returnees"

# Availability status codes spelled out for the table; the JSON keeps the raw
# code so a consumer can branch on it.
_STATUS_LABELS: dict[str, str] = {
    "d": "Doubtful",
    "i": "Injured",
    "s": "Suspended",
    "n": "Unavailable",
}

# Week-over-week markers, phrased as the change a reader is scanning for.
_TRANSITION_LABELS: dict[str, str] = {
    "newly-flagged": "New",
    "chance-improved": "Chance up",
    "chance-worsened": "Chance down",
    "newly-dated": "Date set",
    "date-moved-earlier": "Due earlier",
    "date-moved-later": "Due later",
    "date-lapsed": "Date missed",
    "date-withdrawn": "Date pulled",
    "now-available": "Available",
    "dropped-from-watchlist": "Dropped",
}

_DEPARTURE_REASONS: dict[str, str] = {
    "window": "return no longer inside the window",
    "quality": "no longer clears the quality bar",
    "unknown": "still flagged, no longer tracked",
}

_UNKNOWN_RETURN = "Unknown"


@click.command(COMMAND)
@click.option("--window", type=click.IntRange(min=1, max=38), default=None,
              help="Only show returns expected within this many gameweeks (default: 6)")
@click.option("--all", "show_all", is_flag=True, default=False,
              help="List every flagged player, including those below the quality bar")
@output_format_option
def returnees_command(window: int | None, show_all: bool, output_format: str) -> None:
    """Track injured and suspended players who are due back soon.

    Reads the availability news that ships with the player data, works out when
    each flagged player is expected back, and keeps the list short by filtering
    on past performance. Each run remembers what it showed, so the next one can
    say what changed.
    """

    async def _run() -> None:
        is_json = output_format == "json"
        settings = load_settings()

        try:
            inputs = await _fetch_inputs(quiet=is_json)
        except Exception as exc:  # noqa: BLE001 — an upstream outage is a message, not a traceback
            message = f"Could not fetch FPL data: {exc}"
            if is_json:
                emit_json_error(COMMAND, message)
            console.print(f"[red]{message}[/red]")
            raise SystemExit(1) from exc

        result = _run_radar(inputs, settings=settings, window=window, show_all=show_all)
        # Whether the bar *could* run is a property of the fetch, not of this
        # run's filters: --all supplies its own placeholder priors, so
        # result.degraded would report the bar as fine when nothing was fetched.
        quality_bar_available = bool(inputs["priors"])
        data, metadata = assemble(
            result,
            gameweek=inputs["next_gw_id"],
            window=_window_gameweeks(settings, window),
            quality_bar_available=quality_bar_available,
            quality_bar_applied=quality_bar_available and not show_all,
        )

        if not quality_bar_available:
            _warn_quality_bar_unavailable(result, show_all=show_all)

        if is_json:
            emit_json(COMMAND, data, metadata=metadata)
            return
        render_table(data, metadata)

    asyncio.run(_run())


# ---------------------------------------------------------------------------
# Fetching -- everything the radar service needs but never fetches itself
# ---------------------------------------------------------------------------


async def _fetch_inputs(*, quiet: bool) -> dict[str, Any]:
    """Gather the player pool, priors, schedule and the two optional seams.

    `run_radar` fetches nothing, so the historical profiles and Understat
    seasons its price-sourced quality branch reads are gathered here. Both are
    best-effort: without them that branch falls back to the within-position
    price percentile, which is a coarser watchlist rather than no watchlist.
    """
    from fpl_cli.api.fpl import FPLClient
    from fpl_cli.services.scoring import prepare_scoring_data

    spinner = nullcontext() if quiet else console.status("Fetching player data...")
    with spinner:
        async with FPLClient() as client:
            scoring_data = await prepare_scoring_data(
                client, include_players=True, include_prior=True,
            )
            gameweeks = await client.get_gameweeks()

    players = list(scoring_data.players or [])
    profiles = await _load_profiles()
    understat_seasons = await _load_understat_seasons(_seasons_to_score(players, profiles))

    return {
        "players": players,
        "priors": scoring_data.player_priors,
        "next_gw_id": scoring_data.next_gw_id,
        "gameweeks": gameweeks,
        "profiles": profiles,
        "understat_seasons": understat_seasons,
        # match_fpl_to_understat matches on the full club name, not the short one.
        "team_names": {team.id: team.name for team in scoring_data.teams},
    }


async def _load_profiles() -> dict[int, PlayerProfile] | None:
    """Historical profiles keyed by element_code, or None when unavailable.

    `prepare_scoring_data` builds the priors from these and then discards them
    -- and skips fetching them altogether on a prior-cache hit -- so they are
    fetched here. A failure degrades to the price-percentile path, mirroring
    how `prepare_scoring_data` swallows a failed prior fetch.
    """
    from fpl_cli.api.historical import make_historical_provider

    try:
        async with make_historical_provider() as historical:
            return await historical.get_all_player_histories()
    except Exception:  # noqa: BLE001 — graceful degradation: history is an enrichment
        logger.warning(
            "Historical profiles unavailable; the quality bar falls back to price",
            exc_info=True,
        )
        return None


def _seasons_to_score(
    players: list[Any], profiles: dict[int, PlayerProfile] | None,
) -> set[str]:
    """The season labels the quality bar will actually score, and no others.

    The bar judges a price-sourced flagged player over their most recent season
    carrying real minutes, so only those seasons are worth a scrape. Mirrors
    the service's own selection -- widening it would cost a request per season
    for data nothing reads.
    """
    from fpl_cli.services.player_prior import MIN_MINUTES
    from fpl_cli.services.returnee_radar import FLAGGED_STATUSES

    if not profiles:
        return set()

    seasons: set[str] = set()
    for player in players:
        raw_status = getattr(player, "status", "a")
        if getattr(raw_status, "value", raw_status) not in FLAGGED_STATUSES:
            continue
        profile = profiles.get(getattr(player, "code", 0))
        if profile is None:
            continue
        qualifying = [s.season for s in profile.seasons if s.minutes >= MIN_MINUTES]
        if qualifying:
            seasons.add(max(qualifying))
    return seasons


async def _load_understat_seasons(seasons: set[str]) -> dict[str, list[dict[str, Any]]]:
    """Whole-season Understat player lists keyed by season label.

    Best-effort per season: a scrape that fails or comes back empty leaves that
    season out, and the players it would have sharpened are scored on their FPL
    stats alone.
    """
    if not seasons:
        return {}

    from fpl_cli.api.understat import UnderstatClient
    from fpl_cli.season import understat_season

    fetched: dict[str, list[dict[str, Any]]] = {}
    try:
        async with UnderstatClient() as client:
            for label in sorted(seasons):
                players = await client.get_league_players(understat_season(int(label[:4])))
                if players:
                    fetched[label] = players
    except Exception:  # noqa: BLE001 — graceful degradation: xG sharpening is optional
        logger.info("Understat season data unavailable; scoring on FPL stats alone",
                    exc_info=True)
    return fetched


def _window_gameweeks(settings: dict[str, Any], window: int | None) -> int:
    from fpl_cli.services.returnee_radar import radar_config_from_settings

    return window if window is not None else radar_config_from_settings(settings).window_gameweeks


def _run_radar(
    inputs: dict[str, Any], *, settings: dict[str, Any], window: int | None, show_all: bool,
) -> RadarResult:
    """Apply the flags to the resolved config and run one radar pass."""
    from fpl_cli.services.returnee_radar import radar_config_from_settings, run_radar

    config = radar_config_from_settings(settings)
    if window is not None:
        config = replace(config, window_gameweeks=window)

    priors = inputs["priors"]
    if show_all:
        # The bar is `score >= threshold` on 0-1 measures, so a zero threshold
        # admits everyone without touching how any score is computed.
        config = replace(config, history_watchlist_strength=0.0, price_watchlist_percentile=0.0)
        priors = priors or _neutral_priors(inputs["players"])

    return run_radar(
        inputs["players"],
        priors=priors,
        next_gw_id=inputs["next_gw_id"],
        gameweeks=inputs["gameweeks"],
        config=config,
        profiles=inputs["profiles"],
        understat_seasons=inputs["understat_seasons"],
        team_names=inputs["team_names"],
        # A filter-bypassed watchlist must not become next week's baseline, or
        # the next ordinary run reports everyone it re-excluded as dropped.
        persist=not show_all,
    )


def _neutral_priors(players: list[Any]) -> dict[int, PlayerPrior]:
    """Placeholder priors so `--all` still lists everyone when priors failed.

    The radar needs a prior per player to have something to judge, and reports
    a degraded run when it has none. With the bar bypassed there is nothing to
    judge, so a neutral entry keeps `--all` answering the question it exists
    for -- which is what the degraded-run note points the user at.
    """
    from fpl_cli.services.player_prior import PlayerPrior

    return {
        player.id: PlayerPrior(prior_strength=0.0, confidence=0.0, source="price")
        for player in players
    }


def _warn_quality_bar_unavailable(result: RadarResult, *, show_all: bool) -> None:
    """Say why the quality bar did not run, on stderr so JSON stays parseable.

    An empty watchlist with nothing said about it reads as "nobody is flagged",
    which is the one thing this run cannot tell you.
    """
    reason = result.degraded_reason or (
        "Player priors are unavailable, so the quality bar could not run — "
        "every flagged player below is unranked."
    )
    error_console.print(f"[yellow]{reason}[/yellow]")
    if not show_all:
        error_console.print(
            "[yellow]Run 'fpl returnees --all' to list every flagged player without it.[/yellow]",
        )


# ---------------------------------------------------------------------------
# One assembled payload, rendered two ways
# ---------------------------------------------------------------------------


def assemble(
    result: RadarResult,
    *,
    gameweek: int,
    window: int,
    quality_bar_available: bool,
    quality_bar_applied: bool,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Build the data and metadata both output paths render from.

    Returns only data: the table renderer derives every label it needs from
    these dicts, so a change to the payload moves both outputs at once.
    """
    from fpl_cli.season import season_label

    data = {
        "entries": [_entry_data(entry) for entry in result.entries],
        "departures": [
            {
                "id": departure.player_id,
                "web_name": departure.web_name,
                "status": departure.status,
                "transition": departure.transition,
                "reason": departure.reason,
            }
            for departure in result.departures
        ],
    }
    metadata = {
        "season": season_label(),
        "gameweek": gameweek,
        "window": window,
        "transitions_available": result.transitions_available,
        "quality_bar_available": quality_bar_available,
        "quality_bar_applied": quality_bar_applied,
    }
    return data, metadata


def _entry_data(entry: RadarEntry) -> dict[str, Any]:
    """One watchlist row as plain data."""
    signal, quality = entry.signal, entry.quality
    return {
        "id": entry.player_id,
        "code": entry.code,
        "web_name": entry.web_name,
        "team": entry.team_name,
        "team_id": entry.team_id,
        "position": entry.position,
        "status": entry.status,
        "status_label": _STATUS_LABELS.get(entry.status, entry.status),
        "price": entry.price,
        "chance_of_playing": entry.chance_of_playing,
        "news": signal.news,
        "news_age_days": signal.news_age_days,
        "expected_return": signal.return_date.isoformat() if signal.return_date else None,
        "return_known": signal.has_return_date,
        "return_gameweek": signal.return_gameweek,
        "return_source": signal.source,
        "lapsed": signal.lapsed,
        "quality": {
            "basis": quality.basis,
            "score": round(quality.score, 3),
            "threshold": quality.threshold,
            "passed": quality.passed,
            "meets_stash": quality.meets_stash,
            "prior_source": quality.prior_source,
            "season": quality.season,
            "quality_score": quality.quality_score,
        },
        "transition": entry.transition,
    }


def render_table(data: dict[str, Any], metadata: dict[str, Any]) -> None:
    """Render the assembled payload as a Rich table."""
    entries = data["entries"]
    gameweek, window = metadata["gameweek"], metadata["window"]

    if not entries:
        if metadata["quality_bar_available"]:
            hint = (
                " Run with --all to list every flagged player."
                if metadata["quality_bar_applied"] else ""
            )
            console.print(Panel.fit(
                f"[green]Nobody on the radar for GW{gameweek}[/green] — no flagged player is "
                f"worth watching in the next {window} gameweeks.{hint}",
            ))
        _render_departures(data["departures"])
        return

    table = Table(title=f"Returnee Radar (GW{gameweek}, next {window} gameweeks)")
    table.add_column("Player", style="bold")
    table.add_column("Team")
    table.add_column("Pos", width=3)
    table.add_column("Price", justify="right")
    table.add_column("Status")
    table.add_column("Expected Return")
    table.add_column("Chance", justify="right")
    table.add_column("Quality", justify="right")
    table.add_column("Change")

    for entry in entries:
        table.add_row(
            entry["web_name"],
            entry["team"] or "-",
            entry["position"],
            f"£{entry['price']:.1f}m",
            entry["status_label"],
            _return_cell(entry),
            _chance_cell(entry["chance_of_playing"]),
            _quality_cell(entry["quality"]),
            _transition_cell(entry["transition"], metadata["transitions_available"]),
        )

    console.print(table)

    if any(entry["quality"]["meets_stash"] for entry in entries):
        console.print(
            "[dim]* clears the higher bar worth holding a squad place for.[/dim]",
        )
    if not metadata["transitions_available"]:
        console.print(
            "[dim]No stored watchlist to compare against — this is the first run, "
            "so week-over-week changes appear from the next one.[/dim]",
        )
    _render_departures(data["departures"])


def _render_departures(departures: list[dict[str, Any]]) -> None:
    """List who left the watchlist, and whether that is good news."""
    if not departures:
        return
    console.print("\n[bold]No longer on the radar:[/bold]")
    for departure in departures:
        if departure["transition"] == "now-available":
            console.print(f"  [green]{departure['web_name']}[/green] — available again")
        else:
            reason = _DEPARTURE_REASONS.get(
                departure["reason"] or "unknown", _DEPARTURE_REASONS["unknown"],
            )
            console.print(f"  [dim]{departure['web_name']} — {reason}[/dim]")


def _return_cell(entry: dict[str, Any]) -> str:
    """Expected return, never a blank cell: date-unknown is stated outright."""
    stated = entry["expected_return"]
    if entry["return_known"] and stated:
        gameweek = entry["return_gameweek"]
        text = _format_return_date(stated)
        return f"{text} (GW{gameweek})" if gameweek else text
    if stated:
        # The date is in the past and the player is still flagged: the return
        # slipped, so it reads as unknown while still showing what was said.
        return f"{_UNKNOWN_RETURN} (was {_format_return_date(stated)})"
    return _UNKNOWN_RETURN


def _format_return_date(value: str) -> str:
    """Format a stated return date as e.g. `5 Sep`.

    A bare calendar date, not a timestamp: FPL states a day and a month with no
    time or zone, so there is nothing for `utils.time` to convert.
    """
    try:
        parsed = date.fromisoformat(value)
    except ValueError:  # pragma: no cover - the payload is built from a date
        return value
    return f"{parsed.day} {parsed:%b}"


def _chance_cell(chance: int | None) -> str:
    if chance is None:
        return "-"
    style = "green" if chance >= 75 else "yellow" if chance >= 50 else "red"
    return f"[{style}]{chance}%[/{style}]"


def _quality_cell(quality: dict[str, Any]) -> str:
    """The 0-1 measure actually compared against the bar, whichever branch ran."""
    cell = f"{quality['score']:.2f}"
    return f"[bold]{cell}*[/bold]" if quality["meets_stash"] else cell


def _transition_cell(transition: str | None, transitions_available: bool) -> str:
    if not transitions_available:
        return "[dim]?[/dim]"
    if transition is None:
        return "-"
    return f"[cyan]{_TRANSITION_LABELS.get(transition, transition)}[/cyan]"
