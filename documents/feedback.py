"""Learning from reviewer corrections.

When a person approves an invoice, every scalar field is stored as a
:class:`~documents.models.FieldReview`: what the model extracted, what the reviewer
approved and where that value is printed. Those rows

* become per-vendor few-shot examples for later extractions (:func:`examples_for`), and
* measure extraction accuracy over time (:mod:`documents.accuracy`).

Only human-approved invoices count: auto-approved ones were never checked, and rejected
ones are not trustworthy. Examples are chosen for the vendor recognised in the new
document's raw text, corrected documents first, then the most recent.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any

import structlog
from django.conf import settings
from django.db import transaction
from django.db.models import Case, Count, IntegerField, Max, Q, Value, When

from documents.models import Document, FieldReview, Invoice, Vendor
from extraction.enrich.vendors import VendorMatcher
from extraction.evidence import quote_for_value
from extraction.extract import FewShotExample, parse_model_output
from extraction.parse import ParsedDocument
from extraction.schemas import Invoice as InvoiceSchema
from extraction.status import DocumentStatus

logger = structlog.get_logger(__name__)

MAX_QUOTE_CHARS = 160


def field_text(value: object) -> str:
    """Canonical text of a field value, so extracted and approved values compare exactly."""
    if value is None:
        return ""
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value.quantize(Decimal("0.01")))
    return str(value).strip()


def extracted_fields(document: Document) -> dict[str, tuple[str, float]] | None:
    """Field -> (value text, confidence) as the model returned it, from the audit trail."""
    run = document.extraction_runs.filter(succeeded=True).first()
    if run is None:
        return None
    schema = parse_model_output(InvoiceSchema, run.raw_output)
    return {
        name: (field_text(field.value), field.confidence)
        for name, field in schema.scalar_fields().items()
    }


def record_review(invoice: Invoice) -> list[FieldReview]:
    """Store extracted-vs-approved values for a human-approved invoice (idempotent)."""
    document = invoice.document
    extracted = extracted_fields(document)
    if extracted is None or invoice.reviewed_at is None:
        return []
    parsed = document.parsed_document()
    rows = []
    for name in Invoice.SCALAR_FIELDS:
        approved = getattr(invoice, name)
        approved_text = field_text(approved)
        extracted_text, confidence = extracted.get(name, ("", 0.0))
        corrected = approved_text != extracted_text
        rows.append(
            FieldReview(
                document=document,
                vendor=invoice.vendor,
                vendor_name=invoice.display_vendor,
                field=name,
                extracted_value=extracted_text,
                approved_value=approved_text,
                corrected=corrected,
                extracted_confidence=confidence,
                evidence_quote=_evidence_quote(invoice, name, approved, corrected, parsed),
                reviewed_by=invoice.reviewed_by,
                reviewed_at=invoice.reviewed_at,
            )
        )
    with transaction.atomic():
        FieldReview.objects.filter(document=document).delete()
        FieldReview.objects.bulk_create(rows)
    logger.info(
        "feedback.recorded",
        document_id=document.pk,
        corrected=[r.field for r in rows if r.corrected],
    )
    return rows


def _evidence_quote(
    invoice: Invoice, name: str, approved: object, corrected: bool, parsed: ParsedDocument
) -> str:
    """Where the approved value is printed: the model's quote if it was right, else search."""
    if approved is None or approved == "":
        return ""
    evidence = invoice.evidence.get(name) or {}
    if not corrected and evidence.get("found"):
        quote = str(evidence["quote"])
    else:
        quote = quote_for_value(parsed, approved) or ""
    return quote[:MAX_QUOTE_CHARS]


def rebuild_reviews() -> int:
    """Recompute every FieldReview from the invoices people approved; return the count."""
    invoices = Invoice.objects.filter(
        document__status=DocumentStatus.APPROVED, reviewed_at__isnull=False
    ).select_related("document", "vendor", "reviewed_by")
    with transaction.atomic():
        FieldReview.objects.all().delete()
        return sum(len(record_review(invoice)) for invoice in invoices)


def examples_for(
    parsed: ParsedDocument, *, exclude_document_id: int | None = None
) -> list[FewShotExample]:
    """Verified records from earlier documents of the vendor recognised in ``parsed``.

    Corrected documents come first (they carry the most information), then the most
    recently reviewed; at most ``FEWSHOT_MAX_EXAMPLES``. The character budget is
    enforced when the prompt is rendered.
    """
    limit = settings.DOCEXTRACT["FEWSHOT_MAX_EXAMPLES"]
    if limit <= 0:
        return []
    matcher = VendorMatcher(vendor.as_record() for vendor in Vendor.objects.all())
    record = matcher.find_in_text(parsed.text)
    if record is None:
        return []
    documents = (
        FieldReview.objects.filter(vendor_id=record.id)
        .exclude(document_id=exclude_document_id)
        .values("document_id")
        .annotate(
            corrections=Count("id", filter=Q(corrected=True)),
            reviewed=Max("reviewed_at"),
            has_corrections=Case(
                When(corrections__gt=0, then=Value(1)),
                default=Value(0),
                output_field=IntegerField(),
            ),
        )
        .order_by("-has_corrections", "-reviewed", "-document_id")[:limit]
    )
    document_ids = [row["document_id"] for row in documents]
    reviews = FieldReview.objects.filter(document_id__in=document_ids).order_by("id")
    by_document: dict[int, dict[str, Any]] = {doc_id: {} for doc_id in document_ids}
    for review in reviews:
        if not review.approved_value and not review.corrected:
            continue
        entry: dict[str, Any] = {"value": review.approved_value or None}
        if review.evidence_quote:
            entry["evidence"] = review.evidence_quote
        if review.corrected:
            entry["reviewer_corrected_from"] = review.extracted_value or None
        by_document[review.document_id][review.field] = entry
    examples = [
        FewShotExample(f"document #{doc_id}", fields)
        for doc_id, fields in by_document.items()
        if fields
    ]
    logger.info("feedback.examples", vendor=record.name, sources=[e.source for e in examples])
    return examples
