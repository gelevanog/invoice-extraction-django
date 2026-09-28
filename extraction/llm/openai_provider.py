"""OpenAI provider using Chat Completions with a JSON-schema response format."""

from __future__ import annotations

import openai
from openai.types.chat import ChatCompletionMessageParam
from openai.types.shared_params import ResponseFormatJSONSchema

from extraction.llm.base import JsonRequest, LLMError, LLMResponse

DEFAULT_OPENAI_MODEL = "gpt-5.4-mini"


class OpenAIProvider:
    name = "openai"

    def __init__(
        self,
        model: str = DEFAULT_OPENAI_MODEL,
        api_key: str | None = None,
        timeout: float = 120.0,
        client: openai.OpenAI | None = None,
    ) -> None:
        self.model = model
        self._client = client or openai.OpenAI(api_key=api_key, timeout=timeout)

    def complete_json(self, request: JsonRequest) -> LLMResponse:
        messages: list[ChatCompletionMessageParam] = [{"role": "system", "content": request.system}]
        for message in request.messages:
            if message.role == "user":
                messages.append({"role": "user", "content": message.content})
            else:
                messages.append({"role": "assistant", "content": message.content})
        # Non-strict: strict mode requires every property to be required, which does not
        # fit optional fields. Pydantic validation + the retry loop enforce the schema.
        response_format: ResponseFormatJSONSchema = {
            "type": "json_schema",
            "json_schema": {
                "name": request.schema.__name__,
                "schema": request.schema.model_json_schema(),
                "strict": False,
            },
        }
        try:
            response = self._client.chat.completions.create(
                model=self.model,
                messages=messages,
                max_completion_tokens=request.max_tokens,
                response_format=response_format,
            )
        except openai.APIConnectionError as exc:
            raise LLMError(f"OpenAI connection error: {exc}") from exc
        except openai.RateLimitError as exc:
            raise LLMError("OpenAI rate limit exceeded") from exc
        except openai.APIStatusError as exc:
            raise LLMError(f"OpenAI API error {exc.status_code}: {exc.message}") from exc

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
