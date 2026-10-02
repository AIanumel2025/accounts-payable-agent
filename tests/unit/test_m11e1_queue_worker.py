"""M11E.1: SQS dispatch messages and the queue-driven worker (idempotency, poison
messages, interrupted jobs). No database, no AWS."""

from __future__ import annotations

import json
import uuid
from contextlib import contextmanager
from dataclasses import replace
from typing import Any

import pytest

from ap_agent.exceptions import OperationsRequestRejectedError
from ap_agent.models.operations import WorkflowJobStatus
from ap_agent.repositories.operations_repository import JobStateError
from ap_agent.services.job_dispatch import (
    MESSAGE_GROUP_ID,
    DispatchMessage,
    DispatchMessageError,
    NoopJobDispatcher,
    SqsJobDispatcher,
    dispatch_if_queued,
)
from ap_agent.worker.lambda_handler import MessageOutcome, process_dispatch_record, process_sqs_event
from tests.support.m11e1_aws import FakeOperations

pytestmark = pytest.mark.unit

TENANT = uuid.UUID("11111111-1111-1111-1111-111111111111")


def _job(status=WorkflowJobStatus.QUEUED):
    ops = FakeOperations()
    job = ops.insert_upload_job(
        job_id=uuid.uuid4(), tenant_id=TENANT, idempotency_key="ap-direct-" + "a" * 32, request_fingerprint="f",
        source_name="a.pdf", artifact_uri="object://x", artifact_sha256="a" * 64, media_type="application/pdf",
        byte_size=1, created_by="user-1",
    )
    return replace(job, status=status)


class FakeSqs:
    def __init__(self, fail=False):
        self.sent: list[dict[str, Any]] = []
        self.fail = fail

    def send_message(self, **kwargs):
        if self.fail:
            raise ConnectionError("down https://sqs.secret.example")
        self.sent.append(kwargs)


# -- message / dispatcher ---------------------------------------------------------------------


def test_message_contains_exactly_job_tenant_and_generation():
    job = _job()
    body = json.loads(DispatchMessage(job.job_id, job.tenant_id).to_body())

    assert body == {"v": 1, "job_id": str(job.job_id), "tenant_id": str(TENANT), "dispatch_generation": 1}


@pytest.mark.parametrize(
    "body",
    [
        None, "", "not json", "[]", "{}",
        json.dumps({"v": 1, "job_id": str(uuid.uuid4()), "tenant_id": str(TENANT)}),  # no generation
        json.dumps({"v": 2, "job_id": str(uuid.uuid4()), "tenant_id": str(TENANT), "dispatch_generation": 1}),
        json.dumps({"v": 1, "job_id": "nope", "tenant_id": str(TENANT), "dispatch_generation": 1}),
        json.dumps({"v": 1, "job_id": str(uuid.uuid4()), "tenant_id": str(TENANT), "dispatch_generation": 0}),
        json.dumps({"v": 1, "job_id": str(uuid.uuid4()), "tenant_id": str(TENANT), "dispatch_generation": True}),
        json.dumps({"v": 1, "job_id": str(uuid.uuid4()), "tenant_id": str(TENANT), "dispatch_generation": 1, "extra": 1}),
    ],
)
def test_malformed_messages_are_refused_without_echoing_them(body):
    with pytest.raises(DispatchMessageError) as raised:
        DispatchMessage.parse(body)

    assert str(body) not in str(raised.value) or body in (None, "", "{}")


def test_sqs_dispatch_uses_one_fifo_group_and_a_deterministic_deduplication_id():
    client, job = FakeSqs(), _job()
    dispatcher = SqsJobDispatcher(client, "https://sqs.eu-west-2.amazonaws.com/000000000000/q.fifo")

    dispatcher.dispatch(job)
    dispatcher.dispatch(job)

    assert [m["MessageGroupId"] for m in client.sent] == [MESSAGE_GROUP_ID, MESSAGE_GROUP_ID]
    assert client.sent[0]["MessageDeduplicationId"] == client.sent[1]["MessageDeduplicationId"] == f"{job.job_id.hex}-1"
    assert "a.pdf" not in client.sent[0]["MessageBody"] and "object://" not in client.sent[0]["MessageBody"]


def test_dispatch_failure_is_a_redacted_503():
    with pytest.raises(OperationsRequestRejectedError) as raised:
        SqsJobDispatcher(FakeSqs(fail=True), "https://q").dispatch(_job())

    assert raised.value.code == "DISPATCH_UNAVAILABLE" and raised.value.http_status == 503
    assert "secret" not in raised.value.message


def test_only_queued_jobs_are_dispatched():
    client = FakeSqs()
    dispatcher = SqsJobDispatcher(client, "https://q")

    dispatch_if_queued(dispatcher, _job(WorkflowJobStatus.RUNNING))
    dispatch_if_queued(dispatcher, _job(WorkflowJobStatus.SUCCEEDED))
    assert client.sent == []
    dispatch_if_queued(dispatcher, _job(WorkflowJobStatus.QUEUED))
    assert len(client.sent) == 1
    dispatch_if_queued(NoopJobDispatcher(), _job())  # Render/local: nothing happens


# -- worker -------------------------------------------------------------------------------------


class FakeOps:
    """Stand-in for OperationsRepository's worker surface, backed by one job."""

    def __init__(self, job):
        self.job = job
        self.claims = 0
        self.finished: list[dict[str, Any]] = []
        self.fail_finish = False

    def peek_job(self, job_id):
        return self.job if self.job is not None and self.job.job_id == job_id else None

    def claim_job(self, job_id, *, tenant_id):
        if self.job.status is not WorkflowJobStatus.QUEUED:
            return None
        self.claims += 1
        self.job = replace(self.job, status=WorkflowJobStatus.RUNNING)
        return self.job

    @contextmanager
    def transaction(self, tenant_id):
        yield object()

    def finish_job(self, cursor, job, **kwargs):
        if self.fail_finish:
            raise JobStateError("not running")
        self.finished.append(kwargs)
        self.job = replace(self.job, status=kwargs["status"], error_code=kwargs.get("error_code"))
        return self.job


class FakeRunner:
    def __init__(self, job, *, outcome=WorkflowJobStatus.REVIEW_REQUIRED, explode=False):
        self.operations = FakeOps(job)
        self.executions: list[Any] = []
        self.outcome = outcome
        self.explode = explode

    def execute_claimed(self, job):
        self.executions.append(job.job_id)
        if self.explode:
            raise RuntimeError("OCR exploded with /secret/path")
        self.operations.job = replace(self.operations.job, status=self.outcome)
        return self.operations.job


def _record(job, *, receive_count=1, body=None, message_id="m-1"):
    return {
        "messageId": message_id,
        "body": body if body is not None else DispatchMessage(job.job_id, job.tenant_id).to_body(),
        "attributes": {"ApproximateReceiveCount": str(receive_count)},
    }


def test_a_queued_job_is_claimed_and_executed_exactly_once():
    job = _job()
    runner = FakeRunner(job)

    assert process_dispatch_record(_record(job), runner=runner) is MessageOutcome.PROCESSED
    assert runner.executions == [job.job_id] and runner.operations.claims == 1


def test_duplicate_deliveries_never_execute_twice():
    job = _job()
    runner = FakeRunner(job)

    first = process_dispatch_record(_record(job), runner=runner)
    second = process_dispatch_record(_record(job, receive_count=2), runner=runner)
    third = process_dispatch_record(_record(job, receive_count=3, message_id="m-2"), runner=runner)

    assert first is MessageOutcome.PROCESSED
    assert second is third is MessageOutcome.DUPLICATE_SKIPPED  # terminal job: nothing to do
    assert runner.executions == [job.job_id]


def test_a_claim_lost_to_a_concurrent_delivery_is_skipped():
    job = _job()
    runner = FakeRunner(job)
    runner.operations.claim_job = lambda *a, **k: None  # type: ignore[method-assign]

    assert process_dispatch_record(_record(job), runner=runner) is MessageOutcome.DUPLICATE_SKIPPED
    assert runner.executions == []


def test_a_running_job_on_first_delivery_is_left_alone():
    job = _job(WorkflowJobStatus.RUNNING)
    runner = FakeRunner(job)

    assert process_dispatch_record(_record(job, receive_count=1), runner=runner) is MessageOutcome.DUPLICATE_SKIPPED
    assert runner.operations.finished == [] and runner.executions == []


def test_a_job_orphaned_by_a_dead_invocation_is_failed_not_silently_rerun():
    job = _job(WorkflowJobStatus.RUNNING)
    runner = FakeRunner(job)

    outcome = process_dispatch_record(_record(job, receive_count=2), runner=runner)

    assert outcome is MessageOutcome.INTERRUPTED_JOB_FAILED
    assert runner.operations.finished[0]["error_code"] == "WORKER_INTERRUPTED"
    assert runner.operations.job.status is WorkflowJobStatus.FAILED and runner.executions == []


def test_interrupted_job_race_with_a_finishing_invocation_is_a_skip():
    job = _job(WorkflowJobStatus.RUNNING)
    runner = FakeRunner(job)
    runner.operations.fail_finish = True

    assert process_dispatch_record(_record(job, receive_count=2), runner=runner) is MessageOutcome.DUPLICATE_SKIPPED


@pytest.mark.parametrize("status", [WorkflowJobStatus.SUCCEEDED, WorkflowJobStatus.REVIEW_REQUIRED, WorkflowJobStatus.FAILED])
def test_finished_jobs_are_never_touched(status):
    job = _job(status)
    runner = FakeRunner(job)

    assert process_dispatch_record(_record(job, receive_count=4), runner=runner) is MessageOutcome.DUPLICATE_SKIPPED
    assert runner.operations.finished == [] and runner.executions == []


def test_unknown_job_and_tenant_mismatch_are_poison():
    from ap_agent.worker.lambda_handler import PoisonMessageError

    job = _job()
    runner = FakeRunner(job)

    with pytest.raises(PoisonMessageError, match="JOB_NOT_FOUND"):
        process_dispatch_record(_record(replace(job, job_id=uuid.uuid4())), runner=runner)

    wrong_tenant = DispatchMessage(job.job_id, uuid.uuid4()).to_body()
    with pytest.raises(PoisonMessageError, match="TENANT_MISMATCH"):
        process_dispatch_record(_record(job, body=wrong_tenant), runner=runner)

    assert runner.executions == [] and runner.operations.claims == 0


def test_event_response_reports_only_failed_messages_for_redrive():
    good, bad = _job(), _job()
    runner = FakeRunner(good)
    event = {
        "Records": [
            _record(good, message_id="ok"),
            _record(bad, body="garbage", message_id="poison"),
        ]
    }

    assert process_sqs_event(event, runner=runner) == {"batchItemFailures": [{"itemIdentifier": "poison"}]}


def test_transient_infrastructure_errors_are_redriven_without_leaking_details(caplog):
    job = _job()
    runner = FakeRunner(job)
    runner.operations.peek_job = lambda *_: (_ for _ in ()).throw(ConnectionError("postgres://user:pw@host/db"))  # type: ignore[method-assign]

    with caplog.at_level("ERROR"):
        response = process_sqs_event({"Records": [_record(job, message_id="t-1")]}, runner=runner)

    assert response == {"batchItemFailures": [{"itemIdentifier": "t-1"}]}
    assert "postgres://" not in caplog.text and "pw" not in caplog.text


def test_executor_crash_is_the_runners_job_to_record_not_the_handlers():
    """`WorkerRunner.execute_claimed` records WORKER_INTERNAL_ERROR; a crash escaping it is transient."""

    job = _job()
    runner = FakeRunner(job, explode=True)

    response = process_sqs_event({"Records": [_record(job, message_id="x")]}, runner=runner)

    assert response == {"batchItemFailures": [{"itemIdentifier": "x"}]}
