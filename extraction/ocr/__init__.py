"""OCR engines for scanned PDFs and photos: ``tesseract`` | ``vision`` | ``none``."""

from __future__ import annotations

from extraction.llm.base import LLMProvider
from extraction.ocr.base import OcrEngine, OcrError, OcrPage, OcrWord

OCR_ENGINE_NAMES = ("tesseract", "vision", "none")


def build_ocr_engine(
    name: str, *, provider: LLMProvider | None = None, languages: str = "eng"
) -> OcrEngine | None:
    """Instantiate an engine by name; ``none`` disables OCR (scans are then rejected).

    ``vision`` reuses the extraction LLM provider, which must accept images.
    """
    name = name.lower()
    if name == "none":
        return None
    if name == "tesseract":
        from extraction.ocr.tesseract import TesseractOcrEngine

        return TesseractOcrEngine(languages)
    if name == "vision":
        if provider is None or provider.name == "fake":
            raise ValueError("OCR_ENGINE=vision needs LLM_PROVIDER=anthropic or openai")
        from extraction.ocr.vision import VisionOcrEngine

        return VisionOcrEngine(provider)
    raise ValueError(f"Unknown OCR engine '{name}'. Choose one of: {', '.join(OCR_ENGINE_NAMES)}")


__all__ = ["OCR_ENGINE_NAMES", "OcrEngine", "OcrError", "OcrPage", "OcrWord", "build_ocr_engine"]
