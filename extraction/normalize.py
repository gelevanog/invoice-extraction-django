"""Locale-tolerant parsing helpers for amounts, dates and names."""

from __future__ import annotations

import re
import unicodedata
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

_AMOUNT_CHARS = re.compile(r"[^\d.,\-]")


def parse_amount(raw: str) -> Decimal | None:
    """Parse ``1,338.00`` / ``1.158,58`` / ``€ 42,50`` / ``-12.5`` into a Decimal.

    The right-most separator is treated as the decimal mark when both appear. A lone
    comma followed by exactly two digits is a decimal comma; otherwise commas are
    thousands separators.
    """
    cleaned = _AMOUNT_CHARS.sub("", raw.strip())
    if not cleaned or not any(ch.isdigit() for ch in cleaned):
        return None
    if "," in cleaned and "." in cleaned:
        if cleaned.rfind(",") > cleaned.rfind("."):
            cleaned = cleaned.replace(".", "").replace(",", ".")
        else:
            cleaned = cleaned.replace(",", "")
    elif "," in cleaned:
        head, _, tail = cleaned.rpartition(",")
        cleaned = f"{head.replace(',', '')}.{tail}" if len(tail) == 2 else cleaned.replace(",", "")
    try:
        return Decimal(cleaned)
    except InvalidOperation:
        return None


_DATE_FORMATS = (
    "%Y-%m-%d",
    "%d.%m.%Y",
    "%d/%m/%Y",
    "%d %B %Y",
    "%d %b %Y",
    "%B %d, %Y",
    "%b %d, %Y",
    "%B %d %Y",
    "%d-%b-%Y",
)


def parse_date(raw: str) -> date | None:
    """Parse common invoice date formats. Ambiguous ``01/02/2026`` is read day-first."""
    text = " ".join(raw.split()).strip(" .")
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


_LEGAL_SUFFIXES = (
    "gmbh & co kg",
    "gmbh",
    "ag",
    "kg",
    "ltd",
    "limited",
    "llc",
    "inc",
    "incorporated",
    "corp",
    "corporation",
    "co",
    "company",
    "plc",
    "bv",
    "nv",
    "sa",
    "sarl",
    "sas",
    "srl",
    "spa",
    "oy",
    "ab",
    "as",
)
_SUFFIX_RE = re.compile(r"\b(?:" + "|".join(re.escape(s) for s in _LEGAL_SUFFIXES) + r")\s*$")
_NON_ALNUM = re.compile(r"[^a-z0-9& ]+")


def normalize_company_name(name: str) -> str:
    """Lower-case, strip accents, punctuation and trailing legal-form suffixes.

    ``"Northwind Traders Ltd."`` and ``"NORTHWIND TRADERS LIMITED"`` both become
    ``"northwind traders"``.
    """
    ascii_name = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    # Join dotted abbreviations first so "B.V." -> "bv" and "S.A." -> "sa".
    text = re.sub(r"\b([a-z])\.(?=[a-z]\.)", r"\1", ascii_name.lower())
    text = " ".join(_NON_ALNUM.sub(" ", text).split())
    previous = None
    while previous != text:
        previous = text
        text = _SUFFIX_RE.sub("", text).strip()
    return text or " ".join(_NON_ALNUM.sub(" ", ascii_name.lower()).split())
