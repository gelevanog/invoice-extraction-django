from __future__ import annotations

from datetime import date
from decimal import Decimal
from email.message import EmailMessage
from pathlib import Path

import pytest

from extraction.evidence import locate, resolve_invoice_evidence
from extraction.normalize import normalize_company_name, parse_amount, parse_date
from extraction.parse import ParsedDocument, UnsupportedDocumentError, parse_document
from extraction.schemas import Evidence
from tests.factories import make_invoice


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("1,338.00", Decimal("1338.00")),
        ("1.158,58", Decimal("1158.58")),
        ("840,00", Decimal("840.00")),
        ("€ 42,50", Decimal("42.50")),
        ("1,000", Decimal("1000")),
        ("-12.5", Decimal("-12.5")),
        ("USD 4,500.00", Decimal("4500.00")),
        ("n/a", None),
        ("", None),
    ],
)
def test_parse_amount(raw: str, expected: Decimal | None) -> None:
    assert parse_amount(raw) == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("2026-03-02", date(2026, 3, 2)),
        ("12.03.2026", date(2026, 3, 12)),
        ("14 March 2026", date(2026, 3, 14)),
        ("March 5, 2026", date(2026, 3, 5)),
        ("5 Mar 2026", date(2026, 3, 5)),
        ("01/02/2026", date(2026, 2, 1)),  # day-first
        ("sometime soon", None),
    ],
)
def test_parse_date(raw: str, expected: date | None) -> None:
    assert parse_date(raw) == expected


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("Northwind Traders Ltd.", "northwind traders"),
        ("NORTHWIND TRADERS LIMITED", "northwind traders"),
        ("Kestrel Freight Logistics B.V.", "kestrel freight logistics"),
        ("Brightline Software GmbH", "brightline software"),
        ("Café Zürich AG", "cafe zurich"),
        ("Inc", "inc"),  # never normalizes to an empty string
    ],
)
def test_normalize_company_name(name: str, expected: str) -> None:
    assert normalize_company_name(name) == expected


def test_parse_pdf_keeps_pages_and_columns(sample_dir: Path) -> None:
    path = sample_dir / "01_brightline_software_invoice.pdf"
    document = parse_document(path.name, path.read_bytes())
    assert document.source_type == "pdf"
    assert [p.number for p in document.pages] == [1]
    assert "Invoice No:    BLS-2026-0142" in document.text
    assert "Team plan subscription (March)    12    49.00    588.00" in document.text


def test_parse_email_headers_and_body(sample_dir: Path) -> None:
    path = sample_dir / "04_kestrel_freight_email.eml"
    document = parse_document(path.name, path.read_bytes())
    assert document.source_type == "email"
    assert document.metadata["subject"].startswith("Invoice KFL-88412")
    assert document.text.startswith("From: Kestrel Freight Logistics")
    assert "Invoice number: KFL-88412" in document.text


def test_parse_email_with_pdf_attachment_adds_pages(sample_dir: Path) -> None:
    message = EmailMessage()
    message["From"] = "billing@example.com"
    message["Subject"] = "Your invoice"
    message.set_content("Invoice attached.")
    pdf = (sample_dir / "03_bluepeak_consulting_invoice.pdf").read_bytes()
    message.add_attachment(pdf, maintype="application", subtype="pdf", filename="inv.pdf")

    document = parse_document("mail.eml", bytes(message))
    assert len(document.pages) == 2
    assert "Bluepeak Consulting LLC" in document.pages[1].text
    assert document.metadata["attachments"] == "inv.pdf"
    assert document.page_for_offset(document.pages[1].start) == 2


def test_parse_text() -> None:
    document = parse_document("r.txt", b"\xef\xbb\xbfTOTAL  10.00\r\n")
    assert document.text == "TOTAL  10.00"


def test_images_are_rejected_with_ocr_hint() -> None:
    with pytest.raises(UnsupportedDocumentError, match="OCR"):
        parse_document("scan.png", b"\x89PNG")


def test_page_offsets_are_consistent() -> None:
    document = ParsedDocument.from_page_texts(["first page", "second"], "pdf")
    assert document.text[document.pages[1].start : document.pages[1].end] == "second"
    assert document.page_for_offset(0) == 1
    assert document.page_for_offset(document.pages[1].start + 2) == 2


def test_locate_is_whitespace_and_case_insensitive() -> None:
    document = ParsedDocument.from_page_texts(["Header", "Invoice No:      BLS-1\nTotal 5"], "pdf")
    span = locate(Evidence(quote="invoice no: bls-1", page=2), document)
    assert span is not None and span.page == 2
    assert document.text[span.start : span.end] == "Invoice No:      BLS-1"


def test_locate_falls_back_when_page_hint_is_wrong() -> None:
    document = ParsedDocument.from_page_texts(["Total 5", "other"], "pdf")
    span = locate(Evidence(quote="Total 5", page=2), document)
    assert span is not None and span.page == 1


def test_locate_returns_none_for_hallucinated_quote() -> None:
    document = ParsedDocument.from_page_texts(["Total 5"], "pdf")
    assert locate(Evidence(quote="Total 500", page=1), document) is None


def test_resolve_invoice_evidence_covers_fields_and_line_items() -> None:
    total = {"value": "178.50", "confidence": 0.9, "evidence": {"quote": "Total 178.50"}}
    item = {"description": "Support", "amount": "50", "confidence": 0.9,
            "evidence": {"quote": "Support 50", "page": 1}}  # fmt: skip
    invoice = make_invoice(total=total, line_items=[item])
    document = ParsedDocument.from_page_texts(["Support   50\nTotal 178.50"], "text")
    spans = resolve_invoice_evidence(invoice, document)
    assert spans["total"] is not None
    assert spans["line_items.0"] is not None
    assert "vendor_name" not in spans  # no evidence given -> nothing to resolve
