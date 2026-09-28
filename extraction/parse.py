"""Stage 1 - turn raw files into page-aware plain text.

Supported inputs: text-layer PDFs (via pypdf), plain text, and ``.eml`` emails (body
plus any PDF / text attachments). Scanned images need OCR, which is out of scope.
"""

from __future__ import annotations

import email
import html
import io
import re
import textwrap
from dataclasses import dataclass, field
from email import policy
from email.message import EmailMessage
from pathlib import PurePath

from pypdf import PdfReader
from pypdf.errors import PdfReadError

PAGE_SEPARATOR = "\n\n"

TEXT_EXTENSIONS = frozenset({".txt", ".text", ".md"})
EMAIL_EXTENSIONS = frozenset({".eml"})
PDF_EXTENSIONS = frozenset({".pdf"})
SUPPORTED_EXTENSIONS = TEXT_EXTENSIONS | EMAIL_EXTENSIONS | PDF_EXTENSIONS


class ParseError(Exception):
    """The document could not be turned into text."""


class UnsupportedDocumentError(ParseError):
    """The file type is not supported (e.g. images, which would need OCR)."""


@dataclass(frozen=True, slots=True)
class Page:
    number: int
    text: str
    start: int  # offset of the first character in ParsedDocument.text
    end: int  # exclusive


@dataclass(frozen=True, slots=True)
class ParsedDocument:
    pages: tuple[Page, ...]
    source_type: str
    metadata: dict[str, str] = field(default_factory=dict)

    @classmethod
    def from_page_texts(
        cls, texts: list[str], source_type: str, metadata: dict[str, str] | None = None
    ) -> ParsedDocument:
        pages: list[Page] = []
        offset = 0
        for number, text in enumerate(texts, start=1):
            pages.append(Page(number=number, text=text, start=offset, end=offset + len(text)))
            offset += len(text) + len(PAGE_SEPARATOR)
        return cls(pages=tuple(pages), source_type=source_type, metadata=metadata or {})

    @property
    def text(self) -> str:
        return PAGE_SEPARATOR.join(page.text for page in self.pages)

    def page_for_offset(self, offset: int) -> int | None:
        for page in self.pages:
            if page.start <= offset < page.end:
                return page.number
        return None


def parse_document(filename: str, data: bytes) -> ParsedDocument:
    """Dispatch on file extension and return page-aware text."""
    suffix = PurePath(filename).suffix.lower()
    if suffix in PDF_EXTENSIONS:
        texts = _pdf_page_texts(data)
        return ParsedDocument.from_page_texts(texts, "pdf", {"page_count": str(len(texts))})
    if suffix in TEXT_EXTENSIONS:
        return ParsedDocument.from_page_texts([_clean(_decode(data))], "text")
    if suffix in EMAIL_EXTENSIONS:
        return _parse_email(data)
    raise UnsupportedDocumentError(
        f"Unsupported file type '{suffix or filename}'. Supported: "
        f"{', '.join(sorted(SUPPORTED_EXTENSIONS))} (images/scans need OCR, not implemented)."
    )


def _pdf_page_texts(data: bytes) -> list[str]:
    try:
        reader = PdfReader(io.BytesIO(data))
        # Layout mode keeps table columns on one line, which matters for line items;
        # the wide column padding it produces is then compacted for readability.
        texts = [
            _clean(_compact_columns(page.extract_text(extraction_mode="layout")))
            for page in reader.pages
        ]
    except (PdfReadError, ValueError) as exc:
        raise ParseError(f"Could not read PDF: {exc}") from exc
    if not any(texts):
        raise UnsupportedDocumentError("PDF has no text layer (scanned image?) - OCR required.")
    return texts


def _parse_email(data: bytes) -> ParsedDocument:
    message = email.message_from_bytes(data, policy=policy.default)
    if not isinstance(message, EmailMessage):  # pragma: no cover - policy.default guarantees it
        raise ParseError("Could not parse email message")

    headers = {name: str(message.get(name, "")) for name in ("From", "To", "Subject", "Date")}
    header_block = "\n".join(f"{name}: {value}" for name, value in headers.items() if value)

    body_part = message.get_body(preferencelist=("plain", "html"))
    body = ""
    if body_part is not None:
        content = body_part.get_content()
        body = _html_to_text(content) if body_part.get_content_type() == "text/html" else content

    pages = [_clean(f"{header_block}\n\n{body}")]
    attachments: list[str] = []
    for part in message.iter_attachments():
        name = part.get_filename() or "attachment"
        payload = part.get_payload(decode=True)
        if not isinstance(payload, bytes):
            continue
        suffix = PurePath(name).suffix.lower()
        if suffix in PDF_EXTENSIONS:
            pages.extend(_pdf_page_texts(payload))
            attachments.append(name)
        elif suffix in TEXT_EXTENSIONS:
            pages.append(_clean(_decode(payload)))
            attachments.append(name)

    metadata = {k.lower(): v for k, v in headers.items() if v}
    if attachments:
        metadata["attachments"] = ", ".join(attachments)
    return ParsedDocument.from_page_texts(pages, "email", metadata)


def _decode(data: bytes) -> str:
    for encoding in ("utf-8-sig", "cp1252"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("latin-1")


_BLANK_RUNS = re.compile(r"\n{3,}")
_WIDE_GAPS = re.compile(r" {4,}")
_TAGS = re.compile(r"<[^>]+>")
_BLOCK_TAGS = re.compile(r"</?(p|div|br|tr|li|h\d)[^>]*>", re.IGNORECASE)


def _clean(text: str) -> str:
    lines = [line.rstrip() for line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n")]
    cleaned = textwrap.dedent("\n".join(lines)).strip("\n")
    return _BLANK_RUNS.sub("\n\n", cleaned)


def _compact_columns(text: str) -> str:
    """Shrink runs of 4+ spaces to 4: columns stay visibly separated, lines stay short."""
    return _WIDE_GAPS.sub("    ", text)


def _html_to_text(markup: str) -> str:
    return html.unescape(_TAGS.sub("", _BLOCK_TAGS.sub("\n", markup)))
