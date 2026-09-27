"""M10 unit tests: pure row <-> domain mapping and payload-integrity
verification in `ap_agent.repositories.review_repository`
(no PostgreSQL connection -- these exercise the mapping/verification logic
directly against hand-built row tuples shaped like the notebook's SQL
projections, cells 101/102/104-106)."""

from __future__ import annotations

from hashlib import sha256
from uuid import uuid4

import pytest

from ap_agent.config.postgres import MemoryConfig
from ap_agent.exceptions import ReviewIntegrityError
from ap_agent.models.interface import ReviewCaseStatus, ReviewPriority
from ap_agent.models.orchestration import InvoiceWorkflowStatus
from ap_agent.repositories.review_repository import (
    ReviewRepository,
    command_case_status_from_database,
    human_review_disposition_from_database,
    review_case_status_from_database,
    review_priority_from_database,
    workflow_status_from_database,
)
from ap_agent.serialization.memory_json import canonical_json_bytes

pytestmark = pytest.mark.unit


def _repository() -> ReviewRepository:
    return ReviewRepository("postgresql://unused/unused", MemoryConfig())


# ------------------------------------------------------------
# Pure database-value mapping (notebook cells 101/102/106)
# ------------------------------------------------------------


@pytest.mark.parametrize(
    "database_status,expected",
    [("OPEN", ReviewCaseStatus.OPEN), ("CLAIMED", ReviewCaseStatus.IN_REVIEW)],
)
def test_review_case_status_from_database(database_status, expected):
    assert review_case_status_from_database(database_status) == expected


def test_review_case_status_from_database_rejects_unknown_status():
    with pytest.raises(ValueError):
        review_case_status_from_database("RESOLVED")


@pytest.mark.parametrize(
    "database_status,resolution_code,expected",
    [
        ("OPEN", None, ReviewCaseStatus.OPEN),
        ("CLAIMED", None, ReviewCaseStatus.IN_REVIEW),
        ("RESOLVED", "APPROVED", ReviewCaseStatus.RESOLVED),
        ("RESOLVED", "REJECTED", ReviewCaseStatus.REJECTED),
        ("CANCELLED", None, ReviewCaseStatus.REJECTED),
    ],
)
def test_command_case_status_from_database(database_status, resolution_code, expected):
    assert command_case_status_from_database(database_status, resolution_code) == expected


@pytest.mark.parametrize(
    "database_priority,expected",
    [
        (1, ReviewPriority.CRITICAL),
        (2, ReviewPriority.HIGH),
        (3, ReviewPriority.NORMAL),
        (4, ReviewPriority.LOW),
        (5, ReviewPriority.LOW),
    ],
)
def test_review_priority_from_database(database_priority, expected):
    assert review_priority_from_database(database_priority) == expected


def test_review_priority_from_database_rejects_out_of_range():
    with pytest.raises(ValueError):
        review_priority_from_database(0)

    with pytest.raises(ValueError):
        review_priority_from_database(6)


def test_workflow_status_from_database_round_trips():
    assert workflow_status_from_database("REVIEW_REQUIRED") == InvoiceWorkflowStatus.REVIEW_REQUIRED


def test_workflow_status_from_database_rejects_unknown():
    with pytest.raises(ValueError):
        workflow_status_from_database("NOT_A_REAL_STATUS")


@pytest.mark.parametrize(
    "decision_type,expected",
    [
        ("ACCEPT", "APPROVED"),
        ("approve", "APPROVED"),
        ("REJECTED", "REJECTED"),
        ("HOLD", "HOLD"),
        ("request_information", "NEEDS_INFORMATION"),
        ("CORRECTED", "CORRECTED"),
    ],
)
def test_human_review_disposition_from_database(decision_type, expected):
    assert human_review_disposition_from_database(decision_type).value == expected


def test_human_review_disposition_from_database_rejects_unknown():
    with pytest.raises(ValueError):
        human_review_disposition_from_database("NOT_A_REAL_DECISION")


# ------------------------------------------------------------
# Queue-row projection: payload-hash and tenant checks (cell 101)
# ------------------------------------------------------------


def _queue_row(
    tenant_id,
    *,
    payload_sha256=None,
    review_reason_codes=("SUPPLIER_NAME_MISSING",),
    review_status="OPEN",
    resolution_code=None,
):
    normalized_payload = {"invoice_record": {"fields": []}}
    financial_payload = {"checks": []}
    matching_payload = {"line_matches": []}
    reference_payload = {}

    reconstructed = {
        "normalization_result": normalized_payload,
        "financial_validation_result": financial_payload,
        "matching_result": matching_payload,
        "matched_reference_data": reference_payload,
    }
    correct_hash = sha256(canonical_json_bytes(reconstructed)).hexdigest()

    return (
        uuid4(),  # review_case_id
        tenant_id,  # stored_tenant_id
        uuid4(),  # workflow_id
        uuid4(),  # batch_id
        uuid4(),  # document_id
        "invoice.pdf",  # source_name
        "HUMAN_REVIEW",  # current_phase
        "REVIEW_REQUIRED",  # workflow_status
        3,  # workflow_revision
        __import__("datetime").datetime(2026, 1, 1, tzinfo=__import__("datetime").timezone.utc),  # updated_at
        review_status,  # review_status
        resolution_code,  # resolution_code
        3,  # priority
        list(review_reason_codes),  # review_reason_codes
        None,  # assigned_reviewer_id
        __import__("datetime").datetime(2026, 1, 1, tzinfo=__import__("datetime").timezone.utc),  # opened_at
        "INV-1",  # invoice_number
        "Acme",  # supplier_name
        "USD",  # currency
        None,  # total_amount
        "MATCHED",  # supplier_status
        "MATCHED",  # purchase_order_status
        "FAILED",  # financial_status
        list(review_reason_codes),  # invoice_review_reasons
        payload_sha256 or correct_hash,  # stored_payload_sha256
        normalized_payload,
        financial_payload,
        matching_payload,
        reference_payload,
        0,  # decision_count
    )


def test_row_to_queue_record_computes_revision_from_decision_count():
    tenant_id = uuid4()
    row = _queue_row(tenant_id)
    record = _repository()._row_to_queue_record(tenant_id, row)

    assert record.revision == 1
    assert record.case_status == ReviewCaseStatus.OPEN
    assert record.priority == ReviewPriority.NORMAL


def test_row_to_queue_record_rejects_cross_tenant_row():
    tenant_id = uuid4()
    row = _queue_row(uuid4())  # different tenant baked into the row

    with pytest.raises(ReviewIntegrityError):
        _repository()._row_to_queue_record(tenant_id, row)


def test_row_to_queue_record_rejects_payload_hash_mismatch():
    tenant_id = uuid4()
    row = _queue_row(tenant_id, payload_sha256="0" * 64)

    with pytest.raises(ReviewIntegrityError):
        _repository()._row_to_queue_record(tenant_id, row)


def test_row_to_queue_record_reports_rejected_status_from_resolution_code():
    tenant_id = uuid4()
    row = _queue_row(tenant_id, review_status="RESOLVED", resolution_code="REJECTED")
    record = _repository()._row_to_queue_record(tenant_id, row)

    assert record.case_status == ReviewCaseStatus.REJECTED


def test_row_to_queue_record_reports_resolved_status_from_resolution_code():
    tenant_id = uuid4()
    row = _queue_row(tenant_id, review_status="RESOLVED", resolution_code="APPROVED")
    record = _repository()._row_to_queue_record(tenant_id, row)

    assert record.case_status == ReviewCaseStatus.RESOLVED


def test_row_to_queue_record_rejects_review_reason_mismatch():
    tenant_id = uuid4()
    row = list(_queue_row(tenant_id))
    row[23] = ["A_DIFFERENT_REASON"]  # invoice_review_reasons index, disagrees with review_reason_codes

    with pytest.raises(ReviewIntegrityError):
        _repository()._row_to_queue_record(tenant_id, tuple(row))
