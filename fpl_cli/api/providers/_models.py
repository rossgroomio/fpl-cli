"""Shared models and exceptions for LLM providers."""

from __future__ import annotations

from dataclasses import dataclass, field


class ProviderError(Exception):
    """Sanitised error from an LLM provider (no auth headers)."""


class UnknownProviderError(ProviderError):
    """Raised when a configured provider name is not in the registry."""


class ProviderNotConfiguredError(ProviderError):
    """Raised when the required API key for a provider is missing."""


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
