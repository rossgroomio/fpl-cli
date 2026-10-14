"""One synthesis call, retried once when the answer comes back damaged.

`review --summarise` and `league-recap --summarise` both write an LLM answer
into a saved report, and both judge it before they do. Each used to run its
own query-judge-retry loop, and every fix went into only one of them (#397).
The parts that should not differ between them live here: the attempt cap,
which stop reasons earn a retry on their own and which rule one out, telling
the retry what went wrong, and keeping the attempt in hand when the retry
itself fails. What
"damaged" means, and which of two damaged answers is the lesser evil, stays
with each command.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Generic, Protocol, TypeVar

from rich.markup import escape as rich_escape

from fpl_cli.cli._context import error_console

if TYPE_CHECKING:
    from fpl_cli.api.providers import LLMResponse

V = TypeVar("V")
V_contra = TypeVar("V_contra", contravariant=True)

# One retry: a second call is cheap next to a saved report that quietly lost a
# verdict or states a wrong claim as fact, and two attempts is where it stops
# -- a model that gets it wrong twice is telling us something a third roll
# will not fix.
MAX_SYNTHESIS_ATTEMPTS = 2


class RetryPrompt(Protocol[V_contra]):
    """Builds the retry's prompt: the first prompt, then what was wrong.

    `stop_reason` is set only when the last draft hit its token ceiling, so a
    builder never tells a refused draft it was cut off.
    """

    def __call__(self, user_prompt: str, verdict: V_contra, /, *, stop_reason: str | None) -> str: ...


@dataclass(frozen=True, slots=True)
class SynthesisAttempt(Generic[V]):
    """One answer: the text as the caller will use it, what its judge found,
    and the response it came from, for the provider's stop reason."""

    text: str
    verdict: V
    response: LLMResponse


async def synthesise_with_retry(
    provider: Any,
    *,
    prompt: str,
    system_prompt: str,
    subject: str,
    judge: Callable[[str, LLMResponse], V],
    retry_reason: Callable[[V], str | None],
    damage: Callable[[SynthesisAttempt[V]], tuple[int, ...]],
    retry_prompt: RetryPrompt[V],
    normalise: Callable[[str], str] | None = None,
) -> tuple[SynthesisAttempt[V], int]:
    """Query `provider`, retrying once if the answer comes back damaged.

    Returns the attempt that ships and the number of calls made. The text is
    the provider's own `post_process` of the content, then `normalise` when
    given, so `judge` reads the string the report will carry.

    An attempt earns the retry when `retry_reason` names something its judge
    found, or when the provider cut it off at its token ceiling -- which is
    retried even when the judge found nothing, since the text may have stopped
    before whatever the judge reads. A refusal or content filter is never
    retried, whatever the judge found: re-sending a refused request to the same
    model usually draws another refusal, so the retry would spend a call, and
    up to a timeout, to change nothing. Any other abnormal stop neither earns
    the retry nor rules it out. The retry is sent `retry_prompt(prompt,
    verdict, stop_reason=...)`, `stop_reason` set only for a token-ceiling
    stop, so it is told what was wrong instead of rolling the same gamble again.

    The retry only displaces the first attempt when `damage` ranks it strictly
    lower, so a worse second roll never ships over a better first one, and a
    retry that raises leaves the first attempt standing rather than losing an
    answer already in hand. The first call's failure is the caller's.

    `subject` opens the notices on stderr ("The editorial", "The personal
    analysis"); `retry_reason`'s answer completes "<subject> ... -- retrying
    once".
    """
    best: SynthesisAttempt[V] | None = None
    attempt_prompt = prompt
    for attempt in range(1, MAX_SYNTHESIS_ATTEMPTS + 1):
        try:
            response = await provider.query(prompt=attempt_prompt, system_prompt=system_prompt)
        except Exception as e:  # noqa: BLE001 — a failed retry keeps the attempt in hand
            if best is None:
                raise
            error_console.print(
                f"[yellow]  ⚠ {subject} retry failed ({rich_escape(str(e) or type(e).__name__)})"
                " -- keeping the first attempt[/yellow]"
            )
            return best, attempt
        # Post-processed before it is judged: the synthesis role is not
        # restricted to one provider, so a Perplexity-backed run arrives with
        # citation markers and a trailing source list. Judging the raw text
        # would judge a string the report never carries.
        text = provider.post_process(response.content)
        if normalise is not None:
            text = normalise(text)
        candidate = SynthesisAttempt(text, judge(text, response), response)
        if best is None or damage(candidate) < damage(best):
            best = candidate
        stop_reason = best.response.stop_reason if best.response.truncated else None
        reasons = [f"was cut off (stop_reason: {stop_reason})"] if stop_reason else []
        if found := retry_reason(best.verdict):
            reasons.append(found)
        if not reasons or attempt == MAX_SYNTHESIS_ATTEMPTS:
            return best, attempt
        # Escaped, not interpolated raw: a reason can quote text the provider
        # or the prompt supplied, and an unescaped `[/yellow]` in it would
        # raise MarkupError and cost the reader the notice.
        if best.response.refused:
            # Said here, not left to the caller: without it a damaged answer
            # that was not retried reads as a retry the loop forgot to make.
            error_console.print(
                f"[yellow]  ⚠ {subject} {rich_escape(' and '.join(reasons))}, but the provider refused it"
                f" (stop_reason: {rich_escape(str(best.response.stop_reason))}) -- not retrying,"
                " since a refused request is usually refused again[/yellow]"
            )
            return best, attempt
        error_console.print(
            f"[yellow]  ⚠ {subject} {rich_escape(' and '.join(reasons))} -- retrying once[/yellow]"
        )
        attempt_prompt = retry_prompt(prompt, best.verdict, stop_reason=stop_reason)
    raise AssertionError("unreachable: the loop returns on its last attempt")  # pragma: no cover
