"""OpenAI provider using Chat Completions with a JSON-schema response format.

Also the base for OpenAI-compatible APIs (see :mod:`extraction.llm.openrouter_provider`).
"""

from __future__ import annotations

import json
from typing import Any, Literal

import openai
from openai.types.chat import (
    ChatCompletionContentPartImageParam,
    ChatCompletionContentPartTextParam,
    ChatCompletionMessageParam,
)
from openai.types.shared_params import ResponseFormatJSONSchema

from extraction.llm.base import ChatMessage, JsonRequest, LLMError, LLMResponse, Throttle
from extraction.llm.schema import portable_json_schema

DEFAULT_OPENAI_MODEL = "gpt-5.4-mini"

# How the JSON schema is sent: ``json_schema`` (non-strict, the default), ``strict``
# (``strict: true``, for endpoints that only accept that) or ``none`` (no
# ``response_format``; the schema is still in the prompt and Pydantic validates the
# answer - for endpoints whose structured-output mode cannot compile the schema).
ResponseFormatMode = Literal["json_schema", "strict", "none"]
RESPONSE_FORMAT_MODES: tuple[ResponseFormatMode, ...] = ("json_schema", "strict", "none")
SCHEMA_IN_PROMPT = "Respond with a single JSON object (no prose) that matches this JSON schema:"


class OpenAIProvider:
    name = "openai"

    def __init__(
        self,
        model: str = DEFAULT_OPENAI_MODEL,
        api_key: str | None = None,
        timeout: float = 120.0,
        client: openai.OpenAI | None = None,
        *,
        max_retries: int = 2,
        min_interval: float = 0.0,
        extra_body: dict[str, Any] | None = None,
        response_format: ResponseFormatMode = "json_schema",
    ) -> None:
        if response_format not in RESPONSE_FORMAT_MODES:
            raise ValueError(
                f"Unknown response format '{response_format}'. "
                f"Choose one of: {', '.join(RESPONSE_FORMAT_MODES)}"
            )
        self.model = model
        # The SDK retries 429/5xx/connection errors with exponential backoff and honours
        # Retry-After; ``max_retries`` sets how many times.
        self._client = client or openai.OpenAI(
            api_key=api_key, timeout=timeout, max_retries=max_retries
        )
        self._throttle = Throttle(min_interval)
        self._extra_body = extra_body
        self._response_format = response_format

    def complete_json(self, request: JsonRequest) -> LLMResponse:
        system = request.system
        if self._response_format == "none":
            # Without response_format only the prompt carries the schema.
            schema = json.dumps(portable_json_schema(request.schema), separators=(",", ":"))
            system = f"{system}\n\n{SCHEMA_IN_PROMPT}\n{schema}"
        messages: list[ChatCompletionMessageParam] = [{"role": "system", "content": system}]
        for message in request.messages:
            if message.role == "user":
                messages.append({"role": "user", "content": _user_content(message)})
            else:
                messages.append({"role": "assistant", "content": message.content})
        # Non-strict by default: OpenAI's strict mode requires every property to be
        # required, which does not fit optional fields. The portable schema (refs
        # inlined, value constraints dropped) is accepted by OpenAI-compatible gateways
        # too; Pydantic validation + the retry loop enforce the full schema.
        options: dict[str, Any] = {"extra_body": self._extra_body} if self._extra_body else {}
        if self._response_format != "none":
            response_format: ResponseFormatJSONSchema = {
                "type": "json_schema",
                "json_schema": {
                    "name": request.schema.__name__,
                    "schema": portable_json_schema(request.schema),
                    "strict": self._response_format == "strict",
                },
            }
            options["response_format"] = response_format
        self._throttle.wait()
        try:
            response = self._client.chat.completions.create(
                model=self.model,
                messages=messages,
                max_completion_tokens=request.max_tokens,
                **options,
            )
        except openai.APIConnectionError as exc:
            raise LLMError(f"OpenAI connection error: {exc}") from exc
        except openai.RateLimitError as exc:
            raise LLMError("OpenAI rate limit exceeded") from exc
        except openai.APIStatusError as exc:
            raise LLMError(f"OpenAI API error {exc.status_code}: {exc.message}") from exc

        if not response.choices:  # some gateways report upstream errors in a 200 body
            raise LLMError(f"No completion returned: {getattr(response, 'error', None)}")
        choice = response.choices[0]
        if choice.finish_reason == "length":
            raise LLMError(f"Output truncated at max_completion_tokens={request.max_tokens}")
        if choice.message.refusal:
            raise LLMError(f"Model refused: {choice.message.refusal}")

        usage = response.usage
        return LLMResponse(
            text=choice.message.content or "",
            model=response.model,
            input_tokens=usage.prompt_tokens if usage else 0,
            output_tokens=usage.completion_tokens if usage else 0,
            metadata={"finish_reason": str(choice.finish_reason)},
        )


def _user_content(
    message: ChatMessage,
) -> str | list[ChatCompletionContentPartImageParam | ChatCompletionContentPartTextParam]:
    if not message.images:
        return message.content
    parts: list[ChatCompletionContentPartImageParam | ChatCompletionContentPartTextParam] = [
        {
            "type": "image_url",
            "image_url": {"url": f"data:{image.media_type};base64,{image.base64()}"},
        }
        for image in message.images
    ]
    parts.append({"type": "text", "text": message.content})
    return parts
