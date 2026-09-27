"""M10 unit tests: `ap_agent.api.errors.command_http_error` (notebook cell
108's `command_http_error`, verbatim mapping) and the exception-handler
wiring end-to-end through a minimal app (task §12)."""

from __future__ import annotations

from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette import status

from ap_agent.api.errors import command_http_error, register_exception_handlers
from ap_agent.exceptions import (
    ReviewCaseNotFoundError,
    ReviewCommandRejectedError,
    ReviewIntegrityError,
    TenantAccessDeniedError,
)

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    "errors,expected_status",
    [
        (("ACTION_NOT_PERMITTED",), status.HTTP_403_FORBIDDEN),
        (("CROSS_TENANT_COMMAND",), status.HTTP_403_FORBIDDEN),
        (("STALE_REVIEW_REVISION",), status.HTTP_409_CONFLICT),
        (("STALE_WORKFLOW_REVISION",), status.HTTP_409_CONFLICT),
        (("IDEMPOTENCY_KEY_CONTENT_CONFLICT",), status.HTTP_409_CONFLICT),
        (("CORRECTION_EVIDENCE_REQUIRED",), status.HTTP_422_UNPROCESSABLE_ENTITY),
        (("CORRECTIONS_REQUIRED",), status.HTTP_422_UNPROCESSABLE_ENTITY),
    ],
)
def test_command_http_error_mapping(errors, expected_status):
    assert command_http_error(errors) == expected_status


def _test_app() -> FastAPI:
    app = FastAPI()
    register_exception_handlers(app)

    @app.get("/not-found")
    def _not_found():
        raise ReviewCaseNotFoundError("missing")

    @app.get("/tenant-denied")
    def _tenant_denied():
        raise TenantAccessDeniedError("denied", details={"errors": ["TENANT_ACCESS_DENIED"]})

    @app.get("/rejected")
    def _rejected():
        raise ReviewCommandRejectedError(("STALE_REVIEW_REVISION",))

    @app.get("/integrity")
    def _integrity():
        raise ReviewIntegrityError("DSN=postgresql://secret should never leak")

    @app.get("/unexpected")
    def _unexpected():
        raise RuntimeError("boom")

    return app


def test_not_found_maps_to_404():
    with TestClient(_test_app(), raise_server_exceptions=False) as client:
        response = client.get("/not-found")
        assert response.status_code == 404


def test_tenant_denied_maps_to_403():
    with TestClient(_test_app(), raise_server_exceptions=False) as client:
        response = client.get("/tenant-denied")
        assert response.status_code == 403
        assert "TENANT_ACCESS_DENIED" in response.json()["errors"]


def test_command_rejected_maps_through_command_http_error():
    with TestClient(_test_app(), raise_server_exceptions=False) as client:
        response = client.get("/rejected")
        assert response.status_code == 409


def test_integrity_error_never_leaks_its_reason_to_the_client():
    with TestClient(_test_app(), raise_server_exceptions=False) as client:
        response = client.get("/integrity")
        assert response.status_code == 500
        body = response.text
        assert "secret" not in body
        assert "postgresql://" not in body


def test_unexpected_error_is_redacted_500():
    with TestClient(_test_app(), raise_server_exceptions=False) as client:
        response = client.get("/unexpected")
        assert response.status_code == 500
        assert "boom" not in response.text
        assert "Traceback" not in response.text
