"""`GET /health` -- notebook cell 108's `health_check`, generalized to
report the actual configured write mode instead of a hardcoded constant.
Never touches PostgreSQL (task §13: "no eager ... loading at API
startup"; a health check that pings the database would also leak whether
a 503 is the health check's fault or a downstream route's) and never
exposes a credential (task §12: "health checks that do not expose
credentials").
"""

from __future__ import annotations

import logging
from dataclasses import replace

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from ap_agent.api.schemas import ApiEnvelope, HealthResponse
from ap_agent.models.interface import interface_utc_now

router = APIRouter(tags=["system"])

_LOGGER = logging.getLogger("ap_agent.api")
_READINESS_CONNECT_TIMEOUT_SECONDS = 3


@router.get("/health/live", include_in_schema=False)
def liveness() -> dict[str, str]:
    """Process liveness only: no dependency is touched."""

    return {"status": "alive"}


@router.get("/health/ready", include_in_schema=False)
def readiness(request: Request) -> JSONResponse:
    """Readiness: configuration is internally consistent (validated at
    startup, re-checked here for the pieces this process holds) and
    PostgreSQL answers `SELECT 1` as the runtime role. Reports check names
    and `ok`/`failed` only -- never a DSN, hostname or error text."""

    from ap_agent.config.deployment import AuthMode

    state = request.app.state
    api_config = state.api_config
    checks: dict[str, str] = {}

    configuration_ok = True

    if api_config.auth_mode is AuthMode.CLERK_JWT and getattr(state, "clerk_verifier", None) is None:
        configuration_ok = False

    if api_config.enable_operations and getattr(state, "artifact_store", None) is None:
        configuration_ok = False

    checks["configuration"] = "ok" if configuration_ok else "failed"

    try:
        from ap_agent.db.connection import open_connection

        probe_config = replace(state.memory_config, connect_timeout_seconds=_READINESS_CONNECT_TIMEOUT_SECONDS)

        with open_connection(state.postgres_dsn, probe_config) as connection:
            with connection.cursor() as cursor:
                cursor.execute("SELECT 1;")
                cursor.fetchone()

        checks["database"] = "ok"
    except Exception as error:  # noqa: BLE001 - reported as a failed check, never echoed
        _LOGGER.error("Readiness database check failed (%s).", type(error).__name__)
        checks["database"] = "failed"

    ready = all(value == "ok" for value in checks.values())

    return JSONResponse(
        status_code=200 if ready else 503,
        content={"status": "ready" if ready else "not_ready", "checks": checks},
    )


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
