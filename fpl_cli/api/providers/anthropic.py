"""Anthropic provider for the Messages API."""

from __future__ import annotations

import os
from typing import Any, ClassVar, Self

import httpx

from fpl_cli.api.providers._http import RetryPolicy, post_json_with_retry
from fpl_cli.api.providers._models import (
    LLMResponse,
    ProviderError,
    TokenUsage,
    log_abnormal_stop,
    log_textless_response,
)

_BASE_URL = "https://api.anthropic.com/v1"
_PROVIDER_LABEL = "Anthropic"


class AnthropicProvider:
    """LLM provider for the Anthropic Messages API."""

    DEFAULT_MODEL: ClassVar[str] = "claude-sonnet-5-5"
    DEFAULT_TIMEOUT: ClassVar[float] = 60.0
    API_KEY_ENV_VAR: ClassVar[str] = "ANTHROPIC_API_KEY"
    KEY_SETUP_URL: ClassVar[str] = "https://console.anthropic.com/"
    # A 429 is retried with backoff before it becomes a ProviderError; every
    # other status is terminal. Shared with OpenAICompatProvider via `_http`.
    RETRY_POLICY: ClassVar[RetryPolicy] = RetryPolicy()
    # Used when a role's config carries no `max_tokens`. Thinking and output
    # share this ceiling (#316), so a low fallback is spent before any text
    # arrives: 1024 did exactly that to `FPL_RESEARCH_PROVIDER=anthropic`,
    # whose shipped config has no `max_tokens` of its own. Matches the
    # synthesis role's shipped value in `defaults.yaml`.
    DEFAULT_MAX_TOKENS: ClassVar[int] = 8000

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
        max_tokens = int(merged.get("max_tokens", self.DEFAULT_MAX_TOKENS))

        messages = [{"role": "user", "content": prompt}]
        payload: dict[str, Any] = {
            "model": self.model,
            "max_tokens": max_tokens,
            "messages": messages,
        }
        if system_prompt:
            payload["system"] = system_prompt
        # Effort lives here, so pinning it is a `settings.yaml` edit rather
        # than whatever the model's own default happens to be (#450). A key
        # set to null is dropped, which is how a user unpins one for a model
        # that rejects it; an empty block sends nothing.
        output_config = merged.get("output_config")
        if isinstance(output_config, dict):
            output_config = {k: v for k, v in output_config.items() if v is not None}
            if output_config:
                payload["output_config"] = output_config

        headers = {
            "x-api-key": self.api_key,
            "anthropic-version": "2023-06-01",
            "Content-Type": "application/json",
        }

        data = await post_json_with_retry(
            self._ensure_http(), "/messages", payload=payload, headers=headers,
            label=_PROVIDER_LABEL, policy=self.RETRY_POLICY,
        )

        # Only text blocks carry prose, but the types that were *not* text are
        # worth keeping: a ceiling reached while the model was still thinking
        # returns a lone `thinking` block, and the empty string that falls out
        # of this loop is otherwise indistinguishable from a model with
        # nothing to say (#306). Text blocks are deliberately not collected --
        # a response of nothing but an empty text block *is* a model with
        # nothing to say, and naming "text" as the reason there was no text
        # would contradict itself.
        content = ""
        non_text_blocks: list[str] = []
        for block in data.get("content", []):
            if block.get("type") == "text":
                content += block.get("text", "")
            else:
                non_text_blocks.append(str(block.get("type")))

        # Normalise usage: Anthropic returns input_tokens/output_tokens directly
        raw_usage = data.get("usage", {})
        usage = TokenUsage(
            input_tokens=raw_usage.get("input_tokens", 0),
            output_tokens=raw_usage.get("output_tokens", 0),
        )

        response = LLMResponse(
            content=content,
            model=data.get("model", self.model),
            usage=usage,
            # Present on every Messages API response: "end_turn" when the model
            # finished, "max_tokens" when the ceiling cut it off, "refusal" and
            # friends otherwise. Carried rather than acted on here -- the
            # caller decides what an early stop means for its artefact. Blank
            # normalises to None, so "not told" has exactly one representation.
            stop_reason=data.get("stop_reason") or None,
            stop_category=_stop_category(data),
        )
        log_abnormal_stop(response, _PROVIDER_LABEL)
        log_textless_response(response, _PROVIDER_LABEL, non_text_blocks)
        return response

    def post_process(self, content: str) -> str:
        return content

    async def close(self) -> None:
        if self._http is not None:
            await self._http.aclose()

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.close()


def _stop_category(data: dict[str, Any]) -> str | None:
    """The refusal's category from `stop_details`, when the API gave one.

    Only a refusal carries `stop_details`; every other stop sends null or
    omits it, and the category itself may be null. Read defensively, since
    an absent or malformed block must cost the detail, never the response.
    """
    details = data.get("stop_details")
    if not isinstance(details, dict):
        return None
    category = details.get("category")
    return str(category) if category else None
