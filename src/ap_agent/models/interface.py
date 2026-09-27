"""Phase 9 (human-review interface) domain contracts and configuration.

Source: notebook cell 100 ("PHASE 9 — CELL 1", "Human-review interface
contracts and configuration"). Enums, dataclasses, defaults and role
permissions are ported verbatim from that cell's names, field order and
values, with two structural changes required by the M10 task brief:

- `tenant_id` is typed `UUID` everywhere (the notebook's own
  `InterfaceActor.tenant_id: UUID` and every SQL query's `tenant_id UUID`
  column -- migration 0002/0003 -- already agree on this; the M8
  `ap_agent.models.memory` contracts instead carry `tenant_id: str` per
  that module's own docstring, a deliberate M8 departure this module does
  not repeat, since every one of this phase's PostgreSQL columns is
  `UUID NOT NULL`).
- The notebook builds one module-level `interface_config = InterfaceConfig(...)`
  instance. CLAUDE.md ("Configuration is threaded explicitly ... No module
  under `src/` may define a mutable module-level config instance") and the
  M8/M9 precedent (`ap_agent.config.postgres`/`ap_agent.models.orchestration`
  define no module-level config instance either) require a factory function
  instead: `default_interface_config()` returns a fresh `InterfaceConfig`
  with the notebook's exact field values every time it is called, and every
  caller must pass a config explicitly (task §4: "Configuration is threaded
  explicitly, never read as a hidden global").

`ReviewCommandContext` (notebook cell 104's dataclass of the same name) and
`WorkflowResumePlan`/`WorkflowResumeExecution` (notebook cell 107) are
included here too: they are domain contracts read and written by more than
one service module (`ap_agent.services.review_commands`/`review_decisions`/
`workflow_resume`) and by `ap_agent.repositories.review_repository`, so they
belong with the rest of this phase's shapes rather than living inside a
single service module (mirrors `ap_agent.models.memory` hosting
`InvoiceMemoryBundle` for the same reason).

Per CLAUDE.md's dependency graph, `models/` never imports `config/` or
`exceptions`; this module only imports from sibling `models/*` modules
(`memory`, `normalization`, `orchestration`).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
from typing import Any, Optional
from uuid import UUID

from ap_agent.models.memory import HumanReviewDecision, HumanReviewDisposition
from ap_agent.models.normalization import InvoiceFieldName, NormalizedValueType
from ap_agent.models.orchestration import InvoiceWorkflowStatus, OrchestrationStage

__all__ = [
    "InterfaceRole",
    "ReviewCaseStatus",
    "ReviewPriority",
    "ReviewAction",
    "InterfaceCommandStatus",
    "InterfaceConfig",
    "default_interface_config",
    "interface_utc_now",
    "interface_permissions_for_role",
    "InterfaceActor",
    "DashboardRecord",
    "ReviewQueueRecord",
    "InterfaceFieldValue",
    "InterfaceFinancialCheck",
    "InterfaceLineMatch",
    "InterfaceTimelineEvent",
    "InvoiceDetailRecord",
    "ReviewFieldCorrection",
    "ReviewCommand",
    "ReviewCommandResult",
    "ReviewCommandContext",
    "WorkflowResumePlan",
    "WorkflowResumeExecution",
]


# ------------------------------------------------------------
# Interface enumerations (notebook cell 100, verbatim)
# ------------------------------------------------------------


class InterfaceRole(str, Enum):
    AP_OPERATOR = "AP_OPERATOR"
    AP_REVIEWER = "AP_REVIEWER"
    TENANT_ADMIN = "TENANT_ADMIN"
    READ_ONLY_AUDITOR = "READ_ONLY_AUDITOR"


class ReviewCaseStatus(str, Enum):
    OPEN = "OPEN"
    IN_REVIEW = "IN_REVIEW"
    AWAITING_INFORMATION = "AWAITING_INFORMATION"
    ESCALATED = "ESCALATED"
    RESOLVED = "RESOLVED"
    REJECTED = "REJECTED"


class ReviewPriority(str, Enum):
    LOW = "LOW"
    NORMAL = "NORMAL"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"


class ReviewAction(str, Enum):
    CLAIM = "CLAIM"
    RELEASE = "RELEASE"
    ACCEPT = "ACCEPT"
    CORRECT = "CORRECT"
    CONFIRM_SUPPLIER = "CONFIRM_SUPPLIER"
    CONFIRM_PURCHASE_ORDER = "CONFIRM_PURCHASE_ORDER"
    REQUEST_INFORMATION = "REQUEST_INFORMATION"
    ESCALATE = "ESCALATE"
    REJECT = "REJECT"
    RESUME_WORKFLOW = "RESUME_WORKFLOW"


class InterfaceCommandStatus(str, Enum):
    ACCEPTED = "ACCEPTED"
    IDEMPOTENT = "IDEMPOTENT"
    REJECTED = "REJECTED"
    CONFLICT = "CONFLICT"
    FAILED = "FAILED"


# ------------------------------------------------------------
# Interface configuration (notebook cell 100, verbatim values)
# ------------------------------------------------------------


@dataclass(frozen=True)
class InterfaceConfig:
    interface_version: str

    default_page_size: int
    maximum_page_size: int
    claim_timeout_seconds: int

    require_authentication: bool
    enforce_tenant_isolation: bool
    enforce_role_permissions: bool

    append_only_decisions: bool
    require_idempotency_key: bool
    require_observed_revision: bool
    require_correction_reason: bool
    require_correction_evidence: bool

    allow_payment_execution: bool

    correctable_header_fields: tuple[InvoiceFieldName, ...]
    correctable_line_fields: tuple[InvoiceFieldName, ...]
    role_permissions: tuple[tuple[InterfaceRole, tuple[ReviewAction, ...]], ...]

    def __post_init__(self) -> None:
        configured_roles = tuple(role for role, _ in self.role_permissions)

        assert set(configured_roles) == set(InterfaceRole)
        assert len(configured_roles) == len(set(configured_roles))

        assert self.interface_version.strip()
        assert self.default_page_size > 0
        assert self.maximum_page_size >= self.default_page_size
        assert self.claim_timeout_seconds > 0

        assert self.require_authentication is True
        assert self.enforce_tenant_isolation is True
        assert self.enforce_role_permissions is True
        assert self.append_only_decisions is True
        assert self.require_idempotency_key is True
        assert self.require_observed_revision is True
        assert self.require_correction_reason is True
        assert self.require_correction_evidence is True
        assert self.allow_payment_execution is False

        assert len(self.correctable_header_fields) == len(set(self.correctable_header_fields))
        assert len(self.correctable_line_fields) == len(set(self.correctable_line_fields))
        assert not (set(self.correctable_header_fields) & set(self.correctable_line_fields))

        assert "EXECUTE_PAYMENT" not in {action.value for action in ReviewAction}


def default_interface_config() -> InterfaceConfig:
    """Fresh `InterfaceConfig` with the notebook's exact cell-100 values.

    Called explicitly by every service/API caller (never read as a hidden
    module-level global -- see module docstring).
    """

    return InterfaceConfig(
        interface_version="human-review-interface-v1",
        default_page_size=25,
        maximum_page_size=100,
        claim_timeout_seconds=1800,
        require_authentication=True,
        enforce_tenant_isolation=True,
        enforce_role_permissions=True,
        append_only_decisions=True,
        require_idempotency_key=True,
        require_observed_revision=True,
        require_correction_reason=True,
        require_correction_evidence=True,
        allow_payment_execution=False,
        correctable_header_fields=(
            InvoiceFieldName.SUPPLIER_NAME,
            InvoiceFieldName.SUPPLIER_ADDRESS,
            InvoiceFieldName.CUSTOMER_NAME,
            InvoiceFieldName.CUSTOMER_ADDRESS,
            InvoiceFieldName.INVOICE_NUMBER,
            InvoiceFieldName.INVOICE_DATE,
            InvoiceFieldName.DUE_DATE,
            InvoiceFieldName.PURCHASE_ORDER_NUMBER,
            InvoiceFieldName.CURRENCY,
            InvoiceFieldName.SUBTOTAL,
            InvoiceFieldName.TAX_AMOUNT,
            InvoiceFieldName.DISCOUNT_AMOUNT,
            InvoiceFieldName.SHIPPING_AMOUNT,
            InvoiceFieldName.TOTAL_AMOUNT,
            InvoiceFieldName.PAYMENT_TERMS,
        ),
        correctable_line_fields=(
            InvoiceFieldName.LINE_DESCRIPTION,
            InvoiceFieldName.LINE_QUANTITY,
            InvoiceFieldName.LINE_UNIT_PRICE,
            InvoiceFieldName.LINE_AMOUNT,
        ),
        role_permissions=(
            (
                InterfaceRole.AP_OPERATOR,
                (
                    ReviewAction.CLAIM,
                    ReviewAction.RELEASE,
                    ReviewAction.CORRECT,
                    ReviewAction.CONFIRM_SUPPLIER,
                    ReviewAction.CONFIRM_PURCHASE_ORDER,
                    ReviewAction.REQUEST_INFORMATION,
                    ReviewAction.ESCALATE,
                ),
            ),
            (InterfaceRole.AP_REVIEWER, tuple(ReviewAction)),
            (InterfaceRole.TENANT_ADMIN, tuple(ReviewAction)),
            (InterfaceRole.READ_ONLY_AUDITOR, tuple()),
        ),
    )


def interface_utc_now() -> datetime:
    return datetime.now(timezone.utc)


def interface_permissions_for_role(
    role: InterfaceRole,
    config: InterfaceConfig,
) -> tuple[ReviewAction, ...]:
    for configured_role, permissions in config.role_permissions:
        if configured_role == role:
            return permissions

    return tuple()


# ------------------------------------------------------------
# Authenticated actor (notebook cell 100, verbatim)
# ------------------------------------------------------------


@dataclass(frozen=True)
class InterfaceActor:
    actor_id: str
    tenant_id: UUID
    role: InterfaceRole
    authenticated_at: datetime


# ------------------------------------------------------------
# Dashboard and review-queue views (notebook cell 100, verbatim)
# ------------------------------------------------------------


@dataclass(frozen=True)
class DashboardRecord:
    tenant_id: UUID
    generated_at: datetime

    total_invoices: int
    processing_invoices: int
    completed_invoices: int
    review_required_invoices: int
    failed_invoices: int

    open_review_cases: int
    unassigned_review_cases: int

    review_reason_counts: tuple[tuple[str, int], ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class ReviewQueueRecord:
    tenant_id: UUID
    review_case_id: UUID
    workflow_id: UUID
    batch_id: UUID
    document_id: UUID

    source_name: str
    invoice_number: Optional[str]
    supplier_name: Optional[str]
    currency: Optional[str]
    total_amount: Optional[Decimal]

    workflow_status: InvoiceWorkflowStatus
    current_stage: OrchestrationStage
    case_status: ReviewCaseStatus
    priority: ReviewPriority

    review_reasons: tuple[str, ...]

    supplier_status: Optional[str]
    purchase_order_status: Optional[str]
    financial_validation_status: Optional[str]

    assigned_reviewer_id: Optional[str]
    revision: int

    created_at: datetime
    updated_at: datetime


# ------------------------------------------------------------
# Invoice-detail views (notebook cell 100, verbatim)
# ------------------------------------------------------------


@dataclass(frozen=True)
class InterfaceFieldValue:
    field_name: InvoiceFieldName
    raw_value: Optional[str]
    normalized_value: Optional[str]
    value_type: NormalizedValueType

    confidence: Optional[float]
    review_required: bool

    evidence_reference_ids: tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class InterfaceFinancialCheck:
    check_id: str
    check_type: str
    status: str
    message: str

    expected_value: Optional[str]
    observed_value: Optional[str]

    evidence_reference_ids: tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class InterfaceLineMatch:
    line_match_id: str
    invoice_line_number: Optional[int]
    purchase_order_line_number: Optional[int]

    description_status: str
    quantity_status: str
    unit_price_status: str
    line_total_status: str

    review_required: bool
    review_reasons: tuple[str, ...]


@dataclass(frozen=True)
class InterfaceTimelineEvent:
    event_id: str
    event_type: str
    stage: Optional[str]
    status: str
    actor_id: Optional[str]
    message: str
    occurred_at: datetime


@dataclass(frozen=True)
class InvoiceDetailRecord:
    tenant_id: UUID
    review_case_id: UUID
    workflow_id: UUID
    batch_id: UUID
    document_id: UUID

    source_name: str
    source_document_sha256: str
    source_artifact_uri: Optional[str]

    workflow_status: InvoiceWorkflowStatus
    current_stage: OrchestrationStage
    case_status: ReviewCaseStatus
    revision: int

    review_required: bool
    review_reasons: tuple[str, ...]

    fields: tuple[InterfaceFieldValue, ...]
    financial_checks: tuple[InterfaceFinancialCheck, ...]
    line_matches: tuple[InterfaceLineMatch, ...]
    timeline: tuple[InterfaceTimelineEvent, ...]
    review_decisions: tuple[HumanReviewDecision, ...]


# ------------------------------------------------------------
# Review commands and corrections (notebook cell 100, verbatim)
# ------------------------------------------------------------


@dataclass(frozen=True)
class ReviewFieldCorrection:
    field_name: InvoiceFieldName
    line_number: Optional[int]

    previous_value: Optional[str]
    corrected_value: str

    reason: str
    evidence_reference_ids: tuple[str, ...]


@dataclass(frozen=True)
class ReviewCommand:
    command_id: UUID
    idempotency_key: str

    tenant_id: UUID
    workflow_id: UUID
    batch_id: UUID
    document_id: UUID
    review_case_id: UUID

    actor: InterfaceActor
    action: ReviewAction
    disposition: Optional[HumanReviewDisposition]

    observed_review_revision: int
    observed_workflow_revision: int

    reason_codes: tuple[str, ...]
    notes: Optional[str]
    corrections: tuple[ReviewFieldCorrection, ...]

    requested_at: datetime


@dataclass(frozen=True)
class ReviewCommandResult:
    command_id: UUID
    idempotency_key: str

    status: InterfaceCommandStatus
    review_case_id: UUID
    document_id: UUID

    resulting_case_status: Optional[ReviewCaseStatus]
    resulting_revision: Optional[int]
    workflow_resumed: bool

    decision_id: Optional[UUID]
    message: str
    errors: tuple[str, ...] = field(default_factory=tuple)


# ------------------------------------------------------------
# Command-validation context (notebook cell 104, verbatim)
# ------------------------------------------------------------


@dataclass(frozen=True)
class ReviewCommandContext:
    tenant_id: UUID
    workflow_id: UUID
    batch_id: UUID
    document_id: UUID
    review_case_id: UUID

    case_status: ReviewCaseStatus
    assigned_reviewer_id: Optional[str]

    review_revision: int
    workflow_revision: int

    header_field_values: tuple[tuple[InvoiceFieldName, Optional[str]], ...]
    known_invoice_line_numbers: tuple[int, ...]
    available_evidence_reference_ids: tuple[str, ...]


# ------------------------------------------------------------
# Workflow-resume contracts (notebook cell 107, verbatim)
# ------------------------------------------------------------


@dataclass(frozen=True)
class WorkflowResumePlan:
    resume_plan_id: UUID
    tenant_id: UUID
    workflow_id: UUID
    batch_id: UUID
    document_id: UUID
    review_case_id: UUID
    decision_id: UUID

    disposition: HumanReviewDisposition
    restart_stage: OrchestrationStage

    source_normalization_sha256: str
    correction_overlay_json: str
    correction_overlay_sha256: str
    derived_version: str

    created_at: Any


@dataclass(frozen=True)
class WorkflowResumeExecution:
    command_result: ReviewCommandResult
    resume_plan: Optional[WorkflowResumePlan]
