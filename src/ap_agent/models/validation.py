"""Phase 5 (financial validation) contracts.

Source: notebook cell 68 ("PHASE 5 — CELL 1"), active-definition table §2
`models/validation.py`. `FinancialValidationConfig` is a configuration
dataclass and lives in `ap_agent.config.settings` instead of here.
`validation_utc_now` and `create_validation_id` are processing functions
deferred to the tools-extraction milestone.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import Enum
from uuid import UUID

from ap_agent.models.normalization import NormalizedInvoiceRecord

__all__ = [
    "ValidationStatus",
    "ValidationCheckStatus",
    "ValidationCheckType",
    "ValidationSeverity",
    "ValidationInput",
    "ValidationOperand",
    "ValidationCheckResult",
    "FinancialValidationSummary",
    "ValidationEvent",
    "FinancialValidationResult",
]


class ValidationStatus(str, Enum):
    """
    Overall Phase 5 processing status.

    FAILED is reserved for execution or integrity failures.
    Financial discrepancies normally produce REVIEW_REQUIRED.
    """

    SUCCEEDED = "SUCCEEDED"
    REVIEW_REQUIRED = "REVIEW_REQUIRED"
    FAILED = "FAILED"


class ValidationCheckStatus(str, Enum):
    """Outcome of one deterministic validation check."""

    PASSED = "PASSED"
    FAILED = "FAILED"
    REVIEW_REQUIRED = "REVIEW_REQUIRED"
    NOT_APPLICABLE = "NOT_APPLICABLE"
    SKIPPED = "SKIPPED"


class ValidationCheckType(str, Enum):
    """Supported Phase 5 validation checks."""

    INHERITED_REVIEW = "INHERITED_REVIEW"
    REQUIRED_FINANCIAL_FIELDS = "REQUIRED_FINANCIAL_FIELDS"

    LINE_ITEM_ARITHMETIC = "LINE_ITEM_ARITHMETIC"

    LINE_ITEMS_TO_SUBTOTAL = "LINE_ITEMS_TO_SUBTOTAL"

    INVOICE_TOTAL_RECONCILIATION = "INVOICE_TOTAL_RECONCILIATION"

    CURRENCY_CONSISTENCY = "CURRENCY_CONSISTENCY"

    DATE_CONSISTENCY = "DATE_CONSISTENCY"
    MONETARY_VALUE_VALIDITY = "MONETARY_VALUE_VALIDITY"


class ValidationSeverity(str, Enum):
    """Business significance of a validation result."""

    INFORMATION = "INFORMATION"
    WARNING = "WARNING"
    ERROR = "ERROR"
    CRITICAL = "CRITICAL"


@dataclass(frozen=True)
class ValidationInput:
    """Input contract connecting Phase 4 to Phase 5."""

    batch_id: UUID
    document_id: UUID

    source_name: str
    source_document_sha256: str

    normalization_version: str
    invoice_record: NormalizedInvoiceRecord


@dataclass(frozen=True)
class ValidationOperand:
    """
    One value used by a validation check.

    Values are stored as text for safe audit serialization.
    Their source field and line-item identities remain attached.
    """

    name: str
    value: str | None

    field_id: UUID | None = None
    line_item_id: UUID | None = None

    evidence_reference_ids: tuple[UUID, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class ValidationCheckResult:
    """Auditable result of one validation rule."""

    check_id: UUID
    check_type: ValidationCheckType
    status: ValidationCheckStatus
    severity: ValidationSeverity

    message: str

    operands: tuple[ValidationOperand, ...] = field(default_factory=tuple)

    expected_value: str | None = None
    observed_value: str | None = None

    difference: Decimal | None = None
    tolerance: Decimal | None = None

    review_required: bool = False

    reason_codes: tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class FinancialValidationSummary:
    """Summary of all checks performed for one invoice."""

    total_checks: int

    passed_checks: int
    failed_checks: int
    review_required_checks: int
    not_applicable_checks: int
    skipped_checks: int

    review_required: bool


@dataclass(frozen=True)
class ValidationEvent:
    """Phase 5 audit event."""

    event_type: str
    status: ValidationStatus

    batch_id: UUID
    document_id: UUID

    occurred_at: datetime
    message: str

    review_required: bool


@dataclass(frozen=True)
class FinancialValidationResult:
    """Complete Phase 5 result for one invoice."""

    batch_id: UUID
    document_id: UUID

    source_name: str
    source_document_sha256: str

    invoice_record_id: UUID
    normalization_version: str
    validation_version: str

    status: ValidationStatus

    checks: tuple[ValidationCheckResult, ...]

    summary: FinancialValidationSummary
    event: ValidationEvent

    review_required: bool = False

    review_reasons: tuple[str, ...] = field(default_factory=tuple)

    errors: tuple[str, ...] = field(default_factory=tuple)
