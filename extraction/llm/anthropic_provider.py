"""Anthropic Claude provider using structured outputs (``output_config.format``)."""

from __future__ import annotations

import anthropic

from extraction.llm.base import JsonRequest, LLMError, LLMResponse

DEFAULT_ANTHROPIC_MODEL = "claude-sonnet-5"


class AnthropicProvider:
    name = "anthropic"

    def __init__(
        self,
        model: str = DEFAULT_ANTHROPIC_MODEL,
        api_key: str | None = None,
        timeout: float = 120.0,
        client: anthropic.Anthropic | None = None,
    ) -> None:
        self.model = model
        # api_key=None lets the SDK resolve credentials from the environment.
        self._client = client or anthropic.Anthropic(api_key=api_key, timeout=timeout)

    def complete_json(self, request: JsonRequest) -> LLMResponse:
        try:
            response = self._client.messages.create(
                model=self.model,
                max_tokens=request.max_tokens,
                system=request.system,
                messages=[{"role": m.role, "content": m.content} for m in request.messages],
                # Constrained decoding against the schema; the SDK strips keywords the API
                # does not support (min/max etc.) - those are re-checked by Pydantic.
                output_config={
                    "format": {
                        "type": "json_schema",
                        "schema": anthropic.transform_schema(request.schema),
                    }
                },
            )
        except anthropic.APIConnectionError as exc:
            raise LLMError(f"Anthropic connection error: {exc}") from exc
        except anthropic.RateLimitError as exc:
            raise LLMError("Anthropic rate limit exceeded") from exc
        except anthropic.APIStatusError as exc:
            raise LLMError(f"Anthropic API error {exc.status_code}: {exc.message}") from exc

        if response.stop_reason == "refusal":
            raise LLMError("Model declined the request (stop_reason=refusal)")
        if response.stop_reason == "max_tokens":
            raise LLMError(f"Output truncated at max_tokens={request.max_tokens}")

        text = "".join(block.text for block in response.content if block.type == "text")
        return LLMResponse(
            text=text,
            model=response.model,
            input_tokens=response.usage.input_tokens,
            output_tokens=response.usage.output_tokens,
            metadata={"stop_reason": str(response.stop_reason)},
        )
