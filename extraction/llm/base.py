"""Provider-neutral LLM interface used by the extraction and enrichment stages."""

from __future__ import annotations

import base64
import threading
import time
from dataclasses import dataclass, field
from typing import Literal, Protocol

from pydantic import BaseModel


class LLMError(Exception):
    """The provider failed to return a usable response (network, refusal, truncation...)."""


@dataclass(frozen=True, slots=True)
class ImageInput:
    """An image sent alongside a user message (vision-capable models only)."""

    media_type: Literal["image/png", "image/jpeg"]
    data: bytes

    def base64(self) -> str:
        return base64.standard_b64encode(self.data).decode("ascii")


@dataclass(frozen=True, slots=True)
class ChatMessage:
    role: Literal["user", "assistant"]
    content: str
    images: tuple[ImageInput, ...] = ()


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


class Throttle:
    """Keep at least ``min_interval`` seconds between requests (per process).

    Free and trial tiers (e.g. OpenRouter ``:free`` models) allow ~20 requests per
    minute; spacing requests out avoids burning retries on 429 responses.
    """

    def __init__(self, min_interval: float = 0.0) -> None:
        self.min_interval = min_interval
        self._lock = threading.Lock()
        self._last = float("-inf")

    def wait(self) -> None:
        if self.min_interval <= 0:
            return
        with self._lock:
            delay = self._last + self.min_interval - time.monotonic()
            if delay > 0:
                time.sleep(delay)
            self._last = time.monotonic()


class LLMProvider(Protocol):
    """Anything that can turn a :class:`JsonRequest` into raw JSON text.

    Providers only transport text. Validation against the Pydantic schema - and the
    retry-with-feedback loop - lives in :mod:`extraction.extract`, so it behaves the
    same for every vendor.
    """

    name: str
    model: str

    def complete_json(self, request: JsonRequest) -> LLMResponse: ...
