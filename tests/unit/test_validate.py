from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from extraction.evidence import Span
from extraction.issues import Severity
from extraction.validate import (
    ValidationConfig,
    ValidationContext,
    check_currency,
    check_dates,
    check_duplicates,
    check_evidence,
    check_line_item_math,
    check_line_items_sum,
    check_required_fields,
    check_tax_id,
    check_totals,
    detect_tax_id_country,
    validate_invoice,
)
from tests.factories import make_invoice

CTX = ValidationContext(today=date(2026, 3, 20))


def codes(issues: list) -> list[str]:
    return [issue.code for issue in issues]


def test_consistent_invoice_has_no_issues() -> None:
    assert validate_invoice(make_invoice(), CTX) == []


@pytest.mark.parametrize(
    "missing", ["vendor_name", "invoice_number", "issue_date", "currency", "total"]
)
def test_required_fields(missing: str) -> None:
    issues = list(check_required_fields(make_invoice(**{missing: None}), CTX))
    assert [(i.code, i.field, i.severity) for i in issues] == [
        ("missing_required_field", missing, Severity.ERROR)
    ]


def test_blank_string_counts_as_missing() -> None:
    issues = list(check_required_fields(make_invoice(vendor_name="   "), CTX))
    assert codes(issues) == ["missing_required_field"]


@pytest.mark.parametrize(
    ("subtotal", "expected"),
    [
        (Decimal("150.00"), []),
        (Decimal("150.02"), []),  # within default tolerance
        (Decimal("150.03"), ["line_items_sum_mismatch"]),
        (Decimal("450.00"), ["line_items_sum_mismatch"]),
    ],
)
def test_line_items_sum(subtotal: Decimal, expected: list[str]) -> None:
    invoice = make_invoice(subtotal=subtotal)
    assert codes(list(check_line_items_sum(invoice, CTX))) == expected


def test_line_items_sum_falls_back_to_total_minus_tax() -> None:
    invoice = make_invoice(subtotal=None, total=Decimal("200.00"), tax=Decimal("28.50"))
    issues = list(check_line_items_sum(invoice, CTX))
    assert codes(issues) == ["line_items_sum_mismatch"]
    assert "171.50" in issues[0].message


def test_no_line_items_is_a_warning() -> None:
    issues = list(check_line_items_sum(make_invoice(line_items=[]), CTX))
    assert [(i.code, i.severity) for i in issues] == [("no_line_items", Severity.WARNING)]


def test_tolerance_is_configurable() -> None:
    strict = ValidationContext(config=ValidationConfig(amount_tolerance=Decimal("0")))
    invoice = make_invoice(subtotal=Decimal("150.01"))
    assert codes(list(check_line_items_sum(invoice, strict))) == ["line_items_sum_mismatch"]


@pytest.mark.parametrize(
    ("quantity", "unit_price", "amount", "expected"),
    [
        ("2", "50.00", "100.00", []),
        ("3", "33.33", "99.99", []),
        ("2", "50.00", "120.00", ["line_item_amount_mismatch"]),
        (None, "50.00", "120.00", []),  # nothing to cross-check
    ],
)
def test_line_item_math(
    quantity: str | None, unit_price: str, amount: str, expected: list[str]
) -> None:
    item = {"description": "x", "quantity": quantity, "unit_price": unit_price,
            "amount": amount, "confidence": 0.9}  # fmt: skip
    invoice = make_invoice(line_items=[item])
    issues = list(check_line_item_math(invoice, CTX))
    assert codes(issues) == expected
    if issues:
        assert issues[0].severity is Severity.WARNING
        assert issues[0].field == "line_items.0"


@pytest.mark.parametrize(
    ("subtotal", "tax", "total", "expected"),
    [
        ("150.00", "28.50", "178.50", []),
        ("150.00", "28.50", "178.52", []),
        ("150.00", "28.50", "180.00", ["totals_mismatch"]),
        ("150.00", None, "150.00", []),  # no tax line
        ("150.00", None, "178.50", ["totals_mismatch"]),
        (None, "28.50", "999.00", []),  # cannot check without subtotal
    ],
)
def test_totals(subtotal: str | None, tax: str | None, total: str, expected: list[str]) -> None:
    def dec(value: str | None) -> Decimal | None:
        return Decimal(value) if value else None

    invoice = make_invoice(subtotal=dec(subtotal), tax=dec(tax), total=dec(total))
    assert codes(list(check_totals(invoice, CTX))) == expected


@pytest.mark.parametrize(
    ("issue", "due", "expected"),
    [
        (date(2026, 3, 2), date(2026, 4, 1), []),
        (date(2026, 3, 2), date(2026, 3, 2), []),
        (date(2026, 3, 2), None, ["missing_due_date"]),
        (date(2026, 3, 2), date(2026, 2, 1), ["due_before_issue"]),
        (date(2027, 1, 1), date(2027, 2, 1), ["implausible_issue_date"]),  # far future
        (date(1999, 1, 1), date(1999, 2, 1), ["implausible_issue_date"]),
        (None, date(2026, 4, 1), []),  # missing issue date is a required-field error
    ],
)
def test_dates(issue: date | None, due: date | None, expected: list[str]) -> None:
    invoice = make_invoice(issue_date=issue, due_date=due)
    assert codes(list(check_dates(invoice, CTX))) == expected


def test_due_before_issue_is_error_and_missing_due_is_warning() -> None:
    errors = list(check_dates(make_invoice(due_date=date(2026, 1, 1)), CTX))
    warnings = list(check_dates(make_invoice(due_date=None), CTX))
    assert errors[0].severity is Severity.ERROR
    assert warnings[0].severity is Severity.WARNING


@pytest.mark.parametrize(
    ("currency", "expected"),
    [("EUR", []), ("usd", []), ("GBP", []), ("EURO", ["invalid_currency"]),
     ("XYZ", ["invalid_currency"]), (None, [])],
)  # fmt: skip
def test_currency(currency: str | None, expected: list[str]) -> None:
    assert codes(list(check_currency(make_invoice(currency=currency), CTX))) == expected


@pytest.mark.parametrize(
    ("tax_id", "expected"),
    [
        ("DE811234567", []),
        ("DE 811 234 567", []),  # schema strips spaces
        ("DE81123456", ["invalid_tax_id_format"]),
        ("GB123456789", []),
        ("GB123456789012", []),
        ("GB12345", ["invalid_tax_id_format"]),
        ("NL853746291B01", []),
        ("NL853746291", ["invalid_tax_id_format"]),
        ("FRXX123456789", []),
        ("84-2917365", []),  # US EIN
        ("842917365", ["invalid_tax_id_format"]),  # EIN needs the dash
        ("ZZ999", ["unrecognized_tax_id"]),
        (None, ["missing_tax_id"]),
    ],
)
def test_tax_id(tax_id: str | None, expected: list[str]) -> None:
    assert codes(list(check_tax_id(make_invoice(vendor_tax_id=tax_id), CTX))) == expected


@pytest.mark.parametrize(
    ("tax_id", "country"),
    [("DE811234567", "DE"), ("GB123", "GB"), ("84-2917365", "US"), ("XX1", None), ("123", None)],
)
def test_detect_tax_id_country(tax_id: str, country: str | None) -> None:
    assert detect_tax_id_country(tax_id) == country


def test_duplicates_use_lookup() -> None:
    seen: list[tuple] = []

    def lookup(name: str | None, tax_id: str | None, number: str) -> list[str]:
        seen.append((name, tax_id, number))
        return ["document #1"] if number == "BLS-1" else []

    ctx = ValidationContext(duplicate_lookup=lookup)
    issues = list(check_duplicates(make_invoice(), ctx))
    assert [(i.code, i.severity) for i in issues] == [("duplicate_invoice", Severity.ERROR)]
    assert "document #1" in issues[0].message
    assert seen == [("Brightline Software GmbH", "DE811234567", "BLS-1")]
    assert list(check_duplicates(make_invoice(invoice_number="OTHER"), ctx)) == []


def test_duplicates_skipped_without_lookup_or_number() -> None:
    assert list(check_duplicates(make_invoice(), CTX)) == []
    ctx = ValidationContext(duplicate_lookup=lambda *_: ["x"])
    assert list(check_duplicates(make_invoice(invoice_number=None), ctx)) == []


def test_evidence_not_found_is_warning() -> None:
    ctx = ValidationContext(evidence_spans={"total": Span(0, 5, 1), "vendor_name": None})
    issues = list(check_evidence(make_invoice(), ctx))
    assert [(i.code, i.field, i.severity) for i in issues] == [
        ("evidence_not_found", "vendor_name", Severity.WARNING)
    ]


def test_custom_checks_can_be_plugged_in() -> None:
    from extraction.issues import Issue

    def no_round_totals(invoice, ctx):  # type: ignore[no-untyped-def]
        if invoice.total.value and invoice.total.value % 100 == 0:
            yield Issue("round_total", Severity.WARNING, "Suspiciously round total", "total")

    invoice = make_invoice(
        total=Decimal("500.00"), subtotal=Decimal("500.00"), tax=None, line_items=[]
    )
    assert codes(validate_invoice(invoice, CTX, checks=[no_round_totals])) == ["round_total"]
