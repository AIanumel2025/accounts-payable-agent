"""M10 unit tests: `ap_agent.api.schemas` explicit response schemas --
exact-decimal monetary serialization, enum values, pagination metadata,
and that no arbitrary internal field leaks through (task §9)."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from decimal import Decimal
from uuid import uuid4

import pytest

from ap_agent.api.schemas import DashboardResponse, ReviewQueueItem, ReviewQueuePageResponse
from ap_agent.models.interface import (
    DashboardRecord,
    ReviewCaseStatus,
    ReviewPriority,
    ReviewQueueRecord,
)
from ap_agent.models.orchestration import InvoiceWorkflowStatus, OrchestrationStage
from ap_agent.services.review_queries import ReviewQueuePage

pytestmark = pytest.mark.unit

NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _queue_record(**overrides) -> ReviewQueueRecord:
    defaults = dict(
        tenant_id=uuid4(),
        review_case_id=uuid4(),
        workflow_id=uuid4(),
        batch_id=uuid4(),
        document_id=uuid4(),
        source_name="invoice.pdf",
        invoice_number="INV-1",
        supplier_name="Acme",
        currency="USD",
        total_amount=Decimal("1234.10"),
        workflow_status=InvoiceWorkflowStatus.REVIEW_REQUIRED,
        current_stage=OrchestrationStage.HUMAN_REVIEW,
        case_status=ReviewCaseStatus.OPEN,
        priority=ReviewPriority.NORMAL,
        review_reasons=("SUPPLIER_NAME_MISSING",),
        supplier_status="MATCHED",
        purchase_order_status="MATCHED",
        financial_validation_status="FAILED",
        assigned_reviewer_id=None,
        revision=1,
        created_at=NOW,
        updated_at=NOW,
    )
    defaults.update(overrides)
    return ReviewQueueRecord(**defaults)


def test_monetary_value_serializes_with_exact_precision_no_float_rounding():
    record = _queue_record(total_amount=Decimal("1234.10"))
    response = ReviewQueueItem.from_domain(record)

    dumped = json.loads(response.model_dump_json())

    assert dumped["total_amount"] == "1234.10"
    assert "e" not in dumped["total_amount"].lower()  # never scientific notation


def test_none_monetary_value_is_not_silently_coerced_to_zero():
    record = _queue_record(total_amount=None)
    response = ReviewQueueItem.from_domain(record)

    dumped = json.loads(response.model_dump_json())

    assert dumped["total_amount"] is None


def test_enum_fields_serialize_to_their_declared_string_values():
    record = _queue_record()
    response = ReviewQueueItem.from_domain(record)
    dumped = json.loads(response.model_dump_json())

    assert dumped["workflow_status"] == "REVIEW_REQUIRED"
    assert dumped["current_stage"] == "HUMAN_REVIEW"
    assert dumped["case_status"] == "OPEN"
    assert dumped["priority"] == "NORMAL"


def test_uuid_fields_serialize_as_canonical_strings():
    record = _queue_record()
    response = ReviewQueueItem.from_domain(record)
    dumped = json.loads(response.model_dump_json())

    assert dumped["review_case_id"] == str(record.review_case_id)
    assert dumped["tenant_id"] == str(record.tenant_id)


def test_extra_fields_are_rejected_not_silently_dropped():
    with pytest.raises(Exception):
        ReviewQueueItem(**{**ReviewQueueItem.from_domain(_queue_record()).model_dump(), "unexpected_field": 1})


def test_pagination_metadata_computes_total_pages():
    page = ReviewQueuePage(items=(_queue_record(),), page=2, page_size=1, total_count=3)
    response = ReviewQueuePageResponse.from_domain(page)

    assert response.pagination.page == 2
    assert response.pagination.page_size == 1
    assert response.pagination.total_count == 3
    assert response.pagination.total_pages == 3


def test_dashboard_review_reason_counts_are_explicit_pairs():
    record = DashboardRecord(
        tenant_id=uuid4(), generated_at=NOW, total_invoices=4, processing_invoices=0, completed_invoices=1,
        review_required_invoices=3, failed_invoices=0, open_review_cases=3, unassigned_review_cases=3,
        review_reason_counts=(("INHERITED_FINANCIAL_VALIDATION_REVIEW", 3), ("SUPPLIER_NAME_MISSING", 2)),
    )
    response = DashboardResponse.from_domain(record)
    dumped = json.loads(response.model_dump_json())

    assert dumped["review_reason_counts"] == [
        {"reason": "INHERITED_FINANCIAL_VALIDATION_REVIEW", "count": 3},
        {"reason": "SUPPLIER_NAME_MISSING", "count": 2},
    ]
