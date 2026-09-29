"""Assemble recognised words into page text while tracking every word's offsets.

The text mimics what the PDF text-layer parser produces: one line per printed line,
wide horizontal gaps (table columns) rendered as a 4-space run, and a blank line
between text blocks. Keeping that shape means LLM prompts, the fake provider's
heuristics and evidence matching behave the same for scans and born-digital PDFs.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass

from extraction.ocr.base import Box, OcrPage, OcrWord

COLUMN_GAP = "    "
# A horizontal gap wider than this many average character widths starts a new column.
COLUMN_GAP_CHARS = 1.8

_TOKENS = re.compile(r"\S+")


@dataclass(frozen=True, slots=True)
class RecognizedWord:
    text: str
    confidence: float  # 0-1
    box: Box | None = None


class PageBuilder:
    """Accumulates lines and blocks, then builds an :class:`OcrPage`."""

    def __init__(self, engine: str) -> None:
        self.engine = engine
        self._parts: list[str] = []
        self._words: list[OcrWord] = []
        self._offset = 0
        self._block_open = False

    def new_block(self) -> None:
        self._block_open = False

    def add_words(self, words: Sequence[RecognizedWord]) -> None:
        """Add one printed line from positioned words (gaps become spaces or columns)."""
        words = [w for w in words if w.text.strip()]
        if not words:
            return
        self._start_line()
        char_width = _average_char_width(words)
        previous: RecognizedWord | None = None
        for word in words:
            if previous is not None:
                self._emit(_separator(previous, word, char_width))
            self._emit(word.text.strip(), word.confidence, word.box)
            previous = word

    def add_text(self, line: str, confidence: float) -> None:
        """Add one line of already laid-out text (every token gets the line confidence)."""
        line = line.rstrip()
        if not line.strip():
            self.new_block()
            return
        self._start_line()
        base = self._offset
        self._emit(line)
        self._words.extend(
            OcrWord(base + m.start(), base + m.end(), confidence) for m in _TOKENS.finditer(line)
        )

    def build(self) -> OcrPage:
        return OcrPage(text="".join(self._parts), words=tuple(self._words), engine=self.engine)

    # -- internals ----------------------------------------------------------------
    def _start_line(self) -> None:
        if self._parts:
            self._emit("\n" if self._block_open else "\n\n")
        self._block_open = True

    def _emit(self, text: str, confidence: float | None = None, box: Box | None = None) -> None:
        if confidence is not None:
            self._words.append(OcrWord(self._offset, self._offset + len(text), confidence, box))
        self._parts.append(text)
        self._offset += len(text)


def _average_char_width(words: Sequence[RecognizedWord]) -> float | None:
    boxed = [w for w in words if w.box is not None]
    chars = sum(len(w.text.strip()) for w in boxed)
    if not chars:
        return None
    return sum(w.box[2] for w in boxed if w.box is not None) / chars


def _separator(left: RecognizedWord, right: RecognizedWord, char_width: float | None) -> str:
    if left.box is None or right.box is None or char_width is None:
        return " "
    gap = right.box[0] - (left.box[0] + left.box[2])
    return COLUMN_GAP if gap > COLUMN_GAP_CHARS * char_width else " "
