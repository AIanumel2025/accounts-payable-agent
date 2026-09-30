"""M11D Core control-plane acceptance (real PostgreSQL, real FastAPI app).

Runs against the isolated `ap_agent_m8_test` database as the freshly created
least-privilege runtime role (`tests/api/conftest.py`), one fresh tenant per
test. The HTTP request only validates, stores and enqueues: every test that
submits also asserts that no workflow, memory record or review case exists
afterwards (nothing ran in the request).
"""

from __future__ import annotations

import io
import uuid
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from ap_agent.api.config import ApiConfig
from ap_agent.models.interface import InterfaceRole, interface_utc_now
from ap_agent.models.operations import UploadLimits

pytestmark = [pytest.mark.integration, pytest.mark.requires_postgres]

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "invoices"
FLAT_PNG = FIXTURES / "08181_flat_document.png"
JPEG = FIXTURES / "Template1_Instance90.jpg"
PDF = FIXTURES / "invoice_Aaron Bergman_36258.pdf"

SUBMISSIONS = "/api/v1/operations/submissions"


def _client(runtime_dsn, local_config, artifact_root: Path, *, enabled: bool = True, limits: UploadLimits | None = None):
    from ap_agent.api.app import create_app

    api_config = ApiConfig(
        enable_operations=enabled,
        artifact_root=artifact_root if enabled else None,
        upload_limits=limits or UploadLimits(),
    )
    return TestClient(create_app(dsn=runtime_dsn, memory_config=local_config, api_config=api_config))


def _headers(tenant_id: uuid.UUID, role: InterfaceRole = InterfaceRole.AP_OPERATOR, actor: str = "op-1") -> dict[str, str]:
    return {
        "X-Tenant-ID": str(tenant_id),
        "X-Actor-ID": actor,
        "X-Actor-Role": role.value,
        "X-Authenticated-At": (interface_utc_now() - timedelta(seconds=5)).isoformat(),
    }


def _post(client, tenant_id, *, name: str, content: bytes, media_type: str, key: str | None = "ui-key-00000001",
          role=InterfaceRole.AP_OPERATOR, extra_fields: dict[str, str] | None = None):
    headers = _headers(tenant_id, role)

    if key is not None:
        headers["Idempotency-Key"] = key

    return client.post(
        SUBMISSIONS, headers=headers, files={"file": (name, io.BytesIO(content), media_type)}, data=extra_fields or {}
    )


def _count(runtime_dsn, local_config, tenant_id, table: str) -> int:
    from ap_agent.db.connection import open_connection, set_tenant_context

    with open_connection(runtime_dsn, local_config) as connection:
        with connection.cursor() as cursor:
            set_tenant_context(cursor, str(tenant_id))
            cursor.execute(f"SELECT COUNT(*) FROM ap_agent.{table} WHERE tenant_id = %s;", (tenant_id,))
            return int(cursor.fetchone()[0])


def _stored_files(root: Path) -> list[Path]:
    return [path for path in root.rglob("*") if path.is_file()]


def _assert_nothing_ran(runtime_dsn, local_config, tenant_id) -> None:
    for table in ("workflow_instances", "invoice_memory_records", "review_cases"):
        assert _count(runtime_dsn, local_config, tenant_id, table) == 0, table


def test_operations_are_disabled_by_default(runtime_dsn, local_config, tenant_id, tmp_path):
    client = _client(runtime_dsn, local_config, tmp_path, enabled=False)

    response = _post(client, tenant_id, name="a.png", content=FLAT_PNG.read_bytes(), media_type="image/png")
    assert response.status_code == 403
    assert response.json()["errors"] == ["OPERATIONS_DISABLED"]

    assert client.get("/api/v1/operations/jobs", headers=_headers(tenant_id)).status_code == 403
    assert client.get("/health").json()["data"]["operations_mode"] == "DISABLED"
    assert _stored_files(tmp_path) == []


def test_health_reports_operations_mode(runtime_dsn, local_config, tmp_path):
    client = _client(runtime_dsn, local_config, tmp_path)
    assert client.get("/health").json()["data"]["operations_mode"] == "ENABLED"


def test_submission_enqueues_and_returns_without_running_the_pipeline(runtime_dsn, local_config, tenant_id, tmp_path):
    client = _client(runtime_dsn, local_config, tmp_path)
    content = FLAT_PNG.read_bytes()

    response = _post(client, tenant_id, name=FLAT_PNG.name, content=content, media_type="image/png")

    assert response.status_code == 202
    body = response.json()
    assert body["status"] == "ACCEPTED"
    job = body["data"]["job"]
    assert job["status"] == "QUEUED" and job["job_type"] == "PROCESS_DOCUMENT"
    assert job["source_name"] == FLAT_PNG.name and job["attempt_count"] == 0

    # No server path, reference or hash leaks to the client.
    text = response.text
    assert str(tmp_path) not in text and "artifact://" not in text and "sha256" not in text

    assert _count(runtime_dsn, local_config, tenant_id, "workflow_jobs") == 1
    assert _count(runtime_dsn, local_config, tenant_id, "workflow_job_events") == 1
    _assert_nothing_ran(runtime_dsn, local_config, tenant_id)
    assert len(_stored_files(tmp_path)) == 1

    detail = client.get(f"/api/v1/operations/jobs/{job['job_id']}", headers=_headers(tenant_id)).json()["data"]
    assert [event["event_type"] for event in detail["events"]] == ["JOB_SUBMITTED"]


def test_submission_is_idempotent_and_detects_key_reuse_with_other_content(runtime_dsn, local_config, tenant_id, tmp_path):
    client = _client(runtime_dsn, local_config, tmp_path)
    content = FLAT_PNG.read_bytes()

    first = _post(client, tenant_id, name=FLAT_PNG.name, content=content, media_type="image/png")
    replay = _post(client, tenant_id, name=FLAT_PNG.name, content=content, media_type="image/png")

    assert first.status_code == 202 and replay.status_code == 200
    assert replay.json()["data"]["idempotent_replay"] is True
    assert replay.json()["data"]["job"]["job_id"] == first.json()["data"]["job"]["job_id"]

    conflict = _post(client, tenant_id, name=JPEG.name, content=JPEG.read_bytes(), media_type="image/jpeg")
    assert conflict.status_code == 409
    assert conflict.json()["errors"] == ["IDEMPOTENCY_KEY_CONTENT_CONFLICT"]

    assert _count(runtime_dsn, local_config, tenant_id, "workflow_jobs") == 1
    assert len(_stored_files(tmp_path)) == 1  # replay and conflict left no stray artifact
    _assert_nothing_ran(runtime_dsn, local_config, tenant_id)


@pytest.mark.parametrize(
    ("name", "content", "media_type", "status", "code"),
    [
        ("empty.pdf", b"", "application/pdf", 422, "EMPTY_FILE"),
        ("notes.txt", b"hello", "text/plain", 422, "UNSUPPORTED_FILE_TYPE"),
        ("../../etc/passwd.pdf", b"%PDF-1.4\n%%EOF", "application/pdf", 422, "FILENAME_INVALID"),
        ("..\\evil.pdf", b"%PDF-1.4\n%%EOF", "application/pdf", 422, "FILENAME_INVALID"),
        ("a" * 200 + ".pdf", b"%PDF-1.4\n%%EOF", "application/pdf", 422, "FILENAME_TOO_LONG"),
        ("fake.png", b"%PDF-1.4\n%%EOF", "image/png", 415, "FILE_SIGNATURE_MISMATCH"),
        ("scan.pdf", b"%PDF-1.4\n%%EOF", "image/png", 415, "MEDIA_TYPE_MISMATCH"),
        ("broken.pdf", b"%PDF-1.4 truncated", "application/pdf", 422, "MALFORMED_FILE"),
        ("plain.pdf", b"just text", "application/pdf", 415, "UNSUPPORTED_FILE_TYPE"),
    ],
)
def test_hostile_or_unsupported_uploads_are_refused_before_anything_is_stored(
    runtime_dsn, local_config, tenant_id, tmp_path, name, content, media_type, status, code
):
    client = _client(runtime_dsn, local_config, tmp_path)

    response = _post(client, tenant_id, name=name, content=content, media_type=media_type)

    assert response.status_code == status
    assert response.json()["errors"] == [code]
    assert name not in response.text  # input is never echoed
    assert _count(runtime_dsn, local_config, tenant_id, "workflow_jobs") == 0
    assert _stored_files(tmp_path) == []


def test_oversized_file_is_refused_and_cleaned_up(runtime_dsn, local_config, tenant_id, tmp_path):
    limits = UploadLimits(maximum_file_bytes=2048, maximum_request_bytes=4096)
    client = _client(runtime_dsn, local_config, tmp_path, limits=limits)

    # Over the file limit but under the request ceiling -> file-level refusal.
    response = _post(client, tenant_id, name="big.pdf", content=b"%PDF-" + b"0" * 3000 + b"\n%%EOF", media_type="application/pdf")
    assert response.status_code == 413 and response.json()["errors"] == ["FILE_TOO_LARGE"]

    # Over the whole-request ceiling -> refused from Content-Length alone.
    response = _post(client, tenant_id, name="huge.pdf", content=b"%PDF-" + b"0" * 9000 + b"\n%%EOF", media_type="application/pdf")
    assert response.status_code == 413 and response.json()["errors"] == ["REQUEST_TOO_LARGE"]

    assert _count(runtime_dsn, local_config, tenant_id, "workflow_jobs") == 0
    assert _stored_files(tmp_path) == []


def test_missing_or_invalid_idempotency_key_is_refused(runtime_dsn, local_config, tenant_id, tmp_path):
    client = _client(runtime_dsn, local_config, tmp_path)
    content = FLAT_PNG.read_bytes()

    missing = _post(client, tenant_id, name="a.png", content=content, media_type="image/png", key=None)
    assert missing.status_code == 422 and missing.json()["errors"] == ["IDEMPOTENCY_KEY_MISSING"]

    invalid = _post(client, tenant_id, name="a.png", content=content, media_type="image/png", key="short")
    assert invalid.status_code == 422 and invalid.json()["errors"] == ["IDEMPOTENCY_KEY_INVALID"]
    assert _stored_files(tmp_path) == []


def test_identity_payment_and_unknown_form_fields_are_refused(runtime_dsn, local_config, tenant_id, tmp_path):
    client = _client(runtime_dsn, local_config, tmp_path)
    content = FLAT_PNG.read_bytes()

    for field, code in (
        ("tenant_id", "FIELD_NOT_ALLOWED"),
        ("actor_id", "FIELD_NOT_ALLOWED"),
        ("role", "FIELD_NOT_ALLOWED"),
        ("payment_amount", "PAYMENT_FIELD_PROHIBITED"),
        ("bank_account", "PAYMENT_FIELD_PROHIBITED"),
        ("erp_posting", "PAYMENT_FIELD_PROHIBITED"),
        ("execute_payment", "PAYMENT_FIELD_PROHIBITED"),
    ):
        response = _post(
            client, tenant_id, name="a.png", content=content, media_type="image/png", extra_fields={field: "x"}
        )
        assert response.status_code == 422 and response.json()["errors"] == [code], field

    assert _count(runtime_dsn, local_config, tenant_id, "workflow_jobs") == 0
    assert _stored_files(tmp_path) == []


@pytest.mark.parametrize(
    ("role", "allowed"),
    [
        (InterfaceRole.AP_OPERATOR, True),
        (InterfaceRole.TENANT_ADMIN, True),
        (InterfaceRole.AP_REVIEWER, False),
        (InterfaceRole.READ_ONLY_AUDITOR, False),
    ],
)
def test_only_operators_and_admins_may_submit(runtime_dsn, local_config, tenant_id, tmp_path, role, allowed):
    client = _client(runtime_dsn, local_config, tmp_path)

    response = _post(client, tenant_id, name="a.png", content=FLAT_PNG.read_bytes(), media_type="image/png", role=role)

    if allowed:
        assert response.status_code == 202
    else:
        assert response.status_code == 403 and response.json()["errors"] == ["ACTION_NOT_PERMITTED"]
        assert _stored_files(tmp_path) == []

    # Every role can read the queue (reviewers/auditors follow cases).
    listing = client.get("/api/v1/operations/jobs", headers=_headers(tenant_id, role))
    assert listing.status_code == 200


def test_unauthenticated_requests_are_refused(runtime_dsn, local_config, tenant_id, tmp_path):
    client = _client(runtime_dsn, local_config, tmp_path)

    response = client.post(SUBMISSIONS, files={"file": ("a.png", io.BytesIO(FLAT_PNG.read_bytes()), "image/png")})
    assert response.status_code == 401
    assert client.get("/api/v1/operations/jobs").status_code == 401


def test_jobs_are_tenant_isolated(runtime_dsn, local_config, tenant_id, tmp_path):
    from ap_agent.repositories.postgres_memory_repository import PostgresMemoryRepository

    other_tenant = uuid.uuid4()
    PostgresMemoryRepository(runtime_dsn, local_config).register_tenant(
        tenant_id=other_tenant, tenant_key=f"m11d-other-{other_tenant.hex[:8]}", display_name="Other"
    )
    client = _client(runtime_dsn, local_config, tmp_path)

    job_id = _post(client, tenant_id, name="a.png", content=FLAT_PNG.read_bytes(), media_type="image/png").json()[
        "data"
    ]["job"]["job_id"]

    foreign = client.get(f"/api/v1/operations/jobs/{job_id}", headers=_headers(other_tenant))
    assert foreign.status_code == 404 and foreign.json()["errors"] == ["JOB_NOT_FOUND"]

    assert client.get("/api/v1/operations/jobs", headers=_headers(other_tenant)).json()["data"]["items"] == []
    own = client.get("/api/v1/operations/jobs", headers=_headers(tenant_id)).json()["data"]
    assert [item["job_id"] for item in own["items"]] == [job_id]

    # The same idempotency key in another tenant is an independent job.
    other = _post(client, other_tenant, name="a.png", content=FLAT_PNG.read_bytes(), media_type="image/png")
    assert other.status_code == 202 and other.json()["data"]["job"]["job_id"] != job_id


def test_job_state_machine_is_enforced_by_the_database(runtime_dsn, local_config, tenant_id, tmp_path):
    import psycopg

    from ap_agent.db.connection import open_connection, set_tenant_context

    client = _client(runtime_dsn, local_config, tmp_path)
    job_id = _post(client, tenant_id, name="a.png", content=FLAT_PNG.read_bytes(), media_type="image/png").json()[
        "data"
    ]["job"]["job_id"]

    def attempt(sql: str) -> None:
        with open_connection(runtime_dsn, local_config) as connection:
            with connection.cursor() as cursor:
                set_tenant_context(cursor, str(tenant_id))
                cursor.execute(sql, (job_id,))

    # QUEUED -> SUCCEEDED skips RUNNING.
    with pytest.raises(psycopg.errors.Error):
        attempt("UPDATE ap_agent.workflow_jobs SET status='SUCCEEDED', completed_at=now(), lock_version=lock_version+1 WHERE job_id=%s;")

    # Identity columns are immutable; updates must advance lock_version.
    with pytest.raises(psycopg.errors.Error):
        attempt("UPDATE ap_agent.workflow_jobs SET source_name='x', lock_version=lock_version+1 WHERE job_id=%s;")

    with pytest.raises(psycopg.errors.Error):
        attempt("UPDATE ap_agent.workflow_jobs SET current_stage='OCR' WHERE job_id=%s;")

    # Events are append-only.
    with pytest.raises(psycopg.errors.Error):
        attempt("UPDATE ap_agent.workflow_job_events SET message='x' WHERE job_id=%s;")

    with pytest.raises(psycopg.errors.Error):
        attempt("DELETE FROM ap_agent.workflow_jobs WHERE job_id=%s;")


def test_upload_size_and_filename_limits_are_documented_defaults():
    limits = UploadLimits()
    assert limits.maximum_file_bytes == 10 * 1024 * 1024
    assert limits.maximum_filename_length == 128
    assert set(limits.allowed_extensions) == {".pdf", ".png", ".jpg", ".jpeg"}
