"""Vendor normalization: map an extracted vendor name to a canonical vendor record."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Literal

from rapidfuzz import fuzz, process

from extraction.normalize import normalize_company_name

MatchMethod = Literal["tax_id", "fuzzy", "none"]


@dataclass(frozen=True, slots=True)
class VendorRecord:
    id: int | str
    name: str
    tax_id: str | None = None
    aliases: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class VendorMatch:
    vendor: VendorRecord | None
    score: float
    method: MatchMethod
    matched_on: str | None = None

    @property
    def is_new(self) -> bool:
        return self.vendor is None


def _compact(tax_id: str | None) -> str:
    return "".join((tax_id or "").split()).upper()


class VendorMatcher:
    """Match by exact tax ID first, then fuzzy name similarity (rapidfuzz).

    Names are normalized (case, accents, punctuation, legal suffixes such as GmbH/Ltd)
    before scoring with ``token_sort_ratio``, so word order and "Ltd." vs "Limited"
    do not matter while genuinely different companies stay apart.
    """

    def __init__(self, vendors: Iterable[VendorRecord], threshold: float = 85.0) -> None:
        self.threshold = threshold
        self._vendors = list(vendors)
        self._by_tax_id = {_compact(v.tax_id): v for v in self._vendors if v.tax_id}
        self._choices: dict[int, str] = {}
        self._owners: dict[int, tuple[VendorRecord, str]] = {}
        for vendor in self._vendors:
            for alias in (vendor.name, *vendor.aliases):
                key = len(self._choices)
                self._choices[key] = normalize_company_name(alias)
                self._owners[key] = (vendor, alias)

    def match(self, name: str | None, tax_id: str | None = None) -> VendorMatch:
        if tax_id and (vendor := self._by_tax_id.get(_compact(tax_id))):
            return VendorMatch(vendor, 100.0, "tax_id", vendor.tax_id)
        if not name or not self._choices:
            return VendorMatch(None, 0.0, "none")

        best = process.extractOne(
            normalize_company_name(name),
            self._choices,
            scorer=fuzz.token_sort_ratio,
            score_cutoff=self.threshold,
        )
        if best is None:
            return VendorMatch(None, 0.0, "none")
        _, score, key = best
        vendor, alias = self._owners[key]
        return VendorMatch(vendor, round(float(score), 1), "fuzzy", alias)


def same_vendor(
    name_a: str | None,
    tax_a: str | None,
    name_b: str | None,
    tax_b: str | None,
    threshold: float = 85.0,
) -> bool:
    """Whether two (name, tax id) pairs refer to the same company."""
    if tax_a and tax_b:
        return _compact(tax_a) == _compact(tax_b)
    if not name_a or not name_b:
        return False
    score = fuzz.token_sort_ratio(normalize_company_name(name_a), normalize_company_name(name_b))
    return score >= threshold
