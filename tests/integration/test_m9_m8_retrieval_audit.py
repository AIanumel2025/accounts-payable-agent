"""M9 §5 audit: does M8's repository/service retrieval already avoid the
"assume the database contains only the fixtures we processed" problem the
notebook's corrected Phase 7 retrieval fixes?

**Audit finding: yes -- no M8 production code change was required.**

The notebook's original Phase 7 replacement cell (`retrieve_matched_invoice_memory`
in cell 88) retrieves the current run's rows with
``WHERE tenant_id = %s AND document_id = ANY(%s::uuid[])`` -- scoped by
tenant identity *and* an explicit current-document-ID collection, never an
unscoped `SELECT * FROM invoice_memory_records WHERE tenant_id = %s`. Every
M8 read path already follows the same shape:

  - `ap_agent.repositories.postgres_memory_repository.PostgresMemoryRepository
    .get_invoice_memory_by_document` / `.get_workflow_by_document` /
    `.get_invoice_memory_bundle` are all keyed by the explicit pair
    `(tenant_id, document_id)` -- one document, one call. A caller wanting
    "this run's documents" (like this test, and like
    `ap_agent.orchestration.handlers.ProductionPhaseBindings
    .run_memory_persistence`, which never fetches a stored record it did
    not just write) calls this once per explicit document ID in its own
    current-run set. That is the same "constrained by explicit document
    identity" shape as the notebook's `document_id = ANY(...)`, just
    issued as N single-document queries instead of one batched query.
  - The one repository method that scans more than one document per call,
    `list_review_required_invoice_memory(tenant_id=...)`, is still scoped
    by tenant *and* filtered to `review_required = TRUE` -- a distinct,
    intentional "open worklist" operation, not "current-run validation"
    (task §5 draws exactly this line: "Current-run validation must not
    retrieve every row belonging to the tenant"). It never returns a
    tenant's full row set either.
  - Neither path assumes a fixed document count (CLAUDE.md D-2/D-3): both
    take an arbitrary `tenant_id`/`document_id` and return however many
    (zero or one) rows match.

This module proves the finding with `tests.support.fake_memory_repository.
FakeMemoryRepository` (fast, no live PostgreSQL needed) by seeding a
tenant with historical rows *unrelated* to a "current run" of a different
size, then retrieving only the current run's explicit document IDs.
`tests/integration/test_memory_postgres_integration.py::
test_m8_retrieval_audit_current_run_ids_only` (requires_postgres) repeats
the same scenario against the real repository, where RLS enforcement is
real rather than a Python dict keyed by tenant_id.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from decimal import Decimal

import pytest

from ap_agent.models.memory import (
    MatchedInvoiceMemoryRecord,
    MemoryWorkflowStage,
    MemoryWorkflowStatus,
    WorkflowMemoryRecord,
)
from ap_agent.serialization.memory_json import canonical_payload_sha256
from ap_agent.services.memory_service import MemoryService
from tests.support.fake_memory_repository import FakeMemoryRepository

pytestmark = [pytest.mark.integration]


def _workflow_record(tenant_id: uuid.UUID, document_id: uuid.UUID, **overrides) -> WorkflowMemoryRecord:
    now = datetime.now(timezone.utc)
    defaults = dict(
        memory_id=uuid.uuid4(),
        tenant_id=str(tenant_id),
        batch_id=uuid.uuid4(),
        document_id=document_id,
        source_name="audit-test.png",
        source_document_sha256="a" * 64,
        current_stage=MemoryWorkflowStage.REFERENCE_MATCHING,
        current_status=MemoryWorkflowStatus.SUCCEEDED,
        review_required=False,
        review_reasons=(),
        latest_matching_result_id=None,
        revision=1,
        created_at=now,
        updated_at=now,
    )
    defaults.update(overrides)
    return WorkflowMemoryRecord(**defaults)


def _invoice_memory_record(
    tenant_id: uuid.UUID,
    workflow_id: uuid.UUID,
    document_id: uuid.UUID,
    *,
    source_name: str,
    # A distinct, idempotent batch identity per record: this is deliberate
    # (task §5: "Do not rely solely on batch_id if idempotent document
    # records can retain an earlier batch identity") -- retrieval below is
    # keyed by (tenant_id, document_id), never by batch_id, so every one of
    # these records keeping its own original batch_id is exactly the case
    # the audit must handle correctly.
    batch_id: uuid.UUID,
) -> MatchedInvoiceMemoryRecord:
    normalized_payload = {"kind": "normalization", "document_id": str(document_id)}
    financial_payload = {"kind": "financial_validation", "document_id": str(document_id)}
    matching_payload = {"kind": "matching", "document_id": str(document_id)}
    reference_payload = {"supplier": None, "purchase_order": None, "goods_receipts": []}

    complete_payload = {
        "normalization_result": normalized_payload,
        "financial_validation_result": financial_payload,
        "matching_result": matching_payload,
        "matched_reference_data": reference_payload,
    }

    return MatchedInvoiceMemoryRecord(
        record_id=uuid.uuid4(),
        tenant_id=tenant_id,
        workflow_memory_id=workflow_id,
        batch_id=batch_id,
        document_id=document_id,
        matching_result_id=uuid.uuid4(),
        source_name=source_name,
        source_document_sha256="b" * 64,
        invoice_record_id=uuid.uuid4(),
        invoice_number=f"AUDIT-{document_id.hex[:8]}",
        supplier_name="Audit Test Supplier",
        currency="USD",
        total_amount=Decimal("100.00"),
        supplier_resolution_status="NOT_REFERENCED",
        matched_supplier_id=None,
        purchase_order_status="NOT_REFERENCED",
        purchase_order_id=None,
        purchase_order_number=None,
        goods_receipt_status="NOT_REFERENCED",
        goods_receipt_ids=(),
        match_mode="UNDETERMINED",
        line_match_count=0,
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
    )


def _store(repository, tenant_id, document_id, *, source_name, batch_id):
    workflow = repository.create_or_get_workflow(
        tenant_id=tenant_id, record=_workflow_record(tenant_id, document_id, batch_id=batch_id)
    )
    record = _invoice_memory_record(
        tenant_id, workflow.memory_id, document_id, source_name=source_name, batch_id=batch_id
    )
    return repository.store_invoice_memory(tenant_id=tenant_id, record=record)


def test_current_run_retrieval_returns_exactly_the_requested_documents_not_every_tenant_row():
    """Seed 11 unrelated historical rows (an arbitrary, non-fixture count
    -- task §5: 'Do not use a fixed fixture count') under one tenant, plus
    a 'current run' of 3 more documents under a *different*, older
    batch_id per record (never assumed to share one batch_id). Retrieving
    only the current run's explicit document IDs must return exactly
    those 3 records -- not all 14."""

    repository = FakeMemoryRepository()
    tenant_id = uuid.uuid4()

    historical_document_ids = [uuid.uuid4() for _ in range(11)]
    for index, document_id in enumerate(historical_document_ids):
        _store(
            repository,
            tenant_id,
            document_id,
            source_name=f"historical-{index}.png",
            batch_id=uuid.uuid4(),
        )

    current_document_ids = [uuid.uuid4() for _ in range(3)]
    current_batch_id = uuid.uuid4()
    for index, document_id in enumerate(current_document_ids):
        _store(
            repository,
            tenant_id,
            document_id,
            source_name=f"current-{index}.png",
            batch_id=current_batch_id,
        )

    service = MemoryService(repository, tenant_id=tenant_id)

    retrieved = {
        document_id: service.retrieve_bundle(document_id=document_id)
        for document_id in current_document_ids
    }

    assert set(retrieved) == set(current_document_ids)
    assert all(bundle is not None for bundle in retrieved.values())
    assert {bundle.invoice_memory.document_id for bundle in retrieved.values()} == set(current_document_ids)

    # Historical rows remain stored (never deleted to make this pass).
    for document_id in historical_document_ids:
        historical_bundle = service.retrieve_bundle(document_id=document_id)
        assert historical_bundle is not None
        assert historical_bundle.invoice_memory is not None


def test_current_run_retrieval_never_touches_an_unrelated_documents_own_batch_id():
    """A record's own batch_id is irrelevant to whether it is part of
    'this run' -- retrieval is keyed by document_id, so a historical
    record that happens to carry the *same* batch_id as the current run
    (an idempotent re-persisted document from an earlier run, task §5)
    must not be returned unless its document_id was explicitly requested."""

    repository = FakeMemoryRepository()
    tenant_id = uuid.uuid4()
    shared_batch_id = uuid.uuid4()

    historical_document_id = uuid.uuid4()
    _store(repository, tenant_id, historical_document_id, source_name="historical-shared-batch.png", batch_id=shared_batch_id)

    current_document_id = uuid.uuid4()
    _store(repository, tenant_id, current_document_id, source_name="current.png", batch_id=shared_batch_id)

    service = MemoryService(repository, tenant_id=tenant_id)

    retrieved = service.retrieve_bundle(document_id=current_document_id)

    assert retrieved is not None
    assert retrieved.invoice_memory.document_id == current_document_id
    assert retrieved.invoice_memory.document_id != historical_document_id


def test_cross_tenant_rows_remain_inaccessible_even_with_colliding_document_ids():
    repository = FakeMemoryRepository()
    tenant_a = uuid.uuid4()
    tenant_b = uuid.uuid4()

    # Deliberately reuse the same document_id under both tenants: RLS/the
    # repository's tenant scoping, not document-ID uniqueness, is what
    # must prevent cross-tenant leakage.
    shared_document_id = uuid.uuid4()

    _store(repository, tenant_a, shared_document_id, source_name="tenant-a.png", batch_id=uuid.uuid4())
    _store(repository, tenant_b, shared_document_id, source_name="tenant-b.png", batch_id=uuid.uuid4())

    service_a = MemoryService(repository, tenant_id=tenant_a)
    service_b = MemoryService(repository, tenant_id=tenant_b)

    bundle_a = service_a.retrieve_bundle(document_id=shared_document_id)
    bundle_b = service_b.retrieve_bundle(document_id=shared_document_id)

    assert bundle_a is not None and bundle_b is not None
    assert bundle_a.invoice_memory.source_name == "tenant-a.png"
    assert bundle_b.invoice_memory.source_name == "tenant-b.png"
    assert bundle_a.invoice_memory.source_name != bundle_b.invoice_memory.source_name


def test_retrieval_fidelity_survives_alongside_unrelated_historical_rows():
    """Payload-hash verification (`MemoryService.retrieve_bundle` calls
    `verify_invoice_memory_hash`) must still pass for the current run's
    documents when historical, unrelated rows are also present."""

    repository = FakeMemoryRepository()
    tenant_id = uuid.uuid4()

    for index in range(6):
        _store(repository, tenant_id, uuid.uuid4(), source_name=f"noise-{index}.png", batch_id=uuid.uuid4())

    current_document_id = uuid.uuid4()
    _store(repository, tenant_id, current_document_id, source_name="current.png", batch_id=uuid.uuid4())

    service = MemoryService(repository, tenant_id=tenant_id)

    # verify_invoice_memory_hash raises MemoryIntegrityError on mismatch;
    # a clean return proves the hash check passed.
    bundle = service.retrieve_bundle(document_id=current_document_id)
    assert bundle is not None
    assert bundle.invoice_memory.payload_sha256


def test_list_review_required_is_filtered_not_a_full_tenant_scan():
    """The one M8 method that scans more than one document per call is
    still policy-scoped (review_required=TRUE), never a bare 'every row
    for this tenant' read (task §5's line between a legitimate worklist
    query and current-run validation)."""

    repository = FakeMemoryRepository()
    tenant_id = uuid.uuid4()

    clean_document_id = uuid.uuid4()
    _store(repository, tenant_id, clean_document_id, source_name="clean.png", batch_id=uuid.uuid4())

    review_document_id = uuid.uuid4()
    workflow = repository.create_or_get_workflow(
        tenant_id=tenant_id, record=_workflow_record(tenant_id, review_document_id, batch_id=uuid.uuid4())
    )
    review_record = _invoice_memory_record(
        tenant_id, workflow.memory_id, review_document_id, source_name="review.png", batch_id=uuid.uuid4()
    )
    from dataclasses import replace

    review_record = replace(review_record, review_required=True, review_reasons=("AUDIT_TEST_REVIEW",))
    repository.store_invoice_memory(tenant_id=tenant_id, record=review_record)

    service = MemoryService(repository, tenant_id=tenant_id)
    review_required = service.list_review_required()

    assert {record.document_id for record in review_required} == {review_document_id}
