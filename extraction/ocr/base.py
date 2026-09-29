"""OCR data model and the engine interface every backend implements.

An engine turns one page image into an :class:`OcrPage`: plain text laid out line by
line (so the rest of the pipeline treats it like any other text) plus the words it
recognised, each with its character offsets in that text, a confidence and - when the
engine knows it - a bounding box. Offsets let evidence quotes be mapped back to the
words they were read from, which is how OCR uncertainty reaches field confidence.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from PIL import Image

type Box = tuple[int, int, int, int]  # left, top, width, height in page-image pixels


class OcrError(Exception):
    """The engine could not read the image (binary missing, timeout, API error...)."""


@dataclass(frozen=True, slots=True)
class OcrWord:
    start: int  # offset of the first character in OcrPage.text
    end: int  # exclusive
    confidence: float  # 0-1
    box: Box | None = None


@dataclass(frozen=True, slots=True)
class OcrPage:
    text: str
    words: tuple[OcrWord, ...]
    engine: str

    @property
    def confidence(self) -> float:
        """Mean word confidence, weighted by word length (0.0 for an empty page)."""
        total = sum(w.end - w.start for w in self.words)
        if total == 0:
            return 0.0
        return sum(w.confidence * (w.end - w.start) for w in self.words) / total


class OcrEngine(Protocol):
    """Anything that can read one page image."""

    name: str

    def recognize(self, image: Image.Image) -> OcrPage: ...
