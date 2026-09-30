"""Phase 9 controlled workflow-resumption handoff.

Source: notebook cell 107 ("PHASE 9 — CELL 8", "Controlled
workflow-resumption handoff"): `choose_review_restart_stage`,
`validate_resume_command`, `build_workflow_resume_plan`,
`resume_plan_from_audit_payload` are ported verbatim;
`execute_resume_command` composes
`ap_agent.repositories.review_repository.ReviewRepository`'s primitives
the same way the notebook's `execute_resume_command_in_transaction`
composes its own inline helpers.

A resume command only ever *creates a handoff plan* -- it selects the
earliest orchestration stage the recorded correction could have affected
and derives a new, immutable version id for the corrected input. It never
executes the Phase 1-8 pipeline itself (task §7: "Workflow resumption must
create a controlled handoff only. It must not execute the full Phase 1-8
pipeline inside the HTTP request."); actually resuming the workflow from
that plan is `ap_agent.orchestration`'s job, driven by a worker outside
the request/response cycle (out of scope for M10, task §23).
"""

from __future__ import annotations

import json
from hashlib import sha256
from typing import Any
from uuid import UUID, uuid5

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
    WorkflowResumeExecution,
    WorkflowResumePlan,
    interface_permissions_for_role,
)
from ap_agent.models.normalization import InvoiceFieldName
from ap_agent.models.orchestration import OrchestrationStage
from ap_agent.repositories.operations_repository import insert_resume_job, resume_job_id
from ap_agent.repositories.review_repository import ReviewRepository, human_review_disposition_from_database
from ap_agent.serialization.memory_json import canonical_json_bytes
from ap_agent.services.review_commands import (
    IDEMPOTENCY_KEY_PATTERN,
    idempotent_assignment_result,
    rejected_assignment_result,
    review_command_fingerprint,
)

__all__ = [
    "FINANCIAL_RESTART_FIELDS",
    "REFERENCE_RESTART_FIELDS",
    "choose_review_restart_stage",
    "validate_resume_command",
    "build_workflow_resume_plan",
    "resume_plan_from_audit_payload",
    "execute_resume_command",
]


FINANCIAL_RESTART_FIELDS = {
    InvoiceFieldName.INVOICE_DATE,
    InvoiceFieldName.DUE_DATE,
    InvoiceFieldName.CURRENCY,
    InvoiceFieldName.SUBTOTAL,
    InvoiceFieldName.TAX_AMOUNT,
    InvoiceFieldName.DISCOUNT_AMOUNT,
    InvoiceFieldName.SHIPPING_AMOUNT,
    InvoiceFieldName.TOTAL_AMOUNT,
    InvoiceFieldName.LINE_DESCRIPTION,
    InvoiceFieldName.LINE_QUANTITY,
    InvoiceFieldName.LINE_UNIT_PRICE,
    InvoiceFieldName.LINE_AMOUNT,
}

REFERENCE_RESTART_FIELDS = {
    InvoiceFieldName.SUPPLIER_NAME,
    InvoiceFieldName.SUPPLIER_ADDRESS,
    InvoiceFieldName.PURCHASE_ORDER_NUMBER,
}


def choose_review_restart_stage(
    disposition: HumanReviewDisposition,
    corrected_fields: tuple[InvoiceFieldName, ...],
) -> OrchestrationStage:
    corrected_field_set = set(corrected_fields)

    if disposition == HumanReviewDisposition.APPROVED:
        if corrected_field_set:
            raise ValueError("Approved decisions must not contain corrections.")
        return OrchestrationStage.MEMORY_PERSISTENCE

    if disposition != HumanReviewDisposition.CORRECTED:
        raise ValueError("Only approved or corrected decisions may resume processing.")

    if not corrected_field_set:
        raise ValueError("A corrected decision contains no corrections.")

    if corrected_field_set & FINANCIAL_RESTART_FIELDS:
        return OrchestrationStage.FINANCIAL_VALIDATION

    if corrected_field_set & REFERENCE_RESTART_FIELDS:
        return OrchestrationStage.REFERENCE_MATCHING

    # Non-financial metadata corrections are conservatively rechecked from
    # financial validation onward.
    return OrchestrationStage.FINANCIAL_VALIDATION


def validate_resume_command(
    command: ReviewCommand,
    context: ReviewCommandContext,
    decision_id: UUID,
    decision_disposition: HumanReviewDisposition,
    decision_evidence: dict[str, Any],
    config: InterfaceConfig,
) -> tuple[str, ...]:
    errors: list[str] = []

    def add_error(error_code: str) -> None:
        if error_code not in errors:
            errors.append(error_code)

    if command.action != ReviewAction.RESUME_WORKFLOW:
        add_error("RESUME_ACTION_REQUIRED")

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

    if (command.workflow_id, command.batch_id, command.document_id, command.review_case_id) != (
        context.workflow_id, context.batch_id, context.document_id, context.review_case_id,
    ):
        add_error("COMMAND_IDENTITY_MISMATCH")

    if not command.idempotency_key.strip():
        add_error("IDEMPOTENCY_KEY_MISSING")
    elif IDEMPOTENCY_KEY_PATTERN.fullmatch(command.idempotency_key) is None:
        add_error("IDEMPOTENCY_KEY_INVALID")

    if ReviewAction.RESUME_WORKFLOW not in interface_permissions_for_role(command.actor.role, config):
        add_error("ACTION_NOT_PERMITTED")

    if context.case_status != ReviewCaseStatus.RESOLVED:
        add_error("RESOLVED_CASE_REQUIRED")

    if context.assigned_reviewer_id != command.actor.actor_id:
        add_error("CASE_ASSIGNED_TO_DIFFERENT_REVIEWER")

    if command.observed_review_revision != context.review_revision:
        add_error("STALE_REVIEW_REVISION")

    if command.observed_workflow_revision != context.workflow_revision:
        add_error("STALE_WORKFLOW_REVISION")

    if command.disposition not in {HumanReviewDisposition.APPROVED, HumanReviewDisposition.CORRECTED}:
        add_error("RESUME_DISPOSITION_INVALID")

    if command.disposition != decision_disposition:
        add_error("DECISION_DISPOSITION_MISMATCH")

    if command.corrections:
        add_error("RESUME_CORRECTIONS_NOT_ALLOWED")

    if not tuple(reason.strip() for reason in command.reason_codes if reason.strip()):
        add_error("REASON_CODE_REQUIRED")

    if str(decision_id) != str(decision_evidence.get("decision_id", decision_id)):
        add_error("DECISION_IDENTITY_MISMATCH")

    if decision_evidence.get("source") != "PHASE_9_HUMAN_REVIEW_INTERFACE":
        add_error("DECISION_SOURCE_INVALID")

    return tuple(errors)


def build_workflow_resume_plan(
    command: ReviewCommand,
    decision_id: UUID,
    decision_disposition: HumanReviewDisposition,
    decision_evidence: dict[str, Any],
    normalized_payload: dict[str, Any],
) -> WorkflowResumePlan:
    correction_payloads = decision_evidence.get("corrections", [])

    if not isinstance(correction_payloads, list):
        raise ReviewIntegrityError("Decision corrections are malformed.")

    corrected_fields = tuple(
        InvoiceFieldName(correction_payload["field_name"])
        for correction_payload in correction_payloads
        if isinstance(correction_payload, dict)
    )

    if len(corrected_fields) != len(correction_payloads):
        raise ReviewIntegrityError("A decision correction is malformed.")

    restart_stage = choose_review_restart_stage(decision_disposition, corrected_fields)

    source_normalization_sha256 = sha256(canonical_json_bytes(normalized_payload)).hexdigest()

    correction_overlay = {
        "decision_id": str(decision_id),
        "document_id": str(command.document_id),
        "source_normalization_sha256": source_normalization_sha256,
        "corrections": correction_payloads,
    }

    correction_overlay_json = json.dumps(
        correction_overlay, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )
    correction_overlay_sha256 = sha256(correction_overlay_json.encode("utf-8")).hexdigest()

    resume_plan_id = uuid5(
        decision_id, f"phase-9-resume-plan:{restart_stage.value}:{correction_overlay_sha256}"
    )
    derived_version = f"human-review-derived-{correction_overlay_sha256[:16]}"

    return WorkflowResumePlan(
        resume_plan_id=resume_plan_id,
        tenant_id=command.tenant_id,
        workflow_id=command.workflow_id,
        batch_id=command.batch_id,
        document_id=command.document_id,
        review_case_id=command.review_case_id,
        decision_id=decision_id,
        disposition=decision_disposition,
        restart_stage=restart_stage,
        source_normalization_sha256=source_normalization_sha256,
        correction_overlay_json=correction_overlay_json,
        correction_overlay_sha256=correction_overlay_sha256,
        derived_version=derived_version,
        created_at=command.requested_at,
    )


def resume_plan_from_audit_payload(command: ReviewCommand, stored_payload: dict[str, Any]) -> WorkflowResumePlan:
    return WorkflowResumePlan(
        resume_plan_id=UUID(stored_payload["resume_plan_id"]),
        tenant_id=command.tenant_id,
        workflow_id=command.workflow_id,
        batch_id=command.batch_id,
        document_id=command.document_id,
        review_case_id=command.review_case_id,
        decision_id=UUID(stored_payload["decision_id"]),
        disposition=HumanReviewDisposition(stored_payload["disposition"]),
        restart_stage=OrchestrationStage(stored_payload["restart_stage"]),
        source_normalization_sha256=stored_payload["source_normalization_sha256"],
        correction_overlay_json=stored_payload["correction_overlay_json"],
        correction_overlay_sha256=stored_payload["correction_overlay_sha256"],
        derived_version=stored_payload["derived_version"],
        created_at=command.requested_at,
    )


def execute_resume_command(
    repository: ReviewRepository,
    command: ReviewCommand,
    config: InterfaceConfig,
) -> WorkflowResumeExecution:
    if command.tenant_id != command.actor.tenant_id:
        return WorkflowResumeExecution(
            command_result=rejected_assignment_result(
                command, status=InterfaceCommandStatus.REJECTED, message="Cross-tenant command rejected.",
                errors=("CROSS_TENANT_COMMAND",),
            ),
            resume_plan=None,
        )

    with repository.transaction(command.tenant_id) as cursor:
        context = repository.lock_decision_context(
            cursor, tenant_id=command.tenant_id, workflow_id=command.workflow_id,
            batch_id=command.batch_id, document_id=command.document_id, review_case_id=command.review_case_id,
        )

        if context is None:
            return WorkflowResumeExecution(
                command_result=rejected_assignment_result(
                    command, status=InterfaceCommandStatus.REJECTED, message="Resume target was not found.",
                    errors=("COMMAND_TARGET_NOT_FOUND",),
                ),
                resume_plan=None,
            )

        command_fingerprint = review_command_fingerprint(command)

        stored_resume_payload = repository.find_stored_command_payload(
            cursor, tenant_id=command.tenant_id, workflow_id=command.workflow_id,
            idempotency_key=command.idempotency_key, event_type="WORKFLOW_RESUME_REQUESTED",
        )

        if stored_resume_payload is not None:
            if stored_resume_payload.get("command_fingerprint") != command_fingerprint:
                return WorkflowResumeExecution(
                    command_result=rejected_assignment_result(
                        command, status=InterfaceCommandStatus.CONFLICT,
                        message="The idempotency key is already bound to another resume request.",
                        errors=("IDEMPOTENCY_KEY_CONTENT_CONFLICT",),
                    ),
                    resume_plan=None,
                )

            idempotent_result = idempotent_assignment_result(command, stored_resume_payload)
            return WorkflowResumeExecution(
                command_result=idempotent_result,
                resume_plan=resume_plan_from_audit_payload(command, stored_resume_payload),
            )

        decision_row = repository.latest_review_decision_for_resume(
            cursor, tenant_id=command.tenant_id, workflow_id=command.workflow_id, review_case_id=command.review_case_id,
        )

        if decision_row is None:
            return WorkflowResumeExecution(
                command_result=rejected_assignment_result(
                    command, status=InterfaceCommandStatus.REJECTED, message="No review decision is available.",
                    errors=("REVIEW_DECISION_REQUIRED",),
                ),
                resume_plan=None,
            )

        (
            decision_id, decision_type, decided_by, decision_evidence, normalized_payload,
            financial_payload, matching_payload, reference_payload, stored_memory_payload_sha256,
            original_memory_record_id, source_memory_version_id, source_memory_version_label,
        ) = decision_row

        if decided_by != command.actor.actor_id:
            return WorkflowResumeExecution(
                command_result=rejected_assignment_result(
                    command, status=InterfaceCommandStatus.REJECTED,
                    message="Only the deciding reviewer may request this resume operation.",
                    errors=("DECISION_ACTOR_MISMATCH",),
                ),
                resume_plan=None,
            )

        if not isinstance(decision_evidence, dict):
            raise ReviewIntegrityError("Stored decision evidence is malformed.")

        if not isinstance(normalized_payload, dict):
            raise ReviewIntegrityError("Stored normalization payload is malformed.")

        reconstructed_memory_payload = {
            "normalization_result": normalized_payload,
            "financial_validation_result": financial_payload,
            "matching_result": matching_payload,
            "matched_reference_data": reference_payload,
        }

        recalculated_memory_payload_sha256 = sha256(
            canonical_json_bytes(reconstructed_memory_payload)
        ).hexdigest()

        if recalculated_memory_payload_sha256 != stored_memory_payload_sha256.strip():
            raise ReviewIntegrityError("Resume input payload-integrity failure.")

        decision_disposition = human_review_disposition_from_database(decision_type)

        validation_evidence = dict(decision_evidence)
        validation_evidence["decision_id"] = str(decision_id)

        validation_errors = validate_resume_command(
            command=command, context=context, decision_id=decision_id,
            decision_disposition=decision_disposition, decision_evidence=validation_evidence, config=config,
        )

        if validation_errors:
            return WorkflowResumeExecution(
                command_result=rejected_assignment_result(
                    command, status=InterfaceCommandStatus.REJECTED, message="Resume command failed validation.",
                    errors=validation_errors,
                ),
                resume_plan=None,
            )

        resume_plan = build_workflow_resume_plan(
            command=command, decision_id=decision_id, decision_disposition=decision_disposition,
            decision_evidence=decision_evidence, normalized_payload=normalized_payload,
        )

        resulting_workflow_revision = repository.advance_workflow_phase(
            cursor, tenant_id=command.tenant_id, workflow_id=command.workflow_id,
            new_phase=resume_plan.restart_stage.value, new_status="IN_PROGRESS", review_required=False,
            clear_completed_at=True, observed_workflow_revision=context.workflow_revision,
            require_current_phase="HUMAN_REVIEW", require_current_status="REVIEW_REQUIRED",
            require_review_required=True,
        )

        if resulting_workflow_revision is None:
            # M11C deviation D-M11C-2 (docs/m11c_review_actions_report.md): the
            # notebook/M10 raised a bare integrity error here. Under the
            # `FOR UPDATE` lock held since `lock_decision_context`, the
            # observed revision was just validated, so the only way this
            # guarded UPDATE can match zero rows is that the workflow is
            # already past `HUMAN_REVIEW/REVIEW_REQUIRED` -- i.e. a resume
            # handoff was already requested under a *different* idempotency
            # key (two tabs, a double submit that regenerated identity). That
            # is a client-visible conflict (409), not an internal failure
            # (500). Nothing has been written yet, so returning is safe.
            return WorkflowResumeExecution(
                command_result=rejected_assignment_result(
                    command, status=InterfaceCommandStatus.CONFLICT,
                    message="A workflow resume handoff was already requested for this case.",
                    errors=("WORKFLOW_NOT_AWAITING_RESUME",),
                ),
                resume_plan=None,
            )

        next_sequence_number = repository.next_audit_sequence_number(
            cursor, tenant_id=command.tenant_id, workflow_id=command.workflow_id
        )
        audit_event_id = uuid5(command.command_id, "phase-9-workflow-resume-request")

        audit_payload = {
            "command_id": str(command.command_id),
            "idempotency_key": command.idempotency_key,
            "command_fingerprint": command_fingerprint,
            "action": command.action.value,
            "disposition": decision_disposition.value,
            "decision_id": str(decision_id),
            "resume_plan_id": str(resume_plan.resume_plan_id),
            "restart_stage": resume_plan.restart_stage.value,
            "source_normalization_sha256": resume_plan.source_normalization_sha256,
            "source_memory_payload_sha256": stored_memory_payload_sha256.strip(),
            # M11D Core: which stored memory the plan was built from (the
            # original record, or a derived version for a second review cycle).
            "source_memory_record_id": str(original_memory_record_id),
            "source_memory_version_id": (
                None if source_memory_version_id is None else str(source_memory_version_id)
            ),
            "correction_overlay_json": resume_plan.correction_overlay_json,
            "correction_overlay_sha256": resume_plan.correction_overlay_sha256,
            "derived_version": resume_plan.derived_version,
            "resulting_case_status": ReviewCaseStatus.RESOLVED.value,
            "resulting_review_revision": context.review_revision,
            "resulting_workflow_revision": resulting_workflow_revision,
            "workflow_resumed": True,
            # M11D Core: the durable job that consumes this plan (additive).
            "resume_job_id": str(resume_job_id(command.tenant_id, resume_plan.resume_plan_id)),
            "requested_at": command.requested_at.isoformat(),
        }

        repository.insert_command_audit_event(
            cursor, event_id=audit_event_id, tenant_id=command.tenant_id, workflow_id=command.workflow_id,
            sequence_number=next_sequence_number, event_type="WORKFLOW_RESUME_REQUESTED", event_status="IN_PROGRESS",
            actor_role=command.actor.role.value, actor_id=command.actor.actor_id,
            message="Human-review decision produced a workflow resume plan.", payload=audit_payload,
        )

        # M11D Core: enqueue the job that will actually resume the workflow
        # in this same transaction -- a committed handoff can never exist
        # without its job, and a job never without its handoff.
        cursor.execute(
            "SELECT source_name FROM ap_agent.workflow_instances WHERE tenant_id = %s AND workflow_id = %s;",
            (command.tenant_id, command.workflow_id),
        )
        source_name = cursor.fetchone()[0]

        insert_resume_job(
            cursor, tenant_id=command.tenant_id, idempotency_key=command.idempotency_key,
            request_fingerprint=command_fingerprint, source_name=source_name, workflow_id=command.workflow_id,
            batch_id=command.batch_id, document_id=command.document_id, review_id=command.review_case_id,
            resume_plan_id=resume_plan.resume_plan_id, restart_stage=resume_plan.restart_stage.value,
            created_by=command.actor.actor_id,
        )

        command_result = ReviewCommandResult(
            command_id=command.command_id,
            idempotency_key=command.idempotency_key,
            status=InterfaceCommandStatus.ACCEPTED,
            review_case_id=command.review_case_id,
            document_id=command.document_id,
            resulting_case_status=ReviewCaseStatus.RESOLVED,
            resulting_revision=context.review_revision,
            workflow_resumed=True,
            decision_id=decision_id,
            message="Workflow resume plan created successfully.",
            errors=tuple(),
        )

        return WorkflowResumeExecution(command_result=command_result, resume_plan=resume_plan)
