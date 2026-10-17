"""The attempt loop `review` and `league-recap` share (#397).

Each command's own judge and ranking are tested beside the command; these pin
the parts that must not differ between them: the single retry, which stop
reasons earn it on their own and which rule it out, what the retry is told,
and keeping the attempt in hand when the retry fails.
"""

import pytest

from fpl_cli.api.providers import LLMResponse, ProviderError, TokenUsage
from fpl_cli.cli._synthesis_retry import synthesise_with_retry


def _reply(content: str, stop_reason: str | None = "end_turn") -> LLMResponse:
    return LLMResponse(content=content, model="m", usage=TokenUsage(1, 1), stop_reason=stop_reason)


class _Provider:
    """Answers each call with the next queued reply, raising a queued exception."""

    def __init__(self, *replies):
        self._replies = list(replies)
        self.prompts: list[str] = []
        self.system_prompts: list[str] = []

    async def query(self, prompt, system_prompt=None, **kwargs):
        self.prompts.append(prompt)
        self.system_prompts.append(system_prompt)
        reply = self._replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply

    def post_process(self, content):
        return content.removesuffix(" [1]")


def _wrong_words(text: str, response: LLMResponse) -> list[str]:
    """A stand-in judge: every "wrong" in the text is a fault."""
    return [word for word in text.split() if word.strip(".") == "wrong"]


def _retry_prompt(user_prompt: str, verdict: list[str], *, stop_reason: str | None = None) -> str:
    return f"{user_prompt} | {len(verdict)} wrong | cut off: {stop_reason}"


async def _run(provider: _Provider, **overrides):
    kwargs = {
        "prompt": "write it",
        "system_prompt": "be brief",
        "subject": "The thing",
        "judge": _wrong_words,
        "retry_reason": lambda verdict: "says wrong things" if verdict else None,
        "damage": lambda attempt: (int(attempt.response.stopped_early), len(attempt.verdict)),
        "retry_prompt": _retry_prompt,
        **overrides,
    }
    return await synthesise_with_retry(provider, **kwargs)


class TestSynthesiseWithRetry:

    # #450: which answer shipped, and what every judged answer cost.

    @staticmethod
    def _costing(content: str, input_tokens: int, output_tokens: int) -> LLMResponse:
        return LLMResponse(
            content=content, model="m", usage=TokenUsage(input_tokens, output_tokens), stop_reason="end_turn",
        )

    async def test_one_answer_reports_its_own_usage(self):
        best, attempts, usage = await _run(_Provider(self._costing("All good.", 100, 40)))
        assert (best.number, attempts, usage) == (1, 1, TokenUsage(100, 40))

    async def test_a_better_retry_ships_and_both_answers_are_costed(self):
        provider = _Provider(self._costing("This is wrong.", 100, 40), self._costing("All good.", 130, 50))
        best, attempts, usage = await _run(provider)
        assert (best.text, best.number, attempts) == ("All good.", 2, 2)
        assert usage == TokenUsage(230, 90)

    async def test_a_worse_retry_keeps_the_first_but_is_still_costed(self):
        # Two answers judged, the first shipped: the count alone cannot say
        # which, so the attempt carries its own number.
        provider = _Provider(self._costing("wrong.", 100, 40), self._costing("wrong wrong.", 130, 50))
        best, attempts, usage = await _run(provider)
        assert (best.text, best.number, attempts) == ("wrong.", 1, 2)
        assert usage == TokenUsage(230, 90)

    async def test_a_retry_that_fails_costs_only_the_first(self):
        provider = _Provider(self._costing("This is wrong.", 100, 40), ProviderError("timed out"))
        best, attempts, usage = await _run(provider)
        assert (best.number, attempts, usage) == (1, 1, TokenUsage(100, 40))

    async def test_a_clean_answer_is_one_call(self):
        provider = _Provider(_reply("All good."))
        best, attempts, _ = await _run(provider)
        assert (best.text, best.verdict, attempts) == ("All good.", [], 1)

    async def test_a_fault_is_retried_once_and_the_retry_is_told_it(self, capsys):
        provider = _Provider(_reply("This is wrong."), _reply("All good."))
        best, attempts, _ = await _run(provider)
        assert (best.text, attempts) == ("All good.", 2)
        assert provider.prompts == ["write it", "write it | 1 wrong | cut off: None"]
        assert provider.system_prompts == ["be brief", "be brief"]
        assert "The thing says wrong things -- retrying once" in capsys.readouterr().err

    async def test_it_stops_after_one_retry_and_keeps_the_less_damaged(self):
        provider = _Provider(_reply("wrong."), _reply("wrong wrong."), _reply("All good."))
        best, attempts, _ = await _run(provider)
        assert attempts == 2
        assert len(provider.prompts) == 2
        assert best.text == "wrong."

    async def test_each_attempt_keeps_the_prompt_that_drew_it(self):
        # A debug bundle saves the shipped prompt beside the shipped text, so
        # the pair replays once the retry's answer is the one that ships.
        provider = _Provider(_reply("This is wrong."), _reply("All good."))
        best, *_ = await _run(provider)
        assert best.prompt == "write it | 1 wrong | cut off: None"
        provider = _Provider(_reply("wrong."), _reply("wrong wrong."))
        best, *_ = await _run(provider)
        assert best.prompt == "write it"

    async def test_a_tie_keeps_the_first_attempt(self):
        provider = _Provider(_reply("First wrong."), _reply("Second wrong."))
        best, *_ = await _run(provider)
        assert best.text == "First wrong."

    @pytest.mark.parametrize("stop_reason", ["max_tokens", "length"])
    async def test_a_token_ceiling_alone_earns_the_retry(self, stop_reason, capsys):
        # Retried even when the judge found nothing: the text may have stopped
        # before whatever the judge reads.
        provider = _Provider(_reply("All good so", stop_reason), _reply("All good."))
        best, attempts, _ = await _run(provider)
        assert (best.text, attempts) == ("All good.", 2)
        assert provider.prompts[1] == f"write it | 0 wrong | cut off: {stop_reason}"
        assert f"was cut off (stop_reason: {stop_reason}) -- retrying once" in capsys.readouterr().err

    @pytest.mark.parametrize("stop_reason", ["refusal", "content_filter", "something_new"])
    async def test_any_other_abnormal_stop_alone_does_not(self, stop_reason):
        provider = _Provider(_reply("I can't help.", stop_reason))
        best, attempts, _ = await _run(provider)
        assert attempts == 1
        assert best.response.stop_reason == stop_reason

    @pytest.mark.parametrize("stop_reason", ["refusal", "content_filter"])
    async def test_a_refusal_rules_out_the_retry_whatever_the_judge_found(self, stop_reason, capsys):
        # Re-sending a refused request to the same model usually draws another
        # refusal, so the retry would spend a call to change nothing.
        provider = _Provider(_reply("This is wrong.", stop_reason), _reply("All good."))
        best, attempts, _ = await _run(provider)
        assert (best.text, attempts) == ("This is wrong.", 1)
        assert len(provider.prompts) == 1
        # Rich hard-wraps to the terminal width, so collapse it before matching.
        err = " ".join(capsys.readouterr().err.split())
        assert f"The thing says wrong things, but the provider refused it (stop_reason: {stop_reason})" in err
        assert "not retrying" in err

    async def test_an_unrecognised_stop_neither_earns_nor_rules_out_the_retry(self, capsys):
        provider = _Provider(_reply("wrong.", "something_new"), _reply("All good."))
        best, attempts, _ = await _run(provider)
        assert (best.text, attempts) == ("All good.", 2)
        assert provider.prompts[1] == "write it | 1 wrong | cut off: None"
        assert "cut off" not in capsys.readouterr().err

    async def test_a_cut_off_faulty_answer_is_told_both(self, capsys):
        provider = _Provider(_reply("wrong and", "max_tokens"), _reply("All good."))
        await _run(provider)
        assert provider.prompts[1] == "write it | 1 wrong | cut off: max_tokens"
        err = capsys.readouterr().err.replace("\n", " ")
        assert "was cut off (stop_reason: max_tokens) and says wrong things" in err

    async def test_a_retry_that_fails_keeps_the_attempt_in_hand(self, capsys):
        provider = _Provider(_reply("This is wrong."), ProviderError("timed out"))
        best, attempts, _ = await _run(provider)
        # One answer judged: a retry that never came back is not a draft.
        assert (best.text, best.verdict, attempts) == ("This is wrong.", ["wrong."], 1)
        err = " ".join(capsys.readouterr().err.split())
        assert "The thing retry failed (ProviderError: timed out) -- keeping the first attempt" in err

    async def test_a_retry_failing_with_no_message_is_named_by_its_type(self, capsys):
        provider = _Provider(_reply("wrong."), TimeoutError())
        await _run(provider)
        assert "retry failed (TimeoutError)" in capsys.readouterr().err

    @pytest.mark.parametrize("stage", ["retry_prompt", "normalise", "judge", "damage"])
    async def test_our_own_code_failing_on_the_retry_keeps_the_attempt_in_hand(self, stage, capsys):
        # #399 review: guarding only the call left the rest of the retry able
        # to throw away the answer in hand -- a check tripping over text the
        # first answer never contained, say. Only the retry's answer, "Boom.",
        # trips the text stages; the other two only ever run for the retry.
        def boom(*args, **kwargs):
            raise AttributeError(f"{stage} tripped")

        overrides = {
            "retry_prompt": {"retry_prompt": boom},
            "damage": {"damage": boom},
            "normalise": {"normalise": lambda text: boom() if text == "Boom." else text},
            "judge": {
                "judge": lambda text, response: boom() if text == "Boom." else _wrong_words(text, response),
            },
        }[stage]
        provider = _Provider(_reply("This is wrong."), _reply("Boom."))
        best, attempts, _ = await _run(provider, **overrides)
        assert (best.text, attempts) == ("This is wrong.", 1)
        err = " ".join(capsys.readouterr().err.split())
        assert f"retry failed (AttributeError: {stage} tripped) -- keeping the first attempt" in err

    async def test_the_first_call_s_failure_is_the_caller_s(self):
        with pytest.raises(ProviderError, match="down"):
            await _run(_Provider(ProviderError("down")))

    async def test_the_judge_reads_the_text_as_it_will_be_used(self):
        # post_process first (a provider stripping its own citation markers),
        # then the caller's normalise; the judge and the result both see that.
        seen: list[str] = []

        def judge(text, response):
            seen.append(text)
            return []

        provider = _Provider(_reply("all good [1]"))
        best, *_ = await _run(provider, judge=judge, normalise=str.capitalize)
        assert seen == ["All good"]
        assert best.text == "All good"

    async def test_a_reason_carrying_rich_markup_does_not_break_the_notice(self, capsys):
        # An unescaped "[/yellow]" would raise MarkupError and cost the reader
        # the notice this exists to print.
        provider = _Provider(_reply("wrong."), _reply("All good."))
        await _run(provider, retry_reason=lambda verdict: "quotes [/yellow] back" if verdict else None)
        assert "quotes [/yellow] back -- retrying once" in capsys.readouterr().err.replace("\n", "")
