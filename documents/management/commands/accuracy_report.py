from __future__ import annotations

import sys
from argparse import ArgumentParser
from typing import Any

from django.core.management.base import BaseCommand
from rich.console import Console
from rich.table import Table

from documents.accuracy import Accuracy, build_report
from documents.feedback import rebuild_reviews


class Command(BaseCommand):
    help = "Report extraction accuracy (share of fields reviewers accepted unchanged)."

    def add_arguments(self, parser: ArgumentParser) -> None:
        parser.add_argument(
            "--rebuild",
            action="store_true",
            help="Recompute field reviews from all human-approved invoices first.",
        )
        parser.add_argument("--vendor", help="Only this vendor (name contains).")

    def handle(self, *args: Any, **options: Any) -> None:
        out = getattr(self.stdout, "_out", sys.stdout)
        console = Console(file=out, width=None if out.isatty() else 120, highlight=False)
        if options["rebuild"]:
            console.print(f"Rebuilt {rebuild_reviews()} field review(s)")

        report = build_report(vendor=options["vendor"])
        if not report.overall.reviewed:
            console.print("No reviewed invoices yet - approve some in the review queue.")
            return
        console.print(
            f"{report.invoices_reviewed} invoice(s) reviewed by people, "
            f"{report.auto_approved} auto-approved (not counted). "
            f"Fields accepted unchanged: {_rate(report.overall)} "
            f"({report.overall.accepted}/{report.overall.reviewed})"
        )
        console.print(_table("Field", report.by_field))
        console.print(_table("Vendor", report.by_vendor))


def _rate(accuracy: Accuracy) -> str:
    return f"{accuracy.rate:.0%}" if accuracy.rate is not None else "-"


def _table(title: str, rows: list[Accuracy]) -> Table:
    table = Table(header_style="bold")
    table.add_column(title, no_wrap=True)
    table.add_column("Reviewed", justify="right")
    table.add_column("Corrected", justify="right")
    table.add_column("Accuracy", justify="right")
    for row in rows:
        table.add_row(row.key, str(row.reviewed), str(row.corrected), _rate(row))
    return table
