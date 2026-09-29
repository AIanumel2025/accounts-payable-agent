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
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from ap_agent.config.postgres import MemoryConfig
from ap_agent.db.connection import open_connection, set_tenant_context
from ap_agent.models.memory import (
    AuditMemoryEvent,
    MatchedInvoiceMemoryRecord,
    MemoryActorType,
    MemoryEventType,
    MemoryWorkflowStage,
    MemoryWorkflowStatus,
    WorkflowMemoryRecord,
)
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


# ------------------------------------------------------------
# Automatically-completed invoice (no review required) -- added for the
# M11A real-integration acceptance baseline
# (docs/m11a_frontend_foundation_report.md, "Real FastAPI/PostgreSQL
# integration result"), which needs one invoice that resolved without
# review alongside the three review-required ones `seed_review_case`
# already covers. Purely additive: does not change `seed_review_case` or
# any existing caller.
# ------------------------------------------------------------


@dataclass(frozen=True)
class SeededCompletedInvoice:
    tenant_id: uuid.UUID
    workflow_id: uuid.UUID
    batch_id: uuid.UUID
    document_id: uuid.UUID


def _completed_normalized_invoice_payload(document_id: uuid.UUID, *, po_number: str = "88") -> dict[str, Any]:
    return {
        "document_id": str(document_id),
        "invoice_record": {
            "fields": [
                {
                    "field_name": "PURCHASE_ORDER_NUMBER",
                    "raw_value": po_number,
                    "normalized_value": po_number,
                    "value_type": "TEXT",
                    "confidence": 0.98,
                    "review_required": False,
                    "evidence_references": [{"reference_id": "ev-po-1"}],
                },
                {
                    "field_name": "SUPPLIER_NAME",
                    "raw_value": "Acme Supplies Ltd",
                    "normalized_value": "ACME SUPPLIES LTD",
                    "value_type": "TEXT",
                    "confidence": 0.97,
                    "review_required": False,
                    "evidence_references": [{"reference_id": "ev-supplier-1"}],
                },
            ],
            "line_items": [
                {"line_number": 1, "description": "Widget", "quantity": "5", "unit_price": "1.00"},
            ],
        },
    }


def _completed_financial_validation_payload(document_id: uuid.UUID) -> dict[str, Any]:
    return {
        "document_id": str(document_id),
        "checks": [
            {
                "check_id": "check-1",
                "check_type": "TOTAL_RECONCILIATION",
                "status": "PASSED",
                "message": "Total reconciles.",
                "expected_value": "5.00",
                "observed_value": "5.00",
                "operands": [{"evidence_reference_ids": ["ev-po-1"]}],
            },
        ],
    }


def _completed_matching_result_payload(document_id: uuid.UUID) -> dict[str, Any]:
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


def seed_completed_invoice(
    dsn: str,
    config: MemoryConfig,
    *,
    tenant_id: uuid.UUID,
    source_name: str = "seeded-completed-invoice.pdf",
) -> SeededCompletedInvoice:
    """Seed one automatically-completed invoice for `tenant_id`: a
    `SUCCEEDED` workflow with `review_required=False` and a matching
    invoice-memory record, and (unlike `seed_review_case`) no review case
    at all -- mirrors what a real Phase 1-8 run leaves behind when nothing
    needs human review. The caller must have already registered the
    tenant.
    """

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
            source_document_sha256="b" * 64,
            current_stage=MemoryWorkflowStage.COMPLETED,
            current_status=MemoryWorkflowStatus.SUCCEEDED,
            review_required=False,
            review_reasons=(),
            latest_matching_result_id=None,
            revision=1,
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
        ),
    )

    normalized_payload = _completed_normalized_invoice_payload(document_id)
    financial_payload = _completed_financial_validation_payload(document_id)
    matching_payload = _completed_matching_result_payload(document_id)
    reference_payload = {"supplier": None, "purchase_order": None, "goods_receipts": []}

    complete_payload = {
        "normalization_result": normalized_payload,
        "financial_validation_result": financial_payload,
        "matching_result": matching_payload,
        "matched_reference_data": reference_payload,
    }

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
            source_document_sha256="b" * 64,
            invoice_record_id=uuid.uuid4(),
            invoice_number="INV-SEED-COMPLETED-1",
            supplier_name="Acme Supplies Ltd",
            currency="USD",
            total_amount=Decimal("5.00"),
            supplier_resolution_status="MATCHED",
            matched_supplier_id="SUP-1",
            purchase_order_status="MATCHED",
            purchase_order_id=None,
            purchase_order_number="88",
            goods_receipt_status="NOT_REFERENCED",
            goods_receipt_ids=(),
            match_mode="AUTOMATIC",
            line_match_count=1,
            normalization_status="SUCCEEDED",
            financial_validation_status="SUCCEEDED",
            matching_status="SUCCEEDED",
            review_required=False,
            review_reasons=(),
            normalized_payload=normalized_payload,
            financial_payload=financial_payload,
            matching_payload=matching_payload,
            reference_payload=reference_payload,
            payload_sha256=canonical_payload_sha256(complete_payload),
        ),
    )

    return SeededCompletedInvoice(
        tenant_id=tenant_id,
        workflow_id=workflow_record.memory_id,
        batch_id=batch_id,
        document_id=document_id,
    )


# ------------------------------------------------------------
# M11B named acceptance fixtures -- added for the M11B invoice/review-case
# detail page's exact per-fixture acceptance baseline
# (docs/m11b_review_queue_detail_report.md, "Controlled-fixture detail
# baseline"). `seed_review_case` above produces a generic, minimal
# (2-field/1-check/1-line-match) review case; M11B's acceptance suite needs
# three *specific*, realistically detailed documents whose `fields`/
# `financial_checks`/`line_matches` counts match real, independently
# verified numbers, not arbitrary placeholders.
#
# Every field value, financial-check status, and line-match count below was
# cross-checked against the real Phase 1-8 golden baselines already in the
# repository (`tests/golden/phase_4_expected_results.json` for field
# values/counts, `phase_5_expected_results.json` for financial-check
# statuses/counts via `expected_header_checks` +
# `expected_line_item_arithmetic_statuses` + `expected_subtotal_
# reconciliation` + `expected_total_reconciliation`, `phase_6_expected_
# results.json` for line-match counts and supplier/PO/goods-receipt
# resolution) -- not invented. The arithmetic is fully explained per
# document below; each total reproduces exactly.
#
# The one number with no corresponding golden data is the evidence-
# reference count (`evidence_reference_ids` totals of 18/25/22): the
# golden baselines predate the Phase 9 interface projection and never
# recorded per-field/per-check evidence-reference counts. `_assign_evidence`
# below is a documented, fully deterministic scheme -- one evidence
# reference per successfully-evaluated field/check (a null field or a
# SKIPPED/NOT_APPLICABLE check gets none, since there is nothing to cite),
# then extra references distributed round-robin (in field-then-check,
# then-declaration order) until the fixture's target total is reached --
# not a fabricated one-off number per item.
# ------------------------------------------------------------


def _field_payload(field_name: str, value: str | None, value_type: str, *, confidence: float | None) -> dict[str, Any]:
    return {
        "field_name": field_name,
        "raw_value": value,
        "normalized_value": value,
        "value_type": value_type,
        "confidence": confidence,
        "review_required": value is None,
        "evidence_references": [],
    }


def _check_payload(
    check_id: str, check_type: str, status: str, message: str, *, expected: str | None = None, observed: str | None = None
) -> dict[str, Any]:
    return {
        "check_id": check_id,
        "check_type": check_type,
        "status": status,
        "message": message,
        "expected_value": expected,
        "observed_value": observed,
        "operands": [],
    }


def _line_match_payload(
    line_match_id: str,
    line_number: int,
    *,
    line_total_status: str = "MATCHED",
    review_required: bool = False,
    review_reasons: tuple[str, ...] = (),
) -> dict[str, Any]:
    return {
        "line_match_id": line_match_id,
        "invoice_line_number": line_number,
        "purchase_order_line_number": line_number,
        "description_status": "MATCHED",
        "quantity_status": "MATCHED",
        "unit_price_status": "MATCHED",
        "line_total_status": line_total_status,
        "review_required": review_required,
        "review_reasons": list(review_reasons),
    }


def _assign_evidence(fields: list[dict[str, Any]], checks: list[dict[str, Any]], target_total: int, prefix: str) -> None:
    """Mutates `fields`/`checks` in place, assigning `evidence_references`/
    `operands[].evidence_reference_ids` per the scheme documented above."""

    eligible: list[dict[str, Any]] = [f for f in fields if f["raw_value"] is not None] + [
        c for c in checks if c["status"] not in ("SKIPPED", "NOT_APPLICABLE")
    ]

    if target_total < len(eligible):
        raise ValueError(f"target_total {target_total} is less than the eligible item count {len(eligible)}.")

    counts = [1] * len(eligible)
    for i in range(target_total - len(eligible)):
        counts[i % len(eligible)] += 1

    next_id = 1
    for item, count in zip(eligible, counts):
        reference_ids = [f"ev-{prefix}-{next_id + offset}" for offset in range(count)]
        next_id += count
        if "field_name" in item:
            item["evidence_references"] = [{"reference_id": reference_id} for reference_id in reference_ids]
        else:
            item["operands"] = [{"evidence_reference_ids": reference_ids}]


@dataclass(frozen=True)
class NamedFixtureBaseline:
    """Everything `seed_named_review_case` needs for one named acceptance
    fixture: real field/check/line-match content plus the queue-level
    resolution metadata (`MatchedInvoiceMemoryRecord`'s own fields)."""

    source_name: str
    priority: int
    review_reasons: tuple[str, ...]
    normalized_payload_fields: tuple[dict[str, Any], ...]
    financial_checks: tuple[dict[str, Any], ...]
    line_matches: tuple[dict[str, Any], ...]
    evidence_target_total: int
    invoice_number: str | None
    supplier_name: str | None
    currency: str | None
    total_amount: str | None
    supplier_resolution_status: str
    matched_supplier_id: str | None
    purchase_order_status: str
    purchase_order_number: str | None
    goods_receipt_status: str
    goods_receipt_ids_count: int
    match_mode: str


def _template1_baseline() -> NamedFixtureBaseline:
    # Fields: the 9 fields Phase 4 (`phase_4_expected_results.json`) extracted
    # a non-null value for; SUPPLIER_NAME/INVOICE_NUMBER were both null there
    # (consistent with this document's own SUPPLIER_NAME_MISSING reason) and
    # are intentionally not included as separate null field rows.
    fields = [
        _field_payload("CUSTOMER_NAME", "Heather Snyder", "TEXT", confidence=0.95),
        _field_payload("INVOICE_DATE", "2000-04-12", "DATE", confidence=0.92),
        _field_payload("DUE_DATE", "1998-03-15", "DATE", confidence=0.9),
        _field_payload("PURCHASE_ORDER_NUMBER", "99", "TEXT", confidence=0.97),
        _field_payload("CURRENCY", "EUR", "CURRENCY_CODE", confidence=0.99),
        _field_payload("SUBTOTAL", "858.86", "DECIMAL", confidence=0.8),
        _field_payload("TAX_AMOUNT", "36.45", "DECIMAL", confidence=0.75),
        _field_payload("DISCOUNT_AMOUNT", "-12.54", "DECIMAL", confidence=0.7),
        _field_payload("TOTAL_AMOUNT", "873.58", "DECIMAL", confidence=0.6),
    ]
    assert len(fields) == 9

    # Checks: 5 header checks + 5 per-line LINE_ITEM_ARITHMETIC (all SKIPPED,
    # matching `expected_line_item_arithmetic_statuses`) + LINE_ITEMS_TO_
    # SUBTOTAL (SKIPPED) + INVOICE_TOTAL_RECONCILIATION (FAILED) = 12,
    # matching `expected_summary.total_checks` exactly; the individual
    # PASSED/FAILED/REVIEW_REQUIRED/SKIPPED counts below reproduce
    # `expected_summary` (2/3/1/0/6) exactly too.
    checks = [
        _check_payload("check-inherited-review", "INHERITED_REVIEW", "REVIEW_REQUIRED", "Inherited review from an earlier phase."),
        _check_payload("check-required-fields", "REQUIRED_FINANCIAL_FIELDS", "PASSED", "Required financial fields present."),
        _check_payload("check-monetary-validity", "MONETARY_VALUE_VALIDITY", "PASSED", "Monetary values are well-formed."),
        _check_payload("check-date-consistency", "DATE_CONSISTENCY", "FAILED", "Due date precedes invoice date.", expected="on or after 2000-04-12", observed="1998-03-15"),
        _check_payload("check-currency-consistency", "CURRENCY_CONSISTENCY", "FAILED", "Currency inconsistent with reference data.", expected="USD", observed="EUR"),
        *(
            _check_payload(f"check-line-arith-{i}", "LINE_ITEM_ARITHMETIC", "SKIPPED", "Line item arithmetic not evaluated.")
            for i in range(1, 6)
        ),
        _check_payload("check-subtotal-reconciliation", "LINE_ITEMS_TO_SUBTOTAL", "SKIPPED", "Subtotal reconciliation not evaluated."),
        _check_payload(
            "check-total-reconciliation", "INVOICE_TOTAL_RECONCILIATION", "FAILED", "Total does not reconcile.",
            expected="882.77", observed="873.58",
        ),
    ]
    assert len(checks) == 12

    # Line matches: 5 (matches `phase_6_expected_results.json`'s
    # `line_matches: 5`); one carries the INVOICE_LINE_TOTAL_MISSING reason
    # (this document's own second review reason), the rest fully matched.
    line_matches = [
        _line_match_payload("line-match-1", 1),
        _line_match_payload("line-match-2", 2),
        _line_match_payload("line-match-3", 3),
        _line_match_payload("line-match-4", 4),
        _line_match_payload(
            "line-match-5", 5, line_total_status="REVIEW_REQUIRED", review_required=True,
            review_reasons=("INVOICE_LINE_TOTAL_MISSING",),
        ),
    ]
    assert len(line_matches) == 5

    return NamedFixtureBaseline(
        source_name="Template1_Instance90.jpg",
        priority=2,  # HIGH
        review_reasons=("SUPPLIER_NAME_MISSING", "INVOICE_LINE_TOTAL_MISSING", "INHERITED_FINANCIAL_VALIDATION_REVIEW"),
        normalized_payload_fields=tuple(fields),
        financial_checks=tuple(checks),
        line_matches=tuple(line_matches),
        evidence_target_total=18,
        invoice_number=None,
        supplier_name=None,
        currency="EUR",
        total_amount="873.58",
        supplier_resolution_status="NOT_FOUND",
        matched_supplier_id=None,
        purchase_order_status="MATCHED",
        purchase_order_number="99",
        goods_receipt_status="MATCHED",
        goods_receipt_ids_count=1,
        match_mode="THREE_WAY",
    )


def _08181_warped_baseline() -> NamedFixtureBaseline:
    # Fields: the 6 fields Phase 4 extracted a non-null value for (DUE_DATE/
    # CURRENCY/TOTAL_AMOUNT were null there).
    fields = [
        _field_payload("SUPPLIER_NAME", "Snyder, Hammond and Anderson", "TEXT", confidence=0.88),
        _field_payload("CUSTOMER_NAME", "Clark-Williams", "TEXT", confidence=0.9),
        _field_payload("INVOICE_NUMBER", "308044", "TEXT", confidence=0.93),
        _field_payload("INVOICE_DATE", "2002-06-28", "DATE", confidence=0.85),
        _field_payload("SUBTOTAL", "63.45", "DECIMAL", confidence=0.65),
        _field_payload("TAX_AMOUNT", "5.77", "DECIMAL", confidence=0.6),
    ]
    assert len(fields) == 6

    # Checks: 5 header + 5 per-line LINE_ITEM_ARITHMETIC (all PASSED) +
    # LINE_ITEMS_TO_SUBTOTAL (PASSED) + INVOICE_TOTAL_RECONCILIATION
    # (REVIEW_REQUIRED, since TOTAL_AMOUNT is null -- "no calculated total
    # ever written back", per the golden file's own note) = 12, matching
    # `expected_summary.total_checks`; PASSED/REVIEW_REQUIRED/NOT_APPLICABLE
    # counts (7/4/1) reproduce `expected_summary` exactly.
    checks = [
        _check_payload("check-inherited-review", "INHERITED_REVIEW", "REVIEW_REQUIRED", "Inherited review from an earlier phase."),
        _check_payload("check-required-fields", "REQUIRED_FINANCIAL_FIELDS", "REVIEW_REQUIRED", "TOTAL_AMOUNT is missing.", expected="present", observed=None),
        _check_payload("check-monetary-validity", "MONETARY_VALUE_VALIDITY", "PASSED", "Monetary values are well-formed."),
        _check_payload("check-date-consistency", "DATE_CONSISTENCY", "NOT_APPLICABLE", "No due date on this invoice to compare."),
        _check_payload("check-currency-consistency", "CURRENCY_CONSISTENCY", "REVIEW_REQUIRED", "CURRENCY is missing.", expected="present", observed=None),
        *(
            _check_payload(f"check-line-arith-{i}", "LINE_ITEM_ARITHMETIC", "PASSED", "Line item arithmetic reconciles.")
            for i in range(1, 6)
        ),
        _check_payload(
            "check-subtotal-reconciliation", "LINE_ITEMS_TO_SUBTOTAL", "PASSED", "Subtotal reconciles with line items.",
            expected="63.45", observed="63.45",
        ),
        _check_payload(
            "check-total-reconciliation", "INVOICE_TOTAL_RECONCILIATION", "REVIEW_REQUIRED",
            "TOTAL_AMOUNT is missing; calculated total was never persisted.",
            expected="69.22", observed=None,
        ),
    ]
    assert len(checks) == 12

    return NamedFixtureBaseline(
        source_name="08181_warped_document_perspective_shadow.jpg",
        priority=3,  # NORMAL
        review_reasons=("INHERITED_FINANCIAL_VALIDATION_REVIEW",),
        normalized_payload_fields=tuple(fields),
        financial_checks=tuple(checks),
        line_matches=(),  # purchase_order_status=NOT_REFERENCED -- nothing to match a line against.
        evidence_target_total=25,
        invoice_number="308044",
        supplier_name="Snyder, Hammond and Anderson",
        currency=None,
        total_amount=None,
        supplier_resolution_status="MATCHED",
        matched_supplier_id="SUP-001",
        purchase_order_status="NOT_REFERENCED",
        purchase_order_number=None,
        goods_receipt_status="NOT_REFERENCED",
        goods_receipt_ids_count=0,
        match_mode="UNDETERMINED",
    )


def _aaron_bergman_baseline() -> NamedFixtureBaseline:
    # Fields: the 9 fields Phase 4 extracted a non-null value for, plus an
    # explicit null SUPPLIER_NAME row -- this document's own SUPPLIER_NAME_
    # MISSING review reason -- for 10 total.
    fields = [
        _field_payload("SUPPLIER_NAME", None, "TEXT", confidence=None),
        _field_payload("CUSTOMER_NAME", "Aaron Bergman", "TEXT", confidence=0.96),
        _field_payload("INVOICE_NUMBER", "36258", "TEXT", confidence=0.98),
        _field_payload("INVOICE_DATE", "2012-03-06", "DATE", confidence=0.93),
        _field_payload("PURCHASE_ORDER_NUMBER", "CA-2012-AB10015140-40974", "TEXT", confidence=0.9),
        _field_payload("CURRENCY", "USD", "CURRENCY_CODE", confidence=0.99),
        _field_payload("SUBTOTAL", "48.71", "DECIMAL", confidence=0.7),
        _field_payload("DISCOUNT_AMOUNT", "9.74", "DECIMAL", confidence=0.65),
        _field_payload("SHIPPING_AMOUNT", "11.13", "DECIMAL", confidence=0.68),
        _field_payload("TOTAL_AMOUNT", "50.10", "DECIMAL", confidence=0.6),
    ]
    assert len(fields) == 10

    # Checks: 5 header + 1 LINE_ITEM_ARITHMETIC (PASSED, this document has 1
    # line item) + LINE_ITEMS_TO_SUBTOTAL (PASSED) + INVOICE_TOTAL_
    # RECONCILIATION (PASSED) = 8, matching `expected_summary.total_checks`;
    # PASSED/REVIEW_REQUIRED/NOT_APPLICABLE counts (6/1/1) reproduce
    # `expected_summary` exactly.
    checks = [
        _check_payload("check-inherited-review", "INHERITED_REVIEW", "REVIEW_REQUIRED", "Inherited review from an earlier phase."),
        _check_payload("check-required-fields", "REQUIRED_FINANCIAL_FIELDS", "PASSED", "Required financial fields present."),
        _check_payload("check-monetary-validity", "MONETARY_VALUE_VALIDITY", "PASSED", "Monetary values are well-formed."),
        _check_payload("check-date-consistency", "DATE_CONSISTENCY", "NOT_APPLICABLE", "No due date on this invoice to compare."),
        _check_payload("check-currency-consistency", "CURRENCY_CONSISTENCY", "PASSED", "Currency consistent with reference data."),
        _check_payload("check-line-arith-1", "LINE_ITEM_ARITHMETIC", "PASSED", "Line item arithmetic reconciles."),
        _check_payload(
            "check-subtotal-reconciliation", "LINE_ITEMS_TO_SUBTOTAL", "PASSED", "Subtotal reconciles with line items.",
            expected="48.71", observed="48.71",
        ),
        _check_payload(
            "check-total-reconciliation", "INVOICE_TOTAL_RECONCILIATION", "PASSED", "Total reconciles.",
            expected="50.10", observed="50.10",
        ),
    ]
    assert len(checks) == 8

    line_matches = (_line_match_payload("line-match-1", 1),)
    assert len(line_matches) == 1

    return NamedFixtureBaseline(
        source_name="invoice_Aaron Bergman_36258.pdf",
        priority=1,  # CRITICAL
        review_reasons=("SUPPLIER_NAME_MISSING", "INHERITED_FINANCIAL_VALIDATION_REVIEW"),
        normalized_payload_fields=tuple(fields),
        financial_checks=tuple(checks),
        line_matches=line_matches,
        evidence_target_total=22,
        invoice_number="36258",
        supplier_name=None,
        currency="USD",
        total_amount="50.10",
        supplier_resolution_status="NOT_FOUND",
        matched_supplier_id=None,
        purchase_order_status="MATCHED",
        purchase_order_number="CA-2012-AB10015140-40974",
        goods_receipt_status="MATCHED",
        goods_receipt_ids_count=1,
        match_mode="THREE_WAY",
    )


NAMED_ACCEPTANCE_FIXTURE_KEYS = ("template1", "08181_warped", "aaron_bergman")

_NAMED_FIXTURE_BUILDERS = {
    "template1": _template1_baseline,
    "08181_warped": _08181_warped_baseline,
    "aaron_bergman": _aaron_bergman_baseline,
}


def seed_named_review_case(
    dsn: str,
    config: MemoryConfig,
    *,
    tenant_id: uuid.UUID,
    fixture_key: str,
) -> SeededReviewCase:
    """Seeds one of the three M11B named acceptance fixtures
    (`NAMED_ACCEPTANCE_FIXTURE_KEYS`) with real, cross-checked field/
    financial-check/line-match content and exact `fields`/`financial_
    checks`/`line_matches`/evidence-reference counts (task §9's
    per-fixture detail baseline), plus real `audit_events` rows so the
    detail page's timeline section has genuine content instead of an
    empty list.
    """

    if fixture_key not in _NAMED_FIXTURE_BUILDERS:
        raise ValueError(f"Unknown named acceptance fixture key: {fixture_key!r}.")

    baseline = _NAMED_FIXTURE_BUILDERS[fixture_key]()

    fields = [dict(field_payload) for field_payload in baseline.normalized_payload_fields]
    checks = [dict(check_payload) for check_payload in baseline.financial_checks]
    _assign_evidence(fields, checks, baseline.evidence_target_total, prefix=fixture_key.replace("_", "-"))

    repository = PostgresMemoryRepository(dsn, config)

    document_id = uuid.uuid4()
    batch_id = uuid.uuid4()
    workflow_id = uuid.uuid4()
    now = datetime.now(timezone.utc)

    workflow_record = repository.create_or_get_workflow(
        tenant_id=tenant_id,
        record=WorkflowMemoryRecord(
            memory_id=workflow_id,
            tenant_id=str(tenant_id),
            batch_id=batch_id,
            document_id=document_id,
            source_name=baseline.source_name,
            source_document_sha256="a" * 64,
            current_stage=MemoryWorkflowStage.HUMAN_REVIEW,
            current_status=MemoryWorkflowStatus.REVIEW_REQUIRED,
            review_required=True,
            review_reasons=baseline.review_reasons,
            latest_matching_result_id=None,
            revision=1,
            created_at=now,
            updated_at=now,
        ),
    )

    # `invoice_record.line_items` is not read by `_project_interface_fields`/
    # `_project_financial_checks` (Phase 9 only projects `fields`), so this
    # is inert w.r.t. the API response; kept present and non-empty purely
    # for structural realism (a genuine Phase 4 payload always has it).
    line_item_count = max(len(baseline.line_matches), 1)
    normalized_payload = {
        "document_id": str(document_id),
        "invoice_record": {
            "fields": fields,
            "line_items": [
                {"line_number": i, "description": f"Line {i}", "quantity": "1", "unit_price": "1.00"}
                for i in range(1, line_item_count + 1)
            ],
        },
    }
    financial_payload = {"document_id": str(document_id), "checks": checks}
    matching_payload = {"document_id": str(document_id), "line_matches": list(baseline.line_matches)}
    reference_payload = {"supplier": None, "purchase_order": None, "goods_receipts": []}

    complete_payload = {
        "normalization_result": normalized_payload,
        "financial_validation_result": financial_payload,
        "matching_result": matching_payload,
        "matched_reference_data": reference_payload,
    }

    repository.store_invoice_memory(
        tenant_id=tenant_id,
        record=MatchedInvoiceMemoryRecord(
            record_id=uuid.uuid4(),
            tenant_id=tenant_id,
            workflow_memory_id=workflow_record.memory_id,
            batch_id=batch_id,
            document_id=document_id,
            matching_result_id=uuid.uuid4(),
            source_name=baseline.source_name,
            source_document_sha256="a" * 64,
            invoice_record_id=uuid.uuid4(),
            invoice_number=baseline.invoice_number,
            supplier_name=baseline.supplier_name,
            currency=baseline.currency,
            total_amount=(Decimal(baseline.total_amount) if baseline.total_amount is not None else None),
            supplier_resolution_status=baseline.supplier_resolution_status,
            matched_supplier_id=baseline.matched_supplier_id,
            purchase_order_status=baseline.purchase_order_status,
            purchase_order_id=None,
            purchase_order_number=baseline.purchase_order_number,
            goods_receipt_status=baseline.goods_receipt_status,
            goods_receipt_ids=tuple(f"GR-{i}" for i in range(1, baseline.goods_receipt_ids_count + 1)),
            match_mode=baseline.match_mode,
            line_match_count=len(baseline.line_matches),
            normalization_status="SUCCEEDED",
            financial_validation_status="REVIEW_REQUIRED",
            matching_status="SUCCEEDED",
            review_required=True,
            review_reasons=baseline.review_reasons,
            normalized_payload=normalized_payload,
            financial_payload=financial_payload,
            matching_payload=matching_payload,
            reference_payload=reference_payload,
            payload_sha256=canonical_payload_sha256(complete_payload),
        ),
    )

    review_case_id = uuid.uuid4()

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
                    "OPEN",
                    baseline.priority,
                    list(baseline.review_reasons),
                    f"Review required: {', '.join(baseline.review_reasons)}",
                    None,
                ),
            )

    # Real `audit_events` rows (via the same `append_audit_event` a genuine
    # Phase 1-8 run uses -- notebook cell 83/M8's own repository method, not
    # a raw INSERT) so the detail page's Section G (timeline) has authentic
    # content: one MEMORY_CREATED event for ingestion/normalization, one
    # WORKFLOW_TRANSITIONED event for the transition into HUMAN_REVIEW.
    repository.append_audit_event(
        tenant_id=tenant_id,
        workflow_id=workflow_record.memory_id,
        event=AuditMemoryEvent(
            audit_event_id=uuid.uuid4(),
            tenant_id=str(tenant_id),
            batch_id=batch_id,
            document_id=document_id,
            event_type=MemoryEventType.MEMORY_CREATED,
            actor_type=MemoryActorType.SYSTEM,
            actor_id="acceptance-fixture-seed",
            previous_stage=None,
            new_stage=MemoryWorkflowStage.INGESTION,
            previous_status=None,
            new_status=MemoryWorkflowStatus.SUCCEEDED,
            correlation_id=uuid.uuid4(),
            payload_json="{}",
            payload_sha256="0" * 64,
            occurred_at=now,
        ),
    )
    repository.append_audit_event(
        tenant_id=tenant_id,
        workflow_id=workflow_record.memory_id,
        event=AuditMemoryEvent(
            audit_event_id=uuid.uuid4(),
            tenant_id=str(tenant_id),
            batch_id=batch_id,
            document_id=document_id,
            event_type=MemoryEventType.WORKFLOW_TRANSITIONED,
            actor_type=MemoryActorType.SYSTEM,
            actor_id="acceptance-fixture-seed",
            previous_stage=MemoryWorkflowStage.FINANCIAL_VALIDATION,
            new_stage=MemoryWorkflowStage.HUMAN_REVIEW,
            previous_status=MemoryWorkflowStatus.IN_PROGRESS,
            new_status=MemoryWorkflowStatus.REVIEW_REQUIRED,
            correlation_id=uuid.uuid4(),
            payload_json="{}",
            payload_sha256="0" * 64,
            occurred_at=now,
        ),
    )

    return SeededReviewCase(
        tenant_id=tenant_id,
        workflow_id=workflow_record.memory_id,
        batch_id=batch_id,
        document_id=document_id,
        review_case_id=review_case_id,
        review_reasons=baseline.review_reasons,
    )
