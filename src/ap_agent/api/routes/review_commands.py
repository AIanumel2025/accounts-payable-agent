"""`POST /api/v1/review-cases/{review_case_id}/commands` -- notebook cell
108's `submit_review_command`, backed by `ap_agent.services.review_commands`/
`review_decisions`/`workflow_resume`.

Deliberate, documented fix vs. the notebook (CLAUDE.md: "Known, deliberate
deviations from the notebook must be documented, not silently fixed"; see
`docs/m10_phase_9_review_api_report.md` §"Deviations"): the notebook's
`queue_record_for_case` looks a case up through `retrieve_review_queue`,
which only ever returns *active* (OPEN/CLAIMED) cases -- so the notebook's
own API layer can never actually resolve a `RESUME_WORKFLOW` command
end-to-end (the case is RESOLVED by the time a resume is requested, so the
lookup 404s before the resume-specific logic below ever runs; only the
notebook's own in-process cell 107 exercises a real resume). This route
instead looks the case up by id regardless of status
(`ReviewRepository.get_review_queue_record`/`_list_review_queue_by_id`),
so a resume command against a resolved case is reachable through the API,
exactly as task §7/§14 require it to be testable.

`EXECUTE_PAYMENT` and any other unsupported command are already rejected
before this function runs: `ApiReviewCommandRequest.action` is a real
`ReviewAction` enum with no `EXECUTE_PAYMENT`/payment member (task §4:
"payment execution is prohibited" -- there is no such member to accept),
so FastAPI/Pydantic itself returns 422 for that payload before any
business logic executes (task §8: "Reject any payment ... instruction").
"""

from __future__ import annotations

from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, Path

from ap_agent.api.config import ApiConfig
from ap_agent.api.dependencies import AuthenticatedActor, get_api_config, get_interface_config, get_review_repository
from ap_agent.api.schemas import (
    ApiEnvelope,
    ApiReviewCommandRequest,
    CommandResultResponse,
    WorkflowResumeResponse,
)
from ap_agent.exceptions import ReviewCaseNotFoundError, ReviewCommandRejectedError, ReviewIntegrityError
from ap_agent.models.interface import (
    InterfaceCommandStatus,
    InterfaceConfig,
    ReviewAction,
    ReviewCommand,
    ReviewFieldCorrection,
    interface_utc_now,
)
from ap_agent.repositories.review_repository import ReviewRepository
from ap_agent.services.review_commands import execute_assignment_command, review_command_fingerprint, validate_review_command
from ap_agent.services.review_decisions import execute_decision_command
from ap_agent.services.workflow_resume import execute_resume_command

router = APIRouter(tags=["review-commands"])

_ASSIGNMENT_ACTIONS = {ReviewAction.CLAIM, ReviewAction.RELEASE}
_DECISION_ACTIONS = {ReviewAction.ACCEPT, ReviewAction.CORRECT, ReviewAction.REJECT}


@router.post("/api/v1/review-cases/{review_case_id}/commands", response_model=ApiEnvelope)
def submit_review_command(
    command_request: ApiReviewCommandRequest,
    actor: AuthenticatedActor,
    review_case_id: UUID = Path(description="Human-review case identifier"),
    repository: ReviewRepository = Depends(get_review_repository),
    interface_config: InterfaceConfig = Depends(get_interface_config),
    api_config: ApiConfig = Depends(get_api_config),
) -> ApiEnvelope:
    queue_record = repository.get_review_queue_record(actor.tenant_id, review_case_id)

    if queue_record is None:
        raise ReviewCaseNotFoundError(f"Review case {review_case_id} was not found for this tenant.")

    context = repository.get_command_context(actor.tenant_id, review_case_id)

    if context is None:
        raise ReviewIntegrityError("Review case has no command context.")

    corrections = tuple(
        ReviewFieldCorrection(
            field_name=correction.field_name,
            line_number=correction.line_number,
            previous_value=correction.previous_value,
            corrected_value=correction.corrected_value,
            reason=correction.reason,
            evidence_reference_ids=correction.evidence_reference_ids,
        )
        for correction in command_request.corrections
    )

    command = ReviewCommand(
        command_id=command_request.command_id,
        idempotency_key=command_request.idempotency_key,
        tenant_id=actor.tenant_id,
        workflow_id=queue_record.workflow_id,
        batch_id=queue_record.batch_id,
        document_id=queue_record.document_id,
        review_case_id=queue_record.review_case_id,
        actor=actor,
        action=command_request.action,
        disposition=command_request.disposition,
        observed_review_revision=command_request.observed_review_revision,
        observed_workflow_revision=command_request.observed_workflow_revision,
        reason_codes=command_request.reason_codes,
        notes=command_request.notes,
        corrections=corrections,
        requested_at=command_request.requested_at,
    )

    is_resume = command.action == ReviewAction.RESUME_WORKFLOW

    if not is_resume:
        validation_errors = validate_review_command(command, context, interface_config)

        if validation_errors:
            raise ReviewCommandRejectedError(validation_errors, "Review command failed validation.")

    elif not api_config.enable_review_command_writes:
        # Resume validation depends on the latest *persisted* resolved
        # decision; it is deliberately not simulated in validation-only
        # mode (notebook cell 108's identical choice).
        raise ReviewCommandRejectedError(
            ("RESUME_REQUIRES_RESOLVED_DECISION",), "Resume commands require write-enabled mode."
        )

    if not api_config.enable_review_command_writes:
        return ApiEnvelope(
            request_id=uuid4(),
            status="VALIDATED",
            data={
                "command_id": str(command.command_id),
                "review_case_id": str(command.review_case_id),
                "action": command.action.value,
                "command_fingerprint": review_command_fingerprint(command),
                "execution_mode": "VALIDATION_ONLY",
                "database_mutation": False,
            },
            errors=tuple(),
            generated_at=interface_utc_now(),
        )

    if command.action in _ASSIGNMENT_ACTIONS:
        result = execute_assignment_command(repository, command, interface_config)
        response_payload = CommandResultResponse.from_domain(result).model_dump(mode="json")

    elif command.action in _DECISION_ACTIONS:
        result = execute_decision_command(repository, command, interface_config)
        response_payload = CommandResultResponse.from_domain(result).model_dump(mode="json")

    elif is_resume:
        execution = execute_resume_command(repository, command, interface_config)
        result = execution.command_result
        response_payload = WorkflowResumeResponse.from_execution(execution).model_dump(mode="json")

    else:
        # CONFIRM_SUPPLIER/CONFIRM_PURCHASE_ORDER/REQUEST_INFORMATION/
        # ESCALATE are authorised, validatable actions (task §4) with no
        # transactional executor: out of the M10 required command set
        # (task §7 lists only claim/release/correction/decision/resume).
        raise ReviewCommandRejectedError(
            ("REVIEW_ACTION_NOT_IMPLEMENTED",), f"{command.action.value} has no transactional executor yet."
        )

    if result.status not in {InterfaceCommandStatus.ACCEPTED, InterfaceCommandStatus.IDEMPOTENT}:
        raise ReviewCommandRejectedError(result.errors, result.message)

    return ApiEnvelope(
        request_id=uuid4(),
        status=result.status.value,
        data=response_payload,
        errors=tuple(),
        generated_at=interface_utc_now(),
    )
