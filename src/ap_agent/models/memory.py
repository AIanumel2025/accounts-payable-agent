"""Phase 7 (PostgreSQL operational memory) domain contracts.

Source: notebook cell 83 ("PHASE 7 — CELL 1", "PostgreSQL operational-memory
contracts and configuration"), active-definition table extended for M8
(docs/modularisation_map.md). The enums and dataclasses below are ported
verbatim from that cell's names, field order and defaults.

`MemoryConfig` and the PostgreSQL DSN/schema constants are **not** extracted
here: per M8 task brief §4A they live in `ap_agent.config.postgres`,
matching where every other phase's `*Config` dataclass lives in
`ap_agent.config.settings` (CLAUDE.md: "Configuration is threaded
explicitly ... No module under `src/` may define a mutable module-level
config *instance*"). `memory_utc_now` and `postgres_dsn_is_configured` move
to `ap_agent.db.connection` alongside the rest of the connection-handling
code they belong with.

These are **domain contracts** — the shapes the memory service and its
callers reason about. They are deliberately kept distinct from the
PostgreSQL *row* models in `ap_agent.repositories.mapping` (`WorkflowRow`,
`PhaseReferenceRow`, `AuditEventRow`, `ReviewCaseRow`, `ReviewDecisionRow`,
`InvoiceMemoryRow`), whose field names mirror the actual migrated columns
(e.g. `workflow_id` / `current_phase` / `lock_version` instead of this
module's `memory_id` / `current_stage` / `revision`). M8 task brief §3.2
requires that the two layers not be silently renamed to imitate each
other; `ap_agent.repositories.mapping` holds the explicit, typed functions
that convert between them.

`MatchedInvoiceMemoryRecord` is new in M8: the notebook's Cell-1 contract
set (`InvoiceMemoryBundle` included) was authored before migration 0003 /
"PHASE 7 — REPLACEMENT CELL 5" introduced `ap_agent.invoice_memory_records`
and `build_invoice_memory_record`'s dict shape. This dataclass gives that
dict shape a typed, domain-facing name so `InvoiceMemoryBundle` can carry
it, and so the memory service and repository share one contract for it
instead of passing an untyped `dict[str, Any]` across the module boundary.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Any, Optional
from uuid import UUID

__all__ = [
    "MemoryWorkflowStage",
    "MemoryWorkflowStatus",
    "MemoryOperationStatus",
    "MemoryActorType",
    "MemoryEventType",
    "HumanReviewDisposition",
    "WorkflowMemoryRecord",
    "PhaseResultReference",
    "AuditMemoryEvent",
    "HumanReviewDecision",
    "MemoryOperationResult",
    "MatchedInvoiceMemoryRecord",
    "InvoiceMemoryBundle",
]


# ------------------------------------------------------------
# Memory enumerations (notebook cell 83, verbatim)
# ------------------------------------------------------------


class MemoryWorkflowStage(str, Enum):
    INGESTION = "INGESTION"
    PREPROCESSING = "PREPROCESSING"
    OCR = "OCR"
    NORMALIZATION = "NORMALIZATION"
    FINANCIAL_VALIDATION = "FINANCIAL_VALIDATION"
    REFERENCE_MATCHING = "REFERENCE_MATCHING"
    MEMORY_PERSISTENCE = "MEMORY_PERSISTENCE"
    REASONING = "REASONING"
    HUMAN_REVIEW = "HUMAN_REVIEW"
    COMPLETED = "COMPLETED"


class MemoryWorkflowStatus(str, Enum):
    IN_PROGRESS = "IN_PROGRESS"
    SUCCEEDED = "SUCCEEDED"
    REVIEW_REQUIRED = "REVIEW_REQUIRED"
    FAILED = "FAILED"
    COMPLETED = "COMPLETED"


class MemoryOperationStatus(str, Enum):
    CREATED = "CREATED"
    UPDATED = "UPDATED"
    IDEMPOTENT = "IDEMPOTENT"
    CONFLICT = "CONFLICT"
    FAILED = "FAILED"


class MemoryActorType(str, Enum):
    SYSTEM = "SYSTEM"
    HUMAN = "HUMAN"
    LLM = "LLM"


class MemoryEventType(str, Enum):
    MEMORY_CREATED = "MEMORY_CREATED"
    WORKFLOW_TRANSITIONED = "WORKFLOW_TRANSITIONED"
    PHASE_RESULT_LINKED = "PHASE_RESULT_LINKED"
    REVIEW_DECISION_RECORDED = "REVIEW_DECISION_RECORDED"
    ARTIFACT_VERIFIED = "ARTIFACT_VERIFIED"
    MEMORY_CONFLICT_REJECTED = "MEMORY_CONFLICT_REJECTED"


class HumanReviewDisposition(str, Enum):
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    HOLD = "HOLD"
    NEEDS_INFORMATION = "NEEDS_INFORMATION"
    CORRECTED = "CORRECTED"


# ------------------------------------------------------------
# Core workflow-memory record (notebook cell 83, verbatim)
# ------------------------------------------------------------


@dataclass(frozen=True)
class WorkflowMemoryRecord:
    memory_id: UUID
    tenant_id: str

    batch_id: UUID
    document_id: UUID

    source_name: str
    source_document_sha256: str

    current_stage: MemoryWorkflowStage
    current_status: MemoryWorkflowStatus

    review_required: bool
    review_reasons: tuple[str, ...]

    latest_matching_result_id: Optional[UUID]

    revision: int

    created_at: datetime
    updated_at: datetime


# ------------------------------------------------------------
# Phase-result reference (notebook cell 83, verbatim)
# ------------------------------------------------------------


@dataclass(frozen=True)
class PhaseResultReference:
    reference_id: UUID
    tenant_id: str

    batch_id: UUID
    document_id: UUID

    phase_name: str
    result_id: str
    result_status: str

    artifact_directory: str
    artifact_manifest_sha256: str
    result_payload_sha256: str

    recorded_at: datetime


# ------------------------------------------------------------
# Append-only audit event (notebook cell 83, verbatim)
# ------------------------------------------------------------


@dataclass(frozen=True)
class AuditMemoryEvent:
    audit_event_id: UUID
    tenant_id: str

    batch_id: UUID
    document_id: UUID

    event_type: MemoryEventType
    actor_type: MemoryActorType
    actor_id: str

    previous_stage: Optional[MemoryWorkflowStage]
    new_stage: MemoryWorkflowStage

    previous_status: Optional[MemoryWorkflowStatus]
    new_status: MemoryWorkflowStatus

    correlation_id: UUID

    payload_json: str
    payload_sha256: str

    occurred_at: datetime


# ------------------------------------------------------------
# Human-review decision (notebook cell 83, verbatim)
# ------------------------------------------------------------


@dataclass(frozen=True)
class HumanReviewDecision:
    decision_id: UUID
    tenant_id: str

    batch_id: UUID
    document_id: UUID

    reviewer_id: str
    disposition: HumanReviewDisposition

    reason_codes: tuple[str, ...]
    notes: Optional[str]

    corrections_json: str
    corrections_sha256: str

    decided_at: datetime


# ------------------------------------------------------------
# Memory-operation result (notebook cell 83, verbatim)
# ------------------------------------------------------------


@dataclass(frozen=True)
class MemoryOperationResult:
    operation_status: MemoryOperationStatus

    memory_record: Optional[WorkflowMemoryRecord]

    audit_event: Optional[AuditMemoryEvent]

    message: str
    errors: tuple[str, ...] = field(default_factory=tuple)


# ------------------------------------------------------------
# Matched-invoice memory record (new in M8; see module docstring)
# ------------------------------------------------------------


@dataclass(frozen=True)
class MatchedInvoiceMemoryRecord:
    """Domain-facing shape of one `ap_agent.invoice_memory_records` row.

    Field-for-field successor of the dict `build_invoice_memory_record`
    (notebook "PHASE 7 — REPLACEMENT CELL 5") built and persisted, with
    domain names instead of raw SQL-parameter names. See
    `ap_agent.repositories.mapping.invoice_memory_row_to_domain` /
    `domain_to_invoice_memory_row` for the row conversion.
    """

    record_id: UUID
    tenant_id: UUID
    workflow_memory_id: UUID
    batch_id: UUID
    document_id: UUID
    matching_result_id: UUID

    source_name: str
    source_document_sha256: str

    invoice_record_id: UUID
    invoice_number: Optional[str]
    supplier_name: Optional[str]
    currency: Optional[str]
    total_amount: Optional[Decimal]

    supplier_resolution_status: str
    matched_supplier_id: Optional[str]

    purchase_order_status: str
    purchase_order_id: Optional[str]
    purchase_order_number: Optional[str]

    goods_receipt_status: str
    goods_receipt_ids: tuple[str, ...]

    match_mode: str
    line_match_count: int

    normalization_status: str
    financial_validation_status: str
    matching_status: str

    review_required: bool
    review_reasons: tuple[str, ...]

    normalized_payload: dict[str, Any]
    financial_payload: dict[str, Any]
    matching_payload: dict[str, Any]
    reference_payload: dict[str, Any]

    payload_sha256: str

    stored_at: Optional[datetime] = None


# ------------------------------------------------------------
# Reconstructed invoice memory (notebook cell 83 shape, extended)
# ------------------------------------------------------------


@dataclass(frozen=True)
class InvoiceMemoryBundle:
    """Everything memory holds for one document.

    `invoice_memory` extends the notebook's Cell-83 shape (which only
    listed `memory_record`, `phase_references`, `audit_events` and
    `review_decisions`): Cell 83 predates the matched-invoice-memory table
    introduced by migration 0003, so it had nothing to carry that content
    in. See module docstring.
    """

    memory_record: WorkflowMemoryRecord

    phase_references: tuple[PhaseResultReference, ...]

    audit_events: tuple[AuditMemoryEvent, ...]

    review_decisions: tuple[HumanReviewDecision, ...]

    invoice_memory: Optional[MatchedInvoiceMemoryRecord] = None
