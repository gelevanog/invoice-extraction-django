from __future__ import annotations

from pathlib import PurePath

from django.core.files.uploadedfile import UploadedFile
from rest_framework import serializers

from documents.models import (
    Document,
    ExtractionRun,
    FieldReview,
    Invoice,
    LineItem,
    ValidationIssue,
    Vendor,
)
from extraction.parse import SUPPORTED_EXTENSIONS


class VendorSerializer(serializers.ModelSerializer):
    class Meta:
        model = Vendor
        fields = ("id", "name", "tax_id", "aliases")


class LineItemSerializer(serializers.ModelSerializer):
    class Meta:
        model = LineItem
        fields = (
            "position",
            "description",
            "quantity",
            "unit_price",
            "amount",
            "category",
            "confidence",
            "evidence",
        )


class ValidationIssueSerializer(serializers.ModelSerializer):
    class Meta:
        model = ValidationIssue
        fields = ("code", "severity", "field", "message", "stage")


class ExtractionRunSerializer(serializers.ModelSerializer):
    class Meta:
        model = ExtractionRun
        fields = (
            "id", "provider", "model", "succeeded", "attempts", "attempt_log",
            "input_tokens", "output_tokens", "latency_ms", "error", "examples", "created_at",
        )  # fmt: skip


class InvoiceSerializer(serializers.ModelSerializer):
    document_id = serializers.IntegerField(read_only=True)
    status = serializers.CharField(source="document.status", read_only=True)
    vendor = VendorSerializer(read_only=True)
    line_items = LineItemSerializer(many=True, read_only=True)

    class Meta:
        model = Invoice
        fields = (
            "id", "document_id", "status", "vendor", "vendor_name", "vendor_tax_id",
            "invoice_number", "issue_date", "due_date", "currency", "subtotal", "tax", "total",
            "base_currency", "total_base", "fx_rate", "fx_source", "min_confidence",
            "confidence", "evidence", "vendor_match", "edited_fields", "line_items",
            "reviewed_at", "review_note",
        )  # fmt: skip


class DocumentSerializer(serializers.ModelSerializer):
    invoice = InvoiceSerializer(read_only=True)
    issues = ValidationIssueSerializer(many=True, read_only=True)
    extraction_runs = ExtractionRunSerializer(many=True, read_only=True)
    url = serializers.HyperlinkedIdentityField(view_name="api:document-detail")
    review_url = serializers.SerializerMethodField()

    class Meta:
        model = Document
        fields = (
            "id", "url", "review_url", "original_filename", "content_type", "size_bytes", "sha256",
            "status", "source_type", "metadata", "error", "routing_reasons", "created_at",
            "processed_at", "invoice", "issues", "extraction_runs",
        )  # fmt: skip

    def get_review_url(self, obj: Document) -> str:
        request = self.context.get("request")
        path = obj.get_absolute_url()
        return request.build_absolute_uri(path) if request else path


class DocumentListSerializer(serializers.ModelSerializer):
    url = serializers.HyperlinkedIdentityField(view_name="api:document-detail")

    class Meta:
        model = Document
        fields = (
            "id",
            "url",
            "original_filename",
            "status",
            "source_type",
            "created_at",
            "processed_at",
        )


class DocumentUploadSerializer(serializers.Serializer):
    file = serializers.FileField()

    def validate_file(self, value: UploadedFile) -> UploadedFile:
        suffix = PurePath(value.name or "").suffix.lower()
        if suffix not in SUPPORTED_EXTENSIONS:
            allowed = ", ".join(sorted(SUPPORTED_EXTENSIONS))
            raise serializers.ValidationError(
                f"Unsupported file type '{suffix}'. Allowed: {allowed}"
            )
        return value


class ReviewDecisionSerializer(serializers.Serializer):
    note = serializers.CharField(required=False, allow_blank=True, max_length=2000, default="")


class FieldReviewSerializer(serializers.ModelSerializer):
    class Meta:
        model = FieldReview
        fields = (
            "document_id", "vendor_name", "field", "extracted_value", "approved_value",
            "corrected", "extracted_confidence", "evidence_quote", "reviewed_at",
        )  # fmt: skip


class AccuracySerializer(serializers.Serializer):
    key = serializers.CharField()
    reviewed = serializers.IntegerField()
    corrected = serializers.IntegerField()
    accepted = serializers.IntegerField()
    rate = serializers.FloatField(allow_null=True)


class TimelinePointSerializer(serializers.Serializer):
    document_id = serializers.IntegerField()
    filename = serializers.CharField()
    vendor = serializers.CharField()
    reviewed_at = serializers.DateTimeField()
    reviewed = serializers.IntegerField()
    corrected = serializers.IntegerField()
    rate = serializers.FloatField()
    cumulative_rate = serializers.FloatField()


class AccuracyReportSerializer(serializers.Serializer):
    overall = AccuracySerializer()
    invoices_reviewed = serializers.IntegerField()
    auto_approved = serializers.IntegerField()
    by_field = AccuracySerializer(many=True)
    by_vendor = AccuracySerializer(many=True)
    timeline = TimelinePointSerializer(many=True)
