"""Resolve LLM-provided evidence quotes to exact character spans in the source text.

LLMs are good at quoting but bad at counting characters, so the model returns a quote
and we find it ourselves (whitespace- and case-insensitive). A quote that cannot be
found is a signal the value may be hallucinated; validation reports it.

For OCR text, a span also carries the confidence of the words it covers: a value can
never be more certain than the characters it was read from, so
:func:`cap_confidence_to_ocr` lowers field confidences accordingly (which in turn
routes documents with poorly recognised values to review).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from extraction.parse import Page, ParsedDocument
from extraction.schemas import Evidence, Extracted, Invoice, LineItem


@dataclass(frozen=True, slots=True)
class Span:
    start: int
    end: int
    page: int | None
    ocr_confidence: float | None = None  # lowest OCR word confidence inside the span

    def as_dict(self) -> dict[str, float | int | None]:
        return {
            "start": self.start,
            "end": self.end,
            "page": self.page,
            "ocr_confidence": self.ocr_confidence,
        }


def locate(evidence: Evidence, document: ParsedDocument) -> Span | None:
    tokens = evidence.quote.split()
    if not tokens:
        return None
    pattern = re.compile(r"\s+".join(re.escape(token) for token in tokens), re.IGNORECASE)

    # Prefer the page the model cited, then fall back to the whole document.
    hinted = next((p for p in document.pages if p.number == evidence.page), None)
    if hinted is not None and (match := pattern.search(hinted.text)):
        return _span(hinted, match.start(), match.end())
    for page in document.pages:
        if match := pattern.search(page.text):
            return _span(page, match.start(), match.end())
    return None


def _span(page: Page, start: int, end: int) -> Span:
    """Build a document-level span from page-relative offsets."""
    return Span(page.start + start, page.start + end, page.number, ocr_confidence(page, start, end))


def ocr_confidence(page: Page, start: int, end: int) -> float | None:
    """Lowest confidence of the OCR words overlapping ``[start, end)`` of the page text.

    The minimum (not the mean) is deliberate: one misread digit makes the whole amount
    wrong, however confidently the label next to it was recognised.
    """
    if not page.is_ocr:
        return None
    scores = [w.confidence for w in page.words if w.start < end and w.end > start]
    return min(scores) if scores else None


def resolve_invoice_evidence(invoice: Invoice, document: ParsedDocument) -> dict[str, Span | None]:
    """Map each field (and ``line_items.<i>``) that has evidence to its span or ``None``."""
    spans: dict[str, Span | None] = {}
    for name, field in invoice.scalar_fields().items():
        if field.evidence is not None:
            spans[name] = locate(field.evidence, document)
    for index, item in enumerate(invoice.line_items):
        if item.evidence is not None:
            spans[f"line_items.{index}"] = locate(item.evidence, document)
    return spans


def cap_confidence_to_ocr(invoice: Invoice, spans: dict[str, Span | None]) -> list[str]:
    """Lower each confidence to the OCR confidence of its evidence; return changed keys."""
    capped: list[str] = []
    targets: dict[str, Extracted[object] | LineItem] = {
        **invoice.scalar_fields(),
        **{f"line_items.{i}": item for i, item in enumerate(invoice.line_items)},
    }
    for key, target in targets.items():
        span = spans.get(key)
        if span is None or span.ocr_confidence is None:
            continue
        if span.ocr_confidence < target.confidence:
            target.confidence = round(span.ocr_confidence, 2)
            capped.append(key)
    return capped


_DATE_RENDERINGS = ("%Y-%m-%d", "%d.%m.%Y", "%d/%m/%Y", "%m/%d/%Y", "%d %B %Y", "%d %b %Y",
                    "%B %d, %Y", "%b %d, %Y")  # fmt: skip


def value_renderings(value: object) -> list[str]:
    """Ways a normalized value may be printed on a document (most specific first)."""
    if isinstance(value, date):
        return [value.strftime(fmt) for fmt in _DATE_RENDERINGS]
    if isinstance(value, Decimal):
        amount = value.quantize(Decimal("0.01"))
        grouped = f"{amount:,.2f}"
        european = grouped.translate(str.maketrans(",.", ".,"))
        return list(dict.fromkeys([grouped, str(amount), european, european.replace(".", "")]))
    text = str(value).strip()
    return [text] if text else []


def quote_for_value(document: ParsedDocument, value: object) -> str | None:
    """The start of the first line that prints ``value``, up to and including the value.

    Used when a reviewer corrects a field: the model's own quote no longer supports
    the corrected value, so we look for where the correct value is printed instead.
    """
    for rendering in value_renderings(value):
        needle = rendering.lower()
        for page in document.pages:
            for line in page.text.splitlines():
                if (index := line.lower().find(needle)) != -1:
                    return " ".join(line[: index + len(rendering)].split())
    return None
