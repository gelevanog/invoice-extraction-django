"""Field-level accuracy of the configured LLM on the sample documents.

Processes every document in a folder (recursively) through the normal pipeline with
the configured provider, then scores the model's own output (the validated record of
the last extraction run, before vendor matching) against ``ground_truth.json``:

- header fields: the nine scalar invoice fields, exact after light normalisation
  (case and spacing for text, separators for tax IDs, two decimals for amounts);
- line items: description (fuzzy >= 90), quantity, unit price and amount per position;
- schema validity: valid on the first attempt / after the retry-with-feedback loop.

Run it on a throwaway database so earlier documents do not trigger duplicate checks,
e.g. ``DATABASE_URL=sqlite:////tmp/eval.sqlite3 MEDIA_ROOT=/tmp/eval-media``.
"""

from __future__ import annotations

import json
import sys
import time
from argparse import ArgumentParser
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from rapidfuzz import fuzz
from rich.console import Console
from rich.table import Table

from documents import services
from documents.batch import discover
from documents.models import Document, ExtractionRun
from extraction.extract import parse_model_output
from extraction.schemas import Invoice as InvoiceSchema

HEADER_FIELDS = (
    "vendor_name",
    "vendor_tax_id",
    "invoice_number",
    "issue_date",
    "due_date",
    "currency",
    "subtotal",
    "tax",
    "total",
)
AMOUNT_FIELDS = frozenset({"subtotal", "tax", "total", "quantity", "unit_price", "amount"})
LINE_FIELDS = ("description", "quantity", "unit_price", "amount")
# Provider errors worth another try (transient gateway refusals, dropped connections).
TRANSIENT_ERRORS = ("403", "Request not allowed", "connection error", "Connection error", "502")


class Command(BaseCommand):
    help = "Process the sample documents with the configured LLM and score field accuracy."

    def add_arguments(self, parser: ArgumentParser) -> None:
        parser.add_argument("folder", type=Path, nargs="?", default=Path("sample_data"))
        parser.add_argument(
            "--truth", type=Path, default=None, help="Default: <folder>/ground_truth.json"
        )
        parser.add_argument("--out", type=Path, help="Write the full report as JSON here.")
        parser.add_argument("--label", default="", help="Name of this run in the report.")
        parser.add_argument(
            "--only", nargs="*", default=[], help="Only these file names (default: all)."
        )
        parser.add_argument(
            "--retries",
            type=int,
            default=2,
            help="Re-run a document whose LLM call failed with a transient error.",
        )

    def handle(self, *args: Any, **options: Any) -> None:
        folder: Path = options["folder"]
        truth_path: Path = options["truth"] or folder / "ground_truth.json"
        if not truth_path.is_file():
            raise CommandError(f"Ground truth not found: {truth_path}")
        truth: dict[str, Any] = json.loads(truth_path.read_text())
        paths = [p for p in discover(folder, recursive=True) if p.name in truth]
        if options["only"]:
            paths = [p for p in paths if p.name in options["only"]]
        if not paths:
            raise CommandError("No documents with ground truth found.")

        cfg = settings.DOCEXTRACT
        out = getattr(self.stdout, "_out", sys.stdout)
        console = Console(file=out, width=None if out.isatty() else 160, highlight=False)
        provider = services.build_llm_provider()
        console.print(
            f"Evaluating {len(paths)} document(s) with [bold]{provider.name}/{provider.model}[/]"
        )

        documents = []
        started = time.perf_counter()
        for path in paths:
            item_started = time.perf_counter()
            document = services.create_document(path.name, path.read_bytes())
            document = services.process_document(document.pk)
            tries = 0
            while is_transient(document) and tries < options["retries"]:
                tries += 1
                time.sleep(5 * tries)
                services.reset_for_reprocessing(document)
                document = services.process_document(document.pk)
            result = score_document(document, truth[path.name])
            result["seconds"] = round(time.perf_counter() - item_started, 2)
            result["transient_retries"] = tries
            documents.append(result)
            console.print(
                f"  {path.name}: {document.status}, header {result['header_correct']}/"
                f"{result['header_total']}, lines {result['line_fields_correct']}/"
                f"{result['line_fields_total']} ({result['seconds']} s)"
            )

        report = {
            "label": options["label"] or provider.model,
            "provider": provider.name,
            "model": provider.model,
            "response_format": cfg.get("LLM_RESPONSE_FORMAT", "json_schema"),
            "ocr_engine": cfg["OCR_ENGINE"],
            "created_at": datetime.now(UTC).isoformat(timespec="seconds"),
            "seconds": round(time.perf_counter() - started, 2),
            "summary": summarize(documents),
            "documents": documents,
        }
        console.print(build_table(report))
        if options["out"]:
            options["out"].parent.mkdir(parents=True, exist_ok=True)
            options["out"].write_text(json.dumps(report, indent=2, default=str) + "\n")
            console.print(f"Report written to {options['out']}")


def is_transient(document: Document) -> bool:
    return document.status == "failed" and any(m in document.error for m in TRANSIENT_ERRORS)


def score_document(document: Document, expected: dict[str, Any]) -> dict[str, Any]:
    runs = list(ExtractionRun.objects.filter(document=document).order_by("id"))
    run = runs[-1] if runs else None
    record = parse_model_output(InvoiceSchema, run.raw_output) if run and run.succeeded else None
    issues = sorted({i.code for i in document.issues.all()})
    result: dict[str, Any] = {
        "file": document.original_filename,
        "status": document.status,
        "error": document.error,
        "issues": issues,
        "routing_reasons": document.routing_reasons,
        "schema_valid": record is not None,
        "attempts": run.attempts if run else 0,
        "valid_first_attempt": bool(run and run.succeeded and run.attempts == 1),
        "input_tokens": run.input_tokens if run else 0,
        "output_tokens": run.output_tokens if run else 0,
        "llm_latency_ms": run.latency_ms if run else 0,
        "model_served": run.model if run else "",
        "ocr": document.metadata.get("ocr_engine", ""),
        "fields": {},
        "line_items": {"expected": len(expected["line_items"]), "extracted": 0, "fields": []},
    }
    header_correct = 0
    for name in HEADER_FIELDS:
        got = getattr(record, name).value if record else None
        ok = record is not None and same_value(name, got, expected[name])
        header_correct += ok
        result["fields"][name] = {
            "expected": expected[name],
            "got": None if got is None else str(got),
            "confidence": getattr(record, name).confidence if record else None,
            "correct": ok,
        }
    lines_correct = 0
    items = record.line_items if record else []
    result["line_items"]["extracted"] = len(items)
    for index, want in enumerate(expected["line_items"]):
        item = items[index] if index < len(items) else None
        row = {}
        for name in LINE_FIELDS:
            got = getattr(item, name) if item else None
            ok = item is not None and same_value(name, got, want[name])
            lines_correct += ok
            row[name] = {"expected": want[name], "got": None if got is None else str(got)}
            row[name]["correct"] = ok
        result["line_items"]["fields"].append(row)
    result["header_correct"] = header_correct
    result["header_total"] = len(HEADER_FIELDS)
    result["line_fields_correct"] = lines_correct
    result["line_fields_total"] = len(LINE_FIELDS) * len(expected["line_items"])
    result["line_count_ok"] = len(items) == len(expected["line_items"])
    return result


def same_value(name: str, got: object, want: object) -> bool:
    if want is None or got is None:
        return want is None and (got is None or str(got).strip() == "")
    if name in AMOUNT_FIELDS:
        try:
            return Decimal(str(got)).quantize(Decimal("0.001")) == Decimal(str(want)).quantize(
                Decimal("0.001")
            )
        except InvalidOperation:
            return False
    if name == "vendor_tax_id":
        return _alnum(got) == _alnum(want)
    if name == "description":
        return fuzz.ratio(_text(got), _text(want)) >= 90
    if name == "vendor_name":
        return _text(got).rstrip(".") == _text(want).rstrip(".")
    return _text(got) == _text(want)


def _text(value: object) -> str:
    return " ".join(str(value).split()).casefold()


def _alnum(value: object) -> str:
    return "".join(ch for ch in str(value) if ch.isalnum()).upper()


def summarize(documents: list[dict[str, Any]]) -> dict[str, Any]:
    def total(key: str) -> int:
        return sum(int(d[key]) for d in documents)

    per_field = {
        name: sum(d["fields"][name]["correct"] for d in documents) for name in HEADER_FIELDS
    }
    statuses: dict[str, int] = {}
    for d in documents:
        statuses[d["status"]] = statuses.get(d["status"], 0) + 1
    header_total = total("header_total")
    line_total = total("line_fields_total")
    return {
        "documents": len(documents),
        "header_correct": total("header_correct"),
        "header_total": header_total,
        "header_accuracy": round(total("header_correct") / header_total, 4) if header_total else 0,
        "docs_all_header_correct": sum(d["header_correct"] == d["header_total"] for d in documents),
        "line_fields_correct": total("line_fields_correct"),
        "line_fields_total": line_total,
        "line_accuracy": round(total("line_fields_correct") / line_total, 4) if line_total else 0,
        "line_count_ok": total("line_count_ok"),
        "schema_valid": total("schema_valid"),
        "valid_first_attempt": total("valid_first_attempt"),
        "attempts": total("attempts"),
        "input_tokens": total("input_tokens"),
        "output_tokens": total("output_tokens"),
        "llm_latency_ms": total("llm_latency_ms"),
        "evidence_not_found": sum("evidence_not_found" in d["issues"] for d in documents),
        "transient_retries": total("transient_retries"),
        "per_field": per_field,
        "statuses": statuses,
    }


def build_table(report: dict[str, Any]) -> Table:
    table = Table(header_style="bold", title=f"{report['label']} ({report['model']})")
    for column in ("File", "Status", "Header", "Lines", "Attempts", "Tokens in/out", "LLM ms"):
        table.add_column(column, no_wrap=True)
    for d in report["documents"]:
        table.add_row(
            d["file"],
            d["status"],
            f"{d['header_correct']}/{d['header_total']}",
            f"{d['line_fields_correct']}/{d['line_fields_total']}",
            str(d["attempts"]),
            f"{d['input_tokens']}/{d['output_tokens']}",
            str(d["llm_latency_ms"]),
        )
    s = report["summary"]
    table.add_row(
        "TOTAL",
        ", ".join(f"{v} {k}" for k, v in sorted(s["statuses"].items())),
        f"{s['header_correct']}/{s['header_total']}",
        f"{s['line_fields_correct']}/{s['line_fields_total']}",
        str(s["attempts"]),
        f"{s['input_tokens']}/{s['output_tokens']}",
        str(s["llm_latency_ms"]),
    )
    return table
