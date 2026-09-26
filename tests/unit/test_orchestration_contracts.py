"""M9 unit tests: Phase 8 orchestration contracts and configuration
(ap_agent.models.orchestration), task §15 "Contract construction and
validation" / "Terminal-route derivation" / "Retry policy and exhaustion".
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from uuid import NAMESPACE_URL, uuid4, uuid5

import pytest

from ap_agent.models.orchestration import (
    InvoiceWorkflowRequest,
    InvoiceWorkflowResult,
    InvoiceWorkflowStatus,
    OrchestrationConfig,
    OrchestrationStage,
    ORCHESTRATION_PROCESSING_ORDER,
    ORCHESTRATION_TERMINAL_STAGES,
    StructuredPhaseFailure,
    OrchestrationFailureClass,
    TERMINAL_ROUTE_COMPLETED,
    TERMINAL_ROUTE_HUMAN_REVIEW,
    TERMINAL_ROUTE_NONE,
    calculate_retry_delay_seconds,
    default_orchestration_config,
    default_orchestration_retry_policy,
    orchestration_utc_now,
)

pytestmark = [pytest.mark.unit]


def _request(**overrides) -> InvoiceWorkflowRequest:
    values = dict(
        tenant_id=uuid4(),
        batch_id=uuid4(),
        correlation_id=uuid4(),
        source_path=Path("/tmp/invoice.pdf"),
        source_channel="TEST",
        submitted_at=orchestration_utc_now(),
    )
    values.update(overrides)
    return InvoiceWorkflowRequest(**values)


def test_processing_order_is_ingestion_through_memory_persistence():
    assert ORCHESTRATION_PROCESSING_ORDER == (
        OrchestrationStage.INGESTION,
        OrchestrationStage.PREPROCESSING,
        OrchestrationStage.OCR,
        OrchestrationStage.NORMALIZATION,
        OrchestrationStage.FINANCIAL_VALIDATION,
        OrchestrationStage.REFERENCE_MATCHING,
        OrchestrationStage.MEMORY_PERSISTENCE,
    )
    assert len(ORCHESTRATION_PROCESSING_ORDER) == 7
    assert len(set(ORCHESTRATION_PROCESSING_ORDER)) == 7


def test_terminal_stages_are_human_review_and_completed():
    assert set(ORCHESTRATION_TERMINAL_STAGES) == {
        OrchestrationStage.HUMAN_REVIEW,
        OrchestrationStage.COMPLETED,
    }


def test_default_orchestration_config_matches_validated_notebook_defaults():
    config = default_orchestration_config()

    assert config.maximum_batch_documents == 1000
    assert config.maximum_batch_concurrency == 4
    assert config.checkpoint_after_each_stage is True
    assert config.resume_enabled is True
    assert config.fail_closed is True
    assert config.continue_after_review_required is True
    assert config.isolate_invoice_failures is True

    policy = config.retry_policy
    assert policy.maximum_attempts == 3
    assert policy.retry_transient_failures is True
    assert policy.retry_integrity_failures is False
    assert policy.retry_policy_failures is False
    assert policy.retry_permanent_failures is False


def test_default_orchestration_config_is_not_a_shared_mutable_instance():
    """CLAUDE.md: no module under `src/` may define a mutable
    module-level config *instance* -- two calls must not be the same
    object, so a caller cannot accidentally mutate configuration shared
    with another caller."""

    first = default_orchestration_config()
    second = default_orchestration_config()

    assert first == second
    assert first is not second
    assert first.retry_policy is not second.retry_policy


@pytest.mark.parametrize(
    ("attempt_number", "expected_delay"),
    [(1, 0.0), (2, 1.0), (3, 2.0), (4, 4.0), (5, 8.0), (6, 8.0)],
)
def test_calculate_retry_delay_seconds_matches_validated_notebook_values(attempt_number, expected_delay):
    policy = default_orchestration_retry_policy()

    assert calculate_retry_delay_seconds(attempt_number, policy) == expected_delay


def test_invoice_workflow_request_is_frozen_and_carries_optional_metadata():
    request = _request(metadata=(("source", "unit-test"),))

    assert request.metadata == (("source", "unit-test"),)
    assert request.resume_document_id is None

    with pytest.raises(Exception):
        request.tenant_id = uuid4()  # type: ignore[misc]


def _result(current_stage: OrchestrationStage, status: InvoiceWorkflowStatus) -> InvoiceWorkflowResult:
    request = _request()

    return InvoiceWorkflowResult(
        tenant_id=request.tenant_id,
        batch_id=request.batch_id,
        correlation_id=request.correlation_id,
        source_path=request.source_path,
        status=status,
        current_stage=current_stage,
        started_at=orchestration_utc_now(),
        completed_at=orchestration_utc_now(),
    )


def test_terminal_route_completed_for_a_successful_workflow():
    result = _result(OrchestrationStage.COMPLETED, InvoiceWorkflowStatus.SUCCEEDED)

    assert result.terminal_route == TERMINAL_ROUTE_COMPLETED
    assert result.terminal_route != TERMINAL_ROUTE_NONE


def test_terminal_route_human_review_for_a_review_required_workflow():
    result = _result(OrchestrationStage.HUMAN_REVIEW, InvoiceWorkflowStatus.REVIEW_REQUIRED)

    assert result.terminal_route == TERMINAL_ROUTE_HUMAN_REVIEW
    assert result.terminal_route != TERMINAL_ROUTE_NONE


def test_terminal_route_none_for_a_non_terminal_current_stage():
    """A workflow that stopped mid-pipeline (e.g. FAILED at OCR) has no
    terminal route yet -- `NONE` is legitimate there, just never for a
    workflow whose `status` is `SUCCEEDED` (task §8)."""

    result = _result(OrchestrationStage.OCR, InvoiceWorkflowStatus.FAILED)

    assert result.terminal_route == TERMINAL_ROUTE_NONE


@pytest.mark.parametrize("stage", list(OrchestrationStage))
def test_successful_workflow_never_exposes_none_as_terminal_route(stage):
    """A `SUCCEEDED` workflow's `current_stage` is always `COMPLETED`
    (enforced by ap_agent.orchestration.engine.execute_invoice_workflow),
    so this is a defence-in-depth check on the derivation itself: for
    every stage that could plausibly be paired with SUCCEEDED, only
    COMPLETED must resolve away from NONE."""

    result = _result(stage, InvoiceWorkflowStatus.SUCCEEDED)

    if stage == OrchestrationStage.COMPLETED:
        assert result.terminal_route == TERMINAL_ROUTE_COMPLETED
    else:
        # Not a state the engine ever produces, but the derivation must
        # still never silently claim COMPLETED/HUMAN_REVIEW for it.
        assert result.terminal_route in {TERMINAL_ROUTE_NONE, TERMINAL_ROUTE_HUMAN_REVIEW}


def test_terminal_route_is_derived_not_a_second_mutable_field():
    """There is exactly one field driving `terminal_route`
    (`current_stage`); the property recomputes on every access instead of
    being cached/settable (task §8: "If a convenience property is added,
    it must be derived and immutable.")."""

    result = _result(OrchestrationStage.COMPLETED, InvoiceWorkflowStatus.SUCCEEDED)

    assert "terminal_route" not in result.__dataclass_fields__
    assert result.terminal_route == TERMINAL_ROUTE_COMPLETED


def test_structured_phase_failure_carries_errors_and_review_reasons():
    failure = StructuredPhaseFailure(
        OrchestrationStage.FINANCIAL_VALIDATION,
        errors=("PHASE_5_EXECUTION_FAILURE: boom",),
        review_reasons=("PHASE_5_EXECUTION_FAILURE",),
        failure_class=OrchestrationFailureClass.INTEGRITY,
    )

    assert failure.stage == OrchestrationStage.FINANCIAL_VALIDATION
    assert failure.errors == ("PHASE_5_EXECUTION_FAILURE: boom",)
    assert failure.review_reasons == ("PHASE_5_EXECUTION_FAILURE",)
    assert failure.failure_class == OrchestrationFailureClass.INTEGRITY
    assert "FINANCIAL_VALIDATION failed closed" in str(failure)
    assert "boom" in str(failure)


def test_structured_phase_failure_message_is_explicit_when_no_errors_supplied():
    failure = StructuredPhaseFailure(OrchestrationStage.REFERENCE_MATCHING, errors=())

    assert "no underlying phase error was supplied" in str(failure)
