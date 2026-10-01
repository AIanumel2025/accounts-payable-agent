"""`GET /api/v1/session` (M11E): the authenticated caller's internal role and
tenant display name, resolved by the same dependency every other route uses.
The interface shell reads the role from here instead of from configuration."""

from __future__ import annotations

from uuid import uuid4

from fastapi import APIRouter, Request

from ap_agent.api.dependencies import AuthenticatedActor
from ap_agent.api.schemas import ApiEnvelope, SessionResponse
from ap_agent.models.interface import interface_utc_now

router = APIRouter(tags=["session"])


@router.get("/api/v1/session", response_model=ApiEnvelope[SessionResponse])
def get_session(request: Request, actor: AuthenticatedActor) -> ApiEnvelope[SessionResponse]:
    api_config = request.app.state.api_config
    repository = getattr(request.app.state, "identity_repository", None)
    display_name = repository.tenant_display_name(actor.tenant_id) if repository is not None else None

    return ApiEnvelope(
        request_id=uuid4(),
        status="SUCCEEDED",
        data=SessionResponse(
            role=actor.role.value, tenant_display_name=display_name, auth_mode=api_config.auth_mode.value
        ).model_dump(mode="json"),
        errors=tuple(),
        generated_at=interface_utc_now(),
    )
