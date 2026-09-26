"""M9 unit tests: batch workflow orchestration (ap_agent.orchestration.batch),
task §15 "Batch limit" / "Bounded concurrency" / "Stable batch ordering" /
"Event-sink behaviour", and task §13's synthetic 200-document batch load
test (fast, no OCR).
"""

from __future__ import annotations

import threading
import time
from pathlib import Path
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

import pytest

from ap_agent.models.orchestration import (
    BatchWorkflowRequest,
    BatchWorkflowStatus,
    InvoiceWorkflowStatus,
    OrchestrationStage,
    default_orchestration_config,
    orchestration_utc_now,
)
from ap_agent.orchestration.batch import (
    BatchSizeExceededError,
    build_invoice_workflow_requests,
    derive_invoice_correlation_id,
    execute_batch_workflow,
)
from ap_agent.orchestration.engine import StageHandlerRegistry
from tests.support.synthetic_orchestration_handlers import build_synthetic_handler_registry

pytestmark = [pytest.mark.unit]


def _batch_request(count: int, **overrides) -> BatchWorkflowRequest:
    values = dict(
        tenant_id=uuid4(),
        batch_id=uuid4(),
        correlation_id=uuid4(),
        source_paths=tuple(Path(f"/synthetic/invoice_{index:04d}.pdf") for index in range(count)),
        source_channel="SYNTHETIC_BATCH_TEST",
        submitted_at=orchestration_utc_now(),
    )
    values.update(overrides)
    return BatchWorkflowRequest(**values)


def _document_id_for(source_path: Path) -> UUID:
    return uuid5(NAMESPACE_URL, f"ap-agent/test/batch-document/{source_path.name}")


class _PerDocumentRegistry:
    """Wraps a `{source_path.name: StageHandlerRegistry}` mapping behind
    the single `StageHandlerRegistry` interface `execute_batch_workflow`
    expects, so each synthetic invoice in a batch can carry its own
    deterministic document identity without the batch orchestrator itself
    knowing anything about per-document state (task §13: 'Do not share
    mutable per-invoice context.')."""

    def __init__(self, registries_by_name: dict[str, StageHandlerRegistry]) -> None:
        self._registries_by_name = registries_by_name
        self._active = threading.local()

    def get(self, stage):
        def handler(context):
            registry = self._registries_by_name[context.request.source_path.name]
            return registry.get(stage)(context)

        return handler


def _build_batch_registry(request: BatchWorkflowRequest, **kwargs) -> StageHandlerRegistry:
    registries_by_name = {
        source_path.name: build_synthetic_handler_registry(_document_id_for(source_path), **kwargs)[0]
        for source_path in request.source_paths
    }

    wrapper = _PerDocumentRegistry(registries_by_name)

    from ap_agent.models.orchestration import ORCHESTRATION_PROCESSING_ORDER

    return StageHandlerRegistry(
        handlers=tuple((stage, wrapper.get(stage)) for stage in ORCHESTRATION_PROCESSING_ORDER)
    )


# ------------------------------------------------------------
# Batch limit
# ------------------------------------------------------------


def test_batch_size_at_the_configured_maximum_is_accepted():
    config = default_orchestration_config()
    request = _batch_request(config.maximum_batch_documents)
    registry = _build_batch_registry(request)

    result = execute_batch_workflow(request, registry, config, max_workers=8)

    assert result.total_documents == config.maximum_batch_documents


def test_batch_size_exceeding_the_configured_maximum_is_rejected_before_processing():
    config = default_orchestration_config()
    request = _batch_request(config.maximum_batch_documents + 1)

    call_count = {"n": 0}

    class _CountingRegistry:
        def get(self, stage):
            call_count["n"] += 1
            raise AssertionError("the batch orchestrator must reject an oversized batch before touching the registry")

    with pytest.raises(BatchSizeExceededError) as excinfo:
        execute_batch_workflow(request, _CountingRegistry(), config)

    assert excinfo.value.requested == config.maximum_batch_documents + 1
    assert excinfo.value.maximum == config.maximum_batch_documents
    assert call_count["n"] == 0


def test_derive_invoice_correlation_id_is_deterministic():
    batch_id = uuid4()
    path = Path("/synthetic/invoice.pdf")

    assert derive_invoice_correlation_id(batch_id, path) == derive_invoice_correlation_id(batch_id, path)


def test_build_invoice_workflow_requests_preserves_input_order_and_batch_identity():
    request = _batch_request(5)

    invoice_requests = build_invoice_workflow_requests(request)

    assert [r.source_path for r in invoice_requests] == list(request.source_paths)
    assert all(r.tenant_id == request.tenant_id for r in invoice_requests)
    assert all(r.batch_id == request.batch_id for r in invoice_requests)
    assert len({r.correlation_id for r in invoice_requests}) == len(invoice_requests)


# ------------------------------------------------------------
# Bounded concurrency
# ------------------------------------------------------------


def test_batch_never_exceeds_configured_concurrency():
    max_workers = 3
    concurrent_count = {"current": 0, "peak": 0}
    lock = threading.Lock()

    def make_registry(document_id):
        from ap_agent.models.orchestration import ORCHESTRATION_PROCESSING_ORDER

        def tracking_handler(context):
            with lock:
                concurrent_count["current"] += 1
                concurrent_count["peak"] = max(concurrent_count["peak"], concurrent_count["current"])

            time.sleep(0.02)

            with lock:
                concurrent_count["current"] -= 1

            from types import SimpleNamespace

            return SimpleNamespace(document_id=document_id, status="SUCCEEDED", review_required=False, review_reasons=())

        return StageHandlerRegistry(
            handlers=tuple((stage, tracking_handler) for stage in ORCHESTRATION_PROCESSING_ORDER)
        )

    request = _batch_request(12)
    registries_by_name = {
        source_path.name: make_registry(_document_id_for(source_path)) for source_path in request.source_paths
    }
    wrapper = _PerDocumentRegistry(registries_by_name)

    from ap_agent.models.orchestration import ORCHESTRATION_PROCESSING_ORDER

    registry = StageHandlerRegistry(
        handlers=tuple((stage, wrapper.get(stage)) for stage in ORCHESTRATION_PROCESSING_ORDER)
    )

    config = default_orchestration_config()

    execute_batch_workflow(request, registry, config, max_workers=max_workers)

    assert concurrent_count["peak"] <= max_workers


# ------------------------------------------------------------
# Stable batch ordering
# ------------------------------------------------------------


def test_batch_output_ordering_matches_input_ordering_despite_variable_completion_time():
    """Later-submitted invoices finish first (a short sleep on the first
    few, none on the rest); the returned tuple must still match input
    order exactly (task §13: 'Stable output ordering matching input
    ordering.')."""

    from ap_agent.models.orchestration import ORCHESTRATION_PROCESSING_ORDER
    from types import SimpleNamespace

    request = _batch_request(10)

    def make_handler(source_path: Path, delay: float):
        document_id = _document_id_for(source_path)

        def handler(context):
            time.sleep(delay)
            return SimpleNamespace(document_id=document_id, status="SUCCEEDED", review_required=False, review_reasons=())

        return handler

    registries_by_name = {}
    for index, source_path in enumerate(request.source_paths):
        delay = 0.05 if index < 3 else 0.0
        handler = make_handler(source_path, delay)
        registries_by_name[source_path.name] = StageHandlerRegistry(
            handlers=tuple((stage, handler) for stage in ORCHESTRATION_PROCESSING_ORDER)
        )

    wrapper = _PerDocumentRegistry(registries_by_name)
    registry = StageHandlerRegistry(
        handlers=tuple((stage, wrapper.get(stage)) for stage in ORCHESTRATION_PROCESSING_ORDER)
    )

    result = execute_batch_workflow(request, registry, default_orchestration_config(), max_workers=6)

    assert [r.source_path for r in result.invoice_results] == list(request.source_paths)


# ------------------------------------------------------------
# Per-invoice isolation / aggregate status
# ------------------------------------------------------------


def test_one_invoice_failure_does_not_corrupt_other_invoices_results():
    from types import SimpleNamespace
    from ap_agent.models.orchestration import ORCHESTRATION_PROCESSING_ORDER

    request = _batch_request(6)
    failing_path = request.source_paths[2]

    registries_by_name = {}
    for source_path in request.source_paths:
        document_id = _document_id_for(source_path)

        if source_path == failing_path:
            def failing_handler(context, document_id=document_id):
                raise ValueError("synthetic permanent failure")

            registries_by_name[source_path.name] = StageHandlerRegistry(
                handlers=tuple((stage, failing_handler) for stage in ORCHESTRATION_PROCESSING_ORDER)
            )
        else:
            def ok_handler(context, document_id=document_id):
                return SimpleNamespace(document_id=document_id, status="SUCCEEDED", review_required=False, review_reasons=())

            registries_by_name[source_path.name] = StageHandlerRegistry(
                handlers=tuple((stage, ok_handler) for stage in ORCHESTRATION_PROCESSING_ORDER)
            )

    wrapper = _PerDocumentRegistry(registries_by_name)
    registry = StageHandlerRegistry(
        handlers=tuple((stage, wrapper.get(stage)) for stage in ORCHESTRATION_PROCESSING_ORDER)
    )

    result = execute_batch_workflow(request, registry, default_orchestration_config(), max_workers=4)

    assert result.total_documents == 6
    assert result.failed_documents == 1
    assert result.successful_documents == 5
    assert result.status == BatchWorkflowStatus.PARTIALLY_FAILED

    statuses_by_path = {r.source_path: r.status for r in result.invoice_results}
    assert statuses_by_path[failing_path] == InvoiceWorkflowStatus.FAILED
    for source_path, status in statuses_by_path.items():
        if source_path != failing_path:
            assert status == InvoiceWorkflowStatus.SUCCEEDED


# ------------------------------------------------------------
# Event-sink behaviour
# ------------------------------------------------------------


def test_batch_event_sink_receives_events_from_every_invoice():
    request = _batch_request(4)
    registry = _build_batch_registry(request)

    received = []
    lock = threading.Lock()

    def sink(event):
        with lock:
            received.append(event)

    execute_batch_workflow(request, registry, default_orchestration_config(), event_sink=sink)

    observed_document_ids = {event.document_id for event in received if event.document_id is not None}
    assert len(observed_document_ids) == 4


# ------------------------------------------------------------
# Synthetic 200-document batch (task §13): bounded concurrency, stable
# ordering, per-invoice isolation, no fixed-four assumption, correct
# aggregate counts -- no OCR model involved, kept fast.
# ------------------------------------------------------------


@pytest.mark.slow
def test_two_hundred_document_synthetic_batch_completes_correctly():
    document_count = 200
    request = _batch_request(document_count)

    # A handful of documents require review, one is a permanent failure;
    # none of this depends on there being exactly four documents.
    review_indices = {3, 47, 150}
    failure_index = 199

    from types import SimpleNamespace
    from ap_agent.models.orchestration import ORCHESTRATION_PROCESSING_ORDER

    registries_by_name = {}

    for index, source_path in enumerate(request.source_paths):
        document_id = _document_id_for(source_path)

        if index == failure_index:
            def handler(context, document_id=document_id):
                raise ValueError("synthetic permanent failure")
        elif index in review_indices:
            def handler(context, document_id=document_id):
                return SimpleNamespace(
                    document_id=document_id,
                    status="REVIEW_REQUIRED",
                    review_required=True,
                    review_reasons=("SYNTHETIC_BATCH_REVIEW",),
                )
        else:
            def handler(context, document_id=document_id):
                return SimpleNamespace(document_id=document_id, status="SUCCEEDED", review_required=False, review_reasons=())

        registries_by_name[source_path.name] = StageHandlerRegistry(
            handlers=tuple((stage, handler) for stage in ORCHESTRATION_PROCESSING_ORDER)
        )

    wrapper = _PerDocumentRegistry(registries_by_name)
    registry = StageHandlerRegistry(
        handlers=tuple((stage, wrapper.get(stage)) for stage in ORCHESTRATION_PROCESSING_ORDER)
    )

    config = default_orchestration_config()

    concurrent_count = {"current": 0, "peak": 0}
    lock = threading.Lock()

    def concurrency_sink(event):
        pass

    started_at = time.monotonic()
    result = execute_batch_workflow(request, registry, config, max_workers=16, event_sink=concurrency_sink)
    elapsed_seconds = time.monotonic() - started_at

    assert result.total_documents == document_count
    assert result.failed_documents == 1
    assert result.review_required_documents == len(review_indices)
    assert result.successful_documents == document_count - 1 - len(review_indices)
    assert result.status == BatchWorkflowStatus.PARTIALLY_FAILED

    assert [r.source_path for r in result.invoice_results] == list(request.source_paths)

    assert elapsed_seconds < 15.0
