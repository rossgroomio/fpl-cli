"""Tests for DatasetFetcher disk caching with ETag and TTL gating."""

from __future__ import annotations

import asyncio
import os
import time
from datetime import timedelta
from pathlib import Path

import httpx
import pytest
import respx

from fpl_cli.api.dataset_fetcher import DatasetFetcher

BASE_URL = "https://raw.githubusercontent.com/test/repo/master/data"
SAMPLE_CSV = "id,name\n1,Salah\n2,Haaland\n"
UPDATED_CSV = "id,name\n1,Salah\n2,Haaland\n3,Palmer\n"


@pytest.fixture()
def cache_dir(tmp_path):
    return tmp_path / "cache"


@pytest.fixture()
def fetcher(cache_dir):
    return DatasetFetcher(
        base_url=BASE_URL,
        cache_dir=cache_dir,
        ttl=timedelta(hours=4),
    )


# --- Happy path ---


@respx.mock
async def test_first_fetch_stores_to_disk(fetcher, cache_dir):
    respx.get(f"{BASE_URL}/2024-25/players_raw.csv").mock(
        return_value=httpx.Response(200, text=SAMPLE_CSV),
    )

    result = await fetcher.get("2024-25/players_raw.csv")

    assert result == SAMPLE_CSV
    assert (cache_dir / "2024-25" / "players_raw.csv").read_text(encoding="utf-8") == SAMPLE_CSV


@respx.mock
async def test_first_fetch_stores_etag_sidecar(fetcher, cache_dir):
    respx.get(f"{BASE_URL}/2024-25/players_raw.csv").mock(
        return_value=httpx.Response(200, text=SAMPLE_CSV, headers={"ETag": '"abc123"'}),
    )

    await fetcher.get("2024-25/players_raw.csv")

    etag_path = cache_dir / "2024-25" / "players_raw.csv.etag"
    assert etag_path.read_text(encoding="utf-8") == '"abc123"'


@respx.mock
async def test_ttl_fresh_serves_from_disk(fetcher, cache_dir):
    route = respx.get(f"{BASE_URL}/2024-25/players_raw.csv").mock(
        return_value=httpx.Response(200, text=SAMPLE_CSV),
    )

    # First fetch populates cache
    await fetcher.get("2024-25/players_raw.csv")
    assert route.call_count == 1

    # Second fetch within TTL serves from disk
    result = await fetcher.get("2024-25/players_raw.csv")
    assert result == SAMPLE_CSV
    assert route.call_count == 1


@respx.mock
async def test_expired_cache_sends_if_none_match_304(fetcher, cache_dir):
    # Populate cache with ETag
    respx.get(f"{BASE_URL}/test.csv").mock(
        return_value=httpx.Response(200, text=SAMPLE_CSV, headers={"ETag": '"v1"'}),
    )
    await fetcher.get("test.csv")

    # Expire the cache by backdating mtime
    cached = cache_dir / "test.csv"
    old_time = time.time() - 5 * 3600  # 5 hours ago
    os.utime(cached, (old_time, old_time))

    # Mock 304 response
    route = respx.get(f"{BASE_URL}/test.csv").mock(
        return_value=httpx.Response(304),
    )

    result = await fetcher.get("test.csv")
    assert result == SAMPLE_CSV
    # Verify If-None-Match header was sent with the stored ETag
    request = route.calls.last.request
    assert request.headers["If-None-Match"] == '"v1"'


@respx.mock
async def test_expired_cache_200_updates_file_and_etag(fetcher, cache_dir):
    # Populate cache
    respx.get(f"{BASE_URL}/test.csv").mock(
        return_value=httpx.Response(200, text=SAMPLE_CSV, headers={"ETag": '"v1"'}),
    )
    await fetcher.get("test.csv")

    # Expire the cache
    cached = cache_dir / "test.csv"
    old_time = time.time() - 5 * 3600
    os.utime(cached, (old_time, old_time))

    # New content with new ETag
    respx.get(f"{BASE_URL}/test.csv").mock(
        return_value=httpx.Response(200, text=UPDATED_CSV, headers={"ETag": '"v2"'}),
    )

    result = await fetcher.get("test.csv")
    assert result == UPDATED_CSV
    assert cached.read_text(encoding="utf-8") == UPDATED_CSV
    assert (cache_dir / "test.csv.etag").read_text(encoding="utf-8") == '"v2"'


# --- Edge cases ---


@respx.mock
async def test_no_etag_in_response(fetcher, cache_dir):
    respx.get(f"{BASE_URL}/test.csv").mock(
        return_value=httpx.Response(200, text=SAMPLE_CSV),
    )

    await fetcher.get("test.csv")

    assert (cache_dir / "test.csv").exists()
    assert not (cache_dir / "test.csv.etag").exists()


@respx.mock
async def test_no_etag_sidecar_sends_unconditional_get(fetcher, cache_dir):
    # Populate cache without ETag
    respx.get(f"{BASE_URL}/test.csv").mock(
        return_value=httpx.Response(200, text=SAMPLE_CSV),
    )
    await fetcher.get("test.csv")

    # Expire the cache
    cached = cache_dir / "test.csv"
    old_time = time.time() - 5 * 3600
    os.utime(cached, (old_time, old_time))

    # Should send GET without If-None-Match
    route = respx.get(f"{BASE_URL}/test.csv").mock(
        return_value=httpx.Response(200, text=UPDATED_CSV),
    )

    result = await fetcher.get("test.csv")
    assert result == UPDATED_CSV
    request = route.calls.last.request
    assert "If-None-Match" not in request.headers


@respx.mock
async def test_304_with_missing_cache_refetches(fetcher, cache_dir):
    """If 304 arrives but cached file was deleted, re-fetch unconditionally."""
    # Create ETag sidecar without a cached file
    etag_dir = cache_dir / "test.csv.etag"
    etag_dir.parent.mkdir(parents=True, exist_ok=True)
    etag_dir.write_text('"v1"', encoding="utf-8")

    # First call gets 304 (but no cache file), then re-fetches
    respx.get(f"{BASE_URL}/test.csv").mock(
        side_effect=[
            httpx.Response(304),
            httpx.Response(200, text=SAMPLE_CSV, headers={"ETag": '"v2"'}),
        ],
    )

    result = await fetcher.get("test.csv")
    assert result == SAMPLE_CSV


@respx.mock
async def test_per_request_ttl_override(fetcher, cache_dir):
    route = respx.get(f"{BASE_URL}/test.csv").mock(
        return_value=httpx.Response(200, text=SAMPLE_CSV),
    )

    await fetcher.get("test.csv")
    assert route.call_count == 1

    # Expire beyond default 4h TTL but within 30-day override
    cached = cache_dir / "test.csv"
    old_time = time.time() - 5 * 3600  # 5 hours ago
    os.utime(cached, (old_time, old_time))

    # With 30-day TTL, file is still fresh
    result = await fetcher.get("test.csv", ttl=timedelta(days=30))
    assert result == SAMPLE_CSV
    assert route.call_count == 1  # No HTTP request made


@respx.mock
async def test_nested_path_creates_directories(fetcher, cache_dir):
    respx.get(f"{BASE_URL}/2024-25/gws/merged_gw.csv").mock(
        return_value=httpx.Response(200, text=SAMPLE_CSV),
    )

    await fetcher.get("2024-25/gws/merged_gw.csv")

    assert (cache_dir / "2024-25" / "gws" / "merged_gw.csv").exists()


# --- Error paths ---


@respx.mock
async def test_transport_error_with_cache_serves_stale(fetcher, cache_dir):
    respx.get(f"{BASE_URL}/test.csv").mock(
        return_value=httpx.Response(200, text=SAMPLE_CSV),
    )
    await fetcher.get("test.csv")

    # Expire cache
    cached = cache_dir / "test.csv"
    old_time = time.time() - 5 * 3600
    os.utime(cached, (old_time, old_time))

    # Network failure
    respx.get(f"{BASE_URL}/test.csv").mock(side_effect=httpx.ConnectError("offline"))

    result = await fetcher.get("test.csv")
    assert result == SAMPLE_CSV


@respx.mock
async def test_429_with_cache_serves_stale(fetcher, cache_dir):
    respx.get(f"{BASE_URL}/test.csv").mock(
        return_value=httpx.Response(200, text=SAMPLE_CSV),
    )
    await fetcher.get("test.csv")

    # Expire cache
    cached = cache_dir / "test.csv"
    old_time = time.time() - 5 * 3600
    os.utime(cached, (old_time, old_time))

    respx.get(f"{BASE_URL}/test.csv").mock(
        return_value=httpx.Response(429, text="rate limited"),
    )

    result = await fetcher.get("test.csv")
    assert result == SAMPLE_CSV


@respx.mock
async def test_500_with_cache_serves_stale(fetcher, cache_dir):
    respx.get(f"{BASE_URL}/test.csv").mock(
        return_value=httpx.Response(200, text=SAMPLE_CSV),
    )
    await fetcher.get("test.csv")

    cached = cache_dir / "test.csv"
    old_time = time.time() - 5 * 3600
    os.utime(cached, (old_time, old_time))

    respx.get(f"{BASE_URL}/test.csv").mock(
        return_value=httpx.Response(500, text="server error"),
    )

    result = await fetcher.get("test.csv")
    assert result == SAMPLE_CSV


@respx.mock
async def test_404_raises_regardless_of_cache(fetcher, cache_dir):
    respx.get(f"{BASE_URL}/test.csv").mock(
        return_value=httpx.Response(200, text=SAMPLE_CSV),
    )
    await fetcher.get("test.csv")

    cached = cache_dir / "test.csv"
    old_time = time.time() - 5 * 3600
    os.utime(cached, (old_time, old_time))

    respx.get(f"{BASE_URL}/test.csv").mock(
        return_value=httpx.Response(404, text="not found"),
    )

    with pytest.raises(httpx.HTTPStatusError) as exc_info:
        await fetcher.get("test.csv")
    assert exc_info.value.response.status_code == 404


@respx.mock
async def test_403_raises_regardless_of_cache(fetcher, cache_dir):
    respx.get(f"{BASE_URL}/test.csv").mock(
        return_value=httpx.Response(200, text=SAMPLE_CSV),
    )
    await fetcher.get("test.csv")

    cached = cache_dir / "test.csv"
    old_time = time.time() - 5 * 3600
    os.utime(cached, (old_time, old_time))

    respx.get(f"{BASE_URL}/test.csv").mock(
        return_value=httpx.Response(403, text="forbidden"),
    )

    with pytest.raises(httpx.HTTPStatusError) as exc_info:
        await fetcher.get("test.csv")
    assert exc_info.value.response.status_code == 403


@respx.mock
async def test_transport_error_no_cache_raises(fetcher):
    respx.get(f"{BASE_URL}/test.csv").mock(side_effect=httpx.ConnectError("offline"))

    with pytest.raises(httpx.ConnectError):
        await fetcher.get("test.csv")


@respx.mock
async def test_404_no_cache_raises(fetcher):
    """First fetch returning 404 raises immediately (no cache to fall back on)."""
    respx.get(f"{BASE_URL}/test.csv").mock(
        return_value=httpx.Response(404, text="not found"),
    )
    with pytest.raises(httpx.HTTPStatusError) as exc_info:
        await fetcher.get("test.csv")
    assert exc_info.value.response.status_code == 404


@respx.mock
async def test_500_no_cache_raises(fetcher):
    """First fetch returning 500 raises immediately (no cache to fall back on)."""
    respx.get(f"{BASE_URL}/test.csv").mock(
        return_value=httpx.Response(500, text="server error"),
    )
    with pytest.raises(httpx.HTTPStatusError) as exc_info:
        await fetcher.get("test.csv")
    assert exc_info.value.response.status_code == 500


@respx.mock
async def test_429_no_cache_raises(fetcher):
    """First fetch returning 429 raises immediately (no cache to fall back on)."""
    respx.get(f"{BASE_URL}/test.csv").mock(
        return_value=httpx.Response(429, text="rate limited"),
    )
    with pytest.raises(httpx.HTTPStatusError) as exc_info:
        await fetcher.get("test.csv")
    assert exc_info.value.response.status_code == 429


# --- Integration ---


@respx.mock
async def test_concurrent_fetches_cache_independently(fetcher, cache_dir):
    respx.get(f"{BASE_URL}/a.csv").mock(
        return_value=httpx.Response(200, text="file_a"),
    )
    respx.get(f"{BASE_URL}/b.csv").mock(
        return_value=httpx.Response(200, text="file_b"),
    )

    results = await asyncio.gather(
        fetcher.get("a.csv"),
        fetcher.get("b.csv"),
    )

    assert results == ["file_a", "file_b"]
    assert (cache_dir / "a.csv").read_text(encoding="utf-8") == "file_a"
    assert (cache_dir / "b.csv").read_text(encoding="utf-8") == "file_b"


# --- Context manager ---


@respx.mock
async def test_async_context_manager():
    respx.get(f"{BASE_URL}/test.csv").mock(
        return_value=httpx.Response(200, text=SAMPLE_CSV),
    )

    async with DatasetFetcher(
        base_url=BASE_URL,
        cache_dir=Path("/tmp/test-fetcher"),
        ttl=timedelta(hours=1),
    ) as fetcher:
        result = await fetcher.get("test.csv")
        assert result == SAMPLE_CSV
