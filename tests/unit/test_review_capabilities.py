"""M11C unit tests: `ap_agent.services.review_capabilities` (pure, no database)."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import BaseModel

from ap_agent.api.errors import register_exception_handlers
from ap_agent.models.interface import (
    HumanReviewDisposition,
    InterfaceActor,
    InterfaceRole,
    InvoiceDetailRecord,
    ReviewAction,
    ReviewCaseStatus,
    ReviewCommandContext,
    default_interface_config,
)
from ap_agent.models.interface import InterfaceTimelineEvent
from ap_agent.models.normalization import InvoiceFieldName
from ap_agent.models.orchestration import InvoiceWorkflowStatus, OrchestrationStage
from ap_agent.services.review_capabilities import (
    SUPPORTED_TRANSACTIONAL_ACTIONS,
    UNSUPPORTED_ACTIONS,
    build_command_capabilities,
    extract_line_values,
)

pytestmark = pytest.mark.unit

TENANT = uuid.uuid4()
NOW = datetime.now(timezone.utc)


def _actor(role=InterfaceRole.AP_REVIEWER, actor_id="rev-1"):
    return InterfaceActor(actor_id=actor_id, tenant_id=TENANT, role=role, authenticated_at=NOW)


def _context(status=ReviewCaseStatus.OPEN, assigned=None):
    return ReviewCommandContext(
        tenant_id=TENANT, workflow_id=uuid.uuid4(), batch_id=uuid.uuid4(), document_id=uuid.uuid4(),
        review_case_id=uuid.uuid4(), case_status=status, assigned_reviewer_id=assigned,
        review_revision=1, workflow_revision=1,
        header_field_values=(
            (InvoiceFieldName.TOTAL_AMOUNT, "100.10"),
            (InvoiceFieldName.INVOICE_NUMBER, None),
        ),
        known_invoice_line_numbers=(1, 2), available_evidence_reference_ids=("ev-1", "ev-2"),
    )


def _detail(decisions=(), events=()):
    return InvoiceDetailRecord(
        tenant_id=TENANT, review_case_id=uuid.uuid4(), workflow_id=uuid.uuid4(), batch_id=uuid.uuid4(),
        document_id=uuid.uuid4(), source_name="x.pdf", source_document_sha256="a" * 64, source_artifact_uri=None,
        workflow_status=InvoiceWorkflowStatus.REVIEW_REQUIRED, current_stage=OrchestrationStage.HUMAN_REVIEW,
        case_status=ReviewCaseStatus.OPEN, revision=1, review_required=True, review_reasons=(),
        fields=(), financial_checks=(), line_matches=(), timeline=tuple(events), review_decisions=tuple(decisions),
    )


class _Decision:
    def __init__(self, disposition, reviewer="rev-1"):
        self.decision_id = uuid.uuid4()
        self.disposition = disposition
        self.reviewer_id = reviewer


def _caps(*, actor=None, context=None, detail=None, payload=None, writes=True):
    return build_command_capabilities(
        actor=actor or _actor(), context=context or _context(), detail=detail or _detail(),
        normalized_payload=payload, config=default_interface_config(), writes_enabled=writes,
    )


def test_supported_and_unsupported_partition_every_action_except_payment():
    assert set(SUPPORTED_TRANSACTIONAL_ACTIONS) | set(UNSUPPORTED_ACTIONS) == set(ReviewAction)
    assert not set(SUPPORTED_TRANSACTIONAL_ACTIONS) & set(UNSUPPORTED_ACTIONS)
    assert not any("PAY" in action.value for action in ReviewAction)


def test_open_case_only_offers_claim():
    caps = _caps()
    assert [item["action"] for item in caps["available_actions"]] == [ReviewAction.CLAIM]
    assert caps["assignment"] == "UNASSIGNED"


def test_owned_case_offers_release_and_decisions_not_claim():
    caps = _caps(context=_context(ReviewCaseStatus.IN_REVIEW, "rev-1"))
    assert [item["action"] for item in caps["available_actions"]] == [
        ReviewAction.RELEASE, ReviewAction.ACCEPT, ReviewAction.CORRECT, ReviewAction.REJECT
    ]
    assert caps["assignment"] == "ASSIGNED_TO_ACTOR"


def test_case_owned_by_someone_else_offers_nothing():
    caps = _caps(context=_context(ReviewCaseStatus.IN_REVIEW, "someone-else"))
    assert caps["available_actions"] == () and caps["assignment"] == "ASSIGNED_TO_OTHER"


def test_operator_is_limited_to_backend_policy():
    caps = _caps(actor=_actor(InterfaceRole.AP_OPERATOR), context=_context(ReviewCaseStatus.IN_REVIEW, "rev-1"))
    assert [item["action"] for item in caps["available_actions"]] == [ReviewAction.RELEASE, ReviewAction.CORRECT]
    assert ReviewAction.ACCEPT not in caps["permitted_actions"]


def test_auditor_has_no_actions():
    caps = _caps(actor=_actor(InterfaceRole.READ_ONLY_AUDITOR))
    assert caps["permitted_actions"] == () and caps["available_actions"] == ()


@pytest.mark.parametrize(
    "disposition,writes,events,expected",
    [
        (HumanReviewDisposition.APPROVED, True, (), None),
        (HumanReviewDisposition.CORRECTED, True, (), None),
        (HumanReviewDisposition.REJECTED, True, (), "RESOLVED_CASE_REQUIRED"),
        (HumanReviewDisposition.APPROVED, False, (), "COMMAND_MODE_VALIDATION_ONLY"),
    ],
)
def test_resume_eligibility(disposition, writes, events, expected):
    status = ReviewCaseStatus.REJECTED if disposition == HumanReviewDisposition.REJECTED else ReviewCaseStatus.RESOLVED
    caps = _caps(context=_context(status, "rev-1"), detail=_detail([_Decision(disposition)], events), writes=writes)
    assert caps["resume"]["ineligible_reason"] == expected
    assert caps["resume"]["eligible"] is (expected is None)


def test_resume_requires_the_deciding_reviewer_and_is_one_shot():
    context = _context(ReviewCaseStatus.RESOLVED, "rev-1")
    decision = _Decision(HumanReviewDisposition.APPROVED, reviewer="rev-1")

    other = _caps(actor=_actor(actor_id="rev-2"), context=context, detail=_detail([decision]))
    assert other["resume"]["ineligible_reason"] == "DECISION_ACTOR_MISMATCH"

    requested = InterfaceTimelineEvent(
        event_id="e", event_type="WORKFLOW_RESUME_REQUESTED", stage=None, status="IN_PROGRESS",
        actor_id="rev-1", message="m", occurred_at=NOW,
    )
    done = _caps(context=context, detail=_detail([decision], [requested]))
    assert done["resume"]["already_requested"] is True
    assert done["resume"]["ineligible_reason"] == "RESUME_ALREADY_REQUESTED"


def test_correction_policy_lists_every_configured_header_field_with_exact_current_values():
    # M11E.4: absent fields are offered too (current_value null) -- but only those explicitly configured.
    caps = _caps()
    headers = {item["field_name"]: item["current_value"] for item in caps["correction_policy"]["header_fields"]}
    configured = default_interface_config().correctable_header_fields
    assert set(headers) == set(configured) and [item["field_name"] for item in caps["correction_policy"]["header_fields"]] == list(configured)
    assert headers[InvoiceFieldName.TOTAL_AMOUNT] == "100.10"  # exact string, not coerced
    assert headers[InvoiceFieldName.INVOICE_NUMBER] is None
    assert headers[InvoiceFieldName.SUPPLIER_NAME] is None  # never extracted: insertable
    assert caps["correction_policy"]["line_numbers"] == (1, 2)


def test_line_values_handle_scalar_nested_and_missing_without_coercion():
    payload = {"invoice_record": {"line_items": [
        {"line_number": 2, "description": "B", "quantity": {"normalized_value": "2.50"}, "unit_price": None},
        {"line_number": 1, "description": {"normalized_value": "A"}, "quantity": "5", "amount": "0"},
        {"description": "no number"},
    ]}}
    values = extract_line_values(payload, default_interface_config().correctable_line_fields)
    by_key = {(number, field.value): value for number, field, value in values}
    assert by_key[(1, "LINE_DESCRIPTION")] == "A"
    assert by_key[(1, "LINE_AMOUNT")] == "0"          # a real zero stays "0"
    assert by_key[(2, "LINE_QUANTITY")] == "2.50"     # exact decimal string preserved
    assert by_key[(2, "LINE_UNIT_PRICE")] is None     # missing is never coerced to zero
    assert {number for number, _, _ in values} == {1, 2}
    assert extract_line_values("garbage", ()) == ()


class _Body(BaseModel):
    action: str
    n: int


def test_request_validation_errors_are_enveloped_and_input_free():
    app = FastAPI()
    register_exception_handlers(app)

    @app.post("/x")
    def _x(body: _Body):
        return {}

    client = TestClient(app)
    response = client.post("/x", json={"action": 5, "n": "super-secret-value"})
    assert response.status_code == 422
    body = response.json()
    assert set(body) == {"request_id", "errors", "generated_at"}
    assert "super-secret-value" not in response.text
    assert "REQUEST_VALIDATION_FAILED" in body["errors"]
    assert "REVIEW_ACTION_NOT_SUPPORTED" in body["errors"]
    assert "INVALID_FIELD:n" in body["errors"]
