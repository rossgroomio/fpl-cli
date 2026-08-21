"""Validation helpers for per-team config files against the live league.

Promotion and relegation are the one routine way a per-team file goes wrong
without going stale by date: rebuilt in early August it still describes last
season's twenty clubs and passes any `last_updated` check. Diffing its keys
against the `bootstrap-static` team list is what actually catches the
rollover, so the check lives here rather than being reimplemented per file.
"""

from __future__ import annotations

from collections.abc import Iterable


def describe_team_set_mismatch(
    label: str,
    stored: Iterable[str],
    current: Iterable[str],
    *,
    verb: str,
) -> str | None:
    """Describe how a stored per-team file has drifted from the live team list.

    Args:
        label: File name to name in the message, e.g. "team_ratings.yaml"
        stored: Team short names the file covers
        current: Team short names in the league right now
        verb: Third-person verb for the clubs the file should have dropped,
            e.g. "rates" gives "still rates BUR, WHU, WOL"

    Returns:
        A sentence naming the specific missing and extra clubs, or None when
        the sets agree or `current` is empty (nothing to diff against, so
        silence beats blaming the file for an API that returned nothing).
    """
    live = {team.upper() for team in current if team}
    if not live:
        return None

    known = {team.upper() for team in stored if team}
    missing = sorted(live - known)
    extra = sorted(known - live)
    if not missing and not extra:
        return None

    parts = []
    if missing:
        parts.append(f"is missing {', '.join(missing)}")
    if extra:
        parts.append(f"still {verb} {', '.join(extra)}")
    return f"{label} {' and '.join(parts)}"
