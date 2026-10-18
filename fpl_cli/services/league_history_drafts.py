"""Snapshots of each held draft's picks, kept in the league-history ledger.

Layout::

    <data_dir>/league_history/<season>/draft-<league_id>/drafts/draft-<draft_id>.json

The draft API serves only a league's *latest* draft (`FPLDraftClient.
get_draft_choices`): the moment an admin schedules a redraft the current
draft's picks stop being served, and once it is held there is no route back to
them (#461). That is the ledger's criterion -- rows that cannot be fetched
again later -- so the snapshots live in the ledger's own partition, beside the
`gwNN.ndjson` files, and outlive the season with them. They are not the
counters' kind of file, which is rebuildable and disposable.

Each file is the endpoint's response as served, with one key added:
`league_draft`, that draft's entry from the league details' `league.drafts`
(its gameweek, date and order method). Nothing else is added, renamed or
dropped, so a manual capture -- the endpoint's response saved straight to the
path -- is a valid snapshot too: it lacks only `league_draft`, and is kept as
it is rather than rewritten to add it.

A held draft's picks never change, so a file is written once and never
rewritten. A later run that is served the same picks writes nothing; one served
different picks leaves the file alone and says so, since that means either the
API or the file is wrong and only a person can say which.
"""

from __future__ import annotations

import json
from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

from fpl_cli.services.league_history import partition_dir
from fpl_cli.utils.files import atomic_write_text

# The key a snapshot adds to the response it stores.
LEAGUE_DRAFT_KEY = "league_draft"

# What makes a pick the pick it is, and so what two copies of a draft are
# compared on. Not the whole response: `element_status` beside the picks is
# every player's owner *today*, which moves with every waiver round, and a
# row's display names follow a manager renaming their team. Neither means the
# API or the file is wrong.
PICK_FIELDS = ("draft", "index", "round", "pick", "entry", "element", "was_auto", "choice_time")


class DraftSnapshotStatus(StrEnum):
    """What one run did about one completed draft."""

    # Served, and written for the first time.
    SAVED = "saved"
    # Served, and the file already holds the same picks.
    UNCHANGED = "unchanged"
    # No longer served, and the file holds them: the snapshot doing its job.
    KEPT = "kept"
    # Served, and the file holds different picks. Left as it is.
    DIFFERS = "differs"
    # Served, and the file is not a snapshot this can read. Left as it is.
    UNREADABLE = "unreadable"
    # No longer served and never captured: the picks are gone.
    LOST = "lost"
    # Served, but the file could not be written; the next run tries again.
    WRITE_FAILED = "write_failed"


@dataclass(frozen=True)
class DraftSnapshotOutcome:
    """One completed draft's snapshot status after this run."""

    draft_id: int
    status: DraftSnapshotStatus
    path: Path
    # LOST only: the drafts the endpoint served picks for instead, if any.
    served_draft_ids: tuple[int, ...] = ()
    # DIFFERS, UNREADABLE and WRITE_FAILED: what was wrong, for the notice.
    detail: str | None = None


def draft_snapshot_dir(season: str, league_id: int) -> Path:
    """Where one draft league's snapshots live for one season.

    Under the ledger partition, resolved per call like `partition_dir`, so an
    `FPL_CLI_DATA_DIR` set after import is honoured.
    """
    return partition_dir(season, "draft", league_id) / "drafts"


def draft_snapshot_path(season: str, league_id: int, draft_id: int) -> Path:
    """The snapshot file for one draft."""
    return draft_snapshot_dir(season, league_id) / f"draft-{draft_id}.json"


def _is_id(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def completed_drafts(league_drafts: Iterable[Any]) -> list[dict[str, Any]]:
    """The entries of `league.drafts` the league says have been held.

    A draft still to be held, or under way, has no settled picks to keep: a
    partial draft saved now would read as a conflict once it finished.
    """
    return [
        draft for draft in league_drafts
        if isinstance(draft, dict) and _is_id(draft.get("id"))
        and draft.get("draft_completed") is True
    ]


def _choice_rows(response: Mapping[str, Any]) -> list[dict[str, Any]]:
    rows = response.get("choices")
    return [row for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []


def _picks(rows: Iterable[Mapping[str, Any]], draft_id: int) -> Counter[str]:
    """One draft's picks as comparable facts, order-insensitive."""
    return Counter(
        json.dumps({name: row.get(name) for name in PICK_FIELDS}, sort_keys=True)
        for row in rows if row.get("draft") == draft_id
    )


def _check_existing(path: Path, draft_id: int, served: Counter[str]) -> tuple[DraftSnapshotStatus, str | None]:
    """Compare a file already on disk with the picks served now."""
    try:
        stored = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return DraftSnapshotStatus.UNREADABLE, f"it is not readable JSON ({exc})"
    if not isinstance(stored, dict) or not isinstance(stored.get("choices"), list):
        return DraftSnapshotStatus.UNREADABLE, "it holds no `choices` list"

    kept = _picks(_choice_rows(stored), draft_id)
    if kept == served:
        return DraftSnapshotStatus.UNCHANGED, None
    missing = sum((served - kept).values())
    return DraftSnapshotStatus.DIFFERS, (
        f"it holds {kept.total()} picks for draft {draft_id} and the endpoint serves "
        f"{served.total()}, {missing} of them not in the file"
    )


def snapshot_draft_choices(
    season: str,
    league_id: int,
    league_drafts: Iterable[Any],
    choices: Mapping[str, Any],
) -> list[DraftSnapshotOutcome]:
    """Keep every completed draft's picks the endpoint still serves.

    *league_drafts* is `league.drafts` from the league details; *choices* is
    `get_draft_choices`' response for the same league. One outcome per
    completed draft, in `league.drafts` order. Raises nothing for a file
    problem: a file that cannot be read or written is an outcome, so the
    caller reports it and carries on.
    """
    rows = _choice_rows(choices)
    served_ids = sorted({row["draft"] for row in rows if _is_id(row.get("draft"))})

    outcomes: list[DraftSnapshotOutcome] = []
    for draft in completed_drafts(league_drafts):
        draft_id: int = draft["id"]
        path = draft_snapshot_path(season, league_id, draft_id)
        if draft_id not in served_ids:
            outcomes.append(DraftSnapshotOutcome(
                draft_id, DraftSnapshotStatus.KEPT if path.is_file() else DraftSnapshotStatus.LOST,
                path, served_draft_ids=tuple(served_ids),
            ))
            continue

        served = _picks(rows, draft_id)
        if path.exists():
            status, detail = _check_existing(path, draft_id, served)
            outcomes.append(DraftSnapshotOutcome(draft_id, status, path, detail=detail))
            continue

        try:
            atomic_write_text(path, json.dumps({**choices, LEAGUE_DRAFT_KEY: draft}, ensure_ascii=False))
        except OSError as exc:
            outcomes.append(DraftSnapshotOutcome(
                draft_id, DraftSnapshotStatus.WRITE_FAILED, path, detail=str(exc),
            ))
            continue
        outcomes.append(DraftSnapshotOutcome(draft_id, DraftSnapshotStatus.SAVED, path))
    return outcomes
