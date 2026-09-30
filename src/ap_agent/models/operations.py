"""M11D operations-console contracts: durable workflow jobs, job events and
the explicit limits/timings that govern upload, leasing and retry.

New in M11D (not sourced from the notebook). Pure data: no I/O, no
configuration instance, no heavy imports. Dependency direction: this module
imports only the standard library (`models/` never imports `config/` or
`exceptions`).

M11D Core supports **one active worker**; there are no leases, retries or
dead-letter state (deferred to M11E). A job is nevertheless executed so that
every side effect is idempotent, so a deliberate re-run is safe. Nothing in
this package executes a payment, transfers funds or posts to an ERP.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Optional
from uuid import UUID

__all__ = [
    "WorkflowJobType",
    "WorkflowJobStatus",
    "TERMINAL_JOB_STATUSES",
    "WorkflowJob",
    "WorkflowJobEvent",
    "UploadLimits",
    "WorkerSettings",
    "JobSubmissionOutcome",
    "SubmissionResult",
]


class WorkflowJobType(str, Enum):
    PROCESS_DOCUMENT = "PROCESS_DOCUMENT"
    RESUME_WORKFLOW = "RESUME_WORKFLOW"


class WorkflowJobStatus(str, Enum):
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    REVIEW_REQUIRED = "REVIEW_REQUIRED"
    FAILED = "FAILED"


TERMINAL_JOB_STATUSES = frozenset(
    {
        WorkflowJobStatus.SUCCEEDED,
        WorkflowJobStatus.REVIEW_REQUIRED,
        WorkflowJobStatus.FAILED,
    }
)


@dataclass(frozen=True)
class WorkflowJob:
    job_id: UUID
    tenant_id: UUID
    job_type: WorkflowJobType
    idempotency_key: str
    request_fingerprint: str
    source_name: str

    batch_id: Optional[UUID]
    workflow_id: Optional[UUID]
    document_id: Optional[UUID]
    review_id: Optional[UUID]
    resume_plan_id: Optional[UUID]

    artifact_uri: Optional[str]
    artifact_sha256: Optional[str]
    media_type: Optional[str]
    byte_size: Optional[int]

    status: WorkflowJobStatus
    current_stage: Optional[str]
    attempt_count: int
    max_attempts: int

    error_code: Optional[str]
    result_summary: dict[str, Any]
    result_review_id: Optional[UUID]
    result_version_id: Optional[UUID]

    created_by: str
    created_at: datetime
    updated_at: datetime
    started_at: Optional[datetime]
    completed_at: Optional[datetime]
    lock_version: int

    @property
    def is_terminal(self) -> bool:
        return self.status in TERMINAL_JOB_STATUSES


@dataclass(frozen=True)
class WorkflowJobEvent:
    event_id: UUID
    tenant_id: UUID
    job_id: UUID
    sequence_number: int
    event_type: str
    stage: Optional[str]
    status: str
    attempt_number: int
    message: str
    error_code: Optional[str]
    occurred_at: datetime


@dataclass(frozen=True)
class UploadLimits:
    """Upload limits (M11D task §7). The 10 MiB file ceiling is a
    deliberately conservative bound for a controlled demonstration; the
    request ceiling leaves room for multipart framing only."""

    maximum_file_bytes: int = 10 * 1024 * 1024
    maximum_request_bytes: int = 10 * 1024 * 1024 + 64 * 1024
    maximum_filename_length: int = 128
    allowed_extensions: tuple[str, ...] = (".pdf", ".png", ".jpg", ".jpeg")

    def __post_init__(self) -> None:
        assert self.maximum_file_bytes > 0
        assert self.maximum_request_bytes > self.maximum_file_bytes
        assert self.maximum_filename_length > 0


@dataclass(frozen=True)
class WorkerSettings:
    """Polling bounds for the single M11D Core worker."""

    poll_interval_seconds: float = 1.0
    maximum_poll_interval_seconds: float = 5.0

    def __post_init__(self) -> None:
        assert self.poll_interval_seconds > 0
        assert self.maximum_poll_interval_seconds >= self.poll_interval_seconds


class JobSubmissionOutcome(str, Enum):
    CREATED = "CREATED"
    IDEMPOTENT_REPLAY = "IDEMPOTENT_REPLAY"


@dataclass(frozen=True)
class SubmissionResult:
    outcome: JobSubmissionOutcome
    job: WorkflowJob
    reasons: tuple[str, ...] = field(default_factory=tuple)
