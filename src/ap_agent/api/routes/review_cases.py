"""`GET /api/v1/review-cases` and `GET /api/v1/review-cases/{review_case_id}`
-- notebook cell 108's `get_review_cases`/`get_review_case_detail`, backed
by `ap_agent.services.review_queries`. The list endpoint adds real
pagination and the optional status/assignment/priority/batch filters task
§6 requires; the notebook's own queue call has none of them (it always
returned every one of the fixed four-document "current run"'s active
cases in one page).
"""

from __future__ import annotations

from typing import Optional
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, Path, Query

from ap_agent.api.dependencies import AuthenticatedActor, get_interface_config, get_review_repository
from ap_agent.api.schemas import ApiEnvelope, InvoiceDetailResponse, ReviewQueuePageResponse
from ap_agent.models.interface import InterfaceConfig, ReviewPriority, interface_utc_now
from ap_agent.repositories.review_repository import ReviewRepository
from ap_agent.services.review_queries import get_invoice_detail, list_review_queue

router = APIRouter(tags=["review-cases"])


@router.get("/api/v1/review-cases", response_model=ApiEnvelope[ReviewQueuePageResponse])
def get_review_cases(
    actor: AuthenticatedActor,
    repository: ReviewRepository = Depends(get_review_repository),
    interface_config: InterfaceConfig = Depends(get_interface_config),
    page: int = Query(default=1, ge=1),
    page_size: Optional[int] = Query(default=None, ge=1),
    status: Optional[str] = Query(default=None, description="ReviewCaseStatus value"),
    assigned_to: Optional[str] = Query(default=None),
    priority: Optional[ReviewPriority] = Query(default=None),
    batch_id: Optional[UUID] = Query(default=None),
) -> ApiEnvelope[ReviewQueuePageResponse]:
    result_page = list_review_queue(
        repository,
        actor.tenant_id,
        interface_config,
        page=page,
        page_size=page_size,
        status_filter=status,
        assigned_filter=assigned_to,
        priority_filter=priority,
        batch_id_filter=batch_id,
    )

    return ApiEnvelope(
        request_id=uuid4(),
        status="SUCCEEDED",
        data=ReviewQueuePageResponse.from_domain(result_page).model_dump(mode="json"),
        errors=tuple(),
        generated_at=interface_utc_now(),
    )


@router.get("/api/v1/review-cases/{review_case_id}", response_model=ApiEnvelope[InvoiceDetailResponse])
def get_review_case_detail(
    actor: AuthenticatedActor,
    review_case_id: UUID = Path(description="Human-review case identifier"),
    repository: ReviewRepository = Depends(get_review_repository),
) -> ApiEnvelope[InvoiceDetailResponse]:
    detail = get_invoice_detail(repository, actor.tenant_id, review_case_id)

    return ApiEnvelope(
        request_id=uuid4(),
        status="SUCCEEDED",
        data=InvoiceDetailResponse.from_domain(detail).model_dump(mode="json"),
        errors=tuple(),
        generated_at=interface_utc_now(),
    )
