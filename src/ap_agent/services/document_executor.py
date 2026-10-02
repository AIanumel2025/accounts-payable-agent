"""`PROCESS_DOCUMENT` job execution (M11D Core).

Runs only inside the worker process. Verifies the stored upload, then drives
the *existing* Phase 1-8 orchestration engine and production handlers
(`ap_agent.orchestration`), streaming safe stage events into the job
timeline, and finally settles the workflow (`HUMAN_REVIEW` or `COMPLETED`)
and the job together in one short transaction.

Every persisted effect is idempotent for a repeated execution of the same
job: the batch and correlation identities derive from the job id, so the
memory service's deterministic, collision-checked writes apply.
"""

from __future__ import annotations

import logging
from contextlib import ExitStack
from pathlib import Path
from typing import Any, Callable, Optional
from uuid import NAMESPACE_URL, UUID, uuid5

from ap_agent.artifacts.storage import ArtifactStore
from ap_agent.exceptions import ArtifactIntegrityError
from ap_agent.models.operations import WorkflowJob, WorkflowJobStatus
from ap_agent.models.orchestration import (
    InvoiceWorkflowRequest,
    InvoiceWorkflowResult,
    InvoiceWorkflowStatus,
    OrchestrationFailureClass,
    default_orchestration_config,
)
from ap_agent.orchestration.engine import StageHandlerRegistry, blocking_retry_waiter, execute_invoice_workflow
from ap_agent.repositories.operations_repository import OperationsRepository
from ap_agent.services.job_events import build_job_event_sink, safe_error_code

__all__ = ["RegistryBuilder", "DocumentJobExecutor", "build_process_summary", "find_review_case_id"]

_LOGGER = logging.getLogger("ap_agent.worker")

# (job, tenant memory service) -> registry for one execution.
RegistryBuilder = Callable[[WorkflowJob], StageHandlerRegistry]


def find_review_case_id(cursor: Any, tenant_id: UUID, workflow_id: UUID) -> Optional[UUID]:
    cursor.execute(
        """
        SELECT review_id FROM ap_agent.review_cases
        WHERE tenant_id = %s AND workflow_id = %s
        ORDER BY opened_at DESC, review_id DESC LIMIT 1;
        """,
        (tenant_id, workflow_id),
    )
    row = cursor.fetchone()
    return None if row is None else row[0]


def build_process_summary(result: InvoiceWorkflowResult) -> dict[str, Any]:
    """Controlled final summary: outcome, stage outcomes and a handful of
    header facts from the tenant's own memory record -- never raw internal
    payloads."""

    stored = getattr(result.memory_bundle, "stored_record", None)

    summary: dict[str, Any] = {
        "workflow_status": result.status.value,
        "review_required": bool(result.review_required),
        "review_reasons": list(result.review_reasons)[:20],
        "stages": [
            {"stage": record.stage.value, "status": record.status.value, "attempt": record.attempt_number}
            for record in result.stage_records
        ],
    }

    if stored is not None:
        summary.update(
            {
                "invoice_number": stored.invoice_number,
                "supplier_name": stored.supplier_name,
                "currency": stored.currency,
                "total_amount": None if stored.total_amount is None else format(stored.total_amount, "f"),
                "supplier_status": stored.supplier_resolution_status,
                "purchase_order_status": stored.purchase_order_status,
                "financial_validation_status": stored.financial_validation_status,
            }
        )

    return summary


class DocumentJobExecutor:
    def __init__(
        self,
        *,
        operations: OperationsRepository,
        artifact_store: ArtifactStore,
        registry_builder: RegistryBuilder,
    ) -> None:
        self._operations = operations
        self._artifact_store = artifact_store
        self._registry_builder = registry_builder

    def _fail(self, job: WorkflowJob, code: str, message: str) -> WorkflowJob:
        with self._operations.transaction(job.tenant_id) as cursor:
            return self._operations.finish_job(
                cursor, job, status=WorkflowJobStatus.FAILED, event_type="JOB_FAILED", message=message,
                error_code=code,
            )

    def execute(self, job: WorkflowJob) -> WorkflowJob:
        assert job.artifact_uri is not None and job.artifact_sha256 is not None and job.batch_id is not None

        # M11E: for object storage this downloads into a private temporary
        # directory and removes it when the job finishes (success or failure);
        # for the local store it verifies the file in place. Source invoices
        # are never deleted by processing.
        with ExitStack() as cleanup:
            try:
                path = cleanup.enter_context(
                    self._artifact_store.verified_local_copy(
                        job.artifact_uri,
                        tenant_id=job.tenant_id,
                        expected_sha256=job.artifact_sha256,
                        filename=job.source_name,
                        expected_size=job.byte_size,
                    )
                )
            except ArtifactIntegrityError as error:
                return self._fail(job, safe_error_code(error.code), "The stored upload failed verification.")

            return self._run_verified(job, path)

    def _run_verified(self, job: WorkflowJob, path: Path) -> WorkflowJob:
        self._operations.append_event(
            tenant_id=job.tenant_id, job_id=job.job_id, event_type="ARTIFACT_VERIFIED", status="RUNNING",
            attempt_number=1, message="Stored upload verified against its recorded SHA-256.",
        )

        request = InvoiceWorkflowRequest(
            tenant_id=job.tenant_id,
            batch_id=job.batch_id,
            correlation_id=uuid5(NAMESPACE_URL, f"ap-agent/job-correlation/{job.tenant_id}/{job.job_id}"),
            source_path=path,
            source_channel="OPERATIONS_CONSOLE",
            submitted_at=job.created_at,
        )

        registry = self._registry_builder(job)

        result = execute_invoice_workflow(
            request=request,
            handler_registry=registry,
            config=default_orchestration_config(),
            retry_waiter=blocking_retry_waiter,
            event_sink=build_job_event_sink(self._operations, job),
        )

        return self._finalize(job, result)

    def _finalize(self, job: WorkflowJob, result: InvoiceWorkflowResult) -> WorkflowJob:
        stored = getattr(result.memory_bundle, "stored_record", None)

        if result.status in {InvoiceWorkflowStatus.SUCCEEDED, InvoiceWorkflowStatus.REVIEW_REQUIRED} and stored is not None:
            review_required = result.status == InvoiceWorkflowStatus.REVIEW_REQUIRED

            with self._operations.transaction(job.tenant_id) as cursor:
                self._operations.finalize_workflow_state(
                    cursor, tenant_id=job.tenant_id, workflow_id=stored.workflow_memory_id,
                    review_required=review_required, display_name=job.source_name,
                )
                review_id = find_review_case_id(cursor, job.tenant_id, stored.workflow_memory_id)
                summary = build_process_summary(result)

                if review_required and review_id is None:
                    return self._operations.finish_job(
                        cursor, job, status=WorkflowJobStatus.FAILED, event_type="JOB_FAILED",
                        message="Review was required but no review case exists.",
                        error_code="REVIEW_CASE_MISSING", summary=summary,
                        workflow_id=stored.workflow_memory_id, document_id=stored.document_id,
                    )

                return self._operations.finish_job(
                    cursor, job,
                    status=WorkflowJobStatus.REVIEW_REQUIRED if review_required else WorkflowJobStatus.SUCCEEDED,
                    event_type="JOB_REVIEW_REQUIRED" if review_required else "JOB_SUCCEEDED",
                    message=(
                        "Processing finished; the invoice requires human review."
                        if review_required
                        else "Processing finished; the invoice completed automatically."
                    ),
                    summary=summary,
                    result_review_id=review_id if review_required else None,
                    workflow_id=stored.workflow_memory_id,
                    document_id=stored.document_id,
                    stage=("HUMAN_REVIEW" if review_required else "COMPLETED"),
                )

        failed_record = next((record for record in reversed(result.stage_records) if record.failure_class), None)
        stage_name = failed_record.stage.value if failed_record is not None else (result.current_stage.value)
        failure_class = (
            failed_record.failure_class.value
            if failed_record is not None and isinstance(failed_record.failure_class, OrchestrationFailureClass)
            else "UNKNOWN"
        )
        code = safe_error_code(f"{stage_name}_{failure_class}_FAILURE")

        if result.status == InvoiceWorkflowStatus.SKIPPED:
            code = "DUPLICATE_DOCUMENT"

        with self._operations.transaction(job.tenant_id) as cursor:
            return self._operations.finish_job(
                cursor, job, status=WorkflowJobStatus.FAILED, event_type="JOB_FAILED",
                message=f"Processing stopped at {stage_name}.", error_code=code,
                summary=build_process_summary(result), stage=stage_name,
            )
