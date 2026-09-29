"""Deterministic, offline stand-in for an LLM.

``FakeProvider`` answers the same :class:`JsonRequest` a real model would get, using
regexes and layout heuristics instead of a neural network. It lets the whole pipeline,
the test suite and the Docker demo run with zero API keys and fully reproducible
output. It understands two schemas: :class:`~extraction.schemas.Invoice` and
:class:`~extraction.enrich.categorize.LineItemCategories`.

Confidence scores mimic what a calibrated model would report: high for explicitly
labeled values, lower for values inferred from position or unlabeled references.

Few-shot examples are honoured deterministically: from each verified example value and
its evidence quote the reader learns the label printed before the value (e.g.
"Document no." or "Tax point") and reads the value after the same label in the new
document - roughly what a real model takes away from a verified example. A learned
label fills fields the heuristics missed or were unsure about; for fields a reviewer
had to correct it also overrides a confident heuristic reading.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any

from extraction.currencies import CURRENCY_SYMBOLS, ISO_4217_CODES
from extraction.enrich.categorize import KeywordCategorizer, LineItemCategories
from extraction.evidence import value_renderings
from extraction.llm.base import JsonRequest, LLMError, LLMResponse
from extraction.normalize import parse_amount, parse_date
from extraction.schemas import Invoice

FAKE_MODEL = "fake-heuristic-v1"

_PAGE_RE = re.compile(r'<page number="(\d+)">\n(.*?)\n</page>', re.DOTALL)
_ITEMS_RE = re.compile(r"<items>\n(.*?)\n</items>", re.DOTALL)
_EXAMPLE_RE = re.compile(r'<example source="[^"]*">\n(.*?)\n</example>', re.DOTALL)
LEARNED_CONFIDENCE = 0.9
DATE_FIELDS = frozenset({"issue_date", "due_date"})
AMOUNT_FIELDS = frozenset({"subtotal", "tax", "total"})
_AMOUNT = r"-?[\d][\d.,]*"
_LEGAL_FORM = re.compile(
    r"\b(GmbH|AG|Ltd\.?|Limited|LLC|Inc\.?|Corp\.?|B\.V\.|BV|N\.V\.|S\.A\.|SAS|S\.r\.l\.|plc)\b",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class _Line:
    page: int
    text: str


@dataclass(frozen=True, slots=True)
class _Hit:
    value: str
    line: _Line
    confidence: float
    quote: str | None = None  # defaults to the whole line


class FakeProvider:
    name = "fake"

    def __init__(self, model: str = FAKE_MODEL) -> None:
        self.model = model
        self._categorizer = KeywordCategorizer()

    def complete_json(self, request: JsonRequest) -> LLMResponse:
        prompt = request.messages[0].content
        if request.schema is Invoice:
            reader = HeuristicInvoiceReader(_pages_from_prompt(prompt), learned_labels(prompt))
            payload = reader.read()
        elif request.schema is LineItemCategories:
            payload = self._categorize(prompt)
        else:
            raise LLMError(f"FakeProvider does not support schema {request.schema.__name__}")
        text = json.dumps(payload, ensure_ascii=False)
        prompt_chars = len(request.system) + sum(len(m.content) for m in request.messages)
        return LLMResponse(
            text=text,
            model=self.model,
            input_tokens=prompt_chars // 4,
            output_tokens=len(text) // 4,
        )

    def _categorize(self, prompt: str) -> dict[str, Any]:
        match = _ITEMS_RE.search(prompt)
        items = json.loads(match.group(1)) if match else []
        categories = self._categorizer.categorize([item["description"] for item in items])
        return {
            "assignments": [
                {"index": item["index"], "category": category.value}
                for item, category in zip(items, categories, strict=True)
            ]
        }


def _pages_from_prompt(prompt: str) -> list[tuple[int, str]]:
    pages = [(int(num), text) for num, text in _PAGE_RE.findall(prompt)]
    if not pages:
        raise LLMError("FakeProvider could not find a <document> block in the prompt")
    return pages


@dataclass(frozen=True, slots=True)
class LearnedLabel:
    label: str  # text printed before the value, e.g. "Document no."
    corrected: bool  # a reviewer had to fix this field on the example document


def learned_labels(prompt: str) -> dict[str, LearnedLabel]:
    """Field -> label printed before its value, from the verified examples in a prompt."""
    labels: dict[str, LearnedLabel] = {}
    for match in _EXAMPLE_RE.finditer(prompt):
        record = json.loads(match.group(1))
        for name, entry in record.items():
            if name in labels or not isinstance(entry, dict):
                continue
            if label := _label_before_value(entry.get("evidence"), entry.get("value")):
                labels[name] = LearnedLabel(label, "reviewer_corrected_from" in entry)
    return labels


def _label_before_value(quote: object, value: object) -> str | None:
    if not isinstance(quote, str) or value is None:
        return None
    for rendering in value_renderings(_typed(str(value))):
        index = quote.lower().find(rendering.lower())
        if index > 0:
            return quote[:index].strip(" :#") or None
    return None


def _typed(text: str) -> object:
    """Examples carry values as text; recover dates and amounts to render them."""
    try:
        return date.fromisoformat(text)
    except ValueError:
        pass
    if re.fullmatch(r"-?\d+\.\d{2}", text):
        try:
            return Decimal(text)
        except InvalidOperation:
            pass
    return text


class HeuristicInvoiceReader:
    """Regex/label based invoice reader producing ``Invoice``-shaped JSON."""

    def __init__(
        self, pages: list[tuple[int, str]], learned: dict[str, LearnedLabel] | None = None
    ) -> None:
        self.lines = [_Line(number, line) for number, text in pages for line in text.splitlines()]
        self.learned = learned or {}

    # -- public -------------------------------------------------------------------
    def read(self) -> dict[str, Any]:
        hits = {
            "vendor_name": self._vendor_name(),
            "vendor_tax_id": self._tax_id(),
            "invoice_number": self._invoice_number(),
            "issue_date": self._issue_date(),
            "due_date": self._labeled(r"(?:due\s+date|payment\s+due|due\s+by|pay\s+by)", 0.95),
            "currency": self._currency(),
            "subtotal": self._amount_line(
                r"(?:sub\s*-?total|net\s+amount|net\s+total|total\s+net)"
            ),
            "tax": self._amount_line(r"(?:VAT|tax|sales\s+tax|GST)(?!\s*(?:ID|No|number|reg))"),
            "total": self._amount_line(
                r"(?:total\s+due|total\s+amount|amount\s+due|grand\s+total|total)(?!\s+net)"
            ),
        }
        for name, learned in self.learned.items():
            if name not in hits:
                continue
            current = hits[name]
            confident = current is not None and current.confidence >= LEARNED_CONFIDENCE
            if confident and not learned.corrected:
                continue  # heuristics agree with what reviewers accepted before
            pattern = r"\s+".join(re.escape(token) for token in learned.label.split())
            hits[name] = self._labeled(pattern, LEARNED_CONFIDENCE) or current

        fields: dict[str, Any] = {}
        for name, hit in hits.items():
            if name in DATE_FIELDS:
                fields[name] = self._date_field(hit)
            elif name in AMOUNT_FIELDS:
                fields[name] = self._amount_field(hit)
            else:
                fields[name] = self._field(hit)
        return {**fields, "line_items": self._line_items()}

    # -- field finders ------------------------------------------------------------
    def _labeled(self, label: str, confidence: float, value: str = r"(.+?)") -> _Hit | None:
        pattern = re.compile(rf"^\s*{label}\s*[:#]?\s*{value}\s*(?:\s{{2,}}.*)?$", re.IGNORECASE)
        for line in self.lines:
            if match := pattern.match(line.text):
                quote = line.text[: match.end(1)].strip()  # label + value, not other columns
                return _Hit(match.group(1).strip(), line, confidence, quote)
        return None

    def _vendor_name(self) -> _Hit | None:
        labeled = self._labeled(r"(?:supplier|vendor|seller)", 0.92)
        if labeled:
            return labeled
        header = [line for line in self.lines if line.text.strip()][:3]
        for line in header:
            name = re.split(r"\s{2,}", line.text.strip())[0]
            if _LEGAL_FORM.search(name):
                return _Hit(name, line, 0.9, name)
        sender = self._labeled(r"from(?=\s*:)", 0.75)
        if sender:
            display = re.sub(r"\s*<[^>]+>", "", sender.value).strip().strip('"')
            return _Hit(display, sender.line, sender.confidence, display) if display else None
        if header:
            name = re.split(r"\s{2,}", header[0].text.strip())[0]
            return _Hit(name, header[0], 0.6, name)
        return None

    def _tax_id(self) -> _Hit | None:
        return self._labeled(
            r"(?:VAT\s*(?:ID|No\.?|number|Reg\.?\s*No\.?)|Tax\s*ID|EIN|USt-IdNr\.?)",
            0.95,
            r"([A-Z]{0,2}[\dA-Z][\dA-Z \-]{5,18}[\dA-Z])",
        )

    def _invoice_number(self) -> _Hit | None:
        labeled = self._labeled(
            r"(?:invoice\s*(?:no\.?|number|#)|inv\.?\s*no\.?)", 0.95, r"([A-Z0-9][A-Z0-9\-/]+)"
        )
        if labeled:
            return labeled
        # Unlabeled references ("Ref PH-88213") are plausible but uncertain.
        return self._labeled(r"(?:ref(?:erence)?\.?)", 0.55, r"([A-Z]{1,5}-?\d{3,})")

    def _issue_date(self) -> _Hit | None:
        return self._labeled(
            r"(?:invoice\s+date|date\s+of\s+issue|issue\s+date)", 0.95
        ) or self._labeled(r"date(?=\s*:)", 0.85)

    def _currency(self) -> _Hit | None:
        codes = "|".join(sorted(ISO_4217_CODES))
        code_re = re.compile(rf"\b({codes})\b")
        total_line = self._amount_line(r"(?:total\s+due|total\s+amount|amount\s+due|total)")
        candidates = [total_line.line] if total_line else []
        for line in [*candidates, *self.lines]:
            if match := code_re.search(line.text):
                return _Hit(match.group(1), line, 0.95)
        for line in self.lines:
            for symbol, code in CURRENCY_SYMBOLS.items():
                if symbol in line.text:
                    return _Hit(code, line, 0.8)
        return None

    def _amount_line(self, label: str) -> _Hit | None:
        """Last number on the first line that starts with ``label``."""
        pattern = re.compile(rf"^\s*{label}\b", re.IGNORECASE)
        for line in self.lines:
            if pattern.match(line.text):
                numbers = re.findall(_AMOUNT, line.text)
                if numbers:
                    return _Hit(numbers[-1], line, 0.95)
        return None

    def _line_items(self) -> list[dict[str, Any]]:
        row = re.compile(
            rf"^\s*(?P<desc>\S.*?\S)\s{{2,}}(?P<qty>\d+(?:[.,]\d+)?)\s{{2,}}"
            rf"(?P<unit>{_AMOUNT})\s{{2,}}(?P<amount>{_AMOUNT})\s*$"
        )
        items = []
        for line in self.lines:
            if not (match := row.match(line.text)):
                continue
            amount = parse_amount(match["amount"])
            if amount is None:
                continue
            items.append(
                {
                    "description": match["desc"],
                    "quantity": _num(parse_amount(match["qty"])),
                    "unit_price": _num(parse_amount(match["unit"])),
                    "amount": _num(amount),
                    "confidence": 0.9,
                    "evidence": _evidence(line),
                }
            )
        return items

    # -- JSON shaping -------------------------------------------------------------
    @staticmethod
    def _field(hit: _Hit | None) -> dict[str, Any]:
        if hit is None:
            return {"value": None, "confidence": 0.9, "evidence": None}
        return {
            "value": hit.value,
            "confidence": hit.confidence,
            "evidence": _evidence(hit.line, hit.quote),
        }

    def _date_field(self, hit: _Hit | None) -> dict[str, Any]:
        parsed = parse_date(hit.value) if hit else None
        if hit is None or parsed is None:
            return self._field(None)
        return {**self._field(hit), "value": parsed.isoformat()}

    def _amount_field(self, hit: _Hit | None) -> dict[str, Any]:
        parsed = parse_amount(hit.value) if hit else None
        if hit is None or parsed is None:
            return self._field(None)
        return {**self._field(hit), "value": _num(parsed)}


def _evidence(line: _Line, quote: str | None = None) -> dict[str, Any]:
    return {"quote": " ".join((quote or line.text).split()), "page": line.page}


def _num(value: Decimal | None) -> str | None:
    return None if value is None else str(value)
