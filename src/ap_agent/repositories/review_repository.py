"""Tenant-scoped PostgreSQL repository for the Phase 9 human-review interface.

Source: notebook cells 101-107 ("PHASE 9 — CELL 2" through "CELL 8"):
`retrieve_review_queue`, `retrieve_invoice_detail`, `retrieve_dashboard_record`,
`retrieve_review_command_contexts`/`load_locked_review_command_context`/
`load_locked_decision_context`, `find_stored_interface_command`/
`find_stored_resume_command`, and the transactional
`execute_assignment_command_in_transaction`/`execute_decision_command_in_transaction`/
`execute_resume_command_in_transaction` bodies. Every SQL statement below is
ported verbatim from those cells (same columns, same joins, same ordering,
same payload-integrity recomputation); what changes is generalization
required by the M10 task brief and CLAUDE.md:

- The notebook scopes every read to a fixed `document_ids` tuple (the
  notebook's own "current run" of exactly four fixture documents -- cell
  101's `phase_9_current_document_ids`, whose length the notebook asserts
  equals the fixture count).
  CLAUDE.md (D-2/D-3) and task §6 ("Do not hardcode three review cases or
  four invoices") forbid that assumption in production code: `get_dashboard`
  and `list_review_queue` below query every one of the tenant's workflows
  (optionally narrowed by an explicit `batch_id`, never by a fixed document
  count), with `list_review_queue` adding real `LIMIT`/`OFFSET` pagination
  and optional status/assignment/priority/batch filters the notebook's
  single-purpose demo query never needed.
- Policy validation (`validate_review_command`, idempotency-fingerprinting,
  restart-stage selection) is deliberately **not** here: task §7 assigns
  "the Phase 9 command policy and transactional executors" to
  `ap_agent.services`, and CLAUDE.md's dependency direction requires
  services to depend on repositories, never the reverse. This module
  exposes the same cursor-taking primitives the notebook itself factored
  the SQL into (`load_locked_*_context`, `find_stored_*_command`, the
  claim/release/decision/resume row mutations, audit-event insertion) plus
  a `transaction()` context manager that sets the tenant context once and
  yields a cursor; `ap_agent.services.review_commands`/`review_decisions`/
  `workflow_resume` compose those primitives with their own pure validation
  functions inside one transaction, exactly as the notebook's
  `execute_*_command_in_transaction` functions compose the same primitives
  inline.
- Every tenant-scoped method calls `ap_agent.db.connection.set_tenant_context`
  before issuing any other statement (task §5), so PostgreSQL's row-level
  security (migration 0002/0003) is the actual enforcement mechanism, not
  application-side filtering; a returned row whose `tenant_id` still
  disagrees with the caller's `tenant_id` is treated as a fail-closed
  integrity failure (`ReviewIntegrityError`), matching the notebook's own
  "Cross-tenant ... row detected" `RuntimeError`s.
- All user-controlled values are passed as `cursor.execute(sql, params)`
  parameters; no value is ever interpolated into a SQL string.
"""

from __future__ import annotations

from collections import Counter
from contextlib import contextmanager
from hashlib import sha256
from typing import TYPE_CHECKING, Any, Iterator, Optional
from uuid import UUID

from ap_agent.config.postgres import MemoryConfig
from ap_agent.db.connection import open_connection, set_tenant_context
from ap_agent.exceptions import ReviewCaseNotFoundError, ReviewIntegrityError
from ap_agent.models.interface import (
    DashboardRecord,
    InterfaceFieldValue,
    InterfaceFinancialCheck,
    InterfaceLineMatch,
    InterfaceTimelineEvent,
    InvoiceDetailRecord,
    ReviewCaseStatus,
    ReviewCommandContext,
    ReviewPriority,
    ReviewQueueRecord,
    interface_utc_now,
)
from ap_agent.models.memory import HumanReviewDecision, HumanReviewDisposition
from ap_agent.models.normalization import InvoiceFieldName, NormalizedValueType
from ap_agent.models.orchestration import InvoiceWorkflowStatus, OrchestrationStage
from ap_agent.serialization.memory_json import canonical_json_bytes, optional_text

if TYPE_CHECKING:  # pragma: no cover - typing only, no runtime import
    import psycopg

__all__ = [
    "ACTIVE_DATABASE_REVIEW_STATUSES",
    "ReviewRepository",
    "review_case_status_from_database",
    "command_case_status_from_database",
    "review_priority_from_database",
    "workflow_status_from_database",
    "human_review_disposition_from_database",
]


# ------------------------------------------------------------
# Database-to-interface mappings (notebook cells 101/102/106, verbatim)
# ------------------------------------------------------------

ACTIVE_DATABASE_REVIEW_STATUSES = ("OPEN", "CLAIMED")
# M11C: statuses a *detail* read may additionally see (never listed in the
# default active queue).
TERMINAL_DATABASE_REVIEW_STATUSES = ("RESOLVED", "CANCELLED")


def review_case_status_from_database(database_status: str) -> ReviewCaseStatus:
    status_mapping = {
        "OPEN": ReviewCaseStatus.OPEN,
        "CLAIMED": ReviewCaseStatus.IN_REVIEW,
    }

    if database_status not in status_mapping:
        raise ValueError(f"Unsupported active review status: {database_status}.")

    return status_mapping[database_status]


def command_case_status_from_database(
    database_status: str,
    resolution_code: Optional[str],
) -> ReviewCaseStatus:
    if database_status == "OPEN":
        return ReviewCaseStatus.OPEN

    if database_status == "CLAIMED":
        return ReviewCaseStatus.IN_REVIEW

    if database_status == "RESOLVED":
        if resolution_code == HumanReviewDisposition.REJECTED.value:
            return ReviewCaseStatus.REJECTED

        return ReviewCaseStatus.RESOLVED

    if database_status == "CANCELLED":
        return ReviewCaseStatus.REJECTED

    raise ValueError(f"Unsupported database review status: {database_status}.")


def _stage_from_database(database_phase: str) -> OrchestrationStage:
    """M11C: an active case's phase is always `HUMAN_REVIEW` (unchanged);
    after a workflow-resume handoff the workflow row carries the restart
    stage, which the detail/queue now report instead of a stale constant."""

    try:
        return OrchestrationStage(database_phase)
    except ValueError:
        return OrchestrationStage.HUMAN_REVIEW


def review_priority_from_database(database_priority: int) -> ReviewPriority:
    if database_priority == 1:
        return ReviewPriority.CRITICAL

    if database_priority == 2:
        return ReviewPriority.HIGH

    if database_priority == 3:
        return ReviewPriority.NORMAL

    if database_priority in (4, 5):
        return ReviewPriority.LOW

    raise ValueError(f"Review priority is outside the supported range: {database_priority}.")


def workflow_status_from_database(database_status: str) -> InvoiceWorkflowStatus:
    try:
        return InvoiceWorkflowStatus(database_status)
    except ValueError as exc:
        raise ValueError(f"Unsupported workflow status: {database_status}.") from exc


def human_review_disposition_from_database(decision_type: str) -> HumanReviewDisposition:
    disposition_mapping = {
        "ACCEPT": HumanReviewDisposition.APPROVED,
        "APPROVE": HumanReviewDisposition.APPROVED,
        "APPROVED": HumanReviewDisposition.APPROVED,
        "REJECT": HumanReviewDisposition.REJECTED,
        "REJECTED": HumanReviewDisposition.REJECTED,
        "HOLD": HumanReviewDisposition.HOLD,
        "REQUEST_INFORMATION": HumanReviewDisposition.NEEDS_INFORMATION,
        "NEEDS_INFORMATION": HumanReviewDisposition.NEEDS_INFORMATION,
        "CORRECT": HumanReviewDisposition.CORRECTED,
        "CORRECTED": HumanReviewDisposition.CORRECTED,
    }

    normalized_decision_type = decision_type.strip().upper()

    if normalized_decision_type not in disposition_mapping:
        raise ValueError(f"Unsupported human-review decision type: {decision_type}.")

    return disposition_mapping[normalized_decision_type]


def _unique_text_values(values: list[Any]) -> tuple[str, ...]:
    unique_values: list[str] = []
    observed: set[str] = set()

    for value in values:
        if value is None:
            continue

        text_value = str(value)

        if text_value in observed:
            continue

        observed.add(text_value)
        unique_values.append(text_value)

    return tuple(unique_values)


# ------------------------------------------------------------
# Projection helpers (notebook cell 102, verbatim)
# ------------------------------------------------------------


def _project_interface_fields(normalized_payload: dict[str, Any]) -> tuple[InterfaceFieldValue, ...]:
    invoice_record = normalized_payload.get("invoice_record")

    if not isinstance(invoice_record, dict):
        raise ReviewIntegrityError("Normalized invoice payload has no invoice record.")

    field_payloads = invoice_record.get("fields")

    if not isinstance(field_payloads, list):
        raise ReviewIntegrityError("Normalized invoice fields are malformed.")

    projected_fields = []

    for field_payload in field_payloads:
        if not isinstance(field_payload, dict):
            raise ReviewIntegrityError("A normalized invoice field is malformed.")

        evidence_payloads = field_payload.get("evidence_references", [])

        if not isinstance(evidence_payloads, list):
            raise ReviewIntegrityError("Field evidence references are malformed.")

        evidence_reference_ids = _unique_text_values(
            [
                evidence_payload.get("reference_id")
                for evidence_payload in evidence_payloads
                if isinstance(evidence_payload, dict)
            ]
        )

        confidence = field_payload.get("confidence")

        projected_fields.append(
            InterfaceFieldValue(
                field_name=InvoiceFieldName(field_payload["field_name"]),
                raw_value=optional_text(field_payload.get("raw_value")),
                normalized_value=optional_text(field_payload.get("normalized_value")),
                value_type=NormalizedValueType(field_payload["value_type"]),
                confidence=(None if confidence is None else float(confidence)),
                review_required=bool(field_payload.get("review_required", False)),
                evidence_reference_ids=evidence_reference_ids,
            )
        )

    return tuple(projected_fields)


def _project_financial_checks(financial_payload: dict[str, Any]) -> tuple[InterfaceFinancialCheck, ...]:
    check_payloads = financial_payload.get("checks")

    if not isinstance(check_payloads, list):
        raise ReviewIntegrityError("Financial-validation checks are malformed.")

    projected_checks = []

    for check_payload in check_payloads:
        if not isinstance(check_payload, dict):
            raise ReviewIntegrityError("A financial-validation check is malformed.")

        operand_payloads = check_payload.get("operands", [])

        if not isinstance(operand_payloads, list):
            raise ReviewIntegrityError("Financial-check operands are malformed.")

        evidence_reference_ids: list[str] = []

        for operand_payload in operand_payloads:
            if not isinstance(operand_payload, dict):
                raise ReviewIntegrityError("A financial-check operand is malformed.")

            operand_evidence_ids = operand_payload.get("evidence_reference_ids", [])

            if not isinstance(operand_evidence_ids, list):
                raise ReviewIntegrityError("Operand evidence identifiers are malformed.")

            evidence_reference_ids.extend(operand_evidence_ids)

        projected_checks.append(
            InterfaceFinancialCheck(
                check_id=str(check_payload["check_id"]),
                check_type=str(check_payload["check_type"]),
                status=str(check_payload["status"]),
                message=str(check_payload.get("message", "")),
                expected_value=optional_text(check_payload.get("expected_value")),
                observed_value=optional_text(check_payload.get("observed_value")),
                evidence_reference_ids=_unique_text_values(evidence_reference_ids),
            )
        )

    return tuple(projected_checks)


def _project_line_matches(matching_payload: dict[str, Any]) -> tuple[InterfaceLineMatch, ...]:
    line_match_payloads = matching_payload.get("line_matches")

    if not isinstance(line_match_payloads, list):
        raise ReviewIntegrityError("Reference-matching lines are malformed.")

    projected_line_matches = []

    for line_match_payload in line_match_payloads:
        if not isinstance(line_match_payload, dict):
            raise ReviewIntegrityError("A reference-matching line is malformed.")

        projected_line_matches.append(
            InterfaceLineMatch(
                line_match_id=str(line_match_payload["line_match_id"]),
                invoice_line_number=line_match_payload.get("invoice_line_number"),
                purchase_order_line_number=line_match_payload.get("purchase_order_line_number"),
                description_status=str(line_match_payload["description_status"]),
                quantity_status=str(line_match_payload["quantity_status"]),
                unit_price_status=str(line_match_payload["unit_price_status"]),
                line_total_status=str(line_match_payload["line_total_status"]),
                review_required=bool(line_match_payload.get("review_required", False)),
                review_reasons=tuple(line_match_payload.get("review_reasons", [])),
            )
        )

    return tuple(projected_line_matches)


def _project_review_decision(
    decision_row: tuple[Any, ...],
    tenant_id: UUID,
    batch_id: UUID,
    document_id: UUID,
) -> HumanReviewDecision:
    import json

    (
        decision_id,
        stored_tenant_id,
        decision_type,
        decided_by,
        decision_notes,
        decision_evidence,
        decided_at,
    ) = decision_row

    if stored_tenant_id != tenant_id:
        raise ReviewIntegrityError("Cross-tenant review decision detected.")

    evidence_payload = dict(decision_evidence or {})
    corrections_payload = evidence_payload.get("corrections", [])

    corrections_json = json.dumps(
        corrections_payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )

    return HumanReviewDecision(
        decision_id=decision_id,
        tenant_id=str(tenant_id),
        batch_id=batch_id,
        document_id=document_id,
        reviewer_id=decided_by,
        disposition=human_review_disposition_from_database(decision_type),
        reason_codes=tuple(evidence_payload.get("reason_codes", [])),
        notes=decision_notes,
        corrections_json=corrections_json,
        corrections_sha256=sha256(corrections_json.encode("utf-8")).hexdigest(),
        decided_at=decided_at,
    )


def _parse_command_context_payloads(
    normalized_payload: Any,
    financial_payload: Any,
) -> tuple[tuple[tuple[InvoiceFieldName, Optional[str]], ...], tuple[int, ...], set[str]]:
    """Shared parsing behind `lock_assignment_context`/`lock_decision_context`
    (notebook cells 105/106's identical inline blocks)."""

    if not isinstance(normalized_payload, dict):
        raise ReviewIntegrityError("Stored normalization payload is malformed.")

    if not isinstance(financial_payload, dict):
        raise ReviewIntegrityError("Stored financial payload is malformed.")

    invoice_record_payload = normalized_payload.get("invoice_record")

    if not isinstance(invoice_record_payload, dict):
        raise ReviewIntegrityError("Stored invoice record is malformed.")

    field_payloads = invoice_record_payload.get("fields")
    line_item_payloads = invoice_record_payload.get("line_items")
    check_payloads = financial_payload.get("checks")

    if not isinstance(field_payloads, list):
        raise ReviewIntegrityError("Stored invoice fields are malformed.")

    if not isinstance(line_item_payloads, list):
        raise ReviewIntegrityError("Stored invoice lines are malformed.")

    if not isinstance(check_payloads, list):
        raise ReviewIntegrityError("Stored financial checks are malformed.")

    header_field_values = []
    evidence_reference_ids: set[str] = set()

    for field_payload in field_payloads:
        if not isinstance(field_payload, dict):
            raise ReviewIntegrityError("Stored invoice field is malformed.")

        header_field_values.append(
            (
                InvoiceFieldName(field_payload["field_name"]),
                optional_text(field_payload.get("normalized_value")),
            )
        )

        for evidence_payload in field_payload.get("evidence_references", []):
            if not isinstance(evidence_payload, dict):
                continue

            reference_id = evidence_payload.get("reference_id")

            if reference_id is not None:
                evidence_reference_ids.add(str(reference_id))

    for check_payload in check_payloads:
        if not isinstance(check_payload, dict):
            raise ReviewIntegrityError("Stored financial check is malformed.")

        for operand_payload in check_payload.get("operands", []):
            if not isinstance(operand_payload, dict):
                continue

            for reference_id in operand_payload.get("evidence_reference_ids", []):
                evidence_reference_ids.add(str(reference_id))

    known_line_numbers = tuple(
        sorted(
            {
                int(line_item_payload["line_number"])
                for line_item_payload in line_item_payloads
                if isinstance(line_item_payload, dict) and line_item_payload.get("line_number") is not None
            }
        )
    )

    return tuple(header_field_values), known_line_numbers, evidence_reference_ids


class ReviewRepository:
    """PostgreSQL-backed data access for the Phase 9 human-review interface."""

    def __init__(self, dsn: str, config: MemoryConfig) -> None:
        self._dsn = dsn
        self._config = config

    def _connection(self):
        return open_connection(self._dsn, self._config)

    @contextmanager
    def transaction(self, tenant_id: UUID) -> Iterator["psycopg.Cursor"]:
        """Open one connection, set the tenant context, and yield a cursor
        for a caller-composed multi-statement transaction. Commits on clean
        exit (the underlying `psycopg.Connection` context manager's default
        behaviour), rolls back on any exception."""

        with self._connection() as connection:
            with connection.cursor() as cursor:
                set_tenant_context(cursor, str(tenant_id))
                yield cursor

    # ------------------------------------------------------------
    # Dashboard (notebook cell 103)
    # ------------------------------------------------------------

    def get_dashboard(
        self,
        tenant_id: UUID,
        *,
        batch_id: Optional[UUID] = None,
    ) -> tuple[DashboardRecord, tuple[dict[str, Any], ...]]:
        with self._connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute("SET TRANSACTION READ ONLY;")
                set_tenant_context(cursor, str(tenant_id))

                cursor.execute(
                    """
                    SELECT
                        workflow.workflow_id,
                        workflow.tenant_id,
                        workflow.document_id,
                        workflow.source_name,
                        workflow.current_status,
                        workflow.review_required,
                        invoice.review_reasons,
                        invoice.payload_sha256,
                        invoice.normalized_invoice,
                        invoice.financial_validation,
                        invoice.matching_result,
                        invoice.matched_reference_data,

                        COUNT(review.review_id) FILTER (
                            WHERE review.review_status = ANY(%s::text[])
                        ) AS active_review_cases,

                        COUNT(review.review_id) FILTER (
                            WHERE review.review_status = ANY(%s::text[])
                            AND review.assigned_to IS NULL
                        ) AS unassigned_review_cases

                    FROM ap_agent.workflow_instances AS workflow

                    JOIN ap_agent.workflow_effective_memory AS invoice
                        ON invoice.tenant_id = workflow.tenant_id
                           AND invoice.workflow_id = workflow.workflow_id

                    LEFT JOIN ap_agent.review_cases AS review
                        ON review.tenant_id = workflow.tenant_id
                       AND review.workflow_id = workflow.workflow_id

                    WHERE
                        workflow.tenant_id = %s
                        AND (%s::uuid IS NULL OR workflow.batch_id = %s::uuid)

                    GROUP BY
                        workflow.workflow_id, workflow.tenant_id, workflow.document_id,
                        workflow.source_name, workflow.current_status, workflow.review_required,
                        invoice.review_reasons, invoice.payload_sha256, invoice.normalized_invoice,
                        invoice.financial_validation, invoice.matching_result,
                        invoice.matched_reference_data

                    ORDER BY workflow.source_name ASC, workflow.document_id ASC;
                    """,
                    (
                        list(ACTIVE_DATABASE_REVIEW_STATUSES),
                        list(ACTIVE_DATABASE_REVIEW_STATUSES),
                        tenant_id,
                        batch_id,
                        batch_id,
                    ),
                )

                database_rows = cursor.fetchall()

        reason_counter: Counter[str] = Counter()
        dashboard_source_rows = []

        processing_invoices = 0
        completed_invoices = 0
        review_required_invoices = 0
        failed_invoices = 0
        open_review_cases = 0
        unassigned_review_cases = 0

        for row in database_rows:
            (
                workflow_id,
                stored_tenant_id,
                document_id,
                source_name,
                stored_status,
                stored_review_required,
                stored_review_reasons,
                stored_payload_sha256,
                normalized_payload,
                financial_payload,
                matching_payload,
                reference_payload,
                active_case_count,
                unassigned_case_count,
            ) = row

            if stored_tenant_id != tenant_id:
                raise ReviewIntegrityError("Cross-tenant dashboard row detected.")

            reconstructed_payload = {
                "normalization_result": normalized_payload,
                "financial_validation_result": financial_payload,
                "matching_result": matching_payload,
                "matched_reference_data": reference_payload,
            }

            recalculated_payload_sha256 = sha256(canonical_json_bytes(reconstructed_payload)).hexdigest()

            if recalculated_payload_sha256 != stored_payload_sha256.strip():
                raise ReviewIntegrityError(f"Dashboard payload-integrity failure for {source_name}.")

            try:
                workflow_status = InvoiceWorkflowStatus(stored_status)
            except ValueError as exc:
                raise ReviewIntegrityError(f"Unsupported dashboard workflow status: {stored_status}.") from exc

            active_case_count = int(active_case_count)
            unassigned_case_count = int(unassigned_case_count)

            if unassigned_case_count > active_case_count:
                raise ReviewIntegrityError("Unassigned review-case count exceeds the active review-case count.")

            if workflow_status in {InvoiceWorkflowStatus.PENDING, InvoiceWorkflowStatus.IN_PROGRESS}:
                processing_invoices += 1
            elif workflow_status == InvoiceWorkflowStatus.SUCCEEDED:
                completed_invoices += 1
            elif workflow_status == InvoiceWorkflowStatus.REVIEW_REQUIRED:
                review_required_invoices += 1
            elif workflow_status == InvoiceWorkflowStatus.FAILED:
                failed_invoices += 1

            if bool(stored_review_required) != (workflow_status == InvoiceWorkflowStatus.REVIEW_REQUIRED):
                raise ReviewIntegrityError(f"Workflow status and review flag differ for {source_name}.")

            if stored_review_required:
                if active_case_count != 1:
                    raise ReviewIntegrityError(
                        f"A review-required invoice must have exactly one active review case: {source_name}."
                    )
                reason_counter.update(stored_review_reasons)
            elif active_case_count != 0:
                raise ReviewIntegrityError(
                    f"A completed invoice unexpectedly has an active review case: {source_name}."
                )

            open_review_cases += active_case_count
            unassigned_review_cases += unassigned_case_count

            dashboard_source_rows.append(
                {
                    "workflow_id": workflow_id,
                    "document_id": document_id,
                    "source_file": source_name,
                    "workflow_status": workflow_status.value,
                    "review_required": bool(stored_review_required),
                    "active_review_cases": active_case_count,
                    "unassigned_review_cases": unassigned_case_count,
                }
            )

        review_reason_counts = tuple(
            sorted(reason_counter.items(), key=lambda item: (-item[1], item[0]))
        )

        dashboard_record = DashboardRecord(
            tenant_id=tenant_id,
            generated_at=interface_utc_now(),
            total_invoices=len(database_rows),
            processing_invoices=processing_invoices,
            completed_invoices=completed_invoices,
            review_required_invoices=review_required_invoices,
            failed_invoices=failed_invoices,
            open_review_cases=open_review_cases,
            unassigned_review_cases=unassigned_review_cases,
            review_reason_counts=review_reason_counts,
        )

        return dashboard_record, tuple(dashboard_source_rows)

    # ------------------------------------------------------------
    # Review queue (notebook cell 101, generalized -- see module docstring)
    # ------------------------------------------------------------

    def list_review_queue(
        self,
        tenant_id: UUID,
        *,
        status_filter: Optional[tuple[str, ...]] = None,
        assigned_filter: Optional[str] = None,
        priority_filter: Optional[int] = None,
        batch_id_filter: Optional[UUID] = None,
        limit: int = 25,
        offset: int = 0,
    ) -> tuple[tuple[ReviewQueueRecord, ...], int]:
        statuses = list(status_filter) if status_filter else list(ACTIVE_DATABASE_REVIEW_STATUSES)

        where_clause = """
            WHERE
                review.tenant_id = %s
                AND review.review_status = ANY(%s::text[])
                AND workflow.review_required = TRUE
                AND invoice.review_required = TRUE
                AND (%s::text IS NULL OR review.assigned_to = %s::text)
                AND (%s::smallint IS NULL OR review.priority = %s::smallint)
                AND (%s::uuid IS NULL OR workflow.batch_id = %s::uuid)
        """
        where_params = (
            tenant_id,
            statuses,
            assigned_filter,
            assigned_filter,
            priority_filter,
            priority_filter,
            batch_id_filter,
            batch_id_filter,
        )

        with self._connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute("SET TRANSACTION READ ONLY;")
                set_tenant_context(cursor, str(tenant_id))

                cursor.execute(
                    f"""
                    SELECT COUNT(*) FROM (
                        SELECT review.review_id
                        FROM ap_agent.review_cases AS review
                        JOIN ap_agent.workflow_instances AS workflow
                            ON workflow.tenant_id = review.tenant_id
                           AND workflow.workflow_id = review.workflow_id
                        JOIN ap_agent.review_case_effective_memory AS invoice
                            ON invoice.tenant_id = review.tenant_id
                               AND invoice.review_id = review.review_id
                        {where_clause}
                    ) AS counted;
                    """,
                    where_params,
                )
                total_count = int(cursor.fetchone()[0])

                cursor.execute(
                    f"""
                    SELECT
                        review.review_id, review.tenant_id, review.workflow_id,
                        workflow.batch_id, workflow.document_id, workflow.source_name,
                        workflow.current_phase, workflow.current_status, workflow.lock_version,
                        workflow.updated_at,
                        review.review_status, review.resolution_code, review.priority,
                        review.reason_codes, review.assigned_to, review.opened_at,
                        invoice.invoice_number, invoice.supplier_name, invoice.currency,
                        invoice.total_amount,
                        invoice.supplier_resolution_status, invoice.purchase_order_status,
                        invoice.financial_validation_status,
                        invoice.review_reasons, invoice.payload_sha256,
                        invoice.normalized_invoice, invoice.financial_validation,
                        invoice.matching_result, invoice.matched_reference_data,
                        COUNT(decision.decision_id) AS decision_count
                    FROM ap_agent.review_cases AS review
                    JOIN ap_agent.workflow_instances AS workflow
                        ON workflow.tenant_id = review.tenant_id
                       AND workflow.workflow_id = review.workflow_id
                    JOIN ap_agent.review_case_effective_memory AS invoice
                        ON invoice.tenant_id = review.tenant_id
                           AND invoice.review_id = review.review_id
                    LEFT JOIN ap_agent.review_decisions AS decision
                        ON decision.tenant_id = review.tenant_id
                       AND decision.review_id = review.review_id
                    {where_clause}
                    GROUP BY
                        review.review_id, review.tenant_id, review.workflow_id,
                        workflow.batch_id, workflow.document_id, workflow.source_name,
                        workflow.current_phase, workflow.current_status, workflow.lock_version,
                        workflow.updated_at, review.review_status, review.resolution_code,
                        review.priority, review.reason_codes, review.assigned_to, review.opened_at,
                        invoice.invoice_number, invoice.supplier_name, invoice.currency,
                        invoice.total_amount, invoice.supplier_resolution_status,
                        invoice.purchase_order_status, invoice.financial_validation_status,
                        invoice.review_reasons, invoice.payload_sha256,
                        invoice.normalized_invoice, invoice.financial_validation,
                        invoice.matching_result, invoice.matched_reference_data
                    ORDER BY review.priority ASC, review.opened_at ASC, review.review_id ASC
                    LIMIT %s OFFSET %s;
                    """,
                    where_params + (limit, offset),
                )

                database_rows = cursor.fetchall()

        queue_records = [self._row_to_queue_record(tenant_id, row) for row in database_rows]

        return tuple(queue_records), total_count

    def get_review_queue_record(self, tenant_id: UUID, review_case_id: UUID) -> Optional[ReviewQueueRecord]:
        record, _ = self._list_review_queue_by_id(tenant_id, review_case_id)
        return record

    def _list_review_queue_by_id(
        self, tenant_id: UUID, review_case_id: UUID
    ) -> tuple[Optional[ReviewQueueRecord], int]:
        with self._connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute("SET TRANSACTION READ ONLY;")
                set_tenant_context(cursor, str(tenant_id))

                cursor.execute(
                    """
                    SELECT
                        review.review_id, review.tenant_id, review.workflow_id,
                        workflow.batch_id, workflow.document_id, workflow.source_name,
                        workflow.current_phase, workflow.current_status, workflow.lock_version,
                        workflow.updated_at,
                        review.review_status, review.resolution_code, review.priority,
                        review.reason_codes, review.assigned_to, review.opened_at,
                        invoice.invoice_number, invoice.supplier_name, invoice.currency,
                        invoice.total_amount,
                        invoice.supplier_resolution_status, invoice.purchase_order_status,
                        invoice.financial_validation_status,
                        invoice.review_reasons, invoice.payload_sha256,
                        invoice.normalized_invoice, invoice.financial_validation,
                        invoice.matching_result, invoice.matched_reference_data,
                        COUNT(decision.decision_id) AS decision_count
                    FROM ap_agent.review_cases AS review
                    JOIN ap_agent.workflow_instances AS workflow
                        ON workflow.tenant_id = review.tenant_id
                       AND workflow.workflow_id = review.workflow_id
                    JOIN ap_agent.review_case_effective_memory AS invoice
                        ON invoice.tenant_id = review.tenant_id
                           AND invoice.review_id = review.review_id
                    LEFT JOIN ap_agent.review_decisions AS decision
                        ON decision.tenant_id = review.tenant_id
                       AND decision.review_id = review.review_id
                    WHERE review.tenant_id = %s AND review.review_id = %s
                    GROUP BY
                        review.review_id, review.tenant_id, review.workflow_id,
                        workflow.batch_id, workflow.document_id, workflow.source_name,
                        workflow.current_phase, workflow.current_status, workflow.lock_version,
                        workflow.updated_at, review.review_status, review.resolution_code,
                        review.priority, review.reason_codes, review.assigned_to, review.opened_at,
                        invoice.invoice_number, invoice.supplier_name, invoice.currency,
                        invoice.total_amount, invoice.supplier_resolution_status,
                        invoice.purchase_order_status, invoice.financial_validation_status,
                        invoice.review_reasons, invoice.payload_sha256,
                        invoice.normalized_invoice, invoice.financial_validation,
                        invoice.matching_result, invoice.matched_reference_data;
                    """,
                    (tenant_id, review_case_id),
                )

                row = cursor.fetchone()

        if row is None:
            return None, 0

        return self._row_to_queue_record(tenant_id, row), 1

    def _row_to_queue_record(self, tenant_id: UUID, row: tuple[Any, ...]) -> ReviewQueueRecord:
        (
            review_case_id,
            stored_tenant_id,
            workflow_id,
            batch_id,
            document_id,
            source_name,
            stored_current_phase,
            stored_workflow_status,
            workflow_revision,
            workflow_updated_at,
            stored_review_status,
            resolution_code,
            stored_priority,
            review_reason_codes,
            assigned_reviewer_id,
            opened_at,
            invoice_number,
            supplier_name,
            currency,
            total_amount,
            supplier_status,
            purchase_order_status,
            financial_status,
            invoice_review_reasons,
            stored_payload_sha256,
            normalized_payload,
            financial_payload,
            matching_payload,
            reference_payload,
            decision_count,
        ) = row

        if stored_tenant_id != tenant_id:
            raise ReviewIntegrityError("Cross-tenant review record detected.")

        reconstructed_payload = {
            "normalization_result": normalized_payload,
            "financial_validation_result": financial_payload,
            "matching_result": matching_payload,
            "matched_reference_data": reference_payload,
        }

        recalculated_payload_sha256 = sha256(canonical_json_bytes(reconstructed_payload)).hexdigest()

        if recalculated_payload_sha256 != stored_payload_sha256.strip():
            raise ReviewIntegrityError(f"Review-queue payload-integrity failure for {source_name}.")

        if tuple(review_reason_codes) != tuple(invoice_review_reasons):
            raise ReviewIntegrityError(
                f"Review reasons differ between the review case and invoice memory for {source_name}."
            )

        return ReviewQueueRecord(
            tenant_id=stored_tenant_id,
            review_case_id=review_case_id,
            workflow_id=workflow_id,
            batch_id=batch_id,
            document_id=document_id,
            source_name=source_name,
            invoice_number=invoice_number,
            supplier_name=supplier_name,
            currency=currency,
            total_amount=total_amount,
            workflow_status=workflow_status_from_database(stored_workflow_status),
            current_stage=_stage_from_database(stored_current_phase),
            case_status=command_case_status_from_database(stored_review_status, resolution_code),
            priority=review_priority_from_database(int(stored_priority)),
            review_reasons=tuple(review_reason_codes),
            supplier_status=supplier_status,
            purchase_order_status=purchase_order_status,
            financial_validation_status=financial_status,
            assigned_reviewer_id=assigned_reviewer_id,
            revision=1 + int(decision_count),
            created_at=opened_at,
            updated_at=workflow_updated_at,
        )

    # ------------------------------------------------------------
    # Invoice detail (notebook cell 102)
    # ------------------------------------------------------------

    def get_invoice_detail(self, tenant_id: UUID, queue_record: ReviewQueueRecord) -> InvoiceDetailRecord:
        if queue_record.tenant_id != tenant_id:
            raise ReviewIntegrityError("Queue record belongs to another tenant.")

        with self._connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute("SET TRANSACTION READ ONLY;")
                set_tenant_context(cursor, str(tenant_id))

                cursor.execute(
                    """
                    SELECT
                        review.review_id, review.tenant_id, review.workflow_id,
                        workflow.batch_id, workflow.document_id, workflow.source_name,
                        workflow.source_document_sha256, workflow.current_status,
                        workflow.review_required, review.review_status, review.reason_codes,
                        invoice.payload_sha256, invoice.normalized_invoice,
                        invoice.financial_validation, invoice.matching_result,
                        invoice.matched_reference_data
                    FROM ap_agent.review_cases AS review
                    JOIN ap_agent.workflow_instances AS workflow
                        ON workflow.tenant_id = review.tenant_id
                       AND workflow.workflow_id = review.workflow_id
                    JOIN ap_agent.review_case_effective_memory AS invoice
                        ON invoice.tenant_id = review.tenant_id
                           AND invoice.review_id = review.review_id
                    WHERE
                        review.tenant_id = %s AND review.review_id = %s
                        AND workflow.workflow_id = %s AND workflow.document_id = %s;
                    """,
                    (tenant_id, queue_record.review_case_id, queue_record.workflow_id, queue_record.document_id),
                )

                detail_rows = cursor.fetchall()

                if len(detail_rows) != 1:
                    raise ReviewCaseNotFoundError(
                        f"Invoice-detail retrieval expected exactly one row for "
                        f"{queue_record.source_name}; found {len(detail_rows)}."
                    )

                cursor.execute(
                    """
                    SELECT event_id, event_type, phase_name, event_status, actor_id, message, occurred_at
                    FROM ap_agent.audit_events
                    WHERE tenant_id = %s AND workflow_id = %s
                    ORDER BY sequence_number ASC, occurred_at ASC, event_id ASC;
                    """,
                    (tenant_id, queue_record.workflow_id),
                )
                audit_rows = cursor.fetchall()

                cursor.execute(
                    """
                    SELECT decision_id, tenant_id, decision_type, decided_by, decision_notes,
                           evidence, decided_at
                    FROM ap_agent.review_decisions
                    WHERE tenant_id = %s AND workflow_id = %s AND review_id = %s
                    ORDER BY decided_at ASC, decision_id ASC;
                    """,
                    (tenant_id, queue_record.workflow_id, queue_record.review_case_id),
                )
                decision_rows = cursor.fetchall()

        (
            stored_review_case_id,
            stored_tenant_id,
            stored_workflow_id,
            stored_batch_id,
            stored_document_id,
            stored_source_name,
            stored_source_sha256,
            stored_workflow_status,
            stored_review_required,
            stored_review_status,
            stored_review_reasons,
            stored_payload_sha256,
            normalized_payload,
            financial_payload,
            matching_payload,
            reference_payload,
        ) = detail_rows[0]

        identity_values = (
            stored_tenant_id == tenant_id,
            stored_review_case_id == queue_record.review_case_id,
            stored_workflow_id == queue_record.workflow_id,
            stored_batch_id == queue_record.batch_id,
            stored_document_id == queue_record.document_id,
            stored_source_name == queue_record.source_name,
        )

        if not all(identity_values):
            raise ReviewIntegrityError(
                f"Invoice-detail identity continuity failed for {queue_record.source_name}."
            )

        reconstructed_payload = {
            "normalization_result": normalized_payload,
            "financial_validation_result": financial_payload,
            "matching_result": matching_payload,
            "matched_reference_data": reference_payload,
        }

        recalculated_payload_sha256 = sha256(canonical_json_bytes(reconstructed_payload)).hexdigest()

        if recalculated_payload_sha256 != stored_payload_sha256.strip():
            raise ReviewIntegrityError(
                f"Invoice-detail payload-integrity failure for {queue_record.source_name}."
            )

        for phase_name, phase_payload in (
            ("normalization", normalized_payload),
            ("financial validation", financial_payload),
            ("reference matching", matching_payload),
        ):
            if not isinstance(phase_payload, dict):
                raise ReviewIntegrityError(f"Stored {phase_name} payload is malformed.")

            if UUID(str(phase_payload["document_id"])) != stored_document_id:
                raise ReviewIntegrityError(f"Stored {phase_name} payload belongs to another document.")

        if tuple(stored_review_reasons) != queue_record.review_reasons:
            raise ReviewIntegrityError("Invoice-detail review reasons differ from the review queue.")

        if stored_workflow_status != queue_record.workflow_status.value:
            raise ReviewIntegrityError("Invoice-detail workflow status differs from the review queue.")

        # M11C deviation D-M11C-1 (documented in docs/m11c_review_actions_report.md):
        # the notebook's/M10's detail read only ever supported *active*
        # (OPEN/CLAIMED) cases. M11C's terminal decisions and workflow-resume
        # handoff make RESOLVED cases (and, after a resume request, a workflow
        # no longer flagged `review_required`) legitimate detail targets --
        # the decision history and timeline are exactly what a reviewer must
        # see after acting. Every active-case invariant below is unchanged.
        case_is_active = stored_review_status in ACTIVE_DATABASE_REVIEW_STATUSES

        if case_is_active and not stored_review_required:
            raise ReviewIntegrityError("An active review detail is not marked for review.")

        if not case_is_active and stored_review_status not in TERMINAL_DATABASE_REVIEW_STATUSES:
            raise ReviewIntegrityError("Invoice-detail review case has an unsupported status.")

        timeline = tuple(
            InterfaceTimelineEvent(
                event_id=str(event_id),
                event_type=event_type,
                stage=phase_name,
                status=event_status,
                actor_id=actor_id,
                message=message,
                occurred_at=occurred_at,
            )
            for (event_id, event_type, phase_name, event_status, actor_id, message, occurred_at) in audit_rows
        )

        review_decisions = tuple(
            _project_review_decision(
                decision_row, tenant_id=tenant_id, batch_id=stored_batch_id, document_id=stored_document_id
            )
            for decision_row in decision_rows
        )

        # No local filesystem path is ever exposed (task §6/§21); a secure
        # streaming/signed-URL mechanism does not exist yet.
        source_artifact_uri = None

        return InvoiceDetailRecord(
            tenant_id=stored_tenant_id,
            review_case_id=stored_review_case_id,
            workflow_id=stored_workflow_id,
            batch_id=stored_batch_id,
            document_id=stored_document_id,
            source_name=stored_source_name,
            source_document_sha256=stored_source_sha256,
            source_artifact_uri=source_artifact_uri,
            workflow_status=queue_record.workflow_status,
            current_stage=queue_record.current_stage,
            case_status=queue_record.case_status,
            revision=queue_record.revision,
            review_required=bool(stored_review_required),
            review_reasons=tuple(stored_review_reasons),
            fields=_project_interface_fields(normalized_payload),
            financial_checks=_project_financial_checks(financial_payload),
            line_matches=_project_line_matches(matching_payload),
            timeline=timeline,
            review_decisions=review_decisions,
        )

    # ------------------------------------------------------------
    # Command contexts (notebook cells 104-107)
    # ------------------------------------------------------------

    def get_command_context(self, tenant_id: UUID, review_case_id: UUID) -> Optional[ReviewCommandContext]:
        """Unlocked, read-only context (for building/echoing a command
        before a client submits it). The transactional executors use
        `lock_assignment_context`/`lock_decision_context` instead, which
        take `FOR UPDATE` locks inside a caller-managed transaction."""

        with self._connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute("SET TRANSACTION READ ONLY;")
                set_tenant_context(cursor, str(tenant_id))

                row = self._select_command_context_row(cursor, tenant_id=tenant_id, review_case_id=review_case_id)

        if row is None:
            return None

        return self._row_to_command_context(row, resolution_aware=True)

    def get_normalized_invoice_payload(self, tenant_id: UUID, review_case_id: UUID) -> Optional[dict[str, Any]]:
        """Read-only copy of the case's *original* normalized invoice memory
        (M11C task §6: lets the capability projection show current line
        values). Never writes; `invoice_memory_records` is append-only by
        trigger, so this is also the immutable value corrections are
        checked against."""

        with self._connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute("SET TRANSACTION READ ONLY;")
                set_tenant_context(cursor, str(tenant_id))
                cursor.execute(
                    """
                    SELECT invoice.normalized_invoice
                    FROM ap_agent.review_cases AS review
                    JOIN ap_agent.review_case_effective_memory AS invoice
                        ON invoice.tenant_id = review.tenant_id AND invoice.review_id = review.review_id
                    WHERE review.tenant_id = %s AND review.review_id = %s;
                    """,
                    (tenant_id, review_case_id),
                )
                row = cursor.fetchone()

        if row is None or not isinstance(row[0], dict):
            return None

        return row[0]

    def lock_assignment_context(
        self, cursor: "psycopg.Cursor", *, tenant_id: UUID, workflow_id: UUID, batch_id: UUID, document_id: UUID, review_case_id: UUID
    ) -> Optional[ReviewCommandContext]:
        """`load_locked_review_command_context` (notebook cell 105): locks
        the review case and workflow row `FOR UPDATE`, treating any
        non-terminal status the same way `review_case_status_from_database`
        does (claim/release only ever see OPEN/CLAIMED)."""

        row = self._select_locked_context_row(
            cursor, tenant_id=tenant_id, workflow_id=workflow_id, batch_id=batch_id,
            document_id=document_id, review_case_id=review_case_id, with_resolution=False,
        )

        if row is None:
            return None

        return self._row_to_command_context(row, resolution_aware=False)

    def lock_decision_context(
        self, cursor: "psycopg.Cursor", *, tenant_id: UUID, workflow_id: UUID, batch_id: UUID, document_id: UUID, review_case_id: UUID
    ) -> Optional[ReviewCommandContext]:
        """`load_locked_decision_context` (notebook cell 106): locks the
        same rows but is resolution-aware (RESOLVED/CANCELLED map to
        REJECTED/RESOLVED via `command_case_status_from_database`), so
        decision and resume commands can see a terminal case."""

        row = self._select_locked_context_row(
            cursor, tenant_id=tenant_id, workflow_id=workflow_id, batch_id=batch_id,
            document_id=document_id, review_case_id=review_case_id, with_resolution=True,
        )

        if row is None:
            return None

        return self._row_to_command_context(row, resolution_aware=True)

    def _select_command_context_row(
        self, cursor: "psycopg.Cursor", *, tenant_id: UUID, review_case_id: UUID
    ) -> Optional[tuple[Any, ...]]:
        cursor.execute(
            """
            SELECT
                review.tenant_id, review.workflow_id, workflow.batch_id, workflow.document_id,
                review.review_id, review.review_status, review.resolution_code, review.assigned_to,
                workflow.lock_version, invoice.normalized_invoice, invoice.financial_validation,
                (SELECT COUNT(*) FROM ap_agent.review_decisions AS decision
                 WHERE decision.tenant_id = review.tenant_id AND decision.review_id = review.review_id) AS decision_count
            FROM ap_agent.review_cases AS review
            JOIN ap_agent.workflow_instances AS workflow
                ON workflow.tenant_id = review.tenant_id AND workflow.workflow_id = review.workflow_id
            JOIN ap_agent.review_case_effective_memory AS invoice
                ON invoice.tenant_id = review.tenant_id AND invoice.review_id = review.review_id
            WHERE review.tenant_id = %s AND review.review_id = %s;
            """,
            (tenant_id, review_case_id),
        )
        return cursor.fetchone()

    def _select_locked_context_row(
        self,
        cursor: "psycopg.Cursor",
        *,
        tenant_id: UUID,
        workflow_id: UUID,
        batch_id: UUID,
        document_id: UUID,
        review_case_id: UUID,
        with_resolution: bool,
    ) -> Optional[tuple[Any, ...]]:
        resolution_select = "review.resolution_code," if with_resolution else "NULL AS resolution_code,"

        cursor.execute(
            f"""
            SELECT
                review.tenant_id, review.workflow_id, workflow.batch_id, workflow.document_id,
                review.review_id, review.review_status, {resolution_select} review.assigned_to,
                workflow.lock_version, invoice.normalized_invoice, invoice.financial_validation,
                (SELECT COUNT(*) FROM ap_agent.review_decisions AS decision
                 WHERE decision.tenant_id = review.tenant_id AND decision.review_id = review.review_id) AS decision_count
            FROM ap_agent.review_cases AS review
            JOIN ap_agent.workflow_instances AS workflow
                ON workflow.tenant_id = review.tenant_id AND workflow.workflow_id = review.workflow_id
            JOIN ap_agent.review_case_effective_memory AS invoice
                ON invoice.tenant_id = review.tenant_id AND invoice.review_id = review.review_id
            WHERE
                review.tenant_id = %s AND review.review_id = %s AND workflow.workflow_id = %s
                AND workflow.batch_id = %s AND workflow.document_id = %s
            FOR UPDATE OF review, workflow;
            """,
            (tenant_id, review_case_id, workflow_id, batch_id, document_id),
        )

        rows = cursor.fetchall()

        if not rows:
            return None

        if len(rows) != 1:
            raise ReviewIntegrityError("Review command resolved to multiple targets.")

        return rows[0]

    def _row_to_command_context(self, row: tuple[Any, ...], *, resolution_aware: bool) -> ReviewCommandContext:
        (
            tenant_id,
            workflow_id,
            batch_id,
            document_id,
            review_case_id,
            stored_review_status,
            resolution_code,
            assigned_reviewer_id,
            workflow_revision,
            normalized_payload,
            financial_payload,
            decision_count,
        ) = row

        header_field_values, known_line_numbers, evidence_reference_ids = _parse_command_context_payloads(
            normalized_payload, financial_payload
        )

        case_status = (
            command_case_status_from_database(stored_review_status, resolution_code)
            if resolution_aware
            else review_case_status_from_database(stored_review_status)
        )

        return ReviewCommandContext(
            tenant_id=tenant_id,
            workflow_id=workflow_id,
            batch_id=batch_id,
            document_id=document_id,
            review_case_id=review_case_id,
            case_status=case_status,
            assigned_reviewer_id=assigned_reviewer_id,
            review_revision=1 + int(decision_count),
            workflow_revision=int(workflow_revision),
            header_field_values=header_field_values,
            known_invoice_line_numbers=known_line_numbers,
            available_evidence_reference_ids=tuple(sorted(evidence_reference_ids)),
        )

    # ------------------------------------------------------------
    # Idempotency lookups (notebook cells 105/107)
    # ------------------------------------------------------------

    def find_stored_command_payload(
        self, cursor: "psycopg.Cursor", *, tenant_id: UUID, workflow_id: UUID, idempotency_key: str, event_type: str
    ) -> Optional[dict[str, Any]]:
        cursor.execute(
            """
            SELECT payload FROM ap_agent.audit_events
            WHERE tenant_id = %s AND workflow_id = %s AND event_type = %s
              AND payload ->> 'idempotency_key' = %s
            ORDER BY sequence_number DESC LIMIT 1;
            """,
            (tenant_id, workflow_id, event_type, idempotency_key),
        )

        row = cursor.fetchone()

        if row is None:
            return None

        if not isinstance(row[0], dict):
            raise ReviewIntegrityError("Stored command audit payload is malformed.")

        return row[0]

    # ------------------------------------------------------------
    # Transactional mutations (notebook cells 105-107)
    # ------------------------------------------------------------

    def apply_claim_or_release(
        self,
        cursor: "psycopg.Cursor",
        *,
        tenant_id: UUID,
        workflow_id: UUID,
        review_case_id: UUID,
        claim: bool,
        actor_id: str,
        observed_workflow_revision: int,
    ) -> Optional[int]:
        database_review_status = "CLAIMED" if claim else "OPEN"
        resulting_assignee = actor_id if claim else None

        cursor.execute(
            """
            UPDATE ap_agent.review_cases SET review_status = %s, assigned_to = %s
            WHERE tenant_id = %s AND review_id = %s AND workflow_id = %s;
            """,
            (database_review_status, resulting_assignee, tenant_id, review_case_id, workflow_id),
        )

        if cursor.rowcount != 1:
            raise ReviewIntegrityError("Review assignment update affected an unexpected number of rows.")

        return self.advance_workflow_phase(
            cursor, tenant_id=tenant_id, workflow_id=workflow_id, new_phase="HUMAN_REVIEW",
            observed_workflow_revision=observed_workflow_revision,
        )

    def advance_workflow_phase(
        self,
        cursor: "psycopg.Cursor",
        *,
        tenant_id: UUID,
        workflow_id: UUID,
        new_phase: str,
        observed_workflow_revision: int,
        new_status: Optional[str] = None,
        review_required: Optional[bool] = None,
        clear_completed_at: bool = False,
        require_current_phase: Optional[str] = None,
        require_current_status: Optional[str] = None,
        require_review_required: Optional[bool] = None,
    ) -> Optional[int]:
        """`require_current_phase`/`require_current_status`/
        `require_review_required` reproduce the extra `WHERE` guards the
        notebook's resume transition alone carries (cell 107:
        `current_phase = 'HUMAN_REVIEW' AND current_status =
        'REVIEW_REQUIRED' AND review_required = TRUE`) -- claim/release/
        decision transitions (cells 105/106) never had them, so they stay
        optional here and default to unguarded (`None`)."""

        set_clauses = ["current_phase = %s", "lock_version = lock_version + 1", "updated_at = transaction_timestamp()"]
        params: list[Any] = [new_phase]

        if new_status is not None:
            set_clauses.append("current_status = %s")
            params.append(new_status)

        if review_required is not None:
            set_clauses.append("review_required = %s")
            params.append(review_required)

        if clear_completed_at:
            set_clauses.append("completed_at = NULL")

        where_clauses = ["tenant_id = %s", "workflow_id = %s", "lock_version = %s"]
        params.extend([tenant_id, workflow_id, observed_workflow_revision])

        if require_current_phase is not None:
            where_clauses.append("current_phase = %s")
            params.append(require_current_phase)

        if require_current_status is not None:
            where_clauses.append("current_status = %s")
            params.append(require_current_status)

        if require_review_required is not None:
            where_clauses.append("review_required = %s")
            params.append(require_review_required)

        cursor.execute(
            f"""
            UPDATE ap_agent.workflow_instances
            SET {", ".join(set_clauses)}
            WHERE {" AND ".join(where_clauses)}
            RETURNING lock_version;
            """,
            tuple(params),
        )

        row = cursor.fetchone()
        return None if row is None else int(row[0])

    def next_audit_sequence_number(self, cursor: "psycopg.Cursor", *, tenant_id: UUID, workflow_id: UUID) -> int:
        cursor.execute(
            """
            SELECT COALESCE(MAX(sequence_number), 0) + 1 FROM ap_agent.audit_events
            WHERE tenant_id = %s AND workflow_id = %s;
            """,
            (tenant_id, workflow_id),
        )
        return int(cursor.fetchone()[0])

    def insert_command_audit_event(
        self,
        cursor: "psycopg.Cursor",
        *,
        event_id: UUID,
        tenant_id: UUID,
        workflow_id: UUID,
        sequence_number: int,
        event_type: str,
        event_status: str,
        actor_role: str,
        actor_id: str,
        message: str,
        payload: dict[str, Any],
    ) -> None:
        from psycopg.types.json import Jsonb

        cursor.execute(
            """
            INSERT INTO ap_agent.audit_events
                (event_id, tenant_id, workflow_id, sequence_number, event_type, phase_name,
                 event_status, actor_type, actor_id, message, payload, occurred_at)
            VALUES (%s, %s, %s, %s, %s, 'HUMAN_REVIEW', %s, %s, %s, %s, %s, transaction_timestamp());
            """,
            (
                event_id, tenant_id, workflow_id, sequence_number, event_type, event_status,
                actor_role, actor_id, message, Jsonb(payload),
            ),
        )

    def append_review_decision(
        self,
        cursor: "psycopg.Cursor",
        *,
        decision_id: UUID,
        tenant_id: UUID,
        workflow_id: UUID,
        review_case_id: UUID,
        decision_type: str,
        decided_by: str,
        decision_notes: Optional[str],
        evidence: dict[str, Any],
    ) -> None:
        from psycopg.types.json import Jsonb

        cursor.execute(
            """
            INSERT INTO ap_agent.review_decisions
                (decision_id, tenant_id, workflow_id, review_id, decision_type, decided_by,
                 decision_notes, evidence, decided_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, transaction_timestamp());
            """,
            (decision_id, tenant_id, workflow_id, review_case_id, decision_type, decided_by, decision_notes, Jsonb(evidence)),
        )

    def resolve_review_case(
        self,
        cursor: "psycopg.Cursor",
        *,
        tenant_id: UUID,
        workflow_id: UUID,
        review_case_id: UUID,
        actor_id: str,
        resolution_code: str,
        resolution_notes: str,
    ) -> None:
        cursor.execute(
            """
            UPDATE ap_agent.review_cases
            SET review_status = 'RESOLVED', resolved_at = transaction_timestamp(),
                resolution_code = %s, resolution_notes = %s
            WHERE tenant_id = %s AND review_id = %s AND workflow_id = %s
              AND review_status = 'CLAIMED' AND assigned_to = %s;
            """,
            (resolution_code, resolution_notes, tenant_id, review_case_id, workflow_id, actor_id),
        )

        if cursor.rowcount != 1:
            raise ReviewIntegrityError("Review resolution affected an unexpected number of rows.")

    def latest_review_decision_for_resume(
        self, cursor: "psycopg.Cursor", *, tenant_id: UUID, workflow_id: UUID, review_case_id: UUID
    ) -> Optional[tuple[Any, ...]]:
        cursor.execute(
            """
            SELECT
                decision.decision_id, decision.decision_type, decision.decided_by, decision.evidence,
                invoice.normalized_invoice, invoice.financial_validation, invoice.matching_result,
                invoice.matched_reference_data, invoice.payload_sha256,
                invoice.original_memory_record_id, invoice.memory_version_id, invoice.memory_version_label
            FROM ap_agent.review_decisions AS decision
            JOIN ap_agent.review_case_effective_memory AS invoice
                ON invoice.tenant_id = decision.tenant_id AND invoice.review_id = decision.review_id
            WHERE decision.tenant_id = %s AND decision.workflow_id = %s AND decision.review_id = %s
            ORDER BY decision.decided_at DESC, decision.decision_id DESC LIMIT 1;
            """,
            (tenant_id, workflow_id, review_case_id),
        )
        return cursor.fetchone()
