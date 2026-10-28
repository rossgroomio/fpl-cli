"""Live provider probes for `fpl doctor --providers`.

The counterpart to doctor's local checks, one layer down (#97): the
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
    folder not uploaded yet), or an upstream gap the runtime degrades
    around rather than reads wrong (Elo published blank, #431).
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
from fpl_cli.api.understat import UNDERSTAT_NAME_MISS_CEILING, UNDERSTAT_SETTLED_GWS
from fpl_cli.cli.doctor import CheckResult, CheckStatus
from fpl_cli.paths import UserDirError
from fpl_cli.season import TOTAL_GAMEWEEKS, resolve_season_year
from fpl_cli.utils.teams import describe_team_set_mismatch

if TYPE_CHECKING:
    from fpl_cli.api.core_insights import PlayerLookup
    from fpl_cli.api.dataset_fetcher import DatasetFetcher
    from fpl_cli.api.fpl import FPLClient

# Force revalidation on every probe: serving a cached copy inside its TTL
# would validate our own cache, not the provider.
PROBE_TTL = timedelta(0)

# Volume sanity floors. Bootstrap carries ~600-800 players depending on the
# point in the season; a value under the floor means a truncated or wrong
# payload, not a quiet transfer window.
ELEMENTS_FLOOR = 400
CSV_ROW_FLOOR = 400

# The Understat settling period and name-miss ceiling live beside the client
# (`api/understat.py`) so the runtime coverage floor reads the same numbers
# this probe does (#438).


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


async def _player_history_check(
    client: FPLClient, elements: list[dict[str, Any]], *, season_started: bool
) -> CheckResult:
    """Whether one `element-summary` carries the history rows scoring reads.

    The per-gameweek history behind form trajectory, xGI sustainability,
    rolling pts/£m and the consistency signals comes from one such call per
    player, and nothing before this probed it: the runtime drops a failed
    call and scores that player neutrally (#438). Probed with the player
    who has played most, so mid-season an empty history is a broken
    endpoint rather than a player who has not featured.
    """
    from fpl_cli.api.fpl import PLAYER_HISTORY_ROW_KEYS

    name = "FPL player history"
    if not elements:
        return CheckResult(name, CheckStatus.UNCHECKED, "no players in the bootstrap to probe with")
    try:
        probe = max(elements, key=lambda e: int(e.get("minutes") or 0))
        probe_id = int(probe["id"])
        probe_minutes = int(probe.get("minutes") or 0)
    except (KeyError, TypeError, ValueError):
        # The bootstrap's own drift: the player-fields row reports it, and
        # this row must not take the whole run down on the way there.
        return CheckResult(
            name,
            CheckStatus.UNCHECKED,
            "bootstrap players carry no usable `id` and `minutes` to pick a probe with — "
            "see the FPL player fields row",
        )
    label = f"{probe.get('web_name') or probe_id} ({probe_minutes}m)"
    try:
        detail = await client.get_player_detail(probe_id)
    except httpx.HTTPError as exc:
        return CheckResult(name, CheckStatus.UNCHECKED, _unreachable(exc))
    except json.JSONDecodeError:
        return CheckResult(
            name,
            CheckStatus.BROKEN,
            "element-summary did not return JSON — the API shape may have changed",
        )

    history = detail.get("history") if isinstance(detail, dict) else None
    if not isinstance(history, list):
        return CheckResult(
            name,
            CheckStatus.BROKEN,
            "element-summary carries no `history` list — every player would score with "
            "neutral form trajectory and no consistency signal",
        )
    if not history:
        if not season_started:
            return CheckResult(
                name, CheckStatus.SKIPPED, "no gameweek finished yet, so no history rows to check"
            )
        return CheckResult(
            name,
            CheckStatus.BROKEN,
            f"element-summary for {label} has no history rows mid-season — every player "
            "would score with neutral form trajectory and no consistency signal",
        )
    missing = [key for key in PLAYER_HISTORY_ROW_KEYS if key not in history[0]]
    if missing:
        return CheckResult(
            name,
            CheckStatus.BROKEN,
            f"history rows are missing {', '.join(sorted(missing))} — these stats would "
            "silently read as 0 for every gameweek of every player",
        )
    return CheckResult(
        name,
        CheckStatus.OK,
        f"{len(history)} history rows for {label}, all {len(PLAYER_HISTORY_ROW_KEYS)} "
        "expected fields present",
    )


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
        # Same client, so the probe below costs one request and no second
        # connection; it needs the bootstrap to pick a player to ask about.
        history_check = await _player_history_check(
            client,
            bootstrap.get("elements") or [],
            season_started=any(e.get("finished") for e in (bootstrap.get("events") or [])),
        )

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
    results.append(history_check)
    return results, bootstrap


# ---------------------------------------------------------------------------
# FPL Draft API
# ---------------------------------------------------------------------------


_DRAFT_CLUBS_NO_MAIN_LIST = (
    "the FPL bootstrap was unreachable, so there was no main-game club list to compare against"
)
_DRAFT_CLUBS_NO_DRAFT_LIST = (
    "the Draft bootstrap could not be read, so there was no draft club list to compare"
)


def _draft_club_check(
    draft_teams: list[dict[str, Any]],
    main_teams: list[dict[str, Any]] | None,
) -> CheckResult:
    """Whether the two games' club lists agree by `code` (#423).

    Every draft path translates a draft club id to the main game's through
    the club's `code`, so a draft club whose code the main game does not
    carry comes out as no club: its players lose their fixtures, form and
    FDR, and nothing at runtime tells that apart from a quiet club. Clubs
    the two games number differently are translated and so not a fault,
    but they are named: the numberings have always agreed so far, and the
    day they stop is worth seeing. So is a main-game club the draft list
    lacks, though it is no fault either: no draft player can be at a club
    the draft game does not list, so nothing reads it.
    """
    from fpl_cli.models.player import usable_code

    name = "Draft club ids"
    if main_teams is None:
        return CheckResult(name, CheckStatus.UNCHECKED, _DRAFT_CLUBS_NO_MAIN_LIST)
    if not main_teams:
        # The FPL bootstrap row reports the empty list itself; blaming every
        # draft club for it here would point at the wrong side
        return CheckResult(
            name, CheckStatus.UNCHECKED,
            "the FPL bootstrap carried no clubs, so there was nothing to compare against",
        )

    main_by_code = {
        code: team for team in main_teams if (code := usable_code(team.get("code"))) is not None
    }
    draft_by_code = {
        code: team for team in draft_teams if (code := usable_code(team.get("code"))) is not None
    }
    unmatched_draft = sorted(
        str(team.get("short_name", team.get("id")))
        for team in draft_teams
        if usable_code(team.get("code")) not in main_by_code
    )
    missing_from_draft = sorted(
        str(team.get("short_name", team.get("id")))
        for code, team in main_by_code.items()
        if code not in draft_by_code
    )
    missing = (
        f"; main-game clubs missing from the draft list: {', '.join(missing_from_draft)}"
        if missing_from_draft else ""
    )
    if unmatched_draft:
        return CheckResult(
            name,
            CheckStatus.BROKEN,
            f"draft clubs with no main-game club of the same code: {', '.join(unmatched_draft)}"
            f" — their players are read with no club{missing}",
        )

    renumbered = sorted(
        str(team.get("short_name", team.get("id")))
        for code, team in draft_by_code.items()
        if team.get("id") != main_by_code[code].get("id")
    )
    if renumbered:
        return CheckResult(
            name,
            CheckStatus.OK,
            f"{len(draft_by_code)} clubs agree by code; {len(renumbered)} numbered differently"
            f" in the two games ({', '.join(renumbered)}), translated by code{missing}",
        )
    return CheckResult(
        name,
        CheckStatus.OK,
        f"all {len(draft_by_code)} clubs share their code and id in both games{missing}",
    )


async def _draft_checks(main_teams: list[dict[str, Any]] | None) -> list[CheckResult]:
    """Probe the draft bootstrap, comparing its clubs with *main_teams*.

    *main_teams* is the FPL bootstrap's raw `teams` list, None when that
    bootstrap was unreachable.
    """
    from fpl_cli.api.fpl_draft import FPLDraftClient

    name = "Draft bootstrap"
    # The club row is always reported, so the row set does not depend on
    # which API happens to be down
    no_draft_list = CheckResult("Draft club ids", CheckStatus.UNCHECKED, _DRAFT_CLUBS_NO_DRAFT_LIST)
    async with FPLDraftClient() as client:
        try:
            bootstrap = await client.get_bootstrap_static()
        except httpx.HTTPError as exc:
            return [CheckResult(name, CheckStatus.UNCHECKED, _unreachable(exc)), no_draft_list]
        except json.JSONDecodeError:
            return [
                CheckResult(
                    name,
                    CheckStatus.BROKEN,
                    "bootstrap-static did not return JSON — the API shape may have changed",
                ),
                no_draft_list,
            ]

    teams = bootstrap.get("teams") or []
    elements = bootstrap.get("elements") or []
    problems: list[str] = []
    if len(teams) != 20:
        problems.append(f"{len(teams)} teams (expected 20)")
    if len(elements) < ELEMENTS_FLOOR:
        problems.append(f"{len(elements)} players (expected at least {ELEMENTS_FLOOR})")
    if problems:
        shape = CheckResult(name, CheckStatus.BROKEN, "; ".join(problems))
    else:
        shape = CheckResult(name, CheckStatus.OK, f"{len(teams)} teams, {len(elements)} players")
    return [shape, _draft_club_check(teams, main_teams)]


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


def _ci_matches_path(season: str, gw: int) -> str:
    return f"{season}/By Tournament/Premier League/GW{gw}/matches.csv"


def _ci_gw_paths(season: str, gw: int) -> list[tuple[str, str, frozenset[str]]]:
    from fpl_cli.api.core_insights import (
        GW_STATS_REQUIRED_COLUMNS,
        MATCHES_REQUIRED_COLUMNS,
        PLAYERMATCHSTATS_REQUIRED_COLUMNS,
    )

    tournament = f"{season}/By Tournament/Premier League/GW{gw}"
    return [
        ("matches.csv", _ci_matches_path(season, gw), MATCHES_REQUIRED_COLUMNS),
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
    # playermatchstats.csv rows the join read, so a majority drop shows up
    # here as it does in the runtime's warning (#431).
    match_rows_read: int

    @property
    def thin_units(self) -> set[str]:
        """The match join, when it kept rows but under half of those it read."""
        if self.match_records and self.match_records * 2 < self.match_rows_read:
            return {MATCH_JOIN_UNIT}
        return set()

    @property
    def unusable_units(self) -> set[str]:
        """Units this gameweek yields nothing, or too little, from, however they failed.

        Absent and empty are the same outcome one gameweek later — the data
        is not there — so persistence is judged on the unit, not on the
        failure kind. Comparing kind against kind would let a unit that
        404s one gameweek and parses to nothing the next read as two
        separate one-off lags. A join keeping under half its rows counts
        too: the runtime warns on it, so the probe must not read it as ok.
        """
        return {FILE_UNITS[f] for f in self.absent} | set(self.empty) | self.thin_units

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
    match_rows_read = 0
    if matches_text is not None and stats_text is not None:
        joined, match_rows_read = parse_match_records(matches_text, stats_text, lookup)
        match_records = sum(len(records) for records in joined.values())
    gw_stat_rows = 0
    if gw_stats_text is not None:
        gw_stat_rows = len(parse_gw_stat_rows(gw_stats_text, lookup)[0])

    empty: list[str] = []
    if matches_text is not None and stats_text is not None and not match_records:
        empty.append(MATCH_JOIN_UNIT)
    if gw_stats_text is not None and not gw_stat_rows:
        empty.append(GW_STATS_UNIT)

    return _GwProbe(
        absent, column_problems, empty, unknown, match_records, gw_stat_rows,
        match_rows_read,
    )


@dataclasses.dataclass(frozen=True)
class _EloCoverage:
    """How many matches lack opponent Elo across the window scoring reads.

    A blank Elo no longer empties the join (#431): the record is kept and
    scores with a neutral opponent factor, so what a gap costs is the
    opponent adjustment, not the signal. The window, not the newest
    gameweek, is what the runtime adjusts over, so it is what the verdict
    is about: blanks a backfill left behind in earlier gameweeks still cost
    the adjustment, and one blank match among dozens costs almost nothing.
    """

    gameweeks: list[int]
    missing_by_gw: dict[int, int]
    matches_by_gw: dict[int, int]

    @property
    def missing(self) -> int:
        return sum(self.missing_by_gw.values())

    @property
    def matches(self) -> int:
        return sum(self.matches_by_gw.values())

    @property
    def gap(self) -> bool:
        """Whether most matches lack Elo: the adjustment is then mostly off."""
        return self.missing * 2 > self.matches

    def phrase(self) -> str | None:
        """Name the gap, or the minority without Elo, or None when every match has it."""
        if not self.missing:
            return None
        first, last = self.gameweeks[0], self.gameweeks[-1]
        span = f"GW{first}" if first == last else f"GW{first}-{last}"
        if not self.gap:
            return (
                f"{self.missing} of {self.matches} matches in {span} carry no "
                "opponent Elo and count unadjusted"
            )
        phrase = (
            f"opponent Elo is missing for {self.missing} of {self.matches} matches in "
            f"{span}, the window scoring reads, so those matches count unadjusted in "
            "the opponent-adjusted xG signals"
        )
        lacking = [gw for gw in self.gameweeks if self.missing_by_gw[gw]]
        if lacking == [last]:
            return f"{phrase}; only GW{last} lacks it, so this is likely a backfill in progress"
        # The earliest gameweek from which every match lacks it, when there is one.
        blank_from = None
        for gw in reversed(self.gameweeks):
            if self.missing_by_gw[gw] != self.matches_by_gw[gw]:
                break
            blank_from = gw
        since = f"every match from GW{blank_from} lacks it, " if blank_from else ""
        return (
            f"{phrase}; {since}so this is an upstream gap rather than a backfill in "
            "progress, and clears only once Core-Insights fills Elo in"
        )


async def _ci_elo_coverage(
    fetcher: DatasetFetcher, season: str, latest_finished_gw: int
) -> _EloCoverage | None:
    """Elo coverage over the gameweeks `get_match_stats` reads, or None when none fetch.

    Only matches.csv carries Elo, so only it is fetched. A gameweek that will
    not fetch or 404s is left out rather than failing the check: the per-GW
    probe already reports on the files themselves.
    """
    from fpl_cli.api.core_insights import count_matches_missing_elo, match_stats_gameweeks

    window = list(match_stats_gameweeks(latest_finished_gw + 1))
    texts = await asyncio.gather(
        *(_ci_gw_file_text(fetcher, _ci_matches_path(season, gw)) for gw in window),
        return_exceptions=True,
    )
    missing_by_gw: dict[int, int] = {}
    matches_by_gw: dict[int, int] = {}
    for gw, text in zip(window, texts):
        if text is None or isinstance(text, BaseException):
            continue
        missing, matches = count_matches_missing_elo(text)
        if matches:
            missing_by_gw[gw], matches_by_gw[gw] = missing, matches
    if not matches_by_gw:
        return None
    return _EloCoverage(sorted(matches_by_gw), missing_by_gw, matches_by_gw)


def _ci_thin_phrase(probe: _GwProbe) -> str:
    """Name a match join that kept rows, but under half of them."""
    return (
        f"{MATCH_JOIN_UNIT} keep only {probe.match_records} of "
        f"{probe.match_rows_read} player-match rows — every expected column is "
        "present but most rows do not survive the join, so opponent-adjusted xG "
        "signals run on what is left"
    )


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
    here instead and asserts they yield something, and that the match join
    keeps most of the rows it reads. Since #431 a blank Elo keeps its
    record, so Elo is judged on its own, across the window scoring reads
    (`_EloCoverage`): stale when most of it lacks Elo, whether a backfill
    in progress or open-ended upstream, because the runtime degrades around
    it rather than reading anything wrong.

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
    coverage = await _ci_elo_coverage(fetcher, season, latest_finished_gw)
    elo = coverage.phrase() if coverage is not None else None
    elo_suffix = f"; {elo}" if elo else ""
    if not probe.unusable_units:
        if coverage is not None and coverage.gap:
            return CheckResult(name, CheckStatus.STALE, f"GW{latest_finished_gw}: {elo}")
        return CheckResult(
            name,
            CheckStatus.OK,
            f"GW{latest_finished_gw}: all per-GW files present, parsing to "
            f"{probe.match_records} player-match records and "
            f"{probe.gw_stat_rows} gameweek stat rows{elo_suffix}",
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
        if probe.thin_units & broken_units:
            problems.append(_ci_thin_phrase(probe))
        # previous is not None whenever broken_units is non-empty.
        assert previous is not None
        prev_cause = (
            f" ({'; '.join(previous.column_problems)})"
            if previous.column_problems
            else ""
        )
        # A join that kept a few rows has not yielded nothing.
        prev_verdict = (
            "is no better" if previous.thin_units & broken_units else "yields nothing either"
        )
        return CheckResult(
            name,
            CheckStatus.BROKEN,
            f"GW{latest_finished_gw}: {'; '.join(problems)}; GW{previous_gw} "
            f"{prev_verdict}{prev_cause}, so this is not publishing lag{elo_suffix}",
        )

    lag = []
    if probe.absent:
        lag.append(f"{', '.join(probe.absent)} not published upstream yet")
    if probe.empty:
        lag.append(_ci_empty_phrase(probe.empty))
    if probe.thin_units:
        lag.append(_ci_thin_phrase(probe))
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
        f"GW{latest_finished_gw}: {'; '.join(lag)} — {caveat}{elo_suffix}",
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
        except httpx.HTTPStatusError as exc:
            # An HTTP status is the provider answering, not the network
            # failing, and while it answers this way every scoring command
            # falls back to FPL xGI for every player without a word (#438).
            # A refusal needs a hand, and a gone endpoint needs a code
            # change (`getLeagueData` is undocumented and has moved before);
            # anything else is the provider's to fix and self-corrects,
            # which is what the stale row means.
            status = exc.response.status_code
            if status in (401, 403):
                return [
                    CheckResult(
                        league_name,
                        CheckStatus.BROKEN,
                        f"Understat refused the request (HTTP {status}) — every scoring "
                        "command silently falls back to FPL xGI for every player while "
                        "it does; the client may be blocked",
                    )
                ]
            if status in (404, 410):
                return [
                    CheckResult(
                        league_name,
                        CheckStatus.BROKEN,
                        f"Understat answered HTTP {status} for the league endpoint — it has "
                        "moved or gone, and every scoring command silently falls back to "
                        "FPL xGI for every player until the client is updated "
                        "(fpl_cli/api/understat.py)",
                    )
                ]
            return [
                CheckResult(
                    league_name,
                    CheckStatus.STALE,
                    f"Understat answered HTTP {status} — scoring runs on FPL xGI for "
                    "every player until it recovers; broken if it persists",
                )
            ]
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
# Premier League awards (#492)
# ---------------------------------------------------------------------------


async def _premier_league_awards_checks(
    season_year: int, element_codes: set[int] | None,
) -> list[CheckResult]:
    """The awards feed the draft Player of the Month voucher reads.

    Shape through the parser itself, which refuses any monthly entry missing
    a key of `AWARD_REQUIRED_KEYS` or carrying a month or id it cannot read.
    Volume from last season, whose Players of the Month are all published:
    this season's list is legitimately empty until mid-September. Then the
    join the voucher stands on, the award's player id being FPL's `code`: an
    id scheme that moved would leave every winner found in nobody's squad,
    which reads as "a free agent at the month's end", not as an error.
    """
    from fpl_cli.api.premier_league import (
        AWARD_REQUIRED_KEYS,
        PLAYER_OF_THE_MONTH,
        AwardsFeedError,
        PremierLeagueClient,
        parse_monthly_awards,
    )
    from fpl_cli.season import season_label

    name = "Premier League awards"
    codes_name = "Player of the Month codes"
    this_season, last_season = season_label(season_year), season_label(season_year - 1)
    async with PremierLeagueClient() as client:
        try:
            payloads = [await client.get_awards(this_season), await client.get_awards(last_season)]
        except httpx.HTTPError as exc:
            return [CheckResult(name, CheckStatus.UNCHECKED, _unreachable(exc))]
        except json.JSONDecodeError:
            return [CheckResult(
                name, CheckStatus.BROKEN, "the awards endpoint did not return JSON — the API may have moved",
            )]
    try:
        current, previous = (
            [award for award in parse_monthly_awards(payload) if award.type == PLAYER_OF_THE_MONTH]
            for payload in payloads
        )
    except AwardsFeedError as exc:
        return [CheckResult(
            name, CheckStatus.BROKEN,
            f"{exc} — every month's Player of the Month would read as not checked",
        )]
    if not previous:
        return [CheckResult(
            name, CheckStatus.BROKEN,
            f"{last_season} lists no Player of the Month — the award's type code may have changed "
            "and every month would read as not yet awarded",
        )]
    results = [CheckResult(
        name, CheckStatus.OK,
        f"{len(current)} Player of the Month award(s) for {this_season}, {len(previous)} for "
        f"{last_season}, each carrying {', '.join(sorted(AWARD_REQUIRED_KEYS))}",
    )]

    if element_codes is None:
        results.append(CheckResult(
            codes_name, CheckStatus.UNCHECKED, "could not fetch the live player list to compare against",
        ))
        return results
    # This season's winners, or last season's before the first award: most
    # of last season's are still in the league, and some always leave.
    probe, season = (current, this_season) if current else (previous, last_season)
    matched = [award for award in probe if award.code in element_codes]
    if not matched:
        results.append(CheckResult(
            codes_name, CheckStatus.BROKEN,
            f"none of {season}'s {len(probe)} Player of the Month winners matches an FPL player code "
            "— the award's player id may no longer be FPL's code, and every winner would read as "
            "owned by nobody",
        ))
    else:
        results.append(CheckResult(
            codes_name, CheckStatus.OK,
            f"{len(matched)} of {season}'s {len(probe)} Player of the Month winners match an FPL player code",
        ))
    return results


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


async def provider_checks() -> tuple[list[CheckResult], int]:
    """Run every provider probe, containing per-provider failures.

    The FPL bootstrap runs first because five later probes compare against
    the live team list, player codes and gameweek state (the draft game's
    club list among them); when it is unreachable those comparisons report
    unchecked rather than guessing.

    Returns the season year alongside the results so `fpl doctor --providers`
    can label its report without a second `bootstrap-static/` download:
    `FPLClient` memoises that payload on the instance alone, so resolving the
    season through a client of its own fetched the same few megabytes twice
    per run (#308 review). An unreachable bootstrap leaves `events` empty and
    `resolve_season_year` falls back to the July clock, which is the same
    degradation the local checks make.
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

    results += await _draft_checks(
        (bootstrap.get("teams") or []) if bootstrap is not None else None,
    )

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
    season_year = resolve_season_year((bootstrap or {}).get("events") or [])
    from fpl_cli.models.player import usable_code

    results += await _premier_league_awards_checks(
        season_year,
        {code for e in elements if (code := usable_code(e.get("code"))) is not None} if elements else None,
    )
    return results, season_year
