"""Phase 9 append-only review-decision transactional executor.

Source: notebook cell 106 ("PHASE 9 — CELL 7", "Append-only review
decisions and terminal case resolution"): `decision_evidence_payload` is
ported verbatim; `execute_decision_command` composes
`ap_agent.repositories.review_repository.ReviewRepository`'s primitives
with `ap_agent.services.review_commands.validate_review_command` the same
way the notebook's `execute_decision_command_in_transaction` composes its
own inline helpers. Terminal decisions (ACCEPT/CORRECT/REJECT) resolve the
review case and, for CORRECT, carry the evidence-backed correction into
`ap_agent.review_decisions` -- an append-only table (migration 0002's
`review_decisions_no_mutation`/`review_decisions_no_truncate` triggers);
this module never issues an UPDATE/DELETE against it.
"""

from __future__ import annotations

from typing import Any
from uuid import uuid5

from ap_agent.models.interface import (
    HumanReviewDisposition,
    InterfaceCommandStatus,
    InterfaceConfig,
    ReviewAction,
    ReviewCaseStatus,
    ReviewCommand,
    ReviewCommandResult,
)
from ap_agent.repositories.review_repository import ReviewRepository
from ap_agent.services.review_commands import (
    idempotent_assignment_result,
    rejected_assignment_result,
    review_command_fingerprint,
    validate_review_command,
)

__all__ = ["TERMINAL_DECISION_ACTIONS", "decision_evidence_payload", "execute_decision_command"]


TERMINAL_DECISION_ACTIONS = {ReviewAction.ACCEPT, ReviewAction.CORRECT, ReviewAction.REJECT}


def decision_evidence_payload(command: ReviewCommand, command_fingerprint: str) -> dict[str, Any]:
    corrections = [
        {
            "field_name": correction.field_name.value,
            "line_number": correction.line_number,
            "previous_value": correction.previous_value,
            "corrected_value": correction.corrected_value,
            "reason": correction.reason,
            "evidence_reference_ids": list(correction.evidence_reference_ids),
        }
        for correction in command.corrections
    ]

    return {
        "command_id": str(command.command_id),
        "idempotency_key": command.idempotency_key,
        "command_fingerprint": command_fingerprint,
        "reason_codes": list(command.reason_codes),
        "corrections": corrections,
        "source": "PHASE_9_HUMAN_REVIEW_INTERFACE",
    }


def execute_decision_command(
    repository: ReviewRepository,
    command: ReviewCommand,
    config: InterfaceConfig,
) -> ReviewCommandResult:
    if command.tenant_id != command.actor.tenant_id:
        return rejected_assignment_result(
            command, status=InterfaceCommandStatus.REJECTED, message="Cross-tenant command rejected.",
            errors=("CROSS_TENANT_COMMAND",),
        )

    with repository.transaction(command.tenant_id) as cursor:
        context = repository.lock_decision_context(
            cursor, tenant_id=command.tenant_id, workflow_id=command.workflow_id,
            batch_id=command.batch_id, document_id=command.document_id, review_case_id=command.review_case_id,
        )

        if context is None:
            return rejected_assignment_result(
                command, status=InterfaceCommandStatus.REJECTED, message="Review decision target was not found.",
                errors=("COMMAND_TARGET_NOT_FOUND",),
            )

        command_fingerprint = review_command_fingerprint(command)

        stored_command_payload = repository.find_stored_command_payload(
            cursor, tenant_id=command.tenant_id, workflow_id=command.workflow_id,
            idempotency_key=command.idempotency_key, event_type="INTERFACE_COMMAND_EXECUTED",
        )

        if stored_command_payload is not None:
            if stored_command_payload.get("command_fingerprint") != command_fingerprint:
                return rejected_assignment_result(
                    command, status=InterfaceCommandStatus.CONFLICT,
                    message="The idempotency key is already bound to different command content.",
                    errors=("IDEMPOTENCY_KEY_CONTENT_CONFLICT",),
                )

            return idempotent_assignment_result(command, stored_command_payload)

        validation_errors = validate_review_command(command, context, config)

        if validation_errors:
            return rejected_assignment_result(
                command, status=InterfaceCommandStatus.REJECTED, message="Review decision failed validation.",
                errors=validation_errors,
            )

        if command.action not in TERMINAL_DECISION_ACTIONS:
            return rejected_assignment_result(
                command, status=InterfaceCommandStatus.REJECTED,
                message="This executor accepts terminal review decisions only.",
                errors=("ACTION_NOT_SUPPORTED_BY_DECISION_EXECUTOR",),
            )

        if command.disposition is None:
            from ap_agent.exceptions import ReviewIntegrityError

            raise ReviewIntegrityError("Validated terminal decision has no disposition.")

        decision_id = uuid5(command.command_id, "phase-9-review-decision")
        evidence_payload = decision_evidence_payload(command, command_fingerprint)

        repository.append_review_decision(
            cursor, decision_id=decision_id, tenant_id=command.tenant_id, workflow_id=command.workflow_id,
            review_case_id=command.review_case_id, decision_type=command.disposition.value,
            decided_by=command.actor.actor_id, decision_notes=command.notes, evidence=evidence_payload,
        )

        resolution_notes = command.notes
        if resolution_notes is None or not resolution_notes.strip():
            resolution_notes = f"Review action {command.action.value} completed."

        repository.resolve_review_case(
            cursor, tenant_id=command.tenant_id, workflow_id=command.workflow_id,
            review_case_id=command.review_case_id, actor_id=command.actor.actor_id,
            resolution_code=command.disposition.value, resolution_notes=resolution_notes,
        )

        resulting_workflow_revision = repository.advance_workflow_phase(
            cursor, tenant_id=command.tenant_id, workflow_id=command.workflow_id, new_phase="HUMAN_REVIEW",
            observed_workflow_revision=context.workflow_revision,
        )

        if resulting_workflow_revision is None:
            from ap_agent.exceptions import ReviewIntegrityError

            raise ReviewIntegrityError("Workflow revision changed during decision execution.")

        next_sequence_number = repository.next_audit_sequence_number(
            cursor, tenant_id=command.tenant_id, workflow_id=command.workflow_id
        )
        audit_event_id = uuid5(command.command_id, "phase-9-interface-command-event")

        resulting_case_status = (
            ReviewCaseStatus.REJECTED
            if command.disposition == HumanReviewDisposition.REJECTED
            else ReviewCaseStatus.RESOLVED
        )
        resulting_review_revision = context.review_revision + 1

        audit_payload = {
            "command_id": str(command.command_id),
            "idempotency_key": command.idempotency_key,
            "command_fingerprint": command_fingerprint,
            "action": command.action.value,
            "disposition": command.disposition.value,
            "review_case_id": str(command.review_case_id),
            "document_id": str(command.document_id),
            "resulting_case_status": resulting_case_status.value,
            "resulting_review_revision": resulting_review_revision,
            "resulting_workflow_revision": resulting_workflow_revision,
            "workflow_resumed": False,
            "decision_id": str(decision_id),
            "requested_at": command.requested_at.isoformat(),
        }

        repository.insert_command_audit_event(
            cursor, event_id=audit_event_id, tenant_id=command.tenant_id, workflow_id=command.workflow_id,
            sequence_number=next_sequence_number, event_type="INTERFACE_COMMAND_EXECUTED", event_status="ACCEPTED",
            actor_role=command.actor.role.value, actor_id=command.actor.actor_id,
            message=f"Human-review decision recorded: {command.disposition.value}.", payload=audit_payload,
        )

        return ReviewCommandResult(
            command_id=command.command_id,
            idempotency_key=command.idempotency_key,
            status=InterfaceCommandStatus.ACCEPTED,
            review_case_id=command.review_case_id,
            document_id=command.document_id,
            resulting_case_status=resulting_case_status,
            resulting_revision=resulting_review_revision,
            workflow_resumed=False,
            decision_id=decision_id,
            message="Human-review decision recorded successfully.",
            errors=tuple(),
        )
