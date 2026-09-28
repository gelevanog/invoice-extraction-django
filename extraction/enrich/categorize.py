"""Assign expense categories to line items (LLM with keyword fallback)."""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from enum import StrEnum
from typing import Protocol

import structlog
from pydantic import BaseModel, ConfigDict, ValidationError

from extraction.llm.base import ChatMessage, JsonRequest, LLMError, LLMProvider

logger = structlog.get_logger(__name__)


class ExpenseCategory(StrEnum):
    SOFTWARE = "software"
    HARDWARE = "hardware"
    OFFICE_SUPPLIES = "office_supplies"
    PROFESSIONAL_SERVICES = "professional_services"
    LOGISTICS = "logistics"
    TRAVEL = "travel"
    UTILITIES = "utilities"
    MARKETING = "marketing"
    OTHER = "other"


class Categorizer(Protocol):
    def categorize(self, descriptions: Sequence[str]) -> list[ExpenseCategory]: ...


DEFAULT_KEYWORDS: dict[ExpenseCategory, tuple[str, ...]] = {
    ExpenseCategory.SOFTWARE: (
        "subscription", "licen", "saas", "software", "cloud", "hosting", "plan", "api",
        "support add-on",
    ),
    ExpenseCategory.HARDWARE: (
        "monitor", "laptop", "docking", "cable", "keyboard", "mouse", "server", "hdmi", "usb",
    ),
    ExpenseCategory.OFFICE_SUPPLIES: (
        "paper", "toner", "ink", "stationery", "pens", "markers", "folders", "envelopes", "stapler",
    ),
    ExpenseCategory.PROFESSIONAL_SERVICES: (
        "consult", "workshop", "advisory", "audit", "legal", "report", "onboarding", "training",
        "strategy",
    ),
    ExpenseCategory.LOGISTICS: (
        "freight", "shipping", "pallet", "transport", "courier", "customs", "delivery", "surcharge",
    ),
    ExpenseCategory.TRAVEL: ("flight", "hotel", "taxi", "train", "mileage", "per diem"),
    ExpenseCategory.UTILITIES: ("electricity", "water", "internet", "phone", "gas supply"),
    ExpenseCategory.MARKETING: ("advert", "campaign", "marketing", "sponsor", "print ads"),
}  # fmt: skip


class KeywordCategorizer:
    """Deterministic word-prefix matcher; the first category with a keyword hit wins."""

    def __init__(self, keywords: dict[ExpenseCategory, tuple[str, ...]] | None = None) -> None:
        source = keywords or DEFAULT_KEYWORDS
        self._patterns = [
            (category, re.compile(r"\b(?:" + "|".join(map(re.escape, words)) + ")", re.IGNORECASE))
            for category, words in source.items()
        ]

    def categorize(self, descriptions: Sequence[str]) -> list[ExpenseCategory]:
        return [self._one(description) for description in descriptions]

    def _one(self, description: str) -> ExpenseCategory:
        for category, pattern in self._patterns:
            if pattern.search(description):
                return category
        return ExpenseCategory.OTHER


class CategoryAssignment(BaseModel):
    model_config = ConfigDict(extra="forbid")

    index: int
    category: ExpenseCategory


class LineItemCategories(BaseModel):
    """LLM response schema for line-item categorization."""

    model_config = ConfigDict(extra="forbid")

    assignments: list[CategoryAssignment]


CATEGORIZE_SYSTEM_PROMPT = (
    "You classify invoice line items into expense categories for bookkeeping. "
    "Return exactly one assignment per item, using the item's index."
)


def render_items(descriptions: Sequence[str]) -> str:
    items = [{"index": i, "description": d} for i, d in enumerate(descriptions)]
    return f"<items>\n{json.dumps(items, ensure_ascii=False)}\n</items>"


class LLMCategorizer:
    """Single LLM call per invoice; falls back to keywords on any failure."""

    def __init__(self, provider: LLMProvider, fallback: Categorizer | None = None) -> None:
        self._provider = provider
        self._fallback = fallback or KeywordCategorizer()

    def categorize(self, descriptions: Sequence[str]) -> list[ExpenseCategory]:
        if not descriptions:
            return []
        categories = ", ".join(c.value for c in ExpenseCategory)
        prompt = f"Allowed categories: {categories}.\n\n{render_items(descriptions)}"
        try:
            response = self._provider.complete_json(
                JsonRequest(
                    system=CATEGORIZE_SYSTEM_PROMPT,
                    messages=[ChatMessage("user", prompt)],
                    schema=LineItemCategories,
                    max_tokens=2000,
                )
            )
            parsed = LineItemCategories.model_validate_json(response.text)
            by_index = {a.index: a.category for a in parsed.assignments}
            if set(by_index) != set(range(len(descriptions))):
                raise ValueError("assignment indexes do not cover every line item")
            return [by_index[i] for i in range(len(descriptions))]
        except (LLMError, ValidationError, ValueError) as exc:
            logger.warning("categorize.fallback", error=str(exc))
            return self._fallback.categorize(descriptions)
