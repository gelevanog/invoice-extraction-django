"""Currency conversion with pluggable exchange-rate providers."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from functools import lru_cache
from pathlib import Path
from typing import Protocol

import httpx

DEFAULT_RATES_FILE = Path(__file__).resolve().parent.parent / "data" / "fx_rates.json"
CENT = Decimal("0.01")


class FxRateUnavailableError(Exception):
    pass


class FxRatesProvider(Protocol):
    name: str

    def get_rate(self, source: str, target: str, on: date | None = None) -> Decimal:
        """Units of ``target`` per 1 unit of ``source``."""
        ...


class StaticFxRatesProvider:
    """Rates from a JSON table: ``{"base": "EUR", "as_of": "...", "rates": {"USD": 1.08}}``.

    Rates are quoted as units of currency per 1 unit of ``base``; cross rates are
    derived through the base. The date argument is ignored (one snapshot).
    """

    name = "static"

    def __init__(self, base: str, rates: dict[str, Decimal], as_of: str | None = None) -> None:
        self.base = base.upper()
        self.as_of = as_of
        self._rates = {code.upper(): rate for code, rate in rates.items()}
        self._rates[self.base] = Decimal(1)

    @classmethod
    def from_file(cls, path: Path | str = DEFAULT_RATES_FILE) -> StaticFxRatesProvider:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
        rates = {code: Decimal(str(value)) for code, value in payload["rates"].items()}
        return cls(payload["base"], rates, payload.get("as_of"))

    def get_rate(self, source: str, target: str, on: date | None = None) -> Decimal:
        source, target = source.upper(), target.upper()
        try:
            return self._rates[target] / self._rates[source]
        except KeyError as exc:
            raise FxRateUnavailableError(f"No static rate for {exc.args[0]}") from exc


class FrankfurterFxRatesProvider:
    """Historical ECB reference rates from the free, keyless Frankfurter API."""

    name = "frankfurter"

    def __init__(
        self,
        base_url: str = "https://api.frankfurter.dev/v1",
        client: httpx.Client | None = None,
        timeout: float = 10.0,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._client = client or httpx.Client(timeout=timeout)
        self._cached = lru_cache(maxsize=512)(self._fetch)

    def get_rate(self, source: str, target: str, on: date | None = None) -> Decimal:
        source, target = source.upper(), target.upper()
        if source == target:
            return Decimal(1)
        return self._cached(source, target, on.isoformat() if on else "latest")

    def _fetch(self, source: str, target: str, day: str) -> Decimal:
        try:
            response = self._client.get(
                f"{self._base_url}/{day}", params={"base": source, "symbols": target}
            )
            response.raise_for_status()
            return Decimal(str(response.json()["rates"][target]))
        except (httpx.HTTPError, KeyError, ValueError) as exc:
            raise FxRateUnavailableError(
                f"Frankfurter lookup {source}->{target} failed: {exc}"
            ) from exc


@dataclass(frozen=True, slots=True)
class Conversion:
    amount: Decimal
    currency: str
    rate: Decimal
    provider: str


def convert(
    amount: Decimal,
    source: str,
    target: str,
    provider: FxRatesProvider,
    on: date | None = None,
) -> Conversion:
    rate = provider.get_rate(source, target, on)
    converted = (amount * rate).quantize(CENT, rounding=ROUND_HALF_UP)
    return Conversion(converted, target.upper(), rate.quantize(Decimal("0.000001")), provider.name)
