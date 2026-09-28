"""Structured issues produced by validation and enrichment."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class Severity(StrEnum):
    ERROR = "error"  # blocks auto-approval
    WARNING = "warning"  # shown to reviewers, does not block
    INFO = "info"  # context only


@dataclass(frozen=True, slots=True)
class Issue:
    code: str
    severity: Severity
    message: str
    field: str | None = None

    def as_dict(self) -> dict[str, str | None]:
        return {
            "code": self.code,
            "severity": self.severity.value,
            "message": self.message,
            "field": self.field,
        }
