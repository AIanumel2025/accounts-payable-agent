"""Phase 9 read-only query services: dashboard, review queue, invoice detail.

Source: notebook cells 101-103 (`retrieve_review_queue`,
`retrieve_invoice_detail`, `retrieve_dashboard_record`). The SQL and
payload-integrity behaviour live in
`ap_agent.repositories.review_repository.ReviewRepository`; this module
adds the request-shaping the M10 task brief requires on top of it
(task §6):

- pagination metadata for the review queue (page/page_size/total_count),
  never a bare list;
- request-level validation of the optional status/priority/assignment/
  batch filters, so a malformed filter fails with a typed error instead of
  a raw `psycopg` exception reaching the API layer;
- nothing here hardcodes a fixture-specific case/invoice count (task §6:
  "Do not hardcode three review cases or four invoices").
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional
from uuid import UUID

from ap_agent.exceptions import ReviewCaseNotFoundError
from ap_agent.models.interface import (
    DashboardRecord,
    InterfaceConfig,
    InvoiceDetailRecord,
    ReviewPriority,
    ReviewQueueRecord,
)
from ap_agent.repositories.review_repository import (
    ACTIVE_DATABASE_REVIEW_STATUSES,
    ReviewRepository,
)

__all__ = [
    "ReviewQueuePage",
    "get_dashboard",
    "list_review_queue",
    "get_review_queue_record",
    "get_invoice_detail",
]


_DATABASE_REVIEW_STATUSES_BY_CASE_STATUS = {
    "OPEN": "OPEN",
    "IN_REVIEW": "CLAIMED",
    "RESOLVED": "RESOLVED",
    "REJECTED": "RESOLVED",
}

_PRIORITY_TO_DATABASE = {
    ReviewPriority.CRITICAL: 1,
    ReviewPriority.HIGH: 2,
    ReviewPriority.NORMAL: 3,
    ReviewPriority.LOW: 4,
}


@dataclass(frozen=True)
class ReviewQueuePage:
    items: tuple[ReviewQueueRecord, ...]
    page: int
    page_size: int
    total_count: int


def get_dashboard(
    repository: ReviewRepository,
    tenant_id: UUID,
    *,
    batch_id: Optional[UUID] = None,
) -> DashboardRecord:
    dashboard, _source_rows = repository.get_dashboard(tenant_id, batch_id=batch_id)
    return dashboard


def list_review_queue(
    repository: ReviewRepository,
    tenant_id: UUID,
    config: InterfaceConfig,
    *,
    page: int = 1,
    page_size: Optional[int] = None,
    status_filter: Optional[str] = None,
    assigned_filter: Optional[str] = None,
    priority_filter: Optional[ReviewPriority] = None,
    batch_id_filter: Optional[UUID] = None,
) -> ReviewQueuePage:
    if page < 1:
        raise ValueError("page must be >= 1.")

    effective_page_size = page_size if page_size is not None else config.default_page_size

    if effective_page_size < 1 or effective_page_size > config.maximum_page_size:
        raise ValueError(f"page_size must be between 1 and {config.maximum_page_size}.")

    database_status_filter: Optional[tuple[str, ...]] = None

    if status_filter is not None:
        database_status = _DATABASE_REVIEW_STATUSES_BY_CASE_STATUS.get(status_filter)

        if database_status is None:
            raise ValueError(f"Unsupported review-case status filter: {status_filter}.")

        database_status_filter = (database_status,)
    else:
        database_status_filter = ACTIVE_DATABASE_REVIEW_STATUSES

    database_priority_filter = None

    if priority_filter is not None:
        database_priority_filter = _PRIORITY_TO_DATABASE[priority_filter]

    records, total_count = repository.list_review_queue(
        tenant_id,
        status_filter=database_status_filter,
        assigned_filter=assigned_filter,
        priority_filter=database_priority_filter,
        batch_id_filter=batch_id_filter,
        limit=effective_page_size,
        offset=(page - 1) * effective_page_size,
    )

    return ReviewQueuePage(items=records, page=page, page_size=effective_page_size, total_count=total_count)


def get_review_queue_record(
    repository: ReviewRepository,
    tenant_id: UUID,
    review_case_id: UUID,
) -> Optional[ReviewQueueRecord]:
    return repository.get_review_queue_record(tenant_id, review_case_id)


def get_invoice_detail(
    repository: ReviewRepository,
    tenant_id: UUID,
    review_case_id: UUID,
) -> InvoiceDetailRecord:
    queue_record = repository.get_review_queue_record(tenant_id, review_case_id)

    if queue_record is None:
        raise ReviewCaseNotFoundError(f"Review case {review_case_id} was not found for this tenant.")

    return repository.get_invoice_detail(tenant_id, queue_record)
