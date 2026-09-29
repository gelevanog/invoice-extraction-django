"""Reviewer corrections -> FieldReview rows -> few-shot examples and accuracy tracking."""

from __future__ import annotations

import io
from pathlib import Path
from typing import Any

import pytest
from django.core.management import call_command
from django.test import Client
from rest_framework.test import APIClient

from documents import feedback, services
from documents.accuracy import build_report
from documents.models import Document, ExtractionRun, FieldReview

pytestmark = pytest.mark.django_db

LOOP = Path(__file__).resolve().parents[2] / "sample_data" / "feedback_loop"
MARCH, APRIL = "haverford_invoice_march.pdf", "haverford_invoice_april.pdf"


def process(name: str, folder: Path = LOOP) -> Document:
    document = services.create_document(name, (folder / name).read_bytes())
    return services.process_document(document.pk)


def review_march(reviewer: Any) -> Document:
    """What a reviewer does with the March invoice: fix three fields, approve."""
    march = process(MARCH)
    assert march.status == "needs_review"
    invoice = march.invoice
    services.update_invoice_field(invoice, "invoice_number", "HOI-7731", reviewer)
    services.update_invoice_field(invoice, "issue_date", "06/03/2026", reviewer)
    services.update_invoice_field(invoice, "tax", "284.00", reviewer)
    march.refresh_from_db()
    services.approve(march, reviewer, "checked")
    return march


def test_approval_records_every_field_and_marks_corrections(reviewer: Any) -> None:
    march = review_march(reviewer)
    rows = {r.field: r for r in FieldReview.objects.filter(document=march)}

    assert len(rows) == 9
    assert ExtractionRun.objects.get(document=march).examples == []  # nothing reviewed yet
    assert {name for name, r in rows.items() if r.corrected} == {
        "invoice_number", "issue_date", "tax",
    }  # fmt: skip
    tax = rows["tax"]
    assert (tax.extracted_value, tax.approved_value) == ("2026.00", "284.00")
    assert tax.extracted_confidence == 0.95  # confidently wrong - caught by the totals check
    # Corrected values are located in the source text; accepted ones keep the model quote.
    assert tax.evidence_quote == "VAT 20% 284.00"
    assert rows["issue_date"].evidence_quote == "Tax point: 06/03/2026"
    assert rows["invoice_number"].extracted_value == ""
    assert rows["currency"].evidence_quote == "Total (GBP) 1,704.00"
    assert all(r.vendor is not None and r.reviewed_by == reviewer for r in rows.values())


def test_corrections_become_few_shot_examples_for_the_same_vendor(reviewer: Any) -> None:
    march = review_march(reviewer)
    april = process(APRIL)

    assert april.status == "approved", april.routing_reasons
    invoice = april.invoice
    assert (invoice.invoice_number, str(invoice.issue_date), str(invoice.tax)) == (
        "HOI-7802",
        "2026-04-03",
        "136.20",
    )
    run = ExtractionRun.objects.get(document=april)
    assert run.examples == [f"document #{march.pk}"]


def test_examples_can_be_disabled(reviewer: Any, settings: Any) -> None:
    review_march(reviewer)
    settings.DOCEXTRACT = {**settings.DOCEXTRACT, "FEWSHOT_MAX_EXAMPLES": 0}
    april = process(APRIL)
    assert ExtractionRun.objects.get(document=april).examples == []
    assert april.status == "needs_review"


def test_examples_prefer_corrected_documents_then_recent(reviewer: Any) -> None:
    march = review_march(reviewer)
    april = process(APRIL)  # auto-approved: not a reviewed example
    parsed = april.parsed_document()

    examples = feedback.examples_for(parsed)
    assert [e.source for e in examples] == [f"document #{march.pk}"]
    record = examples[0].record
    assert record["invoice_number"] == {
        "value": "HOI-7731",
        "evidence": "Document no.: HOI-7731",
        "reviewer_corrected_from": None,
    }
    assert feedback.examples_for(parsed, exclude_document_id=march.pk) == []


def test_auto_approved_and_rejected_documents_are_not_recorded(
    demo_vendors: Any, text_sample_dir: Path, reviewer: Any
) -> None:
    approved = process("01_brightline_software_invoice.pdf", text_sample_dir)
    rejected = process("03_bluepeak_consulting_invoice.pdf", text_sample_dir)
    services.reject(rejected, reviewer, "wrong subtotal")
    assert approved.status == "approved"
    assert not FieldReview.objects.exists()
    assert build_report().auto_approved == 1


def test_accuracy_report_and_rebuild(reviewer: Any) -> None:
    review_march(reviewer)
    process(APRIL)
    report = build_report()

    assert (report.overall.reviewed, report.overall.corrected) == (9, 3)
    assert report.overall.rate == pytest.approx(6 / 9)
    assert (report.invoices_reviewed, report.auto_approved) == (1, 1)
    by_field = {a.key: a.rate for a in report.by_field}
    assert by_field["invoice_number"] == 0 and by_field["total"] == 1
    assert [a.key for a in report.by_vendor] == ["Haverford Office Interiors Ltd"]
    assert report.timeline[0].cumulative_rate == pytest.approx(6 / 9)

    FieldReview.objects.all().delete()
    assert feedback.rebuild_reviews() == 9
    assert feedback.rebuild_reviews() == 9  # idempotent
    assert build_report(vendor="nobody").overall.reviewed == 0


def test_demo_command_shows_the_loop_and_is_idempotent(reviewer: Any) -> None:
    out = io.StringIO()
    call_command("demo_feedback_loop", reviewer="reviewer", stdout=out)
    output = out.getvalue()
    assert "reviewer corrected invoice_number, issue_date, tax; approved" in output
    assert "haverford_invoice_april.pdf: approved (few-shot examples used: document #" in output

    again = io.StringIO()
    call_command("demo_feedback_loop", stdout=again)
    assert "already imported (approved)" in again.getvalue()
    assert Document.objects.count() == 2


def test_accuracy_report_command(reviewer: Any) -> None:
    empty = io.StringIO()
    call_command("accuracy_report", stdout=empty)
    assert "No reviewed invoices yet" in empty.getvalue()

    review_march(reviewer)
    out = io.StringIO()
    call_command("accuracy_report", "--rebuild", stdout=out)
    output = out.getvalue()
    assert "Rebuilt 9 field review(s)" in output
    assert "Fields accepted unchanged: 67% (6/9)" in output
    assert "invoice_number" in output and "Haverford Office Interiors Ltd" in output


def test_accuracy_api_and_corrections_endpoint(api_client: APIClient, reviewer: Any) -> None:
    review_march(reviewer)
    body = api_client.get("/api/accuracy/").json()
    assert body["overall"] == {
        "key": "all", "reviewed": 9, "corrected": 3, "accepted": 6, "rate": pytest.approx(6 / 9),
    }  # fmt: skip
    assert body["timeline"][0]["filename"] == MARCH
    assert {f["key"] for f in body["by_field"]} >= {"invoice_number", "tax"}

    corrections = api_client.get("/api/corrections/", {"field": "tax"}).json()
    assert corrections["count"] == 1
    assert corrections["results"][0]["extracted_value"] == "2026.00"
    assert api_client.get("/api/corrections/", {"vendor": "nobody"}).json()["count"] == 0


def test_accuracy_dashboard(client: Client, reviewer: Any) -> None:
    client.force_login(reviewer)
    assert "No reviewed invoices yet" in client.get("/review/accuracy/").content.decode()

    review_march(reviewer)
    html = client.get("/review/accuracy/").content.decode()
    assert "<svg" in html and 'class="column"' in html and "<polyline" in html
    assert "67%" in html
    assert "<s>2026.00</s>" in html  # recent corrections: extracted value struck through
    assert "Invoice number" in html
