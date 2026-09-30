"""`GET /health` -- notebook cell 108's `health_check`, generalized to
report the actual configured write mode instead of a hardcoded constant.
Never touches PostgreSQL (task §13: "no eager ... loading at API
startup"; a health check that pings the database would also leak whether
a 503 is the health check's fault or a downstream route's) and never
exposes a credential (task §12: "health checks that do not expose
credentials").
"""

from __future__ import annotations

from fastapi import APIRouter, Request

from ap_agent.api.schemas import ApiEnvelope, HealthResponse
from ap_agent.models.interface import interface_utc_now

router = APIRouter(tags=["system"])


@router.get("/health", response_model=ApiEnvelope[HealthResponse])
def health_check(request: Request) -> ApiEnvelope[HealthResponse]:
    from uuid import uuid4

    api_config = request.app.state.api_config

    return ApiEnvelope(
        request_id=uuid4(),
        status="SUCCEEDED",
        data=HealthResponse(
            service="ap-agent-review-api",
            api_version=api_config.api_version,
            command_mode=("COMMIT" if api_config.enable_review_command_writes else "VALIDATION_ONLY"),
            payment_execution="PROHIBITED",
            operations_mode=("ENABLED" if api_config.enable_operations else "DISABLED"),
        ).model_dump(mode="json"),
        errors=tuple(),
        generated_at=interface_utc_now(),
    )
