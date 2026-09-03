"""Tests for LLM provider abstraction (fpl_cli/api/providers/)."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from unittest.mock import AsyncMock, MagicMock, PropertyMock

import httpx
import pytest


def _make_httpx_response(data: dict, status_code: int = 200):
    """Create a mock httpx.Response with sync json() and raise_for_status()."""
    resp = MagicMock()
    resp.json.return_value = data
    resp.status_code = status_code
    resp.headers = {}
    resp.raise_for_status.return_value = None
    return resp


def _http_error_response(status_code: int, detail: str = "", retry_after: str | None = None):
    """A mock error response whose raise_for_status() fails like httpx's would."""
    resp = MagicMock()
    resp.status_code = status_code
    resp.headers = {"Retry-After": retry_after} if retry_after is not None else {}
    resp.json.return_value = {"error": {"message": detail}}
    resp.raise_for_status.side_effect = httpx.HTTPStatusError(
        f"HTTP {status_code}", request=MagicMock(), response=resp,
    )
    return resp


def _rate_limited(retry_after: str | None = None):
    return _http_error_response(429, "Request rate limit exceeded", retry_after)


from fpl_cli.api.providers import (  # noqa: E402 — placed after module-level helper definition; no circular dependency
    AnthropicProvider,
    LLMResponse,
    OpenAICompatProvider,
    PerplexityProvider,
    ProviderError,
    ProviderNotConfiguredError,
    RateLimitError,
    TokenUsage,
    UnknownProviderError,
    get_llm_provider,
)
from fpl_cli.api.providers import _http as provider_http  # noqa: E402
from fpl_cli.api.providers._http import RetryPolicy, error_detail, retry_after_seconds  # noqa: E402


@pytest.fixture(autouse=True)
def backoff_waits(monkeypatch):
    """Record every backoff wait instead of sleeping it, so a retry costs no wall-clock."""
    waits: list[float] = []

    async def _record(seconds: float) -> None:
        waits.append(seconds)

    monkeypatch.setattr(provider_http, "_sleep", _record)
    return waits

# ---------------------------------------------------------------------------
# TokenUsage / LLMResponse
# ---------------------------------------------------------------------------


class TestTokenUsage:
    def test_frozen(self):
        u = TokenUsage(input_tokens=10, output_tokens=20)
        with pytest.raises(AttributeError):
            u.input_tokens = 99  # type: ignore[misc]

    def test_values(self):
        u = TokenUsage(input_tokens=10, output_tokens=20)
        assert u.input_tokens == 10
        assert u.output_tokens == 20


class TestLLMResponse:
    def test_defaults(self):
        r = LLMResponse(
            content="hi", model="m", usage=TokenUsage(0, 0)
        )
        assert r.citations == []

    def test_with_citations(self):
        r = LLMResponse(
            content="hi",
            model="m",
            usage=TokenUsage(0, 0),
            citations=["https://example.com"],
        )
        assert len(r.citations) == 1

    def test_frozen(self):
        r = LLMResponse(content="hi", model="m", usage=TokenUsage(0, 0))
        with pytest.raises(AttributeError):
            r.content = "bye"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Provider conformance (shared structure)
# ---------------------------------------------------------------------------

PROVIDER_CLASSES = [AnthropicProvider, PerplexityProvider, OpenAICompatProvider]


@pytest.mark.parametrize("cls", PROVIDER_CLASSES, ids=lambda c: c.__name__)
class TestProviderConformance:
    def test_has_class_vars(self, cls):
        assert isinstance(cls.DEFAULT_MODEL, str)
        assert isinstance(cls.DEFAULT_TIMEOUT, float)
        assert isinstance(cls.API_KEY_ENV_VAR, str)
        assert isinstance(cls.KEY_SETUP_URL, str)

    def test_is_configured_false_without_key(self, cls, monkeypatch):
        monkeypatch.delenv(cls.API_KEY_ENV_VAR, raising=False)
        provider = cls()
        assert provider.is_configured is False

    def test_is_configured_true_with_key(self, cls, monkeypatch):
        monkeypatch.setenv(cls.API_KEY_ENV_VAR, "test-key")
        provider = cls()
        assert provider.is_configured is True

    def test_post_process_returns_str(self, cls):
        provider = cls()
        assert isinstance(provider.post_process("hello"), str)

    def test_has_query_method(self, cls):
        assert callable(getattr(cls, "query", None))

    def test_has_close_method(self, cls):
        assert callable(getattr(cls, "close", None))

    def test_has_context_manager(self, cls):
        assert callable(getattr(cls, "__aenter__", None))
        assert callable(getattr(cls, "__aexit__", None))

    def test_custom_model_and_timeout(self, cls):
        provider = cls(model="custom-model", timeout=99.0)
        assert provider.model == "custom-model"
        assert provider.timeout == 99.0

    def test_query_defaults_stored(self, cls):
        provider = cls(query_defaults={"max_tokens": 512})
        assert provider.query_defaults == {"max_tokens": 512}

    def test_declares_a_retry_policy(self, cls):
        assert isinstance(cls.RETRY_POLICY, RetryPolicy)


# ---------------------------------------------------------------------------
# AnthropicProvider
# ---------------------------------------------------------------------------


class TestAnthropicProvider:
    @pytest.fixture
    def provider(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
        return AnthropicProvider()

    @pytest.fixture
    def mock_response(self):
        return {
            "content": [{"type": "text", "text": "Hello from Claude"}],
            "model": "claude-sonnet-5",
            "usage": {"input_tokens": 10, "output_tokens": 20},
        }

    async def test_query_success(self, provider, mock_response):
        resp = _make_httpx_response(mock_response)
        provider._http = AsyncMock()
        provider._http.post = AsyncMock(return_value=resp)

        result = await provider.query("test prompt", system_prompt="be helpful")
        assert isinstance(result, LLMResponse)
        assert result.content == "Hello from Claude"
        assert result.usage.input_tokens == 10
        assert result.usage.output_tokens == 20

    async def test_query_without_key_raises(self, monkeypatch):
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        provider = AnthropicProvider()
        with pytest.raises(ProviderError, match="ANTHROPIC_API_KEY"):
            await provider.query("test")

    async def test_http_error_sanitised(self, provider):
        provider._http = AsyncMock()
        provider._http.post = AsyncMock(return_value=_http_error_response(500, "boom"))

        with pytest.raises(ProviderError, match="HTTP 500: boom"):
            await provider.query("test")

    async def test_timeout_sanitised(self, provider):
        provider._http = AsyncMock()
        provider._http.post = AsyncMock(side_effect=httpx.TimeoutException("timeout"))

        with pytest.raises(ProviderError, match="timed out"):
            await provider.query("test")

    def test_post_process_is_identity(self, provider):
        assert provider.post_process("hello [1] world") == "hello [1] world"

    async def test_malformed_json_response_raises(self, provider):
        resp = MagicMock()
        resp.status_code = 200
        resp.raise_for_status.return_value = None
        resp.json.side_effect = json.JSONDecodeError("bad json", "", 0)
        provider._http = AsyncMock()
        provider._http.post = AsyncMock(return_value=resp)

        with pytest.raises(ProviderError, match="invalid JSON"):
            await provider.query("test")

    async def test_query_merges_defaults(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
        provider = AnthropicProvider(query_defaults={"max_tokens": 2048})

        resp = _make_httpx_response({
            "content": [{"type": "text", "text": "ok"}],
            "model": "claude-sonnet-5",
            "usage": {"input_tokens": 0, "output_tokens": 0},
        })
        provider._http = AsyncMock()
        provider._http.post = AsyncMock(return_value=resp)

        await provider.query("test")
        call_payload = provider._http.post.call_args[1]["json"]
        assert call_payload["max_tokens"] == 2048


# ---------------------------------------------------------------------------
# PerplexityProvider
# ---------------------------------------------------------------------------


class TestPerplexityProvider:
    @pytest.fixture
    def provider(self, monkeypatch):
        monkeypatch.setenv("PERPLEXITY_API_KEY", "test-key")
        return PerplexityProvider()

    @pytest.fixture
    def mock_response(self):
        return {
            "choices": [{"message": {"content": "Research result [1]"}}],
            "citations": ["https://example.com"],
            "model": "sonar-pro",
            "usage": {"prompt_tokens": 50, "completion_tokens": 100},
        }

    async def test_query_success(self, provider, mock_response):
        resp = _make_httpx_response(mock_response)
        provider._http = AsyncMock()
        provider._http.post = AsyncMock(return_value=resp)

        result = await provider.query("test prompt")
        assert isinstance(result, LLMResponse)
        assert result.content == "Research result [1]"
        assert result.citations == ["https://example.com"]
        assert result.usage.input_tokens == 50
        assert result.usage.output_tokens == 100

    async def test_query_sends_recency_filter(self, provider, mock_response):
        resp = _make_httpx_response(mock_response)
        provider._http = AsyncMock()
        provider._http.post = AsyncMock(return_value=resp)

        await provider.query("test", search_recency_filter="day")
        call_payload = provider._http.post.call_args[1]["json"]
        assert call_payload["web_search_options"]["search_recency_filter"] == "day"

    def test_post_process_cleans_citations(self, provider):
        text = "Result [1] is good [2].\n\nSources:\n1. https://x.com"
        cleaned = provider.post_process(text)
        assert "[1]" not in cleaned
        assert "Sources:" not in cleaned


# ---------------------------------------------------------------------------
# OpenAICompatProvider
# ---------------------------------------------------------------------------


class TestOpenAICompatProvider:
    @pytest.fixture
    def provider(self, monkeypatch):
        monkeypatch.setenv("OPENAI_API_KEY", "test-key")
        return OpenAICompatProvider()

    @pytest.fixture
    def mock_response(self):
        return {
            "choices": [{"message": {"content": "GPT response"}}],
            "model": "gpt-4o",
            "usage": {"prompt_tokens": 10, "completion_tokens": 30},
        }

    async def test_query_success(self, provider, mock_response):
        resp = _make_httpx_response(mock_response)
        provider._http = AsyncMock()
        provider._http.post = AsyncMock(return_value=resp)

        result = await provider.query("test")
        assert isinstance(result, LLMResponse)
        assert result.content == "GPT response"

    async def test_malformed_json_response_raises(self, provider):
        resp = MagicMock()
        resp.status_code = 200
        resp.raise_for_status.return_value = None
        resp.json.side_effect = json.JSONDecodeError("bad json", "", 0)
        provider._http = AsyncMock()
        provider._http.post = AsyncMock(return_value=resp)

        with pytest.raises(ProviderError, match="invalid JSON"):
            await provider.query("test")

    def test_custom_base_url(self, monkeypatch):
        monkeypatch.setenv("OPENAI_API_KEY", "test-key")
        provider = OpenAICompatProvider(base_url="http://localhost:11434/v1")
        assert provider.base_url == "http://localhost:11434/v1"

    def test_post_process_is_identity(self, provider):
        assert provider.post_process("hello [1]") == "hello [1]"


# ---------------------------------------------------------------------------
# Shared HTTP plumbing: error excerpts, Retry-After, backoff
# ---------------------------------------------------------------------------


class TestErrorDetail:
    def test_reads_the_error_message_from_the_body(self):
        resp = MagicMock()
        resp.json.return_value = {"error": {"message": "quota exhausted"}}
        assert error_detail(resp) == ": quota exhausted"

    def test_falls_back_to_response_text(self):
        resp = MagicMock()
        resp.json.side_effect = json.JSONDecodeError("bad", "", 0)
        type(resp).text = PropertyMock(return_value="plain error message")
        assert error_detail(resp) == ": plain error message"

    def test_returns_empty_on_total_failure(self):
        resp = MagicMock()
        resp.json.side_effect = json.JSONDecodeError("bad", "", 0)
        type(resp).text = PropertyMock(side_effect=RuntimeError("broken"))
        assert error_detail(resp) == ""


def _with_retry_after(value):
    resp = MagicMock()
    resp.headers = {"Retry-After": value} if value is not None else {}
    return resp


class TestRetryAfterHeader:
    def test_delta_seconds(self):
        assert retry_after_seconds(_with_retry_after("12")) == 12.0

    def test_fractional_and_padded_delta(self):
        assert retry_after_seconds(_with_retry_after(" 1.5 ")) == 1.5

    def test_http_date_in_the_future(self):
        when = datetime.now(timezone.utc) + timedelta(seconds=90)
        seconds = retry_after_seconds(_with_retry_after(format_datetime(when, usegmt=True)))
        assert seconds is not None
        assert 85.0 <= seconds <= 90.0

    def test_http_date_already_past_is_zero(self):
        when = datetime.now(timezone.utc) - timedelta(seconds=90)
        assert retry_after_seconds(_with_retry_after(format_datetime(when, usegmt=True))) == 0.0

    def test_negative_delta_is_zero(self):
        assert retry_after_seconds(_with_retry_after("-5")) == 0.0

    def test_missing_header_is_none(self):
        assert retry_after_seconds(_with_retry_after(None)) is None

    def test_blank_or_garbage_header_is_none(self):
        assert retry_after_seconds(_with_retry_after("  ")) is None
        assert retry_after_seconds(_with_retry_after("soon")) is None

    def test_a_non_string_header_value_is_none(self):
        resp = MagicMock()  # headers.get() returns a MagicMock, not a str
        assert retry_after_seconds(resp) is None


def _low(a: float, b: float) -> float:
    return a


def _high(a: float, b: float) -> float:
    return b


class TestRetryPolicy:
    def test_first_attempt_plus_three_retries_by_default(self):
        assert RetryPolicy().max_attempts == 4

    def test_backoff_doubles_between_a_floor_and_a_ceiling(self):
        policy = RetryPolicy(base_delay=2.0, max_delay=30.0)
        # Equal jitter: the ceiling doubles per retry, the wait lands in its upper half.
        assert policy.delay(0, None, rng=_low) == 1.0
        assert policy.delay(0, None, rng=_high) == 2.0
        assert policy.delay(1, None, rng=_low) == 2.0
        assert policy.delay(1, None, rng=_high) == 4.0
        assert policy.delay(2, None, rng=_low) == 4.0
        assert policy.delay(2, None, rng=_high) == 8.0

    def test_backoff_is_capped_at_max_delay(self):
        policy = RetryPolicy(base_delay=2.0, max_delay=5.0)
        assert policy.delay(2, None, rng=_high) == 5.0
        assert policy.delay(2, None, rng=_low) == 2.5
        assert policy.delay(10, None, rng=_high) == 5.0

    def test_retry_after_overrides_the_backoff_with_up_to_a_second_of_jitter(self):
        policy = RetryPolicy(max_delay=30.0)
        assert policy.delay(0, 12.0, rng=_low) == 12.0
        assert policy.delay(0, 12.0, rng=_high) == 13.0
        # ...even on a retry whose computed backoff would be shorter or longer.
        assert policy.delay(2, 0.0, rng=_low) == 0.0

    def test_retry_after_is_capped_at_max_delay_too(self):
        assert RetryPolicy(max_delay=30.0).delay(0, 600.0, rng=_low) == 30.0

    def test_live_jitter_stays_within_the_bounds(self):
        policy = RetryPolicy()
        for retry in range(4):
            ceiling = min(policy.max_delay, policy.base_delay * 2 ** retry)
            for _ in range(25):
                assert ceiling / 2 <= policy.delay(retry, None) <= ceiling


_OK_BODIES = {
    AnthropicProvider: {
        "content": [{"type": "text", "text": "ok"}],
        "model": "m",
        "usage": {"input_tokens": 1, "output_tokens": 1},
    },
    OpenAICompatProvider: {
        "choices": [{"message": {"content": "ok"}}],
        "model": "m",
        "usage": {"prompt_tokens": 1, "completion_tokens": 1},
    },
    PerplexityProvider: {
        "choices": [{"message": {"content": "ok"}}],
        "citations": [],
        "model": "m",
        "usage": {"prompt_tokens": 1, "completion_tokens": 1},
    },
}


@pytest.mark.parametrize("cls", PROVIDER_CLASSES, ids=lambda c: c.__name__)
class TestRateLimitBackoff:
    """A 429 is the one status every provider retries; nothing else is (#184)."""

    @pytest.fixture
    def provider(self, cls, monkeypatch):
        monkeypatch.setenv(cls.API_KEY_ENV_VAR, "test-key")
        instance = cls()
        instance._http = AsyncMock()
        return instance

    async def test_a_429_is_retried_and_the_next_answer_returned(self, provider, cls, backoff_waits):
        provider._http.post = AsyncMock(side_effect=[
            _rate_limited(), _make_httpx_response(_OK_BODIES[cls]),
        ])

        result = await provider.query("test")

        assert result.content == "ok"
        assert provider._http.post.await_count == 2
        assert len(backoff_waits) == 1
        assert backoff_waits[0] > 0

    async def test_the_same_request_is_resent_on_retry(self, provider, cls):
        provider._http.post = AsyncMock(side_effect=[
            _rate_limited(), _make_httpx_response(_OK_BODIES[cls]),
        ])

        await provider.query("test prompt", system_prompt="be brief")

        first, second = provider._http.post.await_args_list
        assert first.args == second.args
        assert first.kwargs["json"] == second.kwargs["json"]
        assert first.kwargs["headers"] == second.kwargs["headers"]

    async def test_retry_after_sets_the_wait(self, provider, cls, backoff_waits):
        provider._http.post = AsyncMock(side_effect=[
            _rate_limited(retry_after="7"), _make_httpx_response(_OK_BODIES[cls]),
        ])

        await provider.query("test")

        assert 7.0 <= backoff_waits[0] <= 8.0

    async def test_a_persistent_429_is_a_rate_limit_error_once_the_attempts_are_spent(
        self, provider, backoff_waits,
    ):
        provider._http.post = AsyncMock(return_value=_rate_limited(retry_after="30"))

        with pytest.raises(RateLimitError, match="HTTP 429: Request rate limit exceeded") as excinfo:
            await provider.query("test")

        attempts = RetryPolicy().max_attempts
        assert provider._http.post.await_count == attempts
        assert len(backoff_waits) == attempts - 1
        assert excinfo.value.retry_after == 30.0
        assert "after 4 attempt(s)" in str(excinfo.value)
        # Still a ProviderError, so every existing handler keeps degrading gracefully.
        assert isinstance(excinfo.value, ProviderError)

    async def test_without_retry_after_the_error_carries_none(self, provider):
        provider._http.post = AsyncMock(return_value=_rate_limited())

        with pytest.raises(RateLimitError) as excinfo:
            await provider.query("test")

        assert excinfo.value.retry_after is None

    async def test_other_http_errors_are_not_retried(self, provider, backoff_waits):
        provider._http.post = AsyncMock(return_value=_http_error_response(503, "overloaded"))

        with pytest.raises(ProviderError, match="HTTP 503: overloaded") as excinfo:
            await provider.query("test")

        assert not isinstance(excinfo.value, RateLimitError)
        assert provider._http.post.await_count == 1
        assert backoff_waits == []

    async def test_a_timeout_is_not_retried(self, provider, backoff_waits):
        provider._http.post = AsyncMock(side_effect=httpx.TimeoutException("timeout"))

        with pytest.raises(ProviderError, match="timed out"):
            await provider.query("test")

        assert provider._http.post.await_count == 1
        assert backoff_waits == []

    async def test_a_policy_with_a_single_attempt_never_retries(self, provider, cls, monkeypatch, backoff_waits):
        monkeypatch.setattr(cls, "RETRY_POLICY", RetryPolicy(max_attempts=1))
        provider._http.post = AsyncMock(return_value=_rate_limited())

        with pytest.raises(RateLimitError, match="after 1 attempt"):
            await provider.query("test")

        assert provider._http.post.await_count == 1
        assert backoff_waits == []


# ---------------------------------------------------------------------------
# get_llm_provider factory
# ---------------------------------------------------------------------------


class TestGetLlmProvider:
    @pytest.fixture
    def default_settings(self):
        return {
            "llm": {
                "research": {
                    "provider": "perplexity",
                    "model": "sonar-pro",
                    "timeout": 120,
                    "query_defaults": {"search_recency_filter": "week"},
                },
                "synthesis": {
                    "provider": "anthropic",
                    "model": "claude-sonnet-5",
                    "timeout": 60,
                    "query_defaults": {"max_tokens": 4096},
                },
            }
        }

    def test_default_resolution_research(self, default_settings, monkeypatch):
        monkeypatch.setenv("PERPLEXITY_API_KEY", "test-key")
        provider = get_llm_provider("research", default_settings)
        assert isinstance(provider, PerplexityProvider)
        assert provider.model == "sonar-pro"
        assert provider.timeout == 120.0

    def test_default_resolution_synthesis(self, default_settings, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
        provider = get_llm_provider("synthesis", default_settings)
        assert isinstance(provider, AnthropicProvider)
        assert provider.model == "claude-sonnet-5"
        assert provider.timeout == 60.0

    def test_env_var_overrides_provider(self, default_settings, monkeypatch):
        monkeypatch.setenv("OPENAI_API_KEY", "test-key")
        monkeypatch.setenv("FPL_SYNTHESIS_PROVIDER", "openai")
        monkeypatch.setenv("FPL_SYNTHESIS_MODEL", "gpt-4o")
        provider = get_llm_provider("synthesis", default_settings)
        assert isinstance(provider, OpenAICompatProvider)
        assert provider.model == "gpt-4o"

    def test_partial_override_resets_model(self, default_settings, monkeypatch):
        """Provider changed via env var, model not set -> use provider default."""
        monkeypatch.setenv("OPENAI_API_KEY", "test-key")
        monkeypatch.setenv("FPL_SYNTHESIS_PROVIDER", "openai")
        # No FPL_SYNTHESIS_MODEL set
        provider = get_llm_provider("synthesis", default_settings)
        assert isinstance(provider, OpenAICompatProvider)
        assert provider.model == OpenAICompatProvider.DEFAULT_MODEL

    def test_unknown_provider_raises(self, monkeypatch):
        settings = {"llm": {"research": {"provider": "nonexistent"}}}
        with pytest.raises(UnknownProviderError, match="nonexistent"):
            get_llm_provider("research", settings)

    def test_missing_key_raises(self, default_settings, monkeypatch):
        monkeypatch.delenv("PERPLEXITY_API_KEY", raising=False)
        with pytest.raises(ProviderNotConfiguredError, match="PERPLEXITY_API_KEY"):
            get_llm_provider("research", default_settings)

    def test_invalid_model_name_raises(self, monkeypatch):
        monkeypatch.setenv("PERPLEXITY_API_KEY", "test-key")
        settings = {"llm": {"research": {"provider": "perplexity", "model": "bad model!"}}}
        with pytest.raises(ProviderError, match="Invalid model name"):
            get_llm_provider("research", settings)

    def test_insecure_base_url_raises(self, monkeypatch):
        monkeypatch.setenv("OPENAI_API_KEY", "test-key")
        settings = {
            "llm": {
                "synthesis": {
                    "provider": "openai",
                    "model": "gpt-4o",
                    "base_url": "http://evil.com/v1",
                }
            }
        }
        with pytest.raises(ProviderError, match="Insecure base_url"):
            get_llm_provider("synthesis", settings)

    def test_localhost_http_allowed(self, monkeypatch):
        monkeypatch.setenv("OPENAI_API_KEY", "test-key")
        settings = {
            "llm": {
                "synthesis": {
                    "provider": "openai",
                    "model": "llama3",
                    "base_url": "http://localhost:11434/v1",
                }
            }
        }
        provider = get_llm_provider("synthesis", settings)
        assert isinstance(provider, OpenAICompatProvider)
        assert provider.base_url == "http://localhost:11434/v1"

    def test_base_url_env_var_override(self, monkeypatch):
        monkeypatch.setenv("OPENAI_API_KEY", "test-key")
        monkeypatch.setenv("FPL_SYNTHESIS_BASE_URL", "http://127.0.0.1:8080/v1")
        settings = {"llm": {"synthesis": {"provider": "openai", "model": "gpt-4o"}}}
        provider = get_llm_provider("synthesis", settings)
        assert isinstance(provider, OpenAICompatProvider)
        assert provider.base_url == "http://127.0.0.1:8080/v1"

    def test_query_defaults_passed_through(self, default_settings, monkeypatch):
        monkeypatch.setenv("PERPLEXITY_API_KEY", "test-key")
        provider = get_llm_provider("research", default_settings)
        assert provider.query_defaults == {"search_recency_filter": "week"}

    def test_nothing_configured_uses_defaults(self, monkeypatch):
        """No llm section in settings at all - falls back to empty config."""
        monkeypatch.setenv("PERPLEXITY_API_KEY", "test-key")
        # Empty settings but with env var provider override
        monkeypatch.setenv("FPL_RESEARCH_PROVIDER", "perplexity")
        provider = get_llm_provider("research", {})
        assert isinstance(provider, PerplexityProvider)
        assert provider.model == PerplexityProvider.DEFAULT_MODEL

    def test_empty_provider_name_raises(self):
        """No provider configured at all."""
        with pytest.raises(UnknownProviderError):
            get_llm_provider("research", {})
