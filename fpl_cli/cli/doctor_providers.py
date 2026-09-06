"""Live provider probes for `fpl doctor --providers`.

The counterpart to doctor's local checks, one layer down (#97): the six
external data sources honour no versioned contract, so the only way to
know one has drifted is to ask it. Each probe asserts shape and volume —
not just reachability — against the same column constants the parsers
index with. Where a column check is not the contract, the probe runs the
parser itself and asserts it yields records (#142): the Core-Insights
per-GW files passed every shape test while the runtime read them as zero,
so a probe that re-implements a weaker check is a probe that lies.

Status mapping keeps #57's taxonomy honest for providers:
  - BROKEN: reachable but the wrong shape — needs a code or upstream fix,
    and is what the scheduled CI probe fails on.
  - STALE: a publishing lag that self-corrects (e.g. the newest gameweek
    folder not uploaded yet).
  - UNCHECKED: the provider was unreachable, so drift could not be ruled
    out — transient, not actionable.

Fetches for the GitHub-hosted datasets go through the same disk cache the
real commands use, with a zero TTL so every probe revalidates against
upstream (an ETag match costs a conditional request, not a download).
"""
# Pattern: direct-api

from __future__ import annotations

import asyncio
import csv
import dataclasses
import io
import json
from datetime import timedelta
from typing import TYPE_CHECKING, Any

import httpx

from fpl_cli.api.contract import missing_columns
from fpl_cli.cli.doctor import CheckResult, CheckStatus
from fpl_cli.paths import UserDirError
from fpl_cli.season import TOTAL_GAMEWEEKS
from fpl_cli.utils.teams import describe_team_set_mismatch

if TYPE_CHECKING:
    from fpl_cli.api.core_insights import PlayerLookup
    from fpl_cli.api.dataset_fetcher import DatasetFetcher

# Force revalidation on every probe: serving a cached copy inside its TTL
# would validate our own cache, not the provider.
PROBE_TTL = timedelta(0)

# Volume sanity floors. Bootstrap carries ~600-800 players depending on the
# point in the season; a value under the floor means a truncated or wrong
# payload, not a quiet transfer window.
ELEMENTS_FLOOR = 400
CSV_ROW_FLOOR = 400

# Understat only lists a club once it has ingested a match for it, so early
# season an unresolved name may be lag rather than a stale TEAM_NAME_MAP.
# After this many finished gameweeks, every club must resolve.
UNDERSTAT_SETTLED_GWS = 3

# The share of players FPL says have played that the live pool is allowed to
# not join by name. A few percent always miss -- players Understat carries no
# row for, cameos in a gameweek it has not ingested, a name the two sources
# genuinely spell differently -- so the ceiling sits well above that: what it
# catches is the two sides ceasing to agree about names wholesale, which is
# what a renamed payload key or a laundered character class looks like. The
# rate is reported either way, because a drift worth noticing shows up in the
# number long before it crosses any threshold (#263).
UNDERSTAT_NAME_MISS_CEILING = 0.20


def _unreachable(exc: httpx.HTTPError) -> str:
    if isinstance(exc, httpx.HTTPStatusError):
        return f"the provider returned HTTP {exc.response.status_code} — could not check"
    return f"could not reach the provider ({exc.__class__.__name__}) — could not check"


def _csv_check(
    name: str,
    filename: str,
    text: str,
    required: frozenset[str],
    *,
    row_floor: int,
) -> CheckResult:
    """Shape-and-volume check for one fetched CSV."""
    reader = csv.DictReader(io.StringIO(text))
    missing = missing_columns(reader.fieldnames, required)
    if missing:
        return CheckResult(
            name,
            CheckStatus.BROKEN,
            f"{filename} is missing column(s) {', '.join(sorted(missing))}",
        )
    rows = sum(1 for _ in reader)
    if rows < row_floor:
        return CheckResult(
            name,
            CheckStatus.BROKEN,
            f"{filename} has {rows} row(s) — expected at least {row_floor}",
        )
    return CheckResult(
        name, CheckStatus.OK, f"{filename}: {rows} rows, all expected columns present"
    )


# ---------------------------------------------------------------------------
# FPL API
# ---------------------------------------------------------------------------


def _expected_player_keys() -> list[str]:
    """The JSON keys the Player model reads from a bootstrap element.

    Derived from the model itself so probe and model cannot drift: every
    field has the silent-default trap — only 7 of ~50 are required, so a
    renamed upstream key validates cleanly and zeroes the stat for every
    player.
    """
    from fpl_cli.models.player import Player

    return [field.alias or name for name, field in Player.model_fields.items()]


async def _fpl_checks() -> tuple[list[CheckResult], dict[str, Any] | None]:
    from fpl_cli.api.fpl import FPLClient

    name = "FPL bootstrap"
    async with FPLClient() as client:
        try:
            bootstrap = await client.get_bootstrap_static()
        except httpx.HTTPError as exc:
            return [CheckResult(name, CheckStatus.UNCHECKED, _unreachable(exc))], None
        except json.JSONDecodeError:
            return [
                CheckResult(
                    name,
                    CheckStatus.BROKEN,
                    "bootstrap-static did not return JSON — the API shape may have changed",
                )
            ], None

    results: list[CheckResult] = []
    teams = bootstrap.get("teams") or []
    elements = bootstrap.get("elements") or []
    events = bootstrap.get("events") or []

    problems: list[str] = []
    if len(teams) != 20:
        problems.append(f"{len(teams)} teams (expected 20)")
    if len(elements) < ELEMENTS_FLOOR:
        problems.append(f"{len(elements)} players (expected at least {ELEMENTS_FLOOR})")
    if len(events) != TOTAL_GAMEWEEKS:
        problems.append(f"{len(events)} gameweeks (expected {TOTAL_GAMEWEEKS})")
    if problems:
        results.append(CheckResult(name, CheckStatus.BROKEN, "; ".join(problems)))
    else:
        results.append(
            CheckResult(
                name,
                CheckStatus.OK,
                f"{len(teams)} teams, {len(elements)} players, {len(events)} gameweeks",
            )
        )

    fields_name = "FPL player fields"
    if elements:
        expected = _expected_player_keys()
        missing = [key for key in expected if key not in elements[0]]
        if missing:
            results.append(
                CheckResult(
                    fields_name,
                    CheckStatus.BROKEN,
                    f"bootstrap players are missing {', '.join(sorted(missing))} — "
                    "these stats would silently read as 0 for every player",
                )
            )
        else:
            results.append(
                CheckResult(
                    fields_name,
                    CheckStatus.OK,
                    f"all {len(expected)} player fields present in the raw data",
                )
            )
    return results, bootstrap


# ---------------------------------------------------------------------------
# FPL Draft API
# ---------------------------------------------------------------------------


async def _draft_checks() -> list[CheckResult]:
    from fpl_cli.api.fpl_draft import FPLDraftClient

    name = "Draft bootstrap"
    async with FPLDraftClient() as client:
        try:
            bootstrap = await client.get_bootstrap_static()
        except httpx.HTTPError as exc:
            return [CheckResult(name, CheckStatus.UNCHECKED, _unreachable(exc))]
        except json.JSONDecodeError:
            return [
                CheckResult(
                    name,
                    CheckStatus.BROKEN,
                    "bootstrap-static did not return JSON — the API shape may have changed",
                )
            ]

    teams = bootstrap.get("teams") or []
    elements = bootstrap.get("elements") or []
    problems: list[str] = []
    if len(teams) != 20:
        problems.append(f"{len(teams)} teams (expected 20)")
    if len(elements) < ELEMENTS_FLOOR:
        problems.append(f"{len(elements)} players (expected at least {ELEMENTS_FLOOR})")
    if problems:
        return [CheckResult(name, CheckStatus.BROKEN, "; ".join(problems))]
    return [
        CheckResult(name, CheckStatus.OK, f"{len(teams)} teams, {len(elements)} players")
    ]


# ---------------------------------------------------------------------------
# Vaastav GitHub dataset (historical seasons)
# ---------------------------------------------------------------------------


async def _vaastav_season_check(fetcher: DatasetFetcher, season: str) -> CheckResult:
    from fpl_cli.api.vaastav import PLAYERS_RAW_REQUIRED_COLUMNS

    name = f"vaastav {season}"
    try:
        text = await fetcher.get(f"{season}/players_raw.csv", ttl=PROBE_TTL)
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 404:
            return CheckResult(
                name,
                CheckStatus.BROKEN,
                "players_raw.csv is missing upstream — the season directory may have moved",
            )
        return CheckResult(name, CheckStatus.UNCHECKED, _unreachable(exc))
    except httpx.HTTPError as exc:
        return CheckResult(name, CheckStatus.UNCHECKED, _unreachable(exc))
    return _csv_check(
        name, "players_raw.csv", text, PLAYERS_RAW_REQUIRED_COLUMNS, row_floor=CSV_ROW_FLOOR
    )


async def _vaastav_checks() -> list[CheckResult]:
    from fpl_cli.api.historical import historical_season_windows
    from fpl_cli.api.vaastav import make_vaastav_fetcher

    # The allocation make_historical_provider reads from, so the probe cannot
    # drift from the seasons the runtime actually fetches (#101).
    seasons = historical_season_windows().vaastav
    fetcher = make_vaastav_fetcher()
    try:
        results = await asyncio.gather(*(_vaastav_season_check(fetcher, s) for s in seasons))
    finally:
        await fetcher.close()
    return list(results)


# ---------------------------------------------------------------------------
# Core-Insights GitHub dataset (last season and the season in progress)
# ---------------------------------------------------------------------------


def _ci_missing_reason(season: str, *, current: bool) -> str:
    """What a 404 on a season's root files costs, and why it might happen.

    Core-Insights is the sole source for both seasons it serves. The season
    in progress may simply not have its directory yet at rollover; a
    completed season has no such excuse.
    """
    if current:
        return (
            "the sole current-season source; the season directory may not exist "
            "yet or may have moved"
        )
    return f"the sole source for {season} player history; the season directory may have moved"


async def _ci_fetch_text(
    fetcher: DatasetFetcher, name: str, path: str, filename: str, *, missing: str
) -> tuple[str | None, CheckResult | None]:
    """Fetch one Core-Insights file, or the CheckResult explaining why not."""
    try:
        return await fetcher.get(path, ttl=PROBE_TTL), None
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 404:
            return None, CheckResult(
                name, CheckStatus.BROKEN, f"{filename} is missing upstream — {missing}"
            )
        return None, CheckResult(name, CheckStatus.UNCHECKED, _unreachable(exc))
    except httpx.HTTPError as exc:
        return None, CheckResult(name, CheckStatus.UNCHECKED, _unreachable(exc))


async def _ci_file_check(
    fetcher: DatasetFetcher,
    name: str,
    path: str,
    filename: str,
    required: frozenset[str],
    *,
    row_floor: int,
    missing: str,
) -> CheckResult:
    text, failure = await _ci_fetch_text(fetcher, name, path, filename, missing=missing)
    if failure is not None:
        return failure
    assert text is not None  # narrowed by failure is None
    return _csv_check(name, filename, text, required, row_floor=row_floor)


async def _ci_players_check(
    fetcher: DatasetFetcher, season: str, *, missing: str
) -> tuple[CheckResult, dict[int, PlayerLookup]]:
    """players.csv: shape, volume, and the identity join every parse below needs.

    The join is the point (#142). players.csv resolves the player_id every
    other Core-Insights file carries, so a file with the right columns whose
    ids parse to nothing empties the per-GW records with no column to blame —
    and the per-GW probe would otherwise report that as the per-GW files
    breaking. The lookup it returns is what those probes join against.
    """
    from fpl_cli.api.core_insights import (
        PLAYERS_CSV_REQUIRED_COLUMNS,
        parse_player_lookup,
        season_dir,
    )

    name = f"Core-Insights {season} players.csv"
    text, failure = await _ci_fetch_text(
        fetcher, name, f"{season_dir(season)}/players.csv", "players.csv", missing=missing
    )
    if failure is not None:
        return failure, {}
    assert text is not None  # narrowed by failure is None

    shape = _csv_check(
        name, "players.csv", text, PLAYERS_CSV_REQUIRED_COLUMNS, row_floor=CSV_ROW_FLOOR
    )
    if shape.status is not CheckStatus.OK:
        return shape, {}

    lookup, rows_read = parse_player_lookup(text)
    if not lookup:
        return (
            CheckResult(
                name,
                CheckStatus.BROKEN,
                f"players.csv has {rows_read} row(s) with every expected column but "
                "none parse into a player — every join onto it is empty",
            ),
            {},
        )
    return (
        CheckResult(
            name,
            CheckStatus.OK,
            f"players.csv: {rows_read} rows, {len(lookup)} players resolve for the join",
        ),
        lookup,
    )


async def _ci_gw_file_text(fetcher: DatasetFetcher, path: str) -> str | None:
    """Contents of one per-GW CSV, or None when it 404s upstream."""
    try:
        return await fetcher.get(path, ttl=PROBE_TTL)
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 404:
            return None
        raise


def _ci_gw_paths(season: str, gw: int) -> list[tuple[str, str, frozenset[str]]]:
    from fpl_cli.api.core_insights import (
        GW_STATS_REQUIRED_COLUMNS,
        MATCHES_REQUIRED_COLUMNS,
        PLAYERMATCHSTATS_REQUIRED_COLUMNS,
    )

    tournament = f"{season}/By Tournament/Premier League/GW{gw}"
    return [
        ("matches.csv", f"{tournament}/matches.csv", MATCHES_REQUIRED_COLUMNS),
        (
            "playermatchstats.csv",
            f"{tournament}/playermatchstats.csv",
            PLAYERMATCHSTATS_REQUIRED_COLUMNS,
        ),
        (
            "player_gameweek_stats.csv",
            f"{season}/By Gameweek/GW{gw}/player_gameweek_stats.csv",
            GW_STATS_REQUIRED_COLUMNS,
        ),
    ]


# The per-GW files parse in two units, each feeding signals of its own: the
# match join wants both By Tournament files, the gameweek stats stand alone.
# A unit is what persistence is judged on — how it failed matters to the
# message, not to whether the gameweek yielded anything.
MATCH_JOIN_UNIT = "matches.csv + playermatchstats.csv"
GW_STATS_UNIT = "player_gameweek_stats.csv"
GW_UNIT_SIGNALS = {
    MATCH_JOIN_UNIT: "opponent-adjusted xG signals",
    GW_STATS_UNIT: "price-trend and transfer-momentum signals",
}
FILE_UNITS = {
    "matches.csv": MATCH_JOIN_UNIT,
    "playermatchstats.csv": MATCH_JOIN_UNIT,
    "player_gameweek_stats.csv": GW_STATS_UNIT,
}


@dataclasses.dataclass(frozen=True)
class _GwProbe:
    """What one gameweek's per-GW files look like to the runtime parsers."""

    absent: list[str]
    column_problems: list[str]
    empty: list[str]
    unknown: list[str]
    match_records: int
    gw_stat_rows: int

    @property
    def unusable_units(self) -> set[str]:
        """Units this gameweek yields nothing from, however they failed.

        Absent and empty are the same outcome one gameweek later — the data
        is not there — so persistence is judged on the unit, not on the
        failure kind. Comparing kind against kind would let a unit that
        404s one gameweek and parses to nothing the next read as two
        separate one-off lags.
        """
        return {FILE_UNITS[f] for f in self.absent} | set(self.empty)

    @property
    def unknown_units(self) -> set[str]:
        """Units carrying a file this probe could not fetch at all."""
        return {FILE_UNITS[f] for f in self.unknown}


async def _ci_probe_gw(
    fetcher: DatasetFetcher,
    season: str,
    gw: int,
    lookup: dict[int, PlayerLookup],
    *,
    tolerate_errors: bool = False,
) -> _GwProbe:
    """Fetch one gameweek's per-GW files and run the real parsers over them.

    Raises httpx.HTTPError for anything but a 404, which is the absence the
    caller classifies as publishing lag or layout change. Pass
    ``tolerate_errors`` when probing an earlier gameweek only to compare
    against: a file that will not fetch is recorded as unknown rather than
    thrown, so one unrelated file being down cannot discard a diagnosis the
    other files already support.
    """
    from fpl_cli.api.core_insights import parse_gw_stat_rows, parse_match_records

    files = _ci_gw_paths(season, gw)
    results = await asyncio.gather(
        *(_ci_gw_file_text(fetcher, path) for _, path, _ in files),
        return_exceptions=tolerate_errors,
    )
    unknown = [
        filename
        for (filename, _, _), result in zip(files, results)
        if isinstance(result, BaseException)
    ]
    texts: list[str | None] = [
        None if isinstance(result, BaseException) else result for result in results
    ]
    by_name = {filename: text for (filename, _, _), text in zip(files, texts)}

    # A file we could not fetch is unknown, not absent: it says nothing about
    # whether the gameweek published it.
    absent = [
        filename
        for (filename, _, _), text in zip(files, texts)
        if text is None and filename not in unknown
    ]
    column_problems = [
        f"{filename} is missing column(s) {', '.join(sorted(missing))}"
        for (filename, _, required), text in zip(files, texts)
        if text is not None
        and (
            missing := missing_columns(
                csv.DictReader(io.StringIO(text)).fieldnames, required
            )
        )
    ]

    matches_text = by_name["matches.csv"]
    stats_text = by_name["playermatchstats.csv"]
    gw_stats_text = by_name["player_gameweek_stats.csv"]

    match_records = 0
    if matches_text is not None and stats_text is not None:
        match_records = sum(
            len(records)
            for records in parse_match_records(matches_text, stats_text, lookup).values()
        )
    gw_stat_rows = 0
    if gw_stats_text is not None:
        gw_stat_rows = len(parse_gw_stat_rows(gw_stats_text, lookup)[0])

    empty: list[str] = []
    if matches_text is not None and stats_text is not None and not match_records:
        empty.append(MATCH_JOIN_UNIT)
    if gw_stats_text is not None and not gw_stat_rows:
        empty.append(GW_STATS_UNIT)

    return _GwProbe(absent, column_problems, empty, unknown, match_records, gw_stat_rows)


def _ci_empty_phrase(units: list[str]) -> str:
    """Name the parse that yielded nothing and the signals it costs."""
    signals = sorted({GW_UNIT_SIGNALS[unit] for unit in units})
    return (
        f"{', '.join(units)} parse to 0 records — every expected column is "
        f"present but no row survives the join, so {' and '.join(signals)} "
        "are unavailable"
    )


async def _ci_gw_check(
    fetcher: DatasetFetcher,
    season: str,
    latest_finished_gw: int,
    lookup: dict[int, PlayerLookup],
) -> CheckResult:
    """Probe the per-GW files at the latest finished gameweek.

    Shape alone is not the contract: #142 was a gameweek whose files carried
    every expected column and still parsed to zero records (Elo published
    blank at the start of a season), so the probe reported ok while every
    scoring command warned the signal was gone. It runs the runtime parsers
    here instead and asserts they yield something.

    A unit that yields nothing for the newest gameweek but is healthy for
    the previous one is a publishing lag (the dataset updates a few times a
    day and backfills) and self-corrects; a unit yielding nothing two
    gameweeks running is a break every future fetch will hit — whether it
    404s one gameweek and parses to nothing the next makes no difference to
    that, only to how the row reads.
    """
    name = "Core-Insights per-GW files"
    if not lookup:
        # Deliberately no cause: players.csv yields no lookup when it is
        # unreachable, when its columns drifted, and when its rows will not
        # parse. Its own row above names which — this one must not guess.
        return CheckResult(
            name,
            CheckStatus.UNCHECKED,
            "no players.csv lookup to join against (see the players.csv row), "
            "so the per-GW join could not be checked",
        )
    try:
        probe = await _ci_probe_gw(fetcher, season, latest_finished_gw, lookup)
    except httpx.HTTPError as exc:
        return CheckResult(name, CheckStatus.UNCHECKED, _unreachable(exc))

    if probe.column_problems:
        return CheckResult(name, CheckStatus.BROKEN, "; ".join(probe.column_problems))
    if not probe.absent and not probe.empty:
        return CheckResult(
            name,
            CheckStatus.OK,
            f"GW{latest_finished_gw}: all per-GW files present, parsing to "
            f"{probe.match_records} player-match records and "
            f"{probe.gw_stat_rows} gameweek stat rows",
        )

    # Something is wrong at the newest finished GW — decide lag vs break by
    # whether the previous gameweek yielded anything from the same unit. At
    # the season's first finished gameweek there is nothing to compare
    # against, so a backfill in progress and a break look identical: report
    # the lag. Errors there are tolerated per file rather than thrown: an
    # unrelated file being down must not discard the diagnosis in hand.
    previous_gw = latest_finished_gw - 1
    previous: _GwProbe | None = None
    if previous_gw >= 1:
        previous = await _ci_probe_gw(
            fetcher, season, previous_gw, lookup, tolerate_errors=True
        )

    broken_units = probe.unusable_units & (
        previous.unusable_units if previous is not None else set()
    )
    if broken_units:
        problems = []
        absent_now = [f for f in probe.absent if FILE_UNITS[f] in broken_units]
        empty_now = [u for u in probe.empty if u in broken_units]
        if absent_now:
            problems.append(
                f"{', '.join(absent_now)} missing upstream — the per-GW folder "
                "layout may have changed"
            )
        if empty_now:
            problems.append(_ci_empty_phrase(empty_now))
        # previous is not None whenever broken_units is non-empty.
        assert previous is not None
        prev_cause = (
            f" ({'; '.join(previous.column_problems)})"
            if previous.column_problems
            else ""
        )
        return CheckResult(
            name,
            CheckStatus.BROKEN,
            f"GW{latest_finished_gw}: {'; '.join(problems)}; GW{previous_gw} yields "
            f"nothing either{prev_cause}, so this is not publishing lag",
        )

    lag = []
    if probe.absent:
        lag.append(f"{', '.join(probe.absent)} not published upstream yet")
    if probe.empty:
        lag.append(_ci_empty_phrase(probe.empty))
    # Say so when the comparison gameweek could not settle it, rather than
    # letting "self-corrects" imply a check that never happened.
    unconfirmed = previous is not None and bool(
        probe.unusable_units & previous.unknown_units
    )
    caveat = (
        f"GW{previous_gw} could not be fetched to confirm, so this may already "
        "be broken"
        if unconfirmed
        else "the dataset updates a few times a day and backfills, so this "
        "self-corrects; broken if it persists"
    )
    return CheckResult(
        name,
        CheckStatus.STALE,
        f"GW{latest_finished_gw}: {'; '.join(lag)} — {caveat}",
    )


async def _core_insights_checks(
    latest_finished_gw: int | None, *, bootstrap_available: bool
) -> list[CheckResult]:
    from fpl_cli.api.core_insights import (
        PLAYERSTATS_REQUIRED_COLUMNS,
        make_core_insights_fetcher,
        season_dir,
    )
    from fpl_cli.api.historical import historical_season_windows

    # The allocation make_historical_provider reads from (#101): the root
    # files of every season it serves, the per-GW files only for the season
    # in progress, the one season the runtime reads them for.
    seasons = historical_season_windows().core_insights
    current = seasons[-1]
    fetcher = make_core_insights_fetcher()
    results: list[CheckResult] = []
    lookup: dict[int, PlayerLookup] = {}
    try:
        for season in seasons:
            missing = _ci_missing_reason(season, current=season == current)
            players_result, season_lookup = await _ci_players_check(
                fetcher, season, missing=missing
            )
            results.append(players_result)
            results.append(
                await _ci_file_check(
                    fetcher,
                    f"Core-Insights {season} playerstats.csv",
                    f"{season_dir(season)}/playerstats.csv",
                    "playerstats.csv",
                    PLAYERSTATS_REQUIRED_COLUMNS,
                    row_floor=CSV_ROW_FLOOR,
                    missing=missing,
                )
            )
            if season == current:
                lookup = season_lookup
        if latest_finished_gw is None:
            results.append(
                CheckResult(
                    "Core-Insights per-GW files",
                    CheckStatus.SKIPPED
                    if bootstrap_available
                    else CheckStatus.UNCHECKED,
                    "no finished gameweek yet — nothing to probe"
                    if bootstrap_available
                    else "could not determine the current gameweek (FPL API unreachable)",
                )
            )
        else:
            results.append(
                await _ci_gw_check(fetcher, season_dir(current), latest_finished_gw, lookup)
            )
    finally:
        await fetcher.close()
    return results



# ---------------------------------------------------------------------------
# Understat
# ---------------------------------------------------------------------------


def _understat_team_titles(players: list[dict[str, Any]]) -> set[str]:
    """Distinct team names in Understat's own data, for the coverage summary.

    A player who moved clubs mid-season carries a comma-joined title
    ("Chelsea,Fulham"), so titles are split before collecting. Descriptive
    only: whether an FPL club resolves is `understat_club_rows`'s answer, not
    this set's — the probe asking it here in its own words is how a probe and
    the runtime start disagreeing (#229).
    """
    from fpl_cli.api.understat import split_team_titles

    titles: set[str] = set()
    for player in players:
        titles.update(part for part in split_team_titles(str(player.get("team", ""))) if part)
    return titles


def _understat_name_join_check(
    understat_players: list[dict[str, Any]],
    elements: list[dict[str, Any]],
    team_name_by_id: dict[int, str],
    unresolved: set[str],
    finished_gws: int,
) -> CheckResult:
    """How much of the pool joins player by player, not just club by club.

    The club check catches a club dropping out whole. #263 was the other
    shape: three players whose club resolved perfectly and whose names could
    never match, because Understat HTML-escapes an apostrophe and the matcher
    laundered the entity into a digit token. Nothing saw it -- this probe
    reported all 20 clubs healthy throughout, because resolving a club says
    nothing about whether its players' names line up.

    Asked of `match_fpl_to_understat` itself, on the pool the scoring commands
    scan, for the same reason the club check is asked of the club gate: a rate
    computed beside the matcher is free to drift from the matcher's own answer
    and report health it does not have (#229).
    """
    from fpl_cli.api.understat import (
        match_fpl_to_understat,
        reset_understat_join_warnings,
        understat_name_join_stats,
    )
    from fpl_cli.models.player import POSITION_MAP

    name = "Understat name join"

    # A club the map fails to resolve is the check above's finding, and every
    # one of its players would arrive here as a name miss -- one club gap
    # restated as a squad's worth, burying the handful of real ones. Skipping
    # them also keeps that check's own warning off doctor's stderr.
    candidates = [
        (el, team)
        for el in elements
        if int(el.get("minutes") or 0) > 0
        and (team := team_name_by_id.get(int(el.get("team") or 0))) is not None
        and team not in unresolved
    ]
    if not candidates:
        return CheckResult(
            name, CheckStatus.SKIPPED, "no players with minutes at a resolved club yet"
        )

    # The tally is process-global, so it is reset here to measure this probe
    # alone rather than whatever ran before it.
    reset_understat_join_warnings()
    for el, team in candidates:
        match_fpl_to_understat(
            str(el.get("web_name") or ""),
            team,
            understat_players,
            fpl_position=POSITION_MAP.get(int(el.get("element_type") or 0)),
            fpl_minutes=int(el.get("minutes") or 0),
        )

    stats = understat_name_join_stats()
    attempted, matched, missed = stats["attempted"], stats["matched"], stats["missed"]
    joined = f"{matched} of {attempted} players with minutes join by name"
    if missed:
        sample = "; ".join(stats["unmatched"][:5])
        joined += f" — no row for {sample}" + (", …" if missed > 5 else "")

    if stats["miss_rate"] <= UNDERSTAT_NAME_MISS_CEILING:
        return CheckResult(name, CheckStatus.OK, joined)

    pct = f"{stats['miss_rate']:.0%}"
    if finished_gws < UNDERSTAT_SETTLED_GWS:
        return CheckResult(
            name,
            CheckStatus.STALE,
            f"{pct} of players with minutes match no Understat row — early season, "
            f"may be ingestion lag; broken if it persists ({joined})",
            "if it persists, check how Understat is spelling names "
            "(fpl_cli/api/understat.py)",
        )
    return CheckResult(
        name,
        CheckStatus.BROKEN,
        f"{pct} of players with minutes match no Understat row — these players "
        f"silently lose xG enrichment ({joined})",
        "compare a failing name against Understat's own spelling: the payload is "
        "HTML-escaped, so a character the matcher does not treat as a separator "
        "fails every name carrying it (_normalise in fpl_cli/api/understat.py)",
    )


async def _understat_checks(
    team_names: list[str] | None,
    finished_gws: int,
    *,
    bootstrap_available: bool,
    elements: list[dict[str, Any]] | None = None,
    team_name_by_id: dict[int, str] | None = None,
) -> list[CheckResult]:
    from fpl_cli.api.understat import UnderstatClient, understat_club_rows

    league_name = "Understat league data"
    map_name = "Understat team map"

    async with UnderstatClient() as client:
        try:
            players = await client.get_league_players()
        except httpx.HTTPError as exc:
            return [CheckResult(league_name, CheckStatus.UNCHECKED, _unreachable(exc))]
        except json.JSONDecodeError:
            return [
                CheckResult(
                    league_name,
                    CheckStatus.BROKEN,
                    "league endpoint did not return JSON — the endpoint shape may have changed",
                )
            ]

    results: list[CheckResult] = []
    if not players:
        if not bootstrap_available:
            # finished_gws defaults to 0 when the FPL bootstrap was
            # unreachable, which must not read as "season not started":
            # that would classify a genuinely drifted empty response as
            # skipped and let one provider being down mask another broken.
            results.append(
                CheckResult(
                    league_name,
                    CheckStatus.UNCHECKED,
                    "no player data, and could not determine whether the season "
                    "has started (FPL API unreachable)",
                )
            )
        elif finished_gws == 0:
            results.append(
                CheckResult(
                    league_name,
                    CheckStatus.SKIPPED,
                    "no player data yet — Understat publishes once matches are played",
                )
            )
        else:
            results.append(
                CheckResult(
                    league_name,
                    CheckStatus.BROKEN,
                    "league endpoint returned no players mid-season — "
                    "the endpoint shape may have changed",
                )
            )
        results.append(
            CheckResult(map_name, CheckStatus.UNCHECKED, "no Understat data to resolve against")
        )
        results.append(
            CheckResult(
                "Understat name join", CheckStatus.UNCHECKED, "no Understat data to join against"
            )
        )
        return results

    titles = _understat_team_titles(players)
    results.append(
        CheckResult(
            league_name, CheckStatus.OK, f"{len(players)} players across {len(titles)} teams"
        )
    )

    if team_names is None:
        results.append(
            CheckResult(
                map_name,
                CheckStatus.UNCHECKED,
                "could not fetch the live team list to compare against",
            )
        )
        results.append(
            CheckResult(
                "Understat name join",
                CheckStatus.UNCHECKED,
                "could not fetch the live team list to join against",
            )
        )
        return results

    # End-to-end join check, run through the enrichment's own club gate: each
    # FPL club must name at least one row in the very list the scoring commands
    # scan. Key coverage alone proves nothing — an unmapped club whose names
    # agree still joins, and a mapped club can still miss — and a set of titles
    # collected here rather than asked of the matcher is a second copy of the
    # gate, free to drift from the one that decides (#229).
    unresolved = sorted(t for t in team_names if not understat_club_rows(t, players))

    # Appended after the club verdict below, but computed here so both read the
    # same `unresolved`: the name check is only meaningful for clubs that did
    # resolve.
    def _name_join() -> list[CheckResult]:
        if elements is None or team_name_by_id is None:
            return [
                CheckResult(
                    "Understat name join",
                    CheckStatus.UNCHECKED,
                    "could not fetch the live player list to join against",
                )
            ]
        return [
            _understat_name_join_check(
                players, elements, team_name_by_id, set(unresolved), finished_gws
            )
        ]

    if not unresolved:
        results.append(
            CheckResult(
                map_name,
                CheckStatus.OK,
                f"all {len(team_names)} clubs resolve to an Understat team",
            )
        )
    elif finished_gws < UNDERSTAT_SETTLED_GWS:
        results.append(
            CheckResult(
                map_name,
                CheckStatus.STALE,
                f"{', '.join(unresolved)} not in Understat's data yet — early season, "
                "may be ingestion lag; broken if it persists",
                "if it persists, update TEAM_NAME_MAP (fpl_cli/api/understat.py)",
            )
        )
    else:
        results.append(
            CheckResult(
                map_name,
                CheckStatus.BROKEN,
                f"{', '.join(unresolved)} do not resolve to any Understat team — "
                "these clubs' players silently lose xG enrichment",
                "update TEAM_NAME_MAP (fpl_cli/api/understat.py)",
            )
        )
    return results + _name_join()


# ---------------------------------------------------------------------------
# football-data.org
# ---------------------------------------------------------------------------


async def _football_data_checks(short_names: list[str] | None) -> list[CheckResult]:
    from fpl_cli.api.football_data import FootballDataClient
    from fpl_cli.services.team_ratings_prior import TLA_TO_FPL

    standings_name = "football-data standings"
    map_name = "football-data TLA map"

    async with FootballDataClient() as client:
        if not client.is_configured:
            return [
                CheckResult(
                    standings_name,
                    CheckStatus.SKIPPED,
                    "FOOTBALL_DATA_API_KEY not set — league table and the ratings-prior "
                    "fallback are unavailable",
                )
            ]
        try:
            rows = await client.get_standings(raise_on_error=True)
        except httpx.HTTPError as exc:
            return [CheckResult(standings_name, CheckStatus.UNCHECKED, _unreachable(exc))]
        except json.JSONDecodeError:
            return [
                CheckResult(
                    standings_name,
                    CheckStatus.BROKEN,
                    "standings endpoint did not return JSON — the API shape may have changed",
                )
            ]

    if not rows:
        return [
            CheckResult(
                standings_name,
                CheckStatus.BROKEN,
                "standings response held no TOTAL table — the response shape may have changed",
            )
        ]

    results: list[CheckResult] = []
    if len(rows) != 20:
        results.append(
            CheckResult(
                standings_name, CheckStatus.BROKEN, f"standings has {len(rows)} rows (expected 20)"
            )
        )
    else:
        results.append(CheckResult(standings_name, CheckStatus.OK, "standings has 20 rows"))

    if short_names is None:
        results.append(
            CheckResult(
                map_name,
                CheckStatus.UNCHECKED,
                "could not fetch the live team list to compare against",
            )
        )
        return results

    # End-to-end join-key check: every TLA football-data serves, mapped
    # through TLA_TO_FPL, must land on an FPL short name — and cover all 20.
    # Column checks cannot see this (#110's NOT/NFO passed every shape test);
    # this is the probe that would have caught it on its first run.
    mapped = {TLA_TO_FPL.get(str(r.get("short_name", "")), str(r.get("short_name", ""))) for r in rows}
    mismatch = describe_team_set_mismatch("TLA_TO_FPL", mapped, short_names, verb="maps")
    if mismatch:
        results.append(
            CheckResult(
                map_name,
                CheckStatus.BROKEN,
                f"{mismatch} — an unmapped club is silently re-rated as promoted",
                "add the TLA to TLA_TO_FPL (fpl_cli/services/team_ratings_prior.py)",
            )
        )
    else:
        results.append(
            CheckResult(
                map_name,
                CheckStatus.OK,
                "all 20 TLAs resolve to FPL short names through TLA_TO_FPL",
            )
        )
    return results


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


async def provider_checks() -> list[CheckResult]:
    """Run every provider probe, containing per-provider failures.

    The FPL bootstrap runs first because three later probes compare against
    the live team list and gameweek state; when it is unreachable those
    comparisons report unchecked rather than guessing.
    """
    fpl_results, bootstrap = await _fpl_checks()
    results = list(fpl_results)

    team_names: list[str] | None = None
    short_names: list[str] | None = None
    elements: list[dict[str, Any]] | None = None
    team_name_by_id: dict[int, str] | None = None
    latest_finished_gw: int | None = None
    finished_gws = 0
    if bootstrap is not None:
        teams = bootstrap.get("teams") or []
        team_names = [str(t.get("name", "")) for t in teams]
        short_names = [str(t.get("short_name", "")) for t in teams]
        elements = bootstrap.get("elements") or []
        team_name_by_id = {int(t.get("id", 0)): str(t.get("name", "")) for t in teams}
        finished = [
            int(e.get("id", 0)) for e in (bootstrap.get("events") or []) if e.get("finished")
        ]
        finished_gws = len(finished)
        latest_finished_gw = max(finished) if finished else None

    results += await _draft_checks()

    # The dataset fetchers resolve the cache dir themselves; an unusable
    # FPL_CLI_CACHE_DIR override becomes that provider's row instead of
    # aborting the whole probe (mirrors _file_checks in doctor.py).
    try:
        results += await _vaastav_checks()
    except UserDirError as exc:
        results.append(CheckResult("vaastav", CheckStatus.BROKEN, str(exc)))
    try:
        results += await _core_insights_checks(
            latest_finished_gw, bootstrap_available=bootstrap is not None
        )
    except UserDirError as exc:
        results.append(CheckResult("Core-Insights", CheckStatus.BROKEN, str(exc)))

    results += await _understat_checks(
        team_names,
        finished_gws,
        bootstrap_available=bootstrap is not None,
        elements=elements,
        team_name_by_id=team_name_by_id,
    )
    results += await _football_data_checks(short_names)
    return results
