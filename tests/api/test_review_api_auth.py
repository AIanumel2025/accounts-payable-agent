"""M10 API tests that need no PostgreSQL connection: authentication
failures, malformed requests, and OpenAPI/health/config behaviour (task
§16). Every route that actually reads/writes review data is covered
instead by `tests/api/test_review_api_postgres.py` (`requires_postgres`),
since those need real seeded rows.
"""

from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient

from ap_agent.api.app import create_app
from ap_agent.api.config import ApiConfig
from ap_agent.config.postgres import MemoryConfig
from ap_agent.models.interface import interface_utc_now

pytestmark = pytest.mark.unit

_FAKE_DSN = "postgresql://user:pass@localhost:5432/testdb"


@pytest.fixture()
def client() -> TestClient:
    app = create_app(dsn=_FAKE_DSN, memory_config=MemoryConfig(), api_config=ApiConfig())
    with TestClient(app, raise_server_exceptions=True) as test_client:
        yield test_client


def test_health_check_never_touches_the_database(client: TestClient):
    response = client.get("/health")

    assert response.status_code == 200
    assert response.json()["data"]["command_mode"] == "VALIDATION_ONLY"
    assert response.json()["data"]["payment_execution"] == "PROHIBITED"


def test_missing_authentication_headers_return_401(client: TestClient):
    response = client.get("/api/v1/dashboard")

    assert response.status_code == 401
    assert "TENANT_ID_MISSING" in response.json()["errors"]


def test_malformed_tenant_id_returns_401(client: TestClient):
    response = client.get(
        "/api/v1/dashboard",
        headers={"X-Tenant-ID": "not-a-uuid", "X-Actor-ID": "a", "X-Actor-Role": "AP_OPERATOR"},
    )

    assert response.status_code == 401
    assert "TENANT_ID_INVALID" in response.json()["errors"]


def test_unknown_role_returns_401(client: TestClient):
    response = client.get(
        "/api/v1/dashboard",
        headers={"X-Tenant-ID": str(uuid.uuid4()), "X-Actor-ID": "a", "X-Actor-Role": "NOT_A_ROLE"},
    )

    assert response.status_code == 401
    assert "ACTOR_ROLE_INVALID" in response.json()["errors"]


def test_malformed_authentication_timestamp_returns_401(client: TestClient):
    response = client.get(
        "/api/v1/dashboard",
        headers={
            "X-Tenant-ID": str(uuid.uuid4()),
            "X-Actor-ID": "a",
            "X-Actor-Role": "AP_OPERATOR",
            "X-Authenticated-At": "not-a-timestamp",
        },
    )

    assert response.status_code == 401
    assert "AUTHENTICATION_TIMESTAMP_INVALID" in response.json()["errors"]


def test_payment_action_is_rejected_at_the_schema_level(client: TestClient):
    body = {
        "command_id": str(uuid.uuid4()),
        "idempotency_key": "fake-payment-attempt-1",
        "action": "EXECUTE_PAYMENT",
        "observed_review_revision": 1,
        "observed_workflow_revision": 1,
        "requested_at": interface_utc_now().isoformat(),
    }

    response = client.post(
        f"/api/v1/review-cases/{uuid.uuid4()}/commands",
        headers={"X-Tenant-ID": str(uuid.uuid4()), "X-Actor-ID": "a", "X-Actor-Role": "AP_OPERATOR"},
        json=body,
    )

    assert response.status_code == 422


def test_malformed_command_body_returns_422(client: TestClient):
    response = client.post(
        f"/api/v1/review-cases/{uuid.uuid4()}/commands",
        headers={"X-Tenant-ID": str(uuid.uuid4()), "X-Actor-ID": "a", "X-Actor-Role": "AP_OPERATOR"},
        json={"not": "a valid command"},
    )

    assert response.status_code == 422


def test_openapi_schema_lists_every_required_endpoint(client: TestClient):
    response = client.get("/openapi.json")

    assert response.status_code == 200
    paths = set(response.json()["paths"])
    assert {
        "/health",
        "/api/v1/dashboard",
        "/api/v1/review-cases",
        "/api/v1/review-cases/{review_case_id}",
        "/api/v1/review-cases/{review_case_id}/commands",
    }.issubset(paths)


def test_docs_and_redoc_are_served(client: TestClient):
    assert client.get("/docs").status_code == 200
    assert client.get("/redoc").status_code == 200


def test_error_envelopes_are_deterministic_across_repeated_requests(client: TestClient):
    first = client.get("/api/v1/dashboard")
    second = client.get("/api/v1/dashboard")

    assert first.status_code == second.status_code == 401
    assert set(first.json().keys()) == set(second.json().keys()) == {"request_id", "errors", "generated_at"}
    assert first.json()["errors"] == second.json()["errors"]
    # request_id/generated_at are per-request, but the *shape* is stable.
    assert first.json()["request_id"] != second.json()["request_id"]


def test_write_mode_is_disabled_by_default_for_a_fresh_app(client: TestClient):
    # health's command_mode already asserts this; this test pins the
    # underlying config object directly (task §15: "write mode disabled
    # by default").
    from ap_agent.api.config import ApiConfig

    assert ApiConfig().enable_review_command_writes is False
