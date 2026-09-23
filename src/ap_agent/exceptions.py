"""Shared exceptions for the accounts payable agent.

Source: notebook cell 10 (Phase 1 contracts), active-definition table
§2 `exceptions.py`.

`NormalizationIntegrityError` is new in M5: it did not exist in the
notebook (the Phase 3 -> Phase 4 bridge was inline orchestration code
there, not a reusable typed function), but the M5 task brief requires the
modular typed bridge to fail closed on an integrity failure rather than
silently continuing or raising a bare `ValueError` like the notebook's
`normalize_invoice_document` does internally. See
`ap_agent.tools.normalization.build_normalization_input`.

`FinancialValidationIntegrityError` is new in M6, the same category of
addition for the Phase 4 -> Phase 5 bridge
(`ap_agent.tools.financial_validation.build_validation_input`) and for the
Phase 5 idempotent-persistence collision guard
(`ap_agent.tools.financial_validation.persist_financial_validation_result`).
The notebook's own cell-69 `build_validation_input` raised a bare
`ValueError` for its four identity checks; this module keeps those checks
but fails closed with this structured exception instead, and adds the
artifact-existence/hash check the task brief requires (M6 task §3) that the
notebook itself never performed.
"""

from typing import Any

from ap_agent.models.ingestion import IngestionErrorCode

__all__ = [
    "IngestionValidationError",
    "NormalizationIntegrityError",
    "FinancialValidationIntegrityError",
]


class IngestionValidationError(Exception):
    """Structured failure raised when document ingestion cannot continue."""

    def __init__(
        self,
        code: IngestionErrorCode,
        message: str,
        details: dict[str, Any] | None = None,
    ):
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code.value,
            "message": self.message,
            "details": self.details,
        }


class NormalizationIntegrityError(Exception):
    """Raised when the Phase 3 -> Phase 4 bridge cannot verify that the OCR
    result it was given is the correct, current, persisted, post-guard
    result for the expected batch and document (M5 task §3). Fail closed:
    the caller must not proceed to normalisation."""

    def __init__(self, reason: str, details: dict[str, Any] | None = None):
        super().__init__(reason)
        self.reason = reason
        self.details = details or {}


class FinancialValidationIntegrityError(Exception):
    """Raised when the Phase 4 -> Phase 5 bridge cannot verify that the
    normalized invoice it was given is the correct, current, persisted
    result for the expected batch and document (M6 task §3), or when Phase
    5 artifact persistence detects a different-bytes collision on rerun
    (M6 task §16). Fail closed: the caller must not proceed."""

    def __init__(self, reason: str, details: dict[str, Any] | None = None):
        super().__init__(reason)
        self.reason = reason
        self.details = details or {}
