from __future__ import annotations

import hashlib
import sys
from argparse import ArgumentParser
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from rich.console import Console
from rich.table import Table

from documents import services
from documents.feedback import extracted_fields, field_text
from documents.models import Document
from extraction.status import DocumentStatus

FOLDER = Path("sample_data") / "feedback_loop"
FIRST, SECOND = "haverford_invoice_march.pdf", "haverford_invoice_april.pdf"

# What is actually printed on the two fictional invoices (the reviewer's ground truth).
TRUTH: dict[str, dict[str, object]] = {
    FIRST: {
        "vendor_name": "Haverford Office Interiors Ltd", "vendor_tax_id": "GB318447625",
        "invoice_number": "HOI-7731", "issue_date": date(2026, 3, 6),
        "due_date": date(2026, 4, 5), "currency": "GBP", "subtotal": Decimal("1420.00"),
        "tax": Decimal("284.00"), "total": Decimal("1704.00"),
    },
    SECOND: {
        "vendor_name": "Haverford Office Interiors Ltd", "vendor_tax_id": "GB318447625",
        "invoice_number": "HOI-7802", "issue_date": date(2026, 4, 3),
        "due_date": date(2026, 5, 3), "currency": "GBP", "subtotal": Decimal("681.00"),
        "tax": Decimal("136.20"), "total": Decimal("817.20"),
    },
}  # fmt: skip


class Command(BaseCommand):
    help = (
        "Show the correction loop: process a vendor's invoice, apply the reviewer's "
        "corrections and approve, then process the next invoice from the same vendor."
    )

    def add_arguments(self, parser: ArgumentParser) -> None:
        parser.add_argument("--folder", type=Path, default=settings.BASE_DIR / FOLDER)
        parser.add_argument("--reviewer", default="demo", help="Username recorded as reviewer.")

    def handle(self, *args: Any, **options: Any) -> None:
        folder: Path = options["folder"]
        if not (folder / FIRST).is_file() or not (folder / SECOND).is_file():
            raise CommandError(f"{folder} must contain {FIRST} and {SECOND}")
        out = getattr(self.stdout, "_out", sys.stdout)
        console = Console(file=out, width=None if out.isatty() else 120, highlight=False)
        reviewer = get_user_model().objects.filter(username=options["reviewer"]).first()

        first, new = self._process(folder / FIRST)
        before = _values(first)
        if first.status == DocumentStatus.NEEDS_REVIEW:
            fixed = self._review(first, reviewer)
            console.print(f"{FIRST}: reviewer corrected {', '.join(fixed) or 'nothing'}; approved")
        elif new:
            console.print(f"{FIRST}: {first.status} without review - no example to learn from")
        else:
            console.print(f"{FIRST}: already imported ({first.status})")

        second, _ = self._process(folder / SECOND)
        run = second.extraction_runs.first()
        used = ", ".join(run.examples) if run and run.examples else "none"
        console.print(f"{SECOND}: {second.status} (few-shot examples used: {used})")
        console.print(_table(before, _values(second)))

    def _process(self, path: Path) -> tuple[Document, bool]:
        """Import and process a file unless it was imported before; flag new imports."""
        data = path.read_bytes()
        existing = Document.objects.filter(sha256=hashlib.sha256(data).hexdigest()).first()
        if existing is not None:
            return existing, False
        document = services.create_document(path.name, data)
        return services.process_document(document.pk), True

    def _review(self, document: Document, reviewer: Any) -> list[str]:
        """Correct every field that differs from what is printed, then approve."""
        invoice = document.invoice
        fixed = []
        for name, value in TRUTH[FIRST].items():
            if field_text(getattr(invoice, name)) != field_text(value):
                services.update_invoice_field(invoice, name, field_text(value), reviewer)
                fixed.append(name)
        document.refresh_from_db()
        services.approve(document, reviewer, "demo_feedback_loop: checked against the original")
        return fixed


def _values(document: Document) -> dict[str, tuple[str, float]]:
    """What the model extracted (before any reviewer edit), from the audit trail."""
    return extracted_fields(document) or {}


def _table(before: dict[str, tuple[str, float]], after: dict[str, tuple[str, float]]) -> Table:
    table = Table(header_style="bold")
    table.add_column("Field", no_wrap=True)
    table.add_column(f"{FIRST} (no examples)", no_wrap=True)
    table.add_column(f"{SECOND} (with examples)", no_wrap=True)
    for name, truth in TRUTH[SECOND].items():
        table.add_row(
            name,
            _cell(before.get(name), TRUTH[FIRST][name]),
            _cell(after.get(name), truth),
        )
    return table


def _cell(extracted: tuple[str, float] | None, truth: object) -> str:
    if extracted is None:
        return "-"
    value, confidence = extracted
    mark = "[green]ok[/]" if value == field_text(truth) else "[red]wrong[/]"
    return f"{value or '(not found)'}  {confidence:.2f}  {mark}"
