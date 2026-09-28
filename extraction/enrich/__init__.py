"""Stage 4 - enrichment: canonical vendor, base-currency totals, expense categories."""

from __future__ import annotations

from dataclasses import dataclass, field

from extraction.enrich.categorize import Categorizer, ExpenseCategory
from extraction.enrich.fx import Conversion, FxRatesProvider, FxRateUnavailableError, convert
from extraction.enrich.vendors import VendorMatch, VendorMatcher
from extraction.issues import Issue, Severity
from extraction.schemas import Invoice


@dataclass(slots=True)
class EnrichmentResult:
    vendor_match: VendorMatch
    conversion: Conversion | None
    categories: list[ExpenseCategory]
    issues: list[Issue] = field(default_factory=list)


def enrich_invoice(
    invoice: Invoice,
    *,
    vendor_matcher: VendorMatcher,
    fx_provider: FxRatesProvider,
    categorizer: Categorizer | None,
    base_currency: str,
) -> EnrichmentResult:
    """Match the vendor, convert the total and categorize line items.

    Pass ``categorizer=None`` to skip categorization (e.g. when re-enriching after a
    human edit, where existing categories should be kept and no LLM call is wanted).
    """
    issues: list[Issue] = []

    match = vendor_matcher.match(invoice.vendor_name.value, invoice.vendor_tax_id.value)
    if match.is_new and invoice.vendor_name.value:
        issues.append(
            Issue(
                "new_vendor",
                Severity.INFO,
                f"'{invoice.vendor_name.value}' does not match any known vendor; "
                "a vendor record is created when the invoice is approved.",
                "vendor_name",
            )
        )

    conversion = None
    total, currency = invoice.total.value, invoice.currency.value
    if total is not None and currency:
        try:
            conversion = convert(
                total, currency, base_currency, fx_provider, on=invoice.issue_date.value
            )
        except FxRateUnavailableError as exc:
            issues.append(
                Issue(
                    "fx_rate_unavailable",
                    Severity.WARNING,
                    f"Could not convert {currency} to {base_currency}: {exc}",
                    "total",
                )
            )

    categories = (
        categorizer.categorize([item.description for item in invoice.line_items])
        if categorizer is not None
        else []
    )
    return EnrichmentResult(match, conversion, categories, issues)


__all__ = ["EnrichmentResult", "enrich_invoice"]
