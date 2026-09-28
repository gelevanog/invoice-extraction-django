from django.contrib import admin

from documents.models import Document, ExtractionRun, Invoice, LineItem, ValidationIssue, Vendor


@admin.register(Vendor)
class VendorAdmin(admin.ModelAdmin):
    list_display = ("name", "tax_id", "created_at")
    search_fields = ("name", "tax_id", "aliases")


class ValidationIssueInline(admin.TabularInline):
    model = ValidationIssue
    extra = 0
    fields = ("severity", "code", "field", "message", "stage")
    readonly_fields = fields
    can_delete = False


class ExtractionRunInline(admin.TabularInline):
    model = ExtractionRun
    extra = 0
    fields = (
        "created_at",
        "provider",
        "model",
        "succeeded",
        "attempts",
        "input_tokens",
        "output_tokens",
        "latency_ms",
    )
    readonly_fields = fields
    can_delete = False
    show_change_link = True


@admin.register(Document)
class DocumentAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "original_filename",
        "status",
        "source_type",
        "created_at",
        "processed_at",
    )
    list_filter = ("status", "source_type")
    search_fields = ("original_filename", "sha256")
    readonly_fields = (
        "sha256",
        "size_bytes",
        "content_type",
        "source_text",
        "pages",
        "created_at",
        "updated_at",
        "processed_at",
    )
    inlines = (ValidationIssueInline, ExtractionRunInline)


class LineItemInline(admin.TabularInline):
    model = LineItem
    extra = 0
    fields = (
        "position",
        "description",
        "quantity",
        "unit_price",
        "amount",
        "category",
        "confidence",
    )


@admin.register(Invoice)
class InvoiceAdmin(admin.ModelAdmin):
    list_display = (
        "invoice_number",
        "vendor_name",
        "vendor",
        "issue_date",
        "total",
        "currency",
        "total_base",
        "min_confidence",
    )
    list_filter = ("currency", "document__status")
    search_fields = ("invoice_number", "vendor_name", "vendor__name")
    raw_id_fields = ("document", "vendor")
    inlines = (LineItemInline,)


@admin.register(ExtractionRun)
class ExtractionRunAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "document",
        "provider",
        "model",
        "succeeded",
        "attempts",
        "input_tokens",
        "output_tokens",
        "latency_ms",
        "created_at",
    )
    list_filter = ("provider", "succeeded")
    raw_id_fields = ("document",)


@admin.register(ValidationIssue)
class ValidationIssueAdmin(admin.ModelAdmin):
    list_display = ("document", "severity", "code", "field", "stage")
    list_filter = ("severity", "code", "stage")
    raw_id_fields = ("document",)
