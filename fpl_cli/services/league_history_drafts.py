"""Snapshots of each held draft's picks, kept in the league-history ledger.

Layout::

    <data_dir>/league_history/<season>/draft-<league_id>/drafts/draft-<draft_id>.json
    <data_dir>/league_history/<season>/draft-<league_id>/drafts/lost-draft-<draft_id>.json

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

A held draft the API has stopped serving with no usable copy is gone for good.
The first run to notice records that in `lost-draft-<draft_id>.json`, so the
loss is reported once rather than on every recap for the rest of the season.
"""

from __future__ import annotations

import json
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
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
    # No longer served, and the file holds its picks: the snapshot doing its job.
    KEPT = "kept"
    # Served, and the file holds different picks. Left as it is.
    DIFFERS = "differs"
    # Served, and the file is not a snapshot this can read. Left as it is.
    UNREADABLE = "unreadable"
    # No longer served and no usable copy: the picks are gone. Noticed for
    # the first time on this run.
    LOST = "lost"
    # Gone, and an earlier run already recorded the loss.
    LOST_NOTED = "lost_noted"
    # Served, but the file could not be written; the next run tries again.
    WRITE_FAILED = "write_failed"


@dataclass(frozen=True)
class DraftSnapshotOutcome:
    """One completed draft's snapshot status after this run."""

    draft_id: int
    status: DraftSnapshotStatus
    path: Path
    # LOST and LOST_NOTED: the drafts the endpoint served picks for instead.
    served_draft_ids: tuple[int, ...] = ()
    # What is wrong with the file at `path`, phrased to follow it ("the file
    # at <path> is not valid JSON"). DIFFERS and UNREADABLE always; LOST when
    # a file is there but holds no usable copy; WRITE_FAILED with the error.
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


def lost_draft_path(season: str, league_id: int, draft_id: int) -> Path:
    """Where a run records that one draft's picks were already gone."""
    return draft_snapshot_dir(season, league_id) / f"lost-draft-{draft_id}.json"


def _is_id(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_timestamp(value: str) -> bool:
    """Whether a string is an ISO 8601 time, as the API sends `draft_completed`."""
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    return True


def is_recognised_draft(entry: Any) -> bool:
    """Whether a `league.drafts` entry has the shape this module reads.

    A numeric `id` and a `draft_completed` key. The live API sends the time the
    draft was held as an ISO timestamp and `null` for one still to be held; a
    bool is accepted too. An entry missing the key, or carrying anything else
    (an empty or non-timestamp string, a number), cannot be told apart from a
    draft still to be held, so a caller that skipped it quietly would lose a
    held draft's picks with no sign.
    """
    if not (isinstance(entry, dict) and _is_id(entry.get("id")) and "draft_completed" in entry):
        return False
    completed = entry["draft_completed"]
    if isinstance(completed, str):
        return _is_timestamp(completed)
    return completed is None or isinstance(completed, bool)


def completed_drafts(league_drafts: Iterable[Any]) -> list[dict[str, Any]]:
    """The entries of `league.drafts` the league says have been held.

    Held means a truthy `draft_completed`: the completion timestamp the API
    sends, or `True`. `null` is a draft still to be held.

    A draft still to be held, or under way, has no settled picks to keep: a
    partial draft saved now would read as a conflict once it finished.
    """
    return [
        draft for draft in league_drafts
        if is_recognised_draft(draft) and draft["draft_completed"]
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


def _stored_picks(path: Path, draft_id: int) -> tuple[Counter[str] | None, str | None]:
    """The picks a file holds for one draft, or what is wrong with it.

    `(None, None)` when there is no file. Read without probing first, so a
    permission problem on the directory is a problem with the file rather
    than an exception for the caller.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None, None
    except (OSError, ValueError) as exc:
        return None, f"could not be read ({exc})"
    try:
        stored = json.loads(text)
    except ValueError as exc:
        return None, f"is not valid JSON ({exc})"
    if not isinstance(stored, dict) or not isinstance(stored.get("choices"), list):
        return None, "holds no `choices` list"
    return _picks(_choice_rows(stored), draft_id), None


def read_saved_choices(season: str, league_id: int, draft_id: int) -> list[dict[str, Any]]:
    """The pick rows a readable snapshot holds for one draft, as the API served
    them; empty when there is no file, it cannot be read, or it holds none of
    this draft's picks.

    What a reader falls back on once the API stops serving a held draft (#458).
    A file that is wrong says nothing here: `snapshot_draft_choices()` is what
    reports it, so a reader treats it as no copy rather than a second voice.
    """
    try:
        stored = json.loads(draft_snapshot_path(season, league_id, draft_id).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    if not isinstance(stored, dict):
        return []
    return [row for row in _choice_rows(stored) if row.get("draft") == draft_id]


def has_saved_picks(season: str, league_id: int, draft_id: int) -> bool:
    """Whether a readable snapshot holds at least one of this draft's picks."""
    picks, _ = _stored_picks(draft_snapshot_path(season, league_id, draft_id), draft_id)
    return bool(picks)


def _difference(kept: Counter[str], served: Counter[str], draft_id: int) -> str:
    return (
        f"holds {kept.total()} picks for draft {draft_id} where the API serves {served.total()}: "
        f"{(served - kept).total()} served are not in the file, and {(kept - served).total()} "
        "in the file are not served"
    )


def _record_loss(
    season: str, league_id: int, draft: Mapping[str, Any], served_ids: Sequence[int],
) -> bool:
    """Record a draft's loss once; True when an earlier run already had."""
    marker = lost_draft_path(season, league_id, draft["id"])
    try:
        if marker.is_file():
            return True
        atomic_write_text(marker, json.dumps({
            "draft_id": draft["id"],
            LEAGUE_DRAFT_KEY: dict(draft),
            "noticed_at": datetime.now(tz=timezone.utc).isoformat(),
            "served_draft_ids": list(served_ids),
        }, ensure_ascii=False))
    except (OSError, ValueError):
        # Unrecorded, so the next run reports the loss again: a repeat is
        # better than a loss nobody hears about.
        pass
    return False


def snapshot_draft_choices(
    season: str,
    league_id: int,
    completed: Sequence[Mapping[str, Any]],
    choices: Mapping[str, Any],
) -> list[DraftSnapshotOutcome]:
    """Keep every completed draft's picks the endpoint still serves.

    *completed* is `completed_drafts()` of `league.drafts`; *choices* is
    `get_draft_choices`' response for the same league, already checked to
    carry a `choices` list. One outcome per completed draft, in order. Raises
    nothing for a file problem: a file that cannot be read or written is an
    outcome, so the caller reports it and carries on.
    """
    rows = _choice_rows(choices)
    served_ids = sorted({row["draft"] for row in rows if _is_id(row.get("draft"))})

    outcomes: list[DraftSnapshotOutcome] = []
    for draft in completed:
        draft_id: int = draft["id"]
        path = draft_snapshot_path(season, league_id, draft_id)
        stored, problem = _stored_picks(path, draft_id)

        if draft_id not in served_ids:
            if stored:
                outcomes.append(DraftSnapshotOutcome(draft_id, DraftSnapshotStatus.KEPT, path))
                continue
            # A file that is there but holds none of the picks -- a capture
            # taken after the redraft was scheduled, an error page -- is no
            # copy at all, and must not stand in for one.
            detail = problem or ("holds none of its picks" if stored is not None else None)
            noted = _record_loss(season, league_id, draft, served_ids)
            outcomes.append(DraftSnapshotOutcome(
                draft_id, DraftSnapshotStatus.LOST_NOTED if noted else DraftSnapshotStatus.LOST,
                path, served_draft_ids=tuple(served_ids), detail=detail,
            ))
            continue

        served = _picks(rows, draft_id)
        if problem is not None:
            outcomes.append(DraftSnapshotOutcome(
                draft_id, DraftSnapshotStatus.UNREADABLE, path, detail=problem,
            ))
        elif stored is not None:
            outcomes.append(
                DraftSnapshotOutcome(draft_id, DraftSnapshotStatus.UNCHANGED, path)
                if stored == served else
                DraftSnapshotOutcome(
                    draft_id, DraftSnapshotStatus.DIFFERS, path,
                    detail=_difference(stored, served, draft_id),
                ),
            )
        else:
            try:
                atomic_write_text(
                    path, json.dumps({**choices, LEAGUE_DRAFT_KEY: dict(draft)}, ensure_ascii=False),
                )
            except (OSError, ValueError) as exc:
                outcomes.append(DraftSnapshotOutcome(
                    draft_id, DraftSnapshotStatus.WRITE_FAILED, path, detail=str(exc),
                ))
                continue
            outcomes.append(DraftSnapshotOutcome(draft_id, DraftSnapshotStatus.SAVED, path))
    return outcomes
