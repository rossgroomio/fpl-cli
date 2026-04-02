"""Anthropic provider for the Messages API."""

from __future__ import annotations

import json
import os
from typing import Any, ClassVar, Self

import httpx

from ._models import LLMResponse, ProviderError, TokenUsage

_BASE_URL = "https://api.anthropic.com/v1"
_MAX_ERROR_DETAIL = 200


def _error_detail(response: httpx.Response) -> str:
    """Extract a short, auth-safe excerpt from an error response."""
    try:
        body = response.json()
        msg = str(body.get("error", {}).get("message") or "")
    except json.JSONDecodeError:
        try:
            msg = (response.text or "")[:_MAX_ERROR_DETAIL]
        except Exception:  # noqa: BLE001 — fallback-of-fallback
            return ""
    if not msg:
        return ""
    return f": {msg[:_MAX_ERROR_DETAIL]}"


class AnthropicProvider:
    """LLM provider for the Anthropic Messages API."""

    DEFAULT_MODEL: ClassVar[str] = "claude-sonnet-4-6"
    DEFAULT_TIMEOUT: ClassVar[float] = 60.0
    API_KEY_ENV_VAR: ClassVar[str] = "ANTHROPIC_API_KEY"
    KEY_SETUP_URL: ClassVar[str] = "https://console.anthropic.com/"

    def __init__(
        self,
        model: str = DEFAULT_MODEL,
        timeout: float = DEFAULT_TIMEOUT,
        query_defaults: dict[str, Any] | None = None,
    ) -> None:
        self.model = model
        self.timeout = timeout
        self.query_defaults = query_defaults or {}
        self.api_key = os.environ.get(self.API_KEY_ENV_VAR)
        self._http: httpx.AsyncClient | None = None

    @property
    def is_configured(self) -> bool:
        return bool(self.api_key)

    def _ensure_http(self) -> httpx.AsyncClient:
        if self._http is None:
            self._http = httpx.AsyncClient(base_url=_BASE_URL, timeout=self.timeout)
        return self._http

    async def query(
        self,
        prompt: str,
        system_prompt: str | None = None,
        **kwargs: Any,
    ) -> LLMResponse:
        if not self.api_key:
            raise ProviderError(
                f"{self.API_KEY_ENV_VAR} not set. "
                f"Get your key from {self.KEY_SETUP_URL}"
            )

        merged = {**self.query_defaults, **kwargs}
        max_tokens = int(merged.get("max_tokens", 1024))

        messages = [{"role": "user", "content": prompt}]
        payload: dict[str, Any] = {
            "model": self.model,
            "max_tokens": max_tokens,
            "messages": messages,
        }
        if system_prompt:
            payload["system"] = system_prompt

        headers = {
            "x-api-key": self.api_key,
            "anthropic-version": "2023-06-01",
            "Content-Type": "application/json",
        }

        try:
            response = await self._ensure_http().post("/messages", json=payload, headers=headers)
            response.raise_for_status()
        except httpx.HTTPStatusError as e:
            detail = _error_detail(e.response)
            raise ProviderError(
                f"Anthropic returned HTTP {e.response.status_code}{detail}"
            ) from None
        except httpx.TimeoutException:
            raise ProviderError("Anthropic request timed out") from None

        try:
            data = response.json()
        except json.JSONDecodeError as e:
            raise ProviderError(f"Anthropic returned invalid JSON: {e}") from None

        content = ""
        for block in data.get("content", []):
            if block.get("type") == "text":
                content += block.get("text", "")

        # Normalise usage: Anthropic returns input_tokens/output_tokens directly
        raw_usage = data.get("usage", {})
        usage = TokenUsage(
            input_tokens=raw_usage.get("input_tokens", 0),
            output_tokens=raw_usage.get("output_tokens", 0),
        )

        return LLMResponse(
            content=content,
            model=data.get("model", self.model),
            usage=usage,
        )

    def post_process(self, content: str) -> str:
        return content

    async def close(self) -> None:
        if self._http is not None:
            await self._http.aclose()

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.close()
