"""M10 unit tests: `ap_agent.services.review_commands.validate_review_command`
/`review_command_fingerprint` (notebook cell 104's policy scenarios,
ported to a `ReviewCommandContext` built by hand instead of a live
PostgreSQL fixture)."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from uuid import NAMESPACE_URL, uuid4, uuid5

import pytest

from ap_agent.models.interface import (
    HumanReviewDisposition,
    InterfaceActor,
    InterfaceRole,
    ReviewAction,
    ReviewCaseStatus,
    ReviewCommand,
    ReviewCommandContext,
    ReviewFieldCorrection,
    default_interface_config,
)
from ap_agent.models.normalization import InvoiceFieldName
from ap_agent.services.review_commands import review_command_fingerprint, validate_review_command

pytestmark = pytest.mark.unit


TENANT_ID = uuid4()
WORKFLOW_ID = uuid4()
BATCH_ID = uuid4()
DOCUMENT_ID = uuid4()
REVIEW_CASE_ID = uuid4()
NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


@pytest.fixture()
def config():
    return default_interface_config()


@pytest.fixture()
def open_context() -> ReviewCommandContext:
    return ReviewCommandContext(
        tenant_id=TENANT_ID,
        workflow_id=WORKFLOW_ID,
        batch_id=BATCH_ID,
        document_id=DOCUMENT_ID,
        review_case_id=REVIEW_CASE_ID,
        case_status=ReviewCaseStatus.OPEN,
        assigned_reviewer_id=None,
        review_revision=1,
        workflow_revision=3,
        header_field_values=((InvoiceFieldName.PURCHASE_ORDER_NUMBER, "99"),),
        known_invoice_line_numbers=(1, 2),
        available_evidence_reference_ids=("ev-1", "ev-2"),
    )


def _actor(role: InterfaceRole, tenant_id=TENANT_ID, actor_id="operator-1") -> InterfaceActor:
    return InterfaceActor(actor_id=actor_id, tenant_id=tenant_id, role=role, authenticated_at=NOW - timedelta(seconds=1))


def _claim_command(context: ReviewCommandContext, actor: InterfaceActor, **overrides) -> ReviewCommand:
    defaults = dict(
        command_id=uuid5(NAMESPACE_URL, "test-claim"),
        idempotency_key="test-claim-key-1",
        tenant_id=context.tenant_id,
        workflow_id=context.workflow_id,
        batch_id=context.batch_id,
        document_id=context.document_id,
        review_case_id=context.review_case_id,
        actor=actor,
        action=ReviewAction.CLAIM,
        disposition=None,
        observed_review_revision=context.review_revision,
        observed_workflow_revision=context.workflow_revision,
        reason_codes=tuple(),
        notes=None,
        corrections=tuple(),
        requested_at=NOW,
    )
    defaults.update(overrides)
    return ReviewCommand(**defaults)


def test_valid_claim_passes(open_context, config):
    command = _claim_command(open_context, _actor(InterfaceRole.AP_OPERATOR))

    assert validate_review_command(command, open_context, config) == tuple()


def test_cross_tenant_command_rejected(open_context, config):
    command = _claim_command(open_context, _actor(InterfaceRole.AP_OPERATOR, tenant_id=uuid4()))

    assert "CROSS_TENANT_COMMAND" in validate_review_command(command, open_context, config)


def test_stale_review_revision_rejected(open_context, config):
    command = _claim_command(
        open_context, _actor(InterfaceRole.AP_OPERATOR), observed_review_revision=open_context.review_revision + 1
    )

    assert "STALE_REVIEW_REVISION" in validate_review_command(command, open_context, config)


def test_stale_workflow_revision_rejected(open_context, config):
    command = _claim_command(
        open_context, _actor(InterfaceRole.AP_OPERATOR), observed_workflow_revision=open_context.workflow_revision + 1
    )

    assert "STALE_WORKFLOW_REVISION" in validate_review_command(command, open_context, config)


def test_read_only_auditor_action_not_permitted(open_context, config):
    command = _claim_command(open_context, _actor(InterfaceRole.READ_ONLY_AUDITOR, actor_id="auditor-1"))

    assert "ACTION_NOT_PERMITTED" in validate_review_command(command, open_context, config)


def test_claim_on_already_claimed_case_rejected(open_context, config):
    claimed = replace(open_context, case_status=ReviewCaseStatus.IN_REVIEW, assigned_reviewer_id="someone-else")
    command = _claim_command(claimed, _actor(InterfaceRole.AP_OPERATOR))

    errors = validate_review_command(command, claimed, config)
    assert "CASE_NOT_OPEN" in errors
    assert "CASE_ALREADY_ASSIGNED" in errors


def test_correct_without_claim_rejected(open_context, config):
    correction = ReviewFieldCorrection(
        field_name=InvoiceFieldName.PURCHASE_ORDER_NUMBER,
        line_number=None,
        previous_value="99",
        corrected_value="99A",
        reason="Reviewer verified the PO.",
        evidence_reference_ids=("ev-1",),
    )
    command = _claim_command(
        open_context, _actor(InterfaceRole.AP_REVIEWER, actor_id="reviewer-1"), action=ReviewAction.CORRECT,
        disposition=HumanReviewDisposition.CORRECTED, reason_codes=("PO_CORRECTED",), notes="checked",
        corrections=(correction,),
    )

    assert "CASE_NOT_CLAIMED" in validate_review_command(command, open_context, config)


def test_correction_without_evidence_rejected(open_context, config):
    claimed = replace(open_context, case_status=ReviewCaseStatus.IN_REVIEW, assigned_reviewer_id="reviewer-1")
    correction = ReviewFieldCorrection(
        field_name=InvoiceFieldName.PURCHASE_ORDER_NUMBER,
        line_number=None,
        previous_value="99",
        corrected_value="99A",
        reason="Reviewer verified the PO.",
        evidence_reference_ids=(),
    )
    command = _claim_command(
        claimed, _actor(InterfaceRole.AP_REVIEWER, actor_id="reviewer-1"), action=ReviewAction.CORRECT,
        disposition=HumanReviewDisposition.CORRECTED, reason_codes=("PO_CORRECTED",), notes="checked",
        corrections=(correction,),
    )

    assert "CORRECTION_EVIDENCE_REQUIRED" in validate_review_command(command, claimed, config)


def test_correction_without_reason_rejected(open_context, config):
    claimed = replace(open_context, case_status=ReviewCaseStatus.IN_REVIEW, assigned_reviewer_id="reviewer-1")
    correction = ReviewFieldCorrection(
        field_name=InvoiceFieldName.PURCHASE_ORDER_NUMBER,
        line_number=None,
        previous_value="99",
        corrected_value="99A",
        reason="",
        evidence_reference_ids=("ev-1",),
    )
    command = _claim_command(
        claimed, _actor(InterfaceRole.AP_REVIEWER, actor_id="reviewer-1"), action=ReviewAction.CORRECT,
        disposition=HumanReviewDisposition.CORRECTED, reason_codes=("PO_CORRECTED",), notes="checked",
        corrections=(correction,),
    )

    assert "CORRECTION_REASON_REQUIRED" in validate_review_command(command, claimed, config)


def test_unknown_evidence_reference_rejected(open_context, config):
    claimed = replace(open_context, case_status=ReviewCaseStatus.IN_REVIEW, assigned_reviewer_id="reviewer-1")
    correction = ReviewFieldCorrection(
        field_name=InvoiceFieldName.PURCHASE_ORDER_NUMBER,
        line_number=None,
        previous_value="99",
        corrected_value="99A",
        reason="Reviewer verified.",
        evidence_reference_ids=("ev-does-not-exist",),
    )
    command = _claim_command(
        claimed, _actor(InterfaceRole.AP_REVIEWER, actor_id="reviewer-1"), action=ReviewAction.CORRECT,
        disposition=HumanReviewDisposition.CORRECTED, reason_codes=("PO_CORRECTED",), notes="checked",
        corrections=(correction,),
    )

    assert "UNKNOWN_EVIDENCE_REFERENCE" in validate_review_command(command, claimed, config)


def test_line_correction_requires_known_line_number(open_context, config):
    claimed = replace(open_context, case_status=ReviewCaseStatus.IN_REVIEW, assigned_reviewer_id="reviewer-1")
    correction = ReviewFieldCorrection(
        field_name=InvoiceFieldName.LINE_QUANTITY,
        line_number=99,
        previous_value=None,
        corrected_value="5",
        reason="Reviewer corrected quantity.",
        evidence_reference_ids=("ev-1",),
    )
    command = _claim_command(
        claimed, _actor(InterfaceRole.AP_REVIEWER, actor_id="reviewer-1"), action=ReviewAction.CORRECT,
        disposition=HumanReviewDisposition.CORRECTED, reason_codes=("QTY_CORRECTED",), notes="checked",
        corrections=(correction,),
    )

    assert "UNKNOWN_INVOICE_LINE_NUMBER" in validate_review_command(command, claimed, config)


def test_idempotency_fingerprint_is_deterministic_across_retries(open_context, config):
    command = _claim_command(open_context, _actor(InterfaceRole.AP_OPERATOR))
    retried = replace(command, requested_at=command.requested_at + timedelta(seconds=30))

    assert review_command_fingerprint(command) == review_command_fingerprint(retried)


def test_idempotency_fingerprint_changes_with_different_content(open_context, config):
    command = _claim_command(open_context, _actor(InterfaceRole.AP_OPERATOR))
    different = replace(command, notes="a different note", action=ReviewAction.RELEASE)

    assert review_command_fingerprint(command) != review_command_fingerprint(different)


def test_authenticated_after_request_rejected(open_context, config):
    command = _claim_command(
        open_context, _actor(InterfaceRole.AP_OPERATOR), requested_at=NOW - timedelta(hours=1)
    )

    assert "ACTOR_AUTHENTICATED_AFTER_REQUEST" in validate_review_command(command, open_context, config)


def test_invalid_idempotency_key_rejected(open_context, config):
    command = _claim_command(open_context, _actor(InterfaceRole.AP_OPERATOR), idempotency_key="short")

    assert "IDEMPOTENCY_KEY_INVALID" in validate_review_command(command, open_context, config)
