"""Provider-neutral LLM interface used by the extraction and enrichment stages."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, Protocol

from pydantic import BaseModel


class LLMError(Exception):
    """The provider failed to return a usable response (network, refusal, truncation...)."""


@dataclass(frozen=True, slots=True)
class ChatMessage:
    role: Literal["user", "assistant"]
    content: str


@dataclass(frozen=True, slots=True)
class JsonRequest:
    """Ask the model for a JSON object matching ``schema``."""

    system: str
    messages: list[ChatMessage]
    schema: type[BaseModel]
    max_tokens: int = 8000


@dataclass(frozen=True, slots=True)
class LLMResponse:
    text: str
    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    metadata: dict[str, str] = field(default_factory=dict)


class LLMProvider(Protocol):
    """Anything that can turn a :class:`JsonRequest` into raw JSON text.

    Providers only transport text. Validation against the Pydantic schema - and the
    retry-with-feedback loop - lives in :mod:`extraction.extract`, so it behaves the
    same for every vendor.
    """

    name: str
    model: str

    def complete_json(self, request: JsonRequest) -> LLMResponse: ...
