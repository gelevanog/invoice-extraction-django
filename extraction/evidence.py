"""Resolve LLM-provided evidence quotes to exact character spans in the source text.

LLMs are good at quoting but bad at counting characters, so the model returns a quote
and we find it ourselves (whitespace- and case-insensitive). A quote that cannot be
found is a signal the value may be hallucinated; validation reports it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from extraction.parse import ParsedDocument
from extraction.schemas import Evidence, Invoice


@dataclass(frozen=True, slots=True)
class Span:
    start: int
    end: int
    page: int | None

    def as_dict(self) -> dict[str, int | None]:
        return {"start": self.start, "end": self.end, "page": self.page}


def locate(evidence: Evidence, document: ParsedDocument) -> Span | None:
    tokens = evidence.quote.split()
    if not tokens:
        return None
    pattern = re.compile(r"\s+".join(re.escape(token) for token in tokens), re.IGNORECASE)

    # Prefer the page the model cited, then fall back to the whole document.
    hinted = next((p for p in document.pages if p.number == evidence.page), None)
    if hinted is not None and (match := pattern.search(hinted.text)):
        return Span(hinted.start + match.start(), hinted.start + match.end(), hinted.number)
    if match := pattern.search(document.text):
        return Span(match.start(), match.end(), document.page_for_offset(match.start()))
    return None


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
