"""``manage.py eval_extraction``: field-level scoring against sample ground truth."""

from __future__ import annotations

import io
import json
from pathlib import Path
from typing import Any

import pytest
from django.conf import settings
from django.core.management import call_command

from documents.management.commands.eval_extraction import same_value

pytestmark = pytest.mark.django_db

TEXT_ONLY = [
    "01_brightline_software_invoice.pdf",
    "04_kestrel_freight_email.eml",
    "haverford_invoice_march.pdf",
]


@pytest.mark.parametrize(
    ("name", "got", "want", "ok"),
    [
        ("vendor_name", "Northwind Traders Ltd", "Northwind Traders Ltd.", True),
        ("vendor_name", "PINECREST  Hardware", "PINECREST HARDWARE", True),
        ("vendor_name", "Northwind Traders Limited", "Northwind Traders Ltd.", False),
        ("vendor_tax_id", "GB123456789", "GB 123 4567 89", True),
        ("vendor_tax_id", "842917365", "84-2917365", True),
        ("total", "1592.2", "1592.22", False),
        ("total", "1592.220", "1592.22", True),
        ("quantity", None, "1", False),
        ("due_date", None, None, True),
        ("due_date", "2026-04-01", None, False),
        (
            "description",
            "Pallet transport Rotterdam-Berlin",
            "Pallet transport Rotterdam - Berlin",
            True,
        ),
    ],
)
def test_same_value(name: str, got: object, want: object, ok: bool) -> None:
    assert same_value(name, got, want) is ok


def test_eval_command_scores_the_fake_provider(demo_vendors: Any, tmp_path: Path) -> None:
    out = tmp_path / "report.json"
    call_command(
        "eval_extraction",
        str(settings.BASE_DIR / "sample_data"),
        "--only",
        *TEXT_ONLY,
        "--out",
        str(out),
        stdout=io.StringIO(),
    )
    report = json.loads(out.read_text())
    docs = {d["file"]: d for d in report["documents"]}
    assert set(docs) == set(TEXT_ONLY)
    assert docs["01_brightline_software_invoice.pdf"]["header_correct"] == 9
    assert docs["01_brightline_software_invoice.pdf"]["line_fields_correct"] == 12
    # The fake does not know "Document no." / "Tax point" / "Payment due" (see the README).
    march = docs["haverford_invoice_march.pdf"]
    assert march["header_correct"] == 6
    assert not march["fields"]["invoice_number"]["correct"]
    summary = report["summary"]
    assert summary["documents"] == 3 and summary["schema_valid"] == 3
    assert summary["header_correct"] == 9 + 9 + 6 and summary["header_total"] == 27
