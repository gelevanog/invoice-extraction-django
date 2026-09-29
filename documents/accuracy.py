"""Extraction accuracy from reviewer decisions: share of fields accepted unchanged.

Computed from :class:`~documents.models.FieldReview` rows, i.e. only invoices a person
approved. Auto-approved invoices are reported separately (straight-through count):
nobody checked them, so they are no evidence of accuracy either way.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from django.db.models import Count, Max, Q, QuerySet

from documents.models import Document, FieldReview, Invoice
from extraction.status import DocumentStatus


@dataclass(frozen=True, slots=True)
class Accuracy:
    key: str
    reviewed: int  # fields reviewed
    corrected: int

    @property
    def accepted(self) -> int:
        return self.reviewed - self.corrected

    @property
    def rate(self) -> float | None:
        return self.accepted / self.reviewed if self.reviewed else None


@dataclass(frozen=True, slots=True)
class TimelinePoint:
    document_id: int
    filename: str
    vendor: str
    reviewed_at: datetime
    reviewed: int
    corrected: int
    cumulative_rate: float  # accuracy over all reviews up to and including this one

    @property
    def rate(self) -> float:
        return (self.reviewed - self.corrected) / self.reviewed


@dataclass(slots=True)
class AccuracyReport:
    overall: Accuracy
    invoices_reviewed: int
    auto_approved: int
    by_field: list[Accuracy] = field(default_factory=list)
    by_vendor: list[Accuracy] = field(default_factory=list)
    timeline: list[TimelinePoint] = field(default_factory=list)


def _accuracy(rows: QuerySet[FieldReview], key: str) -> list[Accuracy]:
    grouped = rows.values(key).annotate(
        reviewed=Count("id"), corrected=Count("id", filter=Q(corrected=True))
    )
    return [Accuracy(row[key] or "-", row["reviewed"], row["corrected"]) for row in grouped]


def build_report(vendor: str | None = None, timeline_limit: int = 40) -> AccuracyReport:
    """Aggregate FieldReview rows (optionally for one vendor name, case-insensitive)."""
    rows = FieldReview.objects.all()
    if vendor:
        rows = rows.filter(vendor_name__icontains=vendor)

    totals = rows.aggregate(reviewed=Count("id"), corrected=Count("id", filter=Q(corrected=True)))
    order = {name: i for i, name in enumerate(Invoice.SCALAR_FIELDS)}
    by_field = sorted(_accuracy(rows, "field"), key=lambda a: order.get(a.key, len(order)))
    by_vendor = sorted(_accuracy(rows, "vendor_name"), key=lambda a: (-a.reviewed, a.key))

    per_document = (
        rows.values("document_id", "document__original_filename", "vendor_name")
        .annotate(
            reviewed=Count("id"),
            corrected=Count("id", filter=Q(corrected=True)),
            at=Max("reviewed_at"),
        )
        .order_by("at", "document_id")
    )
    timeline: list[TimelinePoint] = []
    seen = corrected = 0
    for row in per_document:
        seen += row["reviewed"]
        corrected += row["corrected"]
        cumulative = (seen - corrected) / seen
        timeline.append(
            TimelinePoint(
                document_id=row["document_id"],
                filename=row["document__original_filename"],
                vendor=row["vendor_name"] or "-",
                reviewed_at=row["at"],
                reviewed=row["reviewed"],
                corrected=row["corrected"],
                cumulative_rate=cumulative,
            )
        )

    auto_approved = Document.objects.filter(
        status=DocumentStatus.APPROVED, invoice__reviewed_at__isnull=True
    )
    if vendor:
        auto_approved = auto_approved.filter(invoice__vendor_name__icontains=vendor)
    return AccuracyReport(
        overall=Accuracy("all", totals["reviewed"], totals["corrected"]),
        invoices_reviewed=len(timeline),
        auto_approved=auto_approved.count(),
        by_field=by_field,
        by_vendor=by_vendor,
        timeline=timeline[-timeline_limit:],
    )
