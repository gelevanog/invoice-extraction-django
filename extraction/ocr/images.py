"""Turn uploads into page images: photos/scans (Pillow) and PDF pages (pypdfium2)."""

from __future__ import annotations

import io
from collections.abc import Sequence

import pypdfium2 as pdfium
from PIL import Image, ImageOps, ImageSequence, UnidentifiedImageError

from extraction.ocr.base import OcrError

IMAGE_EXTENSIONS = frozenset({".png", ".jpg", ".jpeg", ".tif", ".tiff"})
DEFAULT_DPI = 300


def load_image_pages(data: bytes) -> list[Image.Image]:
    """Every frame of an image file (multi-page TIFFs have several), upright and RGB."""
    try:
        with Image.open(io.BytesIO(data)) as image:
            # EXIF orientation matters for phone photos taken in portrait mode.
            return [
                ImageOps.exif_transpose(frame.convert("RGB"))
                for frame in ImageSequence.Iterator(image)
            ]
    except (UnidentifiedImageError, Image.DecompressionBombError, OSError) as exc:
        raise OcrError(f"Could not read image: {exc}") from exc


def render_pdf_pages(
    data: bytes, page_indexes: Sequence[int], dpi: int = DEFAULT_DPI
) -> list[Image.Image]:
    """Rasterise the given 0-based PDF pages to grayscale images at ``dpi``."""
    try:
        pdf = pdfium.PdfDocument(data)
    except pdfium.PdfiumError as exc:
        raise OcrError(f"Could not rasterise PDF: {exc}") from exc
    try:
        return [
            pdf[index].render(scale=dpi / 72, grayscale=True).to_pil() for index in page_indexes
        ]
    finally:
        pdf.close()
