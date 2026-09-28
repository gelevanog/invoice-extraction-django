"""Render source text with ``<mark>`` elements around evidence spans."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from itertools import pairwise

from django.utils.html import escape
from django.utils.safestring import SafeString, mark_safe


@dataclass(frozen=True, slots=True)
class PageView:
    number: int
    html: SafeString


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


def highlight_pages(
    text: str, pages: list[dict], spans: Mapping[str, tuple[int, int]]
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
        views.append(PageView(page["number"], _render(text, start, end, spans)))
    return views


def _render(text: str, start: int, end: int, spans: Mapping[str, tuple[int, int]]) -> SafeString:
    local = {
        k: (max(s, start), min(e, end)) for k, (s, e) in spans.items() if s < end and e > start
    }
    cuts = sorted({start, end, *(s for s, _ in local.values()), *(e for _, e in local.values())})
    parts: list[str] = []
    for left, right in pairwise(cuts):
        chunk = escape(text[left:right])
        keys = sorted(k for k, (s, e) in local.items() if s <= left and right <= e)
        if keys:
            fields = " ".join(keys)
            parts.append(f'<mark class="evidence" data-fields="{escape(fields)}">{chunk}</mark>')
        else:
            parts.append(str(chunk))
    return mark_safe("".join(parts))  # noqa: S308 - every text chunk is escaped above
