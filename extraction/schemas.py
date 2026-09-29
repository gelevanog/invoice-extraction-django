"""Pydantic schemas the LLM must fill in.

Every scalar field is wrapped in :class:`Extracted`, which carries the value together
with the model's confidence and the verbatim evidence it was read from. Evidence is a
quote (not character offsets, which LLMs get wrong); the pipeline resolves quotes to
offsets deterministically in :mod:`extraction.evidence`.

To extract a different document type (purchase orders, resumes, contracts...), define
another ``BaseModel`` built from the same pieces and pass it to
:func:`extraction.extract.extract_structured`.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class Evidence(BaseModel):
    """Where in the source document a value was found."""

    model_config = ConfigDict(extra="forbid")

    quote: str = Field(
        description="Short verbatim excerpt copied character-for-character from the document "
        "that contains the value (e.g. 'Invoice No: BLS-2026-0142')."
    )
    page: int | None = Field(
        default=None, description="1-based page number the quote appears on, if known."
    )


class Extracted[T](BaseModel):
    """A single extracted value with confidence and evidence."""

    model_config = ConfigDict(extra="forbid")

    # Required (but nullable): a model that forgets the key gets a validation error and
    # another attempt, instead of the omission silently meaning "not on the document".
    value: T | None = Field(
        description="The normalized value, or null if absent from the document."
    )
    confidence: float = Field(
        ge=0.0,
        le=1.0,
        description="Probability in [0, 1] that the value is correct (or correctly absent).",
    )
    evidence: Evidence | None = Field(
        default=None, description="Source excerpt supporting the value; null if absent."
    )


class LineItem(BaseModel):
    """One billed line on the invoice."""

    model_config = ConfigDict(extra="forbid")

    description: str
    quantity: Decimal | None = Field(default=None, description="Quantity; null if not stated.")
    unit_price: Decimal | None = Field(default=None, description="Price per unit, net of tax.")
    amount: Decimal = Field(description="Line total, net of tax, as printed on the document.")
    confidence: float = Field(ge=0.0, le=1.0)
    evidence: Evidence | None = None


class Invoice(BaseModel):
    """Structured representation of an invoice or receipt."""

    model_config = ConfigDict(extra="forbid")

    vendor_name: Extracted[str] = Field(description="Legal name of the seller / supplier.")
    vendor_tax_id: Extracted[str] = Field(
        description="Seller VAT / tax registration number without spaces (e.g. DE811234567)."
    )
    invoice_number: Extracted[str] = Field(description="Invoice or receipt reference number.")
    issue_date: Extracted[date] = Field(description="Invoice date, ISO 8601 (YYYY-MM-DD).")
    due_date: Extracted[date] = Field(description="Payment due date, ISO 8601 (YYYY-MM-DD).")
    currency: Extracted[str] = Field(description="ISO 4217 currency code, e.g. EUR, USD, GBP.")
    subtotal: Extracted[Decimal] = Field(description="Total before tax.")
    tax: Extracted[Decimal] = Field(description="Total tax / VAT amount.")
    total: Extracted[Decimal] = Field(description="Grand total payable, including tax.")
    line_items: list[LineItem] = Field(default_factory=list)

    @field_validator("currency")
    @classmethod
    def _upper_currency(cls, field: Extracted[str]) -> Extracted[str]:
        if field.value is not None:
            field.value = field.value.strip().upper()
        return field

    @field_validator("vendor_tax_id")
    @classmethod
    def _compact_tax_id(cls, field: Extracted[str]) -> Extracted[str]:
        if field.value is not None:
            field.value = "".join(field.value.split()).upper()
        return field

    def scalar_fields(self) -> dict[str, Extracted[object]]:
        """All top-level ``Extracted`` fields keyed by name (line items excluded)."""
        return {
            name: value
            for name in type(self).model_fields
            if isinstance(value := getattr(self, name), Extracted)
        }


# Fields whose confidence gates routing. Line items contribute their own confidence.
INVOICE_ROUTING_FIELDS: tuple[str, ...] = (
    "vendor_name",
    "invoice_number",
    "issue_date",
    "currency",
    "subtotal",
    "tax",
    "total",
)
