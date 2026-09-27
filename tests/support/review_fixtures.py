"""Shared PostgreSQL seeding helpers for the M10 `requires_postgres` API
and repository/service integration tests.

`ap_agent.repositories.review_repository.ReviewRepository` only ever
*reads and updates* an existing `ap_agent.review_cases` row -- Phase 9
never creates one (that happens when Phase 8 orchestration routes an
invoice to `HUMAN_REVIEW`, out of scope for this milestone). These helpers
seed exactly the rows a real Phase 1-8 run would have left behind: a
tenant, a `workflow_instances` row, an `invoice_memory_records` row (via
`ap_agent.repositories.postgres_memory_repository.PostgresMemoryRepository`,
M8's own repository), and a `review_cases` row (raw SQL -- no M8/M9
repository exposes review-case creation, so this is deliberately
test-only, never imported by production code).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from ap_agent.config.postgres import MemoryConfig
from ap_agent.db.connection import open_connection, set_tenant_context
from ap_agent.models.memory import MemoryWorkflowStage, MemoryWorkflowStatus, WorkflowMemoryRecord
from ap_agent.repositories.postgres_memory_repository import PostgresMemoryRepository
from ap_agent.serialization.memory_json import canonical_payload_sha256


@dataclass(frozen=True)
class SeededReviewCase:
    tenant_id: uuid.UUID
    workflow_id: uuid.UUID
    batch_id: uuid.UUID
    document_id: uuid.UUID
    review_case_id: uuid.UUID
    review_reasons: tuple[str, ...]


def _normalized_invoice_payload(document_id: uuid.UUID, *, po_number: str = "99") -> dict[str, Any]:
    return {
        "document_id": str(document_id),
        "invoice_record": {
            "fields": [
                {
                    "field_name": "PURCHASE_ORDER_NUMBER",
                    "raw_value": po_number,
                    "normalized_value": po_number,
                    "value_type": "TEXT",
                    "confidence": 0.9,
                    "review_required": False,
                    "evidence_references": [{"reference_id": "ev-po-1"}],
                },
                {
                    "field_name": "SUPPLIER_NAME",
                    "raw_value": None,
                    "normalized_value": None,
                    "value_type": "TEXT",
                    "confidence": None,
                    "review_required": True,
                    "evidence_references": [],
                },
            ],
            "line_items": [
                {"line_number": 1, "description": "Widget", "quantity": "5", "unit_price": "1.00"},
            ],
        },
    }


def _financial_validation_payload(document_id: uuid.UUID) -> dict[str, Any]:
    return {
        "document_id": str(document_id),
        "checks": [
            {
                "check_id": "check-1",
                "check_type": "TOTAL_RECONCILIATION",
                "status": "FAILED",
                "message": "Total does not reconcile.",
                "expected_value": "5.00",
                "observed_value": "6.00",
                "operands": [{"evidence_reference_ids": ["ev-po-1"]}],
            },
        ],
    }


def _matching_result_payload(document_id: uuid.UUID) -> dict[str, Any]:
    return {
        "document_id": str(document_id),
        "line_matches": [
            {
                "line_match_id": "line-match-1",
                "invoice_line_number": 1,
                "purchase_order_line_number": 1,
                "description_status": "MATCHED",
                "quantity_status": "MATCHED",
                "unit_price_status": "MATCHED",
                "line_total_status": "MATCHED",
                "review_required": False,
                "review_reasons": [],
            },
        ],
    }


def seed_review_case(
    dsn: str,
    config: MemoryConfig,
    *,
    tenant_id: uuid.UUID,
    source_name: str = "seeded-review-case.pdf",
    review_reasons: tuple[str, ...] = ("SUPPLIER_NAME_MISSING",),
    priority: int = 3,
    claimed_by: str | None = None,
) -> SeededReviewCase:
    """Seed one review case for `tenant_id`, `OPEN` by default, or already
    `CLAIMED` by `claimed_by` when given (for tests that need a case a
    specific reviewer already owns, without going through a real CLAIM
    command). The caller must have already registered the tenant."""

    repository = PostgresMemoryRepository(dsn, config)

    document_id = uuid.uuid4()
    batch_id = uuid.uuid4()
    workflow_id = uuid.uuid4()

    workflow_record = repository.create_or_get_workflow(
        tenant_id=tenant_id,
        record=WorkflowMemoryRecord(
            memory_id=workflow_id,
            tenant_id=str(tenant_id),
            batch_id=batch_id,
            document_id=document_id,
            source_name=source_name,
            source_document_sha256="a" * 64,
            current_stage=MemoryWorkflowStage.HUMAN_REVIEW,
            current_status=MemoryWorkflowStatus.REVIEW_REQUIRED,
            review_required=True,
            review_reasons=review_reasons,
            latest_matching_result_id=None,
            revision=1,
            created_at=__import__("datetime").datetime.now(__import__("datetime").timezone.utc),
            updated_at=__import__("datetime").datetime.now(__import__("datetime").timezone.utc),
        ),
    )

    normalized_payload = _normalized_invoice_payload(document_id)
    financial_payload = _financial_validation_payload(document_id)
    matching_payload = _matching_result_payload(document_id)
    reference_payload = {"supplier": None, "purchase_order": None, "goods_receipts": []}

    complete_payload = {
        "normalization_result": normalized_payload,
        "financial_validation_result": financial_payload,
        "matching_result": matching_payload,
        "matched_reference_data": reference_payload,
    }

    from ap_agent.models.memory import MatchedInvoiceMemoryRecord

    repository.store_invoice_memory(
        tenant_id=tenant_id,
        record=MatchedInvoiceMemoryRecord(
            record_id=uuid.uuid4(),
            tenant_id=tenant_id,
            workflow_memory_id=workflow_record.memory_id,
            batch_id=batch_id,
            document_id=document_id,
            matching_result_id=uuid.uuid4(),
            source_name=source_name,
            source_document_sha256="a" * 64,
            invoice_record_id=uuid.uuid4(),
            invoice_number="INV-SEED-1",
            supplier_name=None,
            currency="USD",
            total_amount=Decimal("6.00"),
            supplier_resolution_status="NOT_REFERENCED",
            matched_supplier_id=None,
            purchase_order_status="MATCHED",
            purchase_order_id=None,
            purchase_order_number="99",
            goods_receipt_status="NOT_REFERENCED",
            goods_receipt_ids=(),
            match_mode="AUTOMATIC",
            line_match_count=1,
            normalization_status="SUCCEEDED",
            financial_validation_status="FAILED",
            matching_status="SUCCEEDED",
            review_required=True,
            review_reasons=review_reasons,
            normalized_payload=normalized_payload,
            financial_payload=financial_payload,
            matching_payload=matching_payload,
            reference_payload=reference_payload,
            payload_sha256=canonical_payload_sha256(complete_payload),
        ),
    )

    review_case_id = uuid.uuid4()
    review_status = "CLAIMED" if claimed_by is not None else "OPEN"

    with open_connection(dsn, config) as connection:
        with connection.cursor() as cursor:
            set_tenant_context(cursor, str(tenant_id))
            cursor.execute(
                """
                INSERT INTO ap_agent.review_cases
                    (review_id, tenant_id, workflow_id, review_status, priority,
                     reason_codes, summary, assigned_to)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s);
                """,
                (
                    review_case_id,
                    tenant_id,
                    workflow_record.memory_id,
                    review_status,
                    priority,
                    list(review_reasons),
                    f"Review required: {', '.join(review_reasons)}",
                    claimed_by,
                ),
            )

    return SeededReviewCase(
        tenant_id=tenant_id,
        workflow_id=workflow_record.memory_id,
        batch_id=batch_id,
        document_id=document_id,
        review_case_id=review_case_id,
        review_reasons=review_reasons,
    )
