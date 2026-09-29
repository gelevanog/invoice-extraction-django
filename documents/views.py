"""Human review UI: queue, side-by-side review page, edit-in-place, approve/reject."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.db.models import Count, Q, QuerySet
from django.http import FileResponse, HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_http_methods, require_POST

from documents import services
from documents.accuracy import build_report
from documents.charts import timeline_chart
from documents.forms import ReviewDecisionForm, UploadForm
from documents.highlight import evidence_spans, highlight_pages, uncertain_words
from documents.models import Document, FieldReview, Invoice
from documents.tasks import enqueue_processing
from extraction.status import IN_PROGRESS, DocumentStatus

S = DocumentStatus

FIELD_LABELS = {
    "vendor_name": "Vendor",
    "vendor_tax_id": "Vendor tax ID",
    "invoice_number": "Invoice number",
    "issue_date": "Issue date",
    "due_date": "Due date",
    "currency": "Currency",
    "subtotal": "Subtotal",
    "tax": "Tax",
    "total": "Total",
}

QUEUE_FILTERS = {
    "needs_review": "Needs review",
    "approved": "Approved",
    "rejected": "Rejected",
    "failed": "Failed",
    "processing": "Processing",
    "all": "All",
}


def _is_htmx(request: HttpRequest) -> bool:
    return request.headers.get("HX-Request") == "true"


# --- queue --------------------------------------------------------------------------
def _filtered_documents(request: HttpRequest) -> tuple[QuerySet[Document], dict[str, Any]]:
    status = request.GET.get("status", "needs_review")
    if status not in QUEUE_FILTERS:
        status = "needs_review"
    query = request.GET.get("q", "").strip()
    errors_only = request.GET.get("errors") == "1"

    documents = (
        Document.objects.select_related("invoice", "invoice__vendor")
        .annotate(
            error_count=Count("issues", filter=Q(issues__severity="error")),
            warning_count=Count("issues", filter=Q(issues__severity="warning")),
        )
        .order_by("-created_at", "-id")
    )
    if status == "processing":
        documents = documents.filter(status__in=[s.value for s in IN_PROGRESS])
    elif status != "all":
        documents = documents.filter(status=status)
    if query:
        documents = documents.filter(
            Q(original_filename__icontains=query)
            | Q(invoice__vendor_name__icontains=query)
            | Q(invoice__vendor__name__icontains=query)
            | Q(invoice__invoice_number__icontains=query)
        )
    if errors_only:
        documents = documents.filter(error_count__gt=0)
    return documents, {"status": status, "q": query, "errors": errors_only}


def _status_counts() -> dict[str, int]:
    rows = Document.objects.values("status").annotate(n=Count("id"))
    by_status = {row["status"]: row["n"] for row in rows}
    counts = {
        key: by_status.get(key, 0) for key in ("needs_review", "approved", "rejected", "failed")
    }
    counts["processing"] = sum(by_status.get(s.value, 0) for s in IN_PROGRESS)
    counts["all"] = sum(by_status.values())
    return counts


@login_required
def queue(request: HttpRequest) -> HttpResponse:
    documents, filters = _filtered_documents(request)
    page = Paginator(documents, 25).get_page(request.GET.get("page"))
    context = {"page": page, "filters": filters, "filter_labels": QUEUE_FILTERS}
    if _is_htmx(request) and request.headers.get("HX-Target") == "queue-table":
        return render(request, "documents/partials/queue_table.html", context)
    context |= {"counts": _status_counts(), "upload_form": UploadForm()}
    return render(request, "documents/queue.html", context)


@login_required
@require_POST
def upload(request: HttpRequest) -> HttpResponse:
    form = UploadForm(request.POST, request.FILES)
    if not form.is_valid():
        for error in form.errors.get("files", []):
            messages.error(request, error)
        return redirect("documents:queue")
    for upload_file in form.cleaned_data["files"]:
        document = services.create_document(
            upload_file.name or "upload",
            upload_file.read(),
            user=request.user,
            content_type=upload_file.content_type or "",
        )
        enqueue_processing(document)
    count = len(form.cleaned_data["files"])
    messages.success(request, f"Uploaded {count} document(s); processing started.")
    return redirect(f"{reverse('documents:queue')}?status=all")


# --- review page --------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class FieldRow:
    name: str
    label: str
    display: str
    input_value: str
    confidence: float | None
    evidence: dict[str, Any] | None
    edited: bool
    severity: str | None  # worst issue severity attached to this field
    editable: bool


def _worst_severities(document: Document) -> dict[str, str]:
    worst: dict[str, str] = {}
    for issue in sorted(document.issues.all(), key=lambda i: i.rank, reverse=True):
        if issue.field:
            worst[issue.field] = issue.severity
    return worst


def _field_rows(document: Document, invoice: Invoice) -> list[FieldRow]:
    worst = _worst_severities(document)
    editable = document.status == S.NEEDS_REVIEW
    return [_field_row(invoice, name, worst.get(name), editable) for name in FIELD_LABELS]


def _format(value: Any) -> tuple[str, str]:
    """(display text, raw text for the edit input)."""
    if value is None or value == "":
        return "", ""
    if isinstance(value, date):
        return value.isoformat(), value.isoformat()
    if isinstance(value, Decimal):
        return f"{value:,.2f}", str(value)
    return str(value), str(value)


def _field_row(invoice: Invoice, name: str, severity: str | None, editable: bool) -> FieldRow:
    display, input_value = _format(getattr(invoice, name))
    return FieldRow(
        name=name,
        label=FIELD_LABELS[name],
        display=display,
        input_value=input_value,
        confidence=invoice.confidence.get(name),
        evidence=invoice.evidence.get(name),
        edited=name in invoice.edited_fields,
        severity=severity,
        editable=editable,
    )


def _review_context(document: Document) -> dict[str, Any]:
    invoice = getattr(document, "invoice", None)
    issues = sorted(document.issues.all(), key=lambda i: (i.rank, i.id))
    context: dict[str, Any] = {
        "document": document,
        "invoice": invoice,
        "issues": issues,
        "runs": document.extraction_runs.all(),
        "decision_form": ReviewDecisionForm(),
    }
    line_items = list(invoice.line_items.all()) if invoice else []
    spans = evidence_spans(invoice.evidence if invoice else {}, [li.evidence for li in line_items])
    threshold = settings.DOCEXTRACT["REVIEW_CONFIDENCE_THRESHOLD"]
    uncertain = uncertain_words(document.pages, threshold)
    context |= {
        "fields": _field_rows(document, invoice) if invoice else [],
        "line_items": line_items,
        "pages": highlight_pages(document.source_text, document.pages, spans, uncertain),
        "uncertain_word_count": len(uncertain),
        "is_image": document.content_type.startswith("image/"),
    }
    return context


@login_required
def detail(request: HttpRequest, pk: int) -> HttpResponse:
    document = get_object_or_404(Document.objects.select_related("invoice__vendor"), pk=pk)
    return render(request, "documents/detail.html", _review_context(document))


@login_required
@require_http_methods(["GET", "POST"])
def edit_field(request: HttpRequest, pk: int, field: str) -> HttpResponse:
    document = get_object_or_404(Document.objects.select_related("invoice"), pk=pk)
    invoice = document.invoice
    if field not in FIELD_LABELS or document.status != S.NEEDS_REVIEW:
        return HttpResponse(status=400)

    row = _field_row(invoice, field, _worst_severities(document).get(field), editable=True)
    context: dict[str, Any] = {"row": row, "document": document}
    if request.method == "GET":
        context["editing"] = request.GET.get("cancel") != "1"
        return render(request, "documents/partials/field_row.html", context)

    raw = request.POST.get("value", "")
    try:
        services.update_invoice_field(invoice, field, raw, request.user)
    except services.FieldValueError as exc:
        context |= {"editing": True, "error": str(exc), "raw": raw}
        return render(request, "documents/partials/field_row.html", context)
    document.refresh_from_db()
    response = render(request, "documents/partials/field_saved.html", _review_context(document))
    # Re-render every row (an edit can resolve issues attached to other fields).
    response["HX-Retarget"] = "#field-rows"
    response["HX-Reswap"] = "outerHTML"
    return response


@login_required
@require_POST
def decide(request: HttpRequest, pk: int) -> HttpResponse:
    document = get_object_or_404(Document, pk=pk)
    form = ReviewDecisionForm(request.POST)
    if not form.is_valid() or document.status != S.NEEDS_REVIEW:
        messages.error(request, "This document cannot be reviewed in its current state.")
        return redirect(document)
    action, note = form.cleaned_data["action"], form.cleaned_data["note"]
    if action == "approve":
        services.approve(document, request.user, note)
    else:
        services.reject(document, request.user, note)
    messages.success(request, f"{document.original_filename} {action}d.")

    next_document = (
        Document.objects.filter(status=S.NEEDS_REVIEW).order_by("created_at", "id").first()
    )
    return redirect(next_document) if next_document else redirect("documents:queue")


@login_required
@require_POST
def reprocess(request: HttpRequest, pk: int) -> HttpResponse:
    document = get_object_or_404(Document, pk=pk)
    if document.status not in {S.NEEDS_REVIEW, S.REJECTED, S.FAILED}:
        messages.error(request, "Only failed, rejected or in-review documents can be reprocessed.")
        return redirect(document)
    services.reset_for_reprocessing(document)
    enqueue_processing(document)
    messages.info(request, "Reprocessing started.")
    return redirect(document)


@login_required
def document_file(request: HttpRequest, pk: int) -> FileResponse:
    document = get_object_or_404(Document, pk=pk)
    return FileResponse(
        document.file.open("rb"),
        filename=document.original_filename,
        content_type=document.content_type or "application/octet-stream",
    )


# --- accuracy dashboard -------------------------------------------------------------
@login_required
def accuracy(request: HttpRequest) -> HttpResponse:
    vendor = request.GET.get("vendor", "").strip()
    report = build_report(vendor=vendor or None)
    corrections = FieldReview.objects.filter(corrected=True).select_related("document")
    if vendor:
        corrections = corrections.filter(vendor_name__icontains=vendor)
    context = {
        "report": report,
        "vendor": vendor,
        "chart": timeline_chart(report.timeline),
        "corrections": corrections[:12],
        "field_labels": FIELD_LABELS,
    }
    return render(request, "documents/accuracy.html", context)
