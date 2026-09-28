"""Stage 2 - LLM structured extraction with validation-error feedback.

The model is asked for JSON matching a Pydantic schema. If the output does not parse
or fails validation, the exact validation errors are sent back as a follow-up turn and
the model gets another attempt (up to ``max_attempts``). Every attempt is recorded so
the Django layer can persist raw output, tokens and latency for auditing.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field

import structlog
from pydantic import BaseModel, ValidationError

from extraction.llm.base import ChatMessage, JsonRequest, LLMError, LLMProvider
from extraction.parse import ParsedDocument

logger = structlog.get_logger(__name__)

SYSTEM_PROMPT = """\
You extract structured data from business documents (invoices, receipts, supplier \
emails) for an accounts-payable team. Your output is checked by deterministic \
validators and uncertain records are reviewed by a human, so accuracy and honest \
confidence matter more than completeness.

Rules:
- Only report values that appear in the document. Use null for anything absent; never guess.
- For every value, copy a short verbatim quote from the document as evidence (exact \
characters, including original number formatting) and give its page number.
- Normalize values: dates as YYYY-MM-DD, amounts as plain numbers with "." as the \
decimal separator and no thousands separators, currency as an ISO 4217 code, tax IDs \
without spaces.
- The vendor is the party issuing the invoice (seller), not the customer being billed.
- confidence is your probability (0-1) that the value is correct. Use lower values for \
unlabeled, ambiguous or inferred values.
- Respond with a single JSON object that matches the schema. No prose, no code fences."""


def render_document(document: ParsedDocument) -> str:
    """Render pages with explicit page markers so the model can cite page numbers."""
    pages = "\n".join(
        f'<page number="{page.number}">\n{page.text}\n</page>' for page in document.pages
    )
    return f"<document>\n{pages}\n</document>"


def build_user_prompt(document: ParsedDocument, schema: type[BaseModel]) -> str:
    return (
        f"Extract a `{schema.__name__}` record from the document below.\n\n"
        f"JSON schema:\n{json.dumps(schema.model_json_schema(), separators=(',', ':'))}\n\n"
        f"{render_document(document)}"
    )


@dataclass(frozen=True, slots=True)
class AttemptRecord:
    attempt: int
    raw_output: str
    error: str | None
    input_tokens: int
    output_tokens: int
    latency_ms: int


@dataclass(slots=True)
class ExtractionResult[M: BaseModel]:
    provider: str
    model: str
    data: M | None = None
    attempts: list[AttemptRecord] = field(default_factory=list)
    error: str | None = None

    @property
    def succeeded(self) -> bool:
        return self.data is not None

    @property
    def raw_output(self) -> str:
        return self.attempts[-1].raw_output if self.attempts else ""

    @property
    def input_tokens(self) -> int:
        return sum(a.input_tokens for a in self.attempts)

    @property
    def output_tokens(self) -> int:
        return sum(a.output_tokens for a in self.attempts)

    @property
    def latency_ms(self) -> int:
        return sum(a.latency_ms for a in self.attempts)


def extract_structured[M: BaseModel](
    document: ParsedDocument,
    schema: type[M],
    provider: LLMProvider,
    *,
    max_attempts: int = 3,
    system_prompt: str = SYSTEM_PROMPT,
    max_tokens: int = 8000,
) -> ExtractionResult[M]:
    """Extract ``schema`` from ``document``; retry with error feedback on invalid output.

    Never raises for bad model output - check ``result.succeeded``. Transport-level
    failures (:class:`LLMError`) stop the loop immediately, since re-asking will not help.
    """
    if max_attempts < 1:
        raise ValueError("max_attempts must be >= 1")

    result: ExtractionResult[M] = ExtractionResult(provider=provider.name, model=provider.model)
    messages = [ChatMessage("user", build_user_prompt(document, schema))]
    log = logger.bind(schema=schema.__name__, provider=provider.name, model=provider.model)

    for attempt in range(1, max_attempts + 1):
        started = time.perf_counter()
        try:
            response = provider.complete_json(
                JsonRequest(
                    system=system_prompt, messages=messages, schema=schema, max_tokens=max_tokens
                )
            )
        except LLMError as exc:
            result.attempts.append(AttemptRecord(attempt, "", str(exc), 0, 0, _elapsed_ms(started)))
            result.error = str(exc)
            log.warning("extraction.provider_error", attempt=attempt, error=str(exc))
            return result

        latency = _elapsed_ms(started)
        if response.model:
            result.model = response.model
        try:
            result.data = schema.model_validate_json(_json_payload(response.text))
            error = None
        except ValidationError as exc:
            error = format_validation_errors(exc)

        result.attempts.append(
            AttemptRecord(
                attempt=attempt,
                raw_output=response.text,
                error=error,
                input_tokens=response.input_tokens,
                output_tokens=response.output_tokens,
                latency_ms=latency,
            )
        )
        if error is None:
            log.info("extraction.succeeded", attempt=attempt, latency_ms=latency)
            return result

        log.info("extraction.invalid_output", attempt=attempt, error=error)
        messages = [
            *messages,
            ChatMessage("assistant", response.text),
            ChatMessage("user", feedback_prompt(error)),
        ]

    result.error = f"Output failed schema validation after {max_attempts} attempts"
    log.warning("extraction.failed", attempts=max_attempts)
    return result


def feedback_prompt(error: str) -> str:
    return (
        "Your previous response did not pass schema validation:\n"
        f"{error}\n\n"
        "Fix these problems and return the complete corrected JSON object only."
    )


def format_validation_errors(exc: ValidationError, limit: int = 20) -> str:
    lines = []
    for err in exc.errors()[:limit]:
        location = ".".join(str(part) for part in err["loc"]) or "<root>"
        lines.append(f"- {location}: {err['msg']}")
    if exc.error_count() > limit:
        lines.append(f"- ... and {exc.error_count() - limit} more errors")
    return "\n".join(lines)


def _json_payload(text: str) -> str:
    """Tolerate code fences or stray prose around the JSON object."""
    start, end = text.find("{"), text.rfind("}")
    return text[start : end + 1] if start != -1 and end > start else text


def _elapsed_ms(started: float) -> int:
    return int((time.perf_counter() - started) * 1000)
