"""Fast (non-`requires_paddle`) Phase 6 golden-baseline test.

Reconstructs the four controlled fixtures' exact, already-validated Phase 4
(`tests/golden/phase_4_expected_results.json`) and Phase 5
(`tests/golden/phase_5_expected_results.json`) outputs *synthetically* --
i.e. without running OCR -- and feeds them through the real Phase 6
matching functions against the real `tests/fixtures/reference_data/`
bundle. This lets every push verify the Phase 6 matching logic itself
against `tests/golden/phase_6_expected_results.json`, independent of
whether a real PaddleOCR engine is available in this environment (that
full, OCR-inclusive parity check is
`tests/integration/test_phase_1_to_6_pipeline.py`'s `requires_paddle`
test).
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest

from ap_agent.adapters.reference_data_adapter import load_reference_data_bundle
from ap_agent.config.settings import MatchingConfig
from ap_agent.models.matching import MatchingInput, MatchingStatus, MatchMode, ResolutionStatus
from ap_agent.models.normalization import (
    ExtractionMethod,
    InvoiceFieldName,
    NormalizedInvoiceField,
    NormalizedInvoiceRecord,
    NormalizedLineItem,
    NormalizedValueType,
)
from ap_agent.models.validation import (
    FinancialValidationResult,
    FinancialValidationSummary,
    ValidationEvent,
    ValidationStatus,
)
from ap_agent.tools.matching import persist_matching_result, process_invoice_matching

pytestmark = pytest.mark.unit

GOLDEN_PATH = Path(__file__).resolve().parents[1] / "golden" / "phase_6_expected_results.json"
REFERENCE_DATA_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "reference_data"


def _field(field_name, value, value_type=NormalizedValueType.TEXT) -> NormalizedInvoiceField:
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


def _line(line_number, description, quantity, unit_price, amount) -> NormalizedLineItem:
    return NormalizedLineItem(
        line_item_id=uuid4(),
        line_number=line_number,
        description=_field(InvoiceFieldName.LINE_DESCRIPTION, description) if description is not None else None,
        quantity=(
            _field(InvoiceFieldName.LINE_QUANTITY, quantity, NormalizedValueType.DECIMAL)
            if quantity is not None
            else None
        ),
        unit_price=(
            _field(InvoiceFieldName.LINE_UNIT_PRICE, unit_price, NormalizedValueType.DECIMAL)
            if unit_price is not None
            else None
        ),
        amount=(
            _field(InvoiceFieldName.LINE_AMOUNT, amount, NormalizedValueType.DECIMAL) if amount is not None else None
        ),
        currency=None,
        confidence=95.0,
        evidence_references=(),
    )


def _invoice_record(*, batch_id, document_id, source_name, sha256, header_fields, lines) -> NormalizedInvoiceRecord:
    fields = tuple(
        _field(
            field_name,
            value,
            NormalizedValueType.DECIMAL if isinstance(value, Decimal) else NormalizedValueType.TEXT,
        )
        for field_name, value in header_fields.items()
        if value is not None
    )

    return NormalizedInvoiceRecord(
        invoice_record_id=uuid4(),
        batch_id=batch_id,
        document_id=document_id,
        source_name=source_name,
        source_document_sha256=sha256,
        fields=fields,
        line_items=tuple(lines),
        normalization_version="normalization-v1",
        created_at=datetime.now(timezone.utc),
    )


def _financial_validation_result(
    *, batch_id, document_id, source_name, sha256, invoice_record_id, review_required
) -> FinancialValidationResult:
    status = ValidationStatus.REVIEW_REQUIRED if review_required else ValidationStatus.SUCCEEDED

    summary = FinancialValidationSummary(
        total_checks=1,
        passed_checks=0 if review_required else 1,
        failed_checks=0,
        review_required_checks=1 if review_required else 0,
        not_applicable_checks=0,
        skipped_checks=0,
        review_required=review_required,
    )

    event = ValidationEvent(
        event_type="FINANCIAL_VALIDATION",
        status=status,
        batch_id=batch_id,
        document_id=document_id,
        occurred_at=datetime.now(timezone.utc),
        message="synthetic golden-baseline fixture",
        review_required=review_required,
    )

    return FinancialValidationResult(
        batch_id=batch_id,
        document_id=document_id,
        source_name=source_name,
        source_document_sha256=sha256,
        invoice_record_id=invoice_record_id,
        normalization_version="normalization-v1",
        validation_version="financial-validation-v1",
        status=status,
        checks=(),
        summary=summary,
        event=event,
        review_required=review_required,
    )


# Per-document Phase 4/5 outputs, taken verbatim from
# tests/golden/phase_4_expected_results.json and
# tests/golden/phase_5_expected_results.json.
_DOCUMENTS = {
    "Template1_Instance90.jpg": {
        "sha256": "1" * 64,
        "header_fields": {
            InvoiceFieldName.CURRENCY: "EUR",
            InvoiceFieldName.PURCHASE_ORDER_NUMBER: "99",
            InvoiceFieldName.SUBTOTAL: Decimal("858.86"),
        },
        "lines": [
            _line(1, "Sit sit together.", Decimal("3.00"), Decimal("50.47"), None),
            _line(2, "Maybe religious several.", Decimal("2.00"), Decimal("7.15"), None),
            _line(3, "Green military listen.", Decimal("6.00"), Decimal("34.69"), None),
            _line(4, "Course eight.", Decimal("2.00"), Decimal("36.33"), None),
            _line(5, "Cost number world.", Decimal("5.00"), Decimal("82.47"), None),
        ],
        "financial_review_required": True,
    },
    "08181_flat_document.png": {
        "sha256": "2" * 64,
        "header_fields": {
            InvoiceFieldName.SUPPLIER_NAME: "Snyder, Hammond and Anderson",
            InvoiceFieldName.CURRENCY: "USD",
            InvoiceFieldName.SUBTOTAL: Decimal("63.45"),
        },
        "lines": [
            _line(1, "Set 12 Colour Pencils Spaceboy", Decimal("10"), Decimal("0.65"), Decimal("6.50")),
            _line(2, "Red Retrospot Charlotte Bag", Decimal("4"), Decimal("0.85"), Decimal("3.40")),
        ],
        "financial_review_required": False,
    },
    "invoice_Aaron Bergman_36258.pdf": {
        "sha256": "3" * 64,
        "header_fields": {
            InvoiceFieldName.PURCHASE_ORDER_NUMBER: "CA-2012-AB10015140-40974",
            InvoiceFieldName.CURRENCY: "USD",
            InvoiceFieldName.SUBTOTAL: Decimal("48.71"),
        },
        "lines": [
            _line(1, "Global Push Button Manager's Chair, Indigo", Decimal("1"), Decimal("48.71"), Decimal("48.71")),
        ],
        "financial_review_required": True,
    },
    "08181_warped_document_perspective_shadow.jpg": {
        "sha256": "4" * 64,
        "header_fields": {
            InvoiceFieldName.SUPPLIER_NAME: "Snyder, Hammond and Anderson",
            InvoiceFieldName.SUBTOTAL: Decimal("63.45"),
        },
        "lines": [],
        "financial_review_required": True,
    },
}


def _build_matching_results(tmp_path):
    config = MatchingConfig(artifact_root=tmp_path)
    reference_data = load_reference_data_bundle(REFERENCE_DATA_DIR)
    batch_id = uuid4()

    results = {}

    for filename, spec in _DOCUMENTS.items():
        document_id = uuid4()

        invoice_record = _invoice_record(
            batch_id=batch_id,
            document_id=document_id,
            source_name=filename,
            sha256=spec["sha256"],
            header_fields=spec["header_fields"],
            lines=spec["lines"],
        )

        financial_validation_result = _financial_validation_result(
            batch_id=batch_id,
            document_id=document_id,
            source_name=filename,
            sha256=spec["sha256"],
            invoice_record_id=invoice_record.invoice_record_id,
            review_required=spec["financial_review_required"],
        )

        matching_input = MatchingInput(
            batch_id=batch_id,
            document_id=document_id,
            source_name=filename,
            source_document_sha256=spec["sha256"],
            normalized_invoice=invoice_record,
            financial_validation=financial_validation_result,
            reference_data=reference_data,
        )

        results[filename] = process_invoice_matching(matching_input, config=config)

    return results, reference_data, config


@pytest.fixture(scope="module")
def golden():
    return json.loads(GOLDEN_PATH.read_text(encoding="utf-8"))


def test_matching_results_match_the_golden_baseline(tmp_path, golden):
    results, _reference_data, _config = _build_matching_results(tmp_path)

    for expected in golden["documents"]:
        result = results[expected["filename"]]
        summary = result.summary

        assert result.status.value == expected["status"]
        assert result.supplier_resolution.status.value == expected["supplier"]
        assert result.supplier_resolution.matched_supplier_id == expected["supplier_id"]
        assert result.purchase_order_resolution.status.value == expected["purchase_order"]
        assert result.purchase_order_resolution.purchase_order_number == expected["po_number"]
        assert result.goods_receipt_resolution.status.value == expected["goods_receipt"]
        assert summary.match_mode.value == expected["match_mode"]
        assert len(result.line_matches) == expected["line_matches"]
        assert summary.checks_passed == expected["checks_passed"]
        assert summary.checks_failed == expected["checks_failed"]
        assert summary.checks_requiring_review == expected["review_checks"]
        assert summary.checks_skipped == expected["checks_skipped"]
        assert summary.checks_performed == golden["aggregate_expected"]["checks_performed_per_document"]

        for reason in expected["required_reasons"]:
            assert reason in result.review_reasons

        if expected["expected_total"] is not None:
            assert summary.expected_total == Decimal(expected["expected_total"])
            assert summary.observed_total == Decimal(expected["observed_total"])
            assert summary.total_variance == Decimal(expected["total_variance"])
        else:
            # No matched PO (flat/warped): the PO-total check is
            # NOT_APPLICABLE and no total is ever computed or inferred.
            assert summary.expected_total is None
            assert summary.observed_total is None
            assert summary.total_variance is None
            assert summary.invoice_total_status.value == "NOT_APPLICABLE"

        assert len(result.goods_receipt_resolution.goods_receipt_ids) == expected["goods_receipt_ids_count"]

    aggregate = golden["aggregate_expected"]
    assert len(results) == aggregate["invoices_processed"]
    assert sum(r.status == MatchingStatus.SUCCEEDED for r in results.values()) == aggregate["successful_invoices"]
    assert (
        sum(r.status == MatchingStatus.REVIEW_REQUIRED for r in results.values())
        == aggregate["review_required_invoices"]
    )
    assert sum(r.status == MatchingStatus.FAILED for r in results.values()) == aggregate["failed_production_invoices"]
    assert (
        sum(r.summary.match_mode == MatchMode.THREE_WAY for r in results.values()) == aggregate["three_way_matches"]
    )
    assert sum(len(r.line_matches) for r in results.values()) == aggregate["line_matches_total"]


def test_supplier_is_never_inferred_from_a_matched_purchase_order(tmp_path):
    """Non-inference safeguard (task §3/§7): Template1 and Aaron both match
    a PO whose owning supplier is known (SUP-002/SUP-003), but neither
    invoice states a supplier name, so the supplier must stay unresolved."""

    results, _reference_data, _config = _build_matching_results(tmp_path)

    for filename in ("Template1_Instance90.jpg", "invoice_Aaron Bergman_36258.pdf"):
        result = results[filename]
        assert result.purchase_order_resolution.status == ResolutionStatus.MATCHED
        assert result.supplier_resolution.status == ResolutionStatus.NOT_FOUND
        assert result.supplier_resolution.matched_supplier_id is None


def test_missing_invoice_line_totals_remain_missing(tmp_path):
    results, _reference_data, _config = _build_matching_results(tmp_path)
    template_result = results["Template1_Instance90.jpg"]

    assert all(line_match.observed_line_total is None for line_match in template_result.line_matches)
    assert all(
        line_match.line_total_status.value == "REVIEW_REQUIRED" for line_match in template_result.line_matches
    )


def test_reference_snapshots_do_not_leak_records_from_other_documents(tmp_path, golden):
    results, reference_data, config = _build_matching_results(tmp_path)
    expected_snapshots = golden["reference_snapshot_expected"]

    for filename, result in results.items():
        directory = persist_matching_result(result, reference_data, config=config)
        snapshot = json.loads((directory / "reference_snapshot.json").read_text(encoding="utf-8"))
        expected = expected_snapshots[filename]

        actual_supplier_id = snapshot["supplier"]["supplier_id"] if snapshot["supplier"] is not None else None
        actual_po_number = (
            snapshot["purchase_order"]["purchase_order_number"] if snapshot["purchase_order"] is not None else None
        )

        assert actual_supplier_id == expected["supplier_id"]
        assert actual_po_number == expected["po_number"]
        assert len(snapshot["goods_receipts"]) == expected["receipt_count"]


def test_persistence_is_idempotent_and_deterministic_ids_reproduce(tmp_path):
    results, reference_data, config = _build_matching_results(tmp_path)

    for filename, result in results.items():
        first_directory = persist_matching_result(result, reference_data, config=config)
        second_directory = persist_matching_result(result, reference_data, config=config)
        assert first_directory == second_directory

    # A clean rerun over identical MatchingInput objects reproduces the same
    # matching_result_id and line_match_id values.
    second_results, _reference_data2, _config2 = _build_matching_results(tmp_path)
    # New batch/document UUIDs are generated per call, so IDs will legitimately
    # differ between _build_matching_results() invocations; instead, rerun
    # process_invoice_matching directly on the same MatchingInput.
    from ap_agent.tools.matching import process_invoice_matching as _process

    for filename, spec in _DOCUMENTS.items():
        result = results[filename]

        matching_input = MatchingInput(
            batch_id=result.batch_id,
            document_id=result.document_id,
            source_name=filename,
            source_document_sha256=spec["sha256"],
            normalized_invoice=_invoice_record(
                batch_id=result.batch_id,
                document_id=result.document_id,
                source_name=filename,
                sha256=spec["sha256"],
                header_fields=spec["header_fields"],
                lines=spec["lines"],
            ),
            financial_validation=_financial_validation_result(
                batch_id=result.batch_id,
                document_id=result.document_id,
                source_name=filename,
                sha256=spec["sha256"],
                invoice_record_id=uuid4(),
                review_required=spec["financial_review_required"],
            ),
            reference_data=reference_data,
        )

        rerun_result = _process(matching_input, config=config)
        assert rerun_result.matching_result_id == result.matching_result_id
        assert [lm.line_match_id for lm in rerun_result.line_matches] == [
            lm.line_match_id for lm in result.line_matches
        ]
