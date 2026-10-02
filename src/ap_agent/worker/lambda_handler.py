"""Queue-driven (AWS Lambda) invoice worker (M11E.1).

    SQS FIFO -> Lambda (batch size 1, reserved concurrency 1) -> this handler

The queue only wakes the worker; PostgreSQL stays the source of truth. For each
message the handler

  1. parses the strictly-shaped dispatch message (job id, tenant id, dispatch
     generation) -- anything else is a *poison* message and is reported as a
     batch-item failure so SQS redrives it and finally moves it to the DLQ;
  2. re-reads the job from the database under the narrow worker scope and
     requires the tenant in the message to equal the job's tenant;
  3. claims the job only if it is still `QUEUED` (`OperationsRepository.claim_job`),
     which makes a duplicate delivery, a replayed message or a message for a job
     that already finished a no-op -- never a second execution;
  4. runs the job in a *fresh child process* (`IsolatedJobRunner` -> `ap_agent.worker.run_job`), which uses the same
     executors as the polling worker (`WorkerRunner.execute_claimed`). A child that is killed (out of memory), crashes
     or times out is recorded as a failed job by this process.

If a previous invocation died mid-job (Lambda timeout, out-of-memory) the job is
left `RUNNING`; SQS redelivers the message after the visibility timeout, and the
redelivery (`ApproximateReceiveCount > 1`) finds the job `RUNNING`. It is then
finished as `FAILED` / `WORKER_INTERRUPTED` -- explicitly *not* silently
re-executed (no duplicate processing; a human re-submits).

Nothing here reads configuration at import time, and this process never imports an OCR library.
"""

from __future__ import annotations

import logging
import subprocess
import sys
from enum import Enum
from functools import lru_cache
from typing import Any, Optional

from ap_agent.models.operations import WorkflowJob, WorkflowJobStatus
from ap_agent.repositories.operations_repository import JobStateError
from ap_agent.services.job_dispatch import DispatchMessage, DispatchMessageError

__all__ = [
    "MessageOutcome",
    "PoisonMessageError",
    "process_dispatch_record",
    "process_sqs_event",
    "IsolatedJobRunner",
    "handler",
]

_LOGGER = logging.getLogger("ap_agent.worker.lambda")


class MessageOutcome(str, Enum):
    PROCESSED = "PROCESSED"
    DUPLICATE_SKIPPED = "DUPLICATE_SKIPPED"
    INTERRUPTED_JOB_FAILED = "INTERRUPTED_JOB_FAILED"


class PoisonMessageError(Exception):
    """A message that can never be processed (malformed, unknown job, or a
    tenant that does not match the job). The text is a stable code."""


def _receive_count(record: dict[str, Any]) -> int:
    try:
        return int((record.get("attributes") or {}).get("ApproximateReceiveCount", "1"))
    except (TypeError, ValueError):
        return 1


def process_dispatch_record(record: dict[str, Any], *, runner: Any) -> MessageOutcome:
    """Handle one SQS record. Raises `PoisonMessageError` for an unusable
    message; any other exception is transient and lets SQS retry."""

    try:
        message = DispatchMessage.parse(record.get("body"))
    except DispatchMessageError as error:
        raise PoisonMessageError(str(error)) from error

    operations = runner.operations
    job: Optional[WorkflowJob] = operations.peek_job(message.job_id)

    if job is None:
        raise PoisonMessageError("JOB_NOT_FOUND")

    if job.tenant_id != message.tenant_id:
        raise PoisonMessageError("TENANT_MISMATCH")

    if job.status is WorkflowJobStatus.QUEUED:
        claimed = operations.claim_job(job.job_id, tenant_id=job.tenant_id)

        if claimed is None:  # another delivery claimed it between the read and the claim
            return MessageOutcome.DUPLICATE_SKIPPED

        finished = runner.execute_claimed(claimed)
        _LOGGER.info("Job %s finished as %s.", claimed.job_id, finished.status.value)
        return MessageOutcome.PROCESSED

    if job.status is WorkflowJobStatus.RUNNING and _receive_count(record) > 1:
        try:
            with operations.transaction(job.tenant_id) as cursor:
                operations.finish_job(
                    cursor, job, status=WorkflowJobStatus.FAILED, event_type="JOB_FAILED",
                    message="The worker was interrupted before the job finished.",
                    error_code="WORKER_INTERRUPTED",
                )
        except JobStateError:
            return MessageOutcome.DUPLICATE_SKIPPED

        return MessageOutcome.INTERRUPTED_JOB_FAILED

    return MessageOutcome.DUPLICATE_SKIPPED


def process_sqs_event(event: dict[str, Any], *, runner: Any) -> dict[str, list[dict[str, str]]]:
    """Process every record of an SQS event and answer in the partial-batch
    response format (`ReportBatchItemFailures`). Only the failing message ids
    are returned, so a successful or skipped message is deleted by Lambda."""

    failures: list[dict[str, str]] = []

    for record in event.get("Records", []):
        message_id = str(record.get("messageId", ""))

        try:
            outcome = process_dispatch_record(record, runner=runner)
            _LOGGER.info("Dispatch record handled: %s.", outcome.value)
        except PoisonMessageError as error:
            _LOGGER.error("Unusable dispatch message (%s).", error)
            failures.append({"itemIdentifier": message_id})
        except Exception as error:  # noqa: BLE001 - transient: let SQS redeliver
            _LOGGER.error("Dispatch record failed transiently (%s).", type(error).__name__)
            failures.append({"itemIdentifier": message_id})

    return {"batchItemFailures": failures}


class IsolatedJobRunner:
    """Runs each claimed job in a fresh child process (`ap_agent.worker.run_job`).

    Keeps this (Lambda) process tiny -- no OCR library is imported here -- and gives the child the whole memory
    budget. An out-of-memory kill, a crash or a timeout of the child is recorded on the job by *this* process as a
    controlled failure (`WORKER_PROCESS_FAILED` / `WORKER_TIMEOUT`), so the job never lingers in `RUNNING`.
    """

    # Seconds reserved at the end of the invocation to kill the child and record the failure.
    SAFETY_MARGIN_SECONDS = 45.0
    MINIMUM_TIMEOUT_SECONDS = 30.0

    def __init__(self, operations: Any, *, command: Optional[list[str]] = None) -> None:
        self.operations = operations
        self._command = command or [sys.executable, "-m", "ap_agent.worker.run_job"]
        self.time_budget_seconds: Optional[float] = None

    def execute_claimed(self, job: WorkflowJob) -> WorkflowJob:
        budget = self.time_budget_seconds
        timeout = None if budget is None else max(self.MINIMUM_TIMEOUT_SECONDS, budget - self.SAFETY_MARGIN_SECONDS)
        failure: Optional[str] = None

        try:
            completed = subprocess.run(  # noqa: S603 - fixed command, ids are validated UUIDs
                [*self._command, str(job.job_id), str(job.tenant_id)],
                timeout=timeout, check=False, stdin=subprocess.DEVNULL,
            )

            if completed.returncode != 0:
                failure = "WORKER_PROCESS_FAILED"
                _LOGGER.error("Job %s: the job process exited with status %s.", job.job_id, completed.returncode)
        except subprocess.TimeoutExpired:
            failure = "WORKER_TIMEOUT"
            _LOGGER.error("Job %s: the job process ran out of time and was stopped.", job.job_id)

        current = self.operations.peek_job(job.job_id)

        if failure is not None and current is not None and current.status is WorkflowJobStatus.RUNNING:
            try:
                with self.operations.transaction(job.tenant_id) as cursor:
                    return self.operations.finish_job(
                        cursor, job, status=WorkflowJobStatus.FAILED, event_type="JOB_FAILED",
                        message="The worker process did not finish the job.", error_code=failure,
                    )
            except JobStateError:
                current = self.operations.peek_job(job.job_id)

        return current if current is not None else job


@lru_cache(maxsize=1)
def _runner() -> IsolatedJobRunner:
    """Built on first use per warm container. Reads no OCR library: only the database repository."""

    from ap_agent.config.postgres import MemoryConfig
    from ap_agent.db.connection import load_dsn
    from ap_agent.repositories.operations_repository import OperationsRepository

    memory_config = MemoryConfig()
    return IsolatedJobRunner(OperationsRepository(load_dsn(memory_config), memory_config))


def handler(event: dict[str, Any], context: Any) -> dict[str, list[dict[str, str]]]:
    logging.getLogger().setLevel(logging.INFO)

    from ap_agent.aws.secrets import load_ssm_parameters_into_environment

    load_ssm_parameters_into_environment()  # the child process inherits the loaded environment
    runner = _runner()
    remaining = getattr(context, "get_remaining_time_in_millis", None)
    runner.time_budget_seconds = (remaining() / 1000.0) if callable(remaining) else None
    return process_sqs_event(event, runner=runner)
