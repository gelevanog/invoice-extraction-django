"""Document status machine shared by the pipeline and the Django models."""

from __future__ import annotations

from enum import StrEnum


class DocumentStatus(StrEnum):
    UPLOADED = "uploaded"
    PARSED = "parsed"
    EXTRACTED = "extracted"
    VALIDATED = "validated"
    ENRICHED = "enriched"
    NEEDS_REVIEW = "needs_review"
    APPROVED = "approved"
    REJECTED = "rejected"
    FAILED = "failed"


S = DocumentStatus

TRANSITIONS: dict[DocumentStatus, frozenset[DocumentStatus]] = {
    S.UPLOADED: frozenset({S.PARSED, S.FAILED}),
    S.PARSED: frozenset({S.EXTRACTED, S.FAILED}),
    S.EXTRACTED: frozenset({S.VALIDATED, S.FAILED}),
    S.VALIDATED: frozenset({S.ENRICHED, S.FAILED}),
    S.ENRICHED: frozenset({S.NEEDS_REVIEW, S.APPROVED, S.FAILED}),
    # Human decisions; "uploaded" means "reprocess from scratch".
    S.NEEDS_REVIEW: frozenset({S.APPROVED, S.REJECTED, S.UPLOADED}),
    S.REJECTED: frozenset({S.UPLOADED}),
    S.FAILED: frozenset({S.UPLOADED}),
    S.APPROVED: frozenset(),
}

IN_PROGRESS: frozenset[DocumentStatus] = frozenset(
    {S.UPLOADED, S.PARSED, S.EXTRACTED, S.VALIDATED, S.ENRICHED}
)


class InvalidTransitionError(Exception):
    def __init__(self, current: DocumentStatus, target: DocumentStatus) -> None:
        super().__init__(f"Cannot move a document from '{current}' to '{target}'")
        self.current = current
        self.target = target


def can_transition(current: DocumentStatus, target: DocumentStatus) -> bool:
    return target in TRANSITIONS[current]


def ensure_transition(current: DocumentStatus, target: DocumentStatus) -> None:
    if not can_transition(current, target):
        raise InvalidTransitionError(current, target)
