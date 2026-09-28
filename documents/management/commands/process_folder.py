from __future__ import annotations

import sys
from argparse import ArgumentParser
from pathlib import Path
from typing import Any

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from rich.console import Console
from rich.table import Table

from documents.batch import BatchReport, import_folder
from documents.models import Invoice

STATUS_STYLE = {
    "approved": "green",
    "needs_review": "yellow",
    "rejected": "red",
    "failed": "bold red",
}


class Command(BaseCommand):
    help = "Import and process every supported document in a folder, then print a summary."

    def add_arguments(self, parser: ArgumentParser) -> None:
        parser.add_argument("folder", type=Path)
        parser.add_argument("--recursive", action="store_true", help="Include subfolders.")
        parser.add_argument("--force", action="store_true", help="Re-import files already seen.")
        parser.add_argument(
            "--async",
            dest="run_async",
            action="store_true",
            help="Queue documents on Celery instead of processing inline.",
        )

    def handle(self, *args: Any, **options: Any) -> None:
        folder: Path = options["folder"]
        if not folder.is_dir():
            raise CommandError(f"{folder} is not a directory")
        out = getattr(self.stdout, "_out", sys.stdout)
        console = Console(file=out, width=None if out.isatty() else 180, highlight=False)
        provider = settings.DOCEXTRACT["LLM_PROVIDER"]
        console.print(f"Processing [bold]{folder}[/] with LLM provider [bold]{provider}[/]")

        with console.status("Running pipeline..."):
            report = import_folder(
                folder,
                recursive=options["recursive"],
                force=options["force"],
                run_async=options["run_async"],
            )
        if not report.items:
            console.print("No supported documents found.")
            return
        console.print(build_table(report, settings.DOCEXTRACT["BASE_CURRENCY"]))
        console.print(summary_line(report))


def build_table(report: BatchReport, base_currency: str) -> Table:
    table = Table(header_style="bold", show_lines=False)
    table.add_column("File", no_wrap=True)
    table.add_column("Status", no_wrap=True)
    table.add_column("Vendor", no_wrap=True)
    table.add_column("Invoice #", no_wrap=True)
    table.add_column("Total", justify="right", no_wrap=True)
    table.add_column(f"Total {base_currency}", justify="right", no_wrap=True)
    table.add_column("Issues (E/W)", justify="center")
    table.add_column("Min conf.", justify="right")
    table.add_column("Time", justify="right", no_wrap=True)

    for item in report.items:
        document = item.document
        if document is None:
            continue
        invoice = Invoice.objects.filter(document=document).select_related("vendor").first()
        errors = document.issues.filter(severity="error").count()
        warnings = document.issues.filter(severity="warning").count()
        status = item.skipped_reason or document.status
        style = STATUS_STYLE.get(document.status, "") if not item.skipped_reason else "dim"
        table.add_row(
            item.path.name,
            f"[{style}]{status}[/]" if style else status,
            (invoice.display_vendor if invoice else "") or "-",
            (invoice.invoice_number if invoice else "") or "-",
            f"{invoice.total:,.2f} {invoice.currency}"
            if invoice and invoice.total is not None
            else "-",
            f"{invoice.total_base:,.2f}" if invoice and invoice.total_base is not None else "-",
            f"{errors}/{warnings}",
            f"{invoice.min_confidence:.2f}" if invoice else "-",
            f"{item.seconds * 1000:.0f} ms" if not item.skipped_reason else "-",
        )
    return table


def summary_line(report: BatchReport) -> str:
    counts = report.status_counts
    parts = [f"{count} {status}" for status, count in sorted(counts.items())]
    if report.skipped:
        parts.append(f"{report.skipped} skipped")
    processed = len(report.items) - report.skipped
    return f"Processed {processed} document(s) in {report.seconds:.2f}s: " + ", ".join(parts)
