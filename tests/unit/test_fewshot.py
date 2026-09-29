"""Few-shot examples: prompt rendering, value location, vendor detection, fake provider."""

from __future__ import annotations

import json
from datetime import date
from decimal import Decimal

from extraction.enrich.vendors import VendorMatcher, VendorRecord
from extraction.evidence import quote_for_value, value_renderings
from extraction.extract import (
    EXAMPLES_INTRO,
    FewShotExample,
    extract_structured,
    parse_model_output,
    render_examples,
)
from extraction.llm.base import ChatMessage, JsonRequest
from extraction.llm.fake import FakeProvider, learned_labels
from extraction.parse import ParsedDocument
from extraction.schemas import Invoice
from tests.unit.test_extract import ScriptedProvider, valid_payload

MARCH = """Haverford Office Interiors Ltd
VAT Reg No: GB 318 4476 25
Document no.:    HOI-7731    Bill to:
Tax point:    06/03/2026    Acme Analytics GmbH
Payment due:    05/04/2026
Subtotal    1,420.00
VAT 20%    284.00
Total (GBP)    1,704.00"""

APRIL = MARCH.replace("HOI-7731", "HOI-7802").replace("06/03/2026", "03/04/2026")


def example(**fields: dict[str, object]) -> FewShotExample:
    return FewShotExample("document #7", dict(fields))


# --- prompt rendering ---------------------------------------------------------------
def test_render_examples_respects_the_character_budget() -> None:
    first = example(invoice_number={"value": "HOI-7731", "evidence": "Document no.: HOI-7731"})
    second = FewShotExample("document #3", {"total": {"value": "1.00"}})
    block, used = render_examples([first, second], max_chars=len(EXAMPLES_INTRO) + 150)
    assert used == [first]
    assert block.startswith("<verified_examples>\n") and block.endswith("</verified_examples>")
    assert '<example source="document #7">' in block and "document #3" not in block
    assert render_examples([first], max_chars=10) == ("", [])


def test_extraction_prompt_carries_examples_before_the_document() -> None:
    provider = ScriptedProvider(json.dumps(valid_payload()))
    document = ParsedDocument.from_page_texts(["ACME Ltd"], "text")
    result = extract_structured(
        document, Invoice, provider, examples=[example(total={"value": "10.00"})]
    )
    prompt = provider.requests[0].messages[0].content
    assert result.examples == ["document #7"]
    assert prompt.index("<verified_examples>") < prompt.index("<document>")
    assert "never copy their values" in prompt


def test_no_examples_means_no_block() -> None:
    provider = ScriptedProvider(json.dumps(valid_payload()))
    document = ParsedDocument.from_page_texts(["ACME Ltd"], "text")
    result = extract_structured(document, Invoice, provider)
    assert result.examples == []
    assert "verified_examples" not in provider.requests[0].messages[0].content


def test_parse_model_output_tolerates_fences() -> None:
    text = f"```json\n{json.dumps(valid_payload())}\n```"
    assert parse_model_output(Invoice, text).invoice_number.value == "A-1"


# --- locating corrected values --------------------------------------------------------
def test_value_renderings() -> None:
    assert value_renderings(date(2026, 3, 6))[:4] == [
        "2026-03-06", "06.03.2026", "06/03/2026", "03/06/2026",
    ]  # fmt: skip
    assert value_renderings(Decimal("1420")) == ["1,420.00", "1420.00", "1.420,00", "1420,00"]
    assert value_renderings("  ") == []


def test_quote_for_value_stops_at_the_value() -> None:
    document = ParsedDocument.from_page_texts([MARCH], "pdf")
    assert quote_for_value(document, "HOI-7731") == "Document no.: HOI-7731"
    assert quote_for_value(document, date(2026, 3, 6)) == "Tax point: 06/03/2026"
    assert quote_for_value(document, Decimal("284")) == "VAT 20% 284.00"
    assert quote_for_value(document, "not printed") is None


# --- vendor detection before extraction -------------------------------------------------
VENDORS = [
    VendorRecord(1, "Haverford Office Interiors Ltd", "GB318447625"),
    VendorRecord(2, "Northwind Traders Limited", None, ("Northwind Traders Ltd.",)),
    VendorRecord(3, "AB", None),
]


def test_find_vendor_by_printed_tax_id_even_with_spaces() -> None:
    assert VendorMatcher(VENDORS).find_in_text(MARCH.replace("Haverford", "HVF")) == VENDORS[0]


def test_find_vendor_by_name_or_alias_as_whole_words() -> None:
    matcher = VendorMatcher(VENDORS)
    assert matcher.find_in_text("NORTHWIND TRADERS LTD.\nInvoice") == VENDORS[1]
    assert matcher.find_in_text("Southnorthwind Traders plc") is None
    assert matcher.find_in_text("AB testing ltd") is None  # names under 4 chars are ignored


# --- fake provider honours examples -----------------------------------------------------
def _prompt(text: str, record: dict[str, object]) -> str:
    block, _ = render_examples([FewShotExample("document #1", record)], 4000)
    return f'{block}\n<document>\n<page number="1">\n{text}\n</page>\n</document>'


def _extract(prompt: str) -> dict[str, dict[str, object]]:
    request = JsonRequest(system="", messages=[ChatMessage("user", prompt)], schema=Invoice)
    return json.loads(FakeProvider().complete_json(request).text)  # type: ignore[no-any-return]


CORRECTED = {
    "invoice_number": {"value": "HOI-7731", "evidence": "Document no.: HOI-7731",
                       "reviewer_corrected_from": None},
    "issue_date": {"value": "2026-03-06", "evidence": "Tax point: 06/03/2026",
                   "reviewer_corrected_from": None},
    "tax": {"value": "284.00", "evidence": "VAT 20% 284.00", "reviewer_corrected_from": "2026.00"},
    "vendor_name": {"value": "Haverford Office Interiors Ltd",
                    "evidence": "Haverford Office Interiors Ltd"},
}  # fmt: skip


def test_learned_labels_from_examples() -> None:
    labels = learned_labels(_prompt("x", CORRECTED))
    assert {name: (label.label, label.corrected) for name, label in labels.items()} == {
        "invoice_number": ("Document no.", True),
        "issue_date": ("Tax point", True),
        "tax": ("VAT 20%", True),
    }  # the vendor name is the whole quote: no label before it


def test_fake_provider_without_examples_misses_unknown_labels() -> None:
    data = _extract(_prompt(APRIL, {}))
    assert data["invoice_number"]["value"] is None and data["issue_date"]["value"] is None
    assert data["tax"]["value"] == "2026"  # "Tax point: 03/04/2026" misread as the tax line


def test_fake_provider_reads_learned_labels_and_prefers_corrected_fields() -> None:
    data = _extract(_prompt(APRIL, CORRECTED))
    assert data["invoice_number"]["value"] == "HOI-7802"
    assert data["invoice_number"]["evidence"]["quote"] == "Document no.: HOI-7802"
    assert data["issue_date"]["value"] == "2026-04-03"
    assert data["tax"] == {"value": "284.00", "confidence": 0.9,
                           "evidence": {"quote": "VAT 20% 284.00", "page": 1}}  # fmt: skip


def test_accepted_fields_do_not_override_confident_readings() -> None:
    accepted = {"tax": {"value": "284.00", "evidence": "VAT 20% 284.00"}}
    assert _extract(_prompt(APRIL, accepted))["tax"]["value"] == "2026"
