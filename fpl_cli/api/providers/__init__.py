"""LLM provider abstraction: response models, registry, factory."""

from __future__ import annotations

import ipaddress
import os
import re
from typing import Any
from urllib.parse import urlparse

from ._models import (
    LLMResponse,
    ProviderError,
    ProviderNotConfiguredError,
    TokenUsage,
    UnknownProviderError,
)
from .anthropic import AnthropicProvider
from .openai_compat import OpenAICompatProvider
from .perplexity import PerplexityProvider

ProviderType = type[AnthropicProvider] | type[PerplexityProvider] | type[OpenAICompatProvider]

PROVIDERS: dict[str, ProviderType] = {
    "perplexity": PerplexityProvider,
    "anthropic": AnthropicProvider,
    "openai": OpenAICompatProvider,
}

PROVIDER_NAMES: frozenset[str] = frozenset(PROVIDERS)

_MODEL_NAME_RE = re.compile(r"^[\w./:@-]+$")


def _validate_base_url(base_url: str) -> None:
    parsed = urlparse(base_url)
    if parsed.scheme not in ("http", "https"):
        raise ProviderError(
            f"base_url must use http:// or https:// (got {parsed.scheme!r})"
        )
    if parsed.scheme == "https":
        return
    # HTTP is only permitted for loopback addresses
    hostname = parsed.hostname or ""
    if hostname == "localhost":
        return
    try:
        if ipaddress.ip_address(hostname).is_loopback:
            return
    except ValueError:
        pass
    raise ProviderError(
        f"Insecure base_url: {base_url} -- use https:// for remote endpoints, "
        "http:// is only permitted for localhost"
    )


def get_llm_provider(
    role: str,
    settings: dict[str, Any],
) -> AnthropicProvider | PerplexityProvider | OpenAICompatProvider:
    """Resolve and instantiate a provider for a role.

    Resolution order per field: env var > settings.yaml > defaults.yaml.
    Partial override rule: if provider is overridden but model is not,
    model resets to the new provider's DEFAULT_MODEL.
    """
    role_upper = role.upper()
    llm_cfg = settings.get("llm", {}).get(role, {})

    # --- resolve provider name ---
    cfg_provider = llm_cfg.get("provider", "")
    env_provider = os.environ.get(f"FPL_{role_upper}_PROVIDER")
    provider_name = (env_provider or cfg_provider).lower()

    if provider_name not in PROVIDERS:
        available = ", ".join(sorted(PROVIDERS))
        raise UnknownProviderError(
            f"Unknown LLM provider {provider_name!r} for role {role!r}. "
            f"Available: {available}. Run 'fpl init' to configure."
        )

    provider_cls = PROVIDERS[provider_name]
    provider_changed = bool(
        env_provider and cfg_provider and env_provider.lower() != cfg_provider.lower()
    )

    # --- resolve model ---
    cfg_model = llm_cfg.get("model", "")
    env_model = os.environ.get(f"FPL_{role_upper}_MODEL")

    if env_model:
        model = env_model
    elif provider_changed:
        model = provider_cls.DEFAULT_MODEL
    else:
        model = cfg_model or provider_cls.DEFAULT_MODEL

    if not _MODEL_NAME_RE.match(model):
        raise ProviderError(
            f"Invalid model name {model!r}. "
            "Model names may contain letters, digits, hyphens, dots, slashes, colons, and @."
        )

    # --- resolve timeout ---
    timeout = float(llm_cfg.get("timeout", provider_cls.DEFAULT_TIMEOUT))

    # --- resolve base_url (openai provider only) ---
    cfg_base_url = llm_cfg.get("base_url")
    env_base_url = os.environ.get(f"FPL_{role_upper}_BASE_URL")
    base_url = env_base_url or cfg_base_url

    if base_url:
        _validate_base_url(base_url)

    # --- resolve query_defaults ---
    query_defaults = dict(llm_cfg.get("query_defaults", {}))

    # --- instantiate ---
    kwargs: dict[str, Any] = {
        "model": model,
        "timeout": timeout,
        "query_defaults": query_defaults,
    }
    if base_url and provider_name == "openai":
        kwargs["base_url"] = base_url

    provider = provider_cls(**kwargs)

    # --- check configuration ---
    if not provider.is_configured:
        raise ProviderNotConfiguredError(
            f"{provider_cls.API_KEY_ENV_VAR} not set. "
            f"Get your key from {provider_cls.KEY_SETUP_URL}"
        )

    return provider


__all__ = [
    "AnthropicProvider",
    "LLMResponse",
    "OpenAICompatProvider",
    "PROVIDERS",
    "PROVIDER_NAMES",
    "PerplexityProvider",
    "ProviderError",
    "ProviderNotConfiguredError",
    "TokenUsage",
    "UnknownProviderError",
    "get_llm_provider",
]
