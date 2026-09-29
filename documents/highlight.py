"""Render source text with ``<mark>`` elements around evidence spans.

For OCR text, words recognised with low confidence are additionally wrapped in
``<span class="ocr-low">`` so reviewers see exactly which characters are uncertain.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from itertools import pairwise
from typing import Any

from django.utils.html import escape
from django.utils.safestring import SafeString, mark_safe


@dataclass(frozen=True, slots=True)
class PageView:
    number: int
    html: SafeString
    ocr_confidence: float | None = None


def evidence_spans(
    invoice_evidence: Mapping[str, dict], line_items: list[dict | None]
) -> dict[str, tuple[int, int]]:
    """Collect resolved (start, end) offsets keyed by field name / ``line_items.<i>``."""
    spans: dict[str, tuple[int, int]] = {}
    for key, data in invoice_evidence.items():
        if data and data.get("found"):
            spans[key] = (data["start"], data["end"])
    for index, data in enumerate(line_items):
        if data and data.get("found"):
            spans[f"line_items.{index}"] = (data["start"], data["end"])
    return spans


@dataclass(frozen=True, slots=True)
class UncertainWord:
    start: int  # document offsets
    end: int
    confidence: float


def uncertain_words(pages: list[dict[str, Any]], threshold: float) -> list[UncertainWord]:
    """OCR words (from ``Document.pages``) recognised with confidence below ``threshold``."""
    return [
        UncertainWord(page["start"] + start, page["start"] + end, confidence)
        for page in pages
        for start, end, confidence, *_ in page.get("words", ())
        if confidence < threshold
    ]


def highlight_pages(
    text: str,
    pages: list[dict[str, Any]],
    spans: Mapping[str, tuple[int, int]],
    uncertain: Sequence[UncertainWord] = (),
) -> list[PageView]:
    """Split text into pages; wrap every region covered by spans in a ``<mark>``.

    Overlapping spans (e.g. currency and total quoting the same line) are handled by
    cutting the text at every span boundary and listing all covering fields on the mark.
    """
    if not pages:
        pages = [{"number": 1, "start": 0, "end": len(text)}]
    views = []
    for page in pages:
        start, end = page["start"], page["end"]
        words = [w for w in uncertain if w.start < end and w.end > start]
        html = _render(text, start, end, spans, words)
        views.append(PageView(page["number"], html, page.get("ocr_confidence")))
    return views


def _render(
    text: str,
    start: int,
    end: int,
    spans: Mapping[str, tuple[int, int]],
    uncertain: Sequence[UncertainWord],
) -> SafeString:
    local = {
        k: (max(s, start), min(e, end)) for k, (s, e) in spans.items() if s < end and e > start
    }
    boundaries = [*local.values(), *((w.start, w.end) for w in uncertain)]
    cuts = sorted({start, end, *(s for s, _ in boundaries), *(e for _, e in boundaries)})
    parts: list[str] = []
    for left, right in pairwise(cuts):
        chunk = str(escape(text[left:right]))
        word = next((w for w in uncertain if w.start <= left and right <= w.end), None)
        if word is not None:
            title = f"OCR confidence {word.confidence:.2f}"
            chunk = f'<span class="ocr-low" title="{title}">{chunk}</span>'
        keys = sorted(k for k, (s, e) in local.items() if s <= left and right <= e)
        if keys:
            fields = " ".join(keys)
            chunk = f'<mark class="evidence" data-fields="{escape(fields)}">{chunk}</mark>'
        parts.append(chunk)
    return mark_safe("".join(parts))  # noqa: S308 - every text chunk is escaped above
