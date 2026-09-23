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
"""

from typing import Any

from ap_agent.models.ingestion import IngestionErrorCode

__all__ = ["IngestionValidationError", "NormalizationIntegrityError"]


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
