"""FPL-Core-Insights dataset client for historical player data (2024-25+)."""
from __future__ import annotations

from datetime import timedelta

from fpl_cli.api.dataset_fetcher import DatasetFetcher

BASE_URL = "https://raw.githubusercontent.com/olbauday/FPL-Core-Insights/main/data"
DEFAULT_TTL = timedelta(hours=4)


def make_core_insights_fetcher(ttl: timedelta = DEFAULT_TTL) -> DatasetFetcher:
    """Create a DatasetFetcher configured for the FPL-Core-Insights GitHub dataset."""
    from fpl_cli.paths import user_cache_dir

    return DatasetFetcher(
        base_url=BASE_URL,
        cache_dir=user_cache_dir() / "datasets" / "core-insights",
        ttl=ttl,
    )
