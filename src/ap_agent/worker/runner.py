"""The single M11D Core worker loop.

One process, one job at a time. The HTTP control plane never runs this code.
Claiming is one short transaction; OCR/pipeline work then runs with no
database transaction open. Unexpected executor failures are recorded as a
controlled `WORKER_INTERNAL_ERROR` job failure (type name only; never a
message, path or payload). There is no crash recovery in M11D Core: a job
whose worker is killed mid-run stays `RUNNING` until M11E adds leases.
"""

from __future__ import annotations

import logging
import threading
from typing import Optional, Protocol
from uuid import UUID

from ap_agent.models.operations import WorkerSettings, WorkflowJob, WorkflowJobStatus, WorkflowJobType
from ap_agent.repositories.operations_repository import JobStateError, OperationsRepository

__all__ = ["JobExecutor", "WorkerRunner"]

_LOGGER = logging.getLogger("ap_agent.worker")


class JobExecutor(Protocol):
    def execute(self, job: WorkflowJob) -> WorkflowJob: ...


class WorkerRunner:
    def __init__(
        self,
        *,
        operations: OperationsRepository,
        document_executor: JobExecutor,
        resume_executor: JobExecutor,
        settings: Optional[WorkerSettings] = None,
        tenant_scope: Optional[UUID] = None,
    ) -> None:
        self._operations = operations
        self._executors = {
            WorkflowJobType.PROCESS_DOCUMENT: document_executor,
            WorkflowJobType.RESUME_WORKFLOW: resume_executor,
        }
        self._settings = settings or WorkerSettings()
        self._tenant_scope = tenant_scope

    def run_once(self) -> Optional[WorkflowJob]:
        """Claim and run at most one job. Returns the finished job, or
        `None` when the queue is empty."""

        job = self._operations.claim_next_job(tenant_id=self._tenant_scope)

        if job is None:
            return None

        return self.execute_claimed(job)

    @property
    def operations(self) -> OperationsRepository:
        return self._operations

    def execute_claimed(self, job: WorkflowJob) -> WorkflowJob:
        """Run an already-claimed (`RUNNING`) job to a terminal state. Shared
        by the polling loop and the queue-driven (Lambda) worker, so both
        record unexpected failures identically."""

        try:
            return self._executors[job.job_type].execute(job)
        except Exception as error:  # noqa: BLE001 - the worker must survive any single job
            _LOGGER.error("Job %s failed unexpectedly (%s).", job.job_id, type(error).__name__)

            try:
                with self._operations.transaction(job.tenant_id) as cursor:
                    return self._operations.finish_job(
                        cursor, job, status=WorkflowJobStatus.FAILED, event_type="JOB_FAILED",
                        message="The worker hit an unexpected error.", error_code="WORKER_INTERNAL_ERROR",
                    )
            except (JobStateError, Exception) as finalize_error:  # noqa: BLE001
                _LOGGER.error("Job %s could not be finalized (%s).", job.job_id, type(finalize_error).__name__)
                return job

    def run_forever(self, stop: threading.Event) -> int:
        """Poll until `stop` is set. The in-flight job always finishes
        first (graceful shutdown). Idle polling backs off to a bounded
        maximum. Returns the number of jobs processed."""

        processed = 0
        interval = self._settings.poll_interval_seconds

        while not stop.is_set():
            finished = self.run_once()

            if finished is not None:
                processed += 1
                interval = self._settings.poll_interval_seconds
                continue

            stop.wait(interval)
            interval = min(interval * 1.5, self._settings.maximum_poll_interval_seconds)

        return processed
