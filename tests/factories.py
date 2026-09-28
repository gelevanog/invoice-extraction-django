"""Helpers to build pipeline schemas concisely in tests."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any

from extraction.schemas import Invoice


def field(value: Any, confidence: float = 0.95, quote: str | None = None) -> dict[str, Any]:
    evidence = {"quote": quote, "page": 1} if quote else None
    return {"value": value, "confidence": confidence, "evidence": evidence}


def make_invoice(**overrides: Any) -> Invoice:
    """A valid, internally consistent invoice; override any field with a raw dict or value."""
    data: dict[str, Any] = {
        "vendor_name": field("Brightline Software GmbH"),
        "vendor_tax_id": field("DE811234567"),
        "invoice_number": field("BLS-1"),
        "issue_date": field(date(2026, 3, 2)),
        "due_date": field(date(2026, 4, 1)),
        "currency": field("EUR"),
        "subtotal": field(Decimal("150.00")),
        "tax": field(Decimal("28.50")),
        "total": field(Decimal("178.50")),
        "line_items": [
            {"description": "Seat licence", "quantity": 2, "unit_price": "50.00",
             "amount": "100.00", "confidence": 0.9},
            {"description": "Support", "quantity": 1, "unit_price": "50.00",
             "amount": "50.00", "confidence": 0.9},
        ],
    }  # fmt: skip
    for key, value in overrides.items():
        data[key] = value if key == "line_items" or isinstance(value, dict) else field(value)
    return Invoice.model_validate(data)
