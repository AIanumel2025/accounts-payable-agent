"""M8 §8 golden integration: the real Phase 6 matching engine + the real
`MemoryService`, over the exact four controlled fixtures' already-validated
Phase 4/5/6 field values.

Phase 4/5 outputs are reconstructed *synthetically* from
`tests/golden/phase_4_expected_results.json` /
`tests/golden/phase_5_expected_results.json` -- the same "fast,
non-`requires_paddle`" pattern `tests/unit/test_matching_golden_baseline.py`
already uses for Phase 6 -- so this test runs the real
`ap_agent.tools.matching.process_invoice_matching` and the real
`ap_agent.services.memory_service.MemoryService` end to end, deterministically,
on every push, independent of PaddleOCR/network availability. Persistence
goes through `tests.support.fake_memory_repository.FakeMemoryRepository`
(no `psycopg`), since no live PostgreSQL instance is available in this
environment -- see `tests/integration/test_memory_postgres_integration.py`
(`requires_postgres`) and `docs/m8_phase_7_postgres_memory_report.md` for
the real-PostgreSQL-specific behaviour (RLS, triggers, migrations) this
test cannot cover.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from uuid import NAMESPACE_URL, uuid4, uuid5

import pytest

from ap_agent.adapters.reference_data_adapter import load_reference_data_bundle
from ap_agent.config.settings import MatchingConfig
from ap_agent.models.matching import MatchingInput
from ap_agent.models.normalization import (
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
from ap_agent.models.validation import (
    FinancialValidationResult,
    FinancialValidationSummary,
    ValidationEvent,
    ValidationStatus,
)
from ap_agent.tools.matching import process_invoice_matching
from ap_agent.services.memory_service import MemoryService
from tests.support.fake_memory_repository import FakeMemoryRepository

pytestmark = [pytest.mark.integration, pytest.mark.requires_fixtures]

REFERENCE_DATA_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "reference_data"

# The prototype tenant the notebook derived (`PROTOTYPE_TENANT_ID = uuid5(
# NAMESPACE_URL, "ap-agent/tenant/prototype-sme")`, cell 88). Reproduced
# here only as a *test* fixture constant (task §3.3: production carries no
# such constant) so this test exercises the identical UUID the notebook
# already persisted to the real Neon database.
_NOTEBOOK_PROTOTYPE_TENANT_ID = uuid5(NAMESPACE_URL, "ap-agent/tenant/prototype-sme")


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


def _line(number, description, quantity, unit_price, amount):
    return NormalizedLineItem(
        line_item_id=uuid4(),
        line_number=number,
        description=_field(InvoiceFieldName.LINE_DESCRIPTION, description) if description else None,
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
            _field(InvoiceFieldName.LINE_AMOUNT, amount, NormalizedValueType.DECIMAL)
            if amount is not None
            else None
        ),
        currency=None,
        confidence=95.0,
        evidence_references=(),
    )


# Header fields taken verbatim from tests/golden/phase_4_expected_results.json
# `expected_fields`; line items for Template1/Aaron taken verbatim from
# tests/unit/test_matching_golden_baseline.py's `_DOCUMENTS` (itself sourced
# from the same golden file family). Matches the M8 task brief §8 table
# exactly (invoice numbers, suppliers, currencies, totals, statuses).
_DOCUMENTS = {
    "08181_flat_document.png": {
        "sha256": "2" * 64,
        "fields": {
            InvoiceFieldName.SUPPLIER_NAME: "Snyder, Hammond and Anderson",
            InvoiceFieldName.INVOICE_NUMBER: "308044",
            InvoiceFieldName.CURRENCY: "USD",
            InvoiceFieldName.SUBTOTAL: Decimal("63.45"),
            InvoiceFieldName.TOTAL_AMOUNT: Decimal("69.22"),
        },
        "lines": [
            _line(1, "Set 12 Colour Pencils Spaceboy", Decimal("10"), Decimal("0.65"), Decimal("6.50")),
            _line(2, "Red Retrospot Charlotte Bag", Decimal("4"), Decimal("0.85"), Decimal("3.40")),
        ],
        "norm_status": NormalizationStatus.SUCCEEDED,
        "financial_review_required": False,
    },
    "08181_warped_document_perspective_shadow.jpg": {
        "sha256": "4" * 64,
        "fields": {
            InvoiceFieldName.SUPPLIER_NAME: "Snyder, Hammond and Anderson",
            InvoiceFieldName.INVOICE_NUMBER: "308044",
            InvoiceFieldName.SUBTOTAL: Decimal("63.45"),
            # No CURRENCY, no TOTAL_AMOUNT: task §8.2 -- "currency: None",
            # "total: None", "missing total remains missing".
        },
        "lines": [],
        "norm_status": NormalizationStatus.REVIEW_REQUIRED,
        "financial_review_required": True,
    },
    "Template1_Instance90.jpg": {
        "sha256": "1" * 64,
        "fields": {
            InvoiceFieldName.CURRENCY: "EUR",
            InvoiceFieldName.PURCHASE_ORDER_NUMBER: "99",
            InvoiceFieldName.SUBTOTAL: Decimal("858.86"),
            InvoiceFieldName.TOTAL_AMOUNT: Decimal("873.58"),
            # No INVOICE_NUMBER: task §8.3 -- "invoice number: None".
        },
        "lines": [
            _line(1, "Sit sit together.", Decimal("3.00"), Decimal("50.47"), None),
            _line(2, "Maybe religious several.", Decimal("2.00"), Decimal("7.15"), None),
            _line(3, "Green military listen.", Decimal("6.00"), Decimal("34.69"), None),
            _line(4, "Course eight.", Decimal("2.00"), Decimal("36.33"), None),
            _line(5, "Cost number world.", Decimal("5.00"), Decimal("82.47"), None),
        ],
        "norm_status": NormalizationStatus.REVIEW_REQUIRED,
        "financial_review_required": True,
    },
    "invoice_Aaron Bergman_36258.pdf": {
        "sha256": "3" * 64,
        "fields": {
            InvoiceFieldName.INVOICE_NUMBER: "36258",
            InvoiceFieldName.PURCHASE_ORDER_NUMBER: "CA-2012-AB10015140-40974",
            InvoiceFieldName.CURRENCY: "USD",
            InvoiceFieldName.SUBTOTAL: Decimal("48.71"),
            InvoiceFieldName.TOTAL_AMOUNT: Decimal("50.10"),
        },
        "lines": [
            _line(1, "Global Push Button Manager's Chair, Indigo", Decimal("1"), Decimal("48.71"), Decimal("48.71")),
        ],
        "norm_status": NormalizationStatus.REVIEW_REQUIRED,
        "financial_review_required": True,
    },
}


@pytest.fixture(scope="module")
def golden_records(tmp_path_factory):
    tmp_path = tmp_path_factory.mktemp("memory-golden")
    config = MatchingConfig(artifact_root=tmp_path)
    reference_data = load_reference_data_bundle(REFERENCE_DATA_DIR)
    batch_id = uuid4()

    normalization_results = []
    matching_inputs = []
    matching_results = []

    for filename, spec in _DOCUMENTS.items():
        document_id = uuid4()

        fields = tuple(
            _field(
                field_name,
                value,
                NormalizedValueType.DECIMAL if isinstance(value, Decimal) else NormalizedValueType.TEXT,
            )
            for field_name, value in spec["fields"].items()
        )

        invoice_record = NormalizedInvoiceRecord(
            invoice_record_id=uuid4(),
            batch_id=batch_id,
            document_id=document_id,
            source_name=filename,
            source_document_sha256=spec["sha256"],
            fields=fields,
            line_items=tuple(spec["lines"]),
            normalization_version="normalization-v1",
            created_at=datetime.now(timezone.utc),
        )

        normalization_result = NormalizationResult(
            batch_id=batch_id,
            document_id=document_id,
            source_document_sha256=spec["sha256"],
            ocr_version="ocr-v1",
            normalization_version="normalization-v1",
            status=spec["norm_status"],
            invoice_record=invoice_record,
            field_candidates=(),
            event=NormalizationEvent(
                event_type="NORMALIZATION",
                status=spec["norm_status"],
                batch_id=batch_id,
                document_id=document_id,
                occurred_at=datetime.now(timezone.utc),
                message="golden fixture",
                review_required=spec["norm_status"] != NormalizationStatus.SUCCEEDED,
            ),
        )

        financial_status = (
            ValidationStatus.REVIEW_REQUIRED
            if spec["financial_review_required"]
            else ValidationStatus.SUCCEEDED
        )

        financial_result = FinancialValidationResult(
            batch_id=batch_id,
            document_id=document_id,
            source_name=filename,
            source_document_sha256=spec["sha256"],
            invoice_record_id=invoice_record.invoice_record_id,
            normalization_version="normalization-v1",
            validation_version="financial-validation-v1",
            status=financial_status,
            checks=(),
            summary=FinancialValidationSummary(
                total_checks=1,
                passed_checks=0 if spec["financial_review_required"] else 1,
                failed_checks=0,
                review_required_checks=1 if spec["financial_review_required"] else 0,
                not_applicable_checks=0,
                skipped_checks=0,
                review_required=spec["financial_review_required"],
            ),
            event=ValidationEvent(
                event_type="FINANCIAL_VALIDATION",
                status=financial_status,
                batch_id=batch_id,
                document_id=document_id,
                occurred_at=datetime.now(timezone.utc),
                message="golden fixture",
                review_required=spec["financial_review_required"],
            ),
            review_required=spec["financial_review_required"],
        )

        matching_input = MatchingInput(
            batch_id=batch_id,
            document_id=document_id,
            source_name=filename,
            source_document_sha256=spec["sha256"],
            normalized_invoice=invoice_record,
            financial_validation=financial_result,
            reference_data=reference_data,
        )

        matching_result = process_invoice_matching(matching_input, config=config)

        normalization_results.append(normalization_result)
        matching_inputs.append(matching_input)
        matching_results.append(matching_result)

    return normalization_results, matching_inputs, matching_results


@pytest.fixture(scope="module")
def memory_service_and_records(golden_records):
    normalization_results, matching_inputs, matching_results = golden_records

    repository = FakeMemoryRepository()
    service = MemoryService(repository, tenant_id=_NOTEBOOK_PROTOTYPE_TENANT_ID)

    stored = service.persist_batch(
        normalization_results=normalization_results,
        matching_inputs=matching_inputs,
        matching_results=matching_results,
    )

    return service, {record.source_name: record for record in stored}, matching_results


def test_aggregate_acceptance_four_invoices_stored_and_reconstructed(memory_service_and_records):
    service, by_name, matching_results = memory_service_and_records

    assert len(by_name) == 4

    for matching_result in matching_results:
        bundle = service.retrieve_bundle(document_id=matching_result.document_id)

        assert bundle is not None
        assert bundle.invoice_memory is not None
        assert bundle.invoice_memory.source_name == matching_result.source_name


def test_08181_flat_document(memory_service_and_records):
    _service, by_name, _ = memory_service_and_records
    record = by_name["08181_flat_document.png"]

    assert record.invoice_number == "308044"
    assert record.supplier_name == "Snyder, Hammond and Anderson"
    assert record.matched_supplier_id == "SUP-001"
    assert record.currency == "USD"
    assert record.total_amount == Decimal("69.22")
    assert record.normalization_status == "SUCCEEDED"
    assert record.financial_validation_status == "SUCCEEDED"
    assert record.matching_status == "SUCCEEDED"
    assert record.review_required is False


def test_08181_warped_document(memory_service_and_records):
    _service, by_name, _ = memory_service_and_records
    record = by_name["08181_warped_document_perspective_shadow.jpg"]

    assert record.invoice_number == "308044"
    assert record.matched_supplier_id == "SUP-001"
    assert record.currency is None
    assert record.total_amount is None
    assert record.normalization_status == "REVIEW_REQUIRED"
    assert record.financial_validation_status == "REVIEW_REQUIRED"
    assert record.matching_status == "REVIEW_REQUIRED"
    assert record.review_required is True


def test_template1_instance90(memory_service_and_records):
    _service, by_name, _ = memory_service_and_records
    record = by_name["Template1_Instance90.jpg"]

    assert record.invoice_number is None
    assert record.currency == "EUR"
    assert record.total_amount == Decimal("873.58")
    assert record.supplier_resolution_status == "NOT_FOUND"
    assert record.purchase_order_number == "99"
    assert record.goods_receipt_ids == ("GR-ID-00099",)
    assert record.match_mode == "THREE_WAY"
    assert record.line_match_count == 5
    assert record.review_required is True


def test_aaron_bergman_invoice(memory_service_and_records):
    _service, by_name, _ = memory_service_and_records
    record = by_name["invoice_Aaron Bergman_36258.pdf"]

    assert record.invoice_number == "36258"
    assert record.currency == "USD"
    assert record.total_amount == Decimal("50.10")
    assert record.supplier_resolution_status == "NOT_FOUND"
    assert record.purchase_order_number == "CA-2012-AB10015140-40974"
    assert record.goods_receipt_ids == ("GR-ID-CA-2012-AB10015140-40974",)
    assert record.match_mode == "THREE_WAY"
    assert record.line_match_count == 1
    assert record.review_required is True


def test_payload_hashes_verified_on_retrieval(memory_service_and_records):
    service, _by_name, matching_results = memory_service_and_records

    # MemoryService.retrieve_bundle re-verifies the payload hash internally
    # (raises MemoryIntegrityError on mismatch); reaching this line for all
    # four documents already proves it passed.
    for matching_result in matching_results:
        bundle = service.retrieve_bundle(document_id=matching_result.document_id)
        assert bundle.invoice_memory.payload_sha256


def test_idempotent_persistence(golden_records):
    normalization_results, matching_inputs, matching_results = golden_records

    repository = FakeMemoryRepository()
    service = MemoryService(repository, tenant_id=_NOTEBOOK_PROTOTYPE_TENANT_ID)

    first = service.persist_batch(
        normalization_results=normalization_results,
        matching_inputs=matching_inputs,
        matching_results=matching_results,
    )
    second = service.persist_batch(
        normalization_results=normalization_results,
        matching_inputs=matching_inputs,
        matching_results=matching_results,
    )

    assert {r.record_id for r in first} == {r.record_id for r in second}
    assert {r.payload_sha256 for r in first} == {r.payload_sha256 for r in second}


def test_cross_document_isolation(memory_service_and_records):
    """No document's memory bundle contains another document's identity."""

    service, _by_name, matching_results = memory_service_and_records

    document_ids = {str(result.document_id) for result in matching_results}

    for matching_result in matching_results:
        bundle = service.retrieve_bundle(document_id=matching_result.document_id)
        this_document = str(matching_result.document_id)

        for other_document_id in document_ids:
            if other_document_id == this_document:
                continue

            assert other_document_id != str(bundle.memory_record.document_id)
            assert other_document_id != str(bundle.invoice_memory.document_id)
