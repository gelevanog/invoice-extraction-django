"""OCR: layout assembly, engines (stubbed binary / LLM), parse integration, confidence."""

from __future__ import annotations

import base64
import io
import json
from email.message import EmailMessage
from types import SimpleNamespace
from typing import Any

import pytesseract
import pytest
from PIL import Image
from reportlab.pdfgen import canvas

from extraction.evidence import cap_confidence_to_ocr, locate, resolve_invoice_evidence
from extraction.issues import Severity
from extraction.llm.anthropic_provider import AnthropicProvider
from extraction.llm.base import ChatMessage, ImageInput, JsonRequest, LLMError, LLMResponse
from extraction.llm.openai_provider import OpenAIProvider
from extraction.ocr import OcrError, OcrPage, build_ocr_engine
from extraction.ocr.layout import COLUMN_GAP, PageBuilder, RecognizedWord
from extraction.ocr.tesseract import TesseractOcrEngine, page_from_tesseract_data
from extraction.ocr.vision import VISION_SYSTEM_PROMPT, PageTranscription, VisionOcrEngine
from extraction.parse import ParseError, UnsupportedDocumentError, parse_document
from extraction.route import Route, RoutingConfig, route
from extraction.schemas import Evidence
from extraction.validate import ValidationContext, check_ocr_quality
from tests.factories import field, make_invoice


def ocr_page(*lines: tuple[str, float]) -> OcrPage:
    builder = PageBuilder("stub")
    for text, confidence in lines:
        builder.add_text(text, confidence)
    return builder.build()


class StubOcrEngine:
    """Returns canned pages in order and records the images it was given."""

    name = "stub"

    def __init__(self, *pages: OcrPage) -> None:
        self.pages = list(pages)
        self.images: list[Image.Image] = []

    def recognize(self, image: Image.Image) -> OcrPage:
        self.images.append(image)
        return self.pages.pop(0)


def png_bytes(size: tuple[int, int] = (40, 20), frames: int = 1, fmt: str = "PNG") -> bytes:
    images = [Image.new("RGB", size, "white") for _ in range(frames)]
    buffer = io.BytesIO()
    images[0].save(buffer, format=fmt, save_all=frames > 1, append_images=images[1:])
    return buffer.getvalue()


def pdf_bytes(*page_texts: str) -> bytes:
    """A PDF whose pages carry the given text layer ('' = image-only page)."""
    buffer = io.BytesIO()
    pdf = canvas.Canvas(buffer, invariant=True)
    for text in page_texts:
        if text:
            pdf.drawString(72, 720, text)
        pdf.showPage()
    pdf.save()
    return buffer.getvalue()


# --- layout ------------------------------------------------------------------------
def test_page_builder_keeps_word_offsets_and_marks_column_gaps() -> None:
    builder = PageBuilder("t")
    builder.add_words(
        [
            RecognizedWord("Subtotal", 0.96, (0, 0, 80, 10)),
            RecognizedWord("537.00", 0.42, (300, 0, 60, 10)),  # far right: a new column
        ]
    )
    builder.add_words([RecognizedWord("VAT", 0.9, (0, 20, 30, 10)),
                       RecognizedWord("19%", 0.9, (40, 20, 30, 10))])  # fmt: skip
    builder.new_block()
    builder.add_words([RecognizedWord("Thanks", 0.8, (0, 60, 60, 10))])
    page = builder.build()

    assert page.text == f"Subtotal{COLUMN_GAP}537.00\nVAT 19%\n\nThanks"
    assert [page.text[w.start : w.end] for w in page.words] == [
        "Subtotal", "537.00", "VAT", "19%", "Thanks",
    ]  # fmt: skip
    assert page.words[1].confidence == 0.42 and page.words[1].box == (300, 0, 60, 10)


def test_page_builder_text_lines_share_line_confidence() -> None:
    page = ocr_page(("Invoice No:  A-1", 0.9), ("", 1.0), ("Total 5.00", 0.4))
    assert page.text == "Invoice No:  A-1\n\nTotal 5.00"
    assert [(page.text[w.start : w.end], w.confidence) for w in page.words] == [
        ("Invoice", 0.9), ("No:", 0.9), ("A-1", 0.9), ("Total", 0.4), ("5.00", 0.4),
    ]  # fmt: skip


def test_page_confidence_is_length_weighted() -> None:
    page = ocr_page(("aaaaaaaaa", 1.0), ("b", 0.0))
    assert page.confidence == pytest.approx(0.9)
    assert ocr_page().confidence == 0.0


# --- tesseract (binary stubbed) -------------------------------------------------------
TESSERACT_DATA: dict[str, list[Any]] = {
    "text": ["", "Total", "12.50", "Due", "ignored"],
    "conf": [-1, 96, 41.5, 90, -1],
    "block_num": [1, 1, 1, 2, 2],
    "par_num": [1, 1, 1, 1, 1],
    "line_num": [1, 1, 1, 1, 1],
    "left": [0, 0, 400, 0, 0],
    "top": [0, 0, 0, 50, 50],
    "width": [500, 50, 50, 30, 30],
    "height": [10, 10, 10, 10, 10],
}


def test_page_from_tesseract_data() -> None:
    page = page_from_tesseract_data(TESSERACT_DATA)
    assert page.text == f"Total{COLUMN_GAP}12.50\n\nDue"
    assert [w.confidence for w in page.words] == [0.96, 0.415, 0.9]
    assert page.engine == "tesseract"


def test_tesseract_engine_passes_options_and_maps_errors() -> None:
    calls: list[dict[str, Any]] = []

    def image_to_data(image: Image.Image, **kwargs: Any) -> dict[str, list[Any]]:
        calls.append({"mode": image.mode, **kwargs})
        return TESSERACT_DATA

    engine = TesseractOcrEngine("eng+deu", image_to_data=image_to_data)
    assert engine.recognize(Image.new("RGB", (40, 40), "white")).text.startswith("Total")
    assert calls[0]["mode"] == "L"
    assert calls[0]["lang"] == "eng+deu"
    assert calls[0]["config"] == "--psm 6"

    def missing(image: Image.Image, **kwargs: Any) -> dict[str, list[Any]]:
        raise pytesseract.TesseractNotFoundError()

    with pytest.raises(OcrError, match="Tesseract is not installed"):
        TesseractOcrEngine(image_to_data=missing).recognize(Image.new("L", (5, 5)))

    def timeout(image: Image.Image, **kwargs: Any) -> dict[str, list[Any]]:
        raise RuntimeError("Tesseract process timeout")

    with pytest.raises(OcrError, match="Tesseract failed: Tesseract process timeout"):
        TesseractOcrEngine(image_to_data=timeout).recognize(Image.new("L", (5, 5)))


# --- vision (LLM stubbed) ------------------------------------------------------------
class RecordingProvider:
    name = "anthropic"
    model = "claude-sonnet-5"

    def __init__(self, response: str | Exception) -> None:
        self.response = response
        self.requests: list[JsonRequest] = []

    def complete_json(self, request: JsonRequest) -> LLMResponse:
        self.requests.append(request)
        if isinstance(self.response, Exception):
            raise self.response
        return LLMResponse(self.response, self.model, 1500, 200)


def test_vision_engine_sends_downscaled_image_and_builds_page() -> None:
    transcription = {"lines": [{"text": "ACME Ltd", "confidence": 0.95},
                               {"text": "", "confidence": 1},
                               {"text": "Total    12.50", "confidence": 0.5}]}  # fmt: skip
    provider = RecordingProvider(json.dumps(transcription))
    engine = VisionOcrEngine(provider, max_edge=100)

    page = engine.recognize(Image.new("RGB", (400, 200), "white"))

    assert page.text == "ACME Ltd\n\nTotal    12.50"
    assert [w.confidence for w in page.words] == [0.95, 0.95, 0.5, 0.5]
    assert page.engine == engine.name == "vision:anthropic/claude-sonnet-5"
    request = provider.requests[0]
    assert request.system == VISION_SYSTEM_PROMPT and request.schema is PageTranscription
    image = request.messages[0].images[0]
    assert image.media_type == "image/png"
    assert Image.open(io.BytesIO(image.data)).size == (100, 50)


@pytest.mark.parametrize("response", [LLMError("rate limited"), '{"lines": "nope"}'])
def test_vision_engine_errors_become_ocr_errors(response: str | Exception) -> None:
    engine = VisionOcrEngine(RecordingProvider(response))
    with pytest.raises(OcrError, match="Vision OCR"):
        engine.recognize(Image.new("L", (10, 10)))


def test_build_ocr_engine() -> None:
    assert build_ocr_engine("none") is None
    assert isinstance(build_ocr_engine("tesseract", languages="deu"), TesseractOcrEngine)
    assert isinstance(build_ocr_engine("vision", provider=RecordingProvider("")), VisionOcrEngine)
    with pytest.raises(ValueError, match="needs LLM_PROVIDER"):
        build_ocr_engine("vision", provider=SimpleNamespace(name="fake", model="f"))  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="Unknown OCR engine"):
        build_ocr_engine("abbyy")


# --- provider adapters with images (SDK clients stubbed) ------------------------------
class _Recorder:
    def __init__(self, response: Any) -> None:
        self.response = response
        self.kwargs: dict[str, Any] = {}

    def create(self, **kwargs: Any) -> Any:
        self.kwargs = kwargs
        return self.response


def _image_request() -> JsonRequest:
    image = ImageInput("image/png", b"\x89PNG-bytes")
    message = ChatMessage("user", "Transcribe this page.", images=(image,))
    return JsonRequest(system="sys", messages=[message], schema=PageTranscription)


def test_anthropic_provider_sends_image_blocks_before_text() -> None:
    message = SimpleNamespace(
        content=[SimpleNamespace(type="text", text='{"lines": []}')],
        stop_reason="end_turn",
        model="claude-sonnet-5",
        usage=SimpleNamespace(input_tokens=1600, output_tokens=12),
    )
    recorder = _Recorder(message)
    provider = AnthropicProvider(client=SimpleNamespace(messages=recorder))  # type: ignore[arg-type]

    provider.complete_json(_image_request())

    content = recorder.kwargs["messages"][0]["content"]
    assert content[0] == {
        "type": "image",
        "source": {
            "type": "base64",
            "media_type": "image/png",
            "data": base64.standard_b64encode(b"\x89PNG-bytes").decode(),
        },
    }
    assert content[1] == {"type": "text", "text": "Transcribe this page."}
    assert "lines" in recorder.kwargs["output_config"]["format"]["schema"]["properties"]


def test_openai_provider_sends_image_as_data_url() -> None:
    completion = SimpleNamespace(
        choices=[SimpleNamespace(finish_reason="stop",
                                 message=SimpleNamespace(content="{}", refusal=None))],
        model="gpt-5.4-mini", usage=None,
    )  # fmt: skip
    recorder = _Recorder(completion)
    client = SimpleNamespace(chat=SimpleNamespace(completions=recorder))
    OpenAIProvider(client=client).complete_json(_image_request())  # type: ignore[arg-type]

    content = recorder.kwargs["messages"][1]["content"]
    encoded = base64.standard_b64encode(b"\x89PNG-bytes").decode()
    assert content[0] == {
        "type": "image_url",
        "image_url": {"url": f"data:image/png;base64,{encoded}"},
    }
    assert content[1] == {"type": "text", "text": "Transcribe this page."}


# --- parse integration ----------------------------------------------------------------
def test_scanned_pdf_without_ocr_is_rejected() -> None:
    with pytest.raises(UnsupportedDocumentError, match=r"no text layer.*OCR_ENGINE=none"):
        parse_document("scan.pdf", pdf_bytes(""))


def test_scanned_pdf_is_rasterised_and_ocred() -> None:
    engine = StubOcrEngine(ocr_page(("Invoice No: S-1", 0.9)))
    document = parse_document("scan.pdf", pdf_bytes(""), ocr=engine, dpi=100)

    assert document.source_type == "scanned_pdf"
    assert document.text == "Invoice No: S-1"
    assert document.is_ocr and document.pages[0].ocr_confidence == pytest.approx(0.9)
    assert document.metadata == {
        "page_count": "1", "ocr_engine": "stub", "ocr_pages": "1", "ocr_confidence": "0.90",
    }  # fmt: skip
    assert engine.images[0].width == pytest.approx(595.27 * 100 / 72, abs=1)  # A4 at 100 dpi


def test_only_pages_without_text_layer_are_ocred() -> None:
    engine = StubOcrEngine(ocr_page(("Scanned appendix page", 0.8)))
    document = parse_document(
        "mixed.pdf", pdf_bytes("Invoice No: BLS-1 with a real text layer", ""), ocr=engine
    )
    assert len(engine.images) == 1
    assert not document.pages[0].is_ocr and document.pages[1].is_ocr
    assert document.metadata["ocr_pages"] == "2"


def test_multi_frame_tiff_gives_one_page_per_frame() -> None:
    engine = StubOcrEngine(ocr_page(("page one", 0.9)), ocr_page(("page two", 0.7)))
    document = parse_document("fax.tiff", png_bytes(frames=2, fmt="TIFF"), ocr=engine)
    assert document.source_type == "image"
    assert [p.text for p in document.pages] == ["page one", "page two"]
    assert document.metadata["ocr_confidence"] == "0.80"


def test_image_attachment_in_email_is_ocred() -> None:
    message = EmailMessage()
    message["From"] = "billing@example.com"
    message.set_content("Photo of the receipt attached.")
    message.add_attachment(png_bytes(), maintype="image", subtype="png", filename="receipt.png")

    document = parse_document("mail.eml", bytes(message), ocr=StubOcrEngine(ocr_page(("R-9", 0.9))))
    assert document.source_type == "email"
    assert document.pages[1].text == "R-9" and document.pages[1].is_ocr
    assert document.metadata["attachments"] == "receipt.png"


def test_blank_scan_is_a_parse_error() -> None:
    with pytest.raises(ParseError, match="no readable text"):
        parse_document("blank.png", png_bytes(), ocr=StubOcrEngine(ocr_page()))


def test_unreadable_image_is_a_parse_error() -> None:
    with pytest.raises(ParseError, match="OCR failed: Could not read image"):
        parse_document("broken.jpg", b"not a jpeg", ocr=StubOcrEngine())


# --- confidence -----------------------------------------------------------------------
def ocr_document() -> Any:
    page = ocr_page(("Invoice No: A-1", 0.95), ("Total 178.5O", 0.41))
    return parse_document("s.png", png_bytes(), ocr=StubOcrEngine(page))


def test_located_evidence_carries_lowest_word_confidence() -> None:
    document = ocr_document()
    span = locate(Evidence(quote="Total 178.5O"), document)
    assert span is not None and span.ocr_confidence == pytest.approx(0.41)
    assert locate(Evidence(quote="Invoice No: A-1"), document).ocr_confidence == 0.95  # type: ignore[union-attr]


def test_confidence_is_capped_by_ocr_confidence_only_when_lower() -> None:
    invoice = make_invoice(
        invoice_number=field("A-1", 0.9, "Invoice No: A-1"),
        total=field("178.50", 0.95, "Total 178.5O"),
        line_items=[{"description": "x", "amount": "178.50", "confidence": 0.9,
                     "evidence": {"quote": "Total 178.5O", "page": 1}}],
    )  # fmt: skip
    capped = cap_confidence_to_ocr(invoice, resolve_invoice_evidence(invoice, ocr_document()))
    assert capped == ["total", "line_items.0"]
    assert invoice.total.confidence == 0.41 and invoice.line_items[0].confidence == 0.41
    assert invoice.invoice_number.confidence == 0.9  # OCR 0.95 does not raise it


def test_ocr_quality_check_and_routing() -> None:
    context = ValidationContext(ocr_page_confidence={1: 0.93, 2: 0.52}, ocr_engine="tesseract")
    issues = list(check_ocr_quality(make_invoice(), context))
    assert [(i.code, i.severity) for i in issues] == [
        ("ocr_text", Severity.INFO), ("low_ocr_confidence", Severity.WARNING),
    ]  # fmt: skip
    assert "Page 2" in issues[1].message and "0.52" in issues[1].message

    decision = route(make_invoice(), issues)
    assert decision.route is Route.NEEDS_REVIEW
    assert decision.reasons == ("poor scan: OCR confidence below the page threshold",)
    relaxed = RoutingConfig(review_low_ocr_quality=False)
    assert route(make_invoice(), issues, relaxed).route is Route.APPROVED


def test_no_ocr_issues_for_text_documents() -> None:
    assert list(check_ocr_quality(make_invoice(), ValidationContext())) == []
