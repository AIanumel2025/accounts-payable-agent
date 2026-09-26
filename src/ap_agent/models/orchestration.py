"""Phase 8 (workflow orchestration and recovery) contracts and configuration.

Source: notebook cell 90 ("PHASE 8 — CELL 1", "Workflow-orchestration
contracts and configuration"), active-definition table extended for M9
(docs/modularisation_map.md). Enums, dataclasses, defaults and the retry
math (`calculate_retry_delay_seconds`) are ported verbatim from that cell's
names, field order and values.

Two deliberate departures from a byte-for-byte port, both required by the
M9 task brief and neither changing the four-fixture golden outcome (the
notebook's own Phase 8 Cell 5 already exercises the corrected form of the
second one):

  - `InvoiceWorkflowResult` gains a derived, read-only `terminal_route`
    property. The notebook has no `route`/`workflow_route`/`final_route`
    field on this contract at all -- Phase 8 Cell 5's own
    `workflow_result_route` helper falls back to reading `current_stage`
    for exactly this reason. `terminal_route` is that same fallback,
    promoted from ad-hoc validation code into the contract itself, so a
    caller has one unambiguous, immutable place to read it instead of
    reimplementing the fallback (task §8: "Establish one unambiguous
    source of truth."). It is computed on every access, never stored.
  - Two typed stage-result contracts the notebook only ever builds as
    `SimpleNamespace` / `dict` values are named here:
    `MemoryPersistenceStageResult` (notebook cell 93's dataclass of the
    same name, ported verbatim) and `StructuredPhaseFailure`, an exception
    new in M9 (see `ap_agent.orchestration.handlers` module docstring for
    why: notebook correction cells 4C/4D re-raise a structured Phase 5/6
    `FAILED` result as a bare `RuntimeError`, which the M9 task brief
    (§12) requires production code not to do).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Optional
from uuid import UUID

from ap_agent.models.ingestion import IngestionResult
from ap_agent.models.matching import MatchingResult
from ap_agent.models.memory import InvoiceMemoryBundle, MatchedInvoiceMemoryRecord
from ap_agent.models.normalization import NormalizationResult
from ap_agent.models.ocr import OCRDocumentResult
from ap_agent.models.preprocessing import PreprocessingResult
from ap_agent.models.validation import FinancialValidationResult

__all__ = [
    "OrchestrationStage",
    "StageExecutionStatus",
    "InvoiceWorkflowStatus",
    "BatchWorkflowStatus",
    "OrchestrationFailureClass",
    "WorkflowRoute",
    "OrchestrationEventType",
    "ORCHESTRATION_PROCESSING_ORDER",
    "ORCHESTRATION_TERMINAL_STAGES",
    "OrchestrationRetryPolicy",
    "OrchestrationConfig",
    "InvoiceWorkflowRequest",
    "BatchWorkflowRequest",
    "StageExecutionRecord",
    "OrchestrationEvent",
    "WorkflowCheckpoint",
    "InvoiceWorkflowResult",
    "BatchWorkflowResult",
    "MemoryPersistenceStageResult",
    "StructuredPhaseFailure",
    "TERMINAL_ROUTE_COMPLETED",
    "TERMINAL_ROUTE_HUMAN_REVIEW",
    "TERMINAL_ROUTE_NONE",
    "orchestration_utc_now",
    "calculate_retry_delay_seconds",
]


# ------------------------------------------------------------
# Orchestration enumerations (notebook cell 90, verbatim)
# ------------------------------------------------------------


class OrchestrationStage(str, Enum):
    INGESTION = "INGESTION"
    PREPROCESSING = "PREPROCESSING"
    OCR = "OCR"
    NORMALIZATION = "NORMALIZATION"
    FINANCIAL_VALIDATION = "FINANCIAL_VALIDATION"
    REFERENCE_MATCHING = "REFERENCE_MATCHING"
    MEMORY_PERSISTENCE = "MEMORY_PERSISTENCE"
    HUMAN_REVIEW = "HUMAN_REVIEW"
    COMPLETED = "COMPLETED"


class StageExecutionStatus(str, Enum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    REVIEW_REQUIRED = "REVIEW_REQUIRED"
    RETRY_SCHEDULED = "RETRY_SCHEDULED"
    FAILED = "FAILED"
    SKIPPED = "SKIPPED"


class InvoiceWorkflowStatus(str, Enum):
    PENDING = "PENDING"
    IN_PROGRESS = "IN_PROGRESS"
    SUCCEEDED = "SUCCEEDED"
    REVIEW_REQUIRED = "REVIEW_REQUIRED"
    FAILED = "FAILED"
    SKIPPED = "SKIPPED"


class BatchWorkflowStatus(str, Enum):
    PENDING = "PENDING"
    IN_PROGRESS = "IN_PROGRESS"
    SUCCEEDED = "SUCCEEDED"
    COMPLETED_WITH_REVIEW = "COMPLETED_WITH_REVIEW"
    PARTIALLY_FAILED = "PARTIALLY_FAILED"
    FAILED = "FAILED"


class OrchestrationFailureClass(str, Enum):
    TRANSIENT = "TRANSIENT"
    PERMANENT = "PERMANENT"
    INTEGRITY = "INTEGRITY"
    POLICY = "POLICY"
    UNKNOWN = "UNKNOWN"


class WorkflowRoute(str, Enum):
    CONTINUE = "CONTINUE"
    RETRY = "RETRY"
    ROUTE_TO_REVIEW = "ROUTE_TO_REVIEW"
    COMPLETE = "COMPLETE"
    STOP_FAILED = "STOP_FAILED"
    RESUME = "RESUME"
    SKIP_IDEMPOTENT = "SKIP_IDEMPOTENT"


class OrchestrationEventType(str, Enum):
    WORKFLOW_STARTED = "WORKFLOW_STARTED"
    STAGE_STARTED = "STAGE_STARTED"
    STAGE_COMPLETED = "STAGE_COMPLETED"
    STAGE_RETRY_SCHEDULED = "STAGE_RETRY_SCHEDULED"
    STAGE_FAILED = "STAGE_FAILED"
    REVIEW_ROUTED = "REVIEW_ROUTED"
    WORKFLOW_RESUMED = "WORKFLOW_RESUMED"
    WORKFLOW_COMPLETED = "WORKFLOW_COMPLETED"
    WORKFLOW_FAILED = "WORKFLOW_FAILED"


# ------------------------------------------------------------
# Ordered processing stages (notebook cell 90, verbatim)
# ------------------------------------------------------------

ORCHESTRATION_PROCESSING_ORDER = (
    OrchestrationStage.INGESTION,
    OrchestrationStage.PREPROCESSING,
    OrchestrationStage.OCR,
    OrchestrationStage.NORMALIZATION,
    OrchestrationStage.FINANCIAL_VALIDATION,
    OrchestrationStage.REFERENCE_MATCHING,
    OrchestrationStage.MEMORY_PERSISTENCE,
)

ORCHESTRATION_TERMINAL_STAGES = (
    OrchestrationStage.HUMAN_REVIEW,
    OrchestrationStage.COMPLETED,
)


# ------------------------------------------------------------
# Retry and orchestration configuration (notebook cell 90, verbatim
# defaults; CLAUDE.md: threaded explicitly, never a module-level instance)
# ------------------------------------------------------------


@dataclass(frozen=True)
class OrchestrationRetryPolicy:
    maximum_attempts: int

    initial_delay_seconds: float
    backoff_multiplier: float
    maximum_delay_seconds: float

    retry_transient_failures: bool
    retry_integrity_failures: bool
    retry_policy_failures: bool
    retry_permanent_failures: bool


@dataclass(frozen=True)
class OrchestrationConfig:
    orchestration_version: str

    maximum_batch_documents: int
    maximum_batch_concurrency: int

    stage_timeout_seconds: int

    checkpoint_after_each_stage: bool
    resume_enabled: bool
    fail_closed: bool

    continue_after_review_required: bool
    isolate_invoice_failures: bool

    preserve_phase_results: bool
    emit_transition_events: bool

    retry_policy: OrchestrationRetryPolicy


def default_orchestration_retry_policy() -> OrchestrationRetryPolicy:
    """Construct the notebook's validated retry policy (cell 90).

    A function, not a module-level instance: CLAUDE.md forbids a mutable
    module-level config *instance*; callers (production bindings, tests)
    construct their own copy explicitly.
    """

    return OrchestrationRetryPolicy(
        maximum_attempts=3,
        initial_delay_seconds=1.0,
        backoff_multiplier=2.0,
        maximum_delay_seconds=8.0,
        retry_transient_failures=True,
        retry_integrity_failures=False,
        retry_policy_failures=False,
        retry_permanent_failures=False,
    )


def default_orchestration_config(
    retry_policy: Optional[OrchestrationRetryPolicy] = None,
) -> OrchestrationConfig:
    """Construct the notebook's validated orchestration configuration
    (cell 90). See `default_orchestration_retry_policy` for why this is a
    function rather than a module-level instance."""

    return OrchestrationConfig(
        orchestration_version="orchestration-v1",
        maximum_batch_documents=1000,
        maximum_batch_concurrency=4,
        stage_timeout_seconds=600,
        checkpoint_after_each_stage=True,
        resume_enabled=True,
        fail_closed=True,
        continue_after_review_required=True,
        isolate_invoice_failures=True,
        preserve_phase_results=True,
        emit_transition_events=True,
        retry_policy=retry_policy or default_orchestration_retry_policy(),
    )


# ------------------------------------------------------------
# Invoice and batch requests (notebook cell 90, verbatim)
# ------------------------------------------------------------


@dataclass(frozen=True)
class InvoiceWorkflowRequest:
    tenant_id: UUID
    batch_id: UUID
    correlation_id: UUID

    source_path: Path
    source_channel: str

    submitted_at: datetime

    resume_document_id: Optional[UUID] = None

    metadata: tuple[tuple[str, str], ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class BatchWorkflowRequest:
    tenant_id: UUID
    batch_id: UUID
    correlation_id: UUID

    source_paths: tuple[Path, ...]
    source_channel: str

    submitted_at: datetime

    metadata: tuple[tuple[str, str], ...] = field(default_factory=tuple)


# ------------------------------------------------------------
# Stage execution record (notebook cell 90, verbatim)
# ------------------------------------------------------------


@dataclass(frozen=True)
class StageExecutionRecord:
    stage: OrchestrationStage
    attempt_number: int

    status: StageExecutionStatus
    route: WorkflowRoute

    started_at: datetime
    completed_at: Optional[datetime]

    result_id: Optional[str]
    result_status: Optional[str]

    review_required: bool
    review_reasons: tuple[str, ...]

    failure_class: Optional[OrchestrationFailureClass]

    error_type: Optional[str]
    error_message: Optional[str]

    artifact_uri: Optional[str]
    artifact_sha256: Optional[str]


# ------------------------------------------------------------
# Orchestration audit event (notebook cell 90, verbatim)
# ------------------------------------------------------------


@dataclass(frozen=True)
class OrchestrationEvent:
    event_id: UUID

    tenant_id: UUID
    batch_id: UUID
    document_id: Optional[UUID]

    correlation_id: UUID

    event_type: OrchestrationEventType
    stage: Optional[OrchestrationStage]
    status: str

    attempt_number: Optional[int]

    message: str

    payload: tuple[tuple[str, str], ...]

    occurred_at: datetime


# ------------------------------------------------------------
# Workflow checkpoint (notebook cell 90, verbatim)
# ------------------------------------------------------------


@dataclass(frozen=True)
class WorkflowCheckpoint:
    tenant_id: UUID
    batch_id: UUID
    document_id: UUID

    correlation_id: UUID

    current_stage: OrchestrationStage
    workflow_status: InvoiceWorkflowStatus

    completed_stages: tuple[OrchestrationStage, ...]

    stage_records: tuple[StageExecutionRecord, ...]

    review_required: bool
    review_reasons: tuple[str, ...]

    checkpoint_version: str
    checkpointed_at: datetime


# ------------------------------------------------------------
# Terminal-route derivation (task §8; notebook Phase 8 Cell 5's
# `workflow_result_route` fallback, promoted onto the contract)
# ------------------------------------------------------------

TERMINAL_ROUTE_COMPLETED = "COMPLETED"
TERMINAL_ROUTE_HUMAN_REVIEW = "HUMAN_REVIEW"
TERMINAL_ROUTE_NONE = "NONE"

_TERMINAL_ROUTE_BY_STAGE = {
    OrchestrationStage.COMPLETED: TERMINAL_ROUTE_COMPLETED,
    OrchestrationStage.HUMAN_REVIEW: TERMINAL_ROUTE_HUMAN_REVIEW,
}


# ------------------------------------------------------------
# Complete invoice workflow result (notebook cell 90, verbatim fields,
# plus the derived `terminal_route` property described in the module
# docstring)
# ------------------------------------------------------------


@dataclass(frozen=True)
class InvoiceWorkflowResult:
    tenant_id: UUID
    batch_id: UUID
    correlation_id: UUID

    source_path: Path

    status: InvoiceWorkflowStatus
    current_stage: OrchestrationStage

    started_at: datetime
    completed_at: Optional[datetime]

    document_id: Optional[UUID] = None

    ingestion_result: Optional[IngestionResult] = None
    preprocessing_result: Optional[PreprocessingResult] = None
    ocr_result: Optional[OCRDocumentResult] = None
    normalization_result: Optional[NormalizationResult] = None
    financial_validation_result: Optional[FinancialValidationResult] = None
    matching_result: Optional[MatchingResult] = None
    memory_bundle: Optional[InvoiceMemoryBundle] = None

    stage_records: tuple[StageExecutionRecord, ...] = field(default_factory=tuple)
    events: tuple[OrchestrationEvent, ...] = field(default_factory=tuple)

    review_required: bool = False
    review_reasons: tuple[str, ...] = field(default_factory=tuple)

    errors: tuple[str, ...] = field(default_factory=tuple)

    @property
    def terminal_route(self) -> str:
        """The public terminal destination, derived from `current_stage`.

        This is the *only* place this value is computed: it is never
        stored, so there is no second, independently mutable terminal-
        route field to drift out of sync with `current_stage` (task §8).
        A workflow whose `status` is `SUCCEEDED` always has
        `current_stage == COMPLETED` (see
        `ap_agent.orchestration.engine.execute_invoice_workflow`), so it
        can never resolve to `TERMINAL_ROUTE_NONE`.
        """

        return _TERMINAL_ROUTE_BY_STAGE.get(self.current_stage, TERMINAL_ROUTE_NONE)


# ------------------------------------------------------------
# Complete batch workflow result (notebook cell 90, verbatim)
# ------------------------------------------------------------


@dataclass(frozen=True)
class BatchWorkflowResult:
    tenant_id: UUID
    batch_id: UUID
    correlation_id: UUID

    status: BatchWorkflowStatus

    started_at: datetime
    completed_at: Optional[datetime]

    invoice_results: tuple[InvoiceWorkflowResult, ...]

    total_documents: int
    successful_documents: int
    review_required_documents: int
    failed_documents: int
    skipped_documents: int

    errors: tuple[str, ...] = field(default_factory=tuple)


# ------------------------------------------------------------
# Stage 7 (memory-persistence) result (notebook cell 93's
# `MemoryPersistenceStageResult`, verbatim)
# ------------------------------------------------------------


@dataclass(frozen=True)
class MemoryPersistenceStageResult:
    """Orchestration-facing result returned after the real PostgreSQL
    memory operation succeeds. The stored record itself remains
    append-only in PostgreSQL (`ap_agent.services.memory_service`)."""

    batch_id: UUID
    document_id: UUID
    status: str
    review_required: bool
    review_reasons: tuple[str, ...]

    memory_record_id: UUID
    payload_sha256: str
    stored_record: Optional[MatchedInvoiceMemoryRecord] = None


# ------------------------------------------------------------
# Structured phase-failure diagnostics (new in M9; task §12 -- see
# ap_agent.orchestration.handlers module docstring)
# ------------------------------------------------------------


class StructuredPhaseFailure(RuntimeError):
    """Raised by a production stage handler when the bound Phase 5/6 tool
    returns a structured ``FAILED`` result instead of raising.

    Carries the phase's own diagnostics (`errors`, `review_reasons`) and a
    caller-supplied `failure_class` so
    `ap_agent.orchestration.engine.execute_invoice_workflow` can classify
    and report the failure using the phase's real information instead of
    falling back to `OrchestrationFailureClass.UNKNOWN` and an empty
    `errors` tuple (task §12: "Do not report errors=NONE."; "Do not
    silently convert it into an unknown failure.").
    """

    def __init__(
        self,
        stage: OrchestrationStage,
        *,
        errors: tuple[str, ...],
        review_reasons: tuple[str, ...] = (),
        failure_class: OrchestrationFailureClass = OrchestrationFailureClass.INTEGRITY,
    ) -> None:
        message = (
            f"{stage.value} failed closed: "
            + (" | ".join(errors) if errors else "no underlying phase error was supplied")
        )
        super().__init__(message)

        self.stage = stage
        self.errors = tuple(errors)
        self.review_reasons = tuple(review_reasons)
        self.failure_class = failure_class


# ------------------------------------------------------------
# Utilities (notebook cell 90, verbatim)
# ------------------------------------------------------------


def orchestration_utc_now() -> datetime:
    return datetime.now(timezone.utc)


def calculate_retry_delay_seconds(
    attempt_number: int,
    retry_policy: OrchestrationRetryPolicy,
) -> float:
    """
    Return the delay before the requested retry attempt.

    Attempt 1 is the initial execution and therefore has
    no retry delay.
    """
    if attempt_number <= 1:
        return 0.0

    delay = retry_policy.initial_delay_seconds * (
        retry_policy.backoff_multiplier ** (attempt_number - 2)
    )

    return min(delay, retry_policy.maximum_delay_seconds)
