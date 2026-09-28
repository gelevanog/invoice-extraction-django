from __future__ import annotations

import json
from datetime import date
from decimal import Decimal

import httpx
import pytest

from extraction.enrich import enrich_invoice
from extraction.enrich.categorize import (
    ExpenseCategory,
    KeywordCategorizer,
    LineItemCategories,
    LLMCategorizer,
)
from extraction.enrich.fx import (
    FrankfurterFxRatesProvider,
    FxRateUnavailableError,
    StaticFxRatesProvider,
    convert,
)
from extraction.enrich.vendors import VendorMatcher, VendorRecord, same_vendor
from extraction.llm.base import JsonRequest, LLMError, LLMResponse
from tests.factories import make_invoice

VENDORS = [
    VendorRecord(1, "Brightline Software GmbH", "DE811234567"),
    VendorRecord(2, "Northwind Traders Limited", None, ("NWT Office Supplies",)),
    VendorRecord(3, "Kestrel Freight Logistics B.V.", "NL853746291B01"),
    VendorRecord(4, "Northern Wind Energy AG", None),
]


# --- vendors ------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("name", "tax_id", "vendor_id", "method"),
    [
        ("Anything at all", "DE 811 234 567", 1, "tax_id"),  # tax ID wins over name
        ("Northwind Traders Ltd.", None, 2, "fuzzy"),
        ("NORTHWIND TRADERS", None, 2, "fuzzy"),
        ("Nortwind Traders Limited", None, 2, "fuzzy"),  # typo
        ("Traders Northwind Ltd", None, 2, "fuzzy"),  # word order
        ("NWT Office Supplies Ltd", None, 2, "fuzzy"),  # known alias
        ("Kestrel Freight Logistics", None, 3, "fuzzy"),
        ("Bluepeak Consulting LLC", None, None, "none"),
        ("Northern Trading Co", None, None, "none"),
        (None, None, None, "none"),
    ],
)
def test_vendor_matching(
    name: str | None, tax_id: str | None, vendor_id: int | None, method: str
) -> None:
    match = VendorMatcher(VENDORS).match(name, tax_id)
    assert (match.vendor.id if match.vendor else None) == vendor_id
    assert match.method == method
    assert match.is_new is (vendor_id is None)


def test_vendor_threshold_is_configurable() -> None:
    assert VendorMatcher(VENDORS, threshold=99).match("Nortwind Traders").is_new
    assert not VendorMatcher(VENDORS, threshold=80).match("Nortwind Traders").is_new


def test_matcher_with_no_vendors() -> None:
    assert VendorMatcher([]).match("Anyone").method == "none"


@pytest.mark.parametrize(
    ("a", "b", "expected"),
    [
        (("Brightline Software GmbH", None), ("BRIGHTLINE SOFTWARE", None), True),
        (("Brightline", "DE811234567"), ("Other name", "DE 811234567"), True),
        (
            ("Brightline Software GmbH", "DE811234567"),
            ("Brightline Software GmbH", "DE999999999"),
            False,
        ),
        (("Bluepeak Consulting", None), ("Brightline Software", None), False),
        ((None, None), ("Brightline", None), False),
    ],
)
def test_same_vendor(a: tuple, b: tuple, expected: bool) -> None:
    assert same_vendor(*a, *b) is expected


# --- FX -----------------------------------------------------------------------------
STATIC = StaticFxRatesProvider("EUR", {"USD": Decimal("1.0850"), "GBP": Decimal("0.8540")})


@pytest.mark.parametrize(
    ("amount", "source", "target", "expected"),
    [
        ("100.00", "EUR", "EUR", "100.00"),
        ("108.50", "USD", "EUR", "100.00"),
        ("100.00", "EUR", "USD", "108.50"),
        ("656.40", "GBP", "EUR", "768.62"),
        ("100.00", "GBP", "USD", "127.05"),  # cross rate via EUR
        ("0.01", "USD", "EUR", "0.01"),  # rounds half-up to cents
    ],
)
def test_static_conversion(amount: str, source: str, target: str, expected: str) -> None:
    result = convert(Decimal(amount), source, target, STATIC)
    assert result.amount == Decimal(expected)
    assert result.currency == target
    assert result.provider == "static"


def test_unknown_currency_raises() -> None:
    with pytest.raises(FxRateUnavailableError):
        STATIC.get_rate("JPY", "EUR")


def test_static_provider_loads_bundled_table() -> None:
    provider = StaticFxRatesProvider.from_file()
    assert provider.base == "EUR"
    assert provider.get_rate("USD", "USD") == Decimal(1)
    assert provider.get_rate("EUR", "GBP") > 0


def test_frankfurter_provider_uses_api_and_caches() -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json={"base": "GBP", "rates": {"EUR": 1.1712}})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    provider = FrankfurterFxRatesProvider(base_url="https://fx.test/v1", client=client)
    on = date(2026, 3, 14)
    assert provider.get_rate("GBP", "EUR", on) == Decimal("1.1712")
    assert provider.get_rate("gbp", "eur", on) == Decimal("1.1712")
    assert len(calls) == 1
    assert str(calls[0].url) == "https://fx.test/v1/2026-03-14?base=GBP&symbols=EUR"
    assert provider.get_rate("EUR", "EUR") == Decimal(1)


def test_frankfurter_errors_become_rate_unavailable() -> None:
    client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(404)))
    provider = FrankfurterFxRatesProvider(client=client)
    with pytest.raises(FxRateUnavailableError):
        provider.get_rate("XXX", "EUR")


# --- categorization -----------------------------------------------------------------
@pytest.mark.parametrize(
    ("description", "category"),
    [
        ("Team plan subscription (March)", ExpenseCategory.SOFTWARE),
        ("USB-C docking station", ExpenseCategory.HARDWARE),
        ("Toner cartridge TN-2420", ExpenseCategory.OFFICE_SUPPLIES),
        ("Strategy workshop (2 days on site)", ExpenseCategory.PROFESSIONAL_SERVICES),
        ("Pallet transport Rotterdam - Berlin", ExpenseCategory.LOGISTICS),
        ("Hotel Berlin 2 nights", ExpenseCategory.TRAVEL),
        ("Rapid capital allocation", ExpenseCategory.OTHER),  # "api" only matches word starts
    ],
)
def test_keyword_categorizer(description: str, category: ExpenseCategory) -> None:
    assert KeywordCategorizer().categorize([description]) == [category]


class _StubProvider:
    name = "stub"
    model = "stub-1"

    def __init__(self, text: str | None = None, error: Exception | None = None) -> None:
        self.text, self.error = text, error
        self.requests: list[JsonRequest] = []

    def complete_json(self, request: JsonRequest) -> LLMResponse:
        self.requests.append(request)
        if self.error:
            raise self.error
        return LLMResponse(text=self.text or "", model=self.model)


def test_llm_categorizer_uses_model_answer() -> None:
    answer = {
        "assignments": [{"index": 1, "category": "travel"}, {"index": 0, "category": "marketing"}]
    }
    provider = _StubProvider(json.dumps(answer))
    result = LLMCategorizer(provider).categorize(["Flyers", "Taxi"])
    assert result == [ExpenseCategory.MARKETING, ExpenseCategory.TRAVEL]
    assert provider.requests[0].schema is LineItemCategories


@pytest.mark.parametrize(
    "provider",
    [
        _StubProvider(error=LLMError("down")),
        _StubProvider("not json"),
        _StubProvider(
            json.dumps({"assignments": [{"index": 0, "category": "travel"}]})
        ),  # incomplete
    ],
)
def test_llm_categorizer_falls_back_to_keywords(provider: _StubProvider) -> None:
    result = LLMCategorizer(provider).categorize(["USB-C docking station", "Toner"])
    assert result == [ExpenseCategory.HARDWARE, ExpenseCategory.OFFICE_SUPPLIES]


# --- enrich_invoice -----------------------------------------------------------------
def test_enrich_invoice_combines_all_steps() -> None:
    invoice = make_invoice(vendor_name="Northwind Traders Ltd.", vendor_tax_id=None,
                           currency="GBP", total=Decimal("656.40"))  # fmt: skip
    result = enrich_invoice(
        invoice,
        vendor_matcher=VendorMatcher(VENDORS),
        fx_provider=STATIC,
        categorizer=KeywordCategorizer(),
        base_currency="EUR",
    )
    assert result.vendor_match.vendor is not None and result.vendor_match.vendor.id == 2
    assert result.conversion is not None and result.conversion.amount == Decimal("768.62")
    assert len(result.categories) == 2
    assert result.issues == []


def test_enrich_reports_new_vendor_and_missing_rate() -> None:
    invoice = make_invoice(vendor_name="Unknown Co", vendor_tax_id=None, currency="JPY")
    result = enrich_invoice(
        invoice,
        vendor_matcher=VendorMatcher(VENDORS),
        fx_provider=STATIC,
        categorizer=None,
        base_currency="EUR",
    )
    assert [i.code for i in result.issues] == ["new_vendor", "fx_rate_unavailable"]
    assert result.conversion is None
    assert result.categories == []
