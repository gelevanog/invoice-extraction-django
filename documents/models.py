"""Persistence for documents moving through the pipeline."""

from __future__ import annotations

from typing import Any

from django.conf import settings
from django.db import models
from django.urls import reverse

from extraction.enrich.categorize import ExpenseCategory
from extraction.enrich.vendors import VendorRecord
from extraction.issues import Severity
from extraction.parse import Page, ParsedDocument
from extraction.schemas import Invoice as InvoiceSchema
from extraction.status import IN_PROGRESS, DocumentStatus, ensure_transition


class Vendor(models.Model):
    """Canonical supplier record ("vendor master data")."""

    name = models.CharField(max_length=255, unique=True)
    tax_id = models.CharField(max_length=32, blank=True, db_index=True)
    aliases = models.JSONField(
        default=list, blank=True, help_text="Other spellings seen on documents."
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["name"]

    def __str__(self) -> str:
        return self.name

    def as_record(self) -> VendorRecord:
        return VendorRecord(self.pk, self.name, self.tax_id or None, tuple(self.aliases))


class Document(models.Model):
    class Status(models.TextChoices):
        UPLOADED = DocumentStatus.UPLOADED, "Uploaded"
        PARSED = DocumentStatus.PARSED, "Parsed"
        EXTRACTED = DocumentStatus.EXTRACTED, "Extracted"
        VALIDATED = DocumentStatus.VALIDATED, "Validated"
        ENRICHED = DocumentStatus.ENRICHED, "Enriched"
        NEEDS_REVIEW = DocumentStatus.NEEDS_REVIEW, "Needs review"
        APPROVED = DocumentStatus.APPROVED, "Approved"
        REJECTED = DocumentStatus.REJECTED, "Rejected"
        FAILED = DocumentStatus.FAILED, "Failed"

    file = models.FileField(upload_to="documents/%Y/%m/")
    original_filename = models.CharField(max_length=255)
    content_type = models.CharField(max_length=100, blank=True)
    size_bytes = models.PositiveIntegerField(default=0)
    sha256 = models.CharField(max_length=64, db_index=True)
    status = models.CharField(
        max_length=20, choices=Status.choices, default=Status.UPLOADED, db_index=True
    )
    source_type = models.CharField(max_length=20, blank=True)
    source_text = models.TextField(blank=True)
    pages = models.JSONField(default=list, blank=True)
    error = models.TextField(blank=True)
    routing_reasons = models.JSONField(default=list, blank=True)
    uploaded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    processed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at", "-id"]

    def __str__(self) -> str:
        return f"#{self.pk} {self.original_filename}"

    def get_absolute_url(self) -> str:
        return reverse("documents:detail", args=[self.pk])

    @property
    def status_enum(self) -> DocumentStatus:
        return DocumentStatus(self.status)

    @property
    def is_processing(self) -> bool:
        return self.status_enum in IN_PROGRESS

    def transition_to(self, target: DocumentStatus, **fields: Any) -> None:
        """Move along the status machine, persisting ``status`` plus any extra fields."""
        ensure_transition(self.status_enum, target)
        self.status = target.value
        for name, value in fields.items():
            setattr(self, name, value)
        self.save(update_fields=["status", "updated_at", *fields])

    def parsed_document(self) -> ParsedDocument:
        pages = tuple(
            Page(p["number"], self.source_text[p["start"] : p["end"]], p["start"], p["end"])
            for p in self.pages
        )
        return ParsedDocument(pages=pages, source_type=self.source_type)


class ExtractionRun(models.Model):
    """One LLM extraction call sequence (including retries) for audit and cost tracking."""

    document = models.ForeignKey(Document, on_delete=models.CASCADE, related_name="extraction_runs")
    provider = models.CharField(max_length=30)
    model = models.CharField(max_length=100)
    succeeded = models.BooleanField(default=False)
    attempts = models.PositiveSmallIntegerField(default=0)
    attempt_log = models.JSONField(default=list, blank=True)
    raw_output = models.TextField(blank=True)
    input_tokens = models.PositiveIntegerField(default=0)
    output_tokens = models.PositiveIntegerField(default=0)
    latency_ms = models.PositiveIntegerField(default=0)
    error = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at", "-id"]

    def __str__(self) -> str:
        outcome = "ok" if self.succeeded else "failed"
        return f"{self.provider}/{self.model} ({outcome}, {self.attempts} attempt(s))"


class Invoice(models.Model):
    SCALAR_FIELDS = (
        "vendor_name",
        "vendor_tax_id",
        "invoice_number",
        "issue_date",
        "due_date",
        "currency",
        "subtotal",
        "tax",
        "total",
    )

    document = models.OneToOneField(Document, on_delete=models.CASCADE, related_name="invoice")
    vendor = models.ForeignKey(
        Vendor, null=True, blank=True, on_delete=models.SET_NULL, related_name="invoices"
    )
    vendor_name = models.CharField(max_length=255, blank=True)
    vendor_tax_id = models.CharField(max_length=32, blank=True)
    invoice_number = models.CharField(max_length=100, blank=True, db_index=True)
    issue_date = models.DateField(null=True, blank=True)
    due_date = models.DateField(null=True, blank=True)
    currency = models.CharField(max_length=8, blank=True)
    subtotal = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)
    tax = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)
    total = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)

    base_currency = models.CharField(max_length=3, blank=True)
    total_base = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)
    fx_rate = models.DecimalField(max_digits=18, decimal_places=6, null=True, blank=True)
    fx_source = models.CharField(max_length=30, blank=True)

    confidence = models.JSONField(default=dict, blank=True)
    evidence = models.JSONField(default=dict, blank=True)
    min_confidence = models.FloatField(default=0.0)
    vendor_match = models.JSONField(default=dict, blank=True)
    edited_fields = models.JSONField(default=list, blank=True)

    reviewed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    reviewed_at = models.DateTimeField(null=True, blank=True)
    review_note = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-issue_date", "-id"]

    def __str__(self) -> str:
        return f"{self.vendor_name or '?'} {self.invoice_number or '?'}"

    @property
    def display_vendor(self) -> str:
        return self.vendor.name if self.vendor else self.vendor_name

    def to_schema(self) -> InvoiceSchema:
        """Rebuild the pipeline schema (used to re-validate after human edits)."""
        fields: dict[str, Any] = {
            name: {
                "value": getattr(self, name) if getattr(self, name) != "" else None,
                "confidence": float(self.confidence.get(name, 1.0)),
                "evidence": self.evidence.get(name) and _evidence_dict(self.evidence[name]),
            }
            for name in self.SCALAR_FIELDS
        }
        fields["line_items"] = [item.to_schema_dict() for item in self.line_items.all()]
        return InvoiceSchema.model_validate(fields)


class LineItem(models.Model):
    invoice = models.ForeignKey(Invoice, on_delete=models.CASCADE, related_name="line_items")
    position = models.PositiveSmallIntegerField()
    description = models.CharField(max_length=500)
    quantity = models.DecimalField(max_digits=12, decimal_places=3, null=True, blank=True)
    unit_price = models.DecimalField(max_digits=14, decimal_places=4, null=True, blank=True)
    amount = models.DecimalField(max_digits=14, decimal_places=2)
    category = models.CharField(
        max_length=30,
        choices=[(c.value, c.value.replace("_", " ").title()) for c in ExpenseCategory],
        default=ExpenseCategory.OTHER.value,
    )
    confidence = models.FloatField(default=0.0)
    evidence = models.JSONField(null=True, blank=True)

    class Meta:
        ordering = ["invoice", "position"]

    def __str__(self) -> str:
        return f"{self.description} ({self.amount})"

    def to_schema_dict(self) -> dict[str, Any]:
        return {
            "description": self.description,
            "quantity": self.quantity,
            "unit_price": self.unit_price,
            "amount": self.amount,
            "confidence": self.confidence,
            "evidence": self.evidence and _evidence_dict(self.evidence),
        }


class ValidationIssue(models.Model):
    class Stage(models.TextChoices):
        VALIDATION = "validation", "Validation"
        ENRICHMENT = "enrichment", "Enrichment"

    SEVERITY_ORDER = {Severity.ERROR: 0, Severity.WARNING: 1, Severity.INFO: 2}

    document = models.ForeignKey(Document, on_delete=models.CASCADE, related_name="issues")
    stage = models.CharField(max_length=20, choices=Stage.choices, default=Stage.VALIDATION)
    code = models.CharField(max_length=60)
    severity = models.CharField(
        max_length=10, choices=[(s.value, s.value.title()) for s in Severity], db_index=True
    )
    field = models.CharField(max_length=60, blank=True)
    message = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["id"]

    def __str__(self) -> str:
        return f"[{self.severity}] {self.code}"

    @property
    def rank(self) -> int:
        return self.SEVERITY_ORDER[Severity(self.severity)]


def _evidence_dict(stored: dict[str, Any]) -> dict[str, Any] | None:
    """Stored evidence also carries resolved offsets; the schema only wants quote/page."""
    if not stored.get("quote"):
        return None
    return {"quote": stored["quote"], "page": stored.get("page")}
