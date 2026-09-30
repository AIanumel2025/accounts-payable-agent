"""Seed a *realistic* review-required workflow for M11D resume tests.

Builds a typed `NormalizationResult` for a synthetic invoice and then runs
the **real** Phase 5 (financial validation) and Phase 6 (reference matching)
tools and the real memory service over it, exactly as the pipeline would
after OCR -- so the stored payloads are genuine phase output that hydration,
overlay application and the resumed stages can round-trip. Only OCR and
normalization are replaced by a hand-built normalization result (they are
not part of any resume).

Test-only: fixtures and synthetic data never enter production code.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Optional

from ap_agent.adapters.reference_data_adapter import load_reference_data_bundle
from ap_agent.config.postgres import MemoryConfig
from ap_agent.config.settings import FinancialValidationConfig, MatchingConfig, NormalizationConfig
from ap_agent.db.connection import open_connection, set_tenant_context
from ap_agent.models.normalization import (
    EvidenceReference,
    EvidenceReferenceType,
    ExtractionMethod,
    InvoiceFieldName,
    NormalizationEvent,
    NormalizationResult,
    NormalizationStatus,
    NormalizedInvoiceField,
    NormalizedInvoiceRecord,
    NormalizedLineItem,
    NormalizedValueType,
)
from ap_agent.models.ocr import BoundingBox
from ap_agent.repositories.operations_repository import OperationsRepository
from ap_agent.repositories.postgres_memory_repository import PostgresMemoryRepository
from ap_agent.services.memory_service import MemoryService, align_phase_results
from ap_agent.tools.financial_validation import build_validation_input, process_financial_validation
from ap_agent.tools.matching import build_matching_input, process_invoice_matching
from ap_agent.tools.normalization import persist_normalization_result

REFERENCE_DATA_DIRECTORY = Path(__file__).resolve().parents[1] / "fixtures" / "reference_data"

# A clean invoice that matches SuperStore / PO CA-2012-AB10015140-40974.
CLEAN_HEADER: dict[InvoiceFieldName, tuple[Any, NormalizedValueType]] = {
    InvoiceFieldName.SUPPLIER_NAME: ("SuperStore", NormalizedValueType.TEXT),
    InvoiceFieldName.INVOICE_NUMBER: ("INV-1001", NormalizedValueType.TEXT),
    InvoiceFieldName.INVOICE_DATE: (date(2012, 3, 2), NormalizedValueType.DATE),
    InvoiceFieldName.PURCHASE_ORDER_NUMBER: ("CA-2012-AB10015140-40974", NormalizedValueType.TEXT),
    InvoiceFieldName.CURRENCY: ("USD", NormalizedValueType.CURRENCY_CODE),
    InvoiceFieldName.SUBTOTAL: (Decimal("48.71"), NormalizedValueType.DECIMAL),
    InvoiceFieldName.TAX_AMOUNT: (Decimal("0.00"), NormalizedValueType.DECIMAL),
    InvoiceFieldName.TOTAL_AMOUNT: (Decimal("48.71"), NormalizedValueType.DECIMAL),
}

CLEAN_LINE = {
    "description": "Global Push Button Manager's Chair, Indigo",
    "quantity": Decimal("1"),
    "unit_price": Decimal("48.71"),
    "amount": Decimal("48.71"),
}


def _evidence(seed: str, text: str, reading_order: int) -> EvidenceReference:
    return EvidenceReference(
        reference_id=uuid.uuid5(uuid.NAMESPACE_URL, f"m11d-seed-evidence/{seed}"),
        reference_type=EvidenceReferenceType.LINE,
        page_number=1,
        reading_order=reading_order,
        raw_text=text,
        confidence=99.0,
        bounding_box=BoundingBox(x=10, y=10 * reading_order, width=200, height=12),
    )


def _field(document_id, name: InvoiceFieldName, value: Any, value_type: NormalizedValueType, order: int, *, seed: str):
    return NormalizedInvoiceField(
        field_id=uuid.uuid5(uuid.NAMESPACE_URL, f"m11d-seed-field/{seed}/{name.value}"),
        field_name=name,
        raw_value=str(value),
        normalized_value=value,
        value_type=value_type,
        confidence=99.0,
        extraction_method=ExtractionMethod.LABEL_VALUE,
        evidence_references=(_evidence(f"{seed}/{name.value}", str(value), order),),
    )


def make_normalization_result(
    *,
    batch_id: uuid.UUID,
    document_id: uuid.UUID,
    source_sha256: str,
    source_name: str = "original.pdf",
    header: Optional[dict[InvoiceFieldName, tuple[Any, NormalizedValueType]]] = None,
    line: Optional[dict[str, Any]] = None,
    seed: str = "clean",
) -> NormalizationResult:
    header_values = dict(CLEAN_HEADER)
    header_values.update(header or {})
    line_values = dict(CLEAN_LINE)
    line_values.update(line or {})

    fields = tuple(
        _field(document_id, name, value, value_type, order, seed=seed)
        for order, (name, (value, value_type)) in enumerate(header_values.items(), start=1)
    )

    def line_field(name: InvoiceFieldName, value: Any, value_type: NormalizedValueType, order: int):
        return _field(document_id, name, value, value_type, 20 + order, seed=f"{seed}/line1")

    line_item = NormalizedLineItem(
        line_item_id=uuid.uuid5(uuid.NAMESPACE_URL, f"m11d-seed-line/{seed}/1"),
        line_number=1,
        description=line_field(InvoiceFieldName.LINE_DESCRIPTION, line_values["description"], NormalizedValueType.TEXT, 1),
        quantity=line_field(InvoiceFieldName.LINE_QUANTITY, line_values["quantity"], NormalizedValueType.DECIMAL, 2),
        unit_price=line_field(InvoiceFieldName.LINE_UNIT_PRICE, line_values["unit_price"], NormalizedValueType.DECIMAL, 3),
        amount=line_field(InvoiceFieldName.LINE_AMOUNT, line_values["amount"], NormalizedValueType.DECIMAL, 4),
        currency="USD",
        confidence=99.0,
        evidence_references=(_evidence(f"{seed}/line1", "line 1", 30),),
    )

    now = datetime.now(timezone.utc)

    record = NormalizedInvoiceRecord(
        invoice_record_id=uuid.uuid5(uuid.NAMESPACE_URL, f"m11d-seed-record/{seed}/{document_id}"),
        batch_id=batch_id,
        document_id=document_id,
        source_name=source_name,
        source_document_sha256=source_sha256,
        fields=fields,
        line_items=(line_item,),
        normalization_version="normalization-v1",
        created_at=now,
    )

    return NormalizationResult(
        batch_id=batch_id,
        document_id=document_id,
        source_document_sha256=source_sha256,
        ocr_version="ocr-v2-paddle",
        normalization_version="normalization-v1",
        status=NormalizationStatus.SUCCEEDED,
        invoice_record=record,
        field_candidates=tuple(),
        event=NormalizationEvent(
            event_type="NORMALIZATION", status=NormalizationStatus.SUCCEEDED, batch_id=batch_id,
            document_id=document_id, occurred_at=now, message="seeded", review_required=False,
        ),
    )


@dataclass(frozen=True)
class SeededWorkflow:
    tenant_id: uuid.UUID
    workflow_id: uuid.UUID
    batch_id: uuid.UUID
    document_id: uuid.UUID
    review_case_id: Optional[uuid.UUID]
    review_required: bool
    memory_record_id: uuid.UUID
    original_payload_sha256: str
    source_sha256: str


def seed_pipeline_workflow(
    dsn: str,
    config: MemoryConfig,
    *,
    tenant_id: uuid.UUID,
    work_directory: Path,
    header: Optional[dict[InvoiceFieldName, tuple[Any, NormalizedValueType]]] = None,
    line: Optional[dict[str, Any]] = None,
    source_sha256: Optional[str] = None,
    seed: Optional[str] = None,
) -> SeededWorkflow:
    seed = seed or uuid.uuid4().hex
    batch_id = uuid.uuid4()
    document_id = uuid.uuid5(uuid.NAMESPACE_URL, f"m11d-seed-document/{seed}")
    source_sha256 = source_sha256 or uuid.uuid5(uuid.NAMESPACE_URL, f"m11d-seed-source/{seed}").hex * 2

    normalization = make_normalization_result(
        batch_id=batch_id, document_id=document_id, source_sha256=source_sha256, header=header, line=line, seed=seed
    )

    normalization_config = NormalizationConfig(artifact_root=work_directory / "seed-phase4")
    persist_normalization_result(
        normalization_input=type("Input", (), {"batch_id": batch_id, "document_id": document_id})(),
        result=normalization,
        config=normalization_config,
    )

    financial = process_financial_validation(
        build_validation_input(normalization, normalization_config=normalization_config),
        config=FinancialValidationConfig(artifact_root=work_directory / "seed-phase5"),
    )
    reference_data = load_reference_data_bundle(REFERENCE_DATA_DIRECTORY)
    matching_input = build_matching_input(normalization, financial, reference_data)
    matching = process_invoice_matching(matching_input, config=MatchingConfig(artifact_root=work_directory / "seed-phase6"))

    (aligned,) = align_phase_results(
        normalization_results=(normalization,), matching_inputs=(matching_input,), matching_results=(matching,)
    )
    stored = MemoryService(PostgresMemoryRepository(dsn, config), tenant_id=tenant_id).persist_document(aligned)

    operations = OperationsRepository(dsn, config)

    with operations.transaction(tenant_id) as cursor:
        operations.finalize_workflow_state(
            cursor, tenant_id=tenant_id, workflow_id=stored.workflow_memory_id,
            review_required=bool(stored.review_required),
        )
        cursor.execute(
            "SELECT review_id FROM ap_agent.review_cases WHERE tenant_id = %s AND workflow_id = %s;",
            (tenant_id, stored.workflow_memory_id),
        )
        row = cursor.fetchone()

    return SeededWorkflow(
        tenant_id=tenant_id,
        workflow_id=stored.workflow_memory_id,
        batch_id=batch_id,
        document_id=document_id,
        review_case_id=None if row is None else row[0],
        review_required=bool(stored.review_required),
        memory_record_id=stored.record_id,
        original_payload_sha256=stored.payload_sha256,
        source_sha256=source_sha256,
    )
