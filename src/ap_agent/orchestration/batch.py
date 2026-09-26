"""Phase 8 batch workflow orchestration.

New in M9 (task §13). The notebook never modularised a batch entry point:
Phase 8 Cell 5 runs its four real fixtures with a plain, sequential
Python ``for`` loop over `execute_real_invoice_request`, isolating
per-invoice failures with a bare ``try/except`` inside the loop body. This
module generalises that same isolation guarantee -- one invoice's
exception never aborts the batch, a successful invoice's result is never
lost because a later invoice failed -- to an arbitrary, bounded-concurrency
batch, using the `BatchWorkflowRequest`/`BatchWorkflowResult` contracts
`ap_agent.models.orchestration` already carries (ported from notebook cell
90, never exercised by the notebook itself).

Every invoice gets its own `InvoiceWorkflowRequest` and, inside
`ap_agent.orchestration.engine.execute_invoice_workflow`, its own fresh
`WorkflowExecutionContext` -- nothing here shares mutable per-invoice state
across invoices (task §13: "Do not share mutable per-invoice context.").
The `StageHandlerRegistry`/handler bindings *are* shared (by design --
`ap_agent.orchestration.handlers.ProductionPhaseBindings` synchronizes the
batch-scoped state it owns), matching how the notebook's own Cell 5 reuses
one `NotebookPhaseToolBindings` instance across all four fixtures.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from typing import Callable, Optional
from uuid import NAMESPACE_URL, UUID, uuid5

from ap_agent.models.orchestration import (
    BatchWorkflowRequest,
    BatchWorkflowResult,
    BatchWorkflowStatus,
    InvoiceWorkflowRequest,
    InvoiceWorkflowResult,
    InvoiceWorkflowStatus,
    OrchestrationConfig,
    OrchestrationEvent,
    OrchestrationStage,
    orchestration_utc_now,
)
from ap_agent.orchestration.engine import (
    RetryWaiter,
    StageHandlerRegistry,
    default_retry_waiter,
    execute_invoice_workflow,
    safe_orchestration_error_message,
)

__all__ = [
    "BatchSizeExceededError",
    "derive_invoice_correlation_id",
    "build_invoice_workflow_requests",
    "execute_batch_workflow",
]


class BatchSizeExceededError(ValueError):
    """Raised when a batch request exceeds
    `OrchestrationConfig.maximum_batch_documents` (task §13: "Reject
    oversized batches before processing.")."""

    def __init__(self, requested: int, maximum: int) -> None:
        super().__init__(
            f"Batch of {requested} documents exceeds the configured maximum "
            f"of {maximum}."
        )
        self.requested = requested
        self.maximum = maximum


def derive_invoice_correlation_id(batch_id: UUID, source_path) -> UUID:
    """Deterministic per-document correlation ID, so the same
    `(batch_id, source_path.name)` pair always yields the same
    `InvoiceWorkflowRequest.correlation_id` across retries or reruns
    (notebook cell 98's `deterministic_phase_8_uuid`, specialised to the
    one identity this module derives)."""

    return uuid5(
        NAMESPACE_URL,
        f"ap-agent/phase-8/correlation/{batch_id}/{source_path.name}",
    )


def build_invoice_workflow_requests(request: BatchWorkflowRequest) -> tuple[InvoiceWorkflowRequest, ...]:
    """Build one deterministic, independent `InvoiceWorkflowRequest` per
    source path, preserving `request.source_paths`' input order (task
    §13: "Stable output ordering matching input ordering.";
    "Deterministic association between request, document and result.")."""

    return tuple(
        InvoiceWorkflowRequest(
            tenant_id=request.tenant_id,
            batch_id=request.batch_id,
            correlation_id=derive_invoice_correlation_id(request.batch_id, source_path),
            source_path=source_path,
            source_channel=request.source_channel,
            submitted_at=request.submitted_at,
            metadata=request.metadata,
        )
        for source_path in request.source_paths
    )


def _execute_one_invoice(
    invoice_request: InvoiceWorkflowRequest,
    handler_registry: StageHandlerRegistry,
    config: OrchestrationConfig,
    retry_waiter: RetryWaiter,
    event_sink: Optional[Callable[[OrchestrationEvent], None]],
) -> InvoiceWorkflowResult:
    """Run exactly one invoice through the engine, converting an
    unhandled exception from the engine or its handlers into a terminal
    FAILED result instead of propagating it (task §13: "Per-invoice
    isolation."; "Support cancellation/error propagation without
    corrupting completed results.") -- the engine itself already isolates
    every *classified* stage failure this way; this is the outermost
    safety net for a defect in a handler that escapes the engine's own
    try/except (for example a bug raised while building the terminal
    result, not while running a stage)."""

    started_at = orchestration_utc_now()

    try:
        return execute_invoice_workflow(
            request=invoice_request,
            handler_registry=handler_registry,
            config=config,
            retry_waiter=retry_waiter,
            event_sink=event_sink,
        )
    except Exception as error:  # noqa: BLE001 - isolation boundary, by design
        return InvoiceWorkflowResult(
            tenant_id=invoice_request.tenant_id,
            batch_id=invoice_request.batch_id,
            correlation_id=invoice_request.correlation_id,
            source_path=invoice_request.source_path,
            status=InvoiceWorkflowStatus.FAILED,
            current_stage=OrchestrationStage.INGESTION,
            started_at=started_at,
            completed_at=orchestration_utc_now(),
            errors=(f"UNHANDLED: {type(error).__name__}: {safe_orchestration_error_message(error)}",),
        )


def _aggregate_batch_status(
    invoice_results: tuple[InvoiceWorkflowResult, ...],
) -> BatchWorkflowStatus:
    if not invoice_results:
        return BatchWorkflowStatus.SUCCEEDED

    statuses = {result.status for result in invoice_results}

    if statuses == {InvoiceWorkflowStatus.SUCCEEDED}:
        return BatchWorkflowStatus.SUCCEEDED

    if statuses <= {InvoiceWorkflowStatus.SUCCEEDED, InvoiceWorkflowStatus.SKIPPED}:
        return BatchWorkflowStatus.SUCCEEDED

    if InvoiceWorkflowStatus.FAILED not in statuses:
        return BatchWorkflowStatus.COMPLETED_WITH_REVIEW

    if statuses == {InvoiceWorkflowStatus.FAILED}:
        return BatchWorkflowStatus.FAILED

    return BatchWorkflowStatus.PARTIALLY_FAILED


def execute_batch_workflow(
    request: BatchWorkflowRequest,
    handler_registry: StageHandlerRegistry,
    config: OrchestrationConfig,
    retry_waiter: RetryWaiter = default_retry_waiter,
    max_workers: Optional[int] = None,
    event_sink: Optional[Callable[[OrchestrationEvent], None]] = None,
) -> BatchWorkflowResult:
    """Execute every document in `request` independently, with bounded
    concurrency, preserving input ordering in the returned
    `BatchWorkflowResult.invoice_results` (task §13).

    `max_workers` defaults to `config.maximum_batch_concurrency` and is
    never allowed to exceed it (task §13: "Never exceed configured
    concurrency."); a real-OCR-provider caller (`ap_agent.adapters.
    paddleocr_adapter`) should keep the default -- `OrchestrationConfig`'s
    validated default (4) is the safe execution mode for real OCR
    providers (task §13: "Default to a safe execution mode for real OCR
    providers.") -- and pass a fixture-free synthetic handler registry
    with a higher `max_workers` only for the kind of network-free,
    OCR-free synthetic load test task §13 also requires.

    `event_sink`, when given, may be invoked concurrently from multiple
    worker threads (one per in-flight invoice); a caller that renders
    progress (`ap_agent.orchestration.observability`) must synchronize
    its own state.
    """

    started_at = orchestration_utc_now()

    if len(request.source_paths) > config.maximum_batch_documents:
        raise BatchSizeExceededError(
            requested=len(request.source_paths),
            maximum=config.maximum_batch_documents,
        )

    invoice_requests = build_invoice_workflow_requests(request)

    if not invoice_requests:
        return BatchWorkflowResult(
            tenant_id=request.tenant_id,
            batch_id=request.batch_id,
            correlation_id=request.correlation_id,
            status=BatchWorkflowStatus.SUCCEEDED,
            started_at=started_at,
            completed_at=orchestration_utc_now(),
            invoice_results=tuple(),
            total_documents=0,
            successful_documents=0,
            review_required_documents=0,
            failed_documents=0,
            skipped_documents=0,
        )

    worker_count = max(1, min(max_workers or config.maximum_batch_concurrency, config.maximum_batch_concurrency))
    worker_count = min(worker_count, len(invoice_requests))

    # Index-preserving submission: results are collected into a
    # pre-sized list keyed by input position, never by completion order,
    # so concurrent completion order can never reorder the output (task
    # §13: "Stable output ordering matching input ordering.").
    ordered_results: list[Optional[InvoiceWorkflowResult]] = [None] * len(invoice_requests)

    with ThreadPoolExecutor(max_workers=worker_count) as executor:
        futures = {
            executor.submit(
                _execute_one_invoice,
                invoice_request,
                handler_registry,
                config,
                retry_waiter,
                event_sink,
            ): index
            for index, invoice_request in enumerate(invoice_requests)
        }

        for future in futures:
            index = futures[future]
            ordered_results[index] = future.result()

    invoice_results = tuple(ordered_results)  # type: ignore[arg-type]

    successful = sum(1 for r in invoice_results if r.status == InvoiceWorkflowStatus.SUCCEEDED)
    review_required = sum(1 for r in invoice_results if r.status == InvoiceWorkflowStatus.REVIEW_REQUIRED)
    failed = sum(1 for r in invoice_results if r.status == InvoiceWorkflowStatus.FAILED)
    skipped = sum(1 for r in invoice_results if r.status == InvoiceWorkflowStatus.SKIPPED)

    errors = tuple(
        error
        for result in invoice_results
        if result.status == InvoiceWorkflowStatus.FAILED
        for error in result.errors
    )

    return BatchWorkflowResult(
        tenant_id=request.tenant_id,
        batch_id=request.batch_id,
        correlation_id=request.correlation_id,
        status=_aggregate_batch_status(invoice_results),
        started_at=started_at,
        completed_at=orchestration_utc_now(),
        invoice_results=invoice_results,
        total_documents=len(invoice_results),
        successful_documents=successful,
        review_required_documents=review_required,
        failed_documents=failed,
        skipped_documents=skipped,
        errors=errors,
    )
