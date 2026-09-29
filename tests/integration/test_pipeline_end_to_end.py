"""Process every sample document through the Django-wired pipeline."""

from __future__ import annotations

import io
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from django.core.management import call_command

from documents import services
from documents.batch import import_folder
from documents.models import Document, ExtractionRun, Invoice, Vendor
from extraction.status import DocumentStatus
from tests.conftest import HAS_TESSERACT, OCR_SAMPLES

pytestmark = pytest.mark.django_db

TEXT_SAMPLES = {
    "01_brightline_software_invoice.pdf": ("approved", []),
    "02_northwind_office_supplies.pdf": ("approved", []),
    "03_bluepeak_consulting_invoice.pdf": ("needs_review", ["line_items_sum_mismatch"]),
    "04_kestrel_freight_email.eml": ("approved", ["missing_due_date"]),
    "05_pinecrest_hardware_receipt.txt": ("needs_review", ["missing_due_date"]),
    "06_brightline_software_invoice_copy.pdf": ("needs_review", ["duplicate_invoice"]),
}
# Scans need Tesseract; without it they fail at the parse stage with a clear message.
EXPECTED = {**TEXT_SAMPLES, **dict.fromkeys(OCR_SAMPLES)}


@pytest.fixture
def processed(demo_vendors: Any, sample_dir: Path) -> dict[str, Document]:
    report = import_folder(sample_dir)
    return {item.path.name: item.document for item in report.items if item.document}


def test_routing_of_all_samples(processed: dict[str, Document]) -> None:
    assert set(processed) == set(EXPECTED)
    for name, (status, blocking_codes) in TEXT_SAMPLES.items():
        document = processed[name]
        codes = sorted(document.issues.exclude(severity="info").values_list("code", flat=True))
        assert (document.status, codes) == (status, blocking_codes), name


@pytest.mark.skipif(HAS_TESSERACT, reason="covers the missing-binary path")
def test_scans_fail_clearly_without_tesseract(processed: dict[str, Document]) -> None:
    for name in OCR_SAMPLES:
        assert processed[name].status == "failed"
        assert "Tesseract is not installed" in processed[name].error


def test_status_machine_and_audit_trail(processed: dict[str, Document]) -> None:
    for document in processed.values():
        if document.status == "failed":  # scans without Tesseract, covered above
            continue
        assert document.processed_at is not None
        assert document.source_text
        assert document.pages[0]["start"] == 0
        run = document.extraction_runs.get()
        assert run.succeeded and run.provider == "fake" and run.attempts == 1
        assert run.input_tokens > 0 and run.raw_output.startswith("{")


def test_fuzzy_vendor_match_and_fx(processed: dict[str, Document]) -> None:
    invoice = processed["02_northwind_office_supplies.pdf"].invoice
    assert invoice.vendor is not None
    assert invoice.vendor.name == "Northwind Traders Limited"
    assert invoice.vendor_match["method"] == "fuzzy"
    assert invoice.total == Decimal("656.40") and invoice.currency == "GBP"
    assert invoice.base_currency == "EUR" and invoice.total_base == Decimal("768.62")
    # Auto-approval enriched vendor master data: alias + tax ID learned from the invoice.
    vendor = Vendor.objects.get(name="Northwind Traders Limited")
    assert vendor.aliases == ["Northwind Traders Ltd."]
    assert vendor.tax_id == "GB123456789"


def test_evidence_spans_point_at_source_text(processed: dict[str, Document]) -> None:
    document = processed["01_brightline_software_invoice.pdf"]
    evidence = document.invoice.evidence["invoice_number"]
    assert evidence["found"] is True
    assert document.source_text[evidence["start"] : evidence["end"]].startswith("Invoice No:")
    item = document.invoice.line_items.first()
    assert item.evidence["found"] and item.category == "software"


def test_duplicate_references_original(processed: dict[str, Document]) -> None:
    duplicate = processed["06_brightline_software_invoice_copy.pdf"]
    original = processed["01_brightline_software_invoice.pdf"]
    issue = duplicate.issues.get(code="duplicate_invoice")
    assert f"document #{original.pk}" in issue.message


def test_rejecting_original_clears_duplicate_on_reprocess(
    processed: dict[str, Document], reviewer: Any
) -> None:
    original = processed["01_brightline_software_invoice.pdf"]
    # Approved records are final; simulate a mistaken approval being corrected in data.
    Document.objects.filter(pk=original.pk).update(status=DocumentStatus.REJECTED)
    duplicate = processed["06_brightline_software_invoice_copy.pdf"]
    services.reset_for_reprocessing(duplicate)
    duplicate = services.process_document(duplicate.pk)
    assert duplicate.status == "approved"
    assert duplicate.extraction_runs.count() == 2  # history is kept


def test_low_confidence_fix_then_approve(processed: dict[str, Document], reviewer: Any) -> None:
    document = processed["05_pinecrest_hardware_receipt.txt"]
    assert "invoice_number=0.55" in document.routing_reasons[0]

    services.update_invoice_field(document.invoice, "vendor_name", "Pinecrest Hardware", reviewer)
    services.update_invoice_field(document.invoice, "invoice_number", "PH-88213", reviewer)
    document.refresh_from_db()
    assert document.routing_reasons == []
    assert document.invoice.edited_fields == ["vendor_name", "invoice_number"]

    services.approve(document, reviewer, "checked against till receipt")
    document.refresh_from_db()
    assert document.status == "approved"
    assert document.invoice.reviewed_by == reviewer
    assert Vendor.objects.filter(name="Pinecrest Hardware").exists()


def test_fixing_totals_resolves_error(processed: dict[str, Document], reviewer: Any) -> None:
    document = processed["03_bluepeak_consulting_invoice.pdf"]
    invoice = document.invoice
    services.update_invoice_field(invoice, "subtotal", "4.200,00", reviewer)
    services.update_invoice_field(invoice, "total", "4200", reviewer)
    document.refresh_from_db()
    codes = set(document.issues.values_list("code", flat=True))
    assert "line_items_sum_mismatch" not in codes and "totals_mismatch" not in codes
    assert Invoice.objects.get(pk=invoice.pk).total_base == Decimal("3870.97")


def test_invalid_edit_is_rejected(processed: dict[str, Document]) -> None:
    invoice = processed["03_bluepeak_consulting_invoice.pdf"].invoice
    with pytest.raises(services.FieldValueError):
        services.update_invoice_field(invoice, "due_date", "next tuesday")
    with pytest.raises(services.FieldValueError):
        services.update_invoice_field(invoice, "document", "1")


def test_rerun_skips_already_imported_files(
    processed: dict[str, Document], sample_dir: Path
) -> None:
    report = import_folder(sample_dir)
    assert report.skipped == len(EXPECTED)
    assert Document.objects.count() == len(EXPECTED)


def test_failed_extraction_marks_document_failed(db: None, monkeypatch: pytest.MonkeyPatch) -> None:
    from extraction.llm.base import LLMError
    from extraction.llm.fake import FakeProvider

    def broken(self: Any, request: Any) -> Any:
        raise LLMError("provider unavailable")

    monkeypatch.setattr(FakeProvider, "complete_json", broken)
    document = services.create_document("x.txt", b"Invoice No: 1\nTotal 5.00 EUR")
    document = services.process_document(document.pk)
    assert document.status == "failed"
    assert "provider unavailable" in document.error
    assert ExtractionRun.objects.get(document=document).succeeded is False


def test_unreadable_image_fails_at_parse(db: None) -> None:
    document = services.create_document("scan.png", b"\x89PNG")
    document = services.process_document(document.pk)
    assert document.status == "failed"
    assert "OCR failed: Could not read image" in document.error


def test_process_folder_command_prints_summary(demo_vendors: Any, sample_dir: Path) -> None:
    out = io.StringIO()
    call_command("process_folder", str(sample_dir), stdout=out)
    output = out.getvalue()
    assert f"Processed {len(EXPECTED)} document(s)" in output
    assert "BLS-2026-0142" in output
    if HAS_TESSERACT:
        assert "LPS-24-0918" in output
    assert "BLS-2026-0142" in output


def test_seed_demo_is_idempotent(db: None, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DEMO_USER_PASSWORD", raising=False)
    call_command("seed_demo", "--password", "", stdout=io.StringIO())
    call_command("seed_demo", "--password", "", stdout=io.StringIO())
    assert Vendor.objects.count() == 3
