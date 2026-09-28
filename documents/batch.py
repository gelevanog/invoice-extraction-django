"""Batch import of a folder of documents (used by ``process_folder`` and ``seed_demo``)."""

from __future__ import annotations

import hashlib
import time
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from documents import services
from documents.models import Document
from documents.tasks import process_document_task
from extraction.parse import SUPPORTED_EXTENSIONS


@dataclass(frozen=True, slots=True)
class BatchItem:
    path: Path
    document: Document | None
    seconds: float = 0.0
    skipped_reason: str | None = None


@dataclass(slots=True)
class BatchReport:
    items: list[BatchItem] = field(default_factory=list)
    seconds: float = 0.0

    @property
    def status_counts(self) -> Counter[str]:
        return Counter(i.document.status for i in self.items if i.document and not i.skipped_reason)

    @property
    def skipped(self) -> int:
        return sum(1 for i in self.items if i.skipped_reason)


def discover(folder: Path, recursive: bool = False) -> list[Path]:
    pattern = "**/*" if recursive else "*"
    return sorted(
        p for p in folder.glob(pattern) if p.is_file() and p.suffix.lower() in SUPPORTED_EXTENSIONS
    )


def import_folder(
    folder: Path,
    *,
    recursive: bool = False,
    force: bool = False,
    run_async: bool = False,
    on_item: Callable[[BatchItem], None] | None = None,
) -> BatchReport:
    """Create a Document per supported file and process it (inline unless ``run_async``).

    Files whose SHA-256 was already imported are skipped unless ``force`` is set, which
    makes the command safe to re-run (e.g. on every container start).
    """
    report = BatchReport()
    started = time.perf_counter()
    for path in discover(folder, recursive):
        data = path.read_bytes()
        digest = hashlib.sha256(data).hexdigest()
        existing = Document.objects.filter(sha256=digest).order_by("id").first()
        if existing and not force:
            item = BatchItem(path, existing, skipped_reason=f"already imported as #{existing.pk}")
        else:
            item_started = time.perf_counter()
            document = services.create_document(path.name, data)
            if run_async:
                process_document_task.delay(document.pk)
            else:
                document = services.process_document(document.pk)
            item = BatchItem(path, document, time.perf_counter() - item_started)
        report.items.append(item)
        if on_item:
            on_item(item)
    report.seconds = time.perf_counter() - started
    return report
