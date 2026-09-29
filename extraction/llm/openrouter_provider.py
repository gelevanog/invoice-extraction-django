"""OpenRouter: one OpenAI-compatible API in front of many models, including free ones.

Differences from plain OpenAI are configuration only: the base URL, the API key
(``OPENROUTER_API_KEY``), optional attribution headers, and a ``models`` list in the
request body - OpenRouter tries those in order when the primary model is rate-limited
or down. The model that actually answered is reported back and stored with each run.
"""

from __future__ import annotations

import os
from collections.abc import Sequence

import openai

from extraction.llm.openai_provider import OpenAIProvider

OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
DEFAULT_OPENROUTER_MODEL = "dots-studio/dots-3-note-preview:free"


class OpenRouterProvider(OpenAIProvider):
    name = "openrouter"

    def __init__(
        self,
        model: str = DEFAULT_OPENROUTER_MODEL,
        api_key: str | None = None,
        timeout: float = 120.0,
        client: openai.OpenAI | None = None,
        *,
        fallback_models: Sequence[str] = (),
        site_url: str = "",
        app_name: str = "",
        max_retries: int = 2,
        min_interval: float = 0.0,
    ) -> None:
        if client is None:
            key = api_key or os.environ.get("OPENROUTER_API_KEY")
            if not key:
                raise ValueError("LLM_PROVIDER=openrouter needs OPENROUTER_API_KEY")
            headers = {"HTTP-Referer": site_url, "X-Title": app_name}
            client = openai.OpenAI(
                api_key=key,
                base_url=OPENROUTER_BASE_URL,
                timeout=timeout,
                max_retries=max_retries,
                default_headers={k: v for k, v in headers.items() if v} or None,
            )
        fallbacks = [m for m in fallback_models if m != model]
        super().__init__(
            model,
            client=client,
            min_interval=min_interval,
            extra_body={"models": [model, *fallbacks]} if fallbacks else None,
        )
