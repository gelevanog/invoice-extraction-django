from __future__ import annotations

from pathlib import PurePath

from django.core.files.uploadedfile import UploadedFile
from rest_framework import serializers

from documents.models import Document, ExtractionRun, Invoice, LineItem, ValidationIssue, Vendor
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
            "input_tokens", "output_tokens", "latency_ms", "error", "created_at",
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
            "status", "source_type", "error", "routing_reasons", "created_at", "processed_at",
            "invoice", "issues", "extraction_runs",
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
