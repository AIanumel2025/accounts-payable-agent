"""Phase 7 memory service: Phase 4-6 bridge and persistence coordination.

Source: notebook cell 88 ("PHASE 7 — REPLACEMENT CELL 5")'s alignment
block (`normalization_by_document`/`matching_input_by_document`/
`matching_result_by_document`), deterministic-ID functions
(`create_workflow_id`, `create_memory_record_id`, `create_phase_reference_id`,
`create_memory_audit_id`, `create_review_id`), `build_matched_reference_snapshot`,
`build_invoice_memory_record`, and the top-level persist/retrieve driver
code (`persist_invoice_memory_records`, `retrieve_matched_invoice_memory`),
generalized per M8 task brief §6:

  - Every `create_*_id` function derived its identity from
    `PROTOTYPE_TENANT_ID`, a module-level constant built from a fixed
    string (`"ap-agent/tenant/prototype-sme"`). Task §3.3 requires no
    production dependence on that constant; every ID-creation function
    here takes `tenant_id` as an explicit parameter instead. This is not a
    hidden behaviour change: the golden integration test
    (`tests/integration/test_memory_golden.py`) passes the *same* derived
    `PROTOTYPE_TENANT_ID` value the notebook used, so it exercises the
    identical UUIDs already persisted to the real Neon database.
  - `normalization_results`/`matching_inputs`/`matching_results` are
    explicit parameters to `MemoryService.persist_batch`/`align_phase_results`
    instead of notebook globals (task §6: "Replace notebook globals with
    explicit typed inputs").
  - The notebook's alignment asserts (`assert len(normalization_by_document)
    == <fixture count>`, `assert set(...) == set(...) == set(...)`) are
    fixture-count-specific (violates D-2/D-3: "Runtime code must not
    assume there are exactly four documents"). `align_phase_results` below
    checks *consistency*
    (no missing/duplicate/cross-document/cross-batch/source-hash-discontinuous
    results) for any number of documents, raising `MemoryIntegrityError`
    instead of a bare `assert`.
  - `build_invoice_memory_record` returns the typed
    `ap_agent.models.memory.MatchedInvoiceMemoryRecord` dataclass instead
    of the notebook's untyped `dict`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Optional
from uuid import NAMESPACE_URL, UUID, uuid5

from ap_agent.db.connection import memory_utc_now
from ap_agent.exceptions import MemoryIntegrityError
from ap_agent.models.matching import MatchingInput, MatchingResult
from ap_agent.models.memory import (
    AuditMemoryEvent,
    InvoiceMemoryBundle,
    MatchedInvoiceMemoryRecord,
    MemoryActorType,
    MemoryEventType,
    MemoryWorkflowStage,
    MemoryWorkflowStatus,
    WorkflowMemoryRecord,
)
from ap_agent.models.normalization import NormalizationResult
from ap_agent.repositories.memory_repository import MemoryRepository
from ap_agent.serialization.memory_json import (
    canonical_payload_sha256,
    enum_text,
    memory_json_safe,
    normalized_field_value,
    optional_decimal,
    optional_text,
)

__all__ = [
    "AlignedPhaseResult",
    "create_workflow_id",
    "create_memory_record_id",
    "create_phase_reference_id",
    "create_memory_audit_id",
    "create_review_id",
    "align_phase_results",
    "build_matched_reference_snapshot",
    "build_invoice_memory_record",
    "MemoryService",
]


# ------------------------------------------------------------
# Deterministic identity (notebook cell 88, generalized: `tenant_id` is a
# parameter instead of `PROTOTYPE_TENANT_ID`)
# ------------------------------------------------------------


def create_workflow_id(tenant_id: UUID, document_id: UUID) -> UUID:
    return uuid5(NAMESPACE_URL, f"ap-agent/workflow/{tenant_id}/{document_id}")


def create_memory_record_id(tenant_id: UUID, matching_result_id: UUID) -> UUID:
    return uuid5(NAMESPACE_URL, f"ap-agent/invoice-memory/{tenant_id}/{matching_result_id}")


def create_phase_reference_id(tenant_id: UUID, matching_result_id: UUID) -> UUID:
    return uuid5(NAMESPACE_URL, f"ap-agent/phase-reference/{tenant_id}/{matching_result_id}")


def create_memory_audit_id(tenant_id: UUID, matching_result_id: UUID) -> UUID:
    return uuid5(NAMESPACE_URL, f"ap-agent/memory-audit/{tenant_id}/{matching_result_id}")


def create_review_id(tenant_id: UUID, matching_result_id: UUID) -> UUID:
    return uuid5(NAMESPACE_URL, f"ap-agent/review/{tenant_id}/{matching_result_id}")


# ------------------------------------------------------------
# Phase 4/5/6 alignment (generalized from cell 88's
# `normalization_by_document`/`matching_input_by_document`/
# `matching_result_by_document` + its identity asserts)
# ------------------------------------------------------------


@dataclass(frozen=True)
class AlignedPhaseResult:
    document_id: UUID
    normalization_result: NormalizationResult
    matching_input: MatchingInput
    matching_result: MatchingResult


def _index_by_document(results: tuple, collection_name: str) -> dict:
    index: dict = {}

    for result in results:
        document_id = result.document_id

        if document_id in index:
            raise MemoryIntegrityError(
                f"Duplicate document_id {document_id} in {collection_name}.",
                details={"document_id": str(document_id), "collection": collection_name},
            )

        index[document_id] = result

    return index


def align_phase_results(
    *,
    normalization_results: Iterable[NormalizationResult],
    matching_inputs: Iterable[MatchingInput],
    matching_results: Iterable[MatchingResult],
) -> tuple[AlignedPhaseResult, ...]:
    """Align Phase 4/5/6 results by `document_id`, rejecting (task §6.2):
    missing results, duplicate document IDs, cross-document results,
    cross-batch results, and source-hash discontinuity."""

    normalization_by_document = _index_by_document(
        tuple(normalization_results), "normalization_results"
    )
    matching_input_by_document = _index_by_document(tuple(matching_inputs), "matching_inputs")
    matching_result_by_document = _index_by_document(tuple(matching_results), "matching_results")

    document_ids = set(matching_result_by_document)

    missing_from_normalization = document_ids - set(normalization_by_document)
    missing_from_matching_input = document_ids - set(matching_input_by_document)
    extra_in_normalization = set(normalization_by_document) - document_ids
    extra_in_matching_input = set(matching_input_by_document) - document_ids

    if (
        missing_from_normalization
        or missing_from_matching_input
        or extra_in_normalization
        or extra_in_matching_input
    ):
        raise MemoryIntegrityError(
            "Phase 4/5/6 results are not aligned by document_id.",
            details={
                "missing_from_normalization": sorted(
                    str(document_id) for document_id in missing_from_normalization
                ),
                "missing_from_matching_input": sorted(
                    str(document_id) for document_id in missing_from_matching_input
                ),
                "extra_in_normalization": sorted(
                    str(document_id) for document_id in extra_in_normalization
                ),
                "extra_in_matching_input": sorted(
                    str(document_id) for document_id in extra_in_matching_input
                ),
            },
        )

    aligned = []

    for document_id in document_ids:
        normalization_result = normalization_by_document[document_id]
        matching_input = matching_input_by_document[document_id]
        matching_result = matching_result_by_document[document_id]

        financial_result = matching_input.financial_validation
        invoice_record = matching_input.normalized_invoice

        if not (
            normalization_result.document_id
            == matching_input.document_id
            == matching_result.document_id
            == financial_result.document_id
            == invoice_record.document_id
            == document_id
        ):
            raise MemoryIntegrityError(
                f"Cross-document result mismatch for {document_id}.",
                details={"document_id": str(document_id)},
            )

        if not (
            normalization_result.batch_id
            == matching_input.batch_id
            == matching_result.batch_id
            == financial_result.batch_id
        ):
            raise MemoryIntegrityError(
                f"Cross-batch result mismatch for {document_id}.",
                details={"document_id": str(document_id)},
            )

        if not (
            normalization_result.source_document_sha256
            == matching_result.source_document_sha256
            == financial_result.source_document_sha256
        ):
            raise MemoryIntegrityError(
                f"Source-hash discontinuity for {document_id}: "
                "normalization, financial-validation and matching results "
                "disagree on source_document_sha256.",
                details={"document_id": str(document_id)},
            )

        aligned.append(
            AlignedPhaseResult(
                document_id=document_id,
                normalization_result=normalization_result,
                matching_input=matching_input,
                matching_result=matching_result,
            )
        )

    return tuple(
        sorted(aligned, key=lambda entry: entry.matching_result.source_name)
    )


# ------------------------------------------------------------
# Matched-reference snapshot (notebook cell 88
# `build_matched_reference_snapshot`, verbatim)
# ------------------------------------------------------------


def build_matched_reference_snapshot(matching_input: MatchingInput, matching_result) -> dict:
    """Store only the supplier, purchase order and goods receipts actually
    selected by Phase 6 (task §6.5) — never infer a missing value (task
    §6.6)."""

    matched_supplier_id = matching_result.supplier_resolution.matched_supplier_id
    purchase_order_id = matching_result.purchase_order_resolution.purchase_order_id
    goods_receipt_ids = set(matching_result.goods_receipt_resolution.goods_receipt_ids)

    matched_supplier = next(
        (
            supplier
            for supplier in matching_input.reference_data.suppliers
            if supplier.supplier_id == matched_supplier_id
        ),
        None,
    )

    matched_purchase_order = next(
        (
            purchase_order
            for purchase_order in matching_input.reference_data.purchase_orders
            if purchase_order.purchase_order_id == purchase_order_id
        ),
        None,
    )

    matched_goods_receipts = [
        receipt
        for receipt in matching_input.reference_data.goods_receipts
        if receipt.goods_receipt_id in goods_receipt_ids
    ]

    return {
        "supplier": memory_json_safe(matched_supplier),
        "purchase_order": memory_json_safe(matched_purchase_order),
        "goods_receipts": memory_json_safe(matched_goods_receipts),
    }


# ------------------------------------------------------------
# Complete invoice-memory record construction (notebook cell 88
# `build_invoice_memory_record`, generalized to return a typed dataclass
# and take `tenant_id` explicitly)
# ------------------------------------------------------------


def build_invoice_memory_record(
    tenant_id: UUID,
    aligned: AlignedPhaseResult,
) -> MatchedInvoiceMemoryRecord:
    matching_result = aligned.matching_result
    normalization_result = aligned.normalization_result
    matching_input = aligned.matching_input
    financial_result = matching_input.financial_validation
    invoice_record = matching_input.normalized_invoice

    normalized_payload = memory_json_safe(normalization_result)
    financial_payload = memory_json_safe(financial_result)
    matching_payload = memory_json_safe(matching_result)
    reference_payload = build_matched_reference_snapshot(matching_input, matching_result)

    complete_payload = {
        "normalization_result": normalized_payload,
        "financial_validation_result": financial_payload,
        "matching_result": matching_payload,
        "matched_reference_data": reference_payload,
    }

    payload_sha256 = canonical_payload_sha256(complete_payload)

    invoice_number = normalized_field_value(invoice_record, "INVOICE_NUMBER")
    supplier_name = normalized_field_value(invoice_record, "SUPPLIER_NAME")
    currency = normalized_field_value(invoice_record, "CURRENCY")
    total_amount = normalized_field_value(invoice_record, "TOTAL_AMOUNT")

    review_reasons = [enum_text(reason) for reason in matching_result.review_reasons]

    if matching_result.review_required and not review_reasons:
        review_reasons = ["INHERITED_REVIEW_REQUIRED"]

    return MatchedInvoiceMemoryRecord(
        record_id=create_memory_record_id(tenant_id, matching_result.matching_result_id),
        tenant_id=tenant_id,
        workflow_memory_id=create_workflow_id(tenant_id, aligned.document_id),
        batch_id=matching_result.batch_id,
        document_id=aligned.document_id,
        matching_result_id=matching_result.matching_result_id,
        source_name=matching_result.source_name,
        source_document_sha256=matching_result.source_document_sha256,
        invoice_record_id=invoice_record.invoice_record_id,
        invoice_number=optional_text(invoice_number),
        supplier_name=optional_text(supplier_name),
        currency=optional_text(currency),
        total_amount=optional_decimal(total_amount),
        supplier_resolution_status=enum_text(matching_result.supplier_resolution.status),
        matched_supplier_id=optional_text(matching_result.supplier_resolution.matched_supplier_id),
        purchase_order_status=enum_text(matching_result.purchase_order_resolution.status),
        purchase_order_id=optional_text(matching_result.purchase_order_resolution.purchase_order_id),
        purchase_order_number=optional_text(
            matching_result.purchase_order_resolution.purchase_order_number
        ),
        goods_receipt_status=enum_text(matching_result.goods_receipt_resolution.status),
        goods_receipt_ids=tuple(
            str(receipt_id)
            for receipt_id in matching_result.goods_receipt_resolution.goods_receipt_ids
        ),
        match_mode=enum_text(matching_result.summary.match_mode),
        line_match_count=len(matching_result.line_matches),
        normalization_status=enum_text(normalization_result.status),
        financial_validation_status=enum_text(financial_result.status),
        matching_status=enum_text(matching_result.status),
        review_required=bool(matching_result.review_required),
        review_reasons=tuple(review_reasons),
        normalized_payload=normalized_payload,
        financial_payload=financial_payload,
        matching_payload=matching_payload,
        reference_payload=reference_payload,
        payload_sha256=payload_sha256,
    )


# ------------------------------------------------------------
# MemoryService: persistence coordination + retrieval/reconstruction
# ------------------------------------------------------------


class MemoryService:
    """Coordinates the Phase 4-6 -> PostgreSQL memory bridge for one
    tenant. Never writes SQL itself (delegates to `MemoryRepository`); never
    decides business outcomes (task §2: "The LLM must never write directly
    to PostgreSQL. Future LLM decisions must pass through deterministic
    validation and the memory service.") — this service is that
    deterministic validation layer, not a policy layer."""

    def __init__(self, repository: MemoryRepository, *, tenant_id: UUID) -> None:
        self._repository = repository
        self._tenant_id = tenant_id

    @property
    def tenant_id(self) -> UUID:
        return self._tenant_id

    def align(
        self,
        *,
        normalization_results: Iterable[NormalizationResult],
        matching_inputs: Iterable[MatchingInput],
        matching_results: Iterable[MatchingResult],
    ) -> tuple[AlignedPhaseResult, ...]:
        return align_phase_results(
            normalization_results=normalization_results,
            matching_inputs=matching_inputs,
            matching_results=matching_results,
        )

    def build_record(self, aligned: AlignedPhaseResult) -> MatchedInvoiceMemoryRecord:
        return build_invoice_memory_record(self._tenant_id, aligned)

    def _build_workflow_record(
        self,
        record: MatchedInvoiceMemoryRecord,
        aligned: AlignedPhaseResult,
    ) -> WorkflowMemoryRecord:
        now = memory_utc_now()

        return WorkflowMemoryRecord(
            memory_id=record.workflow_memory_id,
            tenant_id=str(self._tenant_id),
            batch_id=record.batch_id,
            document_id=record.document_id,
            source_name=record.source_name,
            source_document_sha256=record.source_document_sha256,
            current_stage=MemoryWorkflowStage.REFERENCE_MATCHING,
            current_status=MemoryWorkflowStatus(aligned.matching_result.status.value),
            review_required=record.review_required,
            review_reasons=record.review_reasons,
            latest_matching_result_id=record.matching_result_id,
            revision=1,
            created_at=now,
            updated_at=now,
        )

    def persist_document(self, aligned: AlignedPhaseResult) -> MatchedInvoiceMemoryRecord:
        """Single-invoice persistence operation (task §6.11)."""

        record = self.build_record(aligned)

        self._repository.create_or_get_workflow(
            tenant_id=self._tenant_id,
            record=self._build_workflow_record(record, aligned),
        )

        stored_record = self._repository.store_invoice_memory(
            tenant_id=self._tenant_id, record=record
        )

        self._link_phase_reference_and_audit(record, aligned)

        return stored_record

    def persist_batch(
        self,
        *,
        normalization_results: Iterable[NormalizationResult],
        matching_inputs: Iterable[MatchingInput],
        matching_results: Iterable[MatchingResult],
        batch_size: int = 25,
    ) -> tuple[MatchedInvoiceMemoryRecord, ...]:
        """Bounded multi-record persistence operation (task §6.11/§6.12):
        `batch_size` controls how many invoice-memory rows
        `MemoryRepository.store_invoice_memory_batch` writes per
        transaction. Workflow/phase-reference/audit bookkeeping is written
        per document, since each needs its own workflow to exist first."""

        aligned_results = self.align(
            normalization_results=normalization_results,
            matching_inputs=matching_inputs,
            matching_results=matching_results,
        )

        records = []

        for aligned in aligned_results:
            record = self.build_record(aligned)
            records.append((aligned, record))

            self._repository.create_or_get_workflow(
                tenant_id=self._tenant_id,
                record=self._build_workflow_record(record, aligned),
            )

        stored_records = self._repository.store_invoice_memory_batch(
            tenant_id=self._tenant_id,
            records=tuple(record for _, record in records),
            batch_size=batch_size,
        )

        for aligned, record in records:
            self._link_phase_reference_and_audit(record, aligned)

        return stored_records

    def _link_phase_reference_and_audit(
        self,
        record: MatchedInvoiceMemoryRecord,
        aligned: AlignedPhaseResult,
    ) -> None:
        self._repository.store_phase_result_reference(
            tenant_id=self._tenant_id,
            workflow_id=record.workflow_memory_id,
            phase_name="REFERENCE_MATCHING",
            phase_version=aligned.matching_result.matching_version,
            attempt_number=1,
            result_id=str(record.matching_result_id),
            result_status=record.matching_status,
            artifact_uri=f"postgresql://ap_agent/invoice_memory_records/{record.record_id}",
            artifact_sha256=record.payload_sha256,
            review_required=record.review_required,
            produced_at=aligned.matching_result.event.occurred_at,
            metadata={
                "source_name": record.source_name,
                "document_id": str(record.document_id),
            },
        )

        audit_event = AuditMemoryEvent(
            audit_event_id=create_memory_audit_id(
                self._tenant_id, record.matching_result_id
            ),
            tenant_id=str(self._tenant_id),
            batch_id=record.batch_id,
            document_id=record.document_id,
            event_type=MemoryEventType.MEMORY_CREATED,
            actor_type=MemoryActorType.SYSTEM,
            actor_id="phase-7-postgresql-memory",
            previous_stage=None,
            new_stage=MemoryWorkflowStage.MEMORY_PERSISTENCE,
            previous_status=None,
            new_status=MemoryWorkflowStatus(aligned.matching_result.status.value),
            correlation_id=record.matching_result_id,
            payload_json=_memory_created_payload_json(record),
            payload_sha256=record.payload_sha256,
            # Deterministic, not `memory_utc_now()`: the audit event's
            # identity (`audit_event_id`) is a pure function of
            # `matching_result_id`, so its content must be too, or a
            # repeated call over the *same* pipeline result would submit
            # different bytes under the same identity on every rerun and
            # never actually be idempotent (task §5: "Identical
            # deterministic writes must be idempotent.").
            occurred_at=aligned.matching_result.event.occurred_at,
        )

        self._repository.append_audit_event(
            tenant_id=self._tenant_id,
            workflow_id=record.workflow_memory_id,
            event=audit_event,
        )

        if record.review_required:
            self._repository.open_review_case(
                tenant_id=self._tenant_id,
                workflow_id=record.workflow_memory_id,
                review_id=create_review_id(self._tenant_id, record.matching_result_id),
                reason_codes=record.review_reasons,
                summary=f"Review required for {record.source_name}.",
            )

    def retrieve_bundle(self, *, document_id: UUID) -> Optional[InvoiceMemoryBundle]:
        bundle = self._repository.get_invoice_memory_bundle(
            tenant_id=self._tenant_id, document_id=document_id
        )

        if bundle is not None and bundle.invoice_memory is not None:
            verify_invoice_memory_hash(bundle.invoice_memory)

        return bundle

    def list_review_required(self) -> tuple[MatchedInvoiceMemoryRecord, ...]:
        records = self._repository.list_review_required_invoice_memory(
            tenant_id=self._tenant_id
        )

        for record in records:
            verify_invoice_memory_hash(record)

        return records


def _memory_created_payload_json(record: MatchedInvoiceMemoryRecord) -> str:
    from ap_agent.serialization.memory_json import canonical_json_bytes

    return canonical_json_bytes(
        {
            "memory_record_id": str(record.record_id),
            "payload_sha256": record.payload_sha256,
        }
    ).decode("utf-8")


def verify_invoice_memory_hash(record: MatchedInvoiceMemoryRecord) -> None:
    """Recompute the canonical payload hash from a retrieved record and
    compare it to the stored `payload_sha256` (task §6.9: "Verify hashes
    again after database retrieval."), mirroring the notebook's
    `retrieve_matched_invoice_memory` `recalculated_hash` check."""

    reconstructed_payload = {
        "normalization_result": record.normalized_payload,
        "financial_validation_result": record.financial_payload,
        "matching_result": record.matching_payload,
        "matched_reference_data": record.reference_payload,
    }

    recalculated_hash = canonical_payload_sha256(reconstructed_payload)

    if recalculated_hash != record.payload_sha256:
        raise MemoryIntegrityError(
            f"Database payload-integrity failure for {record.source_name}.",
            details={"document_id": str(record.document_id)},
        )
