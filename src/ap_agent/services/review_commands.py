"""Phase 9 review-command policy and claim/release transactional executor.

Source: notebook cell 104 ("PHASE 9 — CELL 5", "Review-command
authorization and validation policy") and the claim/release half of cell
105 ("PHASE 9 — CELL 6", "Transactional claim/release command execution").
`validate_review_command`/`review_command_fingerprint`/the policy constants
are ported verbatim; `execute_assignment_command` composes
`ap_agent.repositories.review_repository.ReviewRepository`'s cursor-taking
primitives the same way the notebook's `execute_assignment_command_in_transaction`
composes its own inline helpers (see that module's docstring for why the
SQL and the policy are split across the repository/service boundary here,
where the notebook kept them in one function).

Idempotent-retry and idempotency-content-conflict handling
(`idempotent_assignment_result`/`rejected_assignment_result`, cell 105) is
shared with `ap_agent.services.review_decisions` and
`ap_agent.services.workflow_resume`, so it lives here as the one place all
three import it from.
"""

from __future__ import annotations

import json
import re
from hashlib import sha256
from typing import TYPE_CHECKING, Any
from uuid import UUID

from ap_agent.exceptions import ReviewIntegrityError
from ap_agent.models.interface import (
    HumanReviewDisposition,
    InterfaceCommandStatus,
    InterfaceConfig,
    ReviewAction,
    ReviewCaseStatus,
    ReviewCommand,
    ReviewCommandContext,
    ReviewCommandResult,
)
from ap_agent.models.normalization import InvoiceFieldName
from ap_agent.repositories.review_repository import ReviewRepository

if TYPE_CHECKING:  # pragma: no cover
    pass

__all__ = [
    "IDEMPOTENCY_KEY_PATTERN",
    "DECISION_DISPOSITIONS",
    "ACTIONS_WITHOUT_DISPOSITION",
    "ACTIONS_REQUIRING_REASON_CODES",
    "ACTIONS_REQUIRING_NOTES",
    "ACTIONS_REQUIRING_CLAIM",
    "validate_review_command",
    "review_command_fingerprint",
    "rejected_assignment_result",
    "idempotent_assignment_result",
    "execute_assignment_command",
]


IDEMPOTENCY_KEY_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{7,127}$")

DECISION_DISPOSITIONS = {
    ReviewAction.ACCEPT: HumanReviewDisposition.APPROVED,
    ReviewAction.CORRECT: HumanReviewDisposition.CORRECTED,
    ReviewAction.REQUEST_INFORMATION: HumanReviewDisposition.NEEDS_INFORMATION,
    ReviewAction.ESCALATE: HumanReviewDisposition.HOLD,
    ReviewAction.REJECT: HumanReviewDisposition.REJECTED,
}

ACTIONS_WITHOUT_DISPOSITION = {
    ReviewAction.CLAIM,
    ReviewAction.RELEASE,
    ReviewAction.CONFIRM_SUPPLIER,
    ReviewAction.CONFIRM_PURCHASE_ORDER,
}

ACTIONS_REQUIRING_REASON_CODES = {
    ReviewAction.ACCEPT,
    ReviewAction.CORRECT,
    ReviewAction.CONFIRM_SUPPLIER,
    ReviewAction.CONFIRM_PURCHASE_ORDER,
    ReviewAction.REQUEST_INFORMATION,
    ReviewAction.ESCALATE,
    ReviewAction.REJECT,
    ReviewAction.RESUME_WORKFLOW,
}

ACTIONS_REQUIRING_NOTES = {
    ReviewAction.REQUEST_INFORMATION,
    ReviewAction.ESCALATE,
    ReviewAction.REJECT,
}

ACTIONS_REQUIRING_CLAIM = {action for action in ReviewAction if action != ReviewAction.CLAIM}


# ------------------------------------------------------------
# Canonical idempotency fingerprint (notebook cell 104, verbatim)
# ------------------------------------------------------------


def review_command_fingerprint(command: ReviewCommand) -> str:
    correction_payloads = sorted(
        (
            {
                "field_name": correction.field_name.value,
                "line_number": correction.line_number,
                "previous_value": correction.previous_value,
                "corrected_value": correction.corrected_value,
                "reason": correction.reason,
                "evidence_reference_ids": sorted(correction.evidence_reference_ids),
            }
            for correction in command.corrections
        ),
        key=lambda item: (item["field_name"], -1 if item["line_number"] is None else item["line_number"]),
    )

    payload = {
        "command_id": str(command.command_id),
        "idempotency_key": command.idempotency_key,
        "tenant_id": str(command.tenant_id),
        "workflow_id": str(command.workflow_id),
        "batch_id": str(command.batch_id),
        "document_id": str(command.document_id),
        "review_case_id": str(command.review_case_id),
        "actor_id": command.actor.actor_id,
        "actor_role": command.actor.role.value,
        "action": command.action.value,
        "disposition": (None if command.disposition is None else command.disposition.value),
        "observed_review_revision": command.observed_review_revision,
        "observed_workflow_revision": command.observed_workflow_revision,
        "reason_codes": sorted(command.reason_codes),
        "notes": command.notes,
        "corrections": correction_payloads,
    }

    canonical_payload = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode(
        "utf-8"
    )

    return sha256(canonical_payload).hexdigest()


# ------------------------------------------------------------
# Fail-closed command validation (notebook cell 104, verbatim)
# ------------------------------------------------------------


def validate_review_command(
    command: ReviewCommand,
    context: ReviewCommandContext,
    config: InterfaceConfig,
) -> tuple[str, ...]:
    from ap_agent.models.interface import interface_permissions_for_role

    errors: list[str] = []

    def add_error(error_code: str) -> None:
        if error_code not in errors:
            errors.append(error_code)

    if command.command_id.int == 0:
        add_error("COMMAND_ID_INVALID")

    if not command.actor.actor_id.strip():
        add_error("ACTOR_ID_MISSING")

    if command.actor.authenticated_at.tzinfo is None:
        add_error("ACTOR_AUTHENTICATION_TIME_INVALID")

    if command.requested_at.tzinfo is None:
        add_error("REQUEST_TIME_INVALID")

    if (
        command.actor.authenticated_at.tzinfo is not None
        and command.requested_at.tzinfo is not None
        and command.actor.authenticated_at > command.requested_at
    ):
        add_error("ACTOR_AUTHENTICATED_AFTER_REQUEST")

    if not (command.tenant_id == command.actor.tenant_id == context.tenant_id):
        add_error("CROSS_TENANT_COMMAND")

    command_identity = (command.workflow_id, command.batch_id, command.document_id, command.review_case_id)
    context_identity = (context.workflow_id, context.batch_id, context.document_id, context.review_case_id)

    if command_identity != context_identity:
        add_error("COMMAND_IDENTITY_MISMATCH")

    if not command.idempotency_key.strip():
        add_error("IDEMPOTENCY_KEY_MISSING")
    elif IDEMPOTENCY_KEY_PATTERN.fullmatch(command.idempotency_key) is None:
        add_error("IDEMPOTENCY_KEY_INVALID")

    permitted_actions = interface_permissions_for_role(command.actor.role, config)

    if command.action not in permitted_actions:
        add_error("ACTION_NOT_PERMITTED")

    if command.observed_review_revision != context.review_revision:
        add_error("STALE_REVIEW_REVISION")

    if command.observed_workflow_revision != context.workflow_revision:
        add_error("STALE_WORKFLOW_REVISION")

    if command.action == ReviewAction.CLAIM:
        if context.case_status != ReviewCaseStatus.OPEN:
            add_error("CASE_NOT_OPEN")

        if context.assigned_reviewer_id is not None:
            add_error("CASE_ALREADY_ASSIGNED")

    elif command.action in ACTIONS_REQUIRING_CLAIM:
        if context.case_status != ReviewCaseStatus.IN_REVIEW:
            add_error("CASE_NOT_CLAIMED")

        if context.assigned_reviewer_id != command.actor.actor_id:
            add_error("CASE_ASSIGNED_TO_DIFFERENT_REVIEWER")

    required_disposition = DECISION_DISPOSITIONS.get(command.action)

    if required_disposition is not None:
        if command.disposition is None:
            add_error("DISPOSITION_REQUIRED")
        elif command.disposition != required_disposition:
            add_error("DISPOSITION_MISMATCH")
    elif command.action in ACTIONS_WITHOUT_DISPOSITION:
        if command.disposition is not None:
            add_error("DISPOSITION_NOT_ALLOWED")
    elif command.action == ReviewAction.RESUME_WORKFLOW:
        if command.disposition not in {HumanReviewDisposition.APPROVED, HumanReviewDisposition.CORRECTED}:
            add_error("RESUME_DISPOSITION_INVALID")

    if command.action in ACTIONS_REQUIRING_REASON_CODES and not tuple(
        reason.strip() for reason in command.reason_codes if reason.strip()
    ):
        add_error("REASON_CODE_REQUIRED")

    if command.action in ACTIONS_REQUIRING_NOTES and (command.notes is None or not command.notes.strip()):
        add_error("NOTES_REQUIRED")

    if command.action == ReviewAction.CORRECT:
        if not command.corrections:
            add_error("CORRECTIONS_REQUIRED")
    elif command.corrections:
        add_error("CORRECTIONS_NOT_ALLOWED")

    header_values = dict(context.header_field_values)
    available_evidence_ids = set(context.available_evidence_reference_ids)
    known_line_numbers = set(context.known_invoice_line_numbers)
    observed_targets: set[tuple[InvoiceFieldName, int | None]] = set()

    for correction in command.corrections:
        correction_target = (correction.field_name, correction.line_number)

        if correction_target in observed_targets:
            add_error("DUPLICATE_CORRECTION_TARGET")
        else:
            observed_targets.add(correction_target)

        if correction.field_name in config.correctable_header_fields:
            if correction.line_number is not None:
                add_error("HEADER_CORRECTION_LINE_NUMBER_PROHIBITED")

            # M11E.4: a configured header field the extractor never produced may be inserted, but only as an insertion:
            # previous_value must be null (a forged previous value is refused). A present field must match what is stored.
            expected_previous = header_values.get(correction.field_name)

            if correction.previous_value != expected_previous:
                add_error("PREVIOUS_VALUE_MISMATCH")

        elif correction.field_name in config.correctable_line_fields:
            if correction.line_number is None or correction.line_number < 1:
                add_error("LINE_CORRECTION_LINE_NUMBER_REQUIRED")
            elif known_line_numbers and correction.line_number not in known_line_numbers:
                add_error("UNKNOWN_INVOICE_LINE_NUMBER")

        else:
            add_error("CORRECTION_FIELD_NOT_ALLOWED")

        if not correction.corrected_value.strip():
            add_error("CORRECTED_VALUE_MISSING")

        if correction.corrected_value == correction.previous_value:
            add_error("CORRECTED_VALUE_UNCHANGED")

        if config.require_correction_reason and not correction.reason.strip():
            add_error("CORRECTION_REASON_REQUIRED")

        if config.require_correction_evidence and not correction.evidence_reference_ids:
            add_error("CORRECTION_EVIDENCE_REQUIRED")

        if not set(correction.evidence_reference_ids).issubset(available_evidence_ids):
            add_error("UNKNOWN_EVIDENCE_REFERENCE")

    return tuple(errors)


# ------------------------------------------------------------
# Result helpers shared by every transactional executor (notebook cell 105)
# ------------------------------------------------------------


def rejected_assignment_result(
    command: ReviewCommand, *, status: InterfaceCommandStatus, message: str, errors: tuple[str, ...]
) -> ReviewCommandResult:
    return ReviewCommandResult(
        command_id=command.command_id,
        idempotency_key=command.idempotency_key,
        status=status,
        review_case_id=command.review_case_id,
        document_id=command.document_id,
        resulting_case_status=None,
        resulting_revision=None,
        workflow_resumed=False,
        decision_id=None,
        message=message,
        errors=errors,
    )


def idempotent_assignment_result(command: ReviewCommand, stored_payload: dict[str, Any]) -> ReviewCommandResult:
    resulting_status_value = stored_payload.get("resulting_case_status")
    resulting_status = None if resulting_status_value is None else ReviewCaseStatus(resulting_status_value)
    decision_id_value = stored_payload.get("decision_id")

    return ReviewCommandResult(
        command_id=command.command_id,
        idempotency_key=command.idempotency_key,
        status=InterfaceCommandStatus.IDEMPOTENT,
        review_case_id=command.review_case_id,
        document_id=command.document_id,
        resulting_case_status=resulting_status,
        resulting_revision=stored_payload.get("resulting_review_revision"),
        workflow_resumed=bool(stored_payload.get("workflow_resumed", False)),
        decision_id=(None if decision_id_value is None else UUID(str(decision_id_value))),
        message="Identical review command was already accepted.",
        errors=tuple(),
    )


# ------------------------------------------------------------
# Transactional claim/release executor (notebook cell 105, half of it)
# ------------------------------------------------------------


def execute_assignment_command(
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
        context = repository.lock_assignment_context(
            cursor, tenant_id=command.tenant_id, workflow_id=command.workflow_id,
            batch_id=command.batch_id, document_id=command.document_id, review_case_id=command.review_case_id,
        )

        if context is None:
            return rejected_assignment_result(
                command, status=InterfaceCommandStatus.REJECTED, message="Review command target was not found.",
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
                command, status=InterfaceCommandStatus.REJECTED, message="Review command failed validation.",
                errors=validation_errors,
            )

        if command.action not in {ReviewAction.CLAIM, ReviewAction.RELEASE}:
            return rejected_assignment_result(
                command, status=InterfaceCommandStatus.REJECTED,
                message="This executor accepts assignment commands only.",
                errors=("ACTION_NOT_SUPPORTED_BY_ASSIGNMENT_EXECUTOR",),
            )

        is_claim = command.action == ReviewAction.CLAIM
        resulting_case_status = ReviewCaseStatus.IN_REVIEW if is_claim else ReviewCaseStatus.OPEN
        event_message = (
            "Human-review case claimed by reviewer." if is_claim else "Human-review case released to the queue."
        )

        resulting_workflow_revision = repository.apply_claim_or_release(
            cursor, tenant_id=command.tenant_id, workflow_id=command.workflow_id,
            review_case_id=command.review_case_id, claim=is_claim, actor_id=command.actor.actor_id,
            observed_workflow_revision=context.workflow_revision,
        )

        if resulting_workflow_revision is None:
            # Impossible under a `FOR UPDATE` lock held since
            # `lock_assignment_context` re-read the same revision this
            # transaction just validated against -- a genuine integrity
            # failure, not a normal rejection (notebook cell 105 raises a
            # bare `RuntimeError` for the identical case).
            raise ReviewIntegrityError("Workflow revision changed during command execution.")

        from uuid import uuid5

        next_sequence_number = repository.next_audit_sequence_number(
            cursor, tenant_id=command.tenant_id, workflow_id=command.workflow_id
        )
        audit_event_id = uuid5(command.command_id, "phase-9-interface-command-event")

        audit_payload = {
            "command_id": str(command.command_id),
            "idempotency_key": command.idempotency_key,
            "command_fingerprint": command_fingerprint,
            "action": command.action.value,
            "review_case_id": str(command.review_case_id),
            "document_id": str(command.document_id),
            "resulting_case_status": resulting_case_status.value,
            "resulting_review_revision": context.review_revision,
            "resulting_workflow_revision": resulting_workflow_revision,
            "workflow_resumed": False,
            "decision_id": None,
            "requested_at": command.requested_at.isoformat(),
        }

        repository.insert_command_audit_event(
            cursor, event_id=audit_event_id, tenant_id=command.tenant_id, workflow_id=command.workflow_id,
            sequence_number=next_sequence_number, event_type="INTERFACE_COMMAND_EXECUTED", event_status="ACCEPTED",
            actor_role=command.actor.role.value, actor_id=command.actor.actor_id, message=event_message,
            payload=audit_payload,
        )

        return ReviewCommandResult(
            command_id=command.command_id,
            idempotency_key=command.idempotency_key,
            status=InterfaceCommandStatus.ACCEPTED,
            review_case_id=command.review_case_id,
            document_id=command.document_id,
            resulting_case_status=resulting_case_status,
            resulting_revision=context.review_revision,
            workflow_resumed=False,
            decision_id=None,
            message=event_message,
            errors=tuple(),
        )
