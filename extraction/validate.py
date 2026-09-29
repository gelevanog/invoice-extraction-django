"""Stage 3 - deterministic business-rule validation (no LLM involved).

Each check is a plain function ``(invoice, context) -> Iterable[Issue]`` so rules are
easy to unit-test, reorder, or extend with project-specific checks.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal
from typing import Protocol

from extraction.currencies import is_iso_4217
from extraction.evidence import Span
from extraction.issues import Issue, Severity
from extraction.schemas import Invoice


class DuplicateLookup(Protocol):
    """Return human-readable references of existing invoices with the same identity."""

    def __call__(
        self, vendor_name: str | None, vendor_tax_id: str | None, invoice_number: str
    ) -> Sequence[str]: ...


@dataclass(frozen=True, slots=True)
class ValidationConfig:
    amount_tolerance: Decimal = Decimal("0.02")
    max_future_days: int = 30
    earliest_plausible_date: date = date(2000, 1, 1)
    min_ocr_confidence: float = 0.65  # mean word confidence below this = poor scan


@dataclass(slots=True)
class ValidationContext:
    config: ValidationConfig = field(default_factory=ValidationConfig)
    today: date = field(default_factory=date.today)
    duplicate_lookup: DuplicateLookup | None = None
    evidence_spans: dict[str, Span | None] | None = None
    ocr_page_confidence: dict[int, float] | None = None  # page number -> mean confidence
    ocr_engine: str | None = None


Check = Callable[[Invoice, ValidationContext], Iterable[Issue]]

REQUIRED_FIELDS = ("vendor_name", "invoice_number", "issue_date", "currency", "total")


def check_required_fields(invoice: Invoice, ctx: ValidationContext) -> Iterable[Issue]:
    fields = invoice.scalar_fields()
    for name in REQUIRED_FIELDS:
        value = fields[name].value
        if value is None or (isinstance(value, str) and not value.strip()):
            yield Issue(
                "missing_required_field",
                Severity.ERROR,
                f"Required field '{name}' was not found in the document.",
                name,
            )


def check_line_items_sum(invoice: Invoice, ctx: ValidationContext) -> Iterable[Issue]:
    if not invoice.line_items:
        yield Issue(
            "no_line_items", Severity.WARNING, "No line items were extracted.", "line_items"
        )
        return
    items_sum = sum((item.amount for item in invoice.line_items), Decimal(0))
    subtotal = invoice.subtotal.value
    if subtotal is None and invoice.total.value is not None:
        subtotal = invoice.total.value - (invoice.tax.value or Decimal(0))
    if subtotal is not None and abs(items_sum - subtotal) > ctx.config.amount_tolerance:
        yield Issue(
            "line_items_sum_mismatch",
            Severity.ERROR,
            f"Line items add up to {items_sum:.2f} but the subtotal is {subtotal:.2f} "
            f"(difference {items_sum - subtotal:+.2f}).",
            "subtotal",
        )


def check_line_item_math(invoice: Invoice, ctx: ValidationContext) -> Iterable[Issue]:
    for index, item in enumerate(invoice.line_items):
        if item.quantity is None or item.unit_price is None:
            continue
        expected = item.quantity * item.unit_price
        if abs(expected - item.amount) > ctx.config.amount_tolerance:
            yield Issue(
                "line_item_amount_mismatch",
                Severity.WARNING,
                f"Line {index + 1} '{item.description}': {item.quantity} x {item.unit_price} "
                f"= {expected:.2f}, but the amount is {item.amount:.2f}.",
                f"line_items.{index}",
            )


def check_totals(invoice: Invoice, ctx: ValidationContext) -> Iterable[Issue]:
    subtotal, tax, total = invoice.subtotal.value, invoice.tax.value, invoice.total.value
    if subtotal is None or total is None:
        return
    expected = subtotal + (tax or Decimal(0))
    if abs(expected - total) > ctx.config.amount_tolerance:
        yield Issue(
            "totals_mismatch",
            Severity.ERROR,
            f"Subtotal {subtotal:.2f} + tax {tax or 0:.2f} = {expected:.2f}, "
            f"but the total is {total:.2f}.",
            "total",
        )


def check_dates(invoice: Invoice, ctx: ValidationContext) -> Iterable[Issue]:
    issued, due = invoice.issue_date.value, invoice.due_date.value
    if due is None:
        yield Issue(
            "missing_due_date",
            Severity.WARNING,
            "No due date found; payment terms must be checked manually.",
            "due_date",
        )
    if issued is not None:
        latest = ctx.today + timedelta(days=ctx.config.max_future_days)
        if issued > latest or issued < ctx.config.earliest_plausible_date:
            yield Issue(
                "implausible_issue_date",
                Severity.WARNING,
                f"Issue date {issued.isoformat()} is outside the plausible range.",
                "issue_date",
            )
    if issued is not None and due is not None and due < issued:
        yield Issue(
            "due_before_issue",
            Severity.ERROR,
            f"Due date {due.isoformat()} is before the issue date {issued.isoformat()}.",
            "due_date",
        )


def check_currency(invoice: Invoice, ctx: ValidationContext) -> Iterable[Issue]:
    code = invoice.currency.value
    if code is not None and not is_iso_4217(code):
        yield Issue(
            "invalid_currency",
            Severity.ERROR,
            f"'{code}' is not an ISO 4217 currency code.",
            "currency",
        )


# Deliberately simple formats; real VAT validation would call VIES / HMRC services.
TAX_ID_PATTERNS: dict[str, re.Pattern[str]] = {
    "DE": re.compile(r"DE\d{9}"),
    "GB": re.compile(r"GB(?:\d{9}|\d{12}|GD\d{3}|HA\d{3})"),
    "FR": re.compile(r"FR[0-9A-HJ-NP-Z]{2}\d{9}"),
    "NL": re.compile(r"NL\d{9}B\d{2}"),
    "ES": re.compile(r"ES[0-9A-Z]\d{7}[0-9A-Z]"),
    "IT": re.compile(r"IT\d{11}"),
    "US": re.compile(r"\d{2}-\d{7}"),  # EIN
}


def detect_tax_id_country(tax_id: str) -> str | None:
    prefix = tax_id[:2].upper()
    if prefix.isalpha():
        return prefix if prefix in TAX_ID_PATTERNS else None
    return "US" if re.fullmatch(r"\d{2}-?\d{7}", tax_id) else None


def check_tax_id(invoice: Invoice, ctx: ValidationContext) -> Iterable[Issue]:
    tax_id = invoice.vendor_tax_id.value
    if not tax_id:
        yield Issue(
            "missing_tax_id",
            Severity.INFO,
            "No vendor tax ID on the document.",
            "vendor_tax_id",
        )
        return
    country = detect_tax_id_country(tax_id)
    if country is None:
        yield Issue(
            "unrecognized_tax_id",
            Severity.INFO,
            f"Tax ID '{tax_id}' has no known country format; not verified.",
            "vendor_tax_id",
        )
    elif not TAX_ID_PATTERNS[country].fullmatch(tax_id):
        yield Issue(
            "invalid_tax_id_format",
            Severity.WARNING,
            f"Tax ID '{tax_id}' does not match the {country} format.",
            "vendor_tax_id",
        )


def check_duplicates(invoice: Invoice, ctx: ValidationContext) -> Iterable[Issue]:
    number = invoice.invoice_number.value
    if ctx.duplicate_lookup is None or not number:
        return
    matches = ctx.duplicate_lookup(invoice.vendor_name.value, invoice.vendor_tax_id.value, number)
    if matches:
        yield Issue(
            "duplicate_invoice",
            Severity.ERROR,
            f"Invoice {number} from this vendor was already received ({', '.join(matches)}).",
            "invoice_number",
        )


def check_evidence(invoice: Invoice, ctx: ValidationContext) -> Iterable[Issue]:
    if ctx.evidence_spans is None:
        return
    for name, span in ctx.evidence_spans.items():
        if span is None:
            yield Issue(
                "evidence_not_found",
                Severity.WARNING,
                f"The quoted evidence for '{name}' does not appear in the document.",
                name,
            )


def check_ocr_quality(invoice: Invoice, ctx: ValidationContext) -> Iterable[Issue]:
    if not ctx.ocr_page_confidence:
        return
    scores = ctx.ocr_page_confidence
    pages = ", ".join(str(n) for n in scores)
    mean = sum(scores.values()) / len(scores)
    yield Issue(
        "ocr_text",
        Severity.INFO,
        f"Page(s) {pages} read by OCR ({ctx.ocr_engine or 'unknown engine'}, mean confidence "
        f"{mean:.2f}); each value's confidence is capped at that of the words it was read from.",
    )
    for number, score in scores.items():
        if score < ctx.config.min_ocr_confidence:
            yield Issue(
                "low_ocr_confidence",
                Severity.WARNING,
                f"Page {number} was recognised with mean OCR confidence {score:.2f} "
                f"(below {ctx.config.min_ocr_confidence:.2f}); compare values with the original.",
            )


DEFAULT_CHECKS: tuple[Check, ...] = (
    check_required_fields,
    check_line_items_sum,
    check_line_item_math,
    check_totals,
    check_dates,
    check_currency,
    check_tax_id,
    check_duplicates,
    check_evidence,
    check_ocr_quality,
)


def validate_invoice(
    invoice: Invoice,
    context: ValidationContext | None = None,
    checks: Sequence[Check] = DEFAULT_CHECKS,
) -> list[Issue]:
    ctx = context or ValidationContext()
    return [issue for check in checks for issue in check(invoice, ctx)]
