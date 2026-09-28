"""Explicit request/response schemas for the Phase 9 review API.

Task §9: "Create explicit request and response schemas ... no arbitrary
internal dataclass serialization." Every response schema below is built
from an `ap_agent.models.interface`/`ap_agent.models.memory` dataclass
through an explicit `from_domain` classmethod -- never a bare
`fastapi.encoders.jsonable_encoder(dataclass_instance)` (which the
notebook's cell 108 uses, and which would silently round monetary
`Decimal`s through `float` and expose whatever field names the domain
dataclass happens to have, both of which task §9 prohibits).

`DecimalString` renders every monetary value as an exact-precision JSON
string (`format(value, "f")`, the same convention as
`ap_agent.serialization.memory_json.memory_json_safe`) instead of a JSON
number, so a client never loses precision to floating point. Every
datetime is timezone-aware ISO-8601 (pydantic's default `datetime`
serialization already produces that for a tz-aware value; every domain
datetime this module touches is tz-aware UTC). Every UUID renders as its
canonical string form. Every enum renders as its declared string value.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Annotated, Any, Generic, Literal, Optional, TypeVar
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, PlainSerializer

from ap_agent.models.interface import (
    DashboardRecord,
    InterfaceCommandStatus,
    InterfaceFieldValue,
    InterfaceFinancialCheck,
    InterfaceLineMatch,
    InterfaceTimelineEvent,
    InvoiceDetailRecord,
    InvoiceFieldName,
    NormalizedValueType,
    ReviewAction,
    ReviewCaseStatus,
    ReviewCommandResult,
    ReviewPriority,
    ReviewQueueRecord,
    WorkflowResumeExecution,
)
from ap_agent.models.memory import HumanReviewDecision, HumanReviewDisposition
from ap_agent.models.orchestration import InvoiceWorkflowStatus, OrchestrationStage
from ap_agent.services.review_queries import ReviewQueuePage

__all__ = [
    "ApiEnvelope",
    "HealthResponse",
    "DashboardResponse",
    "ReviewReasonCount",
    "ReviewQueueItem",
    "ReviewQueuePageResponse",
    "PaginationMeta",
    "InterfaceFieldValueResponse",
    "FinancialCheckResponse",
    "LineMatchResponse",
    "TimelineEventResponse",
    "ReviewDecisionResponse",
    "InvoiceDetailResponse",
    "ApiCorrectionRequest",
    "ApiReviewCommandRequest",
    "CommandResultResponse",
    "WorkflowResumeResponse",
]


DecimalString = Annotated[Decimal, PlainSerializer(lambda value: format(value, "f"), return_type=str)]


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


# ------------------------------------------------------------
# Envelope (notebook cell 108's `ApiEnvelope`, generalized)
# ------------------------------------------------------------

# M11B task §4: `ApiEnvelope.data` was `Optional[Any]`, so every route's
# `response_model=ApiEnvelope` produced an OpenAPI schema where `data` was
# untyped, and `openapi-typescript` could only generate `data?: unknown`
# for it (M11A's `docs/m11a_frontend_foundation_report.md` §7 -- worked
# around there with hand-maintained payload types the generator could not
# catch drift in). `ApiEnvelope` is now generic over its payload type, so
# each route can declare `response_model=ApiEnvelope[SomeResponse]`
# instead: FastAPI/Pydantic still serialize exactly the same JSON shape at
# runtime (constructing `ApiEnvelope[SomeResponse](data=<dict already
# matching SomeResponse's own fields>, ...)` round-trips through the same
# validation `SomeResponse(**data)` would), but the OpenAPI schema now
# `$ref`s the real payload type, so the frontend gets an actually-typed
# `data` field with no hand-maintained shadow types needed. `T` defaults
# behaves like `Any` when a route leaves `ApiEnvelope` unparameterized
# (unchanged behaviour, e.g. `review_commands.py`, out of scope for this
# milestone's read-only endpoints) -- Python 3.11 predates PEP 696's
# `TypeVar(..., default=...)`, so this is a plain, unbound `TypeVar`.
T = TypeVar("T")


class ApiEnvelope(_StrictModel, Generic[T]):
    request_id: UUID
    status: str
    data: Optional[T] = None
    errors: tuple[str, ...] = tuple()
    generated_at: datetime


# ------------------------------------------------------------
# Health (M11B task §4: was an inline dict literal in
# ap_agent/api/routes/health.py, so it -- like every other route's `data`
# before this milestone -- had no OpenAPI-visible shape. Same fields,
# same values, now explicit.)
# ------------------------------------------------------------


class HealthResponse(_StrictModel):
    service: str
    api_version: str
    command_mode: Literal["COMMIT", "VALIDATION_ONLY"]
    payment_execution: Literal["PROHIBITED"]


# ------------------------------------------------------------
# Dashboard
# ------------------------------------------------------------


class ReviewReasonCount(_StrictModel):
    reason: str
    count: int


class DashboardResponse(_StrictModel):
    tenant_id: UUID
    generated_at: datetime

    total_invoices: int
    processing_invoices: int
    completed_invoices: int
    review_required_invoices: int
    failed_invoices: int

    open_review_cases: int
    unassigned_review_cases: int

    review_reason_counts: tuple[ReviewReasonCount, ...]

    @classmethod
    def from_domain(cls, record: DashboardRecord) -> "DashboardResponse":
        return cls(
            tenant_id=record.tenant_id,
            generated_at=record.generated_at,
            total_invoices=record.total_invoices,
            processing_invoices=record.processing_invoices,
            completed_invoices=record.completed_invoices,
            review_required_invoices=record.review_required_invoices,
            failed_invoices=record.failed_invoices,
            open_review_cases=record.open_review_cases,
            unassigned_review_cases=record.unassigned_review_cases,
            review_reason_counts=tuple(
                ReviewReasonCount(reason=reason, count=count) for reason, count in record.review_reason_counts
            ),
        )


# ------------------------------------------------------------
# Review queue
# ------------------------------------------------------------


class ReviewQueueItem(_StrictModel):
    tenant_id: UUID
    review_case_id: UUID
    workflow_id: UUID
    batch_id: UUID
    document_id: UUID

    source_name: str
    invoice_number: Optional[str]
    supplier_name: Optional[str]
    currency: Optional[str]
    total_amount: Optional[DecimalString]

    workflow_status: InvoiceWorkflowStatus
    current_stage: OrchestrationStage
    case_status: ReviewCaseStatus
    priority: ReviewPriority

    review_reasons: tuple[str, ...]

    supplier_status: Optional[str]
    purchase_order_status: Optional[str]
    financial_validation_status: Optional[str]

    assigned_reviewer_id: Optional[str]
    review_revision: int

    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_domain(cls, record: ReviewQueueRecord) -> "ReviewQueueItem":
        return cls(
            tenant_id=record.tenant_id,
            review_case_id=record.review_case_id,
            workflow_id=record.workflow_id,
            batch_id=record.batch_id,
            document_id=record.document_id,
            source_name=record.source_name,
            invoice_number=record.invoice_number,
            supplier_name=record.supplier_name,
            currency=record.currency,
            total_amount=record.total_amount,
            workflow_status=record.workflow_status,
            current_stage=record.current_stage,
            case_status=record.case_status,
            priority=record.priority,
            review_reasons=record.review_reasons,
            supplier_status=record.supplier_status,
            purchase_order_status=record.purchase_order_status,
            financial_validation_status=record.financial_validation_status,
            assigned_reviewer_id=record.assigned_reviewer_id,
            review_revision=record.revision,
            created_at=record.created_at,
            updated_at=record.updated_at,
        )


class PaginationMeta(_StrictModel):
    page: int
    page_size: int
    total_count: int
    total_pages: int


class ReviewQueuePageResponse(_StrictModel):
    items: tuple[ReviewQueueItem, ...]
    pagination: PaginationMeta

    @classmethod
    def from_domain(cls, page: ReviewQueuePage) -> "ReviewQueuePageResponse":
        total_pages = max(1, -(-page.total_count // page.page_size)) if page.page_size else 1

        return cls(
            items=tuple(ReviewQueueItem.from_domain(record) for record in page.items),
            pagination=PaginationMeta(
                page=page.page, page_size=page.page_size, total_count=page.total_count, total_pages=total_pages
            ),
        )


# ------------------------------------------------------------
# Invoice detail
# ------------------------------------------------------------


class InterfaceFieldValueResponse(_StrictModel):
    field_name: InvoiceFieldName
    raw_value: Optional[str]
    normalized_value: Optional[str]
    value_type: NormalizedValueType
    confidence: Optional[float]
    review_required: bool
    evidence_reference_ids: tuple[str, ...]

    @classmethod
    def from_domain(cls, value: InterfaceFieldValue) -> "InterfaceFieldValueResponse":
        return cls(
            field_name=value.field_name,
            raw_value=value.raw_value,
            normalized_value=value.normalized_value,
            value_type=value.value_type,
            confidence=value.confidence,
            review_required=value.review_required,
            evidence_reference_ids=value.evidence_reference_ids,
        )


class FinancialCheckResponse(_StrictModel):
    check_id: str
    check_type: str
    status: str
    message: str
    expected_value: Optional[str]
    observed_value: Optional[str]
    evidence_reference_ids: tuple[str, ...]

    @classmethod
    def from_domain(cls, value: InterfaceFinancialCheck) -> "FinancialCheckResponse":
        return cls(
            check_id=value.check_id,
            check_type=value.check_type,
            status=value.status,
            message=value.message,
            expected_value=value.expected_value,
            observed_value=value.observed_value,
            evidence_reference_ids=value.evidence_reference_ids,
        )


class LineMatchResponse(_StrictModel):
    line_match_id: str
    invoice_line_number: Optional[int]
    purchase_order_line_number: Optional[int]
    description_status: str
    quantity_status: str
    unit_price_status: str
    line_total_status: str
    review_required: bool
    review_reasons: tuple[str, ...]

    @classmethod
    def from_domain(cls, value: InterfaceLineMatch) -> "LineMatchResponse":
        return cls(
            line_match_id=value.line_match_id,
            invoice_line_number=value.invoice_line_number,
            purchase_order_line_number=value.purchase_order_line_number,
            description_status=value.description_status,
            quantity_status=value.quantity_status,
            unit_price_status=value.unit_price_status,
            line_total_status=value.line_total_status,
            review_required=value.review_required,
            review_reasons=value.review_reasons,
        )


class TimelineEventResponse(_StrictModel):
    event_id: str
    event_type: str
    stage: Optional[str]
    status: str
    actor_id: Optional[str]
    message: str
    occurred_at: datetime

    @classmethod
    def from_domain(cls, value: InterfaceTimelineEvent) -> "TimelineEventResponse":
        return cls(
            event_id=value.event_id,
            event_type=value.event_type,
            stage=value.stage,
            status=value.status,
            actor_id=value.actor_id,
            message=value.message,
            occurred_at=value.occurred_at,
        )


class ReviewDecisionResponse(_StrictModel):
    decision_id: UUID
    reviewer_id: str
    disposition: HumanReviewDisposition
    reason_codes: tuple[str, ...]
    notes: Optional[str]
    decided_at: datetime

    @classmethod
    def from_domain(cls, value: HumanReviewDecision) -> "ReviewDecisionResponse":
        return cls(
            decision_id=value.decision_id,
            reviewer_id=value.reviewer_id,
            disposition=value.disposition,
            reason_codes=value.reason_codes,
            notes=value.notes,
            decided_at=value.decided_at,
        )


class InvoiceDetailResponse(_StrictModel):
    tenant_id: UUID
    review_case_id: UUID
    workflow_id: UUID
    batch_id: UUID
    document_id: UUID

    source_name: str
    source_document_sha256: str
    original_document_uri: Optional[str]

    workflow_status: InvoiceWorkflowStatus
    current_stage: OrchestrationStage
    case_status: ReviewCaseStatus
    review_revision: int

    review_required: bool
    review_reasons: tuple[str, ...]

    fields: tuple[InterfaceFieldValueResponse, ...]
    financial_checks: tuple[FinancialCheckResponse, ...]
    line_matches: tuple[LineMatchResponse, ...]
    timeline: tuple[TimelineEventResponse, ...]
    review_decisions: tuple[ReviewDecisionResponse, ...]

    @classmethod
    def from_domain(cls, record: InvoiceDetailRecord) -> "InvoiceDetailResponse":
        # `original_document_uri` stays null until a secure authenticated
        # streaming/signed-URL mechanism exists (task §6) -- never a
        # local filesystem path (task §9/§21).
        return cls(
            tenant_id=record.tenant_id,
            review_case_id=record.review_case_id,
            workflow_id=record.workflow_id,
            batch_id=record.batch_id,
            document_id=record.document_id,
            source_name=record.source_name,
            source_document_sha256=record.source_document_sha256,
            original_document_uri=record.source_artifact_uri,
            workflow_status=record.workflow_status,
            current_stage=record.current_stage,
            case_status=record.case_status,
            review_revision=record.revision,
            review_required=record.review_required,
            review_reasons=record.review_reasons,
            fields=tuple(InterfaceFieldValueResponse.from_domain(f) for f in record.fields),
            financial_checks=tuple(FinancialCheckResponse.from_domain(c) for c in record.financial_checks),
            line_matches=tuple(LineMatchResponse.from_domain(m) for m in record.line_matches),
            timeline=tuple(TimelineEventResponse.from_domain(e) for e in record.timeline),
            review_decisions=tuple(ReviewDecisionResponse.from_domain(d) for d in record.review_decisions),
        )


# ------------------------------------------------------------
# Review commands
# ------------------------------------------------------------


class ApiCorrectionRequest(_StrictModel):
    field_name: InvoiceFieldName
    line_number: Optional[int] = Field(default=None, ge=1)
    previous_value: Optional[str] = None
    corrected_value: str = Field(min_length=1, max_length=500)
    reason: str = Field(min_length=1, max_length=1000)
    evidence_reference_ids: tuple[str, ...]


class ApiReviewCommandRequest(_StrictModel):
    command_id: UUID
    idempotency_key: str = Field(min_length=8, max_length=128)
    action: ReviewAction
    disposition: Optional[HumanReviewDisposition] = None

    observed_review_revision: int = Field(ge=1)
    observed_workflow_revision: int = Field(ge=1)

    reason_codes: tuple[str, ...] = tuple()
    notes: Optional[str] = Field(default=None, max_length=4000)
    corrections: tuple[ApiCorrectionRequest, ...] = tuple()

    requested_at: datetime


class CommandResultResponse(_StrictModel):
    command_id: UUID
    idempotency_key: str
    status: InterfaceCommandStatus
    review_case_id: UUID
    document_id: UUID
    resulting_case_status: Optional[ReviewCaseStatus]
    resulting_revision: Optional[int]
    workflow_resumed: bool
    decision_id: Optional[UUID]
    message: str

    @classmethod
    def from_domain(cls, result: ReviewCommandResult) -> "CommandResultResponse":
        return cls(
            command_id=result.command_id,
            idempotency_key=result.idempotency_key,
            status=result.status,
            review_case_id=result.review_case_id,
            document_id=result.document_id,
            resulting_case_status=result.resulting_case_status,
            resulting_revision=result.resulting_revision,
            workflow_resumed=result.workflow_resumed,
            decision_id=result.decision_id,
            message=result.message,
        )


class WorkflowResumeResponse(CommandResultResponse):
    restart_stage: Optional[OrchestrationStage] = None
    derived_version: Optional[str] = None

    @classmethod
    def from_execution(cls, execution: WorkflowResumeExecution) -> "WorkflowResumeResponse":
        base = CommandResultResponse.from_domain(execution.command_result)
        return cls(
            **base.model_dump(),
            restart_stage=(execution.resume_plan.restart_stage if execution.resume_plan else None),
            derived_version=(execution.resume_plan.derived_version if execution.resume_plan else None),
        )
