"""M8D unit tests: Phase 7 memory enums and domain contracts."""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

import pytest

from ap_agent.models.memory import (
    AuditMemoryEvent,
    HumanReviewDecision,
    HumanReviewDisposition,
    InvoiceMemoryBundle,
    MatchedInvoiceMemoryRecord,
    MemoryActorType,
    MemoryEventType,
    MemoryOperationResult,
    MemoryOperationStatus,
    MemoryWorkflowStage,
    MemoryWorkflowStatus,
    PhaseResultReference,
    WorkflowMemoryRecord,
)

pytestmark = pytest.mark.unit


def test_memory_workflow_stage_values():
    assert MemoryWorkflowStage.REFERENCE_MATCHING.value == "REFERENCE_MATCHING"
    assert MemoryWorkflowStage.MEMORY_PERSISTENCE.value == "MEMORY_PERSISTENCE"
    assert len(set(MemoryWorkflowStage)) == 10


def test_memory_workflow_status_values():
    assert {status.value for status in MemoryWorkflowStatus} == {
        "IN_PROGRESS",
        "SUCCEEDED",
        "REVIEW_REQUIRED",
        "FAILED",
        "COMPLETED",
    }


def test_memory_operation_status_values():
    assert {status.value for status in MemoryOperationStatus} == {
        "CREATED",
        "UPDATED",
        "IDEMPOTENT",
        "CONFLICT",
        "FAILED",
    }


def test_human_review_disposition_values():
    assert {d.value for d in HumanReviewDisposition} == {
        "APPROVED",
        "REJECTED",
        "HOLD",
        "NEEDS_INFORMATION",
        "CORRECTED",
    }


def test_workflow_memory_record_is_frozen():
    now = datetime.now(timezone.utc)
    record = WorkflowMemoryRecord(
        memory_id=uuid4(),
        tenant_id=str(uuid4()),
        batch_id=uuid4(),
        document_id=uuid4(),
        source_name="x.png",
        source_document_sha256="a" * 64,
        current_stage=MemoryWorkflowStage.REFERENCE_MATCHING,
        current_status=MemoryWorkflowStatus.SUCCEEDED,
        review_required=False,
        review_reasons=(),
        latest_matching_result_id=None,
        revision=1,
        created_at=now,
        updated_at=now,
    )

    with pytest.raises(Exception):
        record.revision = 2  # type: ignore[misc]


def test_matched_invoice_memory_record_optional_fields_default_missing_to_none():
    record = MatchedInvoiceMemoryRecord(
        record_id=uuid4(),
        tenant_id=uuid4(),
        workflow_memory_id=uuid4(),
        batch_id=uuid4(),
        document_id=uuid4(),
        matching_result_id=uuid4(),
        source_name="warped.jpg",
        source_document_sha256="b" * 64,
        invoice_record_id=uuid4(),
        invoice_number="308044",
        supplier_name="Snyder, Hammond and Anderson",
        currency=None,
        total_amount=None,
        supplier_resolution_status="MATCHED",
        matched_supplier_id="SUP-001",
        purchase_order_status="NOT_REFERENCED",
        purchase_order_id=None,
        purchase_order_number=None,
        goods_receipt_status="NOT_REFERENCED",
        goods_receipt_ids=(),
        match_mode="UNDETERMINED",
        line_match_count=0,
        normalization_status="REVIEW_REQUIRED",
        financial_validation_status="REVIEW_REQUIRED",
        matching_status="REVIEW_REQUIRED",
        review_required=True,
        review_reasons=("INHERITED_FINANCIAL_VALIDATION_REVIEW",),
        normalized_payload={},
        financial_payload={},
        matching_payload={},
        reference_payload={},
        payload_sha256="c" * 64,
    )

    assert record.currency is None
    assert record.total_amount is None
    assert record.stored_at is None


def test_invoice_memory_bundle_invoice_memory_defaults_to_none():
    now = datetime.now(timezone.utc)
    workflow = WorkflowMemoryRecord(
        memory_id=uuid4(),
        tenant_id=str(uuid4()),
        batch_id=uuid4(),
        document_id=uuid4(),
        source_name="x.png",
        source_document_sha256="a" * 64,
        current_stage=MemoryWorkflowStage.REFERENCE_MATCHING,
        current_status=MemoryWorkflowStatus.SUCCEEDED,
        review_required=False,
        review_reasons=(),
        latest_matching_result_id=None,
        revision=1,
        created_at=now,
        updated_at=now,
    )

    bundle = InvoiceMemoryBundle(
        memory_record=workflow,
        phase_references=(),
        audit_events=(),
        review_decisions=(),
    )

    assert bundle.invoice_memory is None
