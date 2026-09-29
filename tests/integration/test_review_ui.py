from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from django.test import Client

from documents.batch import import_folder
from documents.highlight import highlight_pages, uncertain_words
from documents.models import Document

pytestmark = pytest.mark.django_db


@pytest.fixture
def client_logged_in(client: Client, reviewer: Any) -> Client:
    client.force_login(reviewer)
    return client


@pytest.fixture
def docs(demo_vendors: Any, text_sample_dir: Path) -> dict[str, Document]:
    report = import_folder(text_sample_dir)
    return {item.path.name[:2]: item.document for item in report.items if item.document}


def test_login_required(client: Client) -> None:
    response = client.get("/review/")
    assert response.status_code == 302
    assert response["Location"].startswith("/accounts/login/")


def test_queue_defaults_to_needs_review(
    client_logged_in: Client, docs: dict[str, Document]
) -> None:
    html = client_logged_in.get("/review/").content.decode()
    assert "03_bluepeak_consulting_invoice.pdf" in html
    assert "01_brightline_software_invoice.pdf" not in html  # auto-approved


def test_queue_htmx_filter_returns_partial(
    client_logged_in: Client, docs: dict[str, Document]
) -> None:
    response = client_logged_in.get(
        "/review/",
        {"status": "all", "q": "northwind"},
        headers={"HX-Request": "true", "HX-Target": "queue-table"},
    )
    html = response.content.decode()
    assert "<html" not in html
    assert "02_northwind_office_supplies.pdf" in html
    assert "03_bluepeak" not in html


def test_queue_errors_only_filter(client_logged_in: Client, docs: dict[str, Document]) -> None:
    html = client_logged_in.get("/review/", {"status": "all", "errors": "1"}).content.decode()
    assert "03_bluepeak_consulting_invoice.pdf" in html
    assert "05_pinecrest_hardware_receipt.txt" not in html  # only a warning


def test_review_page_shows_evidence_and_issues(
    client_logged_in: Client, docs: dict[str, Document]
) -> None:
    html = client_logged_in.get(f"/review/{docs['03'].pk}/").content.decode()
    assert "Why this needs review" in html
    assert "Line items sum mismatch" in html
    assert (
        '<mark class="evidence" data-fields="invoice_number">Invoice #:    BPC-1007</mark>' in html
    )
    assert 'name="action" value="approve"' in html


def test_inline_edit_flow(client_logged_in: Client, docs: dict[str, Document]) -> None:
    document = docs["03"]
    url = f"/review/{document.pk}/fields/subtotal/"
    editor = client_logged_in.get(url, headers={"HX-Request": "true"}).content.decode()
    assert 'name="value" value="4500.00"' in editor

    bad = client_logged_in.post(url, {"value": "lots"}, headers={"HX-Request": "true"})
    assert "Enter an amount" in bad.content.decode()

    ok = client_logged_in.post(url, {"value": "4,200.00"}, headers={"HX-Request": "true"})
    assert ok["HX-Retarget"] == "#field-rows"
    html = ok.content.decode()
    assert 'id="issues-panel" class="card" hx-swap-oob="true"' in html
    assert "Totals mismatch" in html  # subtotal now 4200 but total still 4500
    document.refresh_from_db()
    assert document.invoice.subtotal == 4200


def test_edit_not_allowed_after_approval(
    client_logged_in: Client, docs: dict[str, Document]
) -> None:
    response = client_logged_in.post(f"/review/{docs['01'].pk}/fields/total/", {"value": "1"})
    assert response.status_code == 400


def test_decide_redirects_to_next_in_queue(
    client_logged_in: Client, docs: dict[str, Document]
) -> None:
    response = client_logged_in.post(
        f"/review/{docs['03'].pk}/decide/", {"action": "reject", "note": "dup"}
    )
    docs["03"].refresh_from_db()
    assert docs["03"].status == "rejected"
    assert response["Location"] == f"/review/{docs['05'].pk}/"


def test_upload_via_ui(
    client_logged_in: Client,
    demo_vendors: Any,
    sample_dir: Path,
    django_capture_on_commit_callbacks: Any,
) -> None:
    path = sample_dir / "04_kestrel_freight_email.eml"
    with path.open("rb") as handle, django_capture_on_commit_callbacks(execute=True):
        response = client_logged_in.post("/review/upload/", {"files": [handle]})
    assert response.status_code == 302
    document = Document.objects.get()
    assert document.status == "approved"


def test_download_original_file(client_logged_in: Client, docs: dict[str, Document]) -> None:
    response = client_logged_in.get(f"/review/{docs['05'].pk}/file/")
    assert b"PINECREST HARDWARE" in b"".join(response.streaming_content)


def test_highlight_handles_overlapping_spans_and_escaping() -> None:
    text = "<b>Total</b> EUR 10.00"
    pages = highlight_pages(text, [], {"total": (0, 22), "currency": (13, 16)})
    html = str(pages[0].html)
    assert "&lt;b&gt;" in html  # source text is escaped
    assert '<mark class="evidence" data-fields="currency total">EUR</mark>' in html
    assert html.count("<mark") == 3


def test_highlight_underlines_uncertain_ocr_words_inside_evidence() -> None:
    text = "Total 178.5O"
    pages = [{"number": 1, "start": 0, "end": 12, "ocr_confidence": 0.7,
              "words": [[0, 5, 0.96, 0, 0, 50, 10], [6, 12, 0.41, 60, 0, 60, 10]]}]  # fmt: skip
    uncertain = uncertain_words(pages, threshold=0.75)
    assert [(w.start, w.end) for w in uncertain] == [(6, 12)]

    view = highlight_pages(text, pages, {"total": (0, 12)}, uncertain)[0]
    assert view.ocr_confidence == 0.7
    assert str(view.html) == (
        '<mark class="evidence" data-fields="total">Total </mark>'
        '<mark class="evidence" data-fields="total">'
        '<span class="ocr-low" title="OCR confidence 0.41">178.5O</span></mark>'
    )
