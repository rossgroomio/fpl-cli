"""Tests for LLM provider abstraction (fpl_cli/api/providers/)."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, PropertyMock

import httpx
import pytest

def _make_httpx_response(data: dict, status_code: int = 200):
    """Create a mock httpx.Response with sync json() and raise_for_status()."""
    resp = MagicMock()
    resp.json.return_value = data
    resp.status_code = status_code
    resp.raise_for_status.return_value = None
    return resp


from fpl_cli.api.providers import (
    AnthropicProvider,
    LLMResponse,
    OpenAICompatProvider,
    PerplexityProvider,
    ProviderError,
    ProviderNotConfiguredError,
    TokenUsage,
    UnknownProviderError,
    get_llm_provider,
)


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
            "model": "claude-sonnet-4-20250514",
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
        resp = MagicMock()
        resp.status_code = 429
        resp.raise_for_status.side_effect = httpx.HTTPStatusError(
            "rate limited", request=MagicMock(), response=resp
        )
        provider._http = AsyncMock()
        provider._http.post = AsyncMock(return_value=resp)

        with pytest.raises(ProviderError, match="HTTP 429"):
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

    def test_error_detail_falls_back_to_response_text(self):
        from fpl_cli.api.providers.anthropic import _error_detail

        resp = MagicMock()
        resp.json.side_effect = json.JSONDecodeError("bad", "", 0)
        type(resp).text = PropertyMock(return_value="plain error message")
        result = _error_detail(resp)
        assert result == ": plain error message"

    def test_error_detail_returns_empty_on_total_failure(self):
        from fpl_cli.api.providers.anthropic import _error_detail

        resp = MagicMock()
        resp.json.side_effect = json.JSONDecodeError("bad", "", 0)
        type(resp).text = PropertyMock(side_effect=RuntimeError("broken"))
        result = _error_detail(resp)
        assert result == ""

    async def test_query_merges_defaults(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
        provider = AnthropicProvider(query_defaults={"max_tokens": 2048})

        resp = _make_httpx_response({
            "content": [{"type": "text", "text": "ok"}],
            "model": "claude-sonnet-4-20250514",
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
                    "model": "claude-sonnet-4-20250514",
                    "timeout": 60,
                    "query_defaults": {"max_tokens": 1024},
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
        assert provider.model == "claude-sonnet-4-20250514"
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
