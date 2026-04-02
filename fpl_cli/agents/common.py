"""Shared utilities for FPL agents."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import TYPE_CHECKING, Any, cast

import httpx

if TYPE_CHECKING:
    from fpl_cli.api.fpl_draft import FPLDraftClient
    from fpl_cli.models.player import Player

from fpl_cli.api.fpl import FPLClient
from fpl_cli.api.understat import UnderstatClient, match_fpl_to_understat
from fpl_cli.constants import MIN_MINUTES_FOR_PER90
from fpl_cli.models.types import EnrichedPlayer

# Re-export: canonical location is now fpl_cli.services.player_scoring
from fpl_cli.services.player_scoring import build_understat_by_player_id as build_understat_by_player_id
from fpl_cli.utils.text import strip_diacritics


def enrich_player(
    player: dict[str, Any],
    team_map: dict[int, Any],
    include_availability: bool = True,
) -> EnrichedPlayer:
    """Enrich player dict with team name, position, and derived stats.

    Mutates and returns ``player``. Callers sharing a dict across
    multiple consumers should pass a copy.
    """
    team = team_map.get(player.get("team_id", 0), {})
    player["team_name"] = team.get("name", "Unknown")
    player["team_short"] = team.get("short_name", "???")

    # position is already a str after normalisation - no separate position_name needed

    # Defensive contribution for quality scoring
    player["dc_per_90"] = player.get("defensive_contribution_per_90", 0)

    # Calculate xGI per 90 (only with sufficient sample size)
    minutes = player.get("minutes", 0)
    if minutes >= MIN_MINUTES_FOR_PER90:
        xg = player.get("expected_goals", 0)
        xa = player.get("expected_assists", 0)
        player["xGI_per_90"] = round(((xg + xa) / minutes) * 90, 2)
    else:
        player["xGI_per_90"] = 0

    # Availability status
    if include_availability:
        chance = player.get("chance_of_playing")
        news = player.get("news", "")
        if chance is None or chance == 100:
            player["availability"] = "\u2713"
        elif chance == 0:
            player["availability"] = "\u2717"
        else:
            player["availability"] = f"{chance}%"
        player["injury_news"] = news[:30] if news else ""

    return cast(EnrichedPlayer, player)


async def fetch_understat_lookup(
    players: Sequence[dict[str, Any]],
    get_team_name: Callable[[dict[str, Any]], str | None],
    log: Callable[[str], None] | None = None,
    client: UnderstatClient | None = None,
) -> dict[int, dict[str, Any]]:
    """Fetch Understat data and return {player_index: understat_match}.

    Centralises the Understat fetch + match loop used by multiple agents.
    Each caller is responsible for extracting the fields it needs from
    the returned match dicts.

    Args:
        players: List of player dicts with at least player_name, position_name,
            and minutes keys. Index alignment between this list and the
            caller's data is required - avoid filtering after construction.
        get_team_name: Callable that extracts the FPL team name from a player
            dict. Varies per caller (e.g. captain uses Team model, draft agents
            use a pre-enriched "team_name" string).
        log: Optional callable for warning on failure.
        client: Optional UnderstatClient to reuse across calls.

    Returns:
        Mapping of player list index to the matched Understat player dict.
    """
    _created = client is None
    understat = client or UnderstatClient()
    try:
        understat_players = await understat.get_league_players()
        result: dict[int, dict[str, Any]] = {}
        for i, player in enumerate(players):
            team_name = get_team_name(player)
            if not team_name:
                continue
            us_match = match_fpl_to_understat(
                player.get("player_name", ""),
                team_name,
                understat_players,
                fpl_position=player.get("position"),
                fpl_minutes=player.get("minutes"),
            )
            if us_match:
                result[i] = us_match
        return result
    except (httpx.HTTPError, ConnectionError, TimeoutError):
        if log:
            log("Understat data unavailable, using FPL-only metrics")
        return {}
    finally:
        if _created:
            await understat.close()




async def get_draft_squad_players(
    draft_client: FPLDraftClient,
    main_players: list[Player],
    draft_entry_id: int,
    gameweek: int,
    log: Callable[[str], None] | None = None,
) -> list[Player]:
    """Fetch draft squad and map to main FPL Player objects.

    Returns list of Player objects from the main API that correspond
    to the draft entry's picks for the given gameweek.
    """
    draft_bootstrap = await draft_client.get_bootstrap_static()
    draft_players = {dp["id"]: dp for dp in draft_bootstrap.get("elements", [])}

    try:
        picks_data = await draft_client.get_entry_picks(draft_entry_id, gameweek)
    except (httpx.HTTPError, ValueError):
        if gameweek > 1:
            if log:
                log(f"GW{gameweek} picks unavailable, trying GW{gameweek - 1}")
            picks_data = await draft_client.get_entry_picks(draft_entry_id, gameweek - 1)
        else:
            raise

    pick_ids = [p["element"] for p in picks_data.get("picks", [])]
    main_by_key = {(strip_diacritics(p.web_name).lower(), p.team_id): p for p in main_players}
    squad: list[Player] = []
    for dpid in pick_ids:
        dp = draft_players.get(dpid)
        if not dp:
            continue
        match = main_by_key.get((strip_diacritics(dp["web_name"]).lower(), dp["team"]))
        if match:
            squad.append(match)
        elif log:
            log(f"Draft: could not map '{dp['web_name']}' to main FPL data")

    return squad


async def get_draft_ownership_mapping(
    draft_client: FPLDraftClient,
    main_players: list[Player],
    draft_league_id: int,
) -> tuple[dict[int, int], dict[int, str], dict[int, int]]:
    """Fetch draft ownership and build ID mapping to main FPL players.

    Returns:
        (draft_owned, draft_entries, main_to_draft_id) where:
        - draft_owned: draft_player_id -> owner_entry_id
        - draft_entries: entry_id -> manager display name
        - main_to_draft_id: main_player_id -> draft_player_id
    """
    league_details = await draft_client.get_league_details(draft_league_id)
    draft_bootstrap = await draft_client.get_bootstrap_static()

    draft_entries: dict[int, str] = {}
    for entry in league_details.get("league_entries", []):
        name = (
            f"{entry.get('player_first_name', '')} "
            f"{entry.get('player_last_name', '')}".strip()
        )
        draft_entries[entry["entry_id"]] = name or "Unknown"

    draft_owned = await draft_client.get_league_ownership(
        draft_league_id, draft_bootstrap,
    )

    draft_by_name_team = {
        (dp.get("web_name"), dp.get("team")): dp["id"]
        for dp in draft_bootstrap.get("elements", [])
    }
    main_to_draft_id: dict[int, int] = {}
    for p in main_players:
        draft_id = draft_by_name_team.get((p.web_name, p.team_id))
        if draft_id:
            main_to_draft_id[p.id] = draft_id

    return draft_owned, draft_entries, main_to_draft_id


async def get_actual_squad_picks(
    client: FPLClient,
    entry_id: int,
    gameweek: int,
    log: Callable[[str], None] | None = None,
) -> tuple[dict[str, Any], int]:
    """Fetch manager picks, falling back past Free Hit gameweeks.

    Returns (picks_data, actual_gameweek) where actual_gameweek may be
    less than the input if a Free Hit was detected.
    """
    picks_data = await client.get_manager_picks(entry_id, gameweek)
    active_chip = picks_data.get("active_chip")

    if active_chip == "freehit" and gameweek > 1:
        if log:
            log(f"Free Hit detected in GW{gameweek}, using GW{gameweek - 1} for actual squad")
        gameweek -= 1
        picks_data = await client.get_manager_picks(entry_id, gameweek)

    return picks_data, gameweek
