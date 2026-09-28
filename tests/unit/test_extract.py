"""Retry-with-validation-feedback loop, the fake provider, and the real provider adapters."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from extraction.extract import SYSTEM_PROMPT, extract_structured, render_document
from extraction.llm import build_provider
from extraction.llm.anthropic_provider import AnthropicProvider
from extraction.llm.base import ChatMessage, JsonRequest, LLMError, LLMResponse
from extraction.llm.fake import FakeProvider
from extraction.llm.openai_provider import OpenAIProvider
from extraction.parse import ParsedDocument, parse_document
from extraction.schemas import Invoice

DOC = ParsedDocument.from_page_texts(["ACME Ltd\nInvoice No: A-1\nTotal EUR 10.00"], "text")


def valid_payload() -> dict[str, Any]:
    empty = {"value": None, "confidence": 0.9, "evidence": None}
    return {
        "vendor_name": {"value": "ACME Ltd", "confidence": 0.9,
                        "evidence": {"quote": "ACME Ltd", "page": 1}},
        "vendor_tax_id": empty, "invoice_number": {**empty, "value": "A-1"},
        "issue_date": empty, "due_date": empty, "currency": {**empty, "value": "EUR"},
        "subtotal": empty, "tax": empty, "total": {**empty, "value": "10.00"},
        "line_items": [],
    }  # fmt: skip


class ScriptedProvider:
    """Returns canned responses in order and records every request it receives."""

    name = "scripted"
    model = "scripted-1"

    def __init__(self, *responses: str | Exception) -> None:
        self.responses = list(responses)
        self.requests: list[JsonRequest] = []

    def complete_json(self, request: JsonRequest) -> LLMResponse:
        self.requests.append(request)
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return LLMResponse(text=response, model=self.model, input_tokens=100, output_tokens=50)


def test_first_attempt_success() -> None:
    provider = ScriptedProvider(json.dumps(valid_payload()))
    result = extract_structured(DOC, Invoice, provider)
    assert result.succeeded
    assert result.data is not None and result.data.invoice_number.value == "A-1"
    assert len(result.attempts) == 1
    assert (result.input_tokens, result.output_tokens) == (100, 50)


def test_retries_with_validation_errors_fed_back() -> None:
    bad = valid_payload()
    bad["total"]["confidence"] = 1.7  # out of range
    del bad["currency"]  # missing required field
    provider = ScriptedProvider(json.dumps(bad), json.dumps(valid_payload()))

    result = extract_structured(DOC, Invoice, provider, max_attempts=3)

    assert result.succeeded
    assert [a.error is None for a in result.attempts] == [False, True]
    assert result.input_tokens == 200

    retry_messages = provider.requests[1].messages
    assert [m.role for m in retry_messages] == ["user", "assistant", "user"]
    assert retry_messages[1].content == json.dumps(bad)  # model sees its own answer
    feedback = retry_messages[2].content
    assert "total.confidence" in feedback
    assert "currency: Field required" in feedback


def test_invalid_json_is_retried() -> None:
    provider = ScriptedProvider("Sure! Here is the data: {oops", json.dumps(valid_payload()))
    result = extract_structured(DOC, Invoice, provider)
    assert result.succeeded
    assert "Invalid JSON" in (result.attempts[0].error or "")


def test_code_fences_are_tolerated() -> None:
    provider = ScriptedProvider(f"```json\n{json.dumps(valid_payload())}\n```")
    assert extract_structured(DOC, Invoice, provider).succeeded


def test_gives_up_after_max_attempts() -> None:
    provider = ScriptedProvider("{}", "{}", "{}")
    result = extract_structured(DOC, Invoice, provider, max_attempts=3)
    assert not result.succeeded
    assert len(result.attempts) == 3
    assert result.error == "Output failed schema validation after 3 attempts"
    assert result.raw_output == "{}"


def test_provider_error_stops_immediately() -> None:
    provider = ScriptedProvider(LLMError("rate limited"), json.dumps(valid_payload()))
    result = extract_structured(DOC, Invoice, provider, max_attempts=3)
    assert not result.succeeded
    assert result.error == "rate limited"
    assert len(provider.requests) == 1


def test_max_attempts_must_be_positive() -> None:
    with pytest.raises(ValueError, match="max_attempts"):
        extract_structured(DOC, Invoice, ScriptedProvider(), max_attempts=0)


def test_prompt_contains_schema_and_page_markers() -> None:
    provider = ScriptedProvider(json.dumps(valid_payload()))
    extract_structured(DOC, Invoice, provider)
    request = provider.requests[0]
    assert request.system == SYSTEM_PROMPT
    assert '<page number="1">' in request.messages[0].content
    assert '"vendor_name"' in request.messages[0].content


# --- fake provider ------------------------------------------------------------------
def test_fake_provider_reads_sample_invoice(sample_dir: Path) -> None:
    path = sample_dir / "02_northwind_office_supplies.pdf"
    document = parse_document(path.name, path.read_bytes())
    result = extract_structured(document, Invoice, FakeProvider())
    invoice = result.data
    assert invoice is not None
    assert invoice.vendor_name.value == "Northwind Traders Ltd."
    assert invoice.vendor_tax_id.value == "GB123456789"
    assert invoice.currency.value == "GBP"
    assert str(invoice.total.value) == "656.40"
    assert [str(i.amount) for i in invoice.line_items] == ["215.00", "275.60", "56.40"]
    assert invoice.invoice_number.evidence is not None
    assert invoice.invoice_number.evidence.quote == "Invoice No: NWT-58311"


def test_fake_provider_is_deterministic(sample_dir: Path) -> None:
    path = sample_dir / "05_pinecrest_hardware_receipt.txt"
    prompt = render_document(parse_document(path.name, path.read_bytes()))
    request = JsonRequest(system="", messages=[ChatMessage("user", prompt)], schema=Invoice)
    first, second = FakeProvider().complete_json(request), FakeProvider().complete_json(request)
    assert first.text == second.text
    data = json.loads(first.text)
    assert data["invoice_number"] == {
        "value": "PH-88213",
        "confidence": 0.55,  # unlabeled reference -> low confidence
        "evidence": {"quote": "Ref PH-88213", "page": 1},
    }


def test_fake_provider_rejects_unknown_schema() -> None:
    request = JsonRequest(system="", messages=[ChatMessage("user", "x")], schema=JsonRequest)  # type: ignore[arg-type]
    with pytest.raises(LLMError):
        FakeProvider().complete_json(request)


def test_build_provider() -> None:
    assert build_provider("fake").name == "fake"
    with pytest.raises(ValueError, match="Unknown LLM provider"):
        build_provider("llama")


# --- real provider adapters (SDK clients stubbed, no network) -----------------------
class _Recorder:
    def __init__(self, response: Any) -> None:
        self.response = response
        self.kwargs: dict[str, Any] = {}

    def create(self, **kwargs: Any) -> Any:
        self.kwargs = kwargs
        return self.response


def _request() -> JsonRequest:
    return JsonRequest(
        system="sys", messages=[ChatMessage("user", "hi")], schema=Invoice, max_tokens=123
    )


def test_anthropic_provider_sends_structured_output_request() -> None:
    message = SimpleNamespace(
        content=[SimpleNamespace(type="thinking"), SimpleNamespace(type="text", text='{"a": 1}')],
        stop_reason="end_turn",
        model="claude-sonnet-5",
        usage=SimpleNamespace(input_tokens=11, output_tokens=7),
    )
    recorder = _Recorder(message)
    provider = AnthropicProvider(client=SimpleNamespace(messages=recorder))  # type: ignore[arg-type]

    response = provider.complete_json(_request())

    assert response == LLMResponse(
        '{"a": 1}', "claude-sonnet-5", 11, 7, {"stop_reason": "end_turn"}
    )
    assert provider.model == "claude-sonnet-5"
    sent = recorder.kwargs
    assert sent["model"] == "claude-sonnet-5"
    assert sent["max_tokens"] == 123
    assert sent["system"] == "sys"
    assert sent["output_config"]["format"]["type"] == "json_schema"
    assert "vendor_name" in sent["output_config"]["format"]["schema"]["properties"]


@pytest.mark.parametrize("stop_reason", ["refusal", "max_tokens"])
def test_anthropic_provider_raises_on_unusable_stop(stop_reason: str) -> None:
    message = SimpleNamespace(content=[], stop_reason=stop_reason, model="m",
                              usage=SimpleNamespace(input_tokens=1, output_tokens=1))  # fmt: skip
    provider = AnthropicProvider(client=SimpleNamespace(messages=_Recorder(message)))  # type: ignore[arg-type]
    with pytest.raises(LLMError):
        provider.complete_json(_request())


def test_openai_provider_sends_json_schema_request() -> None:
    completion = SimpleNamespace(
        choices=[SimpleNamespace(finish_reason="stop",
                                 message=SimpleNamespace(content='{"a": 1}', refusal=None))],
        model="gpt-5-mini",
        usage=SimpleNamespace(prompt_tokens=20, completion_tokens=5),
    )  # fmt: skip
    recorder = _Recorder(completion)
    client = SimpleNamespace(chat=SimpleNamespace(completions=recorder))
    provider = OpenAIProvider(model="gpt-5-mini", client=client)  # type: ignore[arg-type]

    response = provider.complete_json(_request())

    assert (response.text, response.input_tokens, response.output_tokens) == ('{"a": 1}', 20, 5)
    sent = recorder.kwargs
    assert sent["messages"][0] == {"role": "system", "content": "sys"}
    assert sent["response_format"]["json_schema"]["name"] == "Invoice"
    assert sent["max_completion_tokens"] == 123


def test_openai_provider_raises_on_truncation() -> None:
    message = SimpleNamespace(content="{", refusal=None)
    choice = SimpleNamespace(finish_reason="length", message=message)
    completion = SimpleNamespace(choices=[choice], model="m", usage=None)
    client = SimpleNamespace(chat=SimpleNamespace(completions=_Recorder(completion)))
    with pytest.raises(LLMError, match="truncated"):
        OpenAIProvider(client=client).complete_json(_request())  # type: ignore[arg-type]
