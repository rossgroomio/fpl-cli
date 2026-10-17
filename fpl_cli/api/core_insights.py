"""FPL-Core-Insights dataset client for historical player data.

Provides season aggregates for the seasons the dataset publishes -- the one
in progress and the one before it -- and GW-level trend and match data for
the current season, sourced from olbauday/FPL-Core-Insights which updates
3x daily.
"""
from __future__ import annotations

import asyncio
import csv
import io
import logging
from collections.abc import Mapping
from datetime import timedelta
from math import isfinite
from typing import ClassVar, Self, TypedDict

import httpx

from fpl_cli.api.contract import header_covers, warn_all_rows_skipped
from fpl_cli.api.dataset_fetcher import DatasetFetcher
from fpl_cli.api.historical_types import (
    MOMENTUM_WINDOW,
    GwTrendProfile,
    PlayerProfile,
    SeasonHistory,
    _GwRow,
    compute_acceleration,
    compute_reliability,
    compute_trend,
)
from fpl_cli.models.player import usable_code
from fpl_cli.season import core_insights_season, season_label, season_start_year

logger = logging.getLogger(__name__)

BASE_URL = "https://raw.githubusercontent.com/olbauday/FPL-Core-Insights/main/data"


class MatchRecord(TypedDict):
    """Per-player per-match record joined from playermatchstats.csv + matches.csv."""

    player_id: int
    gameweek: int
    xg: float
    xa: float
    # None when playermatchstats.csv carries no count for the match (the
    # column gone, or the cell blank); the adjusted-npxG rate leaves such a
    # match out rather than reading the gap as no penalty (#438).
    penalties_scored: int | None
    penalties_missed: int | None
    minutes_played: int
    # None when matches.csv carries no Elo for the opponent; scoring gives
    # the match a neutral opponent factor (#431).
    opponent_elo: float | None
    is_home: bool
    # Attacking involvement (FWD/MID)
    total_shots: int
    chances_created: int
    touches_opposition_box: int
    # Defensive involvement (DEF)
    clearances: int
    blocks: int
    interceptions: int
    tackles_won: int
    recoveries: int
    # GK consistency
    saves: int
    xgot_faced: float
    goals_prevented: float


DEFAULT_TTL = timedelta(hours=4)

# Columns the parsers below index directly (`row[...]`). The header checks
# and the `fpl doctor --providers` probe both assert against these constants,
# so the declared contract cannot drift from what the parsers consume.
# Optional columns read via `row.get(...)` are deliberately not listed.
PLAYERS_CSV_REQUIRED_COLUMNS: frozenset[str] = frozenset({
    "player_id", "player_code", "web_name", "position", "team_code",
})
PLAYERSTATS_REQUIRED_COLUMNS: frozenset[str] = frozenset({
    "id", "gw", "now_cost", "cost_change_start", "total_points", "minutes",
})
GW_STATS_REQUIRED_COLUMNS: frozenset[str] = frozenset({"id", "now_cost"})
MATCHES_REQUIRED_COLUMNS: frozenset[str] = frozenset({
    "match_id", "gameweek", "home_team", "home_team_elo", "away_team_elo",
})
PLAYERMATCHSTATS_REQUIRED_COLUMNS: frozenset[str] = frozenset({
    "player_id", "match_id", "xg", "minutes_played",
})

# Core-Insights uses full position names; map to FPL abbreviations.
_POSITION_MAP = {
    "Goalkeeper": "GK",
    "Defender": "DEF",
    "Midfielder": "MID",
    "Forward": "FWD",
}


def season_dir(season: str) -> str:
    """The dataset directory for a season label: ``"2025-26"`` -> ``"2025-2026"``.

    Every path the client and the ``fpl doctor --providers`` probe build
    starts with this segment, so both derive it here.
    """
    return core_insights_season(season_start_year(season))


def _optional_float(row: Mapping[str, str], column: str) -> float | None:
    """One per-90 column as a float, or None when the row cannot supply it.

    A column absent from the header (a season published before the stat
    existed upstream) and a blank cell are both "no signal", and stay None:
    the scoring path reads 0.0 as a measured zero and would score the player
    as bad at something that was never recorded (#132). The other optional
    columns above default to 0 instead — for a count, absent and zero really
    do mean the same thing.
    """
    raw = row.get(column)
    if not raw:
        return None
    try:
        value = float(raw)
    except ValueError:
        logger.debug("Non-numeric %s in playerstats.csv: %r", column, raw)
        return None
    if not isfinite(value):
        # `float()` parses "nan" and "inf" without raising, and every one of
        # these columns is a count-over-minutes division upstream, which is
        # exactly how a zero-minute row serialises. Neither is a measurement:
        # NaN reaches `normalise_score`, whose `round()` raises and takes the
        # whole command down for every player; inf saturates its weight cap
        # and credits the maximum for a signal nobody recorded.
        logger.debug("Non-finite %s in playerstats.csv: %r", column, raw)
        return None
    return value


def _optional_count(row: Mapping[str, str | None], column: str) -> int | None:
    """A count column of playermatchstats.csv, or None when the row has none.

    The column absent from the header, a row cut short before it, and a
    blank cell all read as unknown rather than 0: Core-Insights writes every
    count it has, 0 included (every 2025-26 and 2026-27 gameweek sampled
    writes `0` in the penalty columns, never a blank), so a gap is a format
    change, and the one consumer that cannot live without the count, the
    adjusted-npxG rate, degrades visibly rather than crediting a penalty
    taker with the penalty's xG (#438). Text that is not a number raises,
    and the row drops with the other tripwires.
    """
    raw = row.get(column)
    if raw is None or not raw.strip():
        return None
    return int(float(raw))


def _numeric_cell(raw: str | None) -> float | None:
    """A numeric matches.csv / playermatchstats.csv cell, or None when it holds no number.

    None covers the cell `csv.DictReader` fills in for a row shorter than
    its header, a blank or whitespace-only cell, and NaN or infinity, which
    `float()` parses without raising and no column here can mean. Text that
    is not a number raises ValueError instead: that is a format change, and
    dropping the row keeps it visible to the tripwires rather than quietly
    reading it as missing.
    """
    if raw is None or not raw.strip():
        return None
    value = float(raw)
    return value if isfinite(value) else None


def _elo(raw: str | None) -> float | None:
    """One side's Elo from matches.csv, or None when the cell carries none.

    Core-Insights publishes an Elo it has not filled in as a blank cell: at
    the start of a season (#142), and for every match from GW3 of 2026-27
    (#431). Dropping the row with it threw away the match's xG, minutes and
    defensive stats too, so a blank Elo is unknown rather than fatal, and so
    is a number that cannot be a rating (zero or below). Text that is not a
    number still raises, through `_numeric_cell`.
    """
    value = _numeric_cell(raw)
    return value if value is not None and value > 0 else None


def _match_xg(row: Mapping[str, str | None]) -> float:
    """One playermatchstats.csv row's xG; raises ValueError when it has none to give.

    Blank is how 2026-27 writes a match with no shot: 2025-26 wrote 0 for
    those, about half of every gameweek's appearances, and 2026-27 leaves xg
    and total_shots empty instead and never writes 0. Dropping the row kept
    only the matches a player shot in, so every per-90 figure built on them
    read high. A blank xG reads as 0 only when the row agrees there was no
    shot and no penalty, though: with one recorded, the xG is missing rather
    than zero (a feed not filled in yet), and the row drops so the tripwires
    count it. A row cut short before its xg cell drops too.
    """
    raw = row["xg"]
    if raw is None:
        msg = "row ends before its xg cell"
        raise ValueError(msg)
    value = _numeric_cell(raw)
    if value is not None:
        return value
    if any(
        _numeric_cell(row.get(column))
        for column in ("total_shots", "penalties_scored", "penalties_missed")
    ):
        msg = "xg is blank on a row that records a shot"
        raise ValueError(msg)
    return 0.0


def match_stats_gameweeks(current_gw: int) -> range:
    """The gameweeks `get_match_stats` reads for *current_gw*: up to 12 before it.

    Twelve is the lookback of the rolling window `compute_adjusted_npxg`
    reads. The `fpl doctor --providers` probe scans this same range, so its
    verdict on Elo coverage is about the matches scoring actually uses.
    """
    return range(max(1, current_gw - 12), current_gw)


def make_core_insights_fetcher(ttl: timedelta = DEFAULT_TTL) -> DatasetFetcher:
    """Create a DatasetFetcher configured for the FPL-Core-Insights GitHub dataset."""
    from fpl_cli.paths import user_cache_dir

    return DatasetFetcher(
        base_url=BASE_URL,
        cache_dir=user_cache_dir() / "datasets" / "core-insights",
        ttl=ttl,
    )


class PlayerLookup:
    """Parsed row from players.csv: maps FPL player_id to identity fields."""

    __slots__ = ("player_code", "web_name", "position", "team_code")

    def __init__(self, player_code: int, web_name: str, position: str, team_code: int) -> None:
        self.player_code = player_code
        self.web_name = web_name
        self.position = position
        self.team_code = team_code


def parse_player_lookup(text: str) -> tuple[dict[int, PlayerLookup], int]:
    """Parse players.csv into the identity table every other parse joins on.

    Returns the mapping and the number of data rows read, so a caller can
    tell an empty file from one where nothing survived parsing.
    """
    reader = csv.DictReader(io.StringIO(text))
    lookup: dict[int, PlayerLookup] = {}
    row_count = 0
    for row in reader:
        row_count += 1
        try:
            pid = int(row["player_id"])
            lookup[pid] = PlayerLookup(
                player_code=int(row["player_code"]),
                web_name=row["web_name"],
                position=_POSITION_MAP.get(row["position"], "???"),
                team_code=int(row["team_code"]),
            )
        except (ValueError, KeyError) as exc:
            logger.debug("Skipping malformed row in players.csv: %s", exc)
            continue
    return lookup, row_count


def parse_match_records(
    matches_text: str,
    stats_text: str,
    lookup: Mapping[int, PlayerLookup],
) -> tuple[dict[int, list[MatchRecord]], int]:
    """Join one gameweek's playermatchstats.csv onto its matches.csv.

    Returns the records and the number of playermatchstats.csv data rows
    read, so a caller can tell how much of the file survived the join.

    Every value a record depends on is converted here, so a column that is
    present but blank upstream drops the row exactly as it does at runtime.
    The `fpl doctor --providers` per-GW probe runs this same join rather than
    a weaker header check, so it cannot pass a file the runtime reads as zero
    records (#142). Elo and xG are the exceptions (#431): the columns must
    be there, but a blank Elo keeps the row with an unknown `opponent_elo`,
    and a blank xG on a row with no shot reads as 0 (`_match_xg`).
    """
    matches: dict[str, dict[str, str]] = {}
    for row in csv.DictReader(io.StringIO(matches_text)):
        mid = row.get("match_id", "")
        if mid:
            matches[mid] = row

    result: dict[int, list[MatchRecord]] = {}
    rows_read = 0
    for row in csv.DictReader(io.StringIO(stats_text)):
        rows_read += 1
        # TypeError beside ValueError throughout: `csv.DictReader` fills a row
        # shorter than its header with None, which `int()` / `float()` reject
        # with TypeError, and an uncaught one took every gameweek down.
        try:
            pid = int(row["player_id"])
            mid = row["match_id"]
        except (ValueError, KeyError, TypeError):
            continue

        match = matches.get(mid)
        if match is None:
            continue

        player = lookup.get(pid)
        if player is None:
            continue

        try:
            home_elo = _elo(match["home_team_elo"])
            away_elo = _elo(match["away_team_elo"])
            gameweek = int(float(match["gameweek"]))
            home_team_code = int(float(match["home_team"]))
        except (ValueError, KeyError, TypeError):
            continue

        is_home = player.team_code == home_team_code
        opponent_elo = away_elo if is_home else home_elo

        try:
            xg = _match_xg(row)
            minutes_played = int(float(row["minutes_played"]))
        except (ValueError, KeyError, TypeError):
            continue

        record: MatchRecord = {
            "player_id": pid,
            "gameweek": gameweek,
            "xg": xg,
            "xa": float(row.get("xa") or 0),
            "penalties_scored": _optional_count(row, "penalties_scored"),
            "penalties_missed": _optional_count(row, "penalties_missed"),
            "minutes_played": minutes_played,
            "opponent_elo": opponent_elo,
            "is_home": is_home,
            "total_shots": int(float(row.get("total_shots") or 0)),
            "chances_created": int(float(row.get("chances_created") or 0)),
            "touches_opposition_box": int(float(row.get("touches_opposition_box") or 0)),
            "clearances": int(float(row.get("clearances") or 0)),
            "blocks": int(float(row.get("blocks") or 0)),
            "interceptions": int(float(row.get("interceptions") or 0)),
            "tackles_won": int(float(row.get("tackles_won") or 0)),
            "recoveries": int(float(row.get("recoveries") or 0)),
            "saves": int(float(row.get("saves") or 0)),
            "xgot_faced": float(row.get("xgot_faced") or 0),
            "goals_prevented": float(row.get("goals_prevented") or 0),
        }
        result.setdefault(pid, []).append(record)

    return result, rows_read


def count_matches_missing_elo(matches_text: str) -> tuple[int, int]:
    """(matches without a usable Elo on either side, matches) in one matches.csv.

    Each cell is read by `_elo`, the rule the join applies, so the `fpl
    doctor --providers` probe counts what scoring sees. Matches are keyed on
    match_id as the join keys them, and one flagged unfinished is left out:
    it has no player rows to adjust. Text where a rating belongs counts as
    missing here; the join drops those rows, and the probe reports that drop
    on its own.
    """
    matches: dict[str, dict[str, str]] = {}
    for row in csv.DictReader(io.StringIO(matches_text)):
        mid = row.get("match_id")
        if mid and (row.get("finished") or "").strip().lower() != "false":
            matches[mid] = row

    missing = 0
    for row in matches.values():
        try:
            rated = _elo(row.get("home_team_elo")) and _elo(row.get("away_team_elo"))
        except ValueError:
            rated = None
        if not rated:
            missing += 1
    return missing, len(matches)


def parse_gw_stat_rows(
    text: str, lookup: Mapping[int, PlayerLookup]
) -> tuple[dict[int, _GwRow], int]:
    """Parse one gameweek's player_gameweek_stats.csv into {player_id: row}.

    Returns the parsed rows and the number of data rows read. First row per
    player wins, matching the dedupe the season-wide fetch relies on.
    """
    parsed: dict[int, _GwRow] = {}
    rows_read = 0
    for row in csv.DictReader(io.StringIO(text)):
        rows_read += 1
        try:
            pid = int(row["id"])
            now_cost = int(round(float(row["now_cost"]) * 10))
            transfers_in = int(row.get("transfers_in_event", 0) or 0)
            transfers_out = int(row.get("transfers_out_event", 0) or 0)
        except (ValueError, KeyError):
            continue

        if pid in parsed:
            continue

        player = lookup.get(pid)
        gw_row: _GwRow = {
            "value": now_cost,
            "transfers_balance": transfers_in - transfers_out,
            "web_name": player.web_name if player else row.get("web_name", "???"),
            "position": player.position if player else "???",
            "team_name": row.get("team_name", "???"),
        }
        if player:
            gw_row["code"] = player.player_code
        parsed[pid] = gw_row
    return parsed, rows_read


class CoreInsightsClient:
    """Client for the olbauday/FPL-Core-Insights GitHub dataset.

    Serves season aggregates for every configured season and, for the newest
    of them (the season in progress), the per-gameweek trend and match-level
    data. Core-Insights publishes exactly two seasons -- the one in progress
    and the one before it -- so the default window is the current season
    alone and `make_historical_provider` widens it to both (#101). Uses
    DatasetFetcher for disk-cached HTTP with ETag/TTL.
    """

    MIN_MINUTES = 450
    HISTORICAL_TTL = timedelta(days=30)

    # Session-level cache keyed by the season window. Two construction sites
    # hold different windows -- the provider's two seasons, the default
    # single season elsewhere -- and whichever ran first must not answer for
    # the other, or the provider would silently lose last season from every
    # prior. Keying makes that correct by construction rather than by no
    # other call site ever asking for profiles.
    _session_profiles: ClassVar[dict[tuple[str, ...], dict[int, PlayerProfile]]] = {}

    def __init__(
        self,
        fetcher: DatasetFetcher,
        seasons: tuple[str, ...] | None = None,
    ) -> None:
        """
        Args:
            fetcher: Disk-caching fetcher rooted at the dataset's base URL.
            seasons: Hyphenated season labels, oldest first, the last being
                the season in progress. Defaults to the current season alone.
        """
        self.fetcher = fetcher
        self.seasons = seasons if seasons is not None else (season_label(),)
        self._season_label = self.seasons[-1]
        self._ci_season = season_dir(self._season_label)
        self._player_lookups: dict[str, dict[int, PlayerLookup]] = {}
        self._season_data: dict[str, list[SeasonHistory]] | None = None
        self._gw_rows: dict[int, dict[int, _GwRow]] | None = None
        self._match_records: dict[int, list[MatchRecord]] | None = None
        self._current_gw: int = 38

    async def close(self) -> None:
        await self.fetcher.close()

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    def _is_historical(self, season: str) -> bool:
        """A season is complete once it is not the newest configured one."""
        return season != self._season_label

    def _season_ttl(self, season: str) -> timedelta | None:
        """A completed season's files are effectively immutable; otherwise the fetcher default."""
        return self.HISTORICAL_TTL if self._is_historical(season) else None

    def _describe(self, season: str) -> str:
        """How a warning names the season: the one in progress is 'current-season'."""
        return season if self._is_historical(season) else "current-season"

    # --- Player lookup (players.csv join) ---

    async def _fetch_player_lookup(self, season: str | None = None) -> dict[int, PlayerLookup]:
        """Fetch a season's players.csv and build {player_id: PlayerLookup}.

        player_id is season-local, so a join reads the lookup of the season
        the joined file belongs to. Defaults to the season in progress, which
        every per-gameweek file joins against.
        """
        label = season or self._season_label
        cached = self._player_lookups.get(label)
        if cached is not None:
            return cached

        text = await self.fetcher.get(
            f"{season_dir(label)}/players.csv", ttl=self._season_ttl(label)
        )
        source = f"Core-Insights {label} players.csv"
        degraded = f"{self._describe(label)} player histories are unavailable"
        lookup: dict[int, PlayerLookup] = {}
        if header_covers(
            source,
            csv.DictReader(io.StringIO(text)).fieldnames,
            PLAYERS_CSV_REQUIRED_COLUMNS,
            degraded=degraded,
        ):
            lookup, row_count = parse_player_lookup(text)
            if row_count and not lookup:
                warn_all_rows_skipped(source, row_count, degraded=degraded)

        self._player_lookups[label] = lookup
        return lookup

    # --- Match-level data ---

    def _gw_path(self, gw: int, filename: str) -> str:
        """Build the per-GW CSV path under By Tournament/Premier League."""
        return f"{self._ci_season}/By Tournament/Premier League/GW{gw}/{filename}"

    async def get_match_stats(self, current_gw: int) -> dict[int, list[MatchRecord]]:
        """Fetch per-GW playermatchstats.csv + matches.csv for recent gameweeks.

        Fetches from ``By Tournament/Premier League/GW{n}/`` for the last 12
        completed gameweeks (matching the rolling window used by
        compute_adjusted_npxg). Joins on match_id within each GW.
        Returns dict keyed by FPL player element_id.
        """
        if self._match_records is not None:
            return self._match_records

        lookup = await self._fetch_player_lookup()

        # Fetch last 12 GWs concurrently (2 files per GW)
        gw_range = match_stats_gameweeks(current_gw)
        fetch_tasks = []
        for gw in gw_range:
            fetch_tasks.append(self.fetcher.get(self._gw_path(gw, "matches.csv")))
            fetch_tasks.append(self.fetcher.get(self._gw_path(gw, "playermatchstats.csv")))

        try:
            raw_results = await asyncio.gather(*fetch_tasks, return_exceptions=True)
        except Exception as exc:  # noqa: BLE001 — graceful degradation: CI CSV unavailable
            logger.warning("Failed to fetch match-level CSVs: %s", exc)
            self._match_records = {}
            return self._match_records

        # One join per gameweek, through the same parser the provider probe
        # runs (#142). A gameweek missing either file contributes nothing.
        result: dict[int, list[MatchRecord]] = {}
        joined_gws = 0
        # Gameweeks that kept under half their rows, as "GW3 (0 of 398)".
        thin_gws: list[str] = []
        for i, gw in enumerate(gw_range):
            matches_result = raw_results[i * 2]
            stats_result = raw_results[i * 2 + 1]
            if isinstance(matches_result, BaseException):
                logger.debug("GW%d matches.csv unavailable: %s", gw, matches_result)
                continue
            if isinstance(stats_result, BaseException):
                logger.debug("GW%d playermatchstats.csv unavailable: %s", gw, stats_result)
                continue
            joined_gws += 1
            gw_records, gw_rows_read = parse_match_records(matches_result, stats_result, lookup)
            gw_kept = sum(len(records) for records in gw_records.values())
            if gw_kept * 2 < gw_rows_read:
                thin_gws.append(f"GW{gw} ({gw_kept} of {gw_rows_read})")
            for pid, records in gw_records.items():
                result.setdefault(pid, []).extend(records)

        if gw_range and not joined_gws:
            # Every gameweek's files failed to fetch: a debug line per
            # gameweek was all this left, and the zero-records warning below
            # needs at least one gameweek to have loaded (#438).
            logger.warning(
                "Core-Insights per-gameweek match files for GW%d-%d could not be "
                "fetched — opponent-adjusted xG signals are unavailable and consistency "
                "signals rest on FPL history alone; run `fpl doctor --providers`",
                gw_range.start,
                gw_range.stop - 1,
            )
        elif joined_gws and not result:
            logger.warning(
                "Core-Insights match stats parsed to 0 records from %d gameweek file(s) — "
                "the upstream format may have changed; opponent-adjusted xG signals "
                "are unavailable",
                joined_gws,
            )
        elif thin_gws:
            # A partial collapse is as silent as a total one at exactly zero:
            # #431 kept GW1-2 alone, enough records to stay off the warning
            # above and too few for any player to reach the adjusted-npxG
            # minimum, so every score fell back without a word. Judged per
            # gameweek, because a total across the window lets intact early
            # gameweeks hide the newest ones collapsing, and the rolling
            # window leans on the newest.
            logger.warning(
                "Core-Insights match stats kept under half their player-match rows in "
                "%s — the upstream format may have changed; opponent-adjusted xG and "
                "consistency signals rest on what survived",
                ", ".join(thin_gws),
            )
        self._match_records = result
        return result

    # --- Season aggregates ---

    async def _fetch_season_data(self) -> dict[str, list[SeasonHistory]]:
        """Season aggregates for every configured season, keyed by label.

        Each season's root-level playerstats.csv (full columns, one cumulative
        row per player per gameweek) is reduced to the max-GW row per player
        -- a completed season's final figures, the current season's latest --
        and joined onto that season's players.csv. Returns cached data on
        subsequent calls.
        """
        if self._season_data is not None:
            return self._season_data

        results = await asyncio.gather(*(self._fetch_one_season(s) for s in self.seasons))
        self._season_data = dict(results)
        return self._season_data

    async def _fetch_one_season(self, season: str) -> tuple[str, list[SeasonHistory]]:
        """One season's aggregates, or an empty season when it is not published.

        A 404 degrades to an empty season with a warning, as vaastav's does,
        so a directory that does not exist yet at rollover cannot take the
        other seasons down with it. Any other failure propagates.
        """
        try:
            lookup, text = await asyncio.gather(
                self._fetch_player_lookup(season),
                self.fetcher.get(
                    f"{season_dir(season)}/playerstats.csv", ttl=self._season_ttl(season)
                ),
            )
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404:
                logger.warning("Core-Insights data not available for season %s", season)
                return season, []
            raise

        histories, latest_gw = self._parse_playerstats(text, season, lookup)
        if latest_gw and not self._is_historical(season):
            self._current_gw = latest_gw
        return season, histories

    def _parse_playerstats(
        self, text: str, season: str, lookup: Mapping[int, PlayerLookup]
    ) -> tuple[list[SeasonHistory], int]:
        """Parse one season's playerstats.csv into histories plus its latest gameweek."""
        source = f"Core-Insights {season} playerstats.csv"
        degraded = f"{self._describe(season)} aggregates are unavailable"
        reader = csv.DictReader(io.StringIO(text))
        stats_header_ok = header_covers(
            source, reader.fieldnames, PLAYERSTATS_REQUIRED_COLUMNS, degraded=degraded
        )

        # Collect all rows, keep only the max-GW row per player.
        best_gw: dict[int, int] = {}
        best_row: dict[int, dict[str, str]] = {}
        rows_read = 0
        if stats_header_ok:
            for row in reader:
                rows_read += 1
                try:
                    pid = int(row["id"])
                    gw = int(row["gw"])
                except (ValueError, KeyError):
                    continue
                if pid not in best_gw or gw > best_gw[pid]:
                    best_gw[pid] = gw
                    best_row[pid] = row

        histories: list[SeasonHistory] = []
        for pid, row in best_row.items():
            player = lookup.get(pid)
            if player is None:
                logger.debug(
                    "Player %d in %s playerstats but not in players.csv, skipping", pid, season
                )
                continue

            try:
                # Core-Insights publishes now_cost in £m (13.5) but keeps
                # cost_change_start in the API's own tenths (-3 for a £0.3m
                # drop): every gameweek of 2025-26 and 2026-27 carries whole
                # numbers there and never a fraction. Scaling both turned
                # that £0.3m drop into £3.0m and put start_cost out by £2.7m
                # for every player whose price had moved.
                now_cost = int(round(float(row["now_cost"]) * 10))
                cost_change_start = int(round(float(row["cost_change_start"])))
                total_points = int(row["total_points"])
                # Required rather than defaulted (#438): the player prior
                # reads minutes against its 450-minute floor, so a column
                # published blank read as a season nobody played and every
                # player fell to a price prior while the notice still said
                # pedigree. The dataset writes 0 for no minutes, never a
                # blank, so a blank is a format change and the row drops
                # into the all-rows-skipped tripwire below.
                minutes = int(row["minutes"])
            except (ValueError, KeyError, TypeError):
                logger.debug("Skipping player %d: missing/malformed required field", pid)
                continue

            histories.append(SeasonHistory(
                element_code=player.player_code,
                season=season,
                total_points=total_points,
                minutes=minutes,
                starts=int(row.get("starts", 0) or 0),
                goals=int(row.get("goals_scored", 0) or 0),
                assists=int(row.get("assists", 0) or 0),
                expected_goals=float(row.get("expected_goals", 0) or 0),
                expected_assists=float(row.get("expected_assists", 0) or 0),
                expected_goal_involvements=float(
                    row.get("expected_goal_involvements", 0) or 0
                ),
                start_cost=now_cost - cost_change_start,
                end_cost=now_cost,
                position=player.position,
                web_name=player.web_name,
                team_code=player.team_code,
                defensive_contribution_per_90=_optional_float(
                    row, "defensive_contribution_per_90"
                ),
                saves_per_90=_optional_float(row, "saves_per_90"),
                clean_sheets_per_90=_optional_float(row, "clean_sheets_per_90"),
                expected_goals_conceded_per_90=_optional_float(
                    row, "expected_goals_conceded_per_90"
                ),
            ))

        if rows_read and not histories:
            # Covers both value drift (rows read, none survived) and an empty
            # player lookup leaving every row unmatched.
            warn_all_rows_skipped(source, rows_read, degraded=degraded)

        return histories, max(best_gw.values(), default=0)

    def _per_90(self, stat: float, minutes: int) -> float:
        if minutes == 0:
            return 0.0
        return round((stat / minutes) * 90, 2)

    def _build_profile(
        self, element_code: int, seasons: list[SeasonHistory],
    ) -> PlayerProfile:
        seasons.sort(key=lambda s: s.season)
        latest = seasons[-1]
        qualifying = [s for s in seasons if s.minutes >= self.MIN_MINUTES]

        pts_per_90 = [self._per_90(s.total_points, s.minutes) for s in qualifying]
        xgi_per_90_all = [
            (self._per_90(s.expected_goal_involvements, s.minutes), s)
            for s in qualifying
            if s.expected_goal_involvements > 0
        ]
        xgi_per_90 = [v for v, _ in xgi_per_90_all]
        minutes_per_start = [
            round(s.minutes / s.starts, 1) if s.starts > 0 else 0.0
            for s in qualifying
        ]
        cost_values = [s.end_cost for s in qualifying]

        return PlayerProfile(
            element_code=element_code,
            web_name=latest.web_name,
            current_position=latest.position,
            seasons=seasons,
            pts_per_90=pts_per_90,
            pts_per_90_trend=compute_trend(pts_per_90),
            cost_trajectory=compute_trend([float(c) for c in cost_values]),
            xgi_per_90=xgi_per_90,
            xgi_per_90_trend=(
                compute_trend(xgi_per_90) if len(xgi_per_90) >= 2 else None
            ),
            minutes_per_start=minutes_per_start,
            reliability=compute_reliability(
                seasons,
                current_season=self._season_label,
                current_gw=self._current_gw,
            ),
        )

    async def get_all_player_histories(self) -> dict[int, PlayerProfile]:
        """Get historical profiles for all players across the configured seasons.

        Results are cached at the class level for the session, per window.
        """
        cached = CoreInsightsClient._session_profiles.get(self.seasons)
        if cached is not None:
            return cached

        all_data = await self._fetch_season_data()
        by_code: dict[int, list[SeasonHistory]] = {}
        for season_list in all_data.values():
            for sh in season_list:
                by_code.setdefault(sh.element_code, []).append(sh)

        profiles = {
            code: self._build_profile(code, seasons)
            for code, seasons in by_code.items()
        }
        CoreInsightsClient._session_profiles[self.seasons] = profiles
        return profiles

    async def get_player_history(self, element_code: int) -> PlayerProfile | None:
        all_data = await self._fetch_season_data()
        player_seasons: list[SeasonHistory] = []
        for season_list in all_data.values():
            for sh in season_list:
                if sh.element_code == element_code:
                    player_seasons.append(sh)
        if not player_seasons:
            return None
        return self._build_profile(element_code, player_seasons)

    # --- GW-level trend data ---

    async def get_gw_trends(
        self, last_n: int | None = None,
    ) -> dict[int, GwTrendProfile]:
        """Fetch per-GW data for the current season, return trend profiles.

        Fetches individual GW files concurrently. 404s are skipped (unplayed GWs).
        """
        if self._gw_rows is None:
            self._gw_rows = await self._fetch_all_gw_rows()
        return self._compute_gw_profiles(self._gw_rows, last_n=last_n)

    async def _fetch_single_gw(self, gw: int) -> str:
        """Fetch one GW's player_gameweek_stats.csv, or "" when it 404s."""
        path = f"{self._ci_season}/By Gameweek/GW{gw}/player_gameweek_stats.csv"
        try:
            ttl = self.HISTORICAL_TTL if gw < self._latest_finished_gw() else None
            return await self.fetcher.get(path, ttl=ttl)
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404:
                return ""
            raise

    def _latest_finished_gw(self) -> int:
        """Estimate latest finished GW from cached data, or 0 if unknown."""
        if self._gw_rows:
            all_gws = set()
            for gw_dict in self._gw_rows.values():
                all_gws.update(gw_dict.keys())
            return max(all_gws) if all_gws else 0
        return 0

    async def _fetch_all_gw_rows(self) -> dict[int, dict[int, _GwRow]]:
        """Fetch all available GW files concurrently, parse into grouped rows."""
        lookup = await self._fetch_player_lookup()

        results = await asyncio.gather(
            *(self._fetch_single_gw(gw) for gw in range(1, 39)),
            return_exceptions=True,
        )

        by_player: dict[int, dict[int, _GwRow]] = {}
        rows_read = 0
        for gw, result in enumerate(results, start=1):
            if isinstance(result, BaseException):
                logger.warning("Failed to fetch GW%d: %s", gw, result)
                continue
            parsed, gw_rows_read = parse_gw_stat_rows(result, lookup)
            rows_read += gw_rows_read
            for pid, row in parsed.items():
                by_player.setdefault(pid, {})[gw] = row

        if rows_read and not by_player:
            warn_all_rows_skipped(
                "Core-Insights player_gameweek_stats.csv",
                rows_read,
                degraded="price-trend and transfer-momentum signals are unavailable",
            )
        return by_player

    def _compute_gw_profiles(
        self,
        by_player: dict[int, dict[int, _GwRow]],
        last_n: int | None = None,
    ) -> dict[int, GwTrendProfile]:
        profiles: dict[int, GwTrendProfile] = {}
        for element, gw_dict in by_player.items():
            if not gw_dict:
                continue
            sorted_rounds = sorted(gw_dict.keys())
            if last_n is not None:
                sorted_rounds = sorted_rounds[-last_n:]

            values = [float(gw_dict[r]["value"]) for r in sorted_rounds]
            balances = [gw_dict[r]["transfers_balance"] for r in sorted_rounds]
            latest_row = gw_dict[sorted_rounds[-1]]

            if last_n is not None:
                recent_balances = balances
            else:
                window = min(MOMENTUM_WINDOW, len(sorted_rounds))
                recent_balances = balances[-window:]

            profiles[element] = GwTrendProfile(
                element=element,
                web_name=latest_row["web_name"],
                position=latest_row["position"],
                team_name=latest_row["team_name"],
                price_start=int(values[0]),
                price_current=int(values[-1]),
                price_change=int(values[-1] - values[0]),
                price_slope=compute_trend(values),
                price_acceleration=compute_acceleration(values),
                transfer_momentum=sum(recent_balances),
                gw_count=len(sorted_rounds),
                latest_gw=sorted_rounds[-1],
                first_gw=sorted_rounds[0],
                code=usable_code(latest_row.get("code")),
            )

        return profiles
