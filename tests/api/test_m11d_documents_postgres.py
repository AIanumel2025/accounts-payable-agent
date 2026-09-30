"""M11D Core upload -> worker acceptance (real PostgreSQL, real pipeline).

OCR used, stated precisely:
  * `test_upload_runs_through_the_worker_to_automatic_completion` uses
    `TesseractBackedPaddleShim` -- real Tesseract word boxes behind
    PaddleOCR's `predict()` contract. It is NOT PaddleOCR (not installable in
    this sandbox); real PaddleOCR runs in the repository's OCR CI workflow.
  * `test_real_tesseract_ocr_*` runs the worker's `tesseract` provider: the
    primary OCR provider is disabled, so the existing Phase 3 router takes
    its real Tesseract fallback (which Phase 3 marks REVIEW_REQUIRED by
    design).
Both skip (never silently pass) when the `tesseract` binary is missing.
"""

from __future__ import annotations

import io
import os
import shutil
import subprocess
import sys
import threading
import uuid
from pathlib import Path

import pytest

from ap_agent.adapters.reference_data_adapter import load_reference_data_bundle
from ap_agent.models.operations import WorkflowJobStatus
from ap_agent.repositories.operations_repository import OperationsRepository
from ap_agent.repositories.postgres_memory_repository import PostgresMemoryRepository
from ap_agent.serialization.memory_json import canonical_payload_sha256
from ap_agent.services.memory_service import MemoryService
from ap_agent.worker.pipeline import OcrEngineProvider, build_pipeline_configs, build_production_registry
from tests.support.m11d_harness import Db, ReviewApi, auth_headers, make_client, make_runner
from tests.support.m11d_seed import REFERENCE_DATA_DIRECTORY
from tests.support.tesseract_paddle_shim import TesseractBackedPaddleShim
from ap_agent.models.interface import InterfaceRole

pytestmark = [pytest.mark.integration, pytest.mark.requires_postgres]

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "invoices"
FLAT_PNG = FIXTURES / "08181_flat_document.png"

needs_tesseract = pytest.mark.skipif(shutil.which("tesseract") is None, reason="the tesseract binary is not installed")


def _registry_builder(runtime_dsn, local_config, work_directory, *, provider: str):
    ocr = (
        OcrEngineProvider("paddleocr", engine_factory=lambda: (TesseractBackedPaddleShim(), "tesseract-shim"))
        if provider == "shim"
        else OcrEngineProvider("tesseract")
    )
    reference_data = load_reference_data_bundle(REFERENCE_DATA_DIRECTORY)

    def build(job):
        engine, version = ocr.get()
        registry, _ = build_production_registry(
            configs=build_pipeline_configs(
                work_directory / "phases" / job.job_id.hex, ocr_provider="paddleocr" if provider == "shim" else "tesseract"
            ),
            reference_data=reference_data,
            ocr_engine=engine,
            ocr_engine_version=version,
            memory_service=MemoryService(PostgresMemoryRepository(runtime_dsn, local_config), tenant_id=job.tenant_id),
        )
        return registry

    return build


def _upload(client, tenant_id, path: Path, *, key: str = "m11d-upload-00000001"):
    headers = auth_headers(tenant_id, "m11d-operator-1", InterfaceRole.AP_OPERATOR)
    headers["Idempotency-Key"] = key
    media = {".png": "image/png", ".jpg": "image/jpeg", ".pdf": "application/pdf"}[path.suffix]
    response = client.post(
        "/api/v1/operations/submissions", headers=headers, files={"file": (path.name, path.read_bytes(), media)}
    )
    assert response.status_code == 202, response.text
    return response.json()["data"]["job"]


@pytest.fixture()
def client(runtime_dsn, local_config, tmp_path):
    with make_client(runtime_dsn, local_config, tmp_path / "artifacts") as test_client:
        yield test_client


@pytest.fixture()
def db(runtime_dsn, local_config, tenant_id) -> Db:
    return Db(runtime_dsn, local_config, tenant_id)


@needs_tesseract
def test_upload_runs_through_the_worker_to_automatic_completion(
    client, db, runtime_dsn, local_config, tenant_id, tmp_path
):
    job = _upload(client, tenant_id, FLAT_PNG)

    # Accepted and queued; nothing executed inside the request.
    assert job["status"] == "QUEUED"
    assert db.count("workflow_instances") == 0 and db.count("invoice_memory_records") == 0

    runner = make_runner(
        runtime_dsn, local_config, tenant_id=tenant_id, work_directory=tmp_path,
        artifact_root=tmp_path / "artifacts",
        registry_builder=_registry_builder(runtime_dsn, local_config, tmp_path, provider="shim"),
    )
    finished = runner.run_once()

    assert finished is not None and finished.status == WorkflowJobStatus.SUCCEEDED, (
        finished.error_code, finished.result_summary
    )
    assert str(finished.job_id) == job["job_id"]

    detail = client.get(
        f"/api/v1/operations/jobs/{job['job_id']}",
        headers=auth_headers(tenant_id, "m11d-operator-1", InterfaceRole.AP_OPERATOR),
    ).json()["data"]

    assert detail["job"]["status"] == "SUCCEEDED" and detail["job"]["review_case_id"] is None
    started = [event["stage"] for event in detail["events"] if event["event_type"] == "STAGE_STARTED"]
    assert started == [
        "INGESTION", "PREPROCESSING", "OCR", "NORMALIZATION", "FINANCIAL_VALIDATION", "REFERENCE_MATCHING",
        "MEMORY_PERSISTENCE",
    ]
    assert [event["sequence_number"] for event in detail["events"]] == list(range(1, len(detail["events"]) + 1))
    assert detail["events"][-1]["event_type"] == "JOB_SUCCEEDED"

    summary = detail["job"]["summary"]
    assert summary["workflow_status"] == "SUCCEEDED" and summary["review_required"] is False
    assert summary["supplier_status"] == "MATCHED" and summary["total_amount"] == "69.22"

    # Exactly the persisted effects: one workflow (completed), one memory record, no review case.
    (workflow_id,) = [row[0] for row in db.query("SELECT workflow_id FROM ap_agent.workflow_instances WHERE tenant_id = %s;", (tenant_id,))]
    assert db.workflow(workflow_id)[:3] == ("COMPLETED", "SUCCEEDED", False)
    assert db.count("invoice_memory_records") == 1 and db.count("review_cases") == 0

    # The worker never leaks a server path into job events.
    assert str(tmp_path) not in " ".join(event["message"] for event in detail["events"])


@needs_tesseract
def test_real_tesseract_ocr_review_then_approve_and_resume(client, db, runtime_dsn, local_config, tenant_id, tmp_path):
    job = _upload(client, tenant_id, FLAT_PNG)
    runner = make_runner(
        runtime_dsn, local_config, tenant_id=tenant_id, work_directory=tmp_path,
        artifact_root=tmp_path / "artifacts",
        registry_builder=_registry_builder(runtime_dsn, local_config, tmp_path, provider="tesseract"),
    )

    finished = runner.run_once()

    assert finished.status == WorkflowJobStatus.REVIEW_REQUIRED, (finished.error_code, finished.result_summary)
    assert finished.result_review_id is not None and finished.workflow_id is not None
    events = OperationsRepository(runtime_dsn, local_config).list_events(tenant_id, finished.job_id)
    assert [e.stage for e in events if e.event_type == "STAGE_STARTED"][-1] == "MEMORY_PERSISTENCE"
    assert "OCR_PAGE_1:PRIMARY_PROVIDER_FAILED" in finished.result_summary["review_reasons"]  # real fallback ran

    # The review case is reachable through the M11C API and its original memory verifies.
    reviewer = auth_headers(tenant_id, "m11d-reviewer-1", InterfaceRole.AP_REVIEWER)
    case = client.get(f"/api/v1/review-cases/{finished.result_review_id}", headers=reviewer)
    assert case.status_code == 200
    queue = client.get("/api/v1/review-cases", headers=reviewer).json()["data"]
    assert finished.result_review_id.__str__() in [item["review_case_id"] for item in queue["items"]]

    payload_hash, _ = db.original_memory(finished.workflow_id)
    ((normalized, financial, matching, reference),) = db.query(
        "SELECT normalized_invoice, financial_validation, matching_result, matched_reference_data "
        "FROM ap_agent.invoice_memory_records WHERE tenant_id = %s AND workflow_id = %s;",
        (tenant_id, finished.workflow_id),
    )
    assert payload_hash.strip() == canonical_payload_sha256(
        {"normalization_result": normalized, "financial_validation_result": financial,
         "matching_result": matching, "matched_reference_data": reference}
    )
    assert db.workflow(finished.workflow_id)[:3] == ("HUMAN_REVIEW", "REVIEW_REQUIRED", True)
    # The review queue shows the uploaded file's name (workflow display name), while the
    # append-only memory record keeps the pipeline's own preserved-copy name.
    assert [item["source_name"] for item in queue["items"]] == [FLAT_PNG.name]
    assert db.query(
        "SELECT source_name FROM ap_agent.invoice_memory_records WHERE tenant_id = %s AND workflow_id = %s;",
        (tenant_id, finished.workflow_id),
    )[0][0].startswith("original")
    original_before = db.original_memory(finished.workflow_id)

    # Approve and resume the *real* pipeline-created case.
    api = ReviewApi(client, tenant_id, finished.result_review_id)
    api.claim()
    api.approve()
    assert api.resume(disposition="APPROVED")["data"]["restart_stage"] == "MEMORY_PERSISTENCE"

    resumed = runner.run_once()
    assert resumed.job_type.value == "RESUME_WORKFLOW"
    assert resumed.status == WorkflowJobStatus.SUCCEEDED, (resumed.error_code, resumed.result_summary)
    assert resumed.result_summary["executed_stages"] == ["MEMORY_PERSISTENCE"]
    assert db.workflow(finished.workflow_id)[:3] == ("COMPLETED", "SUCCEEDED", False)
    assert len(db.versions(finished.workflow_id)) == 1
    assert db.original_memory(finished.workflow_id) == original_before
    assert job["job_id"] == str(finished.job_id)


def test_tampered_upload_fails_before_any_stage_runs(client, db, runtime_dsn, local_config, tenant_id, tmp_path):
    job = _upload(client, tenant_id, FLAT_PNG)
    (stored,) = [path for path in (tmp_path / "artifacts").rglob("*") if path.is_file()]
    stored.write_bytes(stored.read_bytes() + b"tampered")

    runner = make_runner(
        runtime_dsn, local_config, tenant_id=tenant_id, work_directory=tmp_path, artifact_root=tmp_path / "artifacts"
    )
    finished = runner.run_once()

    assert finished.status == WorkflowJobStatus.FAILED and finished.error_code == "ARTIFACT_HASH_MISMATCH"
    assert db.count("workflow_instances") == 0
    events = OperationsRepository(runtime_dsn, local_config).list_events(tenant_id, uuid.UUID(job["job_id"]))
    assert not [event for event in events if event.event_type == "STAGE_STARTED"]


def test_a_queued_job_is_claimed_by_exactly_one_contender(client, db, runtime_dsn, local_config, tenant_id):
    _upload(client, tenant_id, FLAT_PNG)
    operations = OperationsRepository(runtime_dsn, local_config)
    claims, barrier = [], threading.Barrier(4)

    def contend():
        barrier.wait()
        claims.append(operations.claim_next_job(tenant_id=tenant_id))

    threads = [threading.Thread(target=contend) for _ in range(4)]
    [thread.start() for thread in threads]
    [thread.join() for thread in threads]

    assert len([claim for claim in claims if claim is not None]) == 1
    assert [row[2] for row in db.jobs()] == ["RUNNING"]


def test_the_worker_scope_is_the_only_cross_tenant_view_of_jobs(client, runtime_dsn, local_config, tenant_id):
    from ap_agent.db.connection import open_connection, set_tenant_context

    _upload(client, tenant_id, FLAT_PNG)

    with open_connection(runtime_dsn, local_config) as connection:
        with connection.cursor() as cursor:
            cursor.execute("SELECT COUNT(*) FROM ap_agent.workflow_jobs;")
            assert cursor.fetchone()[0] == 0  # no tenant context, no worker scope: nothing visible

            set_tenant_context(cursor, str(uuid.uuid4()))
            cursor.execute("SELECT COUNT(*) FROM ap_agent.workflow_jobs;")
            assert cursor.fetchone()[0] == 0  # some other tenant: nothing visible

            set_tenant_context(cursor, str(tenant_id))
            cursor.execute("SELECT COUNT(*) FROM ap_agent.workflow_jobs;")
            assert cursor.fetchone()[0] == 1


def _worker_env(**overrides: str) -> dict[str, str]:
    env = {key: value for key, value in os.environ.items() if not key.startswith("AP_AGENT_")}
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[2] / "src")
    env.update(overrides)
    return env


def test_worker_cli_refuses_to_run_unless_execution_is_explicitly_enabled(tmp_path):
    result = subprocess.run(
        [sys.executable, "-m", "ap_agent.worker", "--once"],
        env=_worker_env(AP_AGENT_ARTIFACT_ROOT=str(tmp_path), AP_AGENT_REFERENCE_DATA_DIRECTORY=str(tmp_path)),
        capture_output=True, text=True, timeout=60,
    )
    assert result.returncode == 3 and "disabled" in result.stderr

    missing = subprocess.run(
        [sys.executable, "-m", "ap_agent.worker", "--once"], env=_worker_env(), capture_output=True, text=True, timeout=60
    )
    assert missing.returncode == 4 and "configuration error" in missing.stderr


def _url_dsn(conninfo: str) -> str:
    from urllib.parse import quote

    from psycopg.conninfo import conninfo_to_dict

    p = conninfo_to_dict(conninfo)
    query = "&".join(f"{key}={quote(str(p[key]))}" for key in ("sslmode", "channel_binding") if key in p)
    return (
        f"postgresql://{quote(p['user'], safe='')}:{quote(p['password'], safe='')}@{p['host']}:{p.get('port', 5432)}"
        f"/{p['dbname']}" + (f"?{query}" if query else "")
    )


def test_worker_cli_once_drains_an_empty_queue_and_prints_no_secret(
    runtime_dsn, local_config, tenant_id, tmp_path
):
    from psycopg.conninfo import conninfo_to_dict

    password = conninfo_to_dict(runtime_dsn)["password"]
    result = subprocess.run(
        [sys.executable, "-m", "ap_agent.worker", "--once"],
        env=_worker_env(
            AP_AGENT_ENABLE_WORKER_EXECUTION="true",
            AP_AGENT_POSTGRES_DSN=_url_dsn(runtime_dsn),
            AP_AGENT_ARTIFACT_ROOT=str(tmp_path / "artifacts"),
            AP_AGENT_REFERENCE_DATA_DIRECTORY=str(REFERENCE_DATA_DIRECTORY),
            AP_AGENT_WORKER_TENANT_ID=str(tenant_id),
            AP_AGENT_WORKER_OCR_PROVIDER="tesseract",
        ),
        capture_output=True, text=True, timeout=120,
    )

    assert result.returncode == 0, result.stderr
    assert "no queued job" in result.stdout
    assert password not in result.stdout + result.stderr
    assert "password" not in (result.stdout + result.stderr).lower()
