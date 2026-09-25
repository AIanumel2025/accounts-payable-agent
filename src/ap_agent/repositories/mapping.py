"""PostgreSQL row models and domain <-> row mapping for Phase 7 memory.

M8 task brief §3.2: "Separate domain contracts from database-row
representations ... Do not silently rename one layer to imitate the
other. Introduce explicit, typed and tested mapping functions between:
Domain models / PostgreSQL row models / Stored JSON payloads."

The row dataclasses below mirror the columns actually created by
`ap_agent/db/migrations/000{1,2,3}_*.sql` (notebook cells 85/86/88); the
domain dataclasses in `ap_agent.models.memory` mirror notebook cell 83
("PHASE 7 — CELL 1"), written *before* that schema existed. The two
vocabularies genuinely differ in places (not just spelling):

  - `WorkflowMemoryRecord.memory_id` / `.current_stage` / `.revision`
    <-> `WorkflowRow.workflow_id` / `.current_phase` / `.lock_version`
    (task's own example, §3.2) — straightforward rename.
  - `WorkflowMemoryRecord.review_reasons` and `.latest_matching_result_id`
    have no columns on `workflow_instances` at all (cell 83 anticipated
    them; the cell-86/88 schema resolves them from
    `invoice_memory_records`/`review_cases` instead). Mapping a bare row
    therefore leaves them empty/`None`; the repository fills them in from
    the joined tables it already queries.
  - `PhaseResultReference.artifact_directory`/`.artifact_manifest_sha256`/
    `.result_payload_sha256` <-> `PhaseReferenceRow.artifact_uri`/
    `.artifact_sha256` (one row column feeds both domain hash fields — the
    notebook itself only ever wrote one hash into `artifact_sha256`,
    `record["payload_sha256"]`); `.batch_id`/`.document_id` are not
    columns on `phase_result_references` (only `workflow_id` is) and must
    be supplied by the caller from the owning `WorkflowRow`.
  - `AuditMemoryEvent`/`HumanReviewDecision` model a full structured event
    (previous/new stage *and* status, a correlation id, a canonical
    payload hash) that the migrated `audit_events`/`review_decisions`
    tables do not have columns for beyond a single `phase_name`/
    `event_status` pair and a `payload`/`evidence` JSONB blob. Rather than
    silently dropping that information (or worse, forcing `phase_name`
    through `MemoryWorkflowStage` — the two enums use different
    vocabularies, e.g. row `phase_name='MEMORY'` is not a
    `MemoryWorkflowStage` member), this module writes the *complete*
    canonical domain object into the JSONB column
    (`memory_json_safe(event)`) alongside the flattened, queryable
    columns, and reads it back byte-for-byte on the way out. This is a
    deliberate, documented denormalization, not a silent behaviour
    change: the flattened columns remain indexable/queryable by SQL; the
    JSONB column is the lossless round-trip source of truth.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any, Optional
from uuid import UUID

from ap_agent.models.memory import (
    AuditMemoryEvent,
    HumanReviewDecision,
    HumanReviewDisposition,
    MatchedInvoiceMemoryRecord,
    MemoryActorType,
    MemoryEventType,
    MemoryWorkflowStage,
    MemoryWorkflowStatus,
    PhaseResultReference,
    WorkflowMemoryRecord,
)
from ap_agent.serialization.memory_json import memory_json_safe

__all__ = [
    "TenantRow",
    "WorkflowRow",
    "PhaseReferenceRow",
    "AuditEventRow",
    "ReviewCaseRow",
    "ReviewDecisionRow",
    "InvoiceMemoryRow",
    "domain_to_workflow_row",
    "workflow_row_to_domain",
    "domain_to_phase_reference_row",
    "phase_reference_row_to_domain",
    "domain_to_audit_event_row",
    "audit_event_row_to_domain",
    "domain_to_review_decision_row",
    "review_decision_row_to_domain",
    "domain_to_invoice_memory_row",
    "invoice_memory_row_to_domain",
]


# ------------------------------------------------------------
# Row models (one dataclass per migrated table)
# ------------------------------------------------------------


@dataclass(frozen=True)
class TenantRow:
    tenant_id: UUID
    tenant_key: str
    display_name: str
    status: str = "ACTIVE"
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


@dataclass(frozen=True)
class WorkflowRow:
    workflow_id: UUID
    tenant_id: UUID
    batch_id: UUID
    document_id: UUID
    source_document_sha256: str
    source_name: str
    current_phase: str
    current_status: str
    review_required: bool = False
    lock_version: int = 1
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None


@dataclass(frozen=True)
class PhaseReferenceRow:
    reference_id: UUID
    tenant_id: UUID
    workflow_id: UUID
    phase_name: str
    phase_version: str
    result_id: str
    result_status: str
    artifact_uri: str
    artifact_sha256: str
    attempt_number: int = 1
    review_required: bool = False
    produced_at: Optional[datetime] = None
    recorded_at: Optional[datetime] = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class AuditEventRow:
    event_id: UUID
    tenant_id: UUID
    workflow_id: UUID
    sequence_number: int
    event_type: str
    phase_name: str
    event_status: str
    actor_type: str
    message: str
    actor_id: Optional[str] = None
    payload: dict[str, Any] = field(default_factory=dict)
    occurred_at: Optional[datetime] = None
    recorded_at: Optional[datetime] = None


@dataclass(frozen=True)
class ReviewCaseRow:
    review_id: UUID
    tenant_id: UUID
    workflow_id: UUID
    reason_codes: tuple[str, ...]
    summary: str
    review_status: str = "OPEN"
    priority: int = 3
    assigned_to: Optional[str] = None
    opened_at: Optional[datetime] = None
    due_at: Optional[datetime] = None
    resolved_at: Optional[datetime] = None
    resolution_code: Optional[str] = None
    resolution_notes: Optional[str] = None


@dataclass(frozen=True)
class ReviewDecisionRow:
    decision_id: UUID
    tenant_id: UUID
    workflow_id: UUID
    review_id: UUID
    decision_type: str
    decided_by: str
    decided_at: datetime
    decision_notes: Optional[str] = None
    evidence: dict[str, Any] = field(default_factory=dict)
    recorded_at: Optional[datetime] = None


@dataclass(frozen=True)
class InvoiceMemoryRow:
    memory_record_id: UUID
    tenant_id: UUID
    workflow_id: UUID
    batch_id: UUID
    document_id: UUID
    matching_result_id: UUID
    source_name: str
    source_document_sha256: str
    invoice_record_id: UUID
    supplier_resolution_status: str
    purchase_order_status: str
    goods_receipt_status: str
    match_mode: str
    line_match_count: int
    normalization_status: str
    financial_validation_status: str
    matching_status: str
    review_required: bool
    payload_sha256: str
    invoice_number: Optional[str] = None
    supplier_name: Optional[str] = None
    currency: Optional[str] = None
    total_amount: Optional[Decimal] = None
    matched_supplier_id: Optional[str] = None
    purchase_order_id: Optional[str] = None
    purchase_order_number: Optional[str] = None
    goods_receipt_ids: tuple[str, ...] = ()
    review_reasons: tuple[str, ...] = ()
    normalized_invoice: dict[str, Any] = field(default_factory=dict)
    financial_validation: dict[str, Any] = field(default_factory=dict)
    matching_result: dict[str, Any] = field(default_factory=dict)
    matched_reference_data: dict[str, Any] = field(default_factory=dict)
    stored_at: Optional[datetime] = None


# ------------------------------------------------------------
# WorkflowMemoryRecord <-> WorkflowRow
# ------------------------------------------------------------


def domain_to_workflow_row(record: WorkflowMemoryRecord) -> WorkflowRow:
    return WorkflowRow(
        workflow_id=record.memory_id,
        tenant_id=UUID(str(record.tenant_id)),
        batch_id=record.batch_id,
        document_id=record.document_id,
        source_document_sha256=record.source_document_sha256,
        source_name=record.source_name,
        current_phase=record.current_stage.value,
        current_status=record.current_status.value,
        review_required=record.review_required,
        lock_version=record.revision,
        created_at=record.created_at,
        updated_at=record.updated_at,
    )


def workflow_row_to_domain(
    row: WorkflowRow,
    *,
    review_reasons: tuple[str, ...] = (),
    latest_matching_result_id: Optional[UUID] = None,
) -> WorkflowMemoryRecord:
    """`review_reasons`/`latest_matching_result_id` have no column on
    `workflow_instances` (see module docstring); the repository resolves
    them from `invoice_memory_records`/`review_cases` and passes them in.
    """

    return WorkflowMemoryRecord(
        memory_id=row.workflow_id,
        tenant_id=str(row.tenant_id),
        batch_id=row.batch_id,
        document_id=row.document_id,
        source_name=row.source_name,
        source_document_sha256=row.source_document_sha256,
        current_stage=MemoryWorkflowStage(row.current_phase),
        current_status=MemoryWorkflowStatus(row.current_status),
        review_required=row.review_required,
        review_reasons=tuple(review_reasons),
        latest_matching_result_id=latest_matching_result_id,
        revision=row.lock_version,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


# ------------------------------------------------------------
# PhaseResultReference <-> PhaseReferenceRow
# ------------------------------------------------------------


def domain_to_phase_reference_row(
    reference: PhaseResultReference,
    *,
    workflow_id: UUID,
    phase_version: str = "v1",
    attempt_number: int = 1,
) -> PhaseReferenceRow:
    return PhaseReferenceRow(
        reference_id=reference.reference_id,
        tenant_id=UUID(str(reference.tenant_id)),
        workflow_id=workflow_id,
        phase_name=reference.phase_name,
        phase_version=phase_version,
        attempt_number=attempt_number,
        result_id=reference.result_id,
        result_status=reference.result_status,
        artifact_uri=reference.artifact_directory,
        artifact_sha256=reference.artifact_manifest_sha256,
        review_required=False,
        produced_at=reference.recorded_at,
        recorded_at=reference.recorded_at,
        metadata={
            "document_id": str(reference.document_id),
            "batch_id": str(reference.batch_id),
        },
    )


def phase_reference_row_to_domain(
    row: PhaseReferenceRow,
    *,
    batch_id: UUID,
    document_id: UUID,
) -> PhaseResultReference:
    return PhaseResultReference(
        reference_id=row.reference_id,
        tenant_id=str(row.tenant_id),
        batch_id=batch_id,
        document_id=document_id,
        phase_name=row.phase_name,
        result_id=row.result_id,
        result_status=row.result_status,
        artifact_directory=row.artifact_uri,
        artifact_manifest_sha256=row.artifact_sha256,
        result_payload_sha256=row.artifact_sha256,
        recorded_at=row.recorded_at,
    )


# ------------------------------------------------------------
# AuditMemoryEvent <-> AuditEventRow
#
# The full domain object round-trips through `row.payload["memory_event"]`
# (see module docstring); the flattened columns stay queryable.
# ------------------------------------------------------------


def domain_to_audit_event_row(
    event: AuditMemoryEvent,
    *,
    workflow_id: UUID,
    sequence_number: int,
    message: Optional[str] = None,
) -> AuditEventRow:
    return AuditEventRow(
        event_id=event.audit_event_id,
        tenant_id=UUID(str(event.tenant_id)),
        workflow_id=workflow_id,
        sequence_number=sequence_number,
        event_type=event.event_type.value,
        phase_name=event.new_stage.value,
        event_status=event.new_status.value,
        actor_type=event.actor_type.value,
        actor_id=event.actor_id,
        message=message or event.event_type.value.replace("_", " ").title(),
        payload={"memory_event": memory_json_safe(event)},
        occurred_at=event.occurred_at,
        recorded_at=event.occurred_at,
    )


def audit_event_row_to_domain(row: AuditEventRow) -> AuditMemoryEvent:
    payload = row.payload.get("memory_event")

    if payload is None:
        raise ValueError(
            f"Audit event row {row.event_id} has no embedded "
            "'memory_event' payload to reconstruct from."
        )

    return AuditMemoryEvent(
        audit_event_id=UUID(payload["audit_event_id"]),
        tenant_id=payload["tenant_id"],
        batch_id=UUID(payload["batch_id"]),
        document_id=UUID(payload["document_id"]),
        event_type=MemoryEventType(payload["event_type"]),
        actor_type=MemoryActorType(payload["actor_type"]),
        actor_id=payload["actor_id"],
        previous_stage=(
            MemoryWorkflowStage(payload["previous_stage"])
            if payload["previous_stage"] is not None
            else None
        ),
        new_stage=MemoryWorkflowStage(payload["new_stage"]),
        previous_status=(
            MemoryWorkflowStatus(payload["previous_status"])
            if payload["previous_status"] is not None
            else None
        ),
        new_status=MemoryWorkflowStatus(payload["new_status"]),
        correlation_id=UUID(payload["correlation_id"]),
        payload_json=payload["payload_json"],
        payload_sha256=payload["payload_sha256"],
        occurred_at=datetime.fromisoformat(payload["occurred_at"]),
    )


# ------------------------------------------------------------
# HumanReviewDecision <-> ReviewDecisionRow
#
# Same pattern as AuditMemoryEvent: full object in `row.evidence["memory_decision"]`.
# ------------------------------------------------------------


def domain_to_review_decision_row(
    decision: HumanReviewDecision,
    *,
    workflow_id: UUID,
    review_id: UUID,
) -> ReviewDecisionRow:
    return ReviewDecisionRow(
        decision_id=decision.decision_id,
        tenant_id=UUID(str(decision.tenant_id)),
        workflow_id=workflow_id,
        review_id=review_id,
        decision_type=decision.disposition.value,
        decided_by=decision.reviewer_id,
        decision_notes=decision.notes,
        evidence={"memory_decision": memory_json_safe(decision)},
        decided_at=decision.decided_at,
        recorded_at=decision.decided_at,
    )


def review_decision_row_to_domain(row: ReviewDecisionRow) -> HumanReviewDecision:
    payload = row.evidence.get("memory_decision")

    if payload is None:
        raise ValueError(
            f"Review decision row {row.decision_id} has no embedded "
            "'memory_decision' payload to reconstruct from."
        )

    return HumanReviewDecision(
        decision_id=UUID(payload["decision_id"]),
        tenant_id=payload["tenant_id"],
        batch_id=UUID(payload["batch_id"]),
        document_id=UUID(payload["document_id"]),
        reviewer_id=payload["reviewer_id"],
        disposition=HumanReviewDisposition(payload["disposition"]),
        reason_codes=tuple(payload["reason_codes"]),
        notes=payload["notes"],
        corrections_json=payload["corrections_json"],
        corrections_sha256=payload["corrections_sha256"],
        decided_at=datetime.fromisoformat(payload["decided_at"]),
    )


# ------------------------------------------------------------
# MatchedInvoiceMemoryRecord <-> InvoiceMemoryRow
#
# The closest to a 1:1 mapping: `MatchedInvoiceMemoryRecord` (new in M8,
# see ap_agent.models.memory) was designed directly from this table.
# ------------------------------------------------------------


def domain_to_invoice_memory_row(record: MatchedInvoiceMemoryRecord) -> InvoiceMemoryRow:
    return InvoiceMemoryRow(
        memory_record_id=record.record_id,
        tenant_id=record.tenant_id,
        workflow_id=record.workflow_memory_id,
        batch_id=record.batch_id,
        document_id=record.document_id,
        matching_result_id=record.matching_result_id,
        source_name=record.source_name,
        source_document_sha256=record.source_document_sha256,
        invoice_record_id=record.invoice_record_id,
        invoice_number=record.invoice_number,
        supplier_name=record.supplier_name,
        currency=record.currency,
        total_amount=record.total_amount,
        supplier_resolution_status=record.supplier_resolution_status,
        matched_supplier_id=record.matched_supplier_id,
        purchase_order_status=record.purchase_order_status,
        purchase_order_id=record.purchase_order_id,
        purchase_order_number=record.purchase_order_number,
        goods_receipt_status=record.goods_receipt_status,
        goods_receipt_ids=tuple(record.goods_receipt_ids),
        match_mode=record.match_mode,
        line_match_count=record.line_match_count,
        normalization_status=record.normalization_status,
        financial_validation_status=record.financial_validation_status,
        matching_status=record.matching_status,
        review_required=record.review_required,
        review_reasons=tuple(record.review_reasons),
        normalized_invoice=record.normalized_payload,
        financial_validation=record.financial_payload,
        matching_result=record.matching_payload,
        matched_reference_data=record.reference_payload,
        payload_sha256=record.payload_sha256,
        stored_at=record.stored_at,
    )


def invoice_memory_row_to_domain(row: InvoiceMemoryRow) -> MatchedInvoiceMemoryRecord:
    return MatchedInvoiceMemoryRecord(
        record_id=row.memory_record_id,
        tenant_id=row.tenant_id,
        workflow_memory_id=row.workflow_id,
        batch_id=row.batch_id,
        document_id=row.document_id,
        matching_result_id=row.matching_result_id,
        source_name=row.source_name,
        source_document_sha256=row.source_document_sha256,
        invoice_record_id=row.invoice_record_id,
        invoice_number=row.invoice_number,
        supplier_name=row.supplier_name,
        currency=row.currency,
        total_amount=row.total_amount,
        supplier_resolution_status=row.supplier_resolution_status,
        matched_supplier_id=row.matched_supplier_id,
        purchase_order_status=row.purchase_order_status,
        purchase_order_id=row.purchase_order_id,
        purchase_order_number=row.purchase_order_number,
        goods_receipt_status=row.goods_receipt_status,
        goods_receipt_ids=tuple(row.goods_receipt_ids),
        match_mode=row.match_mode,
        line_match_count=row.line_match_count,
        normalization_status=row.normalization_status,
        financial_validation_status=row.financial_validation_status,
        matching_status=row.matching_status,
        review_required=row.review_required,
        review_reasons=tuple(row.review_reasons),
        normalized_payload=row.normalized_invoice,
        financial_payload=row.financial_validation,
        matching_payload=row.matching_result,
        reference_payload=row.matched_reference_data,
        payload_sha256=row.payload_sha256,
        stored_at=row.stored_at,
    )
