"""`GET /api/v1/dashboard` -- notebook cell 108's `get_dashboard`, backed by
`ap_agent.services.review_queries.get_dashboard`. Supports the optional
`batch_id` filter task §6 requires; the notebook's own dashboard call has
no such filter (it always scoped to the fixed four-document "current
run").
"""

from __future__ import annotations

from typing import Optional
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, Query

from ap_agent.api.dependencies import AuthenticatedActor, get_review_repository
from ap_agent.api.schemas import ApiEnvelope, DashboardResponse
from ap_agent.models.interface import interface_utc_now
from ap_agent.repositories.review_repository import ReviewRepository

router = APIRouter(tags=["dashboard"])


@router.get("/api/v1/dashboard", response_model=ApiEnvelope)
def get_dashboard(
    actor: AuthenticatedActor,
    repository: ReviewRepository = Depends(get_review_repository),
    batch_id: Optional[UUID] = Query(default=None),
) -> ApiEnvelope:
    from ap_agent.services.review_queries import get_dashboard as get_dashboard_record

    dashboard = get_dashboard_record(repository, actor.tenant_id, batch_id=batch_id)

    return ApiEnvelope(
        request_id=uuid4(),
        status="SUCCEEDED",
        data=DashboardResponse.from_domain(dashboard).model_dump(mode="json"),
        errors=tuple(),
        generated_at=interface_utc_now(),
    )
