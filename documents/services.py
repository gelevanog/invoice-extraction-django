"""Glue between the framework-agnostic pipeline (``extraction``) and Django models.

``process_document`` runs the stages one by one and persists the result of each, so
the status machine in the database always reflects real progress and a crash in a
late stage leaves an inspectable record behind.
"""

from __future__ import annotations

import hashlib
import mimetypes
from collections.abc import Sequence
from decimal import Decimal
from pathlib import Path
from typing import Any

import structlog
from django.conf import settings
from django.contrib.auth.models import AbstractBaseUser
from django.core.files.base import ContentFile
from django.db import transaction
from django.utils import timezone

from documents.models import Document, ExtractionRun, Invoice, LineItem, ValidationIssue, Vendor
from extraction.enrich import EnrichmentResult
from extraction.enrich.categorize import Categorizer, KeywordCategorizer, LLMCategorizer
from extraction.enrich.fx import FrankfurterFxRatesProvider, FxRatesProvider, StaticFxRatesProvider
from extraction.enrich.vendors import VendorRecord, same_vendor
from extraction.evidence import Span
from extraction.extract import ExtractionResult
from extraction.issues import Issue
from extraction.llm import LLMProvider, build_provider
from extraction.normalize import parse_amount, parse_date
from extraction.parse import ParseError
from extraction.pipeline import InvoicePipeline, PipelineConfig
from extraction.route import RoutingConfig
from extraction.schemas import Invoice as InvoiceSchema
from extraction.status import DocumentStatus
from extraction.validate import ValidationConfig

logger = structlog.get_logger(__name__)

S = DocumentStatus
DATE_FIELDS = frozenset({"issue_date", "due_date"})
AMOUNT_FIELDS = frozenset({"subtotal", "tax", "total"})


# --- construction from settings -----------------------------------------------------
def _cfg() -> dict[str, Any]:
    return settings.DOCEXTRACT


def build_llm_provider() -> LLMProvider:
    cfg = _cfg()
    name = cfg["LLM_PROVIDER"].lower()
    model = {"anthropic": cfg["ANTHROPIC_MODEL"], "openai": cfg["OPENAI_MODEL"]}.get(name)
    return build_provider(name, model=model, timeout=cfg["LLM_TIMEOUT_SECONDS"])


def build_fx_provider() -> FxRatesProvider:
    cfg = _cfg()
    if cfg["FX_PROVIDER"] == "frankfurter":
        return FrankfurterFxRatesProvider(base_url=cfg["FX_API_URL"])
    if cfg["FX_RATES_FILE"]:
        return StaticFxRatesProvider.from_file(cfg["FX_RATES_FILE"])
    return StaticFxRatesProvider.from_file()


def build_categorizer(provider: LLMProvider) -> Categorizer:
    return (
        LLMCategorizer(provider)
        if _cfg()["LINE_ITEM_CATEGORIZER"] == "llm"
        else KeywordCategorizer()
    )


def build_pipeline(document_id: int | None = None) -> InvoicePipeline:
    cfg = _cfg()
    provider = build_llm_provider()
    return InvoicePipeline(
        provider,
        config=PipelineConfig(
            max_attempts=cfg["LLM_MAX_ATTEMPTS"],
            base_currency=cfg["BASE_CURRENCY"].upper(),
            vendor_match_threshold=cfg["VENDOR_MATCH_THRESHOLD"],
            validation=ValidationConfig(amount_tolerance=Decimal(cfg["AMOUNT_TOLERANCE"])),
            routing=RoutingConfig(
                confidence_threshold=cfg["REVIEW_CONFIDENCE_THRESHOLD"],
                review_on_warnings=cfg["REVIEW_ON_WARNINGS"],
                review_new_vendors=cfg["REVIEW_NEW_VENDORS"],
            ),
        ),
        fx_provider=build_fx_provider(),
        categorizer=build_categorizer(provider),
        vendors=vendor_records,
        duplicate_lookup=DuplicateFinder(document_id, cfg["VENDOR_MATCH_THRESHOLD"]),
        today=timezone.localdate,
    )


def vendor_records() -> list[VendorRecord]:
    return [vendor.as_record() for vendor in Vendor.objects.all()]


class DuplicateFinder:
    """Finds earlier, non-rejected invoices with the same number from the same vendor."""

    def __init__(self, document_id: int | None, threshold: float) -> None:
        self.document_id = document_id
        self.threshold = threshold

    def __call__(
        self, vendor_name: str | None, vendor_tax_id: str | None, invoice_number: str
    ) -> list[str]:
        candidates = (
            Invoice.objects.filter(invoice_number__iexact=invoice_number.strip())
            .exclude(document__status__in=[S.REJECTED, S.FAILED])
            .select_related("document", "vendor")
        )
        if self.document_id is not None:
            candidates = candidates.filter(document_id__lt=self.document_id)
        return [
            f"document #{c.document_id} {c.document.original_filename}"
            for c in candidates
            if same_vendor(
                vendor_name, vendor_tax_id, c.vendor_name, c.vendor_tax_id or None, self.threshold
            )
            or (
                c.vendor is not None
                and same_vendor(
                    vendor_name,
                    vendor_tax_id,
                    c.vendor.name,
                    c.vendor.tax_id or None,
                    self.threshold,
                )
            )
        ]


# --- intake -------------------------------------------------------------------------
def create_document(
    filename: str, data: bytes, *, user: AbstractBaseUser | None = None, content_type: str = ""
) -> Document:
    name = Path(filename).name
    document = Document(
        original_filename=name,
        content_type=content_type or mimetypes.guess_type(name)[0] or "",
        size_bytes=len(data),
        sha256=hashlib.sha256(data).hexdigest(),
        uploaded_by=user if user is not None and user.is_authenticated else None,
    )
    document.file.save(name, ContentFile(data), save=False)
    document.save()
    return document


# --- processing ---------------------------------------------------------------------
def process_document(document_id: int) -> Document:
    """Run all pipeline stages for one document, persisting after each stage."""
    document = Document.objects.get(pk=document_id)
    structlog.contextvars.bind_contextvars(document_id=document_id)
    try:
        if document.status != S.UPLOADED:
            logger.warning("pipeline.skipped", status=document.status)
            return document
        _run_stages(document)
    except Exception as exc:
        logger.exception("pipeline.crashed")
        document.refresh_from_db()
        if document.is_processing:
            _fail(document, f"Internal error: {exc}")
    finally:
        structlog.contextvars.unbind_contextvars("document_id")
    return document


def _run_stages(document: Document) -> None:
    pipeline = build_pipeline(document.pk)

    with document.file.open("rb") as handle:
        data = handle.read()
    try:
        parsed = pipeline.parse(document.original_filename, data)
    except ParseError as exc:
        _fail(document, str(exc))
        return
    document.transition_to(
        S.PARSED,
        source_type=parsed.source_type,
        source_text=parsed.text,
        pages=[{"number": p.number, "start": p.start, "end": p.end} for p in parsed.pages],
    )
    logger.info("stage.parsed", pages=len(parsed.pages), chars=len(parsed.text))

    result = pipeline.extract(parsed)
    _save_extraction_run(document, result)
    schema = result.data
    if schema is None:
        _fail(document, f"Extraction failed: {result.error}")
        return
    with transaction.atomic():
        invoice = _save_invoice(document, schema)
        document.transition_to(S.EXTRACTED)
    logger.info("stage.extracted", attempts=len(result.attempts), tokens=result.output_tokens)

    issues, spans = pipeline.validate(schema, parsed)
    with transaction.atomic():
        _save_evidence(invoice, schema, spans)
        _replace_issues(document, ValidationIssue.Stage.VALIDATION, issues)
        document.transition_to(S.VALIDATED)
    logger.info("stage.validated", issues=[i.code for i in issues])

    enrichment = pipeline.enrich(schema)
    with transaction.atomic():
        _apply_enrichment(invoice, enrichment)
        _replace_issues(document, ValidationIssue.Stage.ENRICHMENT, enrichment.issues)
        document.transition_to(S.ENRICHED)
    logger.info("stage.enriched", vendor_match=enrichment.vendor_match.method)

    decision = pipeline.route(schema, [*issues, *enrichment.issues])
    with transaction.atomic():
        invoice.min_confidence = decision.min_confidence
        invoice.save(update_fields=["min_confidence", "updated_at"])
        document.transition_to(
            S(decision.route.value),
            routing_reasons=list(decision.reasons),
            processed_at=timezone.now(),
        )
        if document.status == S.APPROVED:
            _register_vendor(invoice)
    logger.info("stage.routed", route=decision.route.value, reasons=decision.reasons)


def _fail(document: Document, error: str) -> None:
    document.transition_to(S.FAILED, error=error, processed_at=timezone.now())
    logger.warning("pipeline.failed", error=error)


def _save_extraction_run(document: Document, result: ExtractionResult[InvoiceSchema]) -> None:
    ExtractionRun.objects.create(
        document=document,
        provider=result.provider,
        model=result.model,
        succeeded=result.succeeded,
        attempts=len(result.attempts),
        attempt_log=[
            {
                "attempt": a.attempt,
                "error": a.error,
                "input_tokens": a.input_tokens,
                "output_tokens": a.output_tokens,
                "latency_ms": a.latency_ms,
            }
            for a in result.attempts
        ],
        raw_output=result.raw_output,
        input_tokens=result.input_tokens,
        output_tokens=result.output_tokens,
        latency_ms=result.latency_ms,
        error=result.error or "",
    )


def _save_invoice(document: Document, schema: InvoiceSchema) -> Invoice:
    fields = schema.scalar_fields()
    invoice = Invoice.objects.create(
        document=document,
        **{name: _db_value(name, fields[name].value) for name in Invoice.SCALAR_FIELDS},
        confidence={name: field.confidence for name, field in fields.items()},
    )
    LineItem.objects.bulk_create(
        LineItem(
            invoice=invoice,
            position=index,
            description=item.description[:500],
            quantity=item.quantity,
            unit_price=item.unit_price,
            amount=item.amount,
            confidence=item.confidence,
        )
        for index, item in enumerate(schema.line_items)
    )
    return invoice


def _db_value(name: str, value: object) -> object:
    """Nullable date/amount columns keep None; text columns store missing values as ''."""
    if value is None and name not in DATE_FIELDS | AMOUNT_FIELDS:
        return ""
    return value


def _evidence_json(quote: str, page: int | None, span: Span | None) -> dict[str, Any]:
    data: dict[str, Any] = {"quote": quote, "page": page, "found": span is not None}
    if span is not None:
        data.update(start=span.start, end=span.end, page=span.page or page)
    return data


def _save_evidence(invoice: Invoice, schema: InvoiceSchema, spans: dict[str, Span | None]) -> None:
    invoice.evidence = {
        name: _evidence_json(field.evidence.quote, field.evidence.page, spans.get(name))
        for name, field in schema.scalar_fields().items()
        if field.evidence is not None
    }
    invoice.save(update_fields=["evidence", "updated_at"])
    items = list(invoice.line_items.all())
    for item, extracted in zip(items, schema.line_items, strict=True):
        if extracted.evidence is not None:
            item.evidence = _evidence_json(
                extracted.evidence.quote,
                extracted.evidence.page,
                spans.get(f"line_items.{item.position}"),
            )
    LineItem.objects.bulk_update(items, ["evidence"])


def _replace_issues(document: Document, stage: str, issues: Sequence[Issue]) -> None:
    document.issues.filter(stage=stage).delete()
    ValidationIssue.objects.bulk_create(
        ValidationIssue(
            document=document,
            stage=stage,
            code=issue.code,
            severity=issue.severity.value,
            field=issue.field or "",
            message=issue.message,
        )
        for issue in issues
    )


def _apply_enrichment(invoice: Invoice, enrichment: EnrichmentResult) -> None:
    match = enrichment.vendor_match
    invoice.vendor = Vendor.objects.filter(pk=match.vendor.id).first() if match.vendor else None
    invoice.vendor_match = {
        "method": match.method,
        "score": match.score,
        "matched_on": match.matched_on,
    }
    conversion = enrichment.conversion
    invoice.base_currency = _cfg()["BASE_CURRENCY"].upper()
    invoice.total_base = conversion.amount if conversion else None
    invoice.fx_rate = conversion.rate if conversion else None
    invoice.fx_source = conversion.provider if conversion else ""
    invoice.save()
    if enrichment.categories:
        items = list(invoice.line_items.all())
        for item, category in zip(items, enrichment.categories, strict=True):
            item.category = category.value
        LineItem.objects.bulk_update(items, ["category"])


def _register_vendor(invoice: Invoice) -> None:
    """Update vendor master data - only ever from approved invoices."""
    if not invoice.vendor_name:
        return
    vendor = invoice.vendor
    if vendor is None:
        vendor, _ = Vendor.objects.get_or_create(
            name=invoice.vendor_name, defaults={"tax_id": invoice.vendor_tax_id}
        )
        invoice.vendor = vendor
        invoice.save(update_fields=["vendor", "updated_at"])
    changed = False
    if invoice.vendor_name != vendor.name and invoice.vendor_name not in vendor.aliases:
        vendor.aliases = [*vendor.aliases, invoice.vendor_name]
        changed = True
    if invoice.vendor_tax_id and not vendor.tax_id:
        vendor.tax_id = invoice.vendor_tax_id
        changed = True
    if changed:
        vendor.save()


# --- human review -------------------------------------------------------------------
def approve(document: Document, user: AbstractBaseUser | None, note: str = "") -> Document:
    with transaction.atomic():
        document.transition_to(S.APPROVED)
        invoice = document.invoice
        _mark_reviewed(invoice, user, note)
        _register_vendor(invoice)
    logger.info("review.approved", document_id=document.pk)
    return document


def reject(document: Document, user: AbstractBaseUser | None, note: str = "") -> Document:
    with transaction.atomic():
        document.transition_to(S.REJECTED)
        _mark_reviewed(document.invoice, user, note)
    logger.info("review.rejected", document_id=document.pk)
    return document


def _mark_reviewed(invoice: Invoice, user: AbstractBaseUser | None, note: str) -> None:
    invoice.reviewed_by = user if user is not None and user.is_authenticated else None
    invoice.reviewed_at = timezone.now()
    invoice.review_note = note
    invoice.save(update_fields=["reviewed_by", "reviewed_at", "review_note", "updated_at"])


def reset_for_reprocessing(document: Document) -> Document:
    """Drop derived data and put the document back at the start of the pipeline."""
    with transaction.atomic():
        Invoice.objects.filter(document=document).delete()
        document.issues.all().delete()
        document.transition_to(
            S.UPLOADED, error="", routing_reasons=[], source_text="", pages=[], processed_at=None
        )
    return document


class FieldValueError(ValueError):
    pass


def coerce_field_value(field: str, raw: str) -> object:
    """Parse reviewer input for a scalar invoice field."""
    if field not in Invoice.SCALAR_FIELDS:
        raise FieldValueError(f"'{field}' cannot be edited")
    text = raw.strip()
    if field in DATE_FIELDS:
        if not text:
            return None
        parsed_date = parse_date(text)
        if parsed_date is None:
            raise FieldValueError("Enter a date such as 2026-03-31 or 31.03.2026.")
        return parsed_date
    if field in AMOUNT_FIELDS:
        if not text:
            return None
        amount = parse_amount(text)
        if amount is None:
            raise FieldValueError("Enter an amount such as 1234.56 or 1.234,56.")
        return amount.quantize(Decimal("0.01"))
    if field == "currency":
        return text.upper()
    if field == "vendor_tax_id":
        return "".join(text.split()).upper()
    return text


def update_invoice_field(
    invoice: Invoice, field: str, raw_value: str, user: AbstractBaseUser | None = None
) -> Invoice:
    """Apply a reviewer correction, then re-run validation, FX and routing."""
    value = coerce_field_value(field, raw_value)
    setattr(invoice, field, value)
    invoice.confidence = {**invoice.confidence, field: 1.0}
    if field not in invoice.edited_fields:
        invoice.edited_fields = [*invoice.edited_fields, field]
    invoice.save()
    logger.info("review.field_edited", document_id=invoice.document_id, field=field)
    revalidate(invoice)
    return invoice


def revalidate(invoice: Invoice) -> None:
    """Re-run the deterministic stages on the current (possibly edited) invoice."""
    document = invoice.document
    pipeline = build_pipeline(document.pk)
    schema = invoice.to_schema()
    issues, _ = pipeline.validate(schema, document.parsed_document())
    enrichment = pipeline.enrich(schema, categorize=False)
    decision = pipeline.route(schema, [*issues, *enrichment.issues])
    with transaction.atomic():
        _apply_enrichment(invoice, enrichment)
        _replace_issues(document, ValidationIssue.Stage.VALIDATION, issues)
        _replace_issues(document, ValidationIssue.Stage.ENRICHMENT, enrichment.issues)
        invoice.min_confidence = decision.min_confidence
        invoice.save(update_fields=["min_confidence", "updated_at"])
        document.routing_reasons = list(decision.reasons)
        document.save(update_fields=["routing_reasons", "updated_at"])
