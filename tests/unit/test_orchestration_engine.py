"""M9 unit tests: the framework-neutral single-invoice execution engine
(ap_agent.orchestration.engine), task §15 "Deterministic event IDs" /
"Handler registry completeness" / "Context result retrieval" / "Document
identity continuity" / "Source-hash continuity" / "Structured failure
diagnostics" / "No downstream execution after failure", and task §15's
"Synthetic orchestration tests" subsection (all-success, inherited-review,
transient retry and recovery, retry exhaustion, permanent failure,
integrity failure, structured FAILED phase result, idempotent skip).
"""

from __future__ import annotations

from pathlib import Path
from uuid import NAMESPACE_URL, uuid4, uuid5

import pytest

from ap_agent.models.orchestration import (
    InvoiceWorkflowRequest,
    InvoiceWorkflowStatus,
    OrchestrationEventType,
    OrchestrationFailureClass,
    OrchestrationStage,
    ORCHESTRATION_PROCESSING_ORDER,
    StageExecutionStatus,
    default_orchestration_config,
    orchestration_utc_now,
)
from ap_agent.orchestration.engine import (
    StageHandlerRegistry,
    WorkflowExecutionContext,
    execute_invoice_workflow,
)
from tests.support.synthetic_orchestration_handlers import build_synthetic_handler_registry

pytestmark = [pytest.mark.unit]

CONFIG = default_orchestration_config()

TEST_DOCUMENT_ID = uuid5(NAMESPACE_URL, "ap-agent/test/orchestration-engine-document")


def _request() -> InvoiceWorkflowRequest:
    return InvoiceWorkflowRequest(
        tenant_id=uuid4(),
        batch_id=uuid4(),
        correlation_id=uuid4(),
        source_path=Path("/synthetic/invoice.pdf"),
        source_channel="SYNTHETIC_TEST",
        submitted_at=orchestration_utc_now(),
    )


# ------------------------------------------------------------
# Handler registry completeness
# ------------------------------------------------------------


def test_registry_rejects_a_duplicate_stage_registration():
    registry, _ = build_synthetic_handler_registry(TEST_DOCUMENT_ID)
    duplicated = registry.handlers + (registry.handlers[0],)

    with pytest.raises(ValueError, match="registered more than once"):
        StageHandlerRegistry(handlers=duplicated)


def test_registry_rejects_a_missing_stage():
    registry, _ = build_synthetic_handler_registry(TEST_DOCUMENT_ID)
    incomplete = registry.handlers[:-1]

    with pytest.raises(ValueError, match="does not match the processing graph"):
        StageHandlerRegistry(handlers=incomplete)


def test_registry_get_raises_for_an_unregistered_stage():
    registry, _ = build_synthetic_handler_registry(TEST_DOCUMENT_ID)

    with pytest.raises(KeyError):
        registry.get("NOT_A_STAGE")  # type: ignore[arg-type]


# ------------------------------------------------------------
# Context result retrieval
# ------------------------------------------------------------


def test_context_result_for_raises_before_the_stage_has_run():
    context = WorkflowExecutionContext(request=_request(), config=CONFIG, started_at=orchestration_utc_now())

    with pytest.raises(RuntimeError, match="does not contain a result"):
        context.result_for(OrchestrationStage.INGESTION)


def test_context_result_for_returns_the_stored_stage_result():
    context = WorkflowExecutionContext(request=_request(), config=CONFIG, started_at=orchestration_utc_now())
    context.stage_results[OrchestrationStage.INGESTION] = "sentinel"

    assert context.result_for(OrchestrationStage.INGESTION) == "sentinel"


# ------------------------------------------------------------
# All-success synthetic workflow
# ------------------------------------------------------------


def test_all_success_workflow_completes_and_visits_every_stage_once():
    registry, attempts = build_synthetic_handler_registry(TEST_DOCUMENT_ID)

    result = execute_invoice_workflow(request=_request(), handler_registry=registry, config=CONFIG)

    assert result.status == InvoiceWorkflowStatus.SUCCEEDED
    assert result.current_stage == OrchestrationStage.COMPLETED
    assert result.terminal_route == "COMPLETED"
    assert len(result.stage_records) == len(ORCHESTRATION_PROCESSING_ORDER)
    assert all(count == 1 for count in attempts.values())
    assert result.document_id == TEST_DOCUMENT_ID
    assert result.errors == ()
    assert not result.review_required


def test_deterministic_event_ids_reproduce_across_a_clean_rerun():
    request = _request()
    registry_one, _ = build_synthetic_handler_registry(TEST_DOCUMENT_ID)
    registry_two, _ = build_synthetic_handler_registry(TEST_DOCUMENT_ID)

    result_one = execute_invoice_workflow(request=request, handler_registry=registry_one, config=CONFIG)
    result_two = execute_invoice_workflow(request=request, handler_registry=registry_two, config=CONFIG)

    ids_one = [event.event_id for event in result_one.events]
    ids_two = [event.event_id for event in result_two.events]

    assert ids_one == ids_two
    assert len(ids_one) == len(set(ids_one))


def test_workflow_started_event_is_recorded_first():
    registry, _ = build_synthetic_handler_registry(TEST_DOCUMENT_ID)

    result = execute_invoice_workflow(request=_request(), handler_registry=registry, config=CONFIG)

    assert result.events[0].event_type == OrchestrationEventType.WORKFLOW_STARTED
    assert result.events[-1].event_type == OrchestrationEventType.WORKFLOW_COMPLETED


def test_event_sink_receives_every_event_live():
    from tests.support.synthetic_orchestration_handlers import build_synthetic_handler_registry as _build

    registry, _ = _build(TEST_DOCUMENT_ID)
    received = []

    result = execute_invoice_workflow(
        request=_request(),
        handler_registry=registry,
        config=CONFIG,
        event_sink=received.append,
    )

    assert [event.event_id for event in received] == [event.event_id for event in result.events]


def test_a_raising_event_sink_does_not_break_execution():
    registry, _ = build_synthetic_handler_registry(TEST_DOCUMENT_ID)

    def broken_sink(event):
        raise RuntimeError("observer bug")

    result = execute_invoice_workflow(
        request=_request(), handler_registry=registry, config=CONFIG, event_sink=broken_sink
    )

    assert result.status == InvoiceWorkflowStatus.SUCCEEDED


# ------------------------------------------------------------
# Review propagation through later phases (inherited-review workflow)
# ------------------------------------------------------------


def test_review_at_ocr_propagates_through_to_human_review():
    registry, attempts = build_synthetic_handler_registry(TEST_DOCUMENT_ID, review_stage=OrchestrationStage.OCR)

    result = execute_invoice_workflow(request=_request(), handler_registry=registry, config=CONFIG)

    assert result.status == InvoiceWorkflowStatus.REVIEW_REQUIRED
    assert result.current_stage == OrchestrationStage.HUMAN_REVIEW
    assert result.terminal_route == "HUMAN_REVIEW"
    assert result.review_required is True
    assert "OCR_SYNTHETIC_REVIEW" in result.review_reasons
    assert all(count == 1 for count in attempts.values())


def test_continue_after_review_required_runs_memory_persistence_before_human_review():
    """task §9: 'With continue_after_review_required=True, processing
    must continue through memory persistence before reaching
    HUMAN_REVIEW.'"""

    registry, _ = build_synthetic_handler_registry(TEST_DOCUMENT_ID, review_stage=OrchestrationStage.OCR)

    result = execute_invoice_workflow(request=_request(), handler_registry=registry, config=CONFIG)

    visited_stages = [record.stage for record in result.stage_records]
    assert OrchestrationStage.MEMORY_PERSISTENCE in visited_stages
    assert result.memory_bundle is not None
    assert result.current_stage == OrchestrationStage.HUMAN_REVIEW


def test_review_required_is_not_a_failed_workflow():
    registry, _ = build_synthetic_handler_registry(TEST_DOCUMENT_ID, review_stage=OrchestrationStage.NORMALIZATION)

    result = execute_invoice_workflow(request=_request(), handler_registry=registry, config=CONFIG)

    assert result.status != InvoiceWorkflowStatus.FAILED
    assert result.status == InvoiceWorkflowStatus.REVIEW_REQUIRED
    assert result.errors == ()


def test_memory_persistence_cannot_downgrade_an_inherited_review():
    """Even though the synthetic memory-persistence handler itself
    reports SUCCEEDED, an earlier stage's review requirement must survive
    (task §9: 'Review-state non-downgrade')."""

    registry, _ = build_synthetic_handler_registry(
        TEST_DOCUMENT_ID, review_stage=OrchestrationStage.FINANCIAL_VALIDATION
    )

    result = execute_invoice_workflow(request=_request(), handler_registry=registry, config=CONFIG)

    assert result.status == InvoiceWorkflowStatus.REVIEW_REQUIRED
    assert result.current_stage == OrchestrationStage.HUMAN_REVIEW
    assert "FINANCIAL_VALIDATION_SYNTHETIC_REVIEW" in result.review_reasons


# ------------------------------------------------------------
# Transient retry and recovery / retry exhaustion
# ------------------------------------------------------------


def test_transient_failure_retries_once_then_succeeds():
    registry, attempts = build_synthetic_handler_registry(
        TEST_DOCUMENT_ID, transient_failure_stage=OrchestrationStage.OCR
    )

    result = execute_invoice_workflow(request=_request(), handler_registry=registry, config=CONFIG)

    assert result.status == InvoiceWorkflowStatus.SUCCEEDED
    assert attempts[OrchestrationStage.OCR] == 2
    assert len(result.stage_records) == len(ORCHESTRATION_PROCESSING_ORDER) + 1

    retry_records = [r for r in result.stage_records if r.stage == OrchestrationStage.OCR]
    assert retry_records[0].status == StageExecutionStatus.RETRY_SCHEDULED
    assert retry_records[1].status == StageExecutionStatus.SUCCEEDED


def test_retry_waiter_is_invoked_with_the_computed_delay():
    registry, _ = build_synthetic_handler_registry(TEST_DOCUMENT_ID, transient_failure_stage=OrchestrationStage.OCR)
    recorded_delays = []

    execute_invoice_workflow(
        request=_request(),
        handler_registry=registry,
        config=CONFIG,
        retry_waiter=recorded_delays.append,
    )

    assert recorded_delays == [1.0]


def test_retry_exhaustion_stops_the_workflow_as_failed():
    def always_fails(context):
        raise TimeoutError("temporary service timeout")

    handlers = []
    for stage in ORCHESTRATION_PROCESSING_ORDER:
        if stage == OrchestrationStage.OCR:
            handlers.append((stage, always_fails))
        else:
            registry, _ = build_synthetic_handler_registry(TEST_DOCUMENT_ID)
            handlers.append((stage, registry.get(stage)))

    registry = StageHandlerRegistry(handlers=tuple(handlers))

    result = execute_invoice_workflow(request=_request(), handler_registry=registry, config=CONFIG)

    assert result.status == InvoiceWorkflowStatus.FAILED
    assert result.current_stage == OrchestrationStage.OCR
    ocr_attempts = [r for r in result.stage_records if r.stage == OrchestrationStage.OCR]
    assert len(ocr_attempts) == CONFIG.retry_policy.maximum_attempts
    assert ocr_attempts[-1].status == StageExecutionStatus.FAILED
    assert result.errors


# ------------------------------------------------------------
# Permanent / integrity failure; no downstream execution after failure
# ------------------------------------------------------------


def test_permanent_failure_stops_immediately_without_retry():
    registry, attempts = build_synthetic_handler_registry(
        TEST_DOCUMENT_ID, permanent_failure_stage=OrchestrationStage.NORMALIZATION
    )

    result = execute_invoice_workflow(request=_request(), handler_registry=registry, config=CONFIG)

    assert result.status == InvoiceWorkflowStatus.FAILED
    assert result.current_stage == OrchestrationStage.NORMALIZATION
    assert attempts[OrchestrationStage.NORMALIZATION] == 1
    assert attempts[OrchestrationStage.FINANCIAL_VALIDATION] == 0
    assert attempts[OrchestrationStage.REFERENCE_MATCHING] == 0
    assert attempts[OrchestrationStage.MEMORY_PERSISTENCE] == 0


def test_integrity_failure_stops_downstream_execution():
    registry, attempts = build_synthetic_handler_registry(
        TEST_DOCUMENT_ID, integrity_failure_stage=OrchestrationStage.OCR
    )

    result = execute_invoice_workflow(request=_request(), handler_registry=registry, config=CONFIG)

    assert result.status == InvoiceWorkflowStatus.FAILED
    assert result.current_stage == OrchestrationStage.OCR
    assert attempts[OrchestrationStage.OCR] == 1
    assert attempts[OrchestrationStage.NORMALIZATION] == 0
    assert result.errors


# ------------------------------------------------------------
# Structured FAILED phase result (task §12)
# ------------------------------------------------------------


def test_structured_phase_failure_preserves_errors_and_review_reasons():
    registry, attempts = build_synthetic_handler_registry(
        TEST_DOCUMENT_ID, structured_failure_stage=OrchestrationStage.FINANCIAL_VALIDATION
    )

    result = execute_invoice_workflow(request=_request(), handler_registry=registry, config=CONFIG)

    assert result.status == InvoiceWorkflowStatus.FAILED
    assert result.current_stage == OrchestrationStage.FINANCIAL_VALIDATION
    assert attempts[OrchestrationStage.REFERENCE_MATCHING] == 0
    assert attempts[OrchestrationStage.MEMORY_PERSISTENCE] == 0

    assert any("FINANCIAL_VALIDATION_SYNTHETIC_STRUCTURED_ERROR" in error for error in result.errors)
    assert result.errors != ()

    failed_record = result.stage_records[-1]
    assert failed_record.failure_class == OrchestrationFailureClass.INTEGRITY
    assert failed_record.review_required is True
    assert "FINANCIAL_VALIDATION_SYNTHETIC_STRUCTURED_REVIEW" in failed_record.review_reasons


def test_structured_phase_failure_is_not_classified_as_unknown():
    """The exact defect task §12 calls out: a structured FAILED result
    collapsed to a bare exception must not fall back to UNKNOWN with an
    empty errors tuple."""

    registry, _ = build_synthetic_handler_registry(
        TEST_DOCUMENT_ID, structured_failure_stage=OrchestrationStage.REFERENCE_MATCHING
    )

    result = execute_invoice_workflow(request=_request(), handler_registry=registry, config=CONFIG)

    failed_record = result.stage_records[-1]
    assert failed_record.failure_class != OrchestrationFailureClass.UNKNOWN
    assert result.errors != ()


# ------------------------------------------------------------
# Idempotent skip
# ------------------------------------------------------------


def test_idempotent_skip_completes_the_workflow_as_skipped():
    registry, attempts = build_synthetic_handler_registry(TEST_DOCUMENT_ID, skip_stage=OrchestrationStage.INGESTION)

    result = execute_invoice_workflow(request=_request(), handler_registry=registry, config=CONFIG)

    assert result.status == InvoiceWorkflowStatus.SKIPPED
    assert result.current_stage == OrchestrationStage.COMPLETED
    assert attempts[OrchestrationStage.PREPROCESSING] == 0


# ------------------------------------------------------------
# Document identity / source-hash continuity
# ------------------------------------------------------------


def test_a_stage_returning_a_different_document_id_raises():
    other_document_id = uuid5(NAMESPACE_URL, "ap-agent/test/a-different-document")

    def mismatched_handler(context):
        from types import SimpleNamespace

        return SimpleNamespace(
            document_id=other_document_id, status="SUCCEEDED", review_required=False, review_reasons=()
        )

    registry, _ = build_synthetic_handler_registry(TEST_DOCUMENT_ID)
    handlers = list(registry.handlers)
    handlers[1] = (OrchestrationStage.PREPROCESSING, mismatched_handler)

    broken_registry = StageHandlerRegistry(handlers=tuple(handlers))

    result = execute_invoice_workflow(request=_request(), handler_registry=broken_registry, config=CONFIG)

    assert result.status == InvoiceWorkflowStatus.FAILED
    assert result.current_stage == OrchestrationStage.PREPROCESSING


def test_document_identity_is_stable_across_every_stage():
    registry, _ = build_synthetic_handler_registry(TEST_DOCUMENT_ID)

    result = execute_invoice_workflow(request=_request(), handler_registry=registry, config=CONFIG)

    assert result.document_id == TEST_DOCUMENT_ID
    for record in result.stage_records:
        assert record.result_id is not None
