"""LLM providers: ``openai`` | ``anthropic`` | ``fake`` (deterministic, offline)."""

from __future__ import annotations

from extraction.llm.base import ChatMessage, JsonRequest, LLMError, LLMProvider, LLMResponse

PROVIDER_NAMES = ("fake", "openai", "anthropic")


def build_provider(
    name: str,
    *,
    model: str | None = None,
    api_key: str | None = None,
    timeout: float = 120.0,
) -> LLMProvider:
    """Instantiate a provider by name. SDK imports are lazy so ``fake`` needs no keys."""
    name = name.lower()
    if name == "fake":
        from extraction.llm.fake import FAKE_MODEL, FakeProvider

        return FakeProvider(model or FAKE_MODEL)
    if name == "anthropic":
        from extraction.llm.anthropic_provider import DEFAULT_ANTHROPIC_MODEL, AnthropicProvider

        return AnthropicProvider(model or DEFAULT_ANTHROPIC_MODEL, api_key, timeout)
    if name == "openai":
        from extraction.llm.openai_provider import DEFAULT_OPENAI_MODEL, OpenAIProvider

        return OpenAIProvider(model or DEFAULT_OPENAI_MODEL, api_key, timeout)
    raise ValueError(f"Unknown LLM provider '{name}'. Choose one of: {', '.join(PROVIDER_NAMES)}")


__all__ = [
    "PROVIDER_NAMES",
    "ChatMessage",
    "JsonRequest",
    "LLMError",
    "LLMProvider",
    "LLMResponse",
    "build_provider",
]
