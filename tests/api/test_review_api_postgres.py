"""M10 `requires_postgres` API acceptance suite: every endpoint driven
through FastAPI `TestClient` against the real, isolated `ap_agent_m8_test`
database (task §16).

DSN sourcing, the fail-closed database-identity gate, and the dedicated
least-privilege runtime role follow exactly the same pattern as
`tests/integration/test_memory_postgres_integration.py` (see that module's
docstring for the full safety rationale) -- this suite reads only
`AP_AGENT_TEST_POSTGRES_DSN`, never `AP_AGENT_POSTGRES_DSN`, and never
prints the DSN or any credential.
"""

from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient

from ap_agent.api.config import ApiConfig
from ap_agent.models.interface import InterfaceRole, interface_utc_now

pytestmark = [pytest.mark.integration, pytest.mark.requires_postgres]


def _client(runtime_dsn, local_config, *, enable_writes: bool, allowed_tenant_ids: tuple = ()) -> TestClient:
    from ap_agent.api.app import create_app

    app = create_app(
        dsn=runtime_dsn,
        memory_config=local_config,
        api_config=ApiConfig(enable_review_command_writes=enable_writes, allowed_tenant_ids=allowed_tenant_ids),
    )
    return TestClient(app, raise_server_exceptions=True)


def _headers(tenant_id: uuid.UUID, *, actor_id: str, role: InterfaceRole) -> dict[str, str]:
    return {
        "X-Tenant-ID": str(tenant_id),
        "X-Actor-ID": actor_id,
        "X-Actor-Role": role.value,
        "X-Authenticated-At": interface_utc_now().isoformat(),
    }


# ==============================================================
# Read endpoints
# ==============================================================


def test_health_returns_200(runtime_dsn, local_config):
    with _client(runtime_dsn, local_config, enable_writes=False) as client:
        response = client.get("/health")
        assert response.status_code == 200
        assert response.json()["data"]["payment_execution"] == "PROHIBITED"


def test_dashboard_reflects_seeded_case(runtime_dsn, local_config, tenant_id, seeded_case):
    with _client(runtime_dsn, local_config, enable_writes=False) as client:
        response = client.get(
            "/api/v1/dashboard", headers=_headers(tenant_id, actor_id="auditor-1", role=InterfaceRole.READ_ONLY_AUDITOR)
        )

        assert response.status_code == 200
        data = response.json()["data"]
        assert data["total_invoices"] == 1
        assert data["review_required_invoices"] == 1
        assert data["open_review_cases"] == 1
        assert data["unassigned_review_cases"] == 1


def test_review_queue_returns_the_seeded_case(runtime_dsn, local_config, tenant_id, seeded_case):
    with _client(runtime_dsn, local_config, enable_writes=False) as client:
        response = client.get(
            "/api/v1/review-cases", headers=_headers(tenant_id, actor_id="auditor-1", role=InterfaceRole.READ_ONLY_AUDITOR)
        )

        assert response.status_code == 200
        data = response.json()["data"]
        assert data["pagination"]["total_count"] == 1
        assert data["items"][0]["review_case_id"] == str(seeded_case.review_case_id)


def test_review_case_detail_returns_projected_fields(runtime_dsn, local_config, tenant_id, seeded_case):
    with _client(runtime_dsn, local_config, enable_writes=False) as client:
        response = client.get(
            f"/api/v1/review-cases/{seeded_case.review_case_id}",
            headers=_headers(tenant_id, actor_id="auditor-1", role=InterfaceRole.READ_ONLY_AUDITOR),
        )

        assert response.status_code == 200
        data = response.json()["data"]
        assert data["source_name"] == "seeded-review-case.pdf"
        assert len(data["fields"]) == 2
        assert len(data["financial_checks"]) == 1
        assert len(data["line_matches"]) == 1
        assert data["original_document_uri"] is None


def test_unknown_review_case_returns_404(runtime_dsn, local_config, tenant_id):
    with _client(runtime_dsn, local_config, enable_writes=False) as client:
        response = client.get(
            f"/api/v1/review-cases/{uuid.uuid4()}",
            headers=_headers(tenant_id, actor_id="auditor-1", role=InterfaceRole.READ_ONLY_AUDITOR),
        )
        assert response.status_code == 404


def test_cross_tenant_read_is_denied_for_a_restricted_deployment(runtime_dsn, local_config, tenant_id, seeded_case):
    with _client(runtime_dsn, local_config, enable_writes=False, allowed_tenant_ids=(tenant_id,)) as client:
        other_tenant_headers = _headers(uuid.uuid4(), actor_id="auditor-1", role=InterfaceRole.READ_ONLY_AUDITOR)
        response = client.get("/api/v1/dashboard", headers=other_tenant_headers)
        assert response.status_code == 403


def test_openapi_schema_generation_succeeds(runtime_dsn, local_config):
    with _client(runtime_dsn, local_config, enable_writes=False) as client:
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


# ==============================================================
# Commands: validation-only mode
# ==============================================================


def _claim_request(seeded_case, *, idempotency_key: str = "m10-api-claim-v1") -> dict:
    return {
        "command_id": str(uuid.uuid4()),
        "idempotency_key": idempotency_key,
        "action": "CLAIM",
        "disposition": None,
        "observed_review_revision": 1,
        "observed_workflow_revision": 1,
        "reason_codes": [],
        "notes": None,
        "corrections": [],
        "requested_at": interface_utc_now().isoformat(),
    }


def test_valid_command_is_validated_only_by_default(runtime_dsn, local_config, tenant_id, seeded_case):
    with _client(runtime_dsn, local_config, enable_writes=False) as client:
        response = client.post(
            f"/api/v1/review-cases/{seeded_case.review_case_id}/commands",
            headers=_headers(tenant_id, actor_id="ap-operator-1", role=InterfaceRole.AP_OPERATOR),
            json=_claim_request(seeded_case),
        )

        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "VALIDATED"
        assert body["data"]["database_mutation"] is False


def test_stale_revision_command_returns_409(runtime_dsn, local_config, tenant_id, seeded_case):
    with _client(runtime_dsn, local_config, enable_writes=False) as client:
        stale_request = _claim_request(seeded_case, idempotency_key="m10-api-stale-v1")
        stale_request["observed_review_revision"] = 99

        response = client.post(
            f"/api/v1/review-cases/{seeded_case.review_case_id}/commands",
            headers=_headers(tenant_id, actor_id="ap-operator-1", role=InterfaceRole.AP_OPERATOR),
            json=stale_request,
        )

        assert response.status_code == 409
        assert "STALE_REVIEW_REVISION" in response.json()["errors"]


def test_auditor_mutation_attempt_returns_403(runtime_dsn, local_config, tenant_id, seeded_case):
    with _client(runtime_dsn, local_config, enable_writes=False) as client:
        response = client.post(
            f"/api/v1/review-cases/{seeded_case.review_case_id}/commands",
            headers=_headers(tenant_id, actor_id="auditor-1", role=InterfaceRole.READ_ONLY_AUDITOR),
            json=_claim_request(seeded_case, idempotency_key="m10-api-auditor-v1"),
        )

        assert response.status_code == 403
        assert "ACTION_NOT_PERMITTED" in response.json()["errors"]


def test_payment_command_is_rejected_with_422(runtime_dsn, local_config, tenant_id, seeded_case):
    with _client(runtime_dsn, local_config, enable_writes=False) as client:
        payment_request = _claim_request(seeded_case, idempotency_key="m10-api-payment-v1")
        payment_request["action"] = "EXECUTE_PAYMENT"

        response = client.post(
            f"/api/v1/review-cases/{seeded_case.review_case_id}/commands",
            headers=_headers(tenant_id, actor_id="ap-operator-1", role=InterfaceRole.AP_OPERATOR),
            json=payment_request,
        )

        assert response.status_code == 422


def test_correction_without_evidence_is_rejected(runtime_dsn, local_config, tenant_id, seeded_case):
    with _client(runtime_dsn, local_config, enable_writes=False) as client:
        request_body = _claim_request(seeded_case, idempotency_key="m10-api-correction-v1")
        request_body["action"] = "CORRECT"
        request_body["disposition"] = "CORRECTED"
        request_body["reason_codes"] = ["PURCHASE_ORDER_CORRECTED"]
        request_body["notes"] = "PO reviewed."
        request_body["corrections"] = [
            {
                "field_name": "PURCHASE_ORDER_NUMBER",
                "line_number": None,
                "previous_value": "99",
                "corrected_value": "99A",
                "reason": "Reviewer verified.",
                "evidence_reference_ids": [],
            }
        ]

        response = client.post(
            f"/api/v1/review-cases/{seeded_case.review_case_id}/commands",
            headers=_headers(tenant_id, actor_id="ap-reviewer-1", role=InterfaceRole.AP_REVIEWER),
            json=request_body,
        )

        assert response.status_code == 422
        assert "CORRECTION_EVIDENCE_REQUIRED" in response.json()["errors"]


# ==============================================================
# Commands: write-enabled mode
# ==============================================================


def test_write_enabled_claim_then_release_round_trip(runtime_dsn, local_config, tenant_id, seeded_case):
    with _client(runtime_dsn, local_config, enable_writes=True) as client:
        claim_response = client.post(
            f"/api/v1/review-cases/{seeded_case.review_case_id}/commands",
            headers=_headers(tenant_id, actor_id="ap-operator-1", role=InterfaceRole.AP_OPERATOR),
            json=_claim_request(seeded_case, idempotency_key="m10-api-write-claim-v1"),
        )

        assert claim_response.status_code == 200
        assert claim_response.json()["status"] == "ACCEPTED"
        assert claim_response.json()["data"]["resulting_case_status"] == "IN_REVIEW"

        retry_response = client.post(
            f"/api/v1/review-cases/{seeded_case.review_case_id}/commands",
            headers=_headers(tenant_id, actor_id="ap-operator-1", role=InterfaceRole.AP_OPERATOR),
            json=_claim_request(seeded_case, idempotency_key="m10-api-write-claim-v1"),
        )

        assert retry_response.status_code == 200
        assert retry_response.json()["status"] == "IDEMPOTENT"

        release_request = _claim_request(seeded_case, idempotency_key="m10-api-write-release-v1")
        release_request["action"] = "RELEASE"
        release_request["observed_review_revision"] = 1
        release_request["observed_workflow_revision"] = 2

        release_response = client.post(
            f"/api/v1/review-cases/{seeded_case.review_case_id}/commands",
            headers=_headers(tenant_id, actor_id="ap-operator-1", role=InterfaceRole.AP_OPERATOR),
            json=release_request,
        )

        assert release_response.status_code == 200
        assert release_response.json()["status"] == "ACCEPTED"
        assert release_response.json()["data"]["resulting_case_status"] == "OPEN"
