"""In-memory `MemoryRepository` test double.

Not a substitute for the required PostgreSQL behavioural tests (real row-
level security, real append-only triggers, real migrations) — those live
under `tests/integration/test_memory_postgres_integration.py`, marked
`requires_postgres`, and need a live PostgreSQL instance (see that file's
docstring and `docs/m8_phase_7_postgres_memory_report.md` for why they
could not be executed in this environment). This fake exists so
`ap_agent.services.memory_service.MemoryService` — which only depends on
the `ap_agent.repositories.memory_repository.MemoryRepository` Protocol,
never on `psycopg` — can be exercised end-to-end (alignment, deterministic
IDs, hashing, idempotency, collision detection, cross-document isolation)
on every test run, independent of database availability.

It reproduces the same observable contract as
`ap_agent.repositories.postgres_memory_repository.PostgresMemoryRepository`:
tenant-scoped storage, append-only collision detection (identical writes
are idempotent; conflicting writes under the same identity raise
`MemoryIntegrityError`), and no cross-tenant leakage.
"""

from __future__ import annotations

from typing import Optional
from uuid import UUID

from ap_agent.exceptions import MemoryIntegrityError
from ap_agent.models.memory import (
    AuditMemoryEvent,
    HumanReviewDecision,
    InvoiceMemoryBundle,
    MatchedInvoiceMemoryRecord,
    WorkflowMemoryRecord,
)
from ap_agent.repositories.mapping import TenantRow


class FakeMemoryRepository:
    def __init__(self) -> None:
        self._tenants: dict[UUID, TenantRow] = {}
        self._workflows: dict[tuple[UUID, UUID], WorkflowMemoryRecord] = {}
        self._phase_references: dict[tuple, dict] = {}
        self._audit_events: dict[tuple[UUID, UUID], list[AuditMemoryEvent]] = {}
        self._review_cases: dict[UUID, dict] = {}
        self._review_decisions: dict[tuple[UUID, UUID], HumanReviewDecision] = {}
        self._invoice_memory: dict[tuple[UUID, UUID], MatchedInvoiceMemoryRecord] = {}

    # -- Tenants --------------------------------------------------------

    def register_tenant(self, *, tenant_id, tenant_key, display_name) -> TenantRow:
        existing = self._tenants.get(tenant_id)

        if existing is not None:
            if existing.tenant_key != tenant_key or existing.display_name != display_name:
                raise MemoryIntegrityError(f"Tenant identity collision for {tenant_id}.")

            return existing

        row = TenantRow(tenant_id=tenant_id, tenant_key=tenant_key, display_name=display_name)
        self._tenants[tenant_id] = row

        return row

    def get_tenant(self, *, tenant_id) -> Optional[TenantRow]:
        return self._tenants.get(tenant_id)

    # -- Workflows -------------------------------------------------------

    def create_or_get_workflow(self, *, tenant_id, record: WorkflowMemoryRecord):
        key = (tenant_id, record.document_id)
        existing = self._workflows.get(key)

        if existing is not None:
            if (
                existing.memory_id != record.memory_id
                or existing.batch_id != record.batch_id
                or existing.source_document_sha256 != record.source_document_sha256
                or existing.source_name != record.source_name
            ):
                raise MemoryIntegrityError(f"Workflow identity collision for {record.source_name}.")

            return existing

        self._workflows[key] = record

        return record

    def get_workflow_by_document(self, *, tenant_id, document_id):
        record = self._workflows.get((tenant_id, document_id))

        if record is None:
            return None

        invoice = self._invoice_memory.get((tenant_id, document_id))

        if invoice is not None:
            from dataclasses import replace

            record = replace(
                record,
                latest_matching_result_id=invoice.matching_result_id,
                review_reasons=invoice.review_reasons,
            )

        return record

    # -- Phase-result references -------------------------------------------

    def store_phase_result_reference(
        self,
        *,
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
        metadata=None,
    ) -> None:
        key = (tenant_id, workflow_id, phase_name, phase_version, attempt_number)
        submitted = {
            "result_id": result_id,
            "result_status": result_status,
            "artifact_uri": artifact_uri,
            "artifact_sha256": artifact_sha256,
        }

        existing = self._phase_references.get(key)

        if existing is not None:
            if existing != submitted:
                raise MemoryIntegrityError(
                    f"Phase-result reference collision for {phase_name}/{workflow_id}."
                )

            return

        self._phase_references[key] = submitted

    def list_phase_result_references(self, *, tenant_id, workflow_id):
        return tuple(
            value
            for key, value in self._phase_references.items()
            if key[0] == tenant_id and key[1] == workflow_id
        )

    # -- Audit events (append-only) -----------------------------------------

    def append_audit_event(self, *, tenant_id, workflow_id, event: AuditMemoryEvent):
        key = (tenant_id, workflow_id)
        events = self._audit_events.setdefault(key, [])

        for existing in events:
            if existing.audit_event_id == event.audit_event_id:
                if existing != event:
                    raise MemoryIntegrityError(
                        f"Audit event collision for {event.audit_event_id}."
                    )

                return existing

        events.append(event)

        return event

    def get_audit_events(self, *, tenant_id, workflow_id):
        return tuple(self._audit_events.get((tenant_id, workflow_id), ()))

    # -- Review cases and decisions ------------------------------------------

    def open_review_case(
        self, *, tenant_id, workflow_id, review_id, reason_codes, summary, priority=3
    ):
        self._review_cases.setdefault(
            review_id,
            {
                "tenant_id": tenant_id,
                "workflow_id": workflow_id,
                "reason_codes": reason_codes,
                "summary": summary,
                "priority": priority,
                "status": "OPEN",
            },
        )

        return review_id

    def get_open_review_cases(self, *, tenant_id):
        return tuple(
            case
            for case in self._review_cases.values()
            if case["tenant_id"] == tenant_id and case["status"] == "OPEN"
        )

    def append_review_decision(self, *, tenant_id, workflow_id, review_id, decision):
        key = (tenant_id, decision.decision_id)
        existing = self._review_decisions.get(key)

        if existing is not None:
            if existing != decision:
                raise MemoryIntegrityError(f"Review decision collision for {decision.decision_id}.")

            return existing

        self._review_decisions[key] = decision

        return decision

    # -- Matched-invoice memory (append-only) --------------------------------

    def store_invoice_memory(self, *, tenant_id, record: MatchedInvoiceMemoryRecord):
        key = (tenant_id, record.document_id)
        existing = self._invoice_memory.get(key)

        if existing is not None:
            if existing.payload_sha256 != record.payload_sha256:
                raise MemoryIntegrityError(
                    "Stored invoice content differs from the current "
                    f"pipeline result for {record.source_name}."
                )

            return existing

        from dataclasses import replace

        from ap_agent.db.connection import memory_utc_now

        stored = replace(record, stored_at=memory_utc_now())
        self._invoice_memory[key] = stored

        return stored

    def store_invoice_memory_batch(self, *, tenant_id, records, batch_size=25):
        return tuple(
            self.store_invoice_memory(tenant_id=tenant_id, record=record) for record in records
        )

    def get_invoice_memory_by_document(self, *, tenant_id, document_id):
        return self._invoice_memory.get((tenant_id, document_id))

    def get_invoice_memory_bundle(self, *, tenant_id, document_id) -> Optional[InvoiceMemoryBundle]:
        workflow = self.get_workflow_by_document(tenant_id=tenant_id, document_id=document_id)

        if workflow is None:
            return None

        phase_references_raw = self.list_phase_result_references(
            tenant_id=tenant_id, workflow_id=workflow.memory_id
        )

        from ap_agent.models.memory import PhaseResultReference
        from ap_agent.db.connection import memory_utc_now

        phase_references = tuple(
            PhaseResultReference(
                reference_id=workflow.memory_id,
                tenant_id=str(tenant_id),
                batch_id=workflow.batch_id,
                document_id=workflow.document_id,
                phase_name="REFERENCE_MATCHING",
                result_id=ref["result_id"],
                result_status=ref["result_status"],
                artifact_directory=ref["artifact_uri"],
                artifact_manifest_sha256=ref["artifact_sha256"],
                result_payload_sha256=ref["artifact_sha256"],
                recorded_at=memory_utc_now(),
            )
            for ref in phase_references_raw
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

    def list_review_required_invoice_memory(self, *, tenant_id):
        return tuple(
            record
            for (record_tenant_id, _document_id), record in self._invoice_memory.items()
            if record_tenant_id == tenant_id and record.review_required
        )
