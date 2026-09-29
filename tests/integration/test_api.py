from __future__ import annotations

import csv
import io
import json
from pathlib import Path
from typing import Any

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from rest_framework.authtoken.models import Token
from rest_framework.test import APIClient

from documents.batch import import_folder
from documents.models import Document

pytestmark = pytest.mark.django_db


def upload(client: APIClient, sample_dir: Path, name: str) -> Any:
    path = sample_dir / name
    with path.open("rb") as handle:
        return client.post("/api/documents/", {"file": handle}, format="multipart")


@pytest.fixture
def run_on_commit(django_capture_on_commit_callbacks: Any) -> Any:
    """Tests run inside a transaction; execute the queued Celery task on 'commit'."""
    return lambda: django_capture_on_commit_callbacks(execute=True)


def test_authentication_required(sample_dir: Path) -> None:
    client = APIClient()
    assert client.get("/api/invoices/").status_code == 401
    assert upload(client, sample_dir, "01_brightline_software_invoice.pdf").status_code == 401


def test_token_authentication(reviewer: Any) -> None:
    token = Token.objects.create(user=reviewer)
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f"Token {token.key}")
    assert client.get("/api/invoices/").status_code == 200


def test_upload_processes_document(
    api_client: APIClient, demo_vendors: Any, sample_dir: Path, run_on_commit: Any
) -> None:
    with run_on_commit() as callbacks:
        response = upload(api_client, sample_dir, "02_northwind_office_supplies.pdf")
    assert response.status_code == 202
    assert len(callbacks) == 1  # processing is queued only after the upload commits
    created = response.json()
    assert response["Location"].endswith(f"/api/documents/{created['id']}/")
    assert created["review_url"].endswith(f"/review/{created['id']}/")

    body = api_client.get(f"/api/documents/{created['id']}/").json()
    assert body["status"] == "approved"
    assert body["invoice"]["vendor"]["name"] == "Northwind Traders Limited"
    assert body["invoice"]["total_base"] == "768.62"
    assert body["invoice"]["line_items"][0]["description"].startswith("A4 copy paper")
    assert body["invoice"]["evidence"]["total"]["found"] is True
    assert body["extraction_runs"][0]["provider"] == "fake"


def test_upload_rejects_unsupported_type(api_client: APIClient) -> None:
    sheet = SimpleUploadedFile("costs.xlsx", b"PK\x03\x04", content_type="application/zip")
    response = api_client.post("/api/documents/", {"file": sheet}, format="multipart")
    assert response.status_code == 400
    assert "Unsupported file type" in response.json()["file"][0]
    assert Document.objects.count() == 0


@pytest.fixture
def all_samples(demo_vendors: Any, text_sample_dir: Path) -> dict[str, Document]:
    report = import_folder(text_sample_dir)
    return {item.path.name: item.document for item in report.items if item.document}


def test_list_and_filter_invoices(api_client: APIClient, all_samples: dict[str, Document]) -> None:
    everything = api_client.get("/api/invoices/").json()
    assert everything["count"] == 6

    review = api_client.get("/api/invoices/", {"status": "needs_review"}).json()
    assert {r["invoice_number"] for r in review["results"]} == {
        "BPC-1007",
        "PH-88213",
        "BLS-2026-0142",
    }

    usd = api_client.get("/api/invoices/", {"currency": "usd"}).json()
    assert usd["count"] == 2

    northwind = api_client.get("/api/invoices/", {"vendor": "northwind"}).json()
    assert [r["vendor_name"] for r in northwind["results"]] == ["Northwind Traders Ltd."]

    march = api_client.get(
        "/api/invoices/", {"issued_from": "2026-03-10", "issued_to": "2026-03-15"}
    )
    assert {r["invoice_number"] for r in march.json()["results"]} == {"NWT-58311", "KFL-88412"}

    documents = api_client.get("/api/documents/", {"status": "approved"}).json()
    assert documents["count"] == 3


def test_export_csv_defaults_to_approved(
    api_client: APIClient, all_samples: dict[str, Document]
) -> None:
    response = api_client.get("/api/invoices/export.csv")
    assert response.status_code == 200
    assert response["Content-Type"].startswith("text/csv")
    assert "attachment" in response["Content-Disposition"]
    rows = list(csv.DictReader(io.StringIO(response.content.decode())))
    assert [r["invoice_number"] for r in rows] == ["BLS-2026-0142", "KFL-88412", "NWT-58311"]
    northwind = rows[2]
    assert northwind["vendor"] == "Northwind Traders Limited"
    assert northwind["vendor_name_on_document"] == "Northwind Traders Ltd."
    assert northwind["total_base"] == "768.62"


def test_export_json_all(api_client: APIClient, all_samples: dict[str, Document]) -> None:
    response = api_client.get("/api/invoices/export.json", {"status": "all"})
    data = json.loads(response.content)
    assert len(data) == 6
    assert {"line_items", "evidence", "confidence"} <= set(data[0])


def test_unknown_export_format_is_404(api_client: APIClient) -> None:
    assert api_client.get("/api/invoices/export.xlsx").status_code == 404


def test_approve_and_reject_via_api(
    api_client: APIClient, all_samples: dict[str, Document]
) -> None:
    bluepeak = all_samples["03_bluepeak_consulting_invoice.pdf"].invoice
    response = api_client.post(f"/api/invoices/{bluepeak.pk}/reject/", {"note": "wrong subtotal"})
    assert response.status_code == 200
    assert response.json()["status"] == "rejected"
    assert response.json()["review_note"] == "wrong subtotal"

    again = api_client.post(f"/api/invoices/{bluepeak.pk}/approve/")
    assert again.status_code == 409

    pinecrest = all_samples["05_pinecrest_hardware_receipt.txt"].invoice
    assert api_client.post(f"/api/invoices/{pinecrest.pk}/approve/").json()["status"] == "approved"


def test_reprocess_endpoint(
    api_client: APIClient, all_samples: dict[str, Document], run_on_commit: Any
) -> None:
    approved = all_samples["01_brightline_software_invoice.pdf"]
    assert api_client.post(f"/api/documents/{approved.pk}/reprocess/").status_code == 409

    review = all_samples["03_bluepeak_consulting_invoice.pdf"]
    with run_on_commit():
        response = api_client.post(f"/api/documents/{review.pk}/reprocess/")
    assert response.status_code == 202
    assert response.json()["status"] == "uploaded"
    body = api_client.get(f"/api/documents/{review.pk}/").json()
    assert body["status"] == "needs_review"
    assert len(body["extraction_runs"]) == 2


def test_openapi_schema(api_client: APIClient) -> None:
    response = api_client.get("/api/schema/")
    assert response.status_code == 200
    assert b"/api/invoices/{id}/approve/" in response.content
