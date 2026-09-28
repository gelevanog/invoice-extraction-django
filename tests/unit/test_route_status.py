from __future__ import annotations

from pathlib import Path

import pytest

from extraction.issues import Issue, Severity
from extraction.llm.fake import FakeProvider
from extraction.pipeline import InvoicePipeline
from extraction.route import Route, RoutingConfig, route
from extraction.status import (
    DocumentStatus,
    InvalidTransitionError,
    can_transition,
    ensure_transition,
)
from tests.factories import field, make_invoice

ERROR = Issue("totals_mismatch", Severity.ERROR, "x", "total")
WARNING = Issue("missing_due_date", Severity.WARNING, "x", "due_date")
INFO = Issue("new_vendor", Severity.INFO, "x", "vendor_name")


def test_clean_invoice_is_approved() -> None:
    decision = route(make_invoice(), [])
    assert decision.route is Route.APPROVED
    assert decision.reasons == ()
    assert decision.min_confidence == pytest.approx(0.9)  # line items are 0.9


def test_error_issue_forces_review() -> None:
    decision = route(make_invoice(), [ERROR, WARNING])
    assert decision.route is Route.NEEDS_REVIEW
    assert decision.reasons == ("1 error-level issue(s): totals_mismatch",)


def test_warnings_and_info_do_not_block_by_default() -> None:
    assert route(make_invoice(), [WARNING, INFO]).route is Route.APPROVED


def test_warnings_can_be_configured_to_block() -> None:
    decision = route(make_invoice(), [WARNING], RoutingConfig(review_on_warnings=True))
    assert decision.route is Route.NEEDS_REVIEW


def test_new_vendors_can_be_configured_to_block() -> None:
    config = RoutingConfig(review_new_vendors=True)
    assert route(make_invoice(), [INFO], config).route is Route.NEEDS_REVIEW
    assert route(make_invoice(), [], config).route is Route.APPROVED


@pytest.mark.parametrize(
    ("confidence", "threshold", "expected"),
    [
        (0.95, 0.75, Route.APPROVED),
        (0.75, 0.75, Route.APPROVED),  # threshold is inclusive
        (0.74, 0.75, Route.NEEDS_REVIEW),
        (0.80, 0.85, Route.NEEDS_REVIEW),
        (0.10, 0.00, Route.APPROVED),  # threshold 0 disables confidence routing
    ],
)
def test_confidence_threshold(confidence: float, threshold: float, expected: Route) -> None:
    invoice = make_invoice(invoice_number=field("BLS-1", confidence=confidence))
    decision = route(invoice, [], RoutingConfig(confidence_threshold=threshold))
    assert decision.route is expected
    if expected is Route.NEEDS_REVIEW:
        assert f"invoice_number={confidence:.2f}" in decision.reasons[0]


def test_low_confidence_line_item_forces_review() -> None:
    items = [{"description": "x", "amount": "150.00", "confidence": 0.4}]
    decision = route(make_invoice(line_items=items), [])
    assert decision.route is Route.NEEDS_REVIEW
    assert "line_items.0=0.40" in decision.reasons[0]
    assert decision.min_confidence == pytest.approx(0.4)


def test_vendor_tax_id_and_due_date_confidence_do_not_gate_routing() -> None:
    invoice = make_invoice(
        vendor_tax_id=field(None, confidence=0.1), due_date=field(None, confidence=0.1)
    )
    assert route(invoice, []).route is Route.APPROVED


S = DocumentStatus


@pytest.mark.parametrize(
    ("current", "target", "allowed"),
    [
        (S.UPLOADED, S.PARSED, True),
        (S.PARSED, S.EXTRACTED, True),
        (S.ENRICHED, S.APPROVED, True),
        (S.ENRICHED, S.NEEDS_REVIEW, True),
        (S.NEEDS_REVIEW, S.REJECTED, True),
        (S.FAILED, S.UPLOADED, True),
        (S.UPLOADED, S.APPROVED, False),  # cannot skip stages
        (S.APPROVED, S.REJECTED, False),  # approved is final
        (S.PARSED, S.NEEDS_REVIEW, False),
        (S.EXTRACTED, S.FAILED, True),
    ],
)
def test_status_transitions(current: DocumentStatus, target: DocumentStatus, allowed: bool) -> None:
    assert can_transition(current, target) is allowed
    if not allowed:
        with pytest.raises(InvalidTransitionError):
            ensure_transition(current, target)


def test_core_pipeline_runs_without_django(sample_dir: Path) -> None:
    path = sample_dir / "03_bluepeak_consulting_invoice.pdf"
    result = InvoicePipeline(FakeProvider()).run(path.name, path.read_bytes())
    assert result.status is S.NEEDS_REVIEW
    assert [i.code for i in result.issues] == ["line_items_sum_mismatch", "new_vendor"]
    assert result.enrichment is not None and result.enrichment.conversion is not None


def test_core_pipeline_fails_on_unsupported_file() -> None:
    result = InvoicePipeline(FakeProvider()).run("photo.jpg", b"...")
    assert result.status is S.FAILED
    assert result.error is not None and "OCR" in result.error
