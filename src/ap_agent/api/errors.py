"""Central exception-to-HTTP mapping for the Phase 9 review API.

Source: notebook cell 108's inline `HTTPException` raises and its
`command_http_error` helper, generalized into FastAPI exception handlers
(task §12: "Use central exception handlers. Do not leak implementation
details."). Every handler returns the same deterministic error envelope
shape (task §9/§13) and never includes a stack trace, SQL text, a DSN, a
local filesystem path or any other implementation detail (task §21).
"""

from __future__ import annotations

import logging
from typing import Any
from uuid import uuid4

from fastapi import Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from ap_agent.exceptions import (
    OperationsRequestRejectedError,
    PostgresConfigurationError,
    ReviewAuthenticationError,
    ReviewCaseNotFoundError,
    ReviewCommandRejectedError,
    ReviewIntegrityError,
    TenantAccessDeniedError,
    TenantContextError,
)
from ap_agent.models.interface import interface_utc_now

__all__ = ["command_http_error", "register_exception_handlers"]


_LOGGER = logging.getLogger("ap_agent.api")


def command_http_error(errors: tuple[str, ...]) -> int:
    """Extends notebook cell 108's `command_http_error` (its
    ACTION_NOT_PERMITTED/CROSS_TENANT_COMMAND -> 403 and stale-revision/
    idempotency-conflict -> 409 branches are ported verbatim) with one more
    409 branch task §12 requires and the notebook's own mapping does not
    cover: "claim-ownership conflict: 409" -- a different reviewer than the
    one who holds the claim attempting a decision/resume on it
    (`CASE_ASSIGNED_TO_DIFFERENT_REVIEWER`/`DECISION_ACTOR_MISMATCH`) is a
    conflict over who may act, not a generic validation failure, and must
    not fall through to this function's 422 default. Ordered after the
    stale-revision checks so a request that is stale *and* ownership-
    mismatched still reports the same code the equivalent stale-only
    request would."""

    if "ACTION_NOT_PERMITTED" in errors:
        return status.HTTP_403_FORBIDDEN

    if "CROSS_TENANT_COMMAND" in errors:
        return status.HTTP_403_FORBIDDEN

    if any(
        error in {"STALE_REVIEW_REVISION", "STALE_WORKFLOW_REVISION", "IDEMPOTENCY_KEY_CONTENT_CONFLICT"}
        for error in errors
    ):
        return status.HTTP_409_CONFLICT

    # M11C task §17/§18: a claim that finds the case already claimed/no
    # longer open, or a release/decision on a case that is not claimed, is
    # an ownership/state conflict (409), not a malformed request (422). These
    # codes previously fell through to the 422 default.
    if any(
        error
        in {
            "CASE_ASSIGNED_TO_DIFFERENT_REVIEWER",
            "DECISION_ACTOR_MISMATCH",
            "CASE_NOT_OPEN",
            "CASE_ALREADY_ASSIGNED",
            "CASE_NOT_CLAIMED",
            "WORKFLOW_NOT_AWAITING_RESUME",
        }
        for error in errors
    ):
        return status.HTTP_409_CONFLICT

    return status.HTTP_422_UNPROCESSABLE_ENTITY


def _error_envelope(request_id: str, errors: tuple[str, ...]) -> dict[str, Any]:
    return {
        "request_id": request_id,
        "errors": list(errors),
        "generated_at": interface_utc_now().isoformat(),
    }


def _validation_error_codes(exc: RequestValidationError) -> tuple[str, ...]:
    """Stable, input-free codes for a request-validation failure (M11C
    task §18). FastAPI's default 422 body echoes the offending input value
    back; this reports only which field failed, never its content."""

    codes: list[str] = ["REQUEST_VALIDATION_FAILED"]

    for error in exc.errors():
        location = tuple(str(part) for part in error.get("loc", ()) if part not in {"body", "query", "path"})

        if location[:1] == ("action",):
            code = "REVIEW_ACTION_NOT_SUPPORTED"
        elif location:
            code = "INVALID_FIELD:" + ".".join(location)
        else:
            code = "INVALID_REQUEST"

        if code not in codes:
            codes.append(code)

    return tuple(codes)


def register_exception_handlers(app) -> None:  # `app: FastAPI`, untyped to avoid an import cycle at module load
    @app.exception_handler(RequestValidationError)
    async def _handle_request_validation(request: Request, exc: RequestValidationError) -> JSONResponse:
        request_id = str(uuid4())
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            content=_error_envelope(request_id, _validation_error_codes(exc)),
        )

    @app.exception_handler(OperationsRequestRejectedError)
    async def _handle_operations_rejected(request: Request, exc: OperationsRequestRejectedError) -> JSONResponse:
        # Stable, input-free code only (M11D Core): never the filename,
        # bytes, path or key that caused it.
        return JSONResponse(
            status_code=exc.http_status,
            content=_error_envelope(str(uuid4()), (exc.code,)),
        )

    @app.exception_handler(ReviewCaseNotFoundError)
    async def _handle_not_found(request: Request, exc: ReviewCaseNotFoundError) -> JSONResponse:
        request_id = str(uuid4())
        return JSONResponse(
            status_code=status.HTTP_404_NOT_FOUND,
            content=_error_envelope(request_id, exc.details.get("errors", ("REVIEW_CASE_NOT_FOUND",))),
        )

    @app.exception_handler(TenantAccessDeniedError)
    async def _handle_tenant_denied(request: Request, exc: TenantAccessDeniedError) -> JSONResponse:
        request_id = str(uuid4())
        return JSONResponse(
            status_code=status.HTTP_403_FORBIDDEN,
            content=_error_envelope(request_id, exc.details.get("errors", ("TENANT_ACCESS_DENIED",))),
        )

    @app.exception_handler(ReviewAuthenticationError)
    async def _handle_authentication_error(request: Request, exc: ReviewAuthenticationError) -> JSONResponse:
        request_id = str(uuid4())
        return JSONResponse(
            status_code=status.HTTP_401_UNAUTHORIZED,
            content=_error_envelope(request_id, exc.details.get("errors", ("AUTHENTICATION_FAILED",))),
        )

    @app.exception_handler(ReviewCommandRejectedError)
    async def _handle_command_rejected(request: Request, exc: ReviewCommandRejectedError) -> JSONResponse:
        request_id = str(uuid4())
        return JSONResponse(
            status_code=command_http_error(exc.errors),
            content=_error_envelope(request_id, exc.errors),
        )

    @app.exception_handler(ReviewIntegrityError)
    async def _handle_integrity_error(request: Request, exc: ReviewIntegrityError) -> JSONResponse:
        request_id = str(uuid4())
        # Never echo `exc.reason`/`exc.details` to the client (task §12/
        # §21: no implementation detail, SQL or payload content in a
        # response) -- only server-side logging gets the real reason.
        _LOGGER.error("Review-integrity failure (request_id=%s): %s", request_id, exc.reason)
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content=_error_envelope(request_id, ("INTERNAL_ERROR",)),
        )

    @app.exception_handler(PostgresConfigurationError)
    @app.exception_handler(TenantContextError)
    async def _handle_database_unavailable(request: Request, exc: Exception) -> JSONResponse:
        request_id = str(uuid4())
        _LOGGER.error("Database dependency unavailable (request_id=%s): %s", request_id, type(exc).__name__)
        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            content=_error_envelope(request_id, ("DATABASE_UNAVAILABLE",)),
        )

    @app.exception_handler(Exception)
    async def _handle_unexpected_error(request: Request, exc: Exception) -> JSONResponse:
        request_id = str(uuid4())
        _LOGGER.exception("Unhandled error (request_id=%s)", request_id)

        try:
            import psycopg

            if isinstance(exc, psycopg.Error):
                return JSONResponse(
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                    content=_error_envelope(request_id, ("DATABASE_UNAVAILABLE",)),
                )
        except ImportError:  # pragma: no cover - psycopg always installed for this app
            pass

        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content=_error_envelope(request_id, ("INTERNAL_ERROR",)),
        )
