"""Anthropic Claude provider using structured outputs (``output_config.format``)."""

from __future__ import annotations

import anthropic
from anthropic.types import ImageBlockParam, MessageParam, TextBlockParam

from extraction.llm.base import ChatMessage, JsonRequest, LLMError, LLMResponse, Throttle

DEFAULT_ANTHROPIC_MODEL = "claude-sonnet-5"


class AnthropicProvider:
    name = "anthropic"

    def __init__(
        self,
        model: str = DEFAULT_ANTHROPIC_MODEL,
        api_key: str | None = None,
        timeout: float = 120.0,
        client: anthropic.Anthropic | None = None,
        *,
        max_retries: int = 2,
        min_interval: float = 0.0,
    ) -> None:
        self.model = model
        # api_key=None lets the SDK resolve credentials from the environment. The SDK
        # retries 429/5xx/connection errors with exponential backoff.
        self._client = client or anthropic.Anthropic(
            api_key=api_key, timeout=timeout, max_retries=max_retries
        )
        self._throttle = Throttle(min_interval)

    def complete_json(self, request: JsonRequest) -> LLMResponse:
        self._throttle.wait()
        try:
            response = self._client.messages.create(
                model=self.model,
                max_tokens=request.max_tokens,
                system=request.system,
                messages=[_message_param(m) for m in request.messages],
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


def _message_param(message: ChatMessage) -> MessageParam:
    if not message.images:
        return {"role": message.role, "content": message.content}
    # Images first, then the instruction text that refers to them.
    content: list[ImageBlockParam | TextBlockParam] = [
        {
            "type": "image",
            "source": {"type": "base64", "media_type": image.media_type, "data": image.base64()},
        }
        for image in message.images
    ]
    content.append({"type": "text", "text": message.content})
    return {"role": message.role, "content": content}
