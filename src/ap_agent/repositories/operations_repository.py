"""PostgreSQL data access for the M11D Core durable operations queue.

New in M11D (not sourced from the notebook). Owns every SQL statement that
touches `workflow_jobs` and `workflow_job_events`, plus the small amount of
workflow-state SQL both executors share:

  - the control plane (HTTP) inserts jobs and reads job state/events; it
    never claims or finalizes a job;
  - the worker claims the oldest `QUEUED` job with `FOR UPDATE SKIP LOCKED`
    under the narrow `ap_agent.worker_scope = 'queue'` row-level-security
    scope (the only cross-tenant visibility in the schema, limited to
    `workflow_jobs`), then does all further work under the claimed job's own
    tenant context;
  - no method holds a transaction open across OCR or pipeline execution.

**M11D Core supports exactly one active worker.** `SKIP LOCKED` keeps an
accidental second worker from double-claiming a job, but there are no
leases, heartbeats, retries, crash recovery or dead-letter state: a job left
`RUNNING` by a killed worker stays `RUNNING` (deferred to M11E). Every
status change follows the `QUEUED -> RUNNING -> terminal` state machine,
which a database trigger also enforces.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Iterator, Optional
from uuid import NAMESPACE_URL, UUID, uuid5

from ap_agent.config.postgres import MemoryConfig
from ap_agent.db.connection import open_connection, set_tenant_context
from ap_agent.models.operations import (
    WorkflowJob,
    WorkflowJobEvent,
    WorkflowJobStatus,
    WorkflowJobType,
)

if TYPE_CHECKING:  # pragma: no cover
    import psycopg

__all__ = [
    "IdempotencyRaceError",
    "JobStateError",
    "OperationsRepository",
    "job_event_id",
    "job_batch_id",
    "resume_job_id",
    "insert_job_event",
    "insert_resume_job",
    "DerivedVersionRecord",
    "derived_version_id",
    "derived_review_id",
    "insert_derived_version",
    "open_derived_review_case",
    "set_worker_scope",
]


class IdempotencyRaceError(Exception):
    """A concurrent submission with the same idempotency key committed
    first; the caller re-reads the winner and compares fingerprints."""


class JobStateError(Exception):
    """A guarded job transition found the job not in the expected state."""


_JOB_COLUMNS = """
    job_id, tenant_id, job_type, idempotency_key, request_fingerprint, source_name,
    batch_id, workflow_id, document_id, review_id, resume_plan_id,
    artifact_uri, artifact_sha256, media_type, byte_size,
    status, current_stage, attempt_count, max_attempts,
    error_code, result_summary, result_review_id, result_version_id,
    created_by, created_at, updated_at, started_at, completed_at, lock_version
"""


def job_event_id(tenant_id: UUID, job_id: UUID, sequence_number: int) -> UUID:
    return uuid5(NAMESPACE_URL, f"ap-agent/job-event/{tenant_id}/{job_id}/{sequence_number}")


def job_batch_id(tenant_id: UUID, job_id: UUID) -> UUID:
    """One batch per upload job; deterministic so a repeated execution of the
    same job persists under identical identities."""

    return uuid5(NAMESPACE_URL, f"ap-agent/job-batch/{tenant_id}/{job_id}")


def set_worker_scope(cursor: "psycopg.Cursor") -> None:
    """Transaction-locally enable the queue-claim scope. Only the worker
    calls this; it widens visibility of `workflow_jobs` alone (migration
    0004's `worker_queue_*` policies)."""

    cursor.execute("SELECT set_config('ap_agent.worker_scope', 'queue', TRUE);")


def _row_to_job(row: tuple[Any, ...]) -> WorkflowJob:
    (
        job_id, tenant_id, job_type, idempotency_key, request_fingerprint, source_name,
        batch_id, workflow_id, document_id, review_id, resume_plan_id,
        artifact_uri, artifact_sha256, media_type, byte_size,
        status, current_stage, attempt_count, max_attempts,
        error_code, result_summary, result_review_id, result_version_id,
        created_by, created_at, updated_at, started_at, completed_at, lock_version,
    ) = row[:29]

    return WorkflowJob(
        job_id=job_id,
        tenant_id=tenant_id,
        job_type=WorkflowJobType(job_type),
        idempotency_key=idempotency_key,
        request_fingerprint=request_fingerprint.strip(),
        source_name=source_name,
        batch_id=batch_id,
        workflow_id=workflow_id,
        document_id=document_id,
        review_id=review_id,
        resume_plan_id=resume_plan_id,
        artifact_uri=artifact_uri,
        artifact_sha256=artifact_sha256.strip() if artifact_sha256 else None,
        media_type=media_type,
        byte_size=byte_size,
        status=WorkflowJobStatus(status),
        current_stage=current_stage,
        attempt_count=int(attempt_count),
        max_attempts=int(max_attempts),
        error_code=error_code,
        result_summary=dict(result_summary or {}),
        result_review_id=result_review_id,
        result_version_id=result_version_id,
        created_by=created_by,
        created_at=created_at,
        updated_at=updated_at,
        started_at=started_at,
        completed_at=completed_at,
        lock_version=int(lock_version),
    )


def _row_to_event(row: tuple[Any, ...]) -> WorkflowJobEvent:
    return WorkflowJobEvent(
        event_id=row[0], tenant_id=row[1], job_id=row[2], sequence_number=int(row[3]), event_type=row[4],
        stage=row[5], status=row[6], attempt_number=int(row[7]), message=row[8], error_code=row[9],
        occurred_at=row[10],
    )


def insert_job_event(
    cursor: "psycopg.Cursor",
    *,
    tenant_id: UUID,
    job_id: UUID,
    event_type: str,
    status: str,
    message: str,
    stage: Optional[str] = None,
    attempt_number: int = 0,
    error_code: Optional[str] = None,
) -> int:
    """Append one event inside the caller's transaction. The job row is
    locked first so concurrent appenders (control plane and worker) cannot
    compute the same sequence number."""

    cursor.execute(
        "SELECT 1 FROM ap_agent.workflow_jobs WHERE tenant_id = %s AND job_id = %s FOR NO KEY UPDATE;",
        (tenant_id, job_id),
    )

    if cursor.fetchone() is None:
        raise JobStateError("The job does not exist for this tenant.")

    cursor.execute(
        """
        SELECT COALESCE(MAX(sequence_number), 0) + 1
        FROM ap_agent.workflow_job_events WHERE tenant_id = %s AND job_id = %s;
        """,
        (tenant_id, job_id),
    )
    sequence_number = int(cursor.fetchone()[0])

    cursor.execute(
        """
        INSERT INTO ap_agent.workflow_job_events
            (event_id, tenant_id, job_id, sequence_number, event_type, stage, status,
             attempt_number, message, error_code, occurred_at)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, clock_timestamp());
        """,
        (
            job_event_id(tenant_id, job_id, sequence_number), tenant_id, job_id, sequence_number,
            event_type, stage, status, attempt_number, message[:500] or "event", error_code,
        ),
    )
    return sequence_number


def resume_job_id(tenant_id: UUID, resume_plan_id: UUID) -> UUID:
    return uuid5(NAMESPACE_URL, f"ap-agent/resume-job/{tenant_id}/{resume_plan_id}")


def insert_resume_job(
    cursor: "psycopg.Cursor",
    *,
    tenant_id: UUID,
    idempotency_key: str,
    request_fingerprint: str,
    source_name: str,
    workflow_id: UUID,
    batch_id: UUID,
    document_id: UUID,
    review_id: UUID,
    resume_plan_id: UUID,
    restart_stage: str,
    created_by: str,
) -> WorkflowJob:
    """Enqueue the `RESUME_WORKFLOW` job inside the *caller's* resume-command
    transaction, so the handoff, its audit event and the job commit (or roll
    back) together. The job id is a pure function of the plan id."""

    job_id = resume_job_id(tenant_id, resume_plan_id)

    cursor.execute(
        f"""
        INSERT INTO ap_agent.workflow_jobs
            (job_id, tenant_id, job_type, idempotency_key, request_fingerprint, source_name,
             workflow_id, batch_id, document_id, review_id, resume_plan_id, current_stage, created_by)
        VALUES (%s, %s, 'RESUME_WORKFLOW', %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        RETURNING {_JOB_COLUMNS};
        """,
        (
            job_id, tenant_id, idempotency_key, request_fingerprint, source_name, workflow_id, batch_id,
            document_id, review_id, resume_plan_id, restart_stage, created_by,
        ),
    )
    job = _row_to_job(cursor.fetchone())
    insert_job_event(
        cursor, tenant_id=tenant_id, job_id=job_id, event_type="JOB_SUBMITTED", status="QUEUED",
        stage=restart_stage, message=f"Workflow resume queued; restart stage {restart_stage}.",
    )
    return job


@dataclass(frozen=True)
class DerivedVersionRecord:
    """Everything an append-only `invoice_memory_versions` row holds."""

    version_id: UUID
    tenant_id: UUID
    workflow_id: UUID
    batch_id: UUID
    document_id: UUID
    parent_memory_record_id: UUID
    parent_memory_version_id: Optional[UUID]
    resume_plan_id: UUID
    decision_id: UUID
    derived_version: str
    restart_stage: str
    source_document_sha256: str
    source_memory_payload_sha256: str
    source_normalization_sha256: str
    correction_overlay_sha256: str
    invoice_number: Optional[str]
    supplier_name: Optional[str]
    currency: Optional[str]
    total_amount: Optional[Any]
    normalization_status: str
    financial_validation_status: str
    matching_status: str
    supplier_resolution_status: str
    purchase_order_status: str
    review_required: bool
    review_reasons: tuple[str, ...]
    terminal_status: str
    new_review_id: Optional[UUID]
    normalized_invoice: dict
    financial_validation: dict
    matching_result: dict
    matched_reference_data: dict
    payload_sha256: str


def derived_version_id(tenant_id: UUID, resume_plan_id: UUID) -> UUID:
    return uuid5(NAMESPACE_URL, f"ap-agent/derived-memory-version/{tenant_id}/{resume_plan_id}")


def derived_review_id(tenant_id: UUID, resume_plan_id: UUID) -> UUID:
    return uuid5(NAMESPACE_URL, f"ap-agent/derived-review-case/{tenant_id}/{resume_plan_id}")


def insert_derived_version(cursor: "psycopg.Cursor", record: DerivedVersionRecord) -> bool:
    """Append the derived version once. The deterministic identity and the
    `(tenant, resume_plan)` unique constraint make a repeated resume
    execution a no-op; returns whether a new row was written."""

    from psycopg.types.json import Jsonb

    cursor.execute(
        """
        INSERT INTO ap_agent.invoice_memory_versions
            (version_id, tenant_id, workflow_id, batch_id, document_id, parent_memory_record_id,
             parent_memory_version_id, resume_plan_id, decision_id, derived_version, restart_stage,
             source_document_sha256, source_memory_payload_sha256, source_normalization_sha256,
             correction_overlay_sha256, invoice_number, supplier_name, currency, total_amount,
             normalization_status, financial_validation_status, matching_status,
             supplier_resolution_status, purchase_order_status, review_required, review_reasons,
             terminal_status, new_review_id, normalized_invoice, financial_validation,
             matching_result, matched_reference_data, payload_sha256)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (tenant_id, resume_plan_id) DO NOTHING;
        """,
        (
            record.version_id, record.tenant_id, record.workflow_id, record.batch_id, record.document_id,
            record.parent_memory_record_id, record.parent_memory_version_id, record.resume_plan_id,
            record.decision_id, record.derived_version, record.restart_stage, record.source_document_sha256,
            record.source_memory_payload_sha256, record.source_normalization_sha256,
            record.correction_overlay_sha256, record.invoice_number, record.supplier_name, record.currency,
            record.total_amount, record.normalization_status, record.financial_validation_status,
            record.matching_status, record.supplier_resolution_status, record.purchase_order_status,
            record.review_required, list(record.review_reasons), record.terminal_status, record.new_review_id,
            Jsonb(record.normalized_invoice), Jsonb(record.financial_validation),
            Jsonb(record.matching_result), Jsonb(record.matched_reference_data), record.payload_sha256,
        ),
    )
    return cursor.rowcount == 1


def open_derived_review_case(
    cursor: "psycopg.Cursor",
    *,
    tenant_id: UUID,
    workflow_id: UUID,
    review_id: UUID,
    version_id: UUID,
    resume_plan_id: UUID,
    reason_codes: tuple[str, ...],
    summary: str,
) -> None:
    """A *new* `OPEN` review case for a resumed workflow that still needs
    review. The resolved case is never reopened or touched; the version and
    plan links are immutable (database trigger)."""

    cursor.execute(
        """
        INSERT INTO ap_agent.review_cases
            (review_id, tenant_id, workflow_id, review_status, priority, reason_codes, summary,
             memory_version_id, source_resume_plan_id)
        VALUES (%s, %s, %s, 'OPEN', 3, %s, %s, %s, %s)
        ON CONFLICT (review_id) DO NOTHING;
        """,
        (review_id, tenant_id, workflow_id, list(reason_codes), summary, version_id, resume_plan_id),
    )


class OperationsRepository:
    def __init__(self, dsn: str, config: MemoryConfig) -> None:
        self._dsn = dsn
        self._config = config

    @contextmanager
    def transaction(self, tenant_id: UUID) -> Iterator["psycopg.Cursor"]:
        with open_connection(self._dsn, self._config) as connection:
            with connection.cursor() as cursor:
                set_tenant_context(cursor, str(tenant_id))
                yield cursor

    @contextmanager
    def worker_transaction(self) -> Iterator["psycopg.Cursor"]:
        with open_connection(self._dsn, self._config) as connection:
            with connection.cursor() as cursor:
                set_worker_scope(cursor)
                yield cursor

    # ------------------------------------------------------------
    # Events
    # ------------------------------------------------------------

    def insert_event(self, cursor: "psycopg.Cursor", **kwargs: Any) -> int:
        return insert_job_event(cursor, **kwargs)

    def append_event(self, **kwargs: Any) -> int:
        with self.transaction(kwargs["tenant_id"]) as cursor:
            return self.insert_event(cursor, **kwargs)

    def list_events(
        self, tenant_id: UUID, job_id: UUID, *, after_sequence: int = 0, limit: int = 500
    ) -> tuple[WorkflowJobEvent, ...]:
        with self.transaction(tenant_id) as cursor:
            cursor.execute("SET TRANSACTION READ ONLY;")
            cursor.execute(
                """
                SELECT event_id, tenant_id, job_id, sequence_number, event_type, stage, status,
                       attempt_number, message, error_code, occurred_at
                FROM ap_agent.workflow_job_events
                WHERE tenant_id = %s AND job_id = %s AND sequence_number > %s
                ORDER BY sequence_number ASC LIMIT %s;
                """,
                (tenant_id, job_id, after_sequence, limit),
            )
            return tuple(_row_to_event(row) for row in cursor.fetchall())

    # ------------------------------------------------------------
    # Control plane
    # ------------------------------------------------------------

    def find_job_by_idempotency(
        self, tenant_id: UUID, job_type: WorkflowJobType, idempotency_key: str
    ) -> Optional[WorkflowJob]:
        with self.transaction(tenant_id) as cursor:
            cursor.execute("SET TRANSACTION READ ONLY;")
            cursor.execute(
                f"""
                SELECT {_JOB_COLUMNS} FROM ap_agent.workflow_jobs
                WHERE tenant_id = %s AND job_type = %s AND idempotency_key = %s;
                """,
                (tenant_id, job_type.value, idempotency_key),
            )
            row = cursor.fetchone()
            return None if row is None else _row_to_job(row)

    def insert_upload_job(
        self,
        *,
        tenant_id: UUID,
        job_id: UUID,
        idempotency_key: str,
        request_fingerprint: str,
        source_name: str,
        artifact_uri: str,
        artifact_sha256: str,
        media_type: str,
        byte_size: int,
        created_by: str,
    ) -> WorkflowJob:
        import psycopg

        try:
            with self.transaction(tenant_id) as cursor:
                cursor.execute(
                    f"""
                    INSERT INTO ap_agent.workflow_jobs
                        (job_id, tenant_id, job_type, idempotency_key, request_fingerprint, source_name,
                         artifact_uri, artifact_sha256, media_type, byte_size, created_by, batch_id)
                    VALUES (%s, %s, 'PROCESS_DOCUMENT', %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    RETURNING {_JOB_COLUMNS};
                    """,
                    (
                        job_id, tenant_id, idempotency_key, request_fingerprint, source_name,
                        artifact_uri, artifact_sha256, media_type, byte_size, created_by,
                        job_batch_id(tenant_id, job_id),
                    ),
                )
                job = _row_to_job(cursor.fetchone())
                self.insert_event(
                    cursor, tenant_id=tenant_id, job_id=job_id, event_type="JOB_SUBMITTED", status="QUEUED",
                    message="Invoice upload accepted and queued for processing.",
                )
                return job
        except psycopg.errors.UniqueViolation as error:
            raise IdempotencyRaceError() from error

    def get_job(self, tenant_id: UUID, job_id: UUID) -> Optional[WorkflowJob]:
        with self.transaction(tenant_id) as cursor:
            cursor.execute("SET TRANSACTION READ ONLY;")
            cursor.execute(
                f"SELECT {_JOB_COLUMNS} FROM ap_agent.workflow_jobs WHERE tenant_id = %s AND job_id = %s;",
                (tenant_id, job_id),
            )
            row = cursor.fetchone()
            return None if row is None else _row_to_job(row)

    def list_jobs(
        self,
        tenant_id: UUID,
        *,
        statuses: Optional[tuple[str, ...]] = None,
        job_type: Optional[str] = None,
        review_id: Optional[UUID] = None,
        limit: int = 25,
        offset: int = 0,
    ) -> tuple[tuple[WorkflowJob, ...], int]:
        where = """
            tenant_id = %s
            AND (%s::text[] IS NULL OR status = ANY(%s::text[]))
            AND (%s::text IS NULL OR job_type = %s::text)
            AND (%s::uuid IS NULL OR review_id = %s::uuid OR result_review_id = %s::uuid)
        """
        status_list = list(statuses) if statuses else None
        params = (tenant_id, status_list, status_list, job_type, job_type, review_id, review_id, review_id)

        with self.transaction(tenant_id) as cursor:
            cursor.execute("SET TRANSACTION READ ONLY;")
            cursor.execute(f"SELECT COUNT(*) FROM ap_agent.workflow_jobs WHERE {where};", params)
            total = int(cursor.fetchone()[0])
            cursor.execute(
                f"""
                SELECT {_JOB_COLUMNS} FROM ap_agent.workflow_jobs WHERE {where}
                ORDER BY created_at DESC, job_id DESC LIMIT %s OFFSET %s;
                """,
                params + (limit, offset),
            )
            return tuple(_row_to_job(row) for row in cursor.fetchall()), total

    # ------------------------------------------------------------
    # Worker
    # ------------------------------------------------------------

    def claim_next_job(self, *, tenant_id: Optional[UUID] = None) -> Optional[WorkflowJob]:
        """One short transaction: move the oldest `QUEUED` job to `RUNNING`
        and commit before any expensive work begins. `tenant_id` optionally
        scopes the worker to a single tenant (used by tests and the local
        demo so unrelated queued jobs are never consumed)."""

        with self.worker_transaction() as cursor:
            cursor.execute(
                f"""
                WITH candidate AS (
                    SELECT job_id, tenant_id FROM ap_agent.workflow_jobs
                    WHERE status = 'QUEUED' AND (%s::uuid IS NULL OR tenant_id = %s::uuid)
                    ORDER BY created_at ASC
                    FOR UPDATE SKIP LOCKED
                    LIMIT 1
                )
                UPDATE ap_agent.workflow_jobs AS job
                SET status = 'RUNNING', attempt_count = 1,
                    started_at = clock_timestamp(), updated_at = clock_timestamp(),
                    lock_version = job.lock_version + 1
                FROM candidate
                WHERE job.job_id = candidate.job_id AND job.tenant_id = candidate.tenant_id
                RETURNING {", ".join("job." + c.strip() for c in _JOB_COLUMNS.split(","))};
                """,
                (tenant_id, tenant_id),
            )
            row = cursor.fetchone()

            if row is None:
                return None

            job = _row_to_job(row)
            cursor.execute("SELECT set_config('ap_agent.tenant_id', %s, TRUE);", (str(job.tenant_id),))
            self.insert_event(
                cursor, tenant_id=job.tenant_id, job_id=job.job_id, event_type="JOB_CLAIMED",
                status="RUNNING", stage=job.current_stage, attempt_number=1,
                message="Job claimed by the worker.",
            )
            return job

    def peek_job(self, job_id: UUID) -> Optional[WorkflowJob]:
        """Read one job by id under the narrow worker queue scope (M11E.1: the
        queue message names a job; the worker re-reads it from the database
        rather than trusting the message)."""

        with self.worker_transaction() as cursor:
            cursor.execute("SET TRANSACTION READ ONLY;")
            cursor.execute(f"SELECT {_JOB_COLUMNS} FROM ap_agent.workflow_jobs WHERE job_id = %s;", (job_id,))
            row = cursor.fetchone()
            return None if row is None else _row_to_job(row)

    def claim_job(self, job_id: UUID, *, tenant_id: UUID) -> Optional[WorkflowJob]:
        """Like `claim_next_job`, but for one named job (M11E.1, queue-driven
        worker): `QUEUED -> RUNNING` in one short transaction, or `None` when
        the job is not `QUEUED` any more (already claimed, finished, or a
        duplicate delivery). Claiming is therefore idempotent."""

        with self.worker_transaction() as cursor:
            cursor.execute(
                f"""
                UPDATE ap_agent.workflow_jobs AS job
                SET status = 'RUNNING', attempt_count = 1,
                    started_at = clock_timestamp(), updated_at = clock_timestamp(),
                    lock_version = job.lock_version + 1
                WHERE job.job_id = %s AND job.tenant_id = %s AND job.status = 'QUEUED'
                RETURNING {", ".join("job." + c.strip() for c in _JOB_COLUMNS.split(","))};
                """,
                (job_id, tenant_id),
            )
            row = cursor.fetchone()

            if row is None:
                return None

            job = _row_to_job(row)
            cursor.execute("SELECT set_config('ap_agent.tenant_id', %s, TRUE);", (str(job.tenant_id),))
            self.insert_event(
                cursor, tenant_id=job.tenant_id, job_id=job.job_id, event_type="JOB_CLAIMED",
                status="RUNNING", stage=job.current_stage, attempt_number=1,
                message="Job claimed by the worker.",
            )
            return job

    def update_progress(
        self,
        job: WorkflowJob,
        *,
        stage: Optional[str] = None,
        workflow_id: Optional[UUID] = None,
        document_id: Optional[UUID] = None,
    ) -> None:
        with self.transaction(job.tenant_id) as cursor:
            cursor.execute(
                """
                UPDATE ap_agent.workflow_jobs
                SET current_stage = COALESCE(%s, current_stage),
                    workflow_id = COALESCE(%s, workflow_id),
                    document_id = COALESCE(%s, document_id),
                    updated_at = clock_timestamp(), lock_version = lock_version + 1
                WHERE tenant_id = %s AND job_id = %s AND status = 'RUNNING';
                """,
                (stage, workflow_id, document_id, job.tenant_id, job.job_id),
            )

            if cursor.rowcount != 1:
                raise JobStateError("The job is not RUNNING.")

    def finish_job(
        self,
        cursor: "psycopg.Cursor",
        job: WorkflowJob,
        *,
        status: WorkflowJobStatus,
        event_type: str,
        message: str,
        error_code: Optional[str] = None,
        summary: Optional[dict[str, Any]] = None,
        result_review_id: Optional[UUID] = None,
        result_version_id: Optional[UUID] = None,
        stage: Optional[str] = None,
        workflow_id: Optional[UUID] = None,
        document_id: Optional[UUID] = None,
    ) -> WorkflowJob:
        """Terminal transition inside the caller's transaction (so the
        executors can commit their final side effect and the job outcome
        atomically)."""

        from psycopg.types.json import Jsonb

        cursor.execute(
            f"""
            UPDATE ap_agent.workflow_jobs
            SET status = %s, error_code = %s, result_summary = %s,
                result_review_id = COALESCE(%s, result_review_id),
                result_version_id = COALESCE(%s, result_version_id),
                current_stage = COALESCE(%s, current_stage),
                workflow_id = COALESCE(%s, workflow_id),
                document_id = COALESCE(%s, document_id),
                completed_at = clock_timestamp(), updated_at = clock_timestamp(),
                lock_version = lock_version + 1
            WHERE tenant_id = %s AND job_id = %s AND status = 'RUNNING'
            RETURNING {_JOB_COLUMNS};
            """,
            (
                status.value, error_code, Jsonb(summary or {}), result_review_id, result_version_id,
                stage, workflow_id, document_id, job.tenant_id, job.job_id,
            ),
        )
        row = cursor.fetchone()

        if row is None:
            raise JobStateError("The job is not RUNNING.")

        finished = _row_to_job(row)
        self.insert_event(
            cursor, tenant_id=job.tenant_id, job_id=job.job_id, event_type=event_type,
            status=status.value, stage=stage or finished.current_stage,
            attempt_number=finished.attempt_count, message=message, error_code=error_code,
        )
        return finished

    # ------------------------------------------------------------
    # Workflow finalization (shared by both executors)
    # ------------------------------------------------------------

    def finalize_workflow_state(
        self,
        cursor: "psycopg.Cursor",
        *,
        tenant_id: UUID,
        workflow_id: UUID,
        review_required: bool,
        display_name: Optional[str] = None,
    ) -> int:
        """Move a workflow to its settled state once the pipeline ended:
        `HUMAN_REVIEW`/`REVIEW_REQUIRED` (so the M11C claim/decide/resume
        chain applies) or `COMPLETED`/`SUCCEEDED`. The row is locked; the
        revision advances by one.

        `display_name` (upload jobs) replaces the workflow's `source_name`, which
        the ingestion tool sets to its preserved-copy name (`original.<ext>`),
        with the uploaded file's validated name so the review queue shows
        something a person recognises. Presentation only: the append-only
        memory record keeps the pipeline's own name."""

        cursor.execute(
            """
            SELECT lock_version FROM ap_agent.workflow_instances
            WHERE tenant_id = %s AND workflow_id = %s FOR UPDATE;
            """,
            (tenant_id, workflow_id),
        )

        if cursor.fetchone() is None:
            raise JobStateError("The workflow does not exist for this tenant.")

        if review_required:
            cursor.execute(
                """
                UPDATE ap_agent.workflow_instances
                SET current_phase = 'HUMAN_REVIEW', current_status = 'REVIEW_REQUIRED',
                    review_required = TRUE, completed_at = NULL,
                    source_name = COALESCE(%s, source_name),
                    lock_version = lock_version + 1, updated_at = transaction_timestamp()
                WHERE tenant_id = %s AND workflow_id = %s RETURNING lock_version;
                """,
                (display_name, tenant_id, workflow_id),
            )
        else:
            cursor.execute(
                """
                UPDATE ap_agent.workflow_instances
                SET current_phase = 'COMPLETED', current_status = 'SUCCEEDED',
                    review_required = FALSE, completed_at = transaction_timestamp(),
                    source_name = COALESCE(%s, source_name),
                    lock_version = lock_version + 1, updated_at = transaction_timestamp()
                WHERE tenant_id = %s AND workflow_id = %s RETURNING lock_version;
                """,
                (display_name, tenant_id, workflow_id),
            )

        return int(cursor.fetchone()[0])
