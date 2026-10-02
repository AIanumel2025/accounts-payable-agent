"""Job dispatch to an external queue (M11E.1, AWS deployment).

New in M11E.1 (not sourced from the notebook). The database stays the single
source of truth for a job; the queue only *wakes a worker* for it. A message
carries exactly the job id, its tenant and a dispatch generation:

    {"v": 1, "job_id": "<uuid>", "tenant_id": "<uuid>", "dispatch_generation": 1}

The worker never trusts the message beyond locating the job: it re-reads the
job from PostgreSQL, checks the tenant matches and only claims it while it is
still `QUEUED` (`ap_agent.worker.lambda_handler`). A duplicate delivery, or a
message for a job that already ran, therefore does nothing.

Generation `1` is the first dispatch of a job. A deliberate re-dispatch of a
job stuck in `QUEUED` uses the next generation, which changes the FIFO
de-duplication id so the message is not swallowed by the 5-minute window.

Payloads never contain file names, hashes, object keys or credentials.
`boto3` is imported lazily by `build_job_dispatcher`.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any, Optional, Protocol
from uuid import UUID

from ap_agent.config.deployment import QueueBackend, SqsDispatchConfig
from ap_agent.exceptions import OperationsRequestRejectedError
from ap_agent.models.operations import WorkflowJob, WorkflowJobStatus

__all__ = [
    "DISPATCH_MESSAGE_VERSION",
    "MESSAGE_GROUP_ID",
    "DispatchMessage",
    "DispatchMessageError",
    "JobDispatcher",
    "NoopJobDispatcher",
    "SqsJobDispatcher",
    "build_job_dispatcher",
    "dispatch_if_queued",
]

DISPATCH_MESSAGE_VERSION = 1

# One shared group: FIFO delivers one message at a time, in order, which is
# what keeps the "exactly one worker" guarantee of M11D on a queue.
MESSAGE_GROUP_ID = "ap-agent-jobs"

_LOGGER = logging.getLogger("ap_agent.dispatch")

_MESSAGE_KEYS = frozenset({"v", "job_id", "tenant_id", "dispatch_generation"})


class DispatchMessageError(ValueError):
    """The queue message is not a valid dispatch message. The text never
    contains any part of the message."""


@dataclass(frozen=True)
class DispatchMessage:
    job_id: UUID
    tenant_id: UUID
    dispatch_generation: int = 1

    def __post_init__(self) -> None:
        assert self.dispatch_generation >= 1

    def to_body(self) -> str:
        return json.dumps(
            {
                "v": DISPATCH_MESSAGE_VERSION,
                "job_id": str(self.job_id),
                "tenant_id": str(self.tenant_id),
                "dispatch_generation": self.dispatch_generation,
            },
            sort_keys=True,
            separators=(",", ":"),
        )

    @property
    def deduplication_id(self) -> str:
        return f"{self.job_id.hex}-{self.dispatch_generation}"

    @classmethod
    def parse(cls, body: Any) -> "DispatchMessage":
        if not isinstance(body, str):
            raise DispatchMessageError("MESSAGE_NOT_TEXT")

        try:
            payload = json.loads(body)
        except ValueError as error:
            raise DispatchMessageError("MESSAGE_NOT_JSON") from error

        if not isinstance(payload, dict) or set(payload) != _MESSAGE_KEYS:
            raise DispatchMessageError("MESSAGE_SHAPE_INVALID")

        generation = payload["dispatch_generation"]

        if payload["v"] != DISPATCH_MESSAGE_VERSION or isinstance(generation, bool) or not isinstance(generation, int) or generation < 1:
            raise DispatchMessageError("MESSAGE_VERSION_INVALID")

        try:
            job_id = UUID(str(payload["job_id"]))
            tenant_id = UUID(str(payload["tenant_id"]))
        except ValueError as error:
            raise DispatchMessageError("MESSAGE_IDENTITY_INVALID") from error

        return cls(job_id=job_id, tenant_id=tenant_id, dispatch_generation=generation)


class JobDispatcher(Protocol):
    def dispatch(self, job: WorkflowJob, *, generation: int = 1) -> None: ...


class NoopJobDispatcher:
    """Render/local mode: the polling worker finds `QUEUED` jobs itself."""

    def dispatch(self, job: WorkflowJob, *, generation: int = 1) -> None:
        return None


class SqsJobDispatcher:
    def __init__(self, client: Any, queue_url: str) -> None:
        self._client = client
        self._queue_url = queue_url

    def dispatch(self, job: WorkflowJob, *, generation: int = 1) -> None:
        message = DispatchMessage(job_id=job.job_id, tenant_id=job.tenant_id, dispatch_generation=generation)

        try:
            self._client.send_message(
                QueueUrl=self._queue_url,
                MessageBody=message.to_body(),
                MessageGroupId=MESSAGE_GROUP_ID,
                MessageDeduplicationId=message.deduplication_id,
            )
        except Exception as error:  # noqa: BLE001 - classify, never leak the message
            _LOGGER.error("Job dispatch failed (%s).", type(error).__name__)
            raise OperationsRequestRejectedError("DISPATCH_UNAVAILABLE", http_status=503) from error


def build_job_dispatcher(backend: QueueBackend, sqs: Optional[SqsDispatchConfig]) -> JobDispatcher:
    if backend is QueueBackend.SQS:
        assert sqs is not None, "SQS dispatch requires a queue URL."

        import boto3
        from botocore.config import Config

        client = boto3.client(
            "sqs",
            region_name=sqs.region,
            config=Config(connect_timeout=5, read_timeout=10, retries={"max_attempts": 3, "mode": "standard"}),
        )
        return SqsJobDispatcher(client, sqs.queue_url)

    return NoopJobDispatcher()


def dispatch_if_queued(dispatcher: JobDispatcher, job: WorkflowJob) -> None:
    """Dispatch only a job that is still waiting. A replayed submission of a
    job the worker already took is a no-op; a replayed submission of a job
    whose first dispatch failed re-sends it (same de-duplication id, so a
    message that did get through is not duplicated)."""

    if job.status is WorkflowJobStatus.QUEUED:
        dispatcher.dispatch(job)
