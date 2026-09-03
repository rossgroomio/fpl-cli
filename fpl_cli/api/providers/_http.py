"""HTTP plumbing shared by the providers: 429 backoff and auth-safe error excerpts.

A rate limit is the one provider failure that clears itself with time, so it
is retried here, per request: with the server's own Retry-After when it sends
one, and with exponential backoff plus jitter when it does not. Every other
status is handed back untouched for the provider's own `raise_for_status`, so
what counts as an error stays each provider's decision. `dataset_fetcher`
draws the same 429-is-transient line for the historical datasets; this is the
provider layer's copy of it (#184).
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any

import httpx

from ._models import ProviderError, RateLimitError

logger = logging.getLogger(__name__)

_MAX_ERROR_DETAIL = 200

# Added on top of a Retry-After so concurrent requests limited in the same
# window do not all come back in the same instant and trip it again.
_RETRY_AFTER_JITTER = 1.0


def error_detail(response: httpx.Response) -> str:
    """Extract a short, auth-safe excerpt from an error response.

    Reads `{"error": {"message": ...}}` (or `{"error": "..."}`) when the body
    has that shape and falls back to the raw text otherwise. It runs on an
    error path, so no body shape -- a bare array, a string, no JSON at all --
    may make it raise.
    """
    try:
        body = response.json()
    except json.JSONDecodeError:
        body = None
    if isinstance(body, dict):
        error = body.get("error")
        if isinstance(error, dict):
            msg = str(error.get("message") or "")
        else:
            msg = error if isinstance(error, str) else ""
    else:
        try:
            msg = (response.text or "")[:_MAX_ERROR_DETAIL]
        except Exception:  # noqa: BLE001 — fallback-of-fallback
            return ""
    if not msg:
        return ""
    return f": {msg[:_MAX_ERROR_DETAIL]}"


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    """How long a rate-limited request waits, and how many times it tries.

    `max_attempts` counts the first request, so the default is three retries.
    The delays are sized for a per-minute quota tripped by a small burst: the
    first retry lands after a second or two, the last after roughly ten, and
    a Retry-After overrides the lot. Anything the provider still refuses
    after that is reported rather than waited out -- a CLI run is interactive.
    """

    max_attempts: int = 4
    base_delay: float = 2.0
    max_delay: float = 30.0

    def delay(
        self,
        retry: int,
        retry_after: float | None,
        *,
        rng: Callable[[float, float], float] = random.uniform,
    ) -> float:
        """Seconds to wait before retry number `retry` (0-based).

        A Retry-After is honoured as given, capped at `max_delay`, plus up to
        a second of jitter. Without one the wait doubles per retry from
        `base_delay` up to `max_delay`, with the upper half jittered ("equal
        jitter"): the floor keeps a retry from landing inside the window that
        just refused it, the jitter spreads a burst out.
        """
        if retry_after is not None:
            return min(retry_after, self.max_delay) + rng(0.0, _RETRY_AFTER_JITTER)
        ceiling = min(self.max_delay, self.base_delay * (2 ** max(0, retry)))
        return ceiling / 2 + rng(0.0, ceiling / 2)


def retry_after_seconds(response: httpx.Response) -> float | None:
    """The Retry-After header as a non-negative number of seconds, or None.

    Both forms the header allows are read -- delta-seconds and an HTTP-date --
    and anything else is treated as absent, so a malformed header degrades to
    the computed backoff rather than to no retry.
    """
    raw = response.headers.get("Retry-After")
    if not isinstance(raw, str) or not raw.strip():
        return None
    raw = raw.strip()
    try:
        seconds = float(raw)
    except ValueError:
        try:
            when = parsedate_to_datetime(raw)
        except (TypeError, ValueError):
            return None
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        seconds = (when - datetime.now(timezone.utc)).total_seconds()
    return max(0.0, seconds)


async def post_with_retry(
    http: httpx.AsyncClient,
    path: str,
    *,
    payload: dict[str, Any],
    headers: dict[str, str],
    label: str,
    policy: RetryPolicy | None = None,
) -> httpx.Response:
    """POST, retrying only a 429, and return the first response that is not one.

    Raises `RateLimitError` once the policy's attempts are spent, carrying the
    last Retry-After the server sent. Any other status comes back as-is: the
    caller's `raise_for_status` decides what it means.
    """
    retry_policy = policy or RetryPolicy()
    attempts = max(1, retry_policy.max_attempts)
    attempt = 0
    while True:
        attempt += 1
        response = await http.post(path, json=payload, headers=headers)
        if response.status_code != 429:
            return response
        retry_after = retry_after_seconds(response)
        if attempt >= attempts:
            raise RateLimitError(
                f"{label} returned HTTP 429{error_detail(response)} "
                f"(still rate-limited after {attempts} attempt(s))",
                retry_after=retry_after,
            )
        delay = retry_policy.delay(attempt - 1, retry_after)
        logger.info(
            "%s rate-limited the request (HTTP 429); retrying in %.1fs (%d attempt(s) left)",
            label, delay, attempts - attempt,
        )
        await _sleep(delay)


async def post_json_with_retry(
    http: httpx.AsyncClient,
    path: str,
    *,
    payload: dict[str, Any],
    headers: dict[str, str],
    label: str,
    policy: RetryPolicy | None = None,
) -> Any:
    """POST with 429 backoff and turn every other failure into a ProviderError.

    The one place the providers' error handling lives, so a change to it
    reaches every provider at once: an error status becomes a sanitised
    ProviderError carrying the label and an auth-safe excerpt, a timeout says
    so, and a body that is not JSON is reported rather than parsed. Returns
    the decoded body for the provider to read.
    """
    try:
        response = await post_with_retry(
            http, path, payload=payload, headers=headers, label=label, policy=policy,
        )
        response.raise_for_status()
    except httpx.HTTPStatusError as e:
        raise ProviderError(
            f"{label} returned HTTP {e.response.status_code}{error_detail(e.response)}"
        ) from None
    except httpx.TimeoutException:
        raise ProviderError(f"{label} request timed out") from None

    try:
        return response.json()
    except json.JSONDecodeError as e:
        raise ProviderError(f"{label} returned invalid JSON: {e}") from None


class QueryPacer:
    """Keeps request starts at least `spacing` seconds apart.

    An in-flight cap bounds a burst; this bounds the rate, which is what a
    per-minute quota measures. Together they are a token bucket whose depth is
    the cap. Reserving the next slot needs no lock: nothing in it awaits, so
    under asyncio's cooperative scheduling nothing can interleave between
    reading the last slot and writing the next.
    """

    def __init__(self, spacing: float) -> None:
        self.spacing = max(0.0, spacing)
        self._next_start: float | None = None

    async def wait_turn(self) -> None:
        """Return once this caller may start."""
        if self.spacing <= 0:
            return
        now = asyncio.get_running_loop().time()
        start = now if self._next_start is None else max(now, self._next_start)
        self._next_start = start + self.spacing
        if start > now:
            await _sleep(start - now)


async def _sleep(seconds: float) -> None:
    """Every wait in this module goes through here, so a test can make it free."""
    await asyncio.sleep(seconds)
