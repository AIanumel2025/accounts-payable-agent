"""Shared exceptions for the accounts payable agent.

Source: notebook cell 10 (Phase 1 contracts), active-definition table
§2 `exceptions.py`.
"""

from typing import Any

from ap_agent.models.ingestion import IngestionErrorCode

__all__ = ["IngestionValidationError"]


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
