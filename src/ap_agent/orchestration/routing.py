"""Phase 8 transition graph, result routing and failure classification.

Source: notebook cell 91 ("PHASE 8 — CELL 2", "Transition graph, result
routing and retry classification"), active-definition table extended for
M9 (docs/modularisation_map.md). Ported verbatim: the legal successor
graph, status-normalization text sets, failure-classification name/message
markers, and `route_stage_result`'s branch order and messages.

`resolve_result_status` / `result_requires_review` / `result_review_reasons`
keep the notebook's duck-typed `getattr` probing (`status`, `event.status`,
`review_required`, `invoice_record.review_reasons`, ...) rather than
switching to `isinstance` checks against the M3–M8 result types, because
Phase 8 routes *any* stage's result uniformly and several of those result
types (`IngestionResult`, `PreprocessingResult`, `OCRDocumentResult`,
`NormalizationResult`, `FinancialValidationResult`, `MatchingResult`,
`MemoryPersistenceStageResult`) expose the review/status information under
different attribute names. This is not the notebook's namespace-collision
problem (CLAUDE.md §4): it is one small, explicitly-imported module
examining foreign objects' public attributes, not two same-named globals
overwriting each other.
"""

from __future__ import annotations

from enum import Enum
from typing import Any, Optional

from ap_agent.models.memory import InvoiceMemoryBundle
from ap_agent.models.orchestration import (
    OrchestrationConfig,
    OrchestrationFailureClass,
    OrchestrationStage,
    ORCHESTRATION_PROCESSING_ORDER,
    ORCHESTRATION_TERMINAL_STAGES,
    WorkflowRoute,
    calculate_retry_delay_seconds,
)

__all__ = [
    "StageRoutingDecision",
    "ORCHESTRATION_SUCCESSORS",
    "next_processing_stage",
    "is_allowed_stage_transition",
    "validate_stage_transition",
    "orchestration_text",
    "resolve_result_status",
    "result_requires_review",
    "append_unique_orchestration_reason",
    "result_review_reasons",
    "merge_review_reasons",
    "classify_orchestration_failure",
    "failure_is_retryable",
    "route_stage_result",
]


# ------------------------------------------------------------
# Routing decision contract (notebook cell 91, verbatim)
# ------------------------------------------------------------


from dataclasses import dataclass


@dataclass(frozen=True)
class StageRoutingDecision:
    current_stage: OrchestrationStage
    observed_status: str

    route: WorkflowRoute
    next_stage: Optional[OrchestrationStage]

    failure_class: Optional[OrchestrationFailureClass]

    review_required: bool
    review_reasons: tuple[str, ...]

    retry_delay_seconds: float

    message: str


# ------------------------------------------------------------
# Legal workflow graph (notebook cell 91, verbatim)
# ------------------------------------------------------------

ORCHESTRATION_SUCCESSORS = {
    OrchestrationStage.INGESTION: OrchestrationStage.PREPROCESSING,
    OrchestrationStage.PREPROCESSING: OrchestrationStage.OCR,
    OrchestrationStage.OCR: OrchestrationStage.NORMALIZATION,
    OrchestrationStage.NORMALIZATION: OrchestrationStage.FINANCIAL_VALIDATION,
    OrchestrationStage.FINANCIAL_VALIDATION: OrchestrationStage.REFERENCE_MATCHING,
    OrchestrationStage.REFERENCE_MATCHING: OrchestrationStage.MEMORY_PERSISTENCE,
    OrchestrationStage.MEMORY_PERSISTENCE: None,
    OrchestrationStage.HUMAN_REVIEW: None,
    OrchestrationStage.COMPLETED: None,
}


def next_processing_stage(
    current_stage: OrchestrationStage,
) -> Optional[OrchestrationStage]:
    if current_stage not in ORCHESTRATION_SUCCESSORS:
        raise ValueError(f"Unknown orchestration stage: {current_stage!r}")

    return ORCHESTRATION_SUCCESSORS[current_stage]


def is_allowed_stage_transition(
    current_stage: OrchestrationStage,
    proposed_stage: OrchestrationStage,
) -> bool:
    """
    Return whether the proposed stage transition is legal.

    Retrying the same processing stage is permitted.
    Human-review and completed states are terminal.
    """
    if current_stage in (OrchestrationStage.HUMAN_REVIEW, OrchestrationStage.COMPLETED):
        return False

    if proposed_stage == current_stage and current_stage in ORCHESTRATION_PROCESSING_ORDER:
        return True

    expected_successor = next_processing_stage(current_stage)

    if proposed_stage == expected_successor:
        return True

    if (
        current_stage == OrchestrationStage.MEMORY_PERSISTENCE
        and proposed_stage in ORCHESTRATION_TERMINAL_STAGES
    ):
        return True

    return False


def validate_stage_transition(
    current_stage: OrchestrationStage,
    proposed_stage: OrchestrationStage,
) -> None:
    if not is_allowed_stage_transition(current_stage, proposed_stage):
        raise ValueError(
            f"Illegal orchestration transition: {current_stage.value} -> {proposed_stage.value}"
        )


# ------------------------------------------------------------
# Status normalization (notebook cell 91, verbatim)
# ------------------------------------------------------------

SUCCESS_STATUS_TEXTS = {
    "SUCCEEDED",
    "COMPLETED",
    "CREATED",
    "UPDATED",
    "IDEMPOTENT",
    "ACCEPTED",
}

REVIEW_STATUS_TEXTS = {"REVIEW_REQUIRED"}

FAILED_STATUS_TEXTS = {"FAILED", "CONFLICT", "REJECTED"}

SKIPPED_STATUS_TEXTS = {"SKIPPED", "DUPLICATE"}


def orchestration_text(value: Any) -> str:
    if value is None:
        return ""

    if isinstance(value, Enum):
        return str(value.value).strip()

    enum_value = getattr(value, "value", None)

    if enum_value is not None:
        return str(enum_value).strip()

    return str(value).strip()


def resolve_result_status(result: Any) -> str:
    """
    Resolve the final status of a phase result without assuming
    that all phases use the same status attribute.
    """
    if result is None:
        return "FAILED"

    candidates = (
        getattr(result, "status", None),
        getattr(getattr(result, "event", None), "status", None),
        getattr(result, "operation_status", None),
        getattr(result, "disposition", None),
    )

    for candidate in candidates:
        status_text = orchestration_text(candidate).upper()

        if not status_text:
            continue

        if status_text in SUCCESS_STATUS_TEXTS:
            return "SUCCEEDED"

        if status_text in REVIEW_STATUS_TEXTS:
            return "REVIEW_REQUIRED"

        if status_text in FAILED_STATUS_TEXTS:
            return "FAILED"

        if status_text in SKIPPED_STATUS_TEXTS:
            return "SKIPPED"

        return status_text

    if isinstance(result, InvoiceMemoryBundle):
        return "SUCCEEDED"

    return "UNKNOWN"


# ------------------------------------------------------------
# Review-state propagation (notebook cell 91, verbatim)
# ------------------------------------------------------------


def result_requires_review(result: Any) -> bool:
    if result is None:
        return False

    direct_review = bool(
        getattr(result, "review_required", False) or getattr(result, "requires_review", False)
    )

    event = getattr(result, "event", None)

    event_review = bool(
        getattr(event, "review_required", False) or getattr(event, "requires_review", False)
    )

    record = getattr(result, "invoice_record", None)

    record_review = bool(getattr(record, "review_required", False))

    memory_record = getattr(result, "memory_record", None)

    memory_review = bool(getattr(memory_record, "review_required", False))

    status_review = resolve_result_status(result) == "REVIEW_REQUIRED"

    return bool(direct_review or event_review or record_review or memory_review or status_review)


def append_unique_orchestration_reason(reasons: list[str], reason: Any) -> None:
    reason_text = orchestration_text(reason).strip()

    if reason_text and reason_text not in reasons:
        reasons.append(reason_text)


def result_review_reasons(result: Any) -> tuple[str, ...]:
    if result is None:
        return tuple()

    collected_reasons: list[str] = []

    reason_sources = (
        getattr(result, "review_reasons", ()),
        getattr(getattr(result, "invoice_record", None), "review_reasons", ()),
        getattr(getattr(result, "memory_record", None), "review_reasons", ()),
    )

    for reason_source in reason_sources:
        for reason in reason_source or ():
            append_unique_orchestration_reason(collected_reasons, reason)

    return tuple(collected_reasons)


def merge_review_reasons(*reason_groups: tuple[str, ...]) -> tuple[str, ...]:
    merged: list[str] = []

    for reason_group in reason_groups:
        for reason in reason_group:
            append_unique_orchestration_reason(merged, reason)

    return tuple(merged)


# ------------------------------------------------------------
# Failure classification (notebook cell 91, verbatim)
# ------------------------------------------------------------

TRANSIENT_EXCEPTION_NAMES = {
    "OperationalError",
    "InterfaceError",
    "ConnectionError",
    "ConnectionTimeout",
    "PoolTimeout",
    "ReadTimeout",
    "WriteTimeout",
    "ServiceUnavailableError",
    "RateLimitError",
    "TemporaryError",
}

INTEGRITY_NAME_MARKERS = (
    "Integrity",
    "HashMismatch",
    "Checksum",
    "Collision",
    "Tamper",
    "IdentityMismatch",
    "Isolation",
)

POLICY_NAME_MARKERS = (
    "Policy",
    "Approval",
    "Authorization",
    "Permission",
)

PERMANENT_EXCEPTION_TYPES = (
    FileNotFoundError,
    PermissionError,
    TypeError,
    ValueError,
    UnicodeError,
    NotImplementedError,
)

PERMANENT_MESSAGE_MARKERS = (
    "authentication failed",
    "password authentication failed",
    "permission denied",
    "does not exist",
    "unsupported media type",
    "invalid document",
    "invalid hash",
)

TRANSIENT_MESSAGE_MARKERS = (
    "temporarily unavailable",
    "connection reset",
    "connection refused",
    "connection timed out",
    "timeout",
    "too many requests",
    "rate limit",
    "service unavailable",
)


def classify_orchestration_failure(error: BaseException) -> OrchestrationFailureClass:
    error_name = type(error).__name__
    error_message = str(error).lower()

    if any(marker.lower() in error_name.lower() for marker in INTEGRITY_NAME_MARKERS):
        return OrchestrationFailureClass.INTEGRITY

    if any(marker.lower() in error_name.lower() for marker in POLICY_NAME_MARKERS):
        return OrchestrationFailureClass.POLICY

    if any(marker in error_message for marker in PERMANENT_MESSAGE_MARKERS):
        return OrchestrationFailureClass.PERMANENT

    if isinstance(error, AssertionError):
        return OrchestrationFailureClass.INTEGRITY

    if isinstance(error, PERMANENT_EXCEPTION_TYPES):
        return OrchestrationFailureClass.PERMANENT

    if isinstance(error, (TimeoutError, ConnectionError)):
        return OrchestrationFailureClass.TRANSIENT

    if error_name in TRANSIENT_EXCEPTION_NAMES:
        return OrchestrationFailureClass.TRANSIENT

    if any(marker in error_message for marker in TRANSIENT_MESSAGE_MARKERS):
        return OrchestrationFailureClass.TRANSIENT

    return OrchestrationFailureClass.UNKNOWN


def failure_is_retryable(
    failure_class: OrchestrationFailureClass,
    attempt_number: int,
    config: OrchestrationConfig,
) -> bool:
    policy = config.retry_policy

    if attempt_number >= policy.maximum_attempts:
        return False

    if failure_class == OrchestrationFailureClass.TRANSIENT:
        return bool(policy.retry_transient_failures)

    if failure_class == OrchestrationFailureClass.INTEGRITY:
        return bool(policy.retry_integrity_failures)

    if failure_class == OrchestrationFailureClass.POLICY:
        return bool(policy.retry_policy_failures)

    if failure_class == OrchestrationFailureClass.PERMANENT:
        return bool(policy.retry_permanent_failures)

    return False


# ------------------------------------------------------------
# Stage-result routing (notebook cell 91, verbatim branch order/messages)
# ------------------------------------------------------------


def route_stage_result(
    stage: OrchestrationStage,
    result: Any,
    attempt_number: int,
    config: OrchestrationConfig,
    failure_class: Optional[OrchestrationFailureClass] = None,
) -> StageRoutingDecision:

    observed_status = resolve_result_status(result)

    review_required = result_requires_review(result)
    review_reasons = result_review_reasons(result)

    if observed_status == "SKIPPED":
        return StageRoutingDecision(
            current_stage=stage,
            observed_status=observed_status,
            route=WorkflowRoute.SKIP_IDEMPOTENT,
            next_stage=None,
            failure_class=None,
            review_required=review_required,
            review_reasons=review_reasons,
            retry_delay_seconds=0.0,
            message="Stage returned an idempotent or duplicate outcome.",
        )

    if observed_status == "FAILED":
        resolved_failure_class = failure_class or OrchestrationFailureClass.UNKNOWN

        if failure_is_retryable(resolved_failure_class, attempt_number, config):
            next_attempt = attempt_number + 1

            return StageRoutingDecision(
                current_stage=stage,
                observed_status=observed_status,
                route=WorkflowRoute.RETRY,
                next_stage=stage,
                failure_class=resolved_failure_class,
                review_required=review_required,
                review_reasons=review_reasons,
                retry_delay_seconds=calculate_retry_delay_seconds(
                    next_attempt, config.retry_policy
                ),
                message="Transient stage failure may be retried.",
            )

        return StageRoutingDecision(
            current_stage=stage,
            observed_status=observed_status,
            route=WorkflowRoute.STOP_FAILED,
            next_stage=None,
            failure_class=resolved_failure_class,
            review_required=review_required,
            review_reasons=review_reasons,
            retry_delay_seconds=0.0,
            message="Stage failure is not retryable or retry attempts are exhausted.",
        )

    if observed_status == "UNKNOWN":
        return StageRoutingDecision(
            current_stage=stage,
            observed_status=observed_status,
            route=WorkflowRoute.STOP_FAILED,
            next_stage=None,
            failure_class=OrchestrationFailureClass.INTEGRITY,
            review_required=True,
            review_reasons=("UNKNOWN_STAGE_RESULT_STATUS",),
            retry_delay_seconds=0.0,
            message="Unknown stage result status failed closed.",
        )

    if stage == OrchestrationStage.MEMORY_PERSISTENCE:
        if review_required:
            return StageRoutingDecision(
                current_stage=stage,
                observed_status=observed_status,
                route=WorkflowRoute.ROUTE_TO_REVIEW,
                next_stage=OrchestrationStage.HUMAN_REVIEW,
                failure_class=None,
                review_required=True,
                review_reasons=review_reasons,
                retry_delay_seconds=0.0,
                message="Persisted workflow requires human review.",
            )

        return StageRoutingDecision(
            current_stage=stage,
            observed_status=observed_status,
            route=WorkflowRoute.COMPLETE,
            next_stage=OrchestrationStage.COMPLETED,
            failure_class=None,
            review_required=False,
            review_reasons=tuple(),
            retry_delay_seconds=0.0,
            message="Persisted workflow completed without review.",
        )

    successor = next_processing_stage(stage)

    if successor is None:
        return StageRoutingDecision(
            current_stage=stage,
            observed_status=observed_status,
            route=WorkflowRoute.STOP_FAILED,
            next_stage=None,
            failure_class=OrchestrationFailureClass.INTEGRITY,
            review_required=True,
            review_reasons=("MISSING_STAGE_SUCCESSOR",),
            retry_delay_seconds=0.0,
            message="Processing stage has no legal successor.",
        )

    if review_required and not config.continue_after_review_required:
        return StageRoutingDecision(
            current_stage=stage,
            observed_status=observed_status,
            route=WorkflowRoute.ROUTE_TO_REVIEW,
            next_stage=OrchestrationStage.HUMAN_REVIEW,
            failure_class=None,
            review_required=True,
            review_reasons=review_reasons,
            retry_delay_seconds=0.0,
            message="Workflow routed immediately to review by configuration.",
        )

    return StageRoutingDecision(
        current_stage=stage,
        observed_status=observed_status,
        route=WorkflowRoute.CONTINUE,
        next_stage=successor,
        failure_class=None,
        review_required=review_required,
        review_reasons=review_reasons,
        retry_delay_seconds=0.0,
        message="Stage completed and workflow may continue.",
    )
