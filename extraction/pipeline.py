"""Framework-agnostic orchestration of the five stages.

Callers that need to persist progress between stages (like the Django app) call the
stage methods one by one; scripts and notebooks can simply call :meth:`run`.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import date
from typing import Any

import structlog

from extraction.enrich import EnrichmentResult, enrich_invoice
from extraction.enrich.categorize import Categorizer, KeywordCategorizer
from extraction.enrich.fx import FxRatesProvider, StaticFxRatesProvider
from extraction.enrich.vendors import VendorMatcher, VendorRecord
from extraction.evidence import Span, cap_confidence_to_ocr, resolve_invoice_evidence
from extraction.extract import ExtractionResult, FewShotExample, extract_structured
from extraction.issues import Issue
from extraction.llm.base import LLMProvider
from extraction.ocr.base import OcrEngine
from extraction.ocr.images import DEFAULT_DPI
from extraction.parse import ParsedDocument, ParseError, parse_document
from extraction.route import RoutingConfig, RoutingDecision, route
from extraction.schemas import Invoice
from extraction.status import DocumentStatus
from extraction.validate import (
    DEFAULT_CHECKS,
    Check,
    DuplicateLookup,
    ValidationConfig,
    ValidationContext,
    validate_invoice,
)

logger = structlog.get_logger(__name__)

# Returns verified records from earlier documents of the same vendor, most relevant first.
ExampleSource = Callable[[ParsedDocument], Sequence[FewShotExample]]


@dataclass(frozen=True, slots=True)
class PipelineConfig:
    max_attempts: int = 3
    base_currency: str = "EUR"
    vendor_match_threshold: float = 85.0
    validation: ValidationConfig = field(default_factory=ValidationConfig)
    routing: RoutingConfig = field(default_factory=RoutingConfig)
    checks: tuple[Check, ...] = DEFAULT_CHECKS
    ocr_dpi: int = DEFAULT_DPI
    max_example_chars: int = 4000


@dataclass(slots=True)
class PipelineResult:
    status: DocumentStatus
    parsed: ParsedDocument | None = None
    extraction: ExtractionResult[Invoice] | None = None
    evidence: dict[str, Span | None] = field(default_factory=dict)
    issues: list[Issue] = field(default_factory=list)
    enrichment: EnrichmentResult | None = None
    decision: RoutingDecision | None = None
    error: str | None = None

    @property
    def invoice(self) -> Invoice | None:
        return self.extraction.data if self.extraction else None


class InvoicePipeline:
    def __init__(
        self,
        provider: LLMProvider,
        *,
        config: PipelineConfig | None = None,
        fx_provider: FxRatesProvider | None = None,
        categorizer: Categorizer | None = None,
        vendors: Callable[[], Iterable[VendorRecord]] = lambda: (),
        duplicate_lookup: DuplicateLookup | None = None,
        today: Callable[[], date] = date.today,
        ocr: OcrEngine | None = None,
        examples: ExampleSource | None = None,
    ) -> None:
        self.provider = provider
        self.config = config or PipelineConfig()
        self.fx_provider = fx_provider or StaticFxRatesProvider.from_file()
        self.categorizer = categorizer or KeywordCategorizer()
        self._vendors = vendors
        self._duplicate_lookup = duplicate_lookup
        self._today = today
        self.ocr = ocr
        self._examples = examples

    # -- stages -------------------------------------------------------------------
    def parse(self, filename: str, data: bytes) -> ParsedDocument:
        return parse_document(filename, data, ocr=self.ocr, dpi=self.config.ocr_dpi)

    def extract(self, document: ParsedDocument) -> ExtractionResult[Invoice]:
        """LLM extraction; for OCR text, confidences are then capped by word confidence."""
        result = extract_structured(
            document,
            Invoice,
            self.provider,
            max_attempts=self.config.max_attempts,
            examples=self._examples(document) if self._examples else (),
            max_example_chars=self.config.max_example_chars,
        )
        if result.data is not None and document.is_ocr:
            spans = resolve_invoice_evidence(result.data, document)
            capped = cap_confidence_to_ocr(result.data, spans)
            logger.info("extract.ocr_confidence_capped", fields=capped)
        return result

    def validate(
        self, invoice: Invoice, document: ParsedDocument
    ) -> tuple[list[Issue], dict[str, Span | None]]:
        spans = resolve_invoice_evidence(invoice, document)
        context = ValidationContext(
            config=self.config.validation,
            today=self._today(),
            duplicate_lookup=self._duplicate_lookup,
            evidence_spans=spans,
            ocr_page_confidence={
                page.number: page.ocr_confidence
                for page in document.pages
                if page.ocr_confidence is not None
            },
            ocr_engine=document.metadata.get("ocr_engine"),
        )
        return validate_invoice(invoice, context, self.config.checks), spans

    def enrich(self, invoice: Invoice, *, categorize: bool = True) -> EnrichmentResult:
        matcher = VendorMatcher(self._vendors(), threshold=self.config.vendor_match_threshold)
        return enrich_invoice(
            invoice,
            vendor_matcher=matcher,
            fx_provider=self.fx_provider,
            categorizer=self.categorizer if categorize else None,
            base_currency=self.config.base_currency,
        )

    def route(self, invoice: Invoice, issues: list[Issue]) -> RoutingDecision:
        return route(invoice, issues, self.config.routing)

    # -- all at once --------------------------------------------------------------
    def run(self, filename: str, data: bytes) -> PipelineResult:
        log = logger.bind(filename=filename)
        result = PipelineResult(status=DocumentStatus.UPLOADED)
        try:
            result.parsed = self.parse(filename, data)
        except ParseError as exc:
            return self._fail(result, f"parse: {exc}", log)
        result.status = DocumentStatus.PARSED

        result.extraction = self.extract(result.parsed)
        invoice = result.extraction.data
        if invoice is None:
            return self._fail(result, f"extract: {result.extraction.error}", log)
        result.status = DocumentStatus.EXTRACTED

        result.issues, result.evidence = self.validate(invoice, result.parsed)
        result.status = DocumentStatus.VALIDATED

        result.enrichment = self.enrich(invoice)
        result.issues.extend(result.enrichment.issues)
        result.status = DocumentStatus.ENRICHED

        result.decision = self.route(invoice, result.issues)
        result.status = DocumentStatus(result.decision.route.value)
        log.info("pipeline.done", status=result.status.value, reasons=result.decision.reasons)
        return result

    @staticmethod
    def _fail(result: PipelineResult, error: str, log: Any) -> PipelineResult:
        result.status = DocumentStatus.FAILED
        result.error = error
        log.warning("pipeline.failed", error=error)
        return result
