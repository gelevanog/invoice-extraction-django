"""Local OCR with Tesseract (via pytesseract) keeping word boxes and confidences."""

from __future__ import annotations

import shutil
from collections.abc import Callable
from typing import Any

import pytesseract
from PIL import Image, ImageChops, ImageFilter, ImageOps

from extraction.ocr.base import OcrError, OcrPage
from extraction.ocr.layout import PageBuilder, RecognizedWord

type LineKey = tuple[int, int, int]  # block, paragraph, line


class TesseractOcrEngine:
    """Runs the ``tesseract`` binary; needs ``apt install tesseract-ocr`` (+ language packs).

    ``image_to_data`` returns every word with its block/paragraph/line numbers, box and
    a 0-100 confidence. Lines are re-assembled with :class:`PageBuilder` so columns stay
    on one line, and confidences are kept per word for evidence-level scoring.

    Page segmentation mode 6 ("one uniform block") keeps each table row on one line
    even on skewed photos; the automatic mode splits invoice columns into separate
    blocks, which tears line items apart.
    """

    name = "tesseract"

    def __init__(
        self,
        languages: str = "eng",
        *,
        page_segmentation_mode: int = 6,
        timeout: float = 120.0,
        image_to_data: Callable[..., dict[str, list[Any]]] | None = None,
    ) -> None:
        self.languages = languages
        self.page_segmentation_mode = page_segmentation_mode
        self.timeout = timeout
        self._image_to_data = image_to_data or pytesseract.image_to_data

    @staticmethod
    def is_available() -> bool:
        return shutil.which(pytesseract.pytesseract.tesseract_cmd) is not None

    def recognize(self, image: Image.Image) -> OcrPage:
        try:
            data = self._image_to_data(
                _prepare(image),
                lang=self.languages,
                config=f"--psm {self.page_segmentation_mode}",
                output_type=pytesseract.Output.DICT,
                timeout=self.timeout,
            )
        except pytesseract.TesseractNotFoundError as exc:
            raise OcrError(
                "Tesseract is not installed (apt install tesseract-ocr) - "
                "install it or set OCR_ENGINE=vision / none"
            ) from exc
        except (pytesseract.TesseractError, RuntimeError) as exc:
            # RuntimeError is what pytesseract raises on timeout.
            raise OcrError(f"Tesseract failed: {exc}") from exc
        return page_from_tesseract_data(data)


def _prepare(image: Image.Image) -> Image.Image:
    """Flat-field the page: remove shadows, vignetting and background, keep the ink.

    The background is estimated at quarter resolution with a max filter (which erases
    thin dark strokes) and subtracted, so uneven light in phone photos and dark desk
    borders no longer swamp faded print. Costs a few milliseconds per page.
    """
    gray = ImageOps.grayscale(image)
    small = gray.resize((max(1, gray.width // 4), max(1, gray.height // 4)))
    background = (
        small.filter(ImageFilter.MaxFilter(7)).filter(ImageFilter.GaussianBlur(2)).resize(gray.size)
    )
    ink = ImageChops.subtract(background, gray)
    return ImageOps.autocontrast(ImageOps.invert(ink), cutoff=1)


def page_from_tesseract_data(data: dict[str, list[Any]]) -> OcrPage:
    """Build an :class:`OcrPage` from pytesseract's ``image_to_data`` dictionary."""
    lines: dict[LineKey, list[RecognizedWord]] = {}
    for i, text in enumerate(data["text"]):
        confidence = float(data["conf"][i])
        if not str(text).strip() or confidence < 0:  # -1 marks layout rows, not words
            continue
        key = (int(data["block_num"][i]), int(data["par_num"][i]), int(data["line_num"][i]))
        box = (
            int(data["left"][i]),
            int(data["top"][i]),
            int(data["width"][i]),
            int(data["height"][i]),
        )
        lines.setdefault(key, []).append(RecognizedWord(str(text), confidence / 100, box))

    builder = PageBuilder(TesseractOcrEngine.name)
    previous_block: int | None = None
    for (block, _, _), words in lines.items():  # dicts keep Tesseract's reading order
        if previous_block is not None and block != previous_block:
            builder.new_block()
        builder.add_words(sorted(words, key=lambda w: w.box[0] if w.box else 0))
        previous_block = block
    return builder.build()
