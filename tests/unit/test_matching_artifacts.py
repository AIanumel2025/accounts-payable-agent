"""Unit tests for Phase 6 artifact persistence: the required artifact set,
manifest/SHA-256 integrity, idempotent reruns and the artifact-collision
fail-closed guard (M7 task §5.I/§7). Synthetic `MatchingResult` objects
only.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from decimal import Decimal
from uuid import uuid4

import pytest

from ap_agent.config.settings import MatchingConfig
from ap_agent.exceptions import MatchingIntegrityError
from ap_agent.models.matching import (
    GoodsReceiptResolution,
    InvoiceMatchSummary,
    MatchCheckStatus,
    MatchingEvent,
    MatchingResult,
    MatchingStatus,
    MatchMode,
    PurchaseOrderResolution,
    ReferenceDataBundle,
    ResolutionStatus,
    SupplierMatchMethod,
    SupplierRecord,
    SupplierResolution,
)
from ap_agent.tools.matching import (
    build_reference_snapshot,
    persist_matching_result,
    write_text_idempotently,
)

pytestmark = pytest.mark.unit

REQUIRED_ARTIFACTS = {
    "matching_result.json",
    "matching_event.json",
    "line_matches.jsonl",
    "reference_snapshot.json",
    "manifest.json",
}


def _matching_result(*, batch_id=None, document_id=None, supplier_id=None) -> MatchingResult:
    batch_id = batch_id or uuid4()
    document_id = document_id or uuid4()

    supplier_resolution = SupplierResolution(
        status=ResolutionStatus.MATCHED if supplier_id else ResolutionStatus.NOT_FOUND,
        matched_supplier_id=supplier_id,
        match_method=SupplierMatchMethod.EXACT_NAME if supplier_id else SupplierMatchMethod.NONE,
        candidate_supplier_ids=(supplier_id,) if supplier_id else (),
        score=Decimal("1.0000") if supplier_id else None,
        review_required=supplier_id is None,
        review_reasons=() if supplier_id else ("SUPPLIER_NAME_MISSING",),
    )

    purchase_order_resolution = PurchaseOrderResolution(
        status=ResolutionStatus.NOT_REFERENCED, purchase_order_id=None, purchase_order_number=None, review_required=False, review_reasons=()
    )

    goods_receipt_resolution = GoodsReceiptResolution(
        status=ResolutionStatus.NOT_REFERENCED, goods_receipt_ids=(), review_required=False, review_reasons=()
    )

    summary = InvoiceMatchSummary(
        match_mode=MatchMode.UNDETERMINED,
        supplier_status=MatchCheckStatus.PASSED if supplier_id else MatchCheckStatus.REVIEW_REQUIRED,
        purchase_order_status=MatchCheckStatus.NOT_APPLICABLE,
        goods_receipt_status=MatchCheckStatus.NOT_APPLICABLE,
        currency_status=MatchCheckStatus.NOT_APPLICABLE,
        line_items_status=MatchCheckStatus.NOT_APPLICABLE,
        invoice_total_status=MatchCheckStatus.NOT_APPLICABLE,
        expected_total=None,
        observed_total=None,
        total_variance=None,
        checks_performed=6,
        checks_passed=1 if supplier_id else 0,
        checks_failed=0,
        checks_requiring_review=0 if supplier_id else 1,
        checks_skipped=5,
        review_required=supplier_id is None,
        review_reasons=() if supplier_id else ("SUPPLIER_NAME_MISSING",),
    )

    event = MatchingEvent(
        event_type="REFERENCE_DATA_MATCHING",
        status=MatchingStatus.SUCCEEDED if supplier_id else MatchingStatus.REVIEW_REQUIRED,
        batch_id=batch_id,
        document_id=document_id,
        occurred_at=datetime.now(timezone.utc),
        message="synthetic",
        review_required=supplier_id is None,
    )

    return MatchingResult(
        matching_result_id=uuid4(),
        batch_id=batch_id,
        document_id=document_id,
        source_name="synthetic.pdf",
        source_document_sha256="a" * 64,
        matching_version="matching-v1",
        status=MatchingStatus.SUCCEEDED if supplier_id else MatchingStatus.REVIEW_REQUIRED,
        supplier_resolution=supplier_resolution,
        purchase_order_resolution=purchase_order_resolution,
        goods_receipt_resolution=goods_receipt_resolution,
        line_matches=(),
        summary=summary,
        event=event,
        review_required=supplier_id is None,
        review_reasons=() if supplier_id else ("SUPPLIER_NAME_MISSING",),
    )


@pytest.fixture
def config(tmp_path):
    return MatchingConfig(artifact_root=tmp_path)


def test_persist_matching_result_writes_the_required_artifact_set(config):
    result = _matching_result(supplier_id="SUP-001")
    reference_data = ReferenceDataBundle(
        suppliers=(SupplierRecord(supplier_id="SUP-001", legal_name="Acme Supplies"),), purchase_orders=(), goods_receipts=()
    )

    directory = persist_matching_result(result, reference_data, config=config)

    actual_artifacts = {path.name for path in directory.iterdir() if path.is_file()}
    assert actual_artifacts == REQUIRED_ARTIFACTS


def test_manifest_hashes_and_byte_counts_are_correct(config):
    result = _matching_result(supplier_id="SUP-001")
    reference_data = ReferenceDataBundle(
        suppliers=(SupplierRecord(supplier_id="SUP-001", legal_name="Acme Supplies"),), purchase_orders=(), goods_receipts=()
    )

    directory = persist_matching_result(result, reference_data, config=config)
    manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))

    assert manifest["matching_result_id"] == str(result.matching_result_id)
    assert manifest["document_id"] == str(result.document_id)
    assert manifest["source_document_sha256"] == result.source_document_sha256

    for filename, metadata in manifest["artifacts"].items():
        artifact_bytes = (directory / filename).read_bytes()
        assert hashlib.sha256(artifact_bytes).hexdigest() == metadata["sha256"]
        assert len(artifact_bytes) == metadata["bytes"]


def test_persisted_and_in_memory_status_agree(config):
    result = _matching_result(supplier_id=None)
    reference_data = ReferenceDataBundle(suppliers=(), purchase_orders=(), goods_receipts=())

    directory = persist_matching_result(result, reference_data, config=config)
    persisted_result = json.loads((directory / "matching_result.json").read_text(encoding="utf-8"))
    persisted_event = json.loads((directory / "matching_event.json").read_text(encoding="utf-8"))

    assert persisted_result["status"] == result.status.value
    assert persisted_result["review_required"] == result.review_required
    assert persisted_event["status"] == result.event.status.value
    assert persisted_event["review_required"] == result.event.review_required


def test_persistence_is_idempotent_for_identical_content(config):
    result = _matching_result(supplier_id="SUP-001")
    reference_data = ReferenceDataBundle(
        suppliers=(SupplierRecord(supplier_id="SUP-001", legal_name="Acme Supplies"),), purchase_orders=(), goods_receipts=()
    )

    first_directory = persist_matching_result(result, reference_data, config=config)
    second_directory = persist_matching_result(result, reference_data, config=config)

    assert first_directory == second_directory


def test_artifact_collision_fails_closed(tmp_path):
    destination = tmp_path / "matching_result.json"
    write_text_idempotently(destination, "first content\n")

    with pytest.raises(MatchingIntegrityError, match="PHASE_6_ARTIFACT_COLLISION"):
        write_text_idempotently(destination, "different content\n")


def test_reference_snapshot_only_contains_records_used_by_this_document():
    result = _matching_result(supplier_id="SUP-001")
    reference_data = ReferenceDataBundle(
        suppliers=(
            SupplierRecord(supplier_id="SUP-001", legal_name="Acme Supplies"),
            SupplierRecord(supplier_id="SUP-002", legal_name="Other Supplier"),
        ),
        purchase_orders=(),
        goods_receipts=(),
    )

    snapshot = build_reference_snapshot(result, reference_data)
    assert snapshot["supplier"].supplier_id == "SUP-001"
    assert snapshot["purchase_order"] is None
    assert snapshot["goods_receipts"] == ()


def test_different_documents_cannot_share_artifact_directories(config):
    first_result = _matching_result(supplier_id="SUP-001")
    second_result = _matching_result(supplier_id="SUP-001")
    reference_data = ReferenceDataBundle(
        suppliers=(SupplierRecord(supplier_id="SUP-001", legal_name="Acme Supplies"),), purchase_orders=(), goods_receipts=()
    )

    first_directory = persist_matching_result(first_result, reference_data, config=config)
    second_directory = persist_matching_result(second_result, reference_data, config=config)

    assert first_directory != second_directory
    assert first_result.matching_result_id != second_result.matching_result_id
