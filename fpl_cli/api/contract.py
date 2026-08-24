"""Runtime tripwires for the CSV dataset contracts.

The dataset clients degrade gracefully by design: a malformed row is
skipped, a missing file becomes an empty result, and the command keeps
running. That is the right response to a transient blip and the wrong one
to upstream schema drift, where every row is "malformed", the degradation
is total, and nothing says so (#97). The helpers here let a parser keep
degrading exactly as before while naming the provider that drifted and
the signal that was lost.

Each client declares the columns its parsers index directly as a
module-level frozenset; the header check here and the ``fpl doctor
--providers`` probe both assert against the same constant, so the
declared contract cannot drift from what the parser actually consumes.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence

logger = logging.getLogger(__name__)


def missing_columns(
    fieldnames: Sequence[str] | None, required: frozenset[str]
) -> set[str]:
    """Columns from `required` absent from a CSV header row."""
    return set(required) - set(fieldnames or ())


def header_covers(
    source: str,
    fieldnames: Sequence[str] | None,
    required: frozenset[str],
    *,
    degraded: str,
) -> bool:
    """Check a CSV header covers the parser's required columns, warning if not.

    Returns True when every required column is present. On a miss, warns with
    the provider file and the specific columns so the drift is attributable,
    then leaves the caller to take its usual empty-result path.

    Args:
        source: Provider file to name in the warning, e.g. "vaastav 2024-25
            players_raw.csv".
        fieldnames: The header row (DictReader.fieldnames).
        required: Columns the parser indexes directly.
        degraded: The user-facing consequence, e.g. "price-trend signals are
            unavailable".
    """
    missing = missing_columns(fieldnames, required)
    if not missing:
        return True
    logger.warning(
        "%s is missing expected column(s) %s — the upstream format may have changed; %s",
        source,
        ", ".join(sorted(missing)),
        degraded,
    )
    return False


def warn_all_rows_skipped(source: str, row_count: int, *, degraded: str) -> None:
    """Announce the empty-output tripwire: rows came in, none survived parsing.

    For the drift a header check cannot see — the columns are all present but
    every value fails conversion, so a non-empty file quietly becomes an empty
    dataset.
    """
    logger.warning(
        "%s has %d row(s) but none could be parsed — the upstream format may have changed; %s",
        source,
        row_count,
        degraded,
    )
