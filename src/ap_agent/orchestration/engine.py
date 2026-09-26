"""Phase 8 framework-neutral single-invoice execution engine.

Source: notebook cell 92 ("PHASE 8 — CELL 3", "Framework-neutral
single-invoice execution engine"), active-definition table extended for M9
(docs/modularisation_map.md). `StageHandlerRegistry`,
`WorkflowExecutionContext`, the deterministic event-ID/-append helpers,
result-identity extraction, review-state merging, stage-record/result
construction and `execute_invoice_workflow`'s control flow are ported
verbatim (same branch order, same `WorkflowRoute` handling, same terminal
results).

Two additions, both required by the M9 task brief and neither reachable by
the notebook's own four real fixtures (§2's golden table has zero FAILED
outcomes, so this path was never exercised there):

  - `execute_invoice_workflow` now recognises
    `ap_agent.models.orchestration.StructuredPhaseFailure` specially. The
    notebook's correction cells 4C/4D re-raise a structured Phase 5/6
    `FAILED` result as a bare `RuntimeError`, which this engine's *generic*
    exception path (unchanged, and still exercised for every other error
    type) would classify as `OrchestrationFailureClass.UNKNOWN` and record
    with `context.errors` holding only one synthesized line — losing the
    phase's own `errors`/`review_reasons`. Task §12 requires the
    diagnostic information to survive; the `StructuredPhaseFailure` branch
    below classifies using the exception's own `failure_class`, preserves
    each of its `errors` verbatim in `context.errors`, and carries its
    `review_reasons` into the stage's routing decision instead of a single
    synthesized reason code. The notebook's own control-flow guarantee
    (stop, don't continue downstream) is unchanged either way.
  - `create_synthetic_handler_registry` and the notebook's own inline
    "deterministic single-invoice validation" assertions are *not* ported
    here: they were notebook self-test scaffolding for this cell, not
    behaviour the orchestration engine itself needs at runtime. The
    equivalent synthetic-workflow assertions live in
    `tests/unit/test_orchestration_engine.py` instead (task §15).
"""

from __future__ import annotations

import re

from dataclasses import dataclass, field, replace
from types import SimpleNamespace
from typing import Any, Callable, Optional
from uuid import NAMESPACE_URL, UUID, uuid5

from ap_agent.models.orchestration import (
    InvoiceWorkflowRequest,
    InvoiceWorkflowResult,
    InvoiceWorkflowStatus,
    OrchestrationConfig,
    OrchestrationEvent,
    OrchestrationEventType,
    OrchestrationFailureClass,
    OrchestrationStage,
    ORCHESTRATION_PROCESSING_ORDER,
    StageExecutionRecord,
    StageExecutionStatus,
    StructuredPhaseFailure,
    WorkflowRoute,
    orchestration_utc_now,
)
from ap_agent.orchestration.routing import (
    StageRoutingDecision,
    classify_orchestration_failure,
    merge_review_reasons,
    route_stage_result,
    validate_stage_transition,
)

__all__ = [
    "StageHandler",
    "RetryWaiter",
    "StageHandlerRegistry",
    "WorkflowExecutionContext",
    "safe_orchestration_error_message",
    "create_orchestration_event_id",
    "append_orchestration_event",
    "extract_result_identifier",
    "extract_result_document_id",
    "register_result_document_identity",
    "merge_decision_review_state",
    "apply_inherited_review_to_memory_route",
    "stage_record_status",
    "build_stage_execution_record",
    "build_invoice_workflow_result",
    "default_retry_waiter",
    "blocking_retry_waiter",
    "execute_invoice_workflow",
]


# ------------------------------------------------------------
# Stage-handler contracts (notebook cell 92, verbatim)
# ------------------------------------------------------------

StageHandler = Callable[["WorkflowExecutionContext"], Any]

RetryWaiter = Callable[[float], None]


@dataclass(frozen=True)
class StageHandlerRegistry:
    handlers: tuple[tuple[OrchestrationStage, StageHandler], ...]

    def __post_init__(self) -> None:
        registered_stages = tuple(stage for stage, _ in self.handlers)

        if len(registered_stages) != len(set(registered_stages)):
            raise ValueError("A stage handler was registered more than once.")

        if set(registered_stages) != set(ORCHESTRATION_PROCESSING_ORDER):
            missing = set(ORCHESTRATION_PROCESSING_ORDER) - set(registered_stages)
            unexpected = set(registered_stages) - set(ORCHESTRATION_PROCESSING_ORDER)

            raise ValueError(
                "Stage-handler registry does not match the processing graph. "
                f"Missing={sorted(stage.value for stage in missing)}; "
                f"unexpected={sorted(stage.value for stage in unexpected)}"
            )

    def get(self, stage: OrchestrationStage) -> StageHandler:
        for registered_stage, handler in self.handlers:
            if registered_stage == stage:
                return handler

        raise KeyError(f"No handler is registered for {getattr(stage, 'value', stage)!r}.")


# ------------------------------------------------------------
# Mutable internal execution context (notebook cell 92, verbatim)
#
# One fresh instance per invoice (never shared, never a module global):
# `execute_invoice_workflow` constructs it internally and it is discarded
# once the terminal `InvoiceWorkflowResult` is built (task §10: "Create a
# fresh context per invoice."; "Avoid hidden global state.").
# ------------------------------------------------------------


@dataclass
class WorkflowExecutionContext:
    request: InvoiceWorkflowRequest
    config: OrchestrationConfig

    started_at: Any

    current_stage: OrchestrationStage = OrchestrationStage.INGESTION

    document_id: Optional[UUID] = None

    stage_results: dict[OrchestrationStage, Any] = field(default_factory=dict)
    stage_records: list[StageExecutionRecord] = field(default_factory=list)
    events: list[OrchestrationEvent] = field(default_factory=list)

    review_required: bool = False
    review_reasons: list[str] = field(default_factory=list)

    errors: list[str] = field(default_factory=list)

    event_sink: Optional[Callable[[OrchestrationEvent], None]] = None

    def result_for(self, stage: OrchestrationStage) -> Any:
        """Typed accessor a production stage handler uses to fetch an
        upstream stage's result (`ap_agent.orchestration.handlers`).

        Raises rather than returning `None` when the requested stage has
        not produced a result yet -- the preceding stage did not complete,
        which is a defect in the handler wiring or the processing order,
        not a value a handler should silently treat as missing input.
        """

        result = self.stage_results.get(stage)

        if result is None:
            raise RuntimeError(
                f"The orchestration context does not contain a result for {stage.value}. "
                "The preceding stage may not have completed."
            )

        return result


# ------------------------------------------------------------
# Secure error representation (notebook cell 92, verbatim)
# ------------------------------------------------------------

CONNECTION_STRING_PATTERN = re.compile(r"postgres(?:ql)?://[^\s]+", flags=re.IGNORECASE)


def safe_orchestration_error_message(error: BaseException) -> str:
    message = str(error).strip()

    if not message:
        return type(error).__name__

    return CONNECTION_STRING_PATTERN.sub("[REDACTED_POSTGRES_DSN]", message)


# ------------------------------------------------------------
# Deterministic orchestration-event identity (notebook cell 92, verbatim)
# ------------------------------------------------------------


def create_orchestration_event_id(
    context: WorkflowExecutionContext,
    event_type: OrchestrationEventType,
    stage: Optional[OrchestrationStage],
    attempt_number: Optional[int],
    sequence_number: int,
) -> UUID:
    stage_text = stage.value if stage is not None else "WORKFLOW"
    attempt_text = str(attempt_number) if attempt_number is not None else "NONE"

    return uuid5(
        NAMESPACE_URL,
        (
            "ap-agent/orchestration-event/"
            f"{context.request.tenant_id}/"
            f"{context.request.batch_id}/"
            f"{context.request.correlation_id}/"
            f"{stage_text}/"
            f"{event_type.value}/"
            f"{attempt_text}/"
            f"{sequence_number}"
        ),
    )


def append_orchestration_event(
    context: WorkflowExecutionContext,
    event_type: OrchestrationEventType,
    stage: Optional[OrchestrationStage],
    status: str,
    message: str,
    attempt_number: Optional[int] = None,
    payload: tuple[tuple[str, str], ...] = tuple(),
) -> OrchestrationEvent:
    sequence_number = len(context.events) + 1

    event = OrchestrationEvent(
        event_id=create_orchestration_event_id(
            context=context,
            event_type=event_type,
            stage=stage,
            attempt_number=attempt_number,
            sequence_number=sequence_number,
        ),
        tenant_id=context.request.tenant_id,
        batch_id=context.request.batch_id,
        document_id=context.document_id,
        correlation_id=context.request.correlation_id,
        event_type=event_type,
        stage=stage,
        status=status,
        attempt_number=attempt_number,
        message=message,
        payload=payload,
        occurred_at=orchestration_utc_now(),
    )

    context.events.append(event)

    if context.event_sink is not None:
        # Observability is best-effort and must never break orchestration
        # (task §14: a sink is for progress rendering/logging, not control
        # flow) -- a raising sink is swallowed rather than aborting the
        # workflow it was only meant to observe.
        try:
            context.event_sink(event)
        except Exception:  # noqa: BLE001 - observability must not break execution
            pass

    return event


# ------------------------------------------------------------
# Result identity extraction (notebook cell 92, verbatim)
# ------------------------------------------------------------


def extract_result_identifier(result: Any) -> Optional[str]:
    if result is None:
        return None

    for attribute_name in (
        "matching_result_id",
        "invoice_record_id",
        "result_id",
        "memory_id",
        "document_id",
    ):
        candidate = getattr(result, attribute_name, None)

        if candidate is not None:
            return str(candidate)

    memory_record = getattr(result, "memory_record", None)

    if memory_record is not None:
        for attribute_name in ("memory_id", "workflow_id", "document_id"):
            candidate = getattr(memory_record, attribute_name, None)

            if candidate is not None:
                return str(candidate)

    identity = getattr(result, "identity", None)

    if identity is not None:
        candidate = getattr(identity, "document_id", None)

        if candidate is not None:
            return str(candidate)

    return None


def extract_result_document_id(result: Any) -> Optional[UUID]:
    if result is None:
        return None

    candidate = getattr(result, "document_id", None)

    if candidate is None:
        identity = getattr(result, "identity", None)
        candidate = getattr(identity, "document_id", None)

    if candidate is None:
        memory_record = getattr(result, "memory_record", None)
        candidate = getattr(memory_record, "document_id", None)

    if candidate is None:
        return None

    if isinstance(candidate, UUID):
        return candidate

    return UUID(str(candidate))


def register_result_document_identity(
    context: WorkflowExecutionContext,
    result: Any,
) -> None:
    candidate_document_id = extract_result_document_id(result)

    if candidate_document_id is None:
        return

    if context.document_id is None:
        context.document_id = candidate_document_id
        return

    if context.document_id != candidate_document_id:
        raise AssertionError("A stage returned a result for a different document.")


# ------------------------------------------------------------
# Review propagation (notebook cell 92, verbatim)
# ------------------------------------------------------------


def merge_decision_review_state(
    context: WorkflowExecutionContext,
    decision: StageRoutingDecision,
) -> None:
    if decision.review_required:
        context.review_required = True

    merged = merge_review_reasons(tuple(context.review_reasons), decision.review_reasons)

    context.review_reasons[:] = merged


def apply_inherited_review_to_memory_route(
    context: WorkflowExecutionContext,
    decision: StageRoutingDecision,
) -> StageRoutingDecision:
    """
    Memory persistence cannot downgrade an inherited review.
    """
    if decision.current_stage != OrchestrationStage.MEMORY_PERSISTENCE:
        return decision

    if not context.review_required or decision.route != WorkflowRoute.COMPLETE:
        return decision

    return replace(
        decision,
        route=WorkflowRoute.ROUTE_TO_REVIEW,
        next_stage=OrchestrationStage.HUMAN_REVIEW,
        review_required=True,
        review_reasons=tuple(context.review_reasons),
        message="Persisted workflow retained an inherited human-review requirement.",
    )


# ------------------------------------------------------------
# Stage-record construction (notebook cell 92, verbatim)
# ------------------------------------------------------------


def stage_record_status(decision: StageRoutingDecision) -> StageExecutionStatus:
    if decision.route == WorkflowRoute.RETRY:
        return StageExecutionStatus.RETRY_SCHEDULED

    if decision.route == WorkflowRoute.STOP_FAILED:
        return StageExecutionStatus.FAILED

    if decision.route == WorkflowRoute.SKIP_IDEMPOTENT:
        return StageExecutionStatus.SKIPPED

    if decision.review_required:
        return StageExecutionStatus.REVIEW_REQUIRED

    return StageExecutionStatus.SUCCEEDED


def build_stage_execution_record(
    stage: OrchestrationStage,
    attempt_number: int,
    started_at: Any,
    completed_at: Any,
    result: Any,
    decision: StageRoutingDecision,
    error: Optional[BaseException],
) -> StageExecutionRecord:
    return StageExecutionRecord(
        stage=stage,
        attempt_number=attempt_number,
        status=stage_record_status(decision),
        route=decision.route,
        started_at=started_at,
        completed_at=completed_at,
        result_id=extract_result_identifier(result),
        result_status=decision.observed_status,
        review_required=decision.review_required,
        review_reasons=decision.review_reasons,
        failure_class=decision.failure_class,
        error_type=type(error).__name__ if error is not None else None,
        error_message=safe_orchestration_error_message(error) if error is not None else None,
        artifact_uri=None,
        artifact_sha256=None,
    )


# ------------------------------------------------------------
# Final-result construction (notebook cell 92, verbatim)
# ------------------------------------------------------------


def build_invoice_workflow_result(
    context: WorkflowExecutionContext,
    status: InvoiceWorkflowStatus,
    current_stage: OrchestrationStage,
    completed_at: Any,
) -> InvoiceWorkflowResult:
    return InvoiceWorkflowResult(
        tenant_id=context.request.tenant_id,
        batch_id=context.request.batch_id,
        correlation_id=context.request.correlation_id,
        source_path=context.request.source_path,
        status=status,
        current_stage=current_stage,
        started_at=context.started_at,
        completed_at=completed_at,
        document_id=context.document_id,
        ingestion_result=context.stage_results.get(OrchestrationStage.INGESTION),
        preprocessing_result=context.stage_results.get(OrchestrationStage.PREPROCESSING),
        ocr_result=context.stage_results.get(OrchestrationStage.OCR),
        normalization_result=context.stage_results.get(OrchestrationStage.NORMALIZATION),
        financial_validation_result=context.stage_results.get(
            OrchestrationStage.FINANCIAL_VALIDATION
        ),
        matching_result=context.stage_results.get(OrchestrationStage.REFERENCE_MATCHING),
        memory_bundle=context.stage_results.get(OrchestrationStage.MEMORY_PERSISTENCE),
        stage_records=tuple(context.stage_records),
        events=tuple(context.events),
        review_required=context.review_required,
        review_reasons=tuple(context.review_reasons),
        errors=tuple(context.errors),
    )


# ------------------------------------------------------------
# Retry waiters (notebook cell 92's `notebook_retry_waiter`, verbatim,
# renamed; plus a real-time alternative for production callers)
#
# The engine's default does not block, matching the notebook: recording a
# schedulable delay without blocking a caller (a notebook cell, a request
# handler, a test) is the right default for a framework-neutral engine
# that a future Temporal/queue integration will drive its own way (task
# §10: "Provide clean extension points for those technologies later.").
# `blocking_retry_waiter` is provided for a caller that explicitly wants
# in-process real-time retry and supplies it itself -- this is the
# dependency-injection seam task §10 requires ("Support dependency
# injection for handlers, timing and retry waiting.").
# ------------------------------------------------------------


def default_retry_waiter(delay_seconds: float) -> None:
    """
    The engine records retry delays but does not block by default.

    A production caller integrating a durable scheduler (a Temporal timer
    or queue retry schedule) supplies its own `RetryWaiter` instead.
    """
    if delay_seconds < 0:
        raise ValueError("Retry delay must not be negative.")


def blocking_retry_waiter(delay_seconds: float) -> None:
    if delay_seconds < 0:
        raise ValueError("Retry delay must not be negative.")

    if delay_seconds:
        import time

        time.sleep(delay_seconds)


# ------------------------------------------------------------
# Single-invoice execution engine (notebook cell 92, verbatim control
# flow; see module docstring for the `StructuredPhaseFailure` addition)
# ------------------------------------------------------------


def execute_invoice_workflow(
    request: InvoiceWorkflowRequest,
    handler_registry: StageHandlerRegistry,
    config: OrchestrationConfig,
    retry_waiter: RetryWaiter = default_retry_waiter,
    event_sink: Optional[Callable[[OrchestrationEvent], None]] = None,
) -> InvoiceWorkflowResult:

    context = WorkflowExecutionContext(
        request=request,
        config=config,
        started_at=orchestration_utc_now(),
        event_sink=event_sink,
    )

    append_orchestration_event(
        context=context,
        event_type=OrchestrationEventType.WORKFLOW_STARTED,
        stage=OrchestrationStage.INGESTION,
        status=InvoiceWorkflowStatus.IN_PROGRESS.value,
        message="Invoice workflow started.",
    )

    stage = OrchestrationStage.INGESTION

    while stage in ORCHESTRATION_PROCESSING_ORDER:
        context.current_stage = stage

        handler = handler_registry.get(stage)

        attempt_number = 1

        while True:
            stage_started_at = orchestration_utc_now()

            append_orchestration_event(
                context=context,
                event_type=OrchestrationEventType.STAGE_STARTED,
                stage=stage,
                status=StageExecutionStatus.RUNNING.value,
                attempt_number=attempt_number,
                message=f"{stage.value} started.",
            )

            stage_result = None
            stage_error: Optional[BaseException] = None

            try:
                stage_result = handler(context)

                register_result_document_identity(context=context, result=stage_result)

                decision = route_stage_result(
                    stage=stage,
                    result=stage_result,
                    attempt_number=attempt_number,
                    config=config,
                )

            except StructuredPhaseFailure as error:
                stage_error = error

                failure_probe = SimpleNamespace(
                    status="FAILED",
                    review_required=True,
                    review_reasons=error.review_reasons,
                )

                decision = route_stage_result(
                    stage=stage,
                    result=failure_probe,
                    attempt_number=attempt_number,
                    config=config,
                    failure_class=error.failure_class,
                )

                for contained_error in error.errors:
                    context.errors.append(f"{stage.value}: {contained_error}")

                if not error.errors:
                    context.errors.append(
                        f"{stage.value}: {safe_orchestration_error_message(error)}"
                    )

            except Exception as error:
                stage_error = error

                failure_class = classify_orchestration_failure(error)

                failure_requires_review = failure_class in {
                    OrchestrationFailureClass.INTEGRITY,
                    OrchestrationFailureClass.POLICY,
                }

                failure_reason = f"{stage.value}_{failure_class.value}_FAILURE"

                failure_probe = SimpleNamespace(
                    status="FAILED",
                    review_required=failure_requires_review,
                    review_reasons=(failure_reason,) if failure_requires_review else tuple(),
                )

                decision = route_stage_result(
                    stage=stage,
                    result=failure_probe,
                    attempt_number=attempt_number,
                    config=config,
                    failure_class=failure_class,
                )

            decision = apply_inherited_review_to_memory_route(context=context, decision=decision)

            merge_decision_review_state(context=context, decision=decision)

            stage_completed_at = orchestration_utc_now()

            stage_record = build_stage_execution_record(
                stage=stage,
                attempt_number=attempt_number,
                started_at=stage_started_at,
                completed_at=stage_completed_at,
                result=stage_result,
                decision=decision,
                error=stage_error,
            )

            context.stage_records.append(stage_record)

            if stage_result is not None:
                context.stage_results[stage] = stage_result

            if decision.route == WorkflowRoute.RETRY:
                append_orchestration_event(
                    context=context,
                    event_type=OrchestrationEventType.STAGE_RETRY_SCHEDULED,
                    stage=stage,
                    status=StageExecutionStatus.RETRY_SCHEDULED.value,
                    attempt_number=attempt_number,
                    message=decision.message,
                    payload=(("retry_delay_seconds", str(decision.retry_delay_seconds)),),
                )

                retry_waiter(decision.retry_delay_seconds)

                attempt_number += 1
                continue

            if decision.route == WorkflowRoute.STOP_FAILED:
                if stage_error is not None and not isinstance(stage_error, StructuredPhaseFailure):
                    context.errors.append(
                        f"{stage.value}: {type(stage_error).__name__}: "
                        f"{safe_orchestration_error_message(stage_error)}"
                    )

                append_orchestration_event(
                    context=context,
                    event_type=OrchestrationEventType.WORKFLOW_FAILED,
                    stage=stage,
                    status=InvoiceWorkflowStatus.FAILED.value,
                    attempt_number=attempt_number,
                    message=decision.message,
                )

                return build_invoice_workflow_result(
                    context=context,
                    status=InvoiceWorkflowStatus.FAILED,
                    current_stage=stage,
                    completed_at=orchestration_utc_now(),
                )

            if decision.route == WorkflowRoute.SKIP_IDEMPOTENT:
                append_orchestration_event(
                    context=context,
                    event_type=OrchestrationEventType.WORKFLOW_COMPLETED,
                    stage=stage,
                    status=InvoiceWorkflowStatus.SKIPPED.value,
                    attempt_number=attempt_number,
                    message=decision.message,
                )

                return build_invoice_workflow_result(
                    context=context,
                    status=InvoiceWorkflowStatus.SKIPPED,
                    current_stage=OrchestrationStage.COMPLETED,
                    completed_at=orchestration_utc_now(),
                )

            if decision.route == WorkflowRoute.ROUTE_TO_REVIEW:
                append_orchestration_event(
                    context=context,
                    event_type=OrchestrationEventType.REVIEW_ROUTED,
                    stage=stage,
                    status=InvoiceWorkflowStatus.REVIEW_REQUIRED.value,
                    attempt_number=attempt_number,
                    message=decision.message,
                )

                return build_invoice_workflow_result(
                    context=context,
                    status=InvoiceWorkflowStatus.REVIEW_REQUIRED,
                    current_stage=OrchestrationStage.HUMAN_REVIEW,
                    completed_at=orchestration_utc_now(),
                )

            if decision.route == WorkflowRoute.COMPLETE:
                append_orchestration_event(
                    context=context,
                    event_type=OrchestrationEventType.WORKFLOW_COMPLETED,
                    stage=stage,
                    status=InvoiceWorkflowStatus.SUCCEEDED.value,
                    attempt_number=attempt_number,
                    message=decision.message,
                )

                return build_invoice_workflow_result(
                    context=context,
                    status=InvoiceWorkflowStatus.SUCCEEDED,
                    current_stage=OrchestrationStage.COMPLETED,
                    completed_at=orchestration_utc_now(),
                )

            if decision.route != WorkflowRoute.CONTINUE:
                raise AssertionError(f"Unhandled workflow route: {decision.route.value}")

            next_stage = decision.next_stage

            if next_stage is None:
                raise AssertionError("CONTINUE route does not contain a next stage.")

            validate_stage_transition(current_stage=stage, proposed_stage=next_stage)

            append_orchestration_event(
                context=context,
                event_type=OrchestrationEventType.STAGE_COMPLETED,
                stage=stage,
                status=stage_record.status.value,
                attempt_number=attempt_number,
                message=decision.message,
            )

            stage = next_stage
            break

    raise AssertionError("Workflow exited the processing graph without a terminal result.")
