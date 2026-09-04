"""Injury returnee radar: who is flagged, when they are due back, what moved."""
# Pattern: direct-api

from __future__ import annotations

import asyncio
import logging
from collections.abc import Iterable, Sequence
from contextlib import nullcontext
from dataclasses import dataclass, replace
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
    from fpl_cli.services.returnee_radar import (
        EnrichedReturn,
        RadarConfig,
        RadarEntry,
        RadarResult,
    )

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
              help="Only show returns expected within this many gameweeks, overriding "
                   "the configured window")
@click.option("--all", "show_all", is_flag=True, default=False,
              help="List every flagged player, including those below the quality bar")
@click.option("--enrich", is_flag=True, default=False,
              help="Search the web for fresher return news where FPL says nothing or "
                   "says it a while ago (needs a Perplexity API key)")
@output_format_option
def returnees_command(
    window: int | None, show_all: bool, enrich: bool, output_format: str,
) -> None:
    """Track injured and suspended players who are due back soon.

    Reads the availability news that ships with the player data, works out when
    each flagged player is expected back, and keeps the list short by filtering
    on past performance. Each run remembers what it showed, so the next one can
    say what changed.

    With --enrich it also searches the web for fresher return timing on the
    players FPL is quiet or stale about, and shows what it finds alongside the
    FPL news rather than in place of it.
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

        config = _radar_config(settings, window=window)
        result = _run_radar(inputs, config=config, show_all=show_all)
        enrichment = await _enrich(
            result, inputs=inputs, settings=settings, config=config, requested=enrich,
        )
        result = enrichment.result
        # Whether the bar *could* run is a property of the fetch, not of this
        # run's filters: --all supplies its own placeholder priors, so
        # result.degraded would report the bar as fine when nothing was fetched.
        quality_bar_available = bool(inputs["priors"])
        data, metadata = assemble(
            result,
            gameweek=inputs["next_gw_id"],
            window=config.window_gameweeks,
            escalation_window=config.stash_window_gameweeks,
            stash_upgrade_margin=config.stash_upgrade_margin,
            quality_bar_available=quality_bar_available,
            quality_bar_applied=quality_bar_available and not show_all,
            enrichment=enrichment,
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
    except Exception as exc:  # noqa: BLE001 — graceful degradation: history is an enrichment
        # No traceback: fpl-cli configures no logging handlers, so a WARNING
        # with exc_info reaches logging's lastResort handler and dumps it raw
        # into stderr, including under `--format json` (issue #237/#239 review).
        logger.warning(
            "Historical profiles unavailable; the quality bar falls back to price: %s",
            exc,
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

    Fetched concurrently and best-effort per season: the scrapes are
    independent, so one that fails or comes back empty leaves only its own
    season out, and the players it would have sharpened are scored on their FPL
    stats alone.
    """
    if not seasons:
        return {}

    from fpl_cli.api.understat import UnderstatClient
    from fpl_cli.season import understat_season

    labels = sorted(seasons)
    fetched: dict[str, list[dict[str, Any]]] = {}
    try:
        async with UnderstatClient() as client:
            results = await asyncio.gather(
                *(
                    client.get_league_players(understat_season(int(label[:4])))
                    for label in labels
                ),
                return_exceptions=True,
            )
    except Exception:  # noqa: BLE001 — graceful degradation: xG sharpening is optional
        logger.info("Understat season data unavailable; scoring on FPL stats alone",
                    exc_info=True)
        return fetched

    for label, players in zip(labels, results, strict=True):
        if isinstance(players, BaseException):
            logger.info("Understat data unavailable for %s; scoring it on FPL stats alone",
                        label, exc_info=players)
            continue
        if players:
            fetched[label] = players
    return fetched


def _radar_config(settings: dict[str, Any], *, window: int | None) -> RadarConfig:
    """The resolved knobs, with `--window` applied.

    Built once per run: the radar pass, the enrichment shortlist and the
    rendered metadata all have to agree on the same window.
    """
    from fpl_cli.services.returnee_radar import radar_config_from_settings

    config = radar_config_from_settings(settings)
    return config if window is None else replace(config, window_gameweeks=window)


def _run_radar(
    inputs: dict[str, Any], *, config: RadarConfig, show_all: bool,
) -> RadarResult:
    """Apply the flags to the resolved config and run one radar pass."""
    from fpl_cli.services.returnee_radar import run_radar

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
# Optional AI-search enrichment -- opt-in, bounded, and never load-bearing
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _Enrichment:
    """What the optional enrichment pass did, or why it did nothing.

    Carries the watchlist back out because attaching intel rebuilds the
    entries. `available` is whether the research provider resolved at all;
    `note` says what went wrong when it did not, or what came back short.
    """

    result: RadarResult
    requested: bool = False
    available: bool = False
    note: str | None = None
    count: int = 0
    # Whether any player went unanswered because the provider rate-limited the
    # run even after retrying -- the one failure a re-run is likely to clear.
    rate_limited: bool = False


async def _enrich(
    result: RadarResult,
    *,
    inputs: dict[str, Any],
    settings: dict[str, Any],
    config: RadarConfig,
    requested: bool,
) -> _Enrichment:
    """Top up missing or stale return timing from AI search, if asked to.

    Opt-in and bounded: without `--enrich` nothing external is contacted, and
    with it only the shortlist -- date-unknown or stale-news entries, capped by
    config -- is queried. The provider is resolved before any query work, so a
    machine with no API key pays nothing and still gets the deterministic
    watchlist (R7).
    """
    if not requested or result.degraded:
        return _Enrichment(result=result, requested=requested)

    from fpl_cli.api.providers import ProviderError, get_llm_provider

    try:
        provider = get_llm_provider("research", settings)
    except ProviderError as exc:
        note = f"Return intel enrichment skipped: {exc}"
        error_console.print(f"[yellow]{note}[/yellow]")
        error_console.print(
            "[yellow]The watchlist below is built from FPL availability news alone.[/yellow]",
        )
        return _Enrichment(result=result, requested=True, note=note)

    from fpl_cli.services.returnee_radar import (
        apply_enrichment,
        load_enrichment_cache,
        save_enrichment_cache,
        select_enrichment_shortlist,
    )

    gameweek = inputs["next_gw_id"]
    shortlist = select_enrichment_shortlist(result.entries, config=config)
    intel = load_enrichment_cache(gameweek=gameweek)
    pending = [entry for entry in shortlist if entry.player_id not in intel]

    try:
        outcomes = await _gather_intel(
            provider, pending, gameweeks=inputs["gameweeks"], gameweek=gameweek, config=config,
        )
        outcomes = await _retry_rate_limited(
            provider, pending, outcomes,
            gameweeks=inputs["gameweeks"], gameweek=gameweek, config=config,
        )
    finally:
        await provider.close()

    failures: list[str] = []
    reason = ""
    rate_limited = False
    for entry, outcome in zip(pending, outcomes, strict=True):
        if outcome.found is None:
            failures.append(entry.web_name)
            # The last failure's message is the one reported, unless a rate
            # limit has been seen, which then holds: when the failures are
            # mixed it is the one the user can do something about.
            if outcome.rate_limited or not rate_limited:
                reason = outcome.error or reason
            rate_limited = rate_limited or outcome.rate_limited
            continue
        intel[entry.player_id] = outcome.found

    saved = False
    if len(failures) < len(pending):
        try:
            save_enrichment_cache(intel, gameweek=gameweek)
            saved = True
        except OSError as exc:
            # The intel is already in hand; losing the write costs a repeat
            # query next run, not this run's output.
            logger.warning("Could not cache the returnee enrichment: %s", exc)

    entries = apply_enrichment(
        result.entries, intel, next_gw_id=gameweek, config=config,
    )
    note = None
    if failures:
        note = (
            f"Return intel could not be fetched for {len(failures)} player(s) "
            f"({', '.join(failures)}): {reason or 'the search failed'}"
        )
        if rate_limited:
            # Only promise a cache that was actually written: a run refused
            # for every pending player has nothing to serve next time.
            note += ". The search provider is rate-limiting this run; " + (
                "the answers it did give are cached, so re-running with --enrich "
                "in a minute fills in the rest."
                if saved else
                "re-running with --enrich in a minute tries again."
            )
        error_console.print(f"[yellow]{note}[/yellow]")
    return _Enrichment(
        result=replace(result, entries=entries),
        requested=True,
        available=True,
        note=note,
        count=sum(1 for entry in entries if entry.enrichment is not None),
        rate_limited=rate_limited,
    )


@dataclass(frozen=True)
class _IntelOutcome:
    """One query's answer, or why there is none.

    `rate_limited` marks the one failure worth a second pass: the provider
    refused the query even after its own backoff, and `retry_after` is how
    long it asked for, when it said.
    """

    found: EnrichedReturn | None = None
    error: str | None = None
    rate_limited: bool = False
    retry_after: float | None = None


# Before one more pass over a rate-limited subset, wait at least this long:
# the provider layer's own backoff has just been spent, so a pass straight
# after it would land in the same quota window...
_RATE_LIMIT_PAUSE = 15.0
# ...and never longer than this, whatever Retry-After asked for. A CLI run is
# interactive; past this the subset is reported as rate-limited instead.
_MAX_RATE_LIMIT_PAUSE = 60.0


async def _gather_intel(
    provider: Any,
    entries: Sequence[RadarEntry],
    *,
    gameweeks: list[dict[str, Any]],
    gameweek: int,
    config: RadarConfig,
) -> list[_IntelOutcome]:
    """Query every shortlisted player, paced, in the order given.

    Each query is a multi-second round trip that depends on nothing but its own
    player, so running them one after another spent the shortlist's latency
    needlessly -- but a provider quota counts starts per minute, not queries in
    flight, so a bare concurrency cap still let a shortlist arrive as a burst
    (#184). Two knobs bound it: how many queries are in flight at once, and the
    least time between two starts. Results come back positionally, so a
    failure still lines up with the player it belongs to.
    """
    from fpl_cli.api.providers import QueryPacer

    limit = asyncio.Semaphore(max(1, config.enrich_concurrency))
    pacer = QueryPacer(config.enrich_query_spacing_seconds)

    async def _one(entry: RadarEntry) -> _IntelOutcome:
        # Slot first, then turn. The quota counts actual starts, so the
        # spacing has to separate those: a turn taken before the slot could
        # sit waiting for one and then start inside the next turn's spacing.
        # The slot is also held through the provider's own 429 backoff on
        # purpose -- a query waiting out a rate limit must not free a slot
        # for another to walk into the same wall.
        async with limit:
            await pacer.wait_turn()
            return await _query_intel(
                provider, entry, gameweeks=gameweeks, gameweek=gameweek,
            )

    return list(await asyncio.gather(*(_one(entry) for entry in entries)))


async def _retry_rate_limited(
    provider: Any,
    entries: Sequence[RadarEntry],
    outcomes: Sequence[_IntelOutcome],
    *,
    gameweeks: list[dict[str, Any]],
    gameweek: int,
    config: RadarConfig,
) -> list[_IntelOutcome]:
    """One more pass over the entries the provider rate-limited, after a pause.

    Each query has already retried its own 429s, but concurrent retries can
    spend each other's attempts inside the same quota window. By the time the
    whole shortlist has settled the window has usually moved on, so one pass
    over just the refused subset is what turns a lost player into a few extra
    seconds. Once only: a subset still refused after this is reported as
    rate-limited rather than waited out further.
    """
    refused = [index for index, outcome in enumerate(outcomes) if outcome.rate_limited]
    if not refused:
        return list(outcomes)

    pause = _rate_limit_pause(outcomes[index] for index in refused)
    error_console.print(
        f"[yellow]The search was rate-limited for {len(refused)} player(s); "
        f"trying those again in {pause:.0f}s...[/yellow]"
    )
    await _pause(pause)
    retried = await _gather_intel(
        provider, [entries[index] for index in refused],
        gameweeks=gameweeks, gameweek=gameweek, config=config,
    )
    merged = list(outcomes)
    for index, outcome in zip(refused, retried, strict=True):
        merged[index] = outcome
    return merged


def _rate_limit_pause(outcomes: Iterable[_IntelOutcome]) -> float:
    """How long to wait before re-querying a rate-limited subset.

    At least `_RATE_LIMIT_PAUSE`, longer when the provider asked for it, and
    never past `_MAX_RATE_LIMIT_PAUSE`: a Retry-After beyond the cap is
    reported, not waited out.
    """
    hints = [outcome.retry_after for outcome in outcomes if outcome.retry_after is not None]
    return min(_MAX_RATE_LIMIT_PAUSE, max([_RATE_LIMIT_PAUSE, *hints]))


async def _pause(seconds: float) -> None:
    """The pause before the rate-limited re-query goes through here, so a test can make it free."""
    await asyncio.sleep(seconds)


async def _query_intel(
    provider: Any,
    entry: RadarEntry,
    *,
    gameweeks: list[dict[str, Any]],
    gameweek: int,
) -> _IntelOutcome:
    """One player's enrichment query, and why it failed if it did.

    A failed query is a missing top-up, never a failed run: the deterministic
    watchlist is already built and stands on its own (R7). A rate limit is
    kept apart from every other failure because it is the one a caller can
    expect to clear by waiting.
    """
    from fpl_cli.api.providers import RateLimitError
    from fpl_cli.prompts.returnees import (
        RETURNEE_ENRICHMENT_SYSTEM_PROMPT,
        build_returnee_enrichment_prompt,
    )
    from fpl_cli.services.returnee_radar import enrichment_from_response
    from fpl_cli.utils.time import now_uk

    prompt = build_returnee_enrichment_prompt(
        web_name=entry.web_name,
        team=entry.team_name,
        position=entry.position,
        status=_STATUS_LABELS.get(entry.status, entry.status),
        news=entry.signal.news,
        gameweek=gameweek,
        today=now_uk().date(),
        news_age_days=entry.signal.news_age_days,
        chance_of_playing=entry.chance_of_playing,
    )
    try:
        response = await provider.query(
            prompt=prompt, system_prompt=RETURNEE_ENRICHMENT_SYSTEM_PROMPT,
        )
    except RateLimitError as exc:
        logger.info("Return intel query rate-limited for %s: %s", entry.web_name, exc)
        return _IntelOutcome(error=str(exc), rate_limited=True, retry_after=exc.retry_after)
    except Exception as exc:  # noqa: BLE001 — graceful degradation: enrichment is a top-up
        logger.info("Return intel query failed for %s: %s", entry.web_name, exc)
        return _IntelOutcome(error=str(exc))
    return _IntelOutcome(found=enrichment_from_response(
        response.content, citations=response.citations, gameweeks=gameweeks,
    ))


# ---------------------------------------------------------------------------
# One assembled payload, rendered two ways
# ---------------------------------------------------------------------------


def assemble(
    result: RadarResult,
    *,
    gameweek: int,
    window: int,
    escalation_window: int,
    stash_upgrade_margin: float,
    quality_bar_available: bool,
    quality_bar_applied: bool,
    enrichment: _Enrichment,
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
        # The shorter window an entry's return has to land inside before it is
        # worth holding a squad place for. Published so a consumer can state
        # the escalation rule without re-deriving the arithmetic.
        "escalation_window": escalation_window,
        # Quality points a returnee must beat the incumbent by before a stash is
        # worth a squad place. Published so a consumer applies the configured
        # margin instead of reading settings.yaml itself and guessing when it
        # cannot -- the gate is refusable only if the number is actually known.
        "stash_upgrade_margin": stash_upgrade_margin,
        "transitions_available": result.transitions_available,
        # The gameweek the Change column is measured against, null when
        # nothing older than this gameweek is stored. Published so a
        # consumer states what moved since, rather than assuming last week.
        "transitions_baseline_gameweek": result.baseline_gameweek,
        "quality_bar_available": quality_bar_available,
        "quality_bar_applied": quality_bar_applied,
        "enrichment_requested": enrichment.requested,
        "enrichment_available": enrichment.available,
        "enrichment_note": enrichment.note,
        "enrichment_count": enrichment.count,
        # True when a player went unanswered because the provider rate-limited
        # the run even after retrying, as distinct from having no answer: a
        # consumer deciding whether to re-run reads this, not the prose.
        "enrichment_rate_limited": enrichment.rate_limited,
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
        # Enriched intel sits beside the FPL signal above, never over it (R8):
        # where both state a date, both are carried.
        "enrichment": _enrichment_data(entry.enrichment),
        # Whether this entry's return lands inside the escalation window, and
        # whose date says so (R16). An enrichment date only counts here when it
        # arrived with a source citation.
        "escalation_eligible": entry.escalation_eligible,
        "escalation_basis": entry.escalation_basis,
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


def _enrichment_data(enrichment: EnrichedReturn | None) -> dict[str, Any] | None:
    """Enriched return intel as plain data, or None when none was found."""
    if enrichment is None:
        return None
    return {
        "source": enrichment.source,
        "expected_return": (
            enrichment.return_date.isoformat() if enrichment.return_date else None
        ),
        "return_gameweek": enrichment.return_gameweek,
        "summary": enrichment.summary,
        "confidence": enrichment.confidence,
        "citations": list(enrichment.citations),
        "cited": enrichment.cited,
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

    # The intel column only earns its width when something came back, so an
    # unenriched run renders exactly the table it always did.
    show_intel = any(entry["enrichment"] for entry in entries)

    table = Table(title=f"Returnee Radar (GW{gameweek}, next {window} gameweeks)")
    table.add_column("Player", style="bold")
    table.add_column("Team")
    table.add_column("Pos", width=3)
    table.add_column("Price", justify="right")
    table.add_column("Status")
    table.add_column("Expected Return")
    if show_intel:
        table.add_column("Searched Intel")
    table.add_column("Chance", justify="right")
    table.add_column("Quality", justify="right")
    table.add_column("Change")

    for entry in entries:
        row = [
            entry["web_name"],
            entry["team"] or "-",
            entry["position"],
            f"£{entry['price']:.1f}m",
            entry["status_label"],
            _return_cell(entry),
        ]
        if show_intel:
            row.append(_intel_cell(entry["enrichment"]))
        row.extend((
            _chance_cell(entry["chance_of_playing"]),
            _quality_cell(entry["quality"]),
            _transition_cell(entry["transition"], metadata["transitions_available"]),
        ))
        table.add_row(*row)

    console.print(table)

    if any(entry["quality"]["meets_stash"] for entry in entries):
        console.print(
            "[dim]* clears the higher bar worth holding a squad place for.[/dim]",
        )
    if show_intel:
        console.print(
            "[dim]Searched intel is web-search return timing, shown beside the FPL news "
            "rather than in place of it.[/dim]",
        )
        if any(
            entry["enrichment"] and not entry["enrichment"]["cited"] for entry in entries
        ):
            console.print(
                "[dim]Intel that came back without a source citation is marked, and is "
                "not enough on its own to drop a player for.[/dim]",
            )
    if not metadata["transitions_available"]:
        console.print(
            "[dim]No watchlist stored from an earlier gameweek to compare against, "
            "so week-over-week changes appear from the next gameweek.[/dim]",
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


def _intel_cell(intel: dict[str, Any] | None) -> str:
    """Searched return timing, with its date and whether it was sourced.

    Never merged into the FPL cell beside it: where both state a date, the
    reader sees both and can tell which is which.
    """
    if not intel:
        return "[dim]-[/dim]"
    stated = intel["expected_return"]
    if stated:
        gameweek = intel["return_gameweek"]
        text = _format_return_date(stated)
        cell = f"{text} (GW{gameweek})" if gameweek else text
    else:
        cell = intel["summary"][:40] or _UNKNOWN_RETURN
    return cell if intel["cited"] else f"[yellow]{cell} (uncited)[/yellow]"


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
