"""Shared models and exceptions for LLM providers."""

from __future__ import annotations

import logging
from collections.abc import Sequence
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

    def __add__(self, other: TokenUsage) -> TokenUsage:
        return TokenUsage(self.input_tokens + other.input_tokens, self.output_tokens + other.output_tokens)


# The stop reasons that mean the model finished on its own terms. Anthropic
# reports `stop_reason`, OpenAI-compatible APIs report `finish_reason`; the two
# vocabularies do not collide, so one set covers both dialects. Anything else --
# "max_tokens", "length", "refusal", "content_filter" -- means the text stopped
# for a reason the caller should know about before it writes the answer to a
# durable artefact.
NORMAL_STOP_REASONS: frozenset[str] = frozenset({
    "end_turn", "stop_sequence", "tool_use", "stop", "tool_calls", "function_call",
})

# The abnormal stops that mean the text hit its output-token ceiling, in
# Anthropic's and the OpenAI-compatible dialects: the ones a second roll can
# plausibly fix. The ceiling is shared with the model's reasoning (#316), whose
# length varies run to run, so a retry at the same ceiling is a fresh draw.
TRUNCATION_STOP_REASONS: frozenset[str] = frozenset({"max_tokens", "length"})

# The abnormal stops that mean a policy check declined the answer, in the same
# two dialects: the ones a same-model retry will not fix. Anthropic's docs say
# re-sending a refused request to the same model usually draws another
# refusal; the remedies they give are rephrasing the request or retrying on a
# different model. Neither is attempted here -- a fallback model is out of
# scope, and the prompt is ours, not the user's -- so the same-model retry is
# simply not made: it would spend a call, and up to a timeout, for the same
# answer (#397). Neither stop was "cut off".
REFUSAL_STOP_REASONS: frozenset[str] = frozenset({"refusal", "content_filter"})


@dataclass(frozen=True, slots=True)
class LLMResponse:
    """One provider answer, normalised across dialects.

    `stop_reason` carries the provider's own verdict on why generation ended,
    verbatim in whichever vocabulary it used. Without it a truncated response
    and a complete one are indistinguishable downstream, which is how a saved
    gameweek review shipped with a verdict missing and exit 0 (#266).

    `stop_category` is the refusal's category when the provider names one
    (Anthropic's `stop_details.category`: `cyber`, `general_harms` and so
    on), so a refusal can say which check declined it rather than only that
    one did (#450). None for every other stop, and for a refusal that came
    without one.
    """

    content: str
    model: str
    usage: TokenUsage
    citations: list[str] = field(default_factory=list)
    stop_reason: str | None = None
    stop_category: str | None = None

    @property
    def stopped_early(self) -> bool:
        """True when the provider named a stop reason that is not a normal completion.

        A missing reason is not evidence of truncation -- only an explicit
        abnormal one counts, so a provider (or a test stub) that omits the
        field never raises a false alarm. An *empty* reason is treated the
        same way rather than as an unrecognised one: a non-conformant
        OpenAI-compatible endpoint that sends `"finish_reason": ""` on every
        response would otherwise have every answer read as truncated, which
        costs a retry and a warning each time and says nothing true.
        """
        return bool(self.stop_reason) and self.stop_reason not in NORMAL_STOP_REASONS

    @property
    def truncated(self) -> bool:
        """True when the provider stopped at its output-token ceiling.

        The subset of `stopped_early` worth a retry on its own: the text was
        cut off, not refused, so a second draw can come back whole.
        """
        return self.stop_reason in TRUNCATION_STOP_REASONS

    @property
    def refused(self) -> bool:
        """True when a policy check declined the answer (a refusal or content filter).

        The subset of `stopped_early` a retry should never chase: the same
        request to the same model usually comes back refused again.
        """
        return self.stop_reason in REFUSAL_STOP_REASONS


def log_abnormal_stop(response: LLMResponse, label: str) -> None:
    """Announce a response the provider did not finish normally.

    At WARNING because the CLI configures no logging and WARNING is what
    reaches stderr regardless -- the same reason `_http` announces its 429
    retries there. Every provider calls this, so a truncation is visible on any
    command that talks to an LLM, whatever that command does with the text.
    """
    if not response.stopped_early:
        return
    # A refusal was declined, not cut off: saying "may be cut off" beside the
    # caller's own "refused" wording would make the two lines disagree (#411).
    consequence = "the provider declined to answer" if response.refused else "the response may be cut off"
    category = f", category={response.stop_category!r}" if response.stop_category else ""
    logger.warning(
        "%s stopped early (stop_reason=%r%s) after %d output token(s) -- %s",
        label, response.stop_reason, category, response.usage.output_tokens, consequence,
    )


def log_textless_response(
    response: LLMResponse, label: str, non_text_blocks: Sequence[str],
) -> None:
    """Announce a response whose content was blocks, none of them text.

    A model that reasons before it answers emits its thinking first, so a
    ceiling reached mid-thought returns content with no prose block in it at
    all. The concatenated text is then `""` -- honest, but indistinguishable
    downstream from a model that had nothing to say. Naming the blocks that
    did arrive is what separates the two for anyone reading stderr (#306).

    `non_text_blocks` is the types of the blocks that were *not* text, so an
    answer of nothing but an empty text block stays silent: that is a model
    with nothing to say, which is the case this has nothing to add to.
    `log_abnormal_stop` has already spoken if it was cut off.
    """
    if response.content or not non_text_blocks:
        return
    logger.warning(
        "%s returned no text content -- the response carried only %s block(s)",
        label, ", ".join(sorted(set(non_text_blocks))),
    )
