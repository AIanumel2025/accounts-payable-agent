"""M10 unit tests: `ap_agent.services.workflow_resume` restart-stage
selection and resume-command validation (notebook cell 107's policy)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest

from ap_agent.models.interface import (
    HumanReviewDisposition,
    InterfaceActor,
    InterfaceRole,
    ReviewAction,
    ReviewCaseStatus,
    ReviewCommand,
    ReviewCommandContext,
    default_interface_config,
)
from ap_agent.models.normalization import InvoiceFieldName
from ap_agent.models.orchestration import OrchestrationStage
from ap_agent.services.workflow_resume import choose_review_restart_stage, validate_resume_command

pytestmark = pytest.mark.unit

NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


def test_approved_with_no_corrections_restarts_at_memory_persistence():
    stage = choose_review_restart_stage(HumanReviewDisposition.APPROVED, tuple())
    assert stage == OrchestrationStage.MEMORY_PERSISTENCE


def test_approved_with_corrections_is_invalid():
    with pytest.raises(ValueError):
        choose_review_restart_stage(HumanReviewDisposition.APPROVED, (InvoiceFieldName.SUPPLIER_NAME,))


def test_corrected_with_no_fields_is_invalid():
    with pytest.raises(ValueError):
        choose_review_restart_stage(HumanReviewDisposition.CORRECTED, tuple())


def test_financial_field_correction_restarts_at_financial_validation():
    stage = choose_review_restart_stage(HumanReviewDisposition.CORRECTED, (InvoiceFieldName.TOTAL_AMOUNT,))
    assert stage == OrchestrationStage.FINANCIAL_VALIDATION


def test_supplier_or_po_correction_restarts_at_reference_matching():
    stage = choose_review_restart_stage(HumanReviewDisposition.CORRECTED, (InvoiceFieldName.PURCHASE_ORDER_NUMBER,))
    assert stage == OrchestrationStage.REFERENCE_MATCHING


def test_metadata_only_correction_conservatively_restarts_at_financial_validation():
    stage = choose_review_restart_stage(HumanReviewDisposition.CORRECTED, (InvoiceFieldName.PAYMENT_TERMS,))
    assert stage == OrchestrationStage.FINANCIAL_VALIDATION


def test_rejected_disposition_cannot_resume():
    with pytest.raises(ValueError):
        choose_review_restart_stage(HumanReviewDisposition.REJECTED, tuple())


def _resolved_context(**overrides) -> ReviewCommandContext:
    defaults = dict(
        tenant_id=uuid4(),
        workflow_id=uuid4(),
        batch_id=uuid4(),
        document_id=uuid4(),
        review_case_id=uuid4(),
        case_status=ReviewCaseStatus.RESOLVED,
        assigned_reviewer_id="reviewer-1",
        review_revision=2,
        workflow_revision=4,
        header_field_values=tuple(),
        known_invoice_line_numbers=tuple(),
        available_evidence_reference_ids=tuple(),
    )
    defaults.update(overrides)
    return ReviewCommandContext(**defaults)


def _resume_command(context: ReviewCommandContext, **overrides) -> ReviewCommand:
    actor = InterfaceActor(
        actor_id=context.assigned_reviewer_id, tenant_id=context.tenant_id, role=InterfaceRole.AP_REVIEWER,
        authenticated_at=NOW - timedelta(seconds=1),
    )
    defaults = dict(
        command_id=uuid4(),
        idempotency_key="resume-command-key-1",
        tenant_id=context.tenant_id,
        workflow_id=context.workflow_id,
        batch_id=context.batch_id,
        document_id=context.document_id,
        review_case_id=context.review_case_id,
        actor=actor,
        action=ReviewAction.RESUME_WORKFLOW,
        disposition=HumanReviewDisposition.CORRECTED,
        observed_review_revision=context.review_revision,
        observed_workflow_revision=context.workflow_revision,
        reason_codes=("CORRECTION_READY_FOR_REVALIDATION",),
        notes="resume",
        corrections=tuple(),
        requested_at=NOW,
    )
    defaults.update(overrides)
    return ReviewCommand(**defaults)


def test_valid_resume_command_passes():
    context = _resolved_context()
    command = _resume_command(context)
    config = default_interface_config()

    decision_id = uuid4()
    errors = validate_resume_command(
        command, context, decision_id=decision_id, decision_disposition=HumanReviewDisposition.CORRECTED,
        decision_evidence={"decision_id": str(decision_id), "source": "PHASE_9_HUMAN_REVIEW_INTERFACE"}, config=config,
    )

    assert errors == tuple()


def test_resume_requires_resolved_case():
    context = _resolved_context(case_status=ReviewCaseStatus.IN_REVIEW)
    command = _resume_command(context)
    config = default_interface_config()
    decision_id = uuid4()

    errors = validate_resume_command(
        command, context, decision_id=decision_id, decision_disposition=HumanReviewDisposition.CORRECTED,
        decision_evidence={"decision_id": str(decision_id), "source": "PHASE_9_HUMAN_REVIEW_INTERFACE"}, config=config,
    )

    assert "RESOLVED_CASE_REQUIRED" in errors


def test_resume_with_corrections_rejected():
    from ap_agent.models.interface import ReviewFieldCorrection

    context = _resolved_context()
    correction = ReviewFieldCorrection(
        field_name=InvoiceFieldName.SUPPLIER_NAME, line_number=None, previous_value="a", corrected_value="b",
        reason="x", evidence_reference_ids=("ev-1",),
    )
    command = _resume_command(context, corrections=(correction,))
    config = default_interface_config()
    decision_id = uuid4()

    errors = validate_resume_command(
        command, context, decision_id=decision_id, decision_disposition=HumanReviewDisposition.CORRECTED,
        decision_evidence={"decision_id": str(decision_id), "source": "PHASE_9_HUMAN_REVIEW_INTERFACE"}, config=config,
    )

    assert "RESUME_CORRECTIONS_NOT_ALLOWED" in errors


def test_decision_identity_mismatch_detected():
    context = _resolved_context()
    command = _resume_command(context)
    config = default_interface_config()

    errors = validate_resume_command(
        command, context, decision_id=uuid4(), decision_disposition=HumanReviewDisposition.CORRECTED,
        decision_evidence={"decision_id": str(uuid4()), "source": "PHASE_9_HUMAN_REVIEW_INTERFACE"}, config=config,
    )

    assert "DECISION_IDENTITY_MISMATCH" in errors


def test_decision_source_must_be_the_review_interface():
    context = _resolved_context()
    command = _resume_command(context)
    config = default_interface_config()
    decision_id = uuid4()

    errors = validate_resume_command(
        command, context, decision_id=decision_id, decision_disposition=HumanReviewDisposition.CORRECTED,
        decision_evidence={"decision_id": str(decision_id), "source": "SOMEWHERE_ELSE"}, config=config,
    )

    assert "DECISION_SOURCE_INVALID" in errors


def test_resume_by_a_different_reviewer_rejected():
    context = _resolved_context(assigned_reviewer_id="reviewer-1")
    command = _resume_command(context)
    other_actor = InterfaceActor(
        actor_id="reviewer-2", tenant_id=context.tenant_id, role=InterfaceRole.AP_REVIEWER,
        authenticated_at=NOW - timedelta(seconds=1),
    )
    from dataclasses import replace

    command = replace(command, actor=other_actor)
    config = default_interface_config()
    decision_id = uuid4()

    errors = validate_resume_command(
        command, context, decision_id=decision_id, decision_disposition=HumanReviewDisposition.CORRECTED,
        decision_evidence={"decision_id": str(decision_id), "source": "PHASE_9_HUMAN_REVIEW_INTERFACE"}, config=config,
    )

    assert "CASE_ASSIGNED_TO_DIFFERENT_REVIEWER" in errors
