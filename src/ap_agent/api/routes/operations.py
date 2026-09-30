"""Operations console routes (M11D Core).

    POST /api/v1/operations/submissions   enqueue an uploaded invoice (202)
    GET  /api/v1/operations/jobs          tenant-scoped job list
    GET  /api/v1/operations/jobs/{id}     job detail with its event timeline

The request handler validates, stores the artifact and enqueues a job and
returns; it never runs OCR or orchestration (that is the worker's job).
Tenant and actor come only from the authenticated-actor dependency (the
clearly-labelled prototype header adapter); the request body carries no
identity. Payment-, bank- and ERP-shaped fields are refused before anything
is stored.
"""

from __future__ import annotations

from math import ceil
from typing import Annotated, Optional
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, Header, Path, Query, Request, Response
from fastapi.concurrency import run_in_threadpool

from ap_agent.api.config import ApiConfig
from ap_agent.api.dependencies import AuthenticatedActor, get_api_config
from ap_agent.api.schemas import (
    ApiEnvelope,
    ApiErrorEnvelope,
    JobDetailResponse,
    JobEventResponse,
    JobListResponse,
    JobResponse,
    PaginationMeta,
    SubmissionResponse,
)
from ap_agent.artifacts.storage import ArtifactStore
from ap_agent.artifacts.upload_validation import reject_forbidden_form_fields
from ap_agent.exceptions import OperationsRequestRejectedError
from ap_agent.models.interface import interface_utc_now
from ap_agent.models.operations import JobSubmissionOutcome
from ap_agent.repositories.operations_repository import OperationsRepository
from ap_agent.services.operations_submission import require_read_permission, submit_upload

router = APIRouter(tags=["operations"])

_ERROR_RESPONSES: dict[int | str, dict] = {
    status_code: {"model": ApiErrorEnvelope, "description": description}
    for status_code, description in {
        401: "Missing or invalid authentication.",
        403: "Forbidden role, or operations are disabled.",
        404: "Unknown job.",
        409: "Idempotency key reused with different content.",
        413: "Request or file too large.",
        415: "Unsupported or inconsistent file type.",
        422: "Invalid upload, filename, field or idempotency key.",
        500: "Redacted internal error.",
        503: "Database unavailable.",
    }.items()
}


def get_operations_repository(request: Request) -> OperationsRepository:
    return OperationsRepository(request.app.state.postgres_dsn, request.app.state.memory_config)


def get_artifact_store(request: Request) -> ArtifactStore:
    store = getattr(request.app.state, "artifact_store", None)

    if store is None:
        raise OperationsRequestRejectedError("OPERATIONS_DISABLED", http_status=403)

    return store


def require_operations_enabled(api_config: ApiConfig = Depends(get_api_config)) -> None:
    if not api_config.enable_operations:
        raise OperationsRequestRejectedError("OPERATIONS_DISABLED", http_status=403)


def _envelope(status: str, data: dict) -> ApiEnvelope:
    return ApiEnvelope(
        request_id=uuid4(), status=status, data=data, errors=tuple(), generated_at=interface_utc_now()
    )


@router.post(
    "/api/v1/operations/submissions",
    status_code=202,
    response_model=ApiEnvelope[SubmissionResponse],
    responses=_ERROR_RESPONSES,
    dependencies=[Depends(require_operations_enabled)],
)
async def submit_invoice(
    request: Request,
    response: Response,
    actor: AuthenticatedActor,
    idempotency_key: Annotated[Optional[str], Header(alias="Idempotency-Key")] = None,
    api_config: ApiConfig = Depends(get_api_config),
    repository: OperationsRepository = Depends(get_operations_repository),
    store: ArtifactStore = Depends(get_artifact_store),
) -> ApiEnvelope[SubmissionResponse]:
    from ap_agent.services.operations_submission import require_submit_permission

    require_submit_permission(actor)

    form = await request.form(max_files=1, max_fields=1)

    try:
        reject_forbidden_form_fields(list(form.keys()))
        uploads = form.getlist("file")

        if len(uploads) != 1 or not hasattr(uploads[0], "read"):
            raise OperationsRequestRejectedError("FILE_REQUIRED")

        upload = uploads[0]

        result = await run_in_threadpool(
            submit_upload,
            repository=repository,
            store=store,
            limits=api_config.upload_limits,
            actor=actor,
            stream=upload.file,
            filename=upload.filename,
            declared_media_type=upload.content_type,
            idempotency_key=idempotency_key,
        )
    finally:
        await form.close()

    replay = result.outcome == JobSubmissionOutcome.IDEMPOTENT_REPLAY

    if replay:
        response.status_code = 200

    return _envelope(
        "IDEMPOTENT" if replay else "ACCEPTED",
        SubmissionResponse(job=JobResponse.from_domain(result.job), idempotent_replay=replay).model_dump(mode="json"),
    )


@router.get(
    "/api/v1/operations/jobs",
    response_model=ApiEnvelope[JobListResponse],
    responses=_ERROR_RESPONSES,
    dependencies=[Depends(require_operations_enabled)],
)
def list_jobs(
    actor: AuthenticatedActor,
    status: Annotated[Optional[list[str]], Query()] = None,
    job_type: Annotated[Optional[str], Query()] = None,
    review_case_id: Annotated[Optional[UUID], Query()] = None,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 25,
    repository: OperationsRepository = Depends(get_operations_repository),
) -> ApiEnvelope[JobListResponse]:
    require_read_permission(actor)

    allowed_statuses = {"QUEUED", "RUNNING", "SUCCEEDED", "REVIEW_REQUIRED", "FAILED"}

    if status and any(item not in allowed_statuses for item in status):
        raise OperationsRequestRejectedError("INVALID_STATUS_FILTER")

    if job_type is not None and job_type not in {"PROCESS_DOCUMENT", "RESUME_WORKFLOW"}:
        raise OperationsRequestRejectedError("INVALID_JOB_TYPE_FILTER")

    jobs, total = repository.list_jobs(
        actor.tenant_id, statuses=tuple(status) if status else None, job_type=job_type,
        review_id=review_case_id, limit=page_size, offset=(page - 1) * page_size,
    )

    return _envelope(
        "SUCCEEDED",
        JobListResponse(
            items=tuple(JobResponse.from_domain(job) for job in jobs),
            pagination=PaginationMeta(
                page=page, page_size=page_size, total_count=total, total_pages=max(1, ceil(total / page_size))
            ),
        ).model_dump(mode="json"),
    )


@router.get(
    "/api/v1/operations/jobs/{job_id}",
    response_model=ApiEnvelope[JobDetailResponse],
    responses=_ERROR_RESPONSES,
    dependencies=[Depends(require_operations_enabled)],
)
def get_job_detail(
    actor: AuthenticatedActor,
    job_id: UUID = Path(description="Job identifier"),
    repository: OperationsRepository = Depends(get_operations_repository),
) -> ApiEnvelope[JobDetailResponse]:
    require_read_permission(actor)

    job = repository.get_job(actor.tenant_id, job_id)

    if job is None:
        raise OperationsRequestRejectedError("JOB_NOT_FOUND", http_status=404)

    events = repository.list_events(actor.tenant_id, job_id)

    return _envelope(
        "SUCCEEDED",
        JobDetailResponse(
            job=JobResponse.from_domain(job), events=tuple(JobEventResponse.from_domain(event) for event in events)
        ).model_dump(mode="json"),
    )
