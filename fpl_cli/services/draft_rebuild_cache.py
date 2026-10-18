"""The squads a lost draft's picks were rebuilt from, kept so later replays read none (#472).

Layout::

    <cache_dir>/draft_rebuilds/draft-<league_id>/draft-<draft_id>.json

Where a held draft's picks are in neither the choices feed nor a snapshot
(`league_history_drafts`), the replay rebuilds who drafted whom from every
manager's squad at the draft's gameweek (`draft_provenance._start_from_squads`),
one request per manager on every run. Once that gameweek has started its
squads are locked and every move before it is in the log, so the requests buy
the same answer every time. A replay that passed its checks on squads read
from the API keeps them here (`draft_provenance.keep_rebuild_squads()`), and
later runs read the file instead.

The squads, not the set rebuilt from them: a run that reads the file undoes
the moves made before the gameweek and replays on exactly as it would from
squads just read, so a wrong file faces every check a wrong read does, the
moves fitting the squads included. A replay that refuses squads read from here
discards the file, so the next run reads them again rather than refusing on
the file's account for the rest of the season.

The cache dir, not the ledger beside the snapshots: the endpoint serves a
gameweek's squads all season, so a lost file costs one read per manager and
nothing else, and none of the snapshot's write-once and conflict rules apply.
Each file is stamped with its season and the draft's gameweek; one that names
another, or that cannot be read, is no file.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from pathlib import Path

from fpl_cli.paths import user_cache_dir
from fpl_cli.services.league_history_drafts import api_id
from fpl_cli.utils.files import atomic_write_text

# Debug, never warning: fpl-cli configures no logging handlers, so a warning
# would print raw to stderr, and a cache miss costs requests, not output.
logger = logging.getLogger(__name__)

REBUILD_DIRNAME = "draft_rebuilds"


def rebuild_squads_path(league_id: int, draft_id: int) -> Path:
    """Where one draft's squads are kept, resolved per call so an `FPL_CLI_CACHE_DIR` set after import is honoured."""
    return user_cache_dir() / REBUILD_DIRNAME / f"draft-{league_id}" / f"draft-{draft_id}.json"


def read_rebuild_squads(season: str, league_id: int, draft_id: int, *, gameweek: int) -> dict[int, int] | None:
    """The squads kept for one draft at its *gameweek* (draft element id to entry id), or None.

    None for no file, one that cannot be read or is not the shape
    `save_rebuild_squads()` writes, and one stamped with another season or
    gameweek: each is no file, and the run reads the squads from the API.
    """
    path = rebuild_squads_path(league_id, draft_id)
    try:
        stored = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        logger.debug("Kept squads for draft %s unreadable (%s); reading them again", draft_id, exc)
        return None
    if not isinstance(stored, dict) or stored.get("season") != season or api_id(stored.get("gameweek")) != gameweek:
        logger.debug("Kept squads for draft %s are for another season or gameweek; reading them again", draft_id)
        return None
    rows = stored.get("squads")
    rows = rows if isinstance(rows, dict) else {}
    # JSON keys are strings: each is a draft element id, each value an entry id.
    squads = {
        int(element): entry for element, value in rows.items()
        if element.isdigit() and (entry := api_id(value)) is not None
    }
    if not squads or len(squads) != len(rows):
        logger.debug("Kept squads for draft %s hold no usable squads; reading them again", draft_id)
        return None
    return squads


def save_rebuild_squads(
    season: str, league_id: int, draft_id: int, *, gameweek: int, squads: Mapping[int, int],
) -> None:
    """Keep one draft's squads at its *gameweek*. A write that fails is logged and
    costs only the next run's reads."""
    payload = {
        "season": season,
        "gameweek": gameweek,
        "squads": {str(element): entry for element, entry in sorted(squads.items())},
    }
    try:
        atomic_write_text(rebuild_squads_path(league_id, draft_id), json.dumps(payload))
    except (OSError, ValueError) as exc:
        logger.debug("Could not keep the squads for draft %s: %s", draft_id, exc)


def discard_rebuild_squads(league_id: int, draft_id: int) -> None:
    """Drop one draft's kept squads, so the next run reads them again."""
    try:
        rebuild_squads_path(league_id, draft_id).unlink(missing_ok=True)
    except OSError as exc:
        logger.debug("Could not drop the kept squads for draft %s: %s", draft_id, exc)
