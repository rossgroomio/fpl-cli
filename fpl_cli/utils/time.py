"""Timestamp formatting helpers locked to UK local time.

All user-facing timestamps (deadlines, kickoffs, report generation stamps) flow through
this module so displayed times match the user's wall clock. GMT/BST is resolved
automatically via the IANA database for the actual timestamp date.
"""

from datetime import datetime
from zoneinfo import ZoneInfo

UK_TZ = ZoneInfo("Europe/London")

_DEADLINE_FMT = "%a %d %b, %H:%M %Z"
_KICKOFF_FMT = "%a %H:%M %Z"
_GENERATED_AT_FMT = "%Y-%m-%d %H:%M %Z"


def _coerce(value: str | datetime) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=ZoneInfo("UTC"))
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None


def format_deadline(value: str | datetime) -> str:
    """Format a UTC timestamp as a UK-local deadline string, e.g. 'Sat 18 Apr, 18:30 BST'.

    Accepts ISO strings or timezone-aware datetimes. Returns input unchanged on empty
    strings or parse failure.
    """
    if not value:
        return value if isinstance(value, str) else ""
    dt = _coerce(value)
    if dt is None:
        return value  # type: ignore[return-value]
    return dt.astimezone(UK_TZ).strftime(_DEADLINE_FMT)


def format_kickoff(value: str | datetime) -> str:
    """Format a UTC timestamp as a UK-local kickoff string, e.g. 'Sat 15:00 BST'.

    Accepts ISO strings or timezone-aware datetimes.
    """
    if not value:
        return value if isinstance(value, str) else ""
    dt = _coerce(value)
    if dt is None:
        return value  # type: ignore[return-value]
    return dt.astimezone(UK_TZ).strftime(_KICKOFF_FMT)


def now_uk() -> datetime:
    """Return the current time as a timezone-aware datetime in Europe/London."""
    return datetime.now(UK_TZ)


def format_generated_at(dt: datetime | None = None) -> str:
    """Format a UK-local generation stamp, e.g. '2026-04-18 14:32 BST'. Defaults to now."""
    if dt is None:
        dt = now_uk()
    elif dt.tzinfo is None:
        dt = dt.replace(tzinfo=UK_TZ)
    else:
        dt = dt.astimezone(UK_TZ)
    return dt.strftime(_GENERATED_AT_FMT)
