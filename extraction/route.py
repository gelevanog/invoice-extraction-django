"""Stage 5 - decide whether a record can be auto-approved or needs a human."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum

from extraction.issues import Issue, Severity
from extraction.schemas import INVOICE_ROUTING_FIELDS, Invoice


class Route(StrEnum):
    APPROVED = "approved"
    NEEDS_REVIEW = "needs_review"


@dataclass(frozen=True, slots=True)
class RoutingConfig:
    confidence_threshold: float = 0.75
    review_on_warnings: bool = False
    review_new_vendors: bool = False


@dataclass(frozen=True, slots=True)
class RoutingDecision:
    route: Route
    reasons: tuple[str, ...]
    min_confidence: float


def min_confidence(invoice: Invoice) -> tuple[float, list[tuple[str, float]]]:
    """Lowest confidence across routing fields and line items, plus all scores."""
    scores = [(name, getattr(invoice, name).confidence) for name in INVOICE_ROUTING_FIELDS]
    scores += [(f"line_items.{i}", item.confidence) for i, item in enumerate(invoice.line_items)]
    return min(score for _, score in scores), scores


def route(
    invoice: Invoice, issues: Iterable[Issue], config: RoutingConfig | None = None
) -> RoutingDecision:
    config = config or RoutingConfig()
    issues = list(issues)
    reasons: list[str] = []

    errors = [i.code for i in issues if i.severity is Severity.ERROR]
    if errors:
        reasons.append(f"{len(errors)} error-level issue(s): {', '.join(sorted(set(errors)))}")

    if config.review_on_warnings:
        warnings = [i.code for i in issues if i.severity is Severity.WARNING]
        if warnings:
            reasons.append(f"warnings present: {', '.join(sorted(set(warnings)))}")

    if config.review_new_vendors and any(i.code == "new_vendor" for i in issues):
        reasons.append("first invoice from an unknown vendor")

    lowest, scores = min_confidence(invoice)
    low = [f"{name}={score:.2f}" for name, score in scores if score < config.confidence_threshold]
    if low:
        reasons.append(f"confidence below {config.confidence_threshold:.2f}: {', '.join(low)}")

    return RoutingDecision(
        route=Route.NEEDS_REVIEW if reasons else Route.APPROVED,
        reasons=tuple(reasons),
        min_confidence=lowest,
    )
