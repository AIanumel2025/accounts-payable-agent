"""PostgreSQL implementation of `ap_agent.repositories.memory_repository.MemoryRepository`.

Source: notebook cell 88 ("PHASE 7 — REPLACEMENT CELL 5")'s
`set_memory_tenant`, `persist_invoice_memory_records`,
`retrieve_matched_invoice_memory`, generalized per M8 task brief §5:

  - `persist_invoice_memory_records` hardcoded one `INSERT ... VALUES
    ('REFERENCE_MATCHING', ...)` phase-result reference per invoice; this
    module's `store_phase_result_reference` accepts `phase_name` as a
    parameter (task §3.5: "Implement a generic phase-reference repository
    operation capable of storing references for any phase").
  - The notebook's insert/verify pattern (`INSERT ... ON CONFLICT DO
    NOTHING`, then `SELECT` the stored row back and compare it to what was
    just submitted, raising if they differ) is preserved and applied
    uniformly to every append-only table this module writes to (task §5:
    "`ON CONFLICT DO NOTHING` must never hide a content mismatch."; "Identical
    deterministic writes must be idempotent. Conflicting content under the
    same deterministic identity must raise a typed integrity exception."),
    not just `invoice_memory_records` as the notebook did.
  - `PROTOTYPE_TENANT_ID`/the module-level `POSTGRES_DSN` are gone: every
    method takes `tenant_id` explicitly, and this class takes a `dsn` and
    `MemoryConfig` at construction, never reading a hidden global or
    connecting at import time.

Every method opens one short-lived connection per call
(`ap_agent.db.connection.open_connection`), matching the notebook's own
one-`psycopg.connect(...)`-per-function style; `ap_agent.db.connection`
also exposes `create_connection_pool` for a caller that wants to reuse
connections across calls (not wired in here — task §4A's pool-sizing
configuration is honoured by that helper when a caller chooses to use it).

Every tenant-scoped method sets `ap_agent.tenant_id` transaction-locally
via `set_tenant_context` (task §5) before issuing any other statement, so
that PostgreSQL's row-level-security policies (migration 0002/0003) apply
regardless of runtime role — a missing/failed tenant context fails closed
(`TenantContextError`, not silently exposing zero or wrong-tenant rows).

All user-controlled values (tenant ids, filenames, invoice fields,
hashes, ...) are passed as `cursor.execute(sql, params)` parameters; no
value is ever interpolated into a SQL string (task §5: "Parameterize all
user-controlled values.").
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any, Optional
from uuid import UUID

from ap_agent.config.postgres import MemoryConfig
from ap_agent.db.connection import open_connection, set_tenant_context
from ap_agent.exceptions import MemoryIntegrityError
from ap_agent.models.memory import (
    AuditMemoryEvent,
    HumanReviewDecision,
    InvoiceMemoryBundle,
    MatchedInvoiceMemoryRecord,
    WorkflowMemoryRecord,
)
from ap_agent.repositories.mapping import (
    InvoiceMemoryRow,
    PhaseReferenceRow,
    TenantRow,
    WorkflowRow,
    audit_event_row_to_domain,
    domain_to_audit_event_row,
    domain_to_invoice_memory_row,
    domain_to_workflow_row,
    invoice_memory_row_to_domain,
    workflow_row_to_domain,
)

if TYPE_CHECKING:  # pragma: no cover
    import psycopg

__all__ = ["PostgresMemoryRepository"]


class PostgresMemoryRepository:
    """PostgreSQL-backed implementation of `MemoryRepository`."""

    def __init__(self, dsn: str, config: MemoryConfig) -> None:
        self._dsn = dsn
        self._config = config

    # -- connection helper -------------------------------------------------

    def _connection(self):
        return open_connection(self._dsn, self._config)

    # -- Tenants -------------------------------------------------------

    def register_tenant(
        self,
        *,
        tenant_id: UUID,
        tenant_key: str,
        display_name: str,
    ) -> TenantRow:
        with self._connection() as connection:
            with connection.cursor() as cursor:
                set_tenant_context(cursor, str(tenant_id))

                cursor.execute(
                    """
                    INSERT INTO ap_agent.tenants
                        (tenant_id, tenant_key, display_name, status)
                    VALUES (%s, %s, %s, 'ACTIVE')
                    ON CONFLICT (tenant_id) DO NOTHING;
                    """,
                    (tenant_id, tenant_key, display_name),
                )

                cursor.execute(
                    """
                    SELECT tenant_id, tenant_key, display_name, status,
                           created_at, updated_at
                    FROM ap_agent.tenants
                    WHERE tenant_id = %s;
                    """,
                    (tenant_id,),
                )

                row = cursor.fetchone()

        if row is None:
            raise MemoryIntegrityError(
                f"Tenant {tenant_id} was not stored.",
                details={"tenant_id": str(tenant_id)},
            )

        stored = TenantRow(*row)

        if stored.tenant_key != tenant_key or stored.display_name != display_name:
            raise MemoryIntegrityError(
                f"Tenant identity collision for {tenant_id}: stored "
                f"tenant_key/display_name differ from the submitted values.",
                details={"tenant_id": str(tenant_id)},
            )

        return stored

    def get_tenant(self, *, tenant_id: UUID) -> Optional[TenantRow]:
        with self._connection() as connection:
            with connection.cursor() as cursor:
                set_tenant_context(cursor, str(tenant_id))

                cursor.execute(
                    """
                    SELECT tenant_id, tenant_key, display_name, status,
                           created_at, updated_at
                    FROM ap_agent.tenants
                    WHERE tenant_id = %s;
                    """,
                    (tenant_id,),
                )

                row = cursor.fetchone()

        return TenantRow(*row) if row is not None else None

    # -- Workflows -------------------------------------------------------

    def create_or_get_workflow(
        self,
        *,
        tenant_id: UUID,
        record: WorkflowMemoryRecord,
    ) -> WorkflowMemoryRecord:
        row = domain_to_workflow_row(record)

        with self._connection() as connection:
            with connection.cursor() as cursor:
                set_tenant_context(cursor, str(tenant_id))

                cursor.execute(
                    """
                    INSERT INTO ap_agent.workflow_instances
                        (workflow_id, tenant_id, batch_id, document_id,
                         source_document_sha256, source_name,
                         current_phase, current_status, review_required)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (tenant_id, document_id) DO NOTHING;
                    """,
                    (
                        row.workflow_id,
                        tenant_id,
                        row.batch_id,
                        row.document_id,
                        row.source_document_sha256,
                        row.source_name,
                        row.current_phase,
                        row.current_status,
                        row.review_required,
                    ),
                )

                stored_row = self._fetch_workflow_row(
                    cursor, tenant_id=tenant_id, document_id=row.document_id
                )

        if stored_row is None:
            raise MemoryIntegrityError(
                f"Workflow for document {row.document_id} was not stored.",
                details={"document_id": str(row.document_id)},
            )

        expected = (row.workflow_id, row.batch_id, row.source_document_sha256, row.source_name)
        observed = (
            stored_row.workflow_id,
            stored_row.batch_id,
            stored_row.source_document_sha256,
            stored_row.source_name,
        )

        if observed != expected:
            raise MemoryIntegrityError(
                f"Workflow identity collision for {row.source_name}.",
                details={"document_id": str(row.document_id)},
            )

        return workflow_row_to_domain(stored_row)

    @staticmethod
    def _fetch_workflow_row(
        cursor: "psycopg.Cursor",
        *,
        tenant_id: UUID,
        document_id: UUID,
    ) -> Optional[WorkflowRow]:
        cursor.execute(
            """
            SELECT workflow_id, tenant_id, batch_id, document_id,
                   source_document_sha256, source_name, current_phase,
                   current_status, review_required, lock_version,
                   created_at, updated_at, completed_at
            FROM ap_agent.workflow_instances
            WHERE tenant_id = %s AND document_id = %s;
            """,
            (tenant_id, document_id),
        )

        row = cursor.fetchone()

        return WorkflowRow(*row) if row is not None else None

    def get_workflow_by_document(
        self,
        *,
        tenant_id: UUID,
        document_id: UUID,
    ) -> Optional[WorkflowMemoryRecord]:
        with self._connection() as connection:
            with connection.cursor() as cursor:
                set_tenant_context(cursor, str(tenant_id))

                row = self._fetch_workflow_row(
                    cursor, tenant_id=tenant_id, document_id=document_id
                )

                if row is None:
                    return None

                review_reasons: tuple[str, ...] = ()
                latest_matching_result_id: Optional[UUID] = None

                cursor.execute(
                    """
                    SELECT matching_result_id, review_reasons
                    FROM ap_agent.invoice_memory_records
                    WHERE tenant_id = %s AND document_id = %s;
                    """,
                    (tenant_id, document_id),
                )

                invoice_row = cursor.fetchone()

                if invoice_row is not None:
                    latest_matching_result_id, review_reasons = (
                        invoice_row[0],
                        tuple(invoice_row[1] or ()),
                    )

        return workflow_row_to_domain(
            row,
            review_reasons=review_reasons,
            latest_matching_result_id=latest_matching_result_id,
        )

    # -- Phase-result references (generic, any phase) ---------------------

    def store_phase_result_reference(
        self,
        *,
        tenant_id: UUID,
        workflow_id: UUID,
        phase_name: str,
        phase_version: str,
        attempt_number: int,
        result_id: str,
        result_status: str,
        artifact_uri: str,
        artifact_sha256: str,
        review_required: bool,
        produced_at: datetime,
        metadata: Optional[dict[str, Any]] = None,
    ) -> None:
        from uuid import uuid5, NAMESPACE_URL
        from psycopg.types.json import Jsonb

        reference_id = uuid5(
            NAMESPACE_URL,
            f"ap-agent/phase-reference/{tenant_id}/{workflow_id}/"
            f"{phase_name}/{phase_version}/{attempt_number}",
        )

        with self._connection() as connection:
            with connection.cursor() as cursor:
                set_tenant_context(cursor, str(tenant_id))

                cursor.execute(
                    """
                    INSERT INTO ap_agent.phase_result_references
                        (reference_id, tenant_id, workflow_id, phase_name,
                         phase_version, attempt_number, result_id,
                         result_status, artifact_uri, artifact_sha256,
                         review_required, produced_at, metadata)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (tenant_id, workflow_id, phase_name,
                                 phase_version, attempt_number)
                    DO NOTHING;
                    """,
                    (
                        reference_id,
                        tenant_id,
                        workflow_id,
                        phase_name,
                        phase_version,
                        attempt_number,
                        result_id,
                        result_status,
                        artifact_uri,
                        artifact_sha256,
                        review_required,
                        produced_at,
                        Jsonb(metadata or {}),
                    ),
                )

                cursor.execute(
                    """
                    SELECT artifact_uri, artifact_sha256, result_id, result_status
                    FROM ap_agent.phase_result_references
                    WHERE tenant_id = %s AND workflow_id = %s
                      AND phase_name = %s AND phase_version = %s
                      AND attempt_number = %s;
                    """,
                    (tenant_id, workflow_id, phase_name, phase_version, attempt_number),
                )

                stored = cursor.fetchone()

        if stored is None:
            raise MemoryIntegrityError(
                f"Phase-result reference for {phase_name}/{workflow_id} was not stored."
            )

        if stored != (artifact_uri, artifact_sha256, result_id, result_status):
            raise MemoryIntegrityError(
                f"Phase-result reference collision for {phase_name}/{workflow_id}: "
                "stored content differs from the submitted reference."
            )

    def list_phase_result_references(
        self,
        *,
        tenant_id: UUID,
        workflow_id: UUID,
    ) -> tuple[PhaseReferenceRow, ...]:
        with self._connection() as connection:
            with connection.cursor() as cursor:
                set_tenant_context(cursor, str(tenant_id))

                cursor.execute(
                    """
                    SELECT reference_id, tenant_id, workflow_id, phase_name,
                           phase_version, result_id, result_status,
                           artifact_uri, artifact_sha256, attempt_number,
                           review_required, produced_at, recorded_at, metadata
                    FROM ap_agent.phase_result_references
                    WHERE tenant_id = %s AND workflow_id = %s
                    ORDER BY phase_name, phase_version, attempt_number;
                    """,
                    (tenant_id, workflow_id),
                )

                rows = cursor.fetchall()

        return tuple(PhaseReferenceRow(*row) for row in rows)

    # -- Audit events (append-only) ---------------------------------------

    def append_audit_event(
        self,
        *,
        tenant_id: UUID,
        workflow_id: UUID,
        event: AuditMemoryEvent,
    ) -> AuditMemoryEvent:
        from psycopg.types.json import Jsonb

        with self._connection() as connection:
            with connection.cursor() as cursor:
                set_tenant_context(cursor, str(tenant_id))

                cursor.execute(
                    """
                    SELECT COALESCE(MAX(sequence_number), 0) + 1
                    FROM ap_agent.audit_events
                    WHERE tenant_id = %s AND workflow_id = %s;
                    """,
                    (tenant_id, workflow_id),
                )

                next_sequence = cursor.fetchone()[0]

                row = domain_to_audit_event_row(
                    event,
                    workflow_id=workflow_id,
                    sequence_number=next_sequence,
                )

                cursor.execute(
                    """
                    INSERT INTO ap_agent.audit_events
                        (event_id, tenant_id, workflow_id, sequence_number,
                         event_type, phase_name, event_status, actor_type,
                         actor_id, message, payload, occurred_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (tenant_id, event_id) DO NOTHING;
                    """,
                    (
                        row.event_id,
                        tenant_id,
                        workflow_id,
                        row.sequence_number,
                        row.event_type,
                        row.phase_name,
                        row.event_status,
                        row.actor_type,
                        row.actor_id,
                        row.message,
                        Jsonb(row.payload),
                        row.occurred_at,
                    ),
                )

                cursor.execute(
                    """
                    SELECT event_id, tenant_id, workflow_id, sequence_number,
                           event_type, phase_name, event_status, actor_type,
                           message, actor_id, payload, occurred_at, recorded_at
                    FROM ap_agent.audit_events
                    WHERE tenant_id = %s AND event_id = %s;
                    """,
                    (tenant_id, row.event_id),
                )

                stored = cursor.fetchone()

        if stored is None:
            raise MemoryIntegrityError(f"Audit event {row.event_id} was not stored.")

        stored_row = type(row)(*stored)

        if stored_row.payload.get("memory_event") != row.payload.get("memory_event"):
            raise MemoryIntegrityError(
                f"Audit event collision for {row.event_id}: stored content "
                "differs from the submitted event.",
                details={"event_id": str(row.event_id)},
            )

        return audit_event_row_to_domain(stored_row)

    def get_audit_events(
        self,
        *,
        tenant_id: UUID,
        workflow_id: UUID,
    ) -> tuple[AuditMemoryEvent, ...]:
        from ap_agent.repositories.mapping import AuditEventRow

        with self._connection() as connection:
            with connection.cursor() as cursor:
                set_tenant_context(cursor, str(tenant_id))

                cursor.execute(
                    """
                    SELECT event_id, tenant_id, workflow_id, sequence_number,
                           event_type, phase_name, event_status, actor_type,
                           message, actor_id, payload, occurred_at, recorded_at
                    FROM ap_agent.audit_events
                    WHERE tenant_id = %s AND workflow_id = %s
                    ORDER BY sequence_number;
                    """,
                    (tenant_id, workflow_id),
                )

                rows = cursor.fetchall()

        return tuple(
            audit_event_row_to_domain(AuditEventRow(*row)) for row in rows
        )

    # -- Review cases and decisions ---------------------------------------

    def open_review_case(
        self,
        *,
        tenant_id: UUID,
        workflow_id: UUID,
        review_id: UUID,
        reason_codes: tuple[str, ...],
        summary: str,
        priority: int = 3,
    ) -> UUID:
        with self._connection() as connection:
            with connection.cursor() as cursor:
                set_tenant_context(cursor, str(tenant_id))

                cursor.execute(
                    """
                    INSERT INTO ap_agent.review_cases
                        (review_id, tenant_id, workflow_id, review_status,
                         priority, reason_codes, summary)
                    VALUES (%s, %s, %s, 'OPEN', %s, %s, %s)
                    ON CONFLICT (review_id) DO NOTHING;
                    """,
                    (review_id, tenant_id, workflow_id, priority, list(reason_codes), summary),
                )

        return review_id

    def get_open_review_cases(self, *, tenant_id: UUID):
        from ap_agent.repositories.mapping import ReviewCaseRow

        with self._connection() as connection:
            with connection.cursor() as cursor:
                set_tenant_context(cursor, str(tenant_id))

                cursor.execute(
                    """
                    SELECT review_id, tenant_id, workflow_id, reason_codes,
                           summary, review_status, priority, assigned_to,
                           opened_at, due_at, resolved_at, resolution_code,
                           resolution_notes
                    FROM ap_agent.review_cases
                    WHERE tenant_id = %s AND review_status IN ('OPEN', 'CLAIMED')
                    ORDER BY priority, opened_at;
                    """,
                    (tenant_id,),
                )

                rows = cursor.fetchall()

        return tuple(
            ReviewCaseRow(
                review_id=row[0],
                tenant_id=row[1],
                workflow_id=row[2],
                reason_codes=tuple(row[3] or ()),
                summary=row[4],
                review_status=row[5],
                priority=row[6],
                assigned_to=row[7],
                opened_at=row[8],
                due_at=row[9],
                resolved_at=row[10],
                resolution_code=row[11],
                resolution_notes=row[12],
            )
            for row in rows
        )

    def append_review_decision(
        self,
        *,
        tenant_id: UUID,
        workflow_id: UUID,
        review_id: UUID,
        decision: HumanReviewDecision,
    ) -> HumanReviewDecision:
        from psycopg.types.json import Jsonb
        from ap_agent.repositories.mapping import (
            ReviewDecisionRow,
            domain_to_review_decision_row,
            review_decision_row_to_domain,
        )

        row = domain_to_review_decision_row(decision, workflow_id=workflow_id, review_id=review_id)

        with self._connection() as connection:
            with connection.cursor() as cursor:
                set_tenant_context(cursor, str(tenant_id))

                cursor.execute(
                    """
                    INSERT INTO ap_agent.review_decisions
                        (decision_id, tenant_id, workflow_id, review_id,
                         decision_type, decided_by, decision_notes,
                         evidence, decided_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (tenant_id, decision_id) DO NOTHING;
                    """,
                    (
                        row.decision_id,
                        tenant_id,
                        workflow_id,
                        review_id,
                        row.decision_type,
                        row.decided_by,
                        row.decision_notes,
                        Jsonb(row.evidence),
                        row.decided_at,
                    ),
                )

                cursor.execute(
                    """
                    SELECT decision_id, tenant_id, workflow_id, review_id,
                           decision_type, decided_by, decided_at,
                           decision_notes, evidence, recorded_at
                    FROM ap_agent.review_decisions
                    WHERE tenant_id = %s AND decision_id = %s;
                    """,
                    (tenant_id, row.decision_id),
                )

                stored = cursor.fetchone()

        if stored is None:
            raise MemoryIntegrityError(f"Review decision {row.decision_id} was not stored.")

        stored_row = ReviewDecisionRow(*stored)

        if stored_row.evidence.get("memory_decision") != row.evidence.get("memory_decision"):
            raise MemoryIntegrityError(
                f"Review decision collision for {row.decision_id}: stored "
                "content differs from the submitted decision.",
                details={"decision_id": str(row.decision_id)},
            )

        return review_decision_row_to_domain(stored_row)

    # -- Matched-invoice memory (append-only) ------------------------------

    def store_invoice_memory(
        self,
        *,
        tenant_id: UUID,
        record: MatchedInvoiceMemoryRecord,
    ) -> MatchedInvoiceMemoryRecord:
        from psycopg.types.json import Jsonb

        row = domain_to_invoice_memory_row(record)

        with self._connection() as connection:
            with connection.cursor() as cursor:
                set_tenant_context(cursor, str(tenant_id))
                self._insert_invoice_memory_row(cursor, tenant_id, row)
                stored_row = self._fetch_invoice_memory_row(
                    cursor, tenant_id=tenant_id, document_id=row.document_id
                )

        return self._verify_and_convert_invoice_row(stored_row, row)

    def store_invoice_memory_batch(
        self,
        *,
        tenant_id: UUID,
        records: tuple[MatchedInvoiceMemoryRecord, ...],
        batch_size: int = 25,
    ) -> tuple[MatchedInvoiceMemoryRecord, ...]:
        """Persist an arbitrary number of records, `batch_size` per
        transaction (task §6.10/§6.12: "Support arbitrary batch sizes" /
        "Keep batch-size and transaction-boundary policy explicit and
        configurable rather than hard-coded.")."""

        results: list[MatchedInvoiceMemoryRecord] = []

        for offset in range(0, len(records), max(batch_size, 1)):
            chunk = records[offset : offset + batch_size]

            with self._connection() as connection:
                with connection.cursor() as cursor:
                    set_tenant_context(cursor, str(tenant_id))

                    rows = [domain_to_invoice_memory_row(record) for record in chunk]

                    for row in rows:
                        self._insert_invoice_memory_row(cursor, tenant_id, row)

                    for row in rows:
                        stored_row = self._fetch_invoice_memory_row(
                            cursor, tenant_id=tenant_id, document_id=row.document_id
                        )
                        results.append(self._verify_and_convert_invoice_row(stored_row, row))

        return tuple(results)

    @staticmethod
    def _insert_invoice_memory_row(
        cursor: "psycopg.Cursor",
        tenant_id: UUID,
        row: InvoiceMemoryRow,
    ) -> None:
        from psycopg.types.json import Jsonb

        cursor.execute(
            """
            INSERT INTO ap_agent.invoice_memory_records
                (memory_record_id, tenant_id, workflow_id, batch_id,
                 document_id, matching_result_id, source_name,
                 source_document_sha256, invoice_record_id, invoice_number,
                 supplier_name, currency, total_amount,
                 supplier_resolution_status, matched_supplier_id,
                 purchase_order_status, purchase_order_id,
                 purchase_order_number, goods_receipt_status,
                 goods_receipt_ids, match_mode, line_match_count,
                 normalization_status, financial_validation_status,
                 matching_status, review_required, review_reasons,
                 normalized_invoice, financial_validation, matching_result,
                 matched_reference_data, payload_sha256)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                    %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                    %s, %s, %s, %s, %s)
            ON CONFLICT (tenant_id, document_id) DO NOTHING;
            """,
            (
                row.memory_record_id,
                tenant_id,
                row.workflow_id,
                row.batch_id,
                row.document_id,
                row.matching_result_id,
                row.source_name,
                row.source_document_sha256,
                row.invoice_record_id,
                row.invoice_number,
                row.supplier_name,
                row.currency,
                row.total_amount,
                row.supplier_resolution_status,
                row.matched_supplier_id,
                row.purchase_order_status,
                row.purchase_order_id,
                row.purchase_order_number,
                row.goods_receipt_status,
                list(row.goods_receipt_ids),
                row.match_mode,
                row.line_match_count,
                row.normalization_status,
                row.financial_validation_status,
                row.matching_status,
                row.review_required,
                list(row.review_reasons),
                Jsonb(row.normalized_invoice),
                Jsonb(row.financial_validation),
                Jsonb(row.matching_result),
                Jsonb(row.matched_reference_data),
                row.payload_sha256,
            ),
        )

    @staticmethod
    def _fetch_invoice_memory_row(
        cursor: "psycopg.Cursor",
        *,
        tenant_id: UUID,
        document_id: UUID,
    ) -> Optional[InvoiceMemoryRow]:
        cursor.execute(
            """
            SELECT memory_record_id, tenant_id, workflow_id, batch_id,
                   document_id, matching_result_id, source_name,
                   source_document_sha256, invoice_record_id,
                   supplier_resolution_status, purchase_order_status,
                   goods_receipt_status, match_mode, line_match_count,
                   normalization_status, financial_validation_status,
                   matching_status, review_required, payload_sha256,
                   invoice_number, supplier_name, currency, total_amount,
                   matched_supplier_id, purchase_order_id,
                   purchase_order_number, goods_receipt_ids,
                   review_reasons, normalized_invoice, financial_validation,
                   matching_result, matched_reference_data, stored_at
            FROM ap_agent.invoice_memory_records
            WHERE tenant_id = %s AND document_id = %s;
            """,
            (tenant_id, document_id),
        )

        row = cursor.fetchone()

        if row is None:
            return None

        (
            memory_record_id,
            row_tenant_id,
            workflow_id,
            batch_id,
            document_id,
            matching_result_id,
            source_name,
            source_document_sha256,
            invoice_record_id,
            supplier_resolution_status,
            purchase_order_status,
            goods_receipt_status,
            match_mode,
            line_match_count,
            normalization_status,
            financial_validation_status,
            matching_status,
            review_required,
            payload_sha256,
            invoice_number,
            supplier_name,
            currency,
            total_amount,
            matched_supplier_id,
            purchase_order_id,
            purchase_order_number,
            goods_receipt_ids,
            review_reasons,
            normalized_invoice,
            financial_validation,
            matching_result,
            matched_reference_data,
            stored_at,
        ) = row

        return InvoiceMemoryRow(
            memory_record_id=memory_record_id,
            tenant_id=row_tenant_id,
            workflow_id=workflow_id,
            batch_id=batch_id,
            document_id=document_id,
            matching_result_id=matching_result_id,
            source_name=source_name,
            source_document_sha256=source_document_sha256,
            invoice_record_id=invoice_record_id,
            supplier_resolution_status=supplier_resolution_status,
            purchase_order_status=purchase_order_status,
            goods_receipt_status=goods_receipt_status,
            match_mode=match_mode,
            line_match_count=line_match_count,
            normalization_status=normalization_status,
            financial_validation_status=financial_validation_status,
            matching_status=matching_status,
            review_required=review_required,
            payload_sha256=payload_sha256.strip(),
            invoice_number=invoice_number,
            supplier_name=supplier_name,
            currency=currency,
            total_amount=total_amount,
            matched_supplier_id=matched_supplier_id,
            purchase_order_id=purchase_order_id,
            purchase_order_number=purchase_order_number,
            goods_receipt_ids=tuple(goods_receipt_ids or ()),
            review_reasons=tuple(review_reasons or ()),
            normalized_invoice=normalized_invoice,
            financial_validation=financial_validation,
            matching_result=matching_result,
            matched_reference_data=matched_reference_data,
            stored_at=stored_at,
        )

    @staticmethod
    def _verify_and_convert_invoice_row(
        stored_row: Optional[InvoiceMemoryRow],
        submitted_row: InvoiceMemoryRow,
    ) -> MatchedInvoiceMemoryRecord:
        if stored_row is None:
            raise MemoryIntegrityError(
                f"Invoice memory for document {submitted_row.document_id} was not stored."
            )

        if stored_row.payload_sha256 != submitted_row.payload_sha256:
            raise MemoryIntegrityError(
                "Stored invoice content differs from the current pipeline "
                f"result for {submitted_row.source_name}.",
                details={"document_id": str(submitted_row.document_id)},
            )

        return invoice_memory_row_to_domain(stored_row)

    def get_invoice_memory_by_document(
        self,
        *,
        tenant_id: UUID,
        document_id: UUID,
    ) -> Optional[MatchedInvoiceMemoryRecord]:
        with self._connection() as connection:
            with connection.cursor() as cursor:
                set_tenant_context(cursor, str(tenant_id))

                row = self._fetch_invoice_memory_row(
                    cursor, tenant_id=tenant_id, document_id=document_id
                )

        return invoice_memory_row_to_domain(row) if row is not None else None

    def get_invoice_memory_bundle(
        self,
        *,
        tenant_id: UUID,
        document_id: UUID,
    ) -> Optional[InvoiceMemoryBundle]:
        workflow = self.get_workflow_by_document(tenant_id=tenant_id, document_id=document_id)

        if workflow is None:
            return None

        phase_references_rows = self.list_phase_result_references(
            tenant_id=tenant_id, workflow_id=workflow.memory_id
        )

        from ap_agent.repositories.mapping import phase_reference_row_to_domain

        phase_references = tuple(
            phase_reference_row_to_domain(
                row, batch_id=workflow.batch_id, document_id=workflow.document_id
            )
            for row in phase_references_rows
        )

        audit_events = self.get_audit_events(tenant_id=tenant_id, workflow_id=workflow.memory_id)

        invoice_memory = self.get_invoice_memory_by_document(
            tenant_id=tenant_id, document_id=document_id
        )

        return InvoiceMemoryBundle(
            memory_record=workflow,
            phase_references=phase_references,
            audit_events=audit_events,
            review_decisions=(),
            invoice_memory=invoice_memory,
        )

    def list_review_required_invoice_memory(
        self,
        *,
        tenant_id: UUID,
    ) -> tuple[MatchedInvoiceMemoryRecord, ...]:
        with self._connection() as connection:
            with connection.cursor() as cursor:
                set_tenant_context(cursor, str(tenant_id))

                cursor.execute(
                    """
                    SELECT document_id
                    FROM ap_agent.invoice_memory_records
                    WHERE tenant_id = %s AND review_required = TRUE
                    ORDER BY stored_at DESC;
                    """,
                    (tenant_id,),
                )

                document_ids = [row[0] for row in cursor.fetchall()]

                records = []

                for document_id in document_ids:
                    row = self._fetch_invoice_memory_row(
                        cursor, tenant_id=tenant_id, document_id=document_id
                    )

                    if row is not None:
                        records.append(invoice_memory_row_to_domain(row))

        return tuple(records)
