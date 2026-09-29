"""Stage 1 - turn raw files into page-aware plain text.

Supported inputs: PDFs (text layer via pypdf; pages without a usable text layer are
rasterised and OCRed), images (PNG, JPEG, TIFF - OCR), plain text, and ``.eml``
emails (body plus PDF / image / text attachments). OCR engines live in
:mod:`extraction.ocr`; without one, scans and images are rejected with a clear error.
"""

from __future__ import annotations

import email
import html
import io
import re
import textwrap
from collections.abc import Sequence
from dataclasses import dataclass, field
from email import policy
from email.message import EmailMessage
from pathlib import PurePath

from pypdf import PdfReader
from pypdf.errors import PdfReadError

from extraction.ocr.base import OcrEngine, OcrError, OcrPage, OcrWord
from extraction.ocr.images import (
    DEFAULT_DPI,
    IMAGE_EXTENSIONS,
    load_image_pages,
    render_pdf_pages,
)

PAGE_SEPARATOR = "\n\n"

TEXT_EXTENSIONS = frozenset({".txt", ".text", ".md"})
EMAIL_EXTENSIONS = frozenset({".eml"})
PDF_EXTENSIONS = frozenset({".pdf"})
SUPPORTED_EXTENSIONS = TEXT_EXTENSIONS | EMAIL_EXTENSIONS | PDF_EXTENSIONS | IMAGE_EXTENSIONS

# A PDF page with fewer letters/digits than this has no usable text layer (a scan).
MIN_TEXT_LAYER_CHARS = 20

type PageContent = str | OcrPage


class ParseError(Exception):
    """The document could not be turned into text."""


class UnsupportedDocumentError(ParseError):
    """The file type is not supported, or it needs OCR and OCR is disabled."""


@dataclass(frozen=True, slots=True)
class Page:
    number: int
    text: str
    start: int  # offset of the first character in ParsedDocument.text
    end: int  # exclusive
    words: tuple[OcrWord, ...] = ()  # OCR words; offsets are relative to ``text``
    ocr_confidence: float | None = None  # mean OCR word confidence; None for text layers

    @property
    def is_ocr(self) -> bool:
        return self.ocr_confidence is not None


@dataclass(frozen=True, slots=True)
class ParsedDocument:
    pages: tuple[Page, ...]
    source_type: str
    metadata: dict[str, str] = field(default_factory=dict)

    @classmethod
    def from_page_texts(
        cls,
        contents: Sequence[PageContent],
        source_type: str,
        metadata: dict[str, str] | None = None,
    ) -> ParsedDocument:
        """Lay pages out one after another; OCR pages keep their words and confidence."""
        pages: list[Page] = []
        offset = 0
        for number, content in enumerate(contents, start=1):
            if isinstance(content, OcrPage):
                text, words, confidence = content.text, content.words, content.confidence
            else:
                text, words, confidence = content, (), None
            pages.append(Page(number, text, offset, offset + len(text), words, confidence))
            offset += len(text) + len(PAGE_SEPARATOR)
        return cls(pages=tuple(pages), source_type=source_type, metadata=metadata or {})

    @property
    def text(self) -> str:
        return PAGE_SEPARATOR.join(page.text for page in self.pages)

    @property
    def is_ocr(self) -> bool:
        return any(page.is_ocr for page in self.pages)

    def page_for_offset(self, offset: int) -> int | None:
        for page in self.pages:
            if page.start <= offset < page.end:
                return page.number
        return None


def parse_document(
    filename: str, data: bytes, *, ocr: OcrEngine | None = None, dpi: int = DEFAULT_DPI
) -> ParsedDocument:
    """Dispatch on file extension and return page-aware text.

    ``ocr`` reads images and PDF pages without a text layer; ``dpi`` is the resolution
    PDF pages are rasterised at for it.
    """
    suffix = PurePath(filename).suffix.lower()
    try:
        if suffix in PDF_EXTENSIONS:
            return _document(_pdf_pages(data, ocr, dpi), "pdf")
        if suffix in IMAGE_EXTENSIONS:
            if ocr is None:
                raise UnsupportedDocumentError(
                    f"'{suffix}' images need OCR, which is disabled (OCR_ENGINE=none)."
                )
            return _document([ocr.recognize(image) for image in load_image_pages(data)], "image")
        if suffix in TEXT_EXTENSIONS:
            return ParsedDocument.from_page_texts([_clean(_decode(data))], "text")
        if suffix in EMAIL_EXTENSIONS:
            return _parse_email(data, ocr, dpi)
    except OcrError as exc:
        raise ParseError(f"OCR failed: {exc}") from exc
    raise UnsupportedDocumentError(
        f"Unsupported file type '{suffix or filename}'. "
        f"Supported: {', '.join(sorted(SUPPORTED_EXTENSIONS))}."
    )


def _document(
    contents: list[PageContent], source_type: str, metadata: dict[str, str] | None = None
) -> ParsedDocument:
    """Build the document and describe any OCR that was involved in its metadata."""
    metadata = {"page_count": str(len(contents)), **(metadata or {})}
    ocr_pages = [(n, c) for n, c in enumerate(contents, start=1) if isinstance(c, OcrPage)]
    if ocr_pages:
        if not any(c.strip() if isinstance(c, str) else c.text.strip() for c in contents):
            raise ParseError("OCR found no readable text in the document.")
        if source_type == "pdf":
            source_type = "scanned_pdf"
        confidence = sum(page.confidence for _, page in ocr_pages) / len(ocr_pages)
        metadata |= {
            "ocr_engine": ocr_pages[0][1].engine,
            "ocr_pages": ", ".join(str(n) for n, _ in ocr_pages),
            "ocr_confidence": f"{confidence:.2f}",
        }
    return ParsedDocument.from_page_texts(contents, source_type, metadata)


def _pdf_pages(data: bytes, ocr: OcrEngine | None, dpi: int) -> list[PageContent]:
    """Text-layer pages as text; pages without a usable text layer through OCR."""
    texts = _pdf_page_texts(data)
    scanned = [i for i, text in enumerate(texts) if not _has_text_layer(text)]
    if not scanned:
        return list(texts)
    if ocr is None:
        if len(scanned) == len(texts):
            raise UnsupportedDocumentError(
                "PDF has no text layer (scanned image?) and OCR is disabled (OCR_ENGINE=none)."
            )
        return list(texts)  # mixed PDF without OCR: keep the pages that do have text
    contents: list[PageContent] = list(texts)
    for index, image in zip(scanned, render_pdf_pages(data, scanned, dpi), strict=True):
        contents[index] = ocr.recognize(image)
    return contents


def _has_text_layer(text: str) -> bool:
    return sum(ch.isalnum() for ch in text) >= MIN_TEXT_LAYER_CHARS


def _pdf_page_texts(data: bytes) -> list[str]:
    try:
        reader = PdfReader(io.BytesIO(data))
        # Layout mode keeps table columns on one line, which matters for line items;
        # the wide column padding it produces is then compacted for readability.
        return [
            _clean(_compact_columns(page.extract_text(extraction_mode="layout")))
            for page in reader.pages
        ]
    except (PdfReadError, ValueError) as exc:
        raise ParseError(f"Could not read PDF: {exc}") from exc


def _parse_email(data: bytes, ocr: OcrEngine | None, dpi: int) -> ParsedDocument:
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

    pages: list[PageContent] = [_clean(f"{header_block}\n\n{body}")]
    attachments: list[str] = []
    for part in message.iter_attachments():
        name = part.get_filename() or "attachment"
        payload = part.get_payload(decode=True)
        if not isinstance(payload, bytes):
            continue
        suffix = PurePath(name).suffix.lower()
        if suffix in PDF_EXTENSIONS:
            pages.extend(_pdf_pages(payload, ocr, dpi))
            attachments.append(name)
        elif suffix in IMAGE_EXTENSIONS and ocr is not None:
            pages.extend(ocr.recognize(image) for image in load_image_pages(payload))
            attachments.append(name)
        elif suffix in TEXT_EXTENSIONS:
            pages.append(_clean(_decode(payload)))
            attachments.append(name)

    metadata = {k.lower(): v for k, v in headers.items() if v}
    if attachments:
        metadata["attachments"] = ", ".join(attachments)
    return _document(pages, "email", metadata)


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
