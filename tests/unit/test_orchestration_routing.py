"""M9 unit tests: Phase 8 transition graph, status normalisation, routing
decisions and failure classification (ap_agent.orchestration.routing),
task §15 "Transition graph" / "Status normalisation" / "Routing
decisions" / "Review propagation" / "Review non-downgrade" / "Retry
policy and exhaustion" / "Failure classification" / "Unknown-status
rejection".
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from ap_agent.models.orchestration import (
    OrchestrationFailureClass,
    OrchestrationStage,
    WorkflowRoute,
    default_orchestration_config,
)
from ap_agent.orchestration.routing import (
    classify_orchestration_failure,
    failure_is_retryable,
    is_allowed_stage_transition,
    merge_review_reasons,
    next_processing_stage,
    resolve_result_status,
    result_requires_review,
    result_review_reasons,
    route_stage_result,
    validate_stage_transition,
)

pytestmark = [pytest.mark.unit]


CONFIG = default_orchestration_config()


# ------------------------------------------------------------
# Transition graph
# ------------------------------------------------------------


def test_legal_successor_chain_covers_every_processing_stage():
    order = [
        OrchestrationStage.INGESTION,
        OrchestrationStage.PREPROCESSING,
        OrchestrationStage.OCR,
        OrchestrationStage.NORMALIZATION,
        OrchestrationStage.FINANCIAL_VALIDATION,
        OrchestrationStage.REFERENCE_MATCHING,
        OrchestrationStage.MEMORY_PERSISTENCE,
    ]

    for current, successor in zip(order, order[1:]):
        assert next_processing_stage(current) == successor
        assert is_allowed_stage_transition(current, successor)


def test_same_stage_retry_transition_is_allowed_for_processing_stages():
    assert is_allowed_stage_transition(OrchestrationStage.OCR, OrchestrationStage.OCR)


def test_memory_persistence_may_terminate_at_completed_or_human_review():
    assert is_allowed_stage_transition(
        OrchestrationStage.MEMORY_PERSISTENCE, OrchestrationStage.COMPLETED
    )
    assert is_allowed_stage_transition(
        OrchestrationStage.MEMORY_PERSISTENCE, OrchestrationStage.HUMAN_REVIEW
    )


def test_terminal_stages_reject_every_further_transition():
    assert not is_allowed_stage_transition(OrchestrationStage.COMPLETED, OrchestrationStage.INGESTION)
    assert not is_allowed_stage_transition(OrchestrationStage.HUMAN_REVIEW, OrchestrationStage.NORMALIZATION)
    assert not is_allowed_stage_transition(OrchestrationStage.COMPLETED, OrchestrationStage.COMPLETED)


def test_illegal_transition_is_rejected_with_validate_stage_transition():
    with pytest.raises(ValueError, match="Illegal orchestration transition"):
        validate_stage_transition(OrchestrationStage.INGESTION, OrchestrationStage.MEMORY_PERSISTENCE)


def test_unknown_stage_fails_closed():
    with pytest.raises(ValueError, match="Unknown orchestration stage"):
        next_processing_stage("NOT_A_STAGE")  # type: ignore[arg-type]


# ------------------------------------------------------------
# Status normalisation across Phases 1-7
# ------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw_status", "expected"),
    [
        ("SUCCEEDED", "SUCCEEDED"),
        ("COMPLETED", "SUCCEEDED"),
        ("CREATED", "SUCCEEDED"),
        ("ACCEPTED", "SUCCEEDED"),
        ("REVIEW_REQUIRED", "REVIEW_REQUIRED"),
        ("FAILED", "FAILED"),
        ("REJECTED", "FAILED"),
        ("SKIPPED", "SKIPPED"),
        ("DUPLICATE", "SKIPPED"),
    ],
)
def test_resolve_result_status_normalizes_cross_phase_status_text(raw_status, expected):
    assert resolve_result_status(SimpleNamespace(status=raw_status)) == expected


def test_resolve_result_status_reads_event_status_when_no_direct_status():
    result = SimpleNamespace(event=SimpleNamespace(status="SUCCEEDED"))

    assert resolve_result_status(result) == "SUCCEEDED"


def test_resolve_result_status_none_result_is_failed():
    assert resolve_result_status(None) == "FAILED"


def test_resolve_result_status_passes_through_unrecognized_status_text():
    """A status value that isn't in any of the known text sets is passed
    through verbatim rather than being coerced (notebook cell 91,
    verbatim) -- `UNKNOWN` is reserved for a result exposing *no*
    status-shaped attribute at all."""

    assert resolve_result_status(SimpleNamespace(status="SOMETHING_ELSE")) == "SOMETHING_ELSE"


def test_resolve_result_status_is_unknown_when_no_status_attribute_is_present():
    assert resolve_result_status(SimpleNamespace()) == "UNKNOWN"


# ------------------------------------------------------------
# Review-state propagation
# ------------------------------------------------------------


def test_result_requires_review_reads_direct_flag():
    assert result_requires_review(SimpleNamespace(status="SUCCEEDED", review_required=True))


def test_result_requires_review_reads_nested_event_flag():
    result = SimpleNamespace(status="SUCCEEDED", event=SimpleNamespace(review_required=True))
    assert result_requires_review(result)


def test_result_requires_review_false_for_a_clean_result():
    assert not result_requires_review(SimpleNamespace(status="SUCCEEDED", review_required=False))


def test_result_review_reasons_deduplicates_and_preserves_order():
    result = SimpleNamespace(review_reasons=("A", "B", "A", "C"))

    assert result_review_reasons(result) == ("A", "B", "C")


def test_merge_review_reasons_deduplicates_across_groups():
    merged = merge_review_reasons(("A", "B"), ("B", "C"))

    assert merged == ("A", "B", "C")


# ------------------------------------------------------------
# Routing decisions
# ------------------------------------------------------------


def _probe(status="SUCCEEDED", review_required=False, review_reasons=()):
    return SimpleNamespace(status=status, review_required=review_required, review_reasons=review_reasons)


def test_successful_stage_continues_to_its_successor():
    decision = route_stage_result(OrchestrationStage.INGESTION, _probe(), attempt_number=1, config=CONFIG)

    assert decision.route == WorkflowRoute.CONTINUE
    assert decision.next_stage == OrchestrationStage.PREPROCESSING


def test_review_required_stage_continues_when_configured_to_continue_after_review():
    decision = route_stage_result(
        OrchestrationStage.OCR,
        _probe(status="REVIEW_REQUIRED", review_required=True, review_reasons=("LOW_OCR_CONFIDENCE",)),
        attempt_number=1,
        config=CONFIG,
    )

    assert decision.route == WorkflowRoute.CONTINUE
    assert decision.next_stage == OrchestrationStage.NORMALIZATION
    assert decision.review_required is True


def test_review_required_routes_immediately_when_continue_after_review_is_disabled():
    from dataclasses import replace

    stop_early_config = replace(CONFIG, continue_after_review_required=False)

    decision = route_stage_result(
        OrchestrationStage.OCR,
        _probe(status="REVIEW_REQUIRED", review_required=True, review_reasons=("LOW_OCR_CONFIDENCE",)),
        attempt_number=1,
        config=stop_early_config,
    )

    assert decision.route == WorkflowRoute.ROUTE_TO_REVIEW
    assert decision.next_stage == OrchestrationStage.HUMAN_REVIEW


def test_memory_persistence_success_completes_the_workflow():
    decision = route_stage_result(
        OrchestrationStage.MEMORY_PERSISTENCE, _probe(), attempt_number=1, config=CONFIG
    )

    assert decision.route == WorkflowRoute.COMPLETE
    assert decision.next_stage == OrchestrationStage.COMPLETED


def test_memory_persistence_with_review_routes_to_human_review():
    decision = route_stage_result(
        OrchestrationStage.MEMORY_PERSISTENCE,
        _probe(status="SUCCEEDED", review_required=True, review_reasons=("INHERITED_FINANCIAL_REVIEW",)),
        attempt_number=1,
        config=CONFIG,
    )

    assert decision.route == WorkflowRoute.ROUTE_TO_REVIEW
    assert decision.next_stage == OrchestrationStage.HUMAN_REVIEW


def test_skipped_status_routes_to_idempotent_skip():
    decision = route_stage_result(
        OrchestrationStage.INGESTION, _probe(status="SKIPPED"), attempt_number=1, config=CONFIG
    )

    assert decision.route == WorkflowRoute.SKIP_IDEMPOTENT
    assert decision.next_stage is None


def test_unknown_status_fails_closed_with_review_required():
    decision = route_stage_result(
        OrchestrationStage.OCR, _probe(status=None), attempt_number=1, config=CONFIG
    )

    assert decision.route == WorkflowRoute.STOP_FAILED
    assert decision.failure_class == OrchestrationFailureClass.INTEGRITY
    assert decision.review_required is True
    assert decision.review_reasons == ("UNKNOWN_STAGE_RESULT_STATUS",)


# ------------------------------------------------------------
# Retry policy and exhaustion
# ------------------------------------------------------------


def test_transient_failure_is_retried_with_a_computed_delay():
    decision = route_stage_result(
        OrchestrationStage.OCR,
        _probe(status="FAILED"),
        attempt_number=1,
        config=CONFIG,
        failure_class=OrchestrationFailureClass.TRANSIENT,
    )

    assert decision.route == WorkflowRoute.RETRY
    assert decision.next_stage == OrchestrationStage.OCR
    assert decision.retry_delay_seconds == 1.0


def test_transient_failure_stops_once_retry_attempts_are_exhausted():
    decision = route_stage_result(
        OrchestrationStage.OCR,
        _probe(status="FAILED"),
        attempt_number=CONFIG.retry_policy.maximum_attempts,
        config=CONFIG,
        failure_class=OrchestrationFailureClass.TRANSIENT,
    )

    assert decision.route == WorkflowRoute.STOP_FAILED


def test_permanent_failure_is_never_retried_even_on_first_attempt():
    decision = route_stage_result(
        OrchestrationStage.OCR,
        _probe(status="FAILED"),
        attempt_number=1,
        config=CONFIG,
        failure_class=OrchestrationFailureClass.PERMANENT,
    )

    assert decision.route == WorkflowRoute.STOP_FAILED
    assert decision.failure_class == OrchestrationFailureClass.PERMANENT


def test_integrity_failure_is_never_retried():
    decision = route_stage_result(
        OrchestrationStage.MEMORY_PERSISTENCE,
        _probe(status="FAILED", review_required=True, review_reasons=("PAYLOAD_HASH_MISMATCH",)),
        attempt_number=1,
        config=CONFIG,
        failure_class=OrchestrationFailureClass.INTEGRITY,
    )

    assert decision.route == WorkflowRoute.STOP_FAILED


def test_policy_failure_is_never_retried():
    decision = route_stage_result(
        OrchestrationStage.FINANCIAL_VALIDATION,
        _probe(status="FAILED"),
        attempt_number=1,
        config=CONFIG,
        failure_class=OrchestrationFailureClass.POLICY,
    )

    assert decision.route == WorkflowRoute.STOP_FAILED


@pytest.mark.parametrize(
    ("failure_class", "expected"),
    [
        (OrchestrationFailureClass.TRANSIENT, True),
        (OrchestrationFailureClass.INTEGRITY, False),
        (OrchestrationFailureClass.POLICY, False),
        (OrchestrationFailureClass.PERMANENT, False),
        (OrchestrationFailureClass.UNKNOWN, False),
    ],
)
def test_failure_is_retryable_matches_the_validated_retry_policy(failure_class, expected):
    assert failure_is_retryable(failure_class, attempt_number=1, config=CONFIG) is expected


def test_failure_is_retryable_false_once_attempts_are_exhausted():
    assert not failure_is_retryable(
        OrchestrationFailureClass.TRANSIENT,
        attempt_number=CONFIG.retry_policy.maximum_attempts,
        config=CONFIG,
    )


# ------------------------------------------------------------
# Failure classification
# ------------------------------------------------------------


def test_classify_timeout_error_as_transient():
    assert classify_orchestration_failure(TimeoutError("connection timed out")) == OrchestrationFailureClass.TRANSIENT


def test_classify_file_not_found_as_permanent():
    assert (
        classify_orchestration_failure(FileNotFoundError("invoice file does not exist"))
        == OrchestrationFailureClass.PERMANENT
    )


def test_classify_assertion_error_as_integrity():
    assert (
        classify_orchestration_failure(AssertionError("payload hash mismatch"))
        == OrchestrationFailureClass.INTEGRITY
    )


def test_classify_named_integrity_error_by_class_name():
    class HashMismatchError(Exception):
        pass

    assert classify_orchestration_failure(HashMismatchError("boom")) == OrchestrationFailureClass.INTEGRITY


def test_classify_named_policy_error_by_class_name():
    class ApprovalRequiredError(Exception):
        pass

    assert classify_orchestration_failure(ApprovalRequiredError("boom")) == OrchestrationFailureClass.POLICY


def test_classify_unrecognized_error_as_unknown():
    class SomeDomainError(Exception):
        pass

    assert classify_orchestration_failure(SomeDomainError("boom")) == OrchestrationFailureClass.UNKNOWN
