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

from fastapi import APIRouter, Depends, Path, Request

from ap_agent.api.config import ApiConfig
from ap_agent.api.dependencies import AuthenticatedActor, get_api_config, get_interface_config, get_review_repository
from ap_agent.api.schemas import (
    ApiEnvelope,
    ApiErrorEnvelope,
    ApiReviewCommandRequest,
    CommandCapabilitiesResponse,
    CommandResultResponse,
    ReviewCommandResponseData,
    ValidationOnlyCommandResponse,
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
from ap_agent.models.operations import WorkflowJobType
from ap_agent.repositories.operations_repository import OperationsRepository
from ap_agent.repositories.review_repository import ReviewRepository
from ap_agent.services.job_dispatch import dispatch_if_queued
from ap_agent.services.review_commands import execute_assignment_command, review_command_fingerprint, validate_review_command
from ap_agent.services.review_decisions import execute_decision_command
from ap_agent.services.review_capabilities import build_command_capabilities
from ap_agent.services.review_queries import get_invoice_detail
from ap_agent.services.workflow_resume import execute_resume_command

router = APIRouter(tags=["review-commands"])

_ASSIGNMENT_ACTIONS = {ReviewAction.CLAIM, ReviewAction.RELEASE}
_DECISION_ACTIONS = {ReviewAction.ACCEPT, ReviewAction.CORRECT, ReviewAction.REJECT}


# M11C task §5: controlled error envelopes are part of the OpenAPI contract.
_ERROR_RESPONSES: dict[int | str, dict] = {
    status_code: {"model": ApiErrorEnvelope, "description": description}
    for status_code, description in {
        401: "Missing or invalid authentication.",
        403: "Forbidden role or cross-tenant action.",
        404: "Unknown review case.",
        409: "Stale revision, ownership conflict or idempotency-content conflict.",
        422: "Invalid, unsupported or prohibited command.",
        500: "Redacted internal error.",
        503: "Database unavailable.",
    }.items()
}


@router.get(
    "/api/v1/review-cases/{review_case_id}/command-capabilities",
    response_model=ApiEnvelope[CommandCapabilitiesResponse],
    responses=_ERROR_RESPONSES,
)
def get_review_command_capabilities(
    actor: AuthenticatedActor,
    review_case_id: UUID = Path(description="Human-review case identifier"),
    repository: ReviewRepository = Depends(get_review_repository),
    interface_config: InterfaceConfig = Depends(get_interface_config),
    api_config: ApiConfig = Depends(get_api_config),
) -> ApiEnvelope[CommandCapabilitiesResponse]:
    """Advisory, read-only projection of what this actor may attempt on this
    case right now (M11C task §6). The command executors still re-validate
    everything transactionally; nothing here authorises anything."""

    detail = get_invoice_detail(repository, actor.tenant_id, review_case_id)
    context = repository.get_command_context(actor.tenant_id, review_case_id)

    if context is None:
        raise ReviewIntegrityError("Review case has no command context.")

    capabilities = build_command_capabilities(
        actor=actor,
        context=context,
        detail=detail,
        normalized_payload=repository.get_normalized_invoice_payload(actor.tenant_id, review_case_id),
        config=interface_config,
        writes_enabled=api_config.enable_review_command_writes,
    )

    return ApiEnvelope(
        request_id=uuid4(),
        status="SUCCEEDED",
        data=CommandCapabilitiesResponse(**capabilities).model_dump(mode="json"),
        errors=tuple(),
        generated_at=interface_utc_now(),
    )


@router.post(
    "/api/v1/review-cases/{review_case_id}/commands",
    response_model=ApiEnvelope[ReviewCommandResponseData],
    responses=_ERROR_RESPONSES,
)
def submit_review_command(
    request: Request,
    command_request: ApiReviewCommandRequest,
    actor: AuthenticatedActor,
    review_case_id: UUID = Path(description="Human-review case identifier"),
    repository: ReviewRepository = Depends(get_review_repository),
    interface_config: InterfaceConfig = Depends(get_interface_config),
    api_config: ApiConfig = Depends(get_api_config),
) -> ApiEnvelope[ReviewCommandResponseData]:
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

    if not api_config.enable_review_command_writes:
        # Validation-only mode never persists anything, so there is no
        # stored command to be idempotent against yet -- validating here,
        # against a plain unlocked read of the current context, is safe.
        if not is_resume:
            validation_errors = validate_review_command(command, context, interface_config)

            if validation_errors:
                raise ReviewCommandRejectedError(validation_errors, "Review command failed validation.")
        else:
            # Resume validation depends on the latest *persisted* resolved
            # decision; it is deliberately not simulated in validation-only
            # mode (notebook cell 108's identical choice).
            raise ReviewCommandRejectedError(
                ("RESUME_REQUIRES_RESOLVED_DECISION",), "Resume commands require write-enabled mode."
            )

        return ApiEnvelope(
            request_id=uuid4(),
            status="VALIDATED",
            data=ValidationOnlyCommandResponse(
                command_id=command.command_id,
                review_case_id=command.review_case_id,
                action=command.action,
                command_fingerprint=review_command_fingerprint(command),
                execution_mode="VALIDATION_ONLY",
                database_mutation=False,
            ).model_dump(mode="json"),
            errors=tuple(),
            generated_at=interface_utc_now(),
        )

    # Write-enabled mode: do *not* pre-validate against this unlocked read
    # here. An idempotent retry's re-read context has already moved on from
    # what the original, now-stored command observed (e.g. a retried CLAIM
    # sees its own prior CLAIM's new case_status/workflow_revision), which
    # would make a plain re-validation reject a request that is actually a
    # valid identical retry. Each executor below checks for a stored
    # idempotent match *before* it validates, inside one locked transaction
    # (`ap_agent.services.review_commands`/`review_decisions`/
    # `workflow_resume`), which is the only correct place for this check.
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

    if is_resume:
        # M11E.1: the resume job was committed with the handoff; wake a worker
        # for it (no-op unless an external queue is configured). Looked up by
        # the command's idempotency key so an idempotent replay re-dispatches
        # a job whose first dispatch failed.
        resume_job = OperationsRepository(
            request.app.state.postgres_dsn, request.app.state.memory_config
        ).find_job_by_idempotency(actor.tenant_id, WorkflowJobType.RESUME_WORKFLOW, command.idempotency_key)

        if resume_job is not None:
            dispatch_if_queued(request.app.state.job_dispatcher, resume_job)

    return ApiEnvelope(
        request_id=uuid4(),
        status=result.status.value,
        data=response_payload,
        errors=tuple(),
        generated_at=interface_utc_now(),
    )
