"""OpenRouter adapter (OpenAI-compatible) and request throttling - SDK client stubbed."""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import BaseModel, Field

from extraction.llm import build_provider
from extraction.llm.base import ChatMessage, JsonRequest, LLMError, Throttle
from extraction.llm.openrouter_provider import OPENROUTER_BASE_URL, OpenRouterProvider
from extraction.llm.schema import portable_json_schema
from extraction.schemas import Invoice


class _Completions:
    def __init__(self, response: Any) -> None:
        self.response = response
        self.kwargs: dict[str, Any] = {}

    def create(self, **kwargs: Any) -> Any:
        self.kwargs = kwargs
        return self.response


def _client(response: Any) -> tuple[Any, _Completions]:
    completions = _Completions(response)
    return SimpleNamespace(chat=SimpleNamespace(completions=completions)), completions


def _completion(served_by: str) -> Any:
    message = SimpleNamespace(content='{"a": 1}', refusal=None)
    return SimpleNamespace(
        choices=[SimpleNamespace(finish_reason="stop", message=message)],
        model=served_by,
        usage=SimpleNamespace(prompt_tokens=9, completion_tokens=3),
    )


REQUEST = JsonRequest(system="sys", messages=[ChatMessage("user", "hi")], schema=Invoice)


def test_fallback_models_go_into_the_request_body() -> None:
    client, completions = _client(_completion("google/gemma-4-31b-it:free"))
    provider = OpenRouterProvider(
        "dots-studio/dots-3-note-preview:free",
        client=client,
        fallback_models=["google/gemma-4-31b-it:free", "dots-studio/dots-3-note-preview:free"],
    )

    response = provider.complete_json(REQUEST)

    assert provider.name == "openrouter"
    assert completions.kwargs["model"] == "dots-studio/dots-3-note-preview:free"
    assert completions.kwargs["extra_body"] == {
        "models": ["dots-studio/dots-3-note-preview:free", "google/gemma-4-31b-it:free"]
    }
    assert completions.kwargs["response_format"]["type"] == "json_schema"
    assert response.model == "google/gemma-4-31b-it:free"  # the model that actually answered


def test_without_fallbacks_no_extra_body() -> None:
    client, completions = _client(_completion("m"))
    OpenRouterProvider("m", client=client).complete_json(REQUEST)
    assert "extra_body" not in completions.kwargs


def test_empty_choices_become_llm_error() -> None:
    body = SimpleNamespace(choices=[], model="m", usage=None, error={"code": 429})
    client, _ = _client(body)
    with pytest.raises(LLMError, match="No completion returned"):
        OpenRouterProvider("m", client=client).complete_json(REQUEST)


def test_client_configuration(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
    provider = build_provider(
        "openrouter", site_url="https://example.com", app_name="DocExtract", max_retries=5
    )
    client = provider._client  # type: ignore[attr-defined]
    assert str(client.base_url).rstrip("/") == OPENROUTER_BASE_URL
    assert client.max_retries == 5
    assert client.default_headers["HTTP-Referer"] == "https://example.com"
    assert client.default_headers["X-Title"] == "DocExtract"
    assert provider.model == "dots-studio/dots-3-note-preview:free"

    monkeypatch.delenv("OPENROUTER_API_KEY")
    with pytest.raises(ValueError, match="OPENROUTER_API_KEY"):
        build_provider("openrouter")


def test_throttle_spaces_requests(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = [100.0]
    sleeps: list[float] = []
    monkeypatch.setattr("extraction.llm.base.time.monotonic", lambda: clock[0])

    def sleep(seconds: float) -> None:
        sleeps.append(seconds)
        clock[0] += seconds

    monkeypatch.setattr("extraction.llm.base.time.sleep", sleep)
    throttle = Throttle(3.0)
    throttle.wait()
    clock[0] += 1.0
    throttle.wait()
    assert sleeps == [pytest.approx(2.0)]
    Throttle(0).wait()  # disabled: never sleeps
    assert len(sleeps) == 1


def test_portable_schema_inlines_refs_and_drops_value_constraints() -> None:
    schema = portable_json_schema(Invoice)
    text = json.dumps(schema)
    assert "$ref" not in text and "$defs" not in text
    assert '"pattern"' not in text and '"format"' not in text and '"minimum"' not in text
    vendor = schema["properties"]["vendor_name"]
    assert set(vendor["properties"]) == {"value", "confidence", "evidence"}
    assert "Legal name of the seller" in vendor["description"]


def test_portable_schema_keeps_fields_named_like_keywords() -> None:
    class Titled(BaseModel):
        title: str
        format: str = Field(pattern=r"^[A-Z]+$")

    schema = portable_json_schema(Titled)
    assert set(schema["properties"]) == {"title", "format"}
    assert "pattern" not in schema["properties"]["format"]


def test_portable_schema_rejects_recursion() -> None:
    class Node(BaseModel):
        children: list[Node] = []

    with pytest.raises(ValueError, match="Recursive schema 'Node'"):
        portable_json_schema(Node)
