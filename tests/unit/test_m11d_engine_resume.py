"""`resume_invoice_workflow`: the engine genuinely starts at the restart stage."""

from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest

from ap_agent.models.orchestration import (
    InvoiceWorkflowRequest,
    InvoiceWorkflowStatus,
    OrchestrationEventType,
    OrchestrationStage as S,
    default_orchestration_config,
)
from ap_agent.orchestration.engine import resume_invoice_workflow
from datetime import datetime, timezone
from pathlib import Path
from tests.support.synthetic_orchestration_handlers import build_synthetic_handler_registry

pytestmark = pytest.mark.unit

ORDER = [S.INGESTION, S.PREPROCESSING, S.OCR, S.NORMALIZATION, S.FINANCIAL_VALIDATION, S.REFERENCE_MATCHING,
         S.MEMORY_PERSISTENCE]


def _request():
    return InvoiceWorkflowRequest(
        tenant_id=uuid4(), batch_id=uuid4(), correlation_id=uuid4(), source_path=Path("x.pdf"),
        source_channel="TEST", submitted_at=datetime.now(timezone.utc),
    )


def _upstream(document_id, stages):
    return {stage: SimpleNamespace(document_id=document_id, result_id=uuid4()) for stage in stages}


@pytest.mark.parametrize(
    ("restart", "required", "expected_run"),
    [
        (S.FINANCIAL_VALIDATION, [S.NORMALIZATION], ORDER[4:]),
        (S.REFERENCE_MATCHING, [S.NORMALIZATION, S.FINANCIAL_VALIDATION], ORDER[5:]),
        (S.MEMORY_PERSISTENCE, [S.NORMALIZATION, S.FINANCIAL_VALIDATION, S.REFERENCE_MATCHING], ORDER[6:]),
    ],
)
def test_only_the_restart_stage_and_later_stages_execute(restart, required, expected_run):
    document_id = uuid4()
    registry, attempts = build_synthetic_handler_registry(document_id)

    result = resume_invoice_workflow(
        _request(), registry, default_orchestration_config(), restart_stage=restart,
        upstream_results=_upstream(document_id, required),
    )

    assert result.status == InvoiceWorkflowStatus.SUCCEEDED
    assert [record.stage for record in result.stage_records] == expected_run
    assert {stage for stage, count in attempts.items() if count} == set(expected_run)
    assert result.events[0].event_type == OrchestrationEventType.WORKFLOW_RESUMED
    assert result.events[0].stage == restart
    assert not any(event.event_type == OrchestrationEventType.WORKFLOW_STARTED for event in result.events)


@pytest.mark.parametrize(
    ("restart", "given", "message"),
    [
        (S.INGESTION, [], "not a valid resume restart stage"),
        (S.OCR, [], "not a valid resume restart stage"),
        (S.FINANCIAL_VALIDATION, [], "requires upstream results for: NORMALIZATION"),
        (S.REFERENCE_MATCHING, [S.NORMALIZATION], "FINANCIAL_VALIDATION"),
        (S.MEMORY_PERSISTENCE, [S.NORMALIZATION, S.FINANCIAL_VALIDATION], "REFERENCE_MATCHING"),
        (S.FINANCIAL_VALIDATION, [S.NORMALIZATION, S.OCR], "must not be given: OCR"),
    ],
)
def test_missing_or_surplus_upstream_state_is_refused_before_anything_runs(restart, given, message):
    document_id = uuid4()
    registry, attempts = build_synthetic_handler_registry(document_id)

    with pytest.raises(ValueError, match=message):
        resume_invoice_workflow(
            _request(), registry, default_orchestration_config(), restart_stage=restart,
            upstream_results=_upstream(document_id, given),
        )

    assert sum(attempts.values()) == 0


def test_upstream_results_for_different_documents_are_refused():
    registry, _ = build_synthetic_handler_registry(uuid4())
    upstream = {
        S.NORMALIZATION: SimpleNamespace(document_id=uuid4(), result_id=uuid4()),
        S.FINANCIAL_VALIDATION: SimpleNamespace(document_id=uuid4(), result_id=uuid4()),
    }

    with pytest.raises(AssertionError, match="different document"):
        resume_invoice_workflow(
            _request(), registry, default_orchestration_config(), restart_stage=S.REFERENCE_MATCHING,
            upstream_results=upstream,
        )


def test_a_resumed_stage_that_still_needs_review_routes_to_review():
    document_id = uuid4()
    registry, _ = build_synthetic_handler_registry(document_id, review_stage=S.REFERENCE_MATCHING)

    result = resume_invoice_workflow(
        _request(), registry, default_orchestration_config(), restart_stage=S.REFERENCE_MATCHING,
        upstream_results=_upstream(document_id, [S.NORMALIZATION, S.FINANCIAL_VALIDATION]),
    )

    assert result.status == InvoiceWorkflowStatus.REVIEW_REQUIRED and result.review_required


def test_a_resumed_stage_failure_keeps_structured_failure_handling():
    document_id = uuid4()
    registry, _ = build_synthetic_handler_registry(document_id, structured_failure_stage=S.FINANCIAL_VALIDATION)

    result = resume_invoice_workflow(
        _request(), registry, default_orchestration_config(), restart_stage=S.FINANCIAL_VALIDATION,
        upstream_results=_upstream(document_id, [S.NORMALIZATION]),
    )

    assert result.status == InvoiceWorkflowStatus.FAILED
    assert [record.stage for record in result.stage_records][-1] == S.FINANCIAL_VALIDATION
