"""Perplexity provider for the Sonar API (web-grounded research)."""

from __future__ import annotations

import re
from typing import Any, ClassVar

from ._models import LLMResponse
from .openai_compat import OpenAICompatProvider

_BASE_URL = "https://api.perplexity.ai"


class PerplexityProvider(OpenAICompatProvider):
    """LLM provider for the Perplexity Sonar API.

    Extends OpenAICompatProvider with web search options, citation
    extraction, and citation-cleaning post-processing.
    """

    DEFAULT_MODEL: ClassVar[str] = "sonar-pro"
    DEFAULT_TIMEOUT: ClassVar[float] = 120.0
    API_KEY_ENV_VAR: ClassVar[str] = "PERPLEXITY_API_KEY"
    KEY_SETUP_URL: ClassVar[str] = "https://www.perplexity.ai/settings/api"
    _PROVIDER_LABEL: ClassVar[str] = "Perplexity"

    def __init__(
        self,
        model: str = DEFAULT_MODEL,
        timeout: float = DEFAULT_TIMEOUT,
        query_defaults: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(
            model=model, timeout=timeout, query_defaults=query_defaults,
            base_url=_BASE_URL,
        )

    def _build_payload(
        self, messages: list[dict[str, str]], merged: dict[str, Any],
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {"model": self.model, "messages": messages}
        search_recency_filter = merged.get("search_recency_filter", "week")
        if search_recency_filter:
            payload["web_search_options"] = {
                "search_recency_filter": search_recency_filter,
            }
        return payload

    def _parse_response(self, data: dict[str, Any]) -> LLMResponse:
        base = super()._parse_response(data)
        citations = data.get("citations", [])
        if not citations:
            return base
        return LLMResponse(
            content=base.content, model=base.model, usage=base.usage,
            citations=citations,
        )

    def post_process(self, content: str) -> str:
        """Clean citation markers and source sections from Perplexity output."""
        return _clean_citations(content)


def _clean_citations(text: str) -> str:
    """Remove citation markers and sources from text.

    Cleans the response for LLM consumption by:
    1. Removing inline citation numbers like [1], [2], [1][2], etc.
    2. Removing the sources/references section at the end
    3. Cleaning up extra whitespace
    """
    cleaned = re.sub(r"\[\d+\]", "", text)

    cleaned = re.sub(
        r"\n\s*(Sources|References|Citations)\s*:?\s*\n.*",
        "",
        cleaned,
        flags=re.IGNORECASE | re.DOTALL,
    )

    cleaned = re.sub(
        r"\n\s*\d+\.\s+https?://[^\s]+(\s*\n\s*\d+\.\s+https?://[^\s]+)*\s*$",
        "",
        cleaned,
    )

    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    cleaned = re.sub(r"\s+([.,;:!?])", r"\1", cleaned)
    cleaned = re.sub(r" {2,}", " ", cleaned)

    return cleaned.strip()
