"""Shared models and exceptions for LLM providers."""

from __future__ import annotations

from dataclasses import dataclass, field


class ProviderError(Exception):
    """Sanitised error from an LLM provider (no auth headers)."""


class UnknownProviderError(ProviderError):
    """Raised when a configured provider name is not in the registry."""


class ProviderNotConfiguredError(ProviderError):
    """Raised when the required API key for a provider is missing."""


class RateLimitError(ProviderError):
    """The provider rate-limited the request (HTTP 429) and retrying did not clear it.

    Its own type because it is the one provider failure a caller can expect
    to clear by waiting, so a caller that batches queries can tell "try that
    subset again in a minute" from "the provider had no answer". `retry_after`
    is the server's own hint in seconds, when it sent one.
    """

    def __init__(self, message: str, *, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


@dataclass(frozen=True, slots=True)
class TokenUsage:
    input_tokens: int
    output_tokens: int


@dataclass(frozen=True, slots=True)
class LLMResponse:
    content: str
    model: str
    usage: TokenUsage
    citations: list[str] = field(default_factory=list)
