"""LLM providers: ``anthropic`` | ``openai`` | ``openrouter`` | ``fake`` (offline)."""

from __future__ import annotations

from collections.abc import Sequence

from extraction.llm.base import (
    ChatMessage,
    ImageInput,
    JsonRequest,
    LLMError,
    LLMProvider,
    LLMResponse,
)

PROVIDER_NAMES = ("fake", "openai", "anthropic", "openrouter")


def build_provider(
    name: str,
    *,
    model: str | None = None,
    api_key: str | None = None,
    timeout: float = 120.0,
    max_retries: int = 2,
    min_interval: float = 0.0,
    fallback_models: Sequence[str] = (),
    site_url: str = "",
    app_name: str = "",
    base_url: str = "",
    allow_paid_models: bool = False,
    strict_schema: bool = False,
) -> LLMProvider:
    """Instantiate a provider by name. SDK imports are lazy so ``fake`` needs no keys.

    ``max_retries`` / ``min_interval`` apply to the real providers; ``fallback_models``,
    ``site_url``, ``app_name``, ``base_url``, ``allow_paid_models`` and ``strict_schema``
    only to ``openrouter``, which rejects non-``:free`` models unless
    ``allow_paid_models`` is set.
    """
    name = name.lower()
    if name == "fake":
        from extraction.llm.fake import FAKE_MODEL, FakeProvider

        return FakeProvider(model or FAKE_MODEL)
    if name == "anthropic":
        from extraction.llm.anthropic_provider import DEFAULT_ANTHROPIC_MODEL, AnthropicProvider

        return AnthropicProvider(
            model or DEFAULT_ANTHROPIC_MODEL,
            api_key,
            timeout,
            max_retries=max_retries,
            min_interval=min_interval,
        )
    if name == "openai":
        from extraction.llm.openai_provider import DEFAULT_OPENAI_MODEL, OpenAIProvider

        return OpenAIProvider(
            model or DEFAULT_OPENAI_MODEL,
            api_key,
            timeout,
            max_retries=max_retries,
            min_interval=min_interval,
        )
    if name == "openrouter":
        from extraction.llm.openrouter_provider import (
            DEFAULT_OPENROUTER_MODEL,
            OpenRouterProvider,
        )

        return OpenRouterProvider(
            model or DEFAULT_OPENROUTER_MODEL,
            api_key,
            timeout,
            fallback_models=fallback_models,
            site_url=site_url,
            app_name=app_name,
            max_retries=max_retries,
            min_interval=min_interval,
            base_url=base_url,
            allow_paid_models=allow_paid_models,
            strict_schema=strict_schema,
        )
    raise ValueError(f"Unknown LLM provider '{name}'. Choose one of: {', '.join(PROVIDER_NAMES)}")


__all__ = [
    "PROVIDER_NAMES",
    "ChatMessage",
    "ImageInput",
    "JsonRequest",
    "LLMError",
    "LLMProvider",
    "LLMResponse",
    "build_provider",
]
