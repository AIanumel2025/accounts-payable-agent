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
  4. runs the same executors as the polling worker (`WorkerRunner.execute_claimed`).

If a previous invocation died mid-job (Lambda timeout, out-of-memory) the job is
left `RUNNING`; SQS redelivers the message after the visibility timeout, and the
redelivery (`ApproximateReceiveCount > 1`) finds the job `RUNNING`. It is then
finished as `FAILED` / `WORKER_INTERRUPTED` -- explicitly *not* silently
re-executed (no duplicate processing; a human re-submits).

The executors and the OCR engine are built once per warm container and reused.
Nothing here reads configuration at import time.
"""

from __future__ import annotations

import logging
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


@lru_cache(maxsize=1)
def _runner() -> Any:
    """Built on first use per warm container (loads the OCR engine once)."""

    from ap_agent.aws.paddle_cache import prepare_paddlex_cache
    from ap_agent.worker.__main__ import build_runner
    from ap_agent.worker.config import load_worker_config

    prepare_paddlex_cache()  # before PaddleOCR is imported: models and fonts come from the image, never the network
    config = load_worker_config()
    return build_runner(config, warm_up_ocr=True)


def handler(event: dict[str, Any], context: Any) -> dict[str, list[dict[str, str]]]:
    logging.getLogger().setLevel(logging.INFO)

    from ap_agent.aws.secrets import load_ssm_parameters_into_environment

    load_ssm_parameters_into_environment()
    return process_sqs_event(event, runner=_runner())
