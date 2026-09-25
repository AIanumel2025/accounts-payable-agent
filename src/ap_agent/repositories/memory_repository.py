"""Provider-neutral memory-repository contract.

New in M8 (task §5): the notebook never had a repository abstraction — its
Phase 7 cells called `psycopg` directly. This `Protocol` is the interface
`ap_agent.services.memory_service.MemoryService` programs against, so a
non-PostgreSQL implementation (an in-memory fake for unit tests, or a
different provider) can stand in without the service importing
`psycopg`/`ap_agent.repositories.postgres_memory_repository` at all
(task §2: "The repository must not import from services or
orchestration"; this module and its implementations sit *below* the
service in the dependency graph).

Every method takes `tenant_id` as an explicit argument (task §5: "Tenant
ID must be an explicit argument. Do not use global tenant state.").
Methods store and retrieve validated data; they never decide whether an
invoice should be approved, rejected or paid (task §5: "Do not add
orchestration policy.").
"""

from __future__ import annotations

from typing import Optional, Protocol, runtime_checkable
from uuid import UUID

from ap_agent.models.memory import (
    AuditMemoryEvent,
    HumanReviewDecision,
    InvoiceMemoryBundle,
    MatchedInvoiceMemoryRecord,
    WorkflowMemoryRecord,
)
from ap_agent.repositories.mapping import TenantRow

__all__ = ["MemoryRepository"]


@runtime_checkable
class MemoryRepository(Protocol):
    # -- Tenants --------------------------------------------------------

    def register_tenant(
        self,
        *,
        tenant_id: UUID,
        tenant_key: str,
        display_name: str,
    ) -> TenantRow: ...

    def get_tenant(self, *, tenant_id: UUID) -> Optional[TenantRow]: ...

    # -- Workflows --------------------------------------------------------

    def create_or_get_workflow(
        self,
        *,
        tenant_id: UUID,
        record: WorkflowMemoryRecord,
    ) -> WorkflowMemoryRecord: ...

    def get_workflow_by_document(
        self,
        *,
        tenant_id: UUID,
        document_id: UUID,
    ) -> Optional[WorkflowMemoryRecord]: ...

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
        produced_at,
        metadata: Optional[dict] = None,
    ) -> None: ...

    def list_phase_result_references(
        self,
        *,
        tenant_id: UUID,
        workflow_id: UUID,
    ) -> tuple: ...

    # -- Audit events (append-only) ---------------------------------------

    def append_audit_event(
        self,
        *,
        tenant_id: UUID,
        workflow_id: UUID,
        event: AuditMemoryEvent,
    ) -> AuditMemoryEvent: ...

    def get_audit_events(
        self,
        *,
        tenant_id: UUID,
        workflow_id: UUID,
    ) -> tuple[AuditMemoryEvent, ...]: ...

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
    ) -> UUID: ...

    def get_open_review_cases(self, *, tenant_id: UUID) -> tuple: ...

    def append_review_decision(
        self,
        *,
        tenant_id: UUID,
        workflow_id: UUID,
        review_id: UUID,
        decision: HumanReviewDecision,
    ) -> HumanReviewDecision: ...

    # -- Matched-invoice memory (append-only) ------------------------------

    def store_invoice_memory(
        self,
        *,
        tenant_id: UUID,
        record: MatchedInvoiceMemoryRecord,
    ) -> MatchedInvoiceMemoryRecord: ...

    def store_invoice_memory_batch(
        self,
        *,
        tenant_id: UUID,
        records: tuple[MatchedInvoiceMemoryRecord, ...],
        batch_size: int = 25,
    ) -> tuple[MatchedInvoiceMemoryRecord, ...]: ...

    def get_invoice_memory_by_document(
        self,
        *,
        tenant_id: UUID,
        document_id: UUID,
    ) -> Optional[MatchedInvoiceMemoryRecord]: ...

    def get_invoice_memory_bundle(
        self,
        *,
        tenant_id: UUID,
        document_id: UUID,
    ) -> Optional[InvoiceMemoryBundle]: ...

    def list_review_required_invoice_memory(
        self,
        *,
        tenant_id: UUID,
    ) -> tuple[MatchedInvoiceMemoryRecord, ...]: ...
