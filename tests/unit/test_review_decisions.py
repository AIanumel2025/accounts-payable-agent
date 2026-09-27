"""M10 unit test: `ap_agent.services.review_decisions.decision_evidence_payload`
(notebook cell 106, verbatim shape)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest

from ap_agent.models.interface import (
    HumanReviewDisposition,
    InterfaceActor,
    InterfaceRole,
    ReviewAction,
    ReviewCommand,
    ReviewFieldCorrection,
)
from ap_agent.models.normalization import InvoiceFieldName
from ap_agent.services.review_commands import review_command_fingerprint
from ap_agent.services.review_decisions import decision_evidence_payload

pytestmark = pytest.mark.unit

NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


def test_decision_evidence_payload_shape_and_determinism():
    actor = InterfaceActor(
        actor_id="reviewer-1", tenant_id=uuid4(), role=InterfaceRole.AP_REVIEWER, authenticated_at=NOW - timedelta(seconds=1)
    )
    correction = ReviewFieldCorrection(
        field_name=InvoiceFieldName.PURCHASE_ORDER_NUMBER,
        line_number=None,
        previous_value="99",
        corrected_value="99A",
        reason="Reviewer verified the PO.",
        evidence_reference_ids=("ev-1", "ev-2"),
    )
    command = ReviewCommand(
        command_id=uuid4(),
        idempotency_key="decision-command-key-1",
        tenant_id=actor.tenant_id,
        workflow_id=uuid4(),
        batch_id=uuid4(),
        document_id=uuid4(),
        review_case_id=uuid4(),
        actor=actor,
        action=ReviewAction.CORRECT,
        disposition=HumanReviewDisposition.CORRECTED,
        observed_review_revision=1,
        observed_workflow_revision=2,
        reason_codes=("PURCHASE_ORDER_CORRECTED",),
        notes="PO identifier reviewed.",
        corrections=(correction,),
        requested_at=NOW,
    )

    fingerprint = review_command_fingerprint(command)
    payload = decision_evidence_payload(command, fingerprint)

    assert payload["command_id"] == str(command.command_id)
    assert payload["idempotency_key"] == command.idempotency_key
    assert payload["command_fingerprint"] == fingerprint
    assert payload["reason_codes"] == ["PURCHASE_ORDER_CORRECTED"]
    assert payload["source"] == "PHASE_9_HUMAN_REVIEW_INTERFACE"
    assert len(payload["corrections"]) == 1
    assert payload["corrections"][0] == {
        "field_name": "PURCHASE_ORDER_NUMBER",
        "line_number": None,
        "previous_value": "99",
        "corrected_value": "99A",
        "reason": "Reviewer verified the PO.",
        "evidence_reference_ids": ["ev-1", "ev-2"],
    }

    # Rebuilding from the same command must be byte-identical (this is
    # what gets hashed and compared on an idempotent retry).
    assert decision_evidence_payload(command, fingerprint) == payload
