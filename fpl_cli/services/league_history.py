"""Durable, season-partitioned store for league-history ledger rows.

Layout (KTD4)::

    <data_dir>/league_history/<season>/<format>-<league_id>/gw<NN>.ndjson

One JSON object per line, each carrying its own schema version. Partitioning
down to the gameweek means two runs capturing different gameweeks never touch
the same file -- `atomic_write_text` is atomic per file but takes no lock, so a
season-wide file would let a laptop run and a web-session run silently discard
each other's rows. It also bounds a corrupt file's blast radius to one
gameweek.

The store is append-only as a semantic contract, not as a syscall (KTD5): the
repo has no append primitive, and a raw append can leave a torn final line that
fail-closed loading would then read as corruption. Every write reads the
existing lines as raw text, appends, and replaces the file atomically, so
existing bytes survive untouched -- including lines written by a newer install
this code cannot parse.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path

from pydantic import ValidationError

from fpl_cli.models.league_history import (
    LEAGUE_HISTORY_VERSION,
    MIN_READABLE_LEAGUE_HISTORY_VERSION,
    CaptureStatus,
    FidelityTier,
    LeagueFormat,
    LeagueHistoryRow,
    partition_segment,
    resolve_rows,
    weakest_tier,
)
from fpl_cli.paths import user_data_dir
from fpl_cli.utils.files import atomic_write_text

logger = logging.getLogger(__name__)


class LeagueHistoryError(RuntimeError):
    """A gameweek file could not be read, so capture refuses to guess at it.

    This is the deliberate inverse of `fpl_cli/models/chip_plan.py`, which
    resets to an empty plan on a corrupt file. A chip plan can be rebuilt from
    the API; a ledger gameweek cannot, because the API keeps per-gameweek
    granularity only for the live season. Resetting would destroy the one copy
    of data whose whole purpose is to outlive the API (R4).
    """


def league_history_dir() -> Path:
    """Root of the ledger, under the writable data dir.

    Resolved per call rather than bound to a module constant, so an
    `FPL_CLI_DATA_DIR` set after import (notably from a late-loaded `.env`) is
    honoured -- `tests/test_paths.py` fails a module-level call outright.
    """
    return user_data_dir() / "league_history"


def partition_dir(season: str, fpl_format: LeagueFormat, league_id: int) -> Path:
    """Directory holding one league's rows for one season.

    Season is a *partition key*, deliberately unlike
    `fpl_cli/services/team_ratings.py`, which discards a file stamped with a
    previous season. Rolling over must never invalidate the ledger: the FPL API
    collapses every per-gameweek row into one aggregate at the July rollover,
    so from that moment these files are the only record that the detail ever
    existed (R5).
    """
    return league_history_dir() / partition_segment(season, fpl_format, league_id)


@dataclass(frozen=True)
class GameweekCoverage:
    """What the store holds for one gameweek, after supersession is resolved."""

    gameweek: int
    readable: bool = True
    tier_counts: dict[FidelityTier, int] = field(default_factory=dict)
    unknown_count: int = 0
    unknown_manager_keys: list[int] = field(default_factory=list)

    @property
    def manager_count(self) -> int:
        """Managers with a row of any kind, including unknown ones."""
        return sum(self.tier_counts.values()) + self.unknown_count

    @property
    def lowest_tier(self) -> FidelityTier | None:
        """The weakest tier any captured manager sits at.

        A condition needing captain detail is unavailable for the whole
        gameweek as soon as one manager is only coarse, so the weakest tier is
        what the coverage report has to name.
        """
        return weakest_tier(self.tier_counts)

    @property
    def is_complete(self) -> bool:
        """Readable, holding rows, and holding no unknown row to repair."""
        return self.readable and self.manager_count > 0 and self.unknown_count == 0


class LeagueHistoryStore:
    """Read and append rows for one (season, format, league) partition."""

    def __init__(self, season: str, fpl_format: LeagueFormat, league_id: int) -> None:
        self.season = season
        self.fpl_format: LeagueFormat = fpl_format
        self.league_id = league_id
        # Per-instance memoization for `resolved_gameweek`, invalidated by
        # `append_rows` (the only write path this store has -- see there).
        # Several callers sharing one store within a single call chain --
        # e.g. the counters projection's own read of a gameweek and a later
        # raw-row read of that same gameweek for fidelity-tier lookup --
        # otherwise re-read and re-parse the same file more than once per
        # call. Never persisted and never shared across instances, so this
        # adds no cross-process staleness risk of its own.
        self._resolved_gameweek_cache: dict[int, dict[int, LeagueHistoryRow]] = {}

    # -- paths ---------------------------------------------------------------

    def partition_dir(self) -> Path:
        return partition_dir(self.season, self.fpl_format, self.league_id)

    def gameweek_file(self, gameweek: int) -> Path:
        return self.partition_dir() / f"gw{gameweek:02d}.ndjson"

    def partition_exists(self) -> bool:
        """Whether this season's partition has been created yet.

        Callers print the resolved path on first creation, so an ephemeral
        container-local data directory is visible before a season is lost to it.
        """
        return self.partition_dir().is_dir()

    def captured_gameweeks(self) -> list[int]:
        """Gameweeks with a file, ascending. Says nothing about readability."""
        directory = self.partition_dir()
        if not directory.is_dir():
            return []
        gameweeks: list[int] = []
        for path in directory.glob("gw*.ndjson"):
            try:
                gameweeks.append(int(path.stem[2:]))
            except ValueError:
                logger.debug("Ignoring unexpected file in league history partition: %s", path)
        return sorted(gameweeks)

    # -- reads ---------------------------------------------------------------

    def load_gameweek(self, gameweek: int) -> list[LeagueHistoryRow]:
        """Every readable row for a gameweek, in the order it was written.

        Raises:
            LeagueHistoryError: a line is malformed, fails validation, or sits
                below the readable version floor. The file is left untouched.
        """
        path = self.gameweek_file(gameweek)
        if not path.is_file():
            return []
        return self._parse(path, path.read_text(encoding="utf-8"))

    def resolved_gameweek(self, gameweek: int) -> dict[int, LeagueHistoryRow]:
        """One winning row per manager key, resolved per R3.

        Memoized per instance (invalidated by `append_rows`): a gameweek
        already read off this store is served from cache rather than
        re-parsed. A failed read is never cached, so an unreadable gameweek
        still raises `LeagueHistoryError` on every call, exactly as before.
        """
        if gameweek not in self._resolved_gameweek_cache:
            self._resolved_gameweek_cache[gameweek] = resolve_rows(self.load_gameweek(gameweek))
        return self._resolved_gameweek_cache[gameweek]

    def coverage(self) -> list[GameweekCoverage]:
        """Per-gameweek tier and status counts, ascending by gameweek.

        Failure is scoped to the gameweek: an unreadable file is reported as
        such rather than failing the whole partition, so one corrupt gameweek
        never hides the rest of a season.
        """
        out: list[GameweekCoverage] = []
        for gameweek in self.captured_gameweeks():
            try:
                resolved = self.resolved_gameweek(gameweek)
            except LeagueHistoryError as exc:
                logger.warning("Gameweek %s is unreadable and is skipped: %s", gameweek, exc)
                out.append(GameweekCoverage(gameweek=gameweek, readable=False))
                continue
            tier_counts: dict[FidelityTier, int] = {}
            unknown_keys: list[int] = []
            for key, row in resolved.items():
                if row.capture_status is CaptureStatus.UNKNOWN:
                    unknown_keys.append(key)
                else:
                    tier_counts[row.tier] = tier_counts.get(row.tier, 0) + 1
            out.append(GameweekCoverage(
                gameweek=gameweek,
                tier_counts=tier_counts,
                unknown_count=len(unknown_keys),
                unknown_manager_keys=sorted(unknown_keys),
            ))
        return out

    # -- writes --------------------------------------------------------------

    def append_rows(
        self, gameweek: int, rows: list[LeagueHistoryRow],
    ) -> list[LeagueHistoryRow]:
        """Append the rows that say something new, and return only those.

        A row identical in content to the resolved current row for its key
        writes nothing -- `captured_at` is excluded from the comparison, so a
        re-run that reproduces a gameweek exactly leaves the file byte-identical.
        A row that could never outrank the current winner (R3's resolution
        order) is skipped too, even when its content differs: a repair pass
        that revisits the whole cohort because one *other* manager is still
        unknown would otherwise re-append a lower-tier duplicate for every
        already-fully-captured manager on every single run. A row differing in
        any value that *could* become the winner appends a superseding line;
        no line is ever edited or removed (R3).

        Raises:
            LeagueHistoryError: the existing file is unreadable. Nothing is
                written, so a corrupt file is never overwritten by a repair
                that would destroy whatever it still holds.
        """
        if not rows:
            return []

        path = self.gameweek_file(gameweek)
        existing_text = path.read_text(encoding="utf-8") if path.is_file() else ""
        winners = resolve_rows(self._parse(path, existing_text))

        new_lines: list[str] = []
        written: list[LeagueHistoryRow] = []
        for row in rows:
            current = winners.get(row.manager_key)
            if current is not None:
                if current.content() == row.content():
                    continue
                if row.resolution_sort_key()[0] < current.resolution_sort_key()[0]:
                    continue
            new_lines.append(row.model_dump_json())
            written.append(row)
            # Keep the batch internally consistent: a second row for the same
            # manager in one call must compare against the first, not against
            # the state on disk before the call.
            if current is None or row.resolution_sort_key() > current.resolution_sort_key():
                winners[row.manager_key] = row

        if not new_lines:
            return []

        # Existing bytes are carried through verbatim, so a line written by a
        # newer install (skipped on read) survives this rewrite unchanged. The
        # only edit is a missing trailing newline, without which the next
        # append would fuse two records into one torn line.
        if existing_text and not existing_text.endswith("\n"):
            existing_text += "\n"
        atomic_write_text(path, existing_text + "".join(line + "\n" for line in new_lines))
        # Invalidate this gameweek's memoized read: a later `resolved_gameweek`
        # call on this same instance must see what was just written, not a
        # cached pre-write state.
        self._resolved_gameweek_cache.pop(gameweek, None)
        return written

    # -- parsing -------------------------------------------------------------

    def _parse(self, path: Path, text: str) -> list[LeagueHistoryRow]:
        """Parse NDJSON text into rows, failing closed on anything unreadable."""
        rows: list[LeagueHistoryRow] = []
        for number, line in enumerate(text.splitlines(), start=1):
            if not line.strip():
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError as exc:
                raise LeagueHistoryError(_unreadable(path, number, f"not valid JSON ({exc})")) from exc
            if not isinstance(payload, dict):
                raise LeagueHistoryError(_unreadable(path, number, "not a JSON object"))

            version = payload.get("version")
            if not isinstance(version, int):
                raise LeagueHistoryError(_unreadable(path, number, "has no schema version"))
            if version > LEAGUE_HISTORY_VERSION:
                # Two installs can share one store (a synced data directory, a
                # laptop and a web session). The older one degrades to partial
                # coverage rather than aborting, and preserves the line on write.
                logger.warning(
                    "Skipping league history line %s of %s: schema version %s is newer than this "
                    "install understands (%s). Upgrade fpl-cli to read it; it is preserved untouched.",
                    number, path, version, LEAGUE_HISTORY_VERSION,
                )
                continue
            if version < MIN_READABLE_LEAGUE_HISTORY_VERSION:
                raise LeagueHistoryError(_unreadable(
                    path, number,
                    f"schema version {version} is older than this install can read "
                    f"({MIN_READABLE_LEAGUE_HISTORY_VERSION})",
                ))

            try:
                rows.append(LeagueHistoryRow.model_validate(_upgrade(payload, version)))
            except ValidationError as exc:
                problems = exc.error_count()
                raise LeagueHistoryError(_unreadable(
                    path, number, f"does not match the row schema ({problems} problem(s))",
                )) from exc
        return rows


def _upgrade(payload: dict, version: int) -> dict:
    """Bring an older-but-readable row payload up to the current shape, in memory.

    In memory only: the line on disk is never rewritten, so a store shared
    with an older install keeps lines that install can still read. Each
    future migration adds a branch here; raising
    MIN_READABLE_LEAGUE_HISTORY_VERSION past a version means shipping a
    one-time rewrite of the upgraded lines first, or every store holding them
    becomes unreadable.
    """
    # Version 4 added `fine_rules_evaluated` with no branch here on purpose: a
    # pre-v4 row genuinely does not record what its capture ruled, and the
    # field's `None` default says exactly that. Filling it in with the rule
    # types configured today would backdate a ruling those rows never made
    # (issue #136).
    if version < 2 and "squad_value" in payload:
        # v1 called the API's bank-inclusive `value` `squad_value`. Same
        # number, honest name -- a straight rename, no arithmetic (issue #147).
        payload = {**payload, "team_value": payload["squad_value"]}
        del payload["squad_value"]
    return payload


def _unreadable(path: Path, line_number: int, problem: str) -> str:
    """The one-line, actionable message every fail-closed path raises with."""
    return (
        f"League history file {path} is unreadable: line {line_number} {problem}. "
        f"Move the file aside (mv '{path}' '{path}.corrupt') and re-run to recapture "
        f"that gameweek -- every other gameweek stays readable and the recap itself "
        f"still renders from live data."
    )
