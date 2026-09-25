"""M8D unit tests: Phase 4/5/6 alignment, deterministic IDs, matched-
reference snapshots, payload hashing and collision detection.

Uses `tests.support.fake_memory_repository.FakeMemoryRepository` (a pure
Python double, no `psycopg`) so `MemoryService` is exercised without a
database, per the `MemoryRepository` Protocol boundary (task §2: "The
repository must not import from services or orchestration").
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal
from uuid import NAMESPACE_URL, uuid4, uuid5

import pytest

from ap_agent.exceptions import MemoryIntegrityError
from ap_agent.models.matching import (
    GoodsReceiptResolution,
    InvoiceMatchSummary,
    MatchCheckStatus,
    MatchingEvent,
    MatchingInput,
    MatchingResult,
    MatchingStatus,
    MatchMode,
    PurchaseOrderResolution,
    ReferenceDataBundle,
    ResolutionStatus,
    SupplierMatchMethod,
    SupplierResolution,
)
from ap_agent.models.normalization import (
    ExtractionMethod,
    InvoiceFieldName,
    NormalizationEvent,
    NormalizationResult,
    NormalizationStatus,
    NormalizedInvoiceField,
    NormalizedInvoiceRecord,
    NormalizedValueType,
)
from ap_agent.models.validation import (
    FinancialValidationResult,
    FinancialValidationSummary,
    ValidationEvent,
    ValidationStatus,
)
from ap_agent.serialization.memory_json import canonical_payload_sha256
from ap_agent.services.memory_service import (
    MemoryService,
    align_phase_results,
    build_invoice_memory_record,
    build_matched_reference_snapshot,
    create_memory_record_id,
    create_phase_reference_id,
    create_review_id,
    create_workflow_id,
)
from tests.support.fake_memory_repository import FakeMemoryRepository

pytestmark = pytest.mark.unit


def _field(field_name, value, value_type=NormalizedValueType.TEXT):
    return NormalizedInvoiceField(
        field_id=uuid4(),
        field_name=field_name,
        raw_value=str(value),
        normalized_value=value,
        value_type=value_type,
        confidence=95.0,
        extraction_method=ExtractionMethod.LABEL_VALUE,
        evidence_references=(),
    )


def _make_document(*, batch_id=None, document_id=None, source_name, sha256, total, review_required):
    batch_id = batch_id or uuid4()
    document_id = document_id or uuid4()
    now = datetime.now(timezone.utc)

    fields = (
        _field(InvoiceFieldName.INVOICE_NUMBER, "INV-1"),
        _field(InvoiceFieldName.SUPPLIER_NAME, "Acme"),
        _field(InvoiceFieldName.CURRENCY, "USD"),
    )

    if total is not None:
        fields = fields + (_field(InvoiceFieldName.TOTAL_AMOUNT, total, NormalizedValueType.DECIMAL),)

    invoice_record = NormalizedInvoiceRecord(
        invoice_record_id=uuid4(),
        batch_id=batch_id,
        document_id=document_id,
        source_name=source_name,
        source_document_sha256=sha256,
        fields=fields,
        line_items=(),
        normalization_version="normalization-v1",
        created_at=now,
    )

    status = NormalizationStatus.REVIEW_REQUIRED if review_required else NormalizationStatus.SUCCEEDED

    normalization_result = NormalizationResult(
        batch_id=batch_id,
        document_id=document_id,
        source_document_sha256=sha256,
        ocr_version="ocr-v1",
        normalization_version="normalization-v1",
        status=status,
        invoice_record=invoice_record,
        field_candidates=(),
        event=NormalizationEvent(
            event_type="NORMALIZATION",
            status=status,
            batch_id=batch_id,
            document_id=document_id,
            occurred_at=now,
            message="test",
            review_required=review_required,
        ),
    )

    financial_status = ValidationStatus.REVIEW_REQUIRED if review_required else ValidationStatus.SUCCEEDED

    financial_result = FinancialValidationResult(
        batch_id=batch_id,
        document_id=document_id,
        source_name=source_name,
        source_document_sha256=sha256,
        invoice_record_id=invoice_record.invoice_record_id,
        normalization_version="normalization-v1",
        validation_version="financial-validation-v1",
        status=financial_status,
        checks=(),
        summary=FinancialValidationSummary(
            total_checks=1,
            passed_checks=0 if review_required else 1,
            failed_checks=0,
            review_required_checks=1 if review_required else 0,
            not_applicable_checks=0,
            skipped_checks=0,
            review_required=review_required,
        ),
        event=ValidationEvent(
            event_type="FINANCIAL_VALIDATION",
            status=financial_status,
            batch_id=batch_id,
            document_id=document_id,
            occurred_at=now,
            message="test",
            review_required=review_required,
        ),
        review_required=review_required,
    )

    reference_data = ReferenceDataBundle(suppliers=(), purchase_orders=(), goods_receipts=())

    matching_input = MatchingInput(
        batch_id=batch_id,
        document_id=document_id,
        source_name=source_name,
        source_document_sha256=sha256,
        normalized_invoice=invoice_record,
        financial_validation=financial_result,
        reference_data=reference_data,
    )

    matching_status = MatchingStatus.REVIEW_REQUIRED if review_required else MatchingStatus.SUCCEEDED

    matching_result = MatchingResult(
        matching_result_id=uuid4(),
        batch_id=batch_id,
        document_id=document_id,
        source_name=source_name,
        source_document_sha256=sha256,
        matching_version="matching-v1",
        status=matching_status,
        supplier_resolution=SupplierResolution(
            status=ResolutionStatus.NOT_REFERENCED,
            matched_supplier_id=None,
            match_method=SupplierMatchMethod.NONE,
            candidate_supplier_ids=(),
            score=None,
            review_required=False,
            review_reasons=(),
        ),
        purchase_order_resolution=PurchaseOrderResolution(
            status=ResolutionStatus.NOT_REFERENCED,
            purchase_order_id=None,
            purchase_order_number=None,
            review_required=False,
            review_reasons=(),
        ),
        goods_receipt_resolution=GoodsReceiptResolution(
            status=ResolutionStatus.NOT_REFERENCED,
            goods_receipt_ids=(),
            review_required=False,
            review_reasons=(),
        ),
        line_matches=(),
        summary=InvoiceMatchSummary(
            match_mode=MatchMode.UNDETERMINED,
            supplier_status=MatchCheckStatus.NOT_APPLICABLE,
            purchase_order_status=MatchCheckStatus.NOT_APPLICABLE,
            goods_receipt_status=MatchCheckStatus.NOT_APPLICABLE,
            currency_status=MatchCheckStatus.NOT_APPLICABLE,
            line_items_status=MatchCheckStatus.NOT_APPLICABLE,
            invoice_total_status=MatchCheckStatus.NOT_APPLICABLE,
            expected_total=None,
            observed_total=None,
            total_variance=None,
            checks_performed=0,
            checks_passed=0,
            checks_failed=0,
            checks_requiring_review=0,
            checks_skipped=0,
            review_required=review_required,
            review_reasons=(),
        ),
        event=MatchingEvent(
            event_type="REFERENCE_MATCHING",
            status=matching_status,
            batch_id=batch_id,
            document_id=document_id,
            occurred_at=now,
            message="test",
            review_required=review_required,
        ),
        review_required=review_required,
        review_reasons=("SOME_REASON",) if review_required else (),
    )

    return normalization_result, matching_input, matching_result


def _two_documents():
    doc_a = _make_document(
        source_name="a.png", sha256="a" * 64, total=Decimal("10.00"), review_required=False
    )
    doc_b = _make_document(
        source_name="b.png", sha256="b" * 64, total=None, review_required=True
    )
    return doc_a, doc_b


# -- align_phase_results ------------------------------------------------


def test_align_phase_results_works_for_any_number_of_documents_not_just_four():
    """Task D-2/D-3: no assumption of exactly four documents."""

    for count in (1, 2, 5):
        documents = [
            _make_document(
                source_name=f"doc-{i}.png", sha256=f"{i}" * 64, total=Decimal("1.00"), review_required=False
            )
            for i in range(count)
        ]

        normalization_results = [d[0] for d in documents]
        matching_inputs = [d[1] for d in documents]
        matching_results = [d[2] for d in documents]

        aligned = align_phase_results(
            normalization_results=normalization_results,
            matching_inputs=matching_inputs,
            matching_results=matching_results,
        )

        assert len(aligned) == count


def test_align_phase_results_rejects_missing_result():
    doc_a, doc_b = _two_documents()

    with pytest.raises(MemoryIntegrityError):
        align_phase_results(
            normalization_results=[doc_a[0]],  # doc_b's normalization missing
            matching_inputs=[doc_a[1], doc_b[1]],
            matching_results=[doc_a[2], doc_b[2]],
        )


def test_align_phase_results_rejects_duplicate_document_id():
    doc_a, _ = _two_documents()

    with pytest.raises(MemoryIntegrityError):
        align_phase_results(
            normalization_results=[doc_a[0], doc_a[0]],
            matching_inputs=[doc_a[1]],
            matching_results=[doc_a[2]],
        )


def test_align_phase_results_rejects_cross_batch_mismatch():
    doc_a, _ = _two_documents()
    normalization_result, matching_input, matching_result = doc_a

    tampered_normalization = replace(normalization_result, batch_id=uuid4())

    with pytest.raises(MemoryIntegrityError, match="Cross-batch"):
        align_phase_results(
            normalization_results=[tampered_normalization],
            matching_inputs=[matching_input],
            matching_results=[matching_result],
        )


def test_align_phase_results_rejects_source_hash_discontinuity():
    doc_a, _ = _two_documents()
    normalization_result, matching_input, matching_result = doc_a

    tampered_normalization = replace(normalization_result, source_document_sha256="f" * 64)

    with pytest.raises(MemoryIntegrityError, match="Source-hash discontinuity"):
        align_phase_results(
            normalization_results=[tampered_normalization],
            matching_inputs=[matching_input],
            matching_results=[matching_result],
        )


# -- deterministic identity ----------------------------------------------


def test_deterministic_ids_are_stable_across_repeated_calls():
    tenant_id = uuid4()
    document_id = uuid4()
    matching_result_id = uuid4()

    assert create_workflow_id(tenant_id, document_id) == create_workflow_id(tenant_id, document_id)
    assert create_memory_record_id(tenant_id, matching_result_id) == create_memory_record_id(
        tenant_id, matching_result_id
    )


def test_deterministic_ids_are_timestamp_independent():
    """The ID-creation functions take no time-related argument at all, so
    two calls separated by real wall-clock time still agree."""

    import time

    tenant_id = uuid4()
    document_id = uuid4()

    first = create_workflow_id(tenant_id, document_id)
    time.sleep(0.01)
    second = create_workflow_id(tenant_id, document_id)

    assert first == second


def test_deterministic_ids_differ_across_tenants():
    document_id = uuid4()

    assert create_workflow_id(uuid4(), document_id) != create_workflow_id(uuid4(), document_id)


def test_deterministic_ids_do_not_depend_on_a_shared_mutable_module_global():
    """No production dependence on a prototype tenant constant (task
    §3.3): calling the ID functions twice with fresh arguments must not be
    influenced by any prior call's arguments."""

    tenant_1, document_1 = uuid4(), uuid4()
    tenant_2, document_2 = uuid4(), uuid4()

    create_workflow_id(tenant_1, document_1)
    result = create_workflow_id(tenant_2, document_2)

    assert result == create_workflow_id(tenant_2, document_2)


# -- build_invoice_memory_record / hashing --------------------------------


def test_build_invoice_memory_record_never_converts_decimal_through_float():
    doc_a, _ = _two_documents()
    normalization_result, matching_input, matching_result = doc_a

    from ap_agent.services.memory_service import AlignedPhaseResult

    aligned = AlignedPhaseResult(
        document_id=matching_result.document_id,
        normalization_result=normalization_result,
        matching_input=matching_input,
        matching_result=matching_result,
    )

    record = build_invoice_memory_record(uuid4(), aligned)

    assert record.total_amount == Decimal("10.00")
    assert isinstance(record.total_amount, Decimal)


def test_build_invoice_memory_record_preserves_missing_total_as_none():
    _, doc_b = _two_documents()
    normalization_result, matching_input, matching_result = doc_b

    from ap_agent.services.memory_service import AlignedPhaseResult

    aligned = AlignedPhaseResult(
        document_id=matching_result.document_id,
        normalization_result=normalization_result,
        matching_input=matching_input,
        matching_result=matching_result,
    )

    record = build_invoice_memory_record(uuid4(), aligned)

    assert record.total_amount is None


def test_build_matched_reference_snapshot_stores_only_actually_selected_records():
    from ap_agent.models.matching import PurchaseOrderRecord, SupplierRecord

    supplier = SupplierRecord(supplier_id="SUP-001", legal_name="Acme")
    other_supplier = SupplierRecord(supplier_id="SUP-002", legal_name="Other Co")

    doc_a, _ = _two_documents()
    _normalization_result, matching_input, matching_result = doc_a

    reference_data = ReferenceDataBundle(
        suppliers=(supplier, other_supplier), purchase_orders=(), goods_receipts=()
    )
    matching_input = replace(matching_input, reference_data=reference_data)
    matching_result = replace(
        matching_result,
        supplier_resolution=replace(
            matching_result.supplier_resolution, matched_supplier_id="SUP-001"
        ),
    )

    snapshot = build_matched_reference_snapshot(matching_input, matching_result)

    assert snapshot["supplier"]["supplier_id"] == "SUP-001"
    assert snapshot["purchase_order"] is None
    assert snapshot["goods_receipts"] == []


def test_payload_hash_is_deterministic_given_the_same_pipeline_result():
    doc_a, _ = _two_documents()
    normalization_result, matching_input, matching_result = doc_a

    from ap_agent.services.memory_service import AlignedPhaseResult

    aligned = AlignedPhaseResult(
        document_id=matching_result.document_id,
        normalization_result=normalization_result,
        matching_input=matching_input,
        matching_result=matching_result,
    )

    tenant_id = uuid4()
    record_1 = build_invoice_memory_record(tenant_id, aligned)
    record_2 = build_invoice_memory_record(tenant_id, aligned)

    assert record_1.payload_sha256 == record_2.payload_sha256
    assert record_1.record_id == record_2.record_id


# -- MemoryService: persistence + collision detection ----------------------


def test_memory_service_persist_document_and_retrieve_bundle():
    doc_a, _ = _two_documents()
    normalization_result, matching_input, matching_result = doc_a

    tenant_id = uuid4()
    repository = FakeMemoryRepository()
    service = MemoryService(repository, tenant_id=tenant_id)

    aligned = service.align(
        normalization_results=[normalization_result],
        matching_inputs=[matching_input],
        matching_results=[matching_result],
    )[0]

    stored = service.persist_document(aligned)

    bundle = service.retrieve_bundle(document_id=matching_result.document_id)

    assert bundle is not None
    assert bundle.invoice_memory.record_id == stored.record_id
    assert bundle.invoice_memory.payload_sha256 == stored.payload_sha256


def test_memory_service_persist_document_is_idempotent():
    doc_a, _ = _two_documents()
    normalization_result, matching_input, matching_result = doc_a

    service = MemoryService(FakeMemoryRepository(), tenant_id=uuid4())

    aligned = service.align(
        normalization_results=[normalization_result],
        matching_inputs=[matching_input],
        matching_results=[matching_result],
    )[0]

    first = service.persist_document(aligned)
    second = service.persist_document(aligned)

    assert first.record_id == second.record_id
    assert first.payload_sha256 == second.payload_sha256


def test_memory_service_rejects_conflicting_write_under_same_identity():
    """task §5: 'Conflicting content under the same deterministic identity
    must raise a typed integrity exception.'"""

    doc_a, _ = _two_documents()
    normalization_result, matching_input, matching_result = doc_a

    tenant_id = uuid4()
    repository = FakeMemoryRepository()
    service = MemoryService(repository, tenant_id=tenant_id)

    aligned = service.align(
        normalization_results=[normalization_result],
        matching_inputs=[matching_input],
        matching_results=[matching_result],
    )[0]

    service.persist_document(aligned)

    record = build_invoice_memory_record(tenant_id, aligned)
    tampered = replace(
        record,
        normalized_payload={"tampered": True},
        payload_sha256=canonical_payload_sha256({"tampered": True}),
    )

    with pytest.raises(MemoryIntegrityError):
        repository.store_invoice_memory(tenant_id=tenant_id, record=tampered)


def test_memory_service_cross_tenant_isolation():
    doc_a, _ = _two_documents()
    normalization_result, matching_input, matching_result = doc_a

    repository = FakeMemoryRepository()
    tenant_a, tenant_b = uuid4(), uuid4()

    service_a = MemoryService(repository, tenant_id=tenant_a)
    service_b = MemoryService(repository, tenant_id=tenant_b)

    aligned = service_a.align(
        normalization_results=[normalization_result],
        matching_inputs=[matching_input],
        matching_results=[matching_result],
    )[0]

    service_a.persist_document(aligned)

    assert service_b.retrieve_bundle(document_id=matching_result.document_id) is None
    assert service_a.retrieve_bundle(document_id=matching_result.document_id) is not None


def test_memory_service_persist_batch_supports_arbitrary_batch_size():
    documents = [
        _make_document(
            source_name=f"doc-{i}.png", sha256=f"{i}" * 64, total=Decimal("1.00"), review_required=False
        )
        for i in range(7)
    ]

    service = MemoryService(FakeMemoryRepository(), tenant_id=uuid4())

    stored = service.persist_batch(
        normalization_results=[d[0] for d in documents],
        matching_inputs=[d[1] for d in documents],
        matching_results=[d[2] for d in documents],
        batch_size=3,
    )

    assert len(stored) == 7
