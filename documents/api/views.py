from __future__ import annotations

import csv
import json
from typing import Any

from django.db.models import Q, QuerySet
from django.http import HttpResponse
from django.utils import timezone
from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework import mixins, status, viewsets
from rest_framework.decorators import action, api_view
from rest_framework.parsers import FormParser, MultiPartParser
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.serializers import BaseSerializer

from documents import services
from documents.api.serializers import (
    DocumentListSerializer,
    DocumentSerializer,
    DocumentUploadSerializer,
    InvoiceSerializer,
    ReviewDecisionSerializer,
)
from documents.models import Document, Invoice
from documents.tasks import enqueue_processing
from extraction.status import DocumentStatus

S = DocumentStatus


class DocumentViewSet(
    mixins.CreateModelMixin,
    mixins.ListModelMixin,
    mixins.RetrieveModelMixin,
    viewsets.GenericViewSet,
):
    """Upload documents and poll their processing status and extracted record."""

    parser_classes = (MultiPartParser, FormParser)

    def get_queryset(self) -> QuerySet[Document]:
        queryset = Document.objects.select_related("invoice__vendor").prefetch_related(
            "issues", "extraction_runs", "invoice__line_items"
        )
        if status_filter := self.request.query_params.get("status"):
            queryset = queryset.filter(status=status_filter)
        return queryset

    def get_serializer_class(self) -> type[BaseSerializer[Any]]:
        if self.action == "list":
            return DocumentListSerializer
        if self.action == "create":
            return DocumentUploadSerializer
        return DocumentSerializer

    @extend_schema(
        request={"multipart/form-data": DocumentUploadSerializer},
        responses={202: DocumentSerializer},
    )
    def create(self, request: Request, *args: Any, **kwargs: Any) -> Response:
        upload = DocumentUploadSerializer(data=request.data)
        upload.is_valid(raise_exception=True)
        file = upload.validated_data["file"]
        document = services.create_document(
            file.name, file.read(), user=request.user, content_type=file.content_type or ""
        )
        enqueue_processing(document)  # runs inline when CELERY_TASK_ALWAYS_EAGER=true
        document = self.get_queryset().get(pk=document.pk)
        data = DocumentSerializer(document, context={"request": request}).data
        return Response(data, status=status.HTTP_202_ACCEPTED, headers={"Location": data["url"]})

    @extend_schema(request=None, responses={202: DocumentSerializer})
    @action(detail=True, methods=["post"])
    def reprocess(self, request: Request, pk: str | None = None) -> Response:
        document = self.get_object()
        if document.status not in {S.NEEDS_REVIEW, S.REJECTED, S.FAILED}:
            return Response(
                {"detail": f"Cannot reprocess a document in status '{document.status}'."},
                status=status.HTTP_409_CONFLICT,
            )
        services.reset_for_reprocessing(document)
        enqueue_processing(document)
        document = self.get_queryset().get(pk=document.pk)
        return Response(
            DocumentSerializer(document, context={"request": request}).data,
            status=status.HTTP_202_ACCEPTED,
        )


INVOICE_FILTERS = [
    OpenApiParameter("status", str, description="Document status, e.g. approved or needs_review"),
    OpenApiParameter("vendor", str, description="Case-insensitive vendor name contains"),
    OpenApiParameter("currency", str, description="ISO 4217 code"),
    OpenApiParameter("issued_from", str, description="Issue date >= YYYY-MM-DD"),
    OpenApiParameter("issued_to", str, description="Issue date <= YYYY-MM-DD"),
]


def filter_invoices(queryset: QuerySet[Invoice], params: Any) -> QuerySet[Invoice]:
    if (status_filter := params.get("status")) and status_filter != "all":
        queryset = queryset.filter(document__status=status_filter)
    if vendor := params.get("vendor"):
        queryset = queryset.filter(
            Q(vendor__name__icontains=vendor) | Q(vendor_name__icontains=vendor)
        )
    if currency := params.get("currency"):
        queryset = queryset.filter(currency__iexact=currency)
    if issued_from := params.get("issued_from"):
        queryset = queryset.filter(issue_date__gte=issued_from)
    if issued_to := params.get("issued_to"):
        queryset = queryset.filter(issue_date__lte=issued_to)
    return queryset


def invoice_queryset() -> QuerySet[Invoice]:
    return Invoice.objects.select_related("document", "vendor").prefetch_related("line_items")


@extend_schema(parameters=INVOICE_FILTERS)
class InvoiceViewSet(viewsets.ReadOnlyModelViewSet):
    """Extracted invoice records, plus review actions for integrations."""

    serializer_class = InvoiceSerializer

    def get_queryset(self) -> QuerySet[Invoice]:
        return filter_invoices(invoice_queryset(), self.request.query_params)

    @extend_schema(request=ReviewDecisionSerializer, responses=InvoiceSerializer)
    @action(detail=True, methods=["post"])
    def approve(self, request: Request, pk: str | None = None) -> Response:
        return self._decide(request, services.approve)

    @extend_schema(request=ReviewDecisionSerializer, responses=InvoiceSerializer)
    @action(detail=True, methods=["post"])
    def reject(self, request: Request, pk: str | None = None) -> Response:
        return self._decide(request, services.reject)

    def _decide(self, request: Request, decision: Any) -> Response:
        invoice = self.get_object()
        if invoice.document.status != S.NEEDS_REVIEW:
            return Response(
                {"detail": f"Invoice is '{invoice.document.status}', not awaiting review."},
                status=status.HTTP_409_CONFLICT,
            )
        body = ReviewDecisionSerializer(data=request.data)
        body.is_valid(raise_exception=True)
        decision(invoice.document, request.user, body.validated_data["note"])
        return Response(InvoiceSerializer(invoice_queryset().get(pk=invoice.pk)).data)


EXPORT_COLUMNS = (
    "document_id", "status", "vendor", "vendor_name_on_document", "vendor_tax_id",
    "invoice_number", "issue_date", "due_date", "currency", "subtotal", "tax", "total",
    "base_currency", "total_base", "line_item_count", "min_confidence", "reviewed_at",
)  # fmt: skip


@extend_schema(
    parameters=[
        OpenApiParameter("status", str, description="Default: approved. Use 'all' for everything."),
        *INVOICE_FILTERS[1:],
    ],
    responses={(200, "text/csv"): str, (200, "application/json"): InvoiceSerializer(many=True)},
)
@api_view(["GET"])
def export_invoices(request: Request, fmt: str) -> HttpResponse:
    """Download invoices as CSV (one row per invoice) or JSON (full nested records)."""
    params = request.query_params.copy()
    params.setdefault("status", S.APPROVED.value)
    invoices = filter_invoices(invoice_queryset(), params).order_by("issue_date", "id")
    stamp = timezone.now().strftime("%Y%m%d-%H%M%S")

    if fmt == "json":
        payload = InvoiceSerializer(invoices, many=True).data
        response = HttpResponse(
            json.dumps(payload, indent=2, default=str), content_type="application/json"
        )
    else:
        response = HttpResponse(content_type="text/csv; charset=utf-8")
        writer = csv.writer(response)
        writer.writerow(EXPORT_COLUMNS)
        for inv in invoices:
            writer.writerow(
                [
                    inv.document_id, inv.document.status, inv.display_vendor, inv.vendor_name,
                    inv.vendor_tax_id, inv.invoice_number, inv.issue_date or "", inv.due_date or "",
                    inv.currency, inv.subtotal, inv.tax, inv.total, inv.base_currency,
                    inv.total_base, len(inv.line_items.all()), f"{inv.min_confidence:.2f}",
                    inv.reviewed_at.isoformat() if inv.reviewed_at else "",
                ]
            )  # fmt: skip
    response["Content-Disposition"] = f'attachment; filename="invoices-{stamp}.{fmt}"'
    return response
