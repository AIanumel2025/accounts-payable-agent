"""M11E.1 against real PostgreSQL: queue-driven claiming is idempotent and tenant-safe,
and a Lambda-style delivery runs the real executor path for an uploaded job."""

from __future__ import annotations

import io
import uuid
from datetime import timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from ap_agent.api.config import ApiConfig
from ap_agent.models.interface import interface_utc_now
from ap_agent.models.operations import WorkflowJobStatus
from ap_agent.repositories.operations_repository import OperationsRepository
from ap_agent.services.job_dispatch import DispatchMessage, SqsJobDispatcher
from ap_agent.worker.lambda_handler import MessageOutcome, process_dispatch_record
from tests.unit.test_m11e1_queue_worker import FakeSqs

pytestmark = [pytest.mark.integration, pytest.mark.requires_postgres]

PDF = Path(__file__).resolve().parents[1] / "fixtures" / "invoices" / "invoice_Aaron Bergman_36258.pdf"


def _submit(runtime_dsn, local_config, tenant_id, tmp_path, sqs):
    from ap_agent.api.app import create_app

    app = create_app(
        dsn=runtime_dsn, memory_config=local_config,
        api_config=ApiConfig(enable_operations=True, artifact_root=tmp_path),
        job_dispatcher=SqsJobDispatcher(sqs, "https://sqs.eu-west-2.amazonaws.com/0/q.fifo"),
    )
    headers = {
        "X-Tenant-ID": str(tenant_id), "X-Actor-ID": "op-1", "X-Actor-Role": "AP_OPERATOR",
        "X-Authenticated-At": (interface_utc_now() - timedelta(seconds=5)).isoformat(),
        "Idempotency-Key": f"queue-test-{uuid.uuid4().hex[:12]}",
    }
    response = TestClient(app).post(
        "/api/v1/operations/submissions", headers=headers,
        files={"file": ("a.pdf", io.BytesIO(PDF.read_bytes()), "application/pdf")},
    )
    assert response.status_code == 202
    return uuid.UUID(response.json()["data"]["job"]["job_id"]), headers


class RecordingRunner:
    """Real repository, recording executor (no OCR in this test)."""

    def __init__(self, operations):
        self.operations = operations
        self.executed: list[uuid.UUID] = []

    def execute_claimed(self, job):
        self.executed.append(job.job_id)
        with self.operations.transaction(job.tenant_id) as cursor:
            return self.operations.finish_job(
                cursor, job, status=WorkflowJobStatus.REVIEW_REQUIRED, event_type="JOB_REVIEW_REQUIRED", message="test"
            )


def test_accepted_upload_dispatches_one_message_for_the_committed_job(runtime_dsn, local_config, tenant_id, tmp_path):
    sqs = FakeSqs()
    job_id, _ = _submit(runtime_dsn, local_config, tenant_id, tmp_path, sqs)

    message = DispatchMessage.parse(sqs.sent[0]["MessageBody"])
    assert len(sqs.sent) == 1 and (message.job_id, message.tenant_id) == (job_id, tenant_id)


def test_duplicate_deliveries_claim_and_execute_exactly_once(runtime_dsn, local_config, tenant_id, tmp_path):
    sqs = FakeSqs()
    job_id, _ = _submit(runtime_dsn, local_config, tenant_id, tmp_path, sqs)
    operations = OperationsRepository(runtime_dsn, local_config)
    runner = RecordingRunner(operations)
    record = {"messageId": "m", "body": sqs.sent[0]["MessageBody"], "attributes": {"ApproximateReceiveCount": "1"}}

    outcomes = [
        process_dispatch_record(record, runner=runner),
        process_dispatch_record(record, runner=runner),
        process_dispatch_record({**record, "attributes": {"ApproximateReceiveCount": "2"}}, runner=runner),
    ]

    assert outcomes == [MessageOutcome.PROCESSED, MessageOutcome.DUPLICATE_SKIPPED, MessageOutcome.DUPLICATE_SKIPPED]
    assert runner.executed == [job_id]
    events = [event.event_type for event in operations.list_events(tenant_id, job_id)]
    assert events.count("JOB_CLAIMED") == 1 and events[-1] == "JOB_REVIEW_REQUIRED"


def test_claim_job_is_tenant_scoped_and_idempotent(runtime_dsn, local_config, tenant_id, tmp_path):
    job_id, _ = _submit(runtime_dsn, local_config, tenant_id, tmp_path, FakeSqs())
    operations = OperationsRepository(runtime_dsn, local_config)

    assert operations.claim_job(job_id, tenant_id=uuid.uuid4()) is None  # wrong tenant: untouched
    assert operations.peek_job(job_id).status is WorkflowJobStatus.QUEUED
    claimed = operations.claim_job(job_id, tenant_id=tenant_id)
    assert claimed is not None and claimed.status is WorkflowJobStatus.RUNNING
    assert operations.claim_job(job_id, tenant_id=tenant_id) is None
    assert operations.peek_job(uuid.uuid4()) is None


def test_orphaned_running_job_is_failed_on_redelivery(runtime_dsn, local_config, tenant_id, tmp_path):
    sqs = FakeSqs()
    job_id, _ = _submit(runtime_dsn, local_config, tenant_id, tmp_path, sqs)
    operations = OperationsRepository(runtime_dsn, local_config)
    operations.claim_job(job_id, tenant_id=tenant_id)  # the first invocation then "dies"
    runner = RecordingRunner(operations)

    outcome = process_dispatch_record(
        {"messageId": "m", "body": sqs.sent[0]["MessageBody"], "attributes": {"ApproximateReceiveCount": "2"}}, runner=runner
    )

    job = operations.get_job(tenant_id, job_id)
    assert outcome is MessageOutcome.INTERRUPTED_JOB_FAILED and runner.executed == []
    assert job.status is WorkflowJobStatus.FAILED and job.error_code == "WORKER_INTERRUPTED"


def test_isolated_runner_records_a_dead_child_on_the_real_job(runtime_dsn, local_config, tenant_id, tmp_path):
    import sys

    from ap_agent.worker.lambda_handler import IsolatedJobRunner

    sqs = FakeSqs()
    job_id, _ = _submit(runtime_dsn, local_config, tenant_id, tmp_path, sqs)
    operations = OperationsRepository(runtime_dsn, local_config)
    claimed = operations.claim_job(job_id, tenant_id=tenant_id)
    runner = IsolatedJobRunner(operations, command=[sys.executable, "-c", "import sys; sys.exit(137)"])

    finished = runner.execute_claimed(claimed)

    assert finished.status is WorkflowJobStatus.FAILED and finished.error_code == "WORKER_PROCESS_FAILED"
    assert [e.event_type for e in operations.list_events(tenant_id, job_id)][-1] == "JOB_FAILED"
