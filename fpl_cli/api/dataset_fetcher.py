"""Disk-caching HTTP fetcher for GitHub-hosted dataset files.

Caches responses to disk with ETag-based conditional requests and TTL gating.
Designed for raw.githubusercontent.com but works with any HTTP source that
supports ETag/If-None-Match.
"""

from __future__ import annotations

import logging
import os
import tempfile
import time
from datetime import timedelta
from pathlib import Path

import httpx

logger = logging.getLogger(__name__)


class DatasetFetcher:
    """Fetches files from a base URL with disk caching, ETag validation, and TTL gating.

    Cache layout mirrors the URL path structure under ``cache_dir``.
    ETag values are stored in sidecar files (e.g. ``players_raw.csv.etag``).
    """

    def __init__(
        self,
        base_url: str,
        cache_dir: Path,
        ttl: timedelta,
        timeout: float = 30.0,
    ) -> None:
        self.base_url = base_url
        self.cache_dir = cache_dir
        self.ttl = ttl
        self._http = httpx.AsyncClient(base_url=base_url, timeout=timeout)

    async def close(self) -> None:
        await self._http.aclose()

    async def __aenter__(self) -> DatasetFetcher:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    async def get(self, path: str, ttl: timedelta | None = None) -> str:
        """Fetch a file, serving from cache when possible.

        Args:
            path: Relative path under the base URL (e.g. ``2024-25/players_raw.csv``).
            ttl: Per-request TTL override. ``None`` uses the constructor default.

        Returns:
            The file content as a string.

        Raises:
            httpx.HTTPStatusError: On 4xx responses (always) or 5xx/429 when no cache exists.
            httpx.TransportError: On network failures when no cache exists.
        """
        effective_ttl = ttl or self.ttl
        cache_path = self.cache_dir / path
        etag_path = Path(str(cache_path) + ".etag")

        # TTL gate: serve from disk if fresh
        if cache_path.is_file():
            age = time.time() - cache_path.stat().st_mtime
            if age < effective_ttl.total_seconds():
                return cache_path.read_text(encoding="utf-8")

        # Build conditional request headers
        headers: dict[str, str] = {}
        if etag_path.is_file():
            headers["If-None-Match"] = etag_path.read_text(encoding="utf-8").strip()

        try:
            response = await self._http.get(f"/{path}", headers=headers)
        except httpx.TransportError:
            if cache_path.is_file():
                logger.warning("Network error fetching %s; serving stale cache", path)
                return cache_path.read_text(encoding="utf-8")
            raise

        # 304 Not Modified
        if response.status_code == 304:
            if cache_path.is_file():
                return cache_path.read_text(encoding="utf-8")
            # Cache file deleted externally; re-fetch unconditionally
            response = await self._http.get(f"/{path}")

        # 4xx (except 429): always propagate
        if 400 <= response.status_code < 500 and response.status_code != 429:
            response.raise_for_status()

        # 5xx / 429: serve stale if available
        if response.status_code == 429 or response.status_code >= 500:
            if cache_path.is_file():
                logger.warning(
                    "HTTP %d fetching %s; serving stale cache",
                    response.status_code,
                    path,
                )
                return cache_path.read_text(encoding="utf-8")
            response.raise_for_status()

        # 200: store and return
        text = response.text
        cache_path.parent.mkdir(parents=True, exist_ok=True)

        # Atomic write for the data file
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=cache_path.parent,
            suffix=".tmp",
            delete=False,
        ) as f:
            f.write(text)
            tmp_path = f.name
        os.replace(tmp_path, cache_path)

        # ETag sidecar (non-atomic; corruption just means an unconditional GET next time)
        etag = response.headers.get("ETag")
        if etag:
            etag_path.write_text(etag, encoding="utf-8")

        return text
