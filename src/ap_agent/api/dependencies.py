"""FastAPI dependencies: prototype header authentication and per-request
resources (repository, interface/API configuration).

Source: notebook cell 108's `authenticated_interface_actor`/
`parse_authenticated_at` (task §10: "Preserve the notebook's prototype
header authentication only as a clearly labelled development/test
adapter"). This module is exactly that label:

    **THIS IS A DEVELOPMENT/TEST AUTHENTICATION ADAPTER.** It trusts four
    plain HTTP headers (`X-Tenant-ID`, `X-Actor-ID`, `X-Actor-Role`,
    `X-Authenticated-At`) with no signature, no token verification and no
    identity-provider round trip. Production deployment requires a
    trusted identity provider issuing verified, signed tokens (OIDC/JWT);
    `authenticated_interface_actor` is injected as an ordinary FastAPI
    dependency specifically so it can be swapped for a JWT-verifying
    dependency later without touching a single route (task §10).

Every dependency here reads state that `ap_agent.api.app.create_app`
attached to `request.app.state` during its lifespan -- nothing connects to
PostgreSQL or reads the environment at import time.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Annotated
from uuid import UUID

from fastapi import Depends, Header, Request

from ap_agent.api.config import ApiConfig
from ap_agent.exceptions import ReviewAuthenticationError, TenantAccessDeniedError
from ap_agent.models.interface import InterfaceActor, InterfaceConfig, InterfaceRole, interface_utc_now
from ap_agent.repositories.review_repository import ReviewRepository

__all__ = [
    "AUTHENTICATION_MAX_AGE",
    "parse_authenticated_at",
    "authenticated_interface_actor",
    "AuthenticatedActor",
    "get_review_repository",
    "get_interface_config",
    "get_api_config",
]


# The notebook's own acceptance cell never exercises an expiry window (it
# authenticates and calls the API within the same process, microseconds
# apart); task §10 requires rejecting an "expired ... authentication
# timestamp according to the notebook policy" all the same, so this
# adapter enforces a generous, explicit window instead of accepting any
# timestamp forever.
AUTHENTICATION_MAX_AGE = timedelta(hours=12)


def parse_authenticated_at(value: str | None) -> datetime:
    if value is None:
        return interface_utc_now()

    try:
        parsed_value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ReviewAuthenticationError(
            "Authentication timestamp is not valid ISO-8601.",
            details={"errors": ["AUTHENTICATION_TIMESTAMP_INVALID"]},
        ) from exc

    if parsed_value.tzinfo is None:
        raise ReviewAuthenticationError(
            "Authentication timestamp is not timezone-aware.",
            details={"errors": ["AUTHENTICATION_TIMESTAMP_INVALID"]},
        )

    now = interface_utc_now()

    if parsed_value > now + timedelta(minutes=1) or now - parsed_value > AUTHENTICATION_MAX_AGE:
        raise ReviewAuthenticationError(
            "Authentication timestamp is expired or in the future.",
            details={"errors": ["AUTHENTICATION_TIMESTAMP_EXPIRED"]},
        )

    return parsed_value


def authenticated_interface_actor(
    request: Request,
    x_tenant_id: Annotated[str | None, Header(alias="X-Tenant-ID")] = None,
    x_actor_id: Annotated[str | None, Header(alias="X-Actor-ID")] = None,
    x_actor_role: Annotated[str | None, Header(alias="X-Actor-Role")] = None,
    x_authenticated_at: Annotated[str | None, Header(alias="X-Authenticated-At")] = None,
) -> InterfaceActor:
    # Headers are declared Optional (rather than FastAPI's usual required
    # `Header(...)`) so a *missing* header fails through this function's
    # own `ReviewAuthenticationError` -> HTTP 401 (task §12), not
    # FastAPI's generic request-validation 422 for a missing parameter.
    if x_tenant_id is None:
        raise ReviewAuthenticationError("X-Tenant-ID is required.", details={"errors": ["TENANT_ID_MISSING"]})

    if x_actor_id is None:
        raise ReviewAuthenticationError("X-Actor-ID is required.", details={"errors": ["ACTOR_ID_MISSING"]})

    if x_actor_role is None:
        raise ReviewAuthenticationError("X-Actor-Role is required.", details={"errors": ["ACTOR_ROLE_MISSING"]})

    try:
        tenant_id = UUID(x_tenant_id)
    except ValueError as exc:
        raise ReviewAuthenticationError(
            "X-Tenant-ID is not a valid UUID.", details={"errors": ["TENANT_ID_INVALID"]}
        ) from exc

    api_config: ApiConfig = request.app.state.api_config

    if api_config.allowed_tenant_ids and tenant_id not in api_config.allowed_tenant_ids:
        raise TenantAccessDeniedError(
            "This tenant is not served by this deployment.", details={"errors": ["TENANT_ACCESS_DENIED"]}
        )

    if not x_actor_id.strip():
        raise ReviewAuthenticationError("X-Actor-ID is required.", details={"errors": ["ACTOR_ID_MISSING"]})

    try:
        actor_role = InterfaceRole(x_actor_role)
    except ValueError as exc:
        raise ReviewAuthenticationError(
            "X-Actor-Role is not a recognised role.", details={"errors": ["ACTOR_ROLE_INVALID"]}
        ) from exc

    return InterfaceActor(
        actor_id=x_actor_id.strip(),
        tenant_id=tenant_id,
        role=actor_role,
        authenticated_at=parse_authenticated_at(x_authenticated_at),
    )


AuthenticatedActor = Annotated[InterfaceActor, Depends(authenticated_interface_actor)]


def get_review_repository(request: Request) -> ReviewRepository:
    return ReviewRepository(request.app.state.postgres_dsn, request.app.state.memory_config)


def get_interface_config(request: Request) -> InterfaceConfig:
    return request.app.state.interface_config


def get_api_config(request: Request) -> ApiConfig:
    return request.app.state.api_config
