"""Generic OpenAI-compatible provider (works with OpenAI, Groq, Together, Ollama, etc.)."""

from __future__ import annotations

import os
from typing import Any, ClassVar, Self

import httpx

from fpl_cli.api.providers._http import RetryPolicy, post_json_with_retry
from fpl_cli.api.providers._models import LLMResponse, ProviderError, TokenUsage

_DEFAULT_BASE_URL = "https://api.openai.com/v1"


class OpenAICompatProvider:
    """LLM provider for any OpenAI-compatible chat completions API.

    Subclasses (e.g. PerplexityProvider) override _build_payload and
    _parse_response to handle provider-specific request/response fields.
    """

    DEFAULT_MODEL: ClassVar[str] = "gpt-4.1-mini-2025-04-14"
    DEFAULT_TIMEOUT: ClassVar[float] = 60.0
    API_KEY_ENV_VAR: ClassVar[str] = "OPENAI_API_KEY"
    KEY_SETUP_URL: ClassVar[str] = "https://platform.openai.com/api-keys"
    _PROVIDER_LABEL: ClassVar[str] = "OpenAI-compatible API"
    # A 429 is retried with backoff before it becomes a ProviderError; every
    # other status is terminal. Shared with AnthropicProvider via `_http`.
    RETRY_POLICY: ClassVar[RetryPolicy] = RetryPolicy()

    def __init__(
        self,
        model: str = DEFAULT_MODEL,
        timeout: float = DEFAULT_TIMEOUT,
        query_defaults: dict[str, Any] | None = None,
        base_url: str = _DEFAULT_BASE_URL,
    ) -> None:
        self.model = model
        self.timeout = timeout
        self.query_defaults = query_defaults or {}
        self.base_url = base_url
        self.api_key = os.environ.get(self.API_KEY_ENV_VAR)
        self._http: httpx.AsyncClient | None = None

    @property
    def is_configured(self) -> bool:
        return bool(self.api_key)

    def _ensure_http(self) -> httpx.AsyncClient:
        if self._http is None:
            self._http = httpx.AsyncClient(base_url=self.base_url, timeout=self.timeout)
        return self._http

    def _build_payload(
        self, messages: list[dict[str, str]], merged: dict[str, Any],
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {"model": self.model, "messages": messages}
        max_tokens = merged.get("max_tokens")
        if max_tokens is not None:
            payload["max_tokens"] = int(max_tokens)
        return payload

    def _parse_response(self, data: dict[str, Any]) -> LLMResponse:
        content = ""
        if data.get("choices"):
            content = data["choices"][0].get("message", {}).get("content", "")
        raw_usage = data.get("usage", {})
        usage = TokenUsage(
            input_tokens=raw_usage.get("prompt_tokens", 0),
            output_tokens=raw_usage.get("completion_tokens", 0),
        )
        return LLMResponse(
            content=content,
            model=data.get("model", self.model),
            usage=usage,
        )

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

        messages: list[dict[str, str]] = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.append({"role": "user", "content": prompt})

        payload = self._build_payload(messages, merged)

        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

        data = await post_json_with_retry(
            self._ensure_http(), "/chat/completions", payload=payload, headers=headers,
            label=self._PROVIDER_LABEL, policy=self.RETRY_POLICY,
        )
        return self._parse_response(data)

    def post_process(self, content: str) -> str:
        return content

    async def close(self) -> None:
        if self._http is not None:
            await self._http.aclose()

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.close()
