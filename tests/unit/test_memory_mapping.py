"""M8D unit tests: domain <-> PostgreSQL-row mapping (task §3.2/§7)."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from uuid import uuid4

import pytest

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
from ap_agent.repositories.mapping import (
    audit_event_row_to_domain,
    domain_to_audit_event_row,
    domain_to_invoice_memory_row,
    domain_to_phase_reference_row,
    domain_to_review_decision_row,
    domain_to_workflow_row,
    invoice_memory_row_to_domain,
    phase_reference_row_to_domain,
    review_decision_row_to_domain,
    workflow_row_to_domain,
)

pytestmark = pytest.mark.unit


NOW = datetime.now(timezone.utc)


def _workflow_record(**overrides) -> WorkflowMemoryRecord:
    defaults = dict(
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
        created_at=NOW,
        updated_at=NOW,
    )
    defaults.update(overrides)
    return WorkflowMemoryRecord(**defaults)


def test_workflow_row_to_domain_renames_fields_per_task_example():
    """Task §3.2's own example: memory_id/current_stage/revision <->
    workflow_id/current_phase/lock_version."""

    record = _workflow_record()
    row = domain_to_workflow_row(record)

    assert row.workflow_id == record.memory_id
    assert row.current_phase == record.current_stage.value
    assert row.lock_version == record.revision

    back = workflow_row_to_domain(row)

    assert back.memory_id == record.memory_id
    assert back.current_stage == record.current_stage
    assert back.current_status == record.current_status
    assert back.revision == record.revision
    assert back.tenant_id == record.tenant_id


def test_workflow_row_to_domain_defaults_missing_columns():
    """review_reasons/latest_matching_result_id have no column on
    workflow_instances; a bare row maps to empty/None unless supplied."""

    record = _workflow_record(review_reasons=("SOME_REASON",))
    row = domain_to_workflow_row(record)

    back = workflow_row_to_domain(row)

    assert back.review_reasons == ()
    assert back.latest_matching_result_id is None

    back_with_context = workflow_row_to_domain(
        row, review_reasons=("SOME_REASON",), latest_matching_result_id=uuid4()
    )

    assert back_with_context.review_reasons == ("SOME_REASON",)


def test_phase_reference_row_roundtrip_requires_batch_and_document_context():
    reference = PhaseResultReference(
        reference_id=uuid4(),
        tenant_id=str(uuid4()),
        batch_id=uuid4(),
        document_id=uuid4(),
        phase_name="REFERENCE_MATCHING",
        result_id=str(uuid4()),
        result_status="SUCCEEDED",
        artifact_directory="postgresql://ap_agent/invoice_memory_records/x",
        artifact_manifest_sha256="b" * 64,
        result_payload_sha256="b" * 64,
        recorded_at=NOW,
    )

    row = domain_to_phase_reference_row(reference, workflow_id=uuid4())

    assert row.artifact_uri == reference.artifact_directory
    assert row.artifact_sha256 == reference.artifact_manifest_sha256

    back = phase_reference_row_to_domain(
        row, batch_id=reference.batch_id, document_id=reference.document_id
    )

    assert back.reference_id == reference.reference_id
    assert back.artifact_directory == reference.artifact_directory
    assert back.result_payload_sha256 == reference.artifact_manifest_sha256
    assert back.batch_id == reference.batch_id
    assert back.document_id == reference.document_id


def test_audit_event_row_roundtrip_is_lossless():
    event = AuditMemoryEvent(
        audit_event_id=uuid4(),
        tenant_id=str(uuid4()),
        batch_id=uuid4(),
        document_id=uuid4(),
        event_type=MemoryEventType.MEMORY_CREATED,
        actor_type=MemoryActorType.SYSTEM,
        actor_id="svc",
        previous_stage=MemoryWorkflowStage.REFERENCE_MATCHING,
        new_stage=MemoryWorkflowStage.MEMORY_PERSISTENCE,
        previous_status=MemoryWorkflowStatus.IN_PROGRESS,
        new_status=MemoryWorkflowStatus.SUCCEEDED,
        correlation_id=uuid4(),
        payload_json="{}",
        payload_sha256="c" * 64,
        occurred_at=NOW,
    )

    row = domain_to_audit_event_row(event, workflow_id=uuid4(), sequence_number=1)

    assert row.phase_name == event.new_stage.value
    assert row.event_status == event.new_status.value

    back = audit_event_row_to_domain(row)

    assert back == event


def test_audit_event_row_to_domain_requires_embedded_payload():
    from ap_agent.repositories.mapping import AuditEventRow

    row = AuditEventRow(
        event_id=uuid4(),
        tenant_id=uuid4(),
        workflow_id=uuid4(),
        sequence_number=1,
        event_type="MEMORY_CREATED",
        phase_name="MEMORY_PERSISTENCE",
        event_status="SUCCEEDED",
        actor_type="SYSTEM",
        message="test",
        payload={},
    )

    with pytest.raises(ValueError):
        audit_event_row_to_domain(row)


def test_review_decision_row_roundtrip_is_lossless():
    decision = HumanReviewDecision(
        decision_id=uuid4(),
        tenant_id=str(uuid4()),
        batch_id=uuid4(),
        document_id=uuid4(),
        reviewer_id="alice",
        disposition=HumanReviewDisposition.APPROVED,
        reason_codes=("OK",),
        notes="looks fine",
        corrections_json="{}",
        corrections_sha256="d" * 64,
        decided_at=NOW,
    )

    row = domain_to_review_decision_row(decision, workflow_id=uuid4(), review_id=uuid4())
    back = review_decision_row_to_domain(row)

    assert back == decision


def test_invoice_memory_row_roundtrip_preserves_decimal_and_missing_total():
    record = MatchedInvoiceMemoryRecord(
        record_id=uuid4(),
        tenant_id=uuid4(),
        workflow_memory_id=uuid4(),
        batch_id=uuid4(),
        document_id=uuid4(),
        matching_result_id=uuid4(),
        source_name="08181_warped_document_perspective_shadow.jpg",
        source_document_sha256="e" * 64,
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
        normalized_payload={"a": 1},
        financial_payload={"b": 2},
        matching_payload={"c": 3},
        reference_payload={"d": 4},
        payload_sha256="f" * 64,
    )

    row = domain_to_invoice_memory_row(record)

    assert row.total_amount is None
    assert row.currency is None

    back = invoice_memory_row_to_domain(row)

    assert back == record


def test_invoice_memory_row_roundtrip_preserves_decimal_total():
    record = MatchedInvoiceMemoryRecord(
        record_id=uuid4(),
        tenant_id=uuid4(),
        workflow_memory_id=uuid4(),
        batch_id=uuid4(),
        document_id=uuid4(),
        matching_result_id=uuid4(),
        source_name="08181_flat_document.png",
        source_document_sha256="1" * 64,
        invoice_record_id=uuid4(),
        invoice_number="308044",
        supplier_name="Snyder, Hammond and Anderson",
        currency="USD",
        total_amount=Decimal("69.22"),
        supplier_resolution_status="MATCHED",
        matched_supplier_id="SUP-001",
        purchase_order_status="NOT_REFERENCED",
        purchase_order_id=None,
        purchase_order_number=None,
        goods_receipt_status="NOT_REFERENCED",
        goods_receipt_ids=(),
        match_mode="UNDETERMINED",
        line_match_count=0,
        normalization_status="SUCCEEDED",
        financial_validation_status="SUCCEEDED",
        matching_status="SUCCEEDED",
        review_required=False,
        review_reasons=(),
        normalized_payload={},
        financial_payload={},
        matching_payload={},
        reference_payload={},
        payload_sha256="2" * 64,
    )

    row = domain_to_invoice_memory_row(record)
    back = invoice_memory_row_to_domain(row)

    assert back.total_amount == Decimal("69.22")
    assert isinstance(back.total_amount, Decimal)
    assert back == record
