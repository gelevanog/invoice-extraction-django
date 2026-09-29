"""Real Tesseract on the scanned samples, through the Django-wired pipeline and review UI.

Assertions stick to what any Tesseract 5.x reads reliably on these samples (labelled
values, totals, routing), not to exact OCR text, which varies slightly by version.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from django.test import Client

from documents import services
from documents.models import Document

pytestmark = [pytest.mark.django_db, pytest.mark.requires_tesseract]

SCAN = "07_lantern_print_scan.pdf"
PHOTO = "08_copperleaf_catering_photo.jpg"


def process(sample_dir: Path, name: str) -> Document:
    document = services.create_document(name, (sample_dir / name).read_bytes())
    return services.process_document(document.pk)


def test_scanned_pdf_is_extracted_from_ocr_text(demo_vendors: Any, sample_dir: Path) -> None:
    document = process(sample_dir, SCAN)

    assert document.status == "approved", document.routing_reasons
    assert document.source_type == "scanned_pdf"
    assert document.metadata["ocr_engine"] == "tesseract"
    assert float(document.metadata["ocr_confidence"]) > 0.85
    invoice = document.invoice
    assert (invoice.vendor_name, invoice.invoice_number) == (
        "Lantern Print Studio GmbH",
        "LPS-24-0918",
    )
    assert (invoice.subtotal, invoice.tax, invoice.total) == (
        Decimal("537.00"),
        Decimal("102.03"),
        Decimal("639.03"),
    )
    assert invoice.line_items.count() == 3
    # Evidence resolves inside the OCR text and carries the OCR confidence of its words.
    evidence = invoice.evidence["invoice_number"]
    assert evidence["found"] is True and 0 < evidence["ocr_confidence"] <= 1
    assert "LPS-24-0918" in document.source_text[evidence["start"] : evidence["end"]]
    # A value is never more confident than the words it was read from.
    assert invoice.confidence["invoice_number"] <= evidence["ocr_confidence"]
    assert document.issues.filter(code="ocr_text", severity="info").exists()


def test_photo_with_misread_words_goes_to_review(demo_vendors: Any, sample_dir: Path) -> None:
    document = process(sample_dir, PHOTO)

    assert document.source_type == "image"
    assert document.status == "needs_review"
    invoice = document.invoice
    assert invoice.invoice_number == "CC-20931"
    assert (invoice.currency, invoice.total) == ("GBP", Decimal("202.20"))
    assert document.pages[0]["words"], "word boxes and confidences are persisted"
    assert any(w[2] < 0.75 for w in document.pages[0]["words"]), "some words are uncertain"


def test_ocr_data_survives_a_reviewer_edit(demo_vendors: Any, sample_dir: Path) -> None:
    document = process(sample_dir, PHOTO)
    parsed = document.parsed_document()
    assert parsed.is_ocr and parsed.pages[0].words

    services.update_invoice_field(document.invoice, "due_date", "2026-04-19")
    # Re-validation rebuilds the document from the database, OCR confidence included.
    assert document.issues.filter(code="ocr_text").exists()
    assert not document.issues.filter(code="missing_due_date").exists()


def test_review_page_shows_image_and_uncertain_words(
    client: Client, reviewer: Any, demo_vendors: Any, sample_dir: Path
) -> None:
    document = process(sample_dir, PHOTO)
    client.force_login(reviewer)

    html = client.get(f"/review/{document.pk}/").content.decode()

    assert "OCR text, confidence" in html
    assert f'src="/review/{document.pk}/file/"' in html
    assert 'class="ocr-low" title="OCR confidence' in html
    assert client.get(f"/review/{document.pk}/file/")["Content-Type"] == "image/jpeg"
