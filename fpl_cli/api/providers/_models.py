"""Shared models and exceptions for LLM providers."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


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


# The stop reasons that mean the model finished on its own terms. Anthropic
# reports `stop_reason`, OpenAI-compatible APIs report `finish_reason`; the two
# vocabularies do not collide, so one set covers both dialects. Anything else --
# "max_tokens", "length", "refusal", "content_filter" -- means the text stopped
# for a reason the caller should know about before it writes the answer to a
# durable artefact.
NORMAL_STOP_REASONS: frozenset[str] = frozenset({
    "end_turn", "stop_sequence", "tool_use", "stop", "tool_calls", "function_call",
})


@dataclass(frozen=True, slots=True)
class LLMResponse:
    """One provider answer, normalised across dialects.

    `stop_reason` carries the provider's own verdict on why generation ended,
    verbatim in whichever vocabulary it used. Without it a truncated response
    and a complete one are indistinguishable downstream, which is how a saved
    gameweek review shipped with a verdict missing and exit 0 (#266).
    """

    content: str
    model: str
    usage: TokenUsage
    citations: list[str] = field(default_factory=list)
    stop_reason: str | None = None

    @property
    def stopped_early(self) -> bool:
        """True when the provider named a stop reason that is not a normal completion.

        `None` means the provider did not say, which is not evidence of
        truncation -- only an explicit abnormal reason counts, so a provider
        (or a test stub) that omits the field never raises a false alarm.
        """
        return self.stop_reason is not None and self.stop_reason not in NORMAL_STOP_REASONS


def log_abnormal_stop(response: LLMResponse, label: str) -> None:
    """Announce a response the provider did not finish normally.

    At WARNING because the CLI configures no logging and WARNING is what
    reaches stderr regardless -- the same reason `_http` announces its 429
    retries there. Every provider calls this, so a truncation is visible on any
    command that talks to an LLM, whatever that command does with the text.
    """
    if not response.stopped_early:
        return
    logger.warning(
        "%s stopped early (stop_reason=%r) after %d output token(s) -- "
        "the response may be cut off",
        label, response.stop_reason, response.usage.output_tokens,
    )
