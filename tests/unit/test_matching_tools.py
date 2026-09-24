"""Unit tests for `ap_agent.tools.matching`'s resolution, retrieval, line-
matching and integrity-bridge functions (M7 task §5/§7). Synthetic
reference data only (never the real prototype fixture data, so these
tests generalise beyond the four notebook documents per decisions D-2/D-3).
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest

from ap_agent.config.settings import MatchingConfig
from ap_agent.exceptions import MatchingIntegrityError
from ap_agent.models.matching import (
    GoodsReceiptLine,
    GoodsReceiptRecord,
    GoodsReceiptState,
    MatchCheckStatus,
    MatchingInput,
    MatchMode,
    PurchaseOrderLine,
    PurchaseOrderRecord,
    PurchaseOrderState,
    ResolutionStatus,
    SupplierRecord,
)
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
from ap_agent.tools.matching import (
    build_matching_input,
    create_line_match_id,
    create_matching_result_id,
    determine_match_mode,
    evaluate_invoice_line_match,
    find_purchase_orders_by_reference,
    get_purchase_order_by_id,
    get_supplier_by_id,
    process_invoice_matching,
    retrieve_goods_receipts,
    retrieve_referenced_purchase_order,
    resolve_approved_supplier,
    values_within_tolerance,
)

pytestmark = pytest.mark.unit


# --- synthetic builders ------------------------------------------------------


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


def _line_item(line_number, description=None, quantity=None, unit_price=None, amount=None) -> NormalizedLineItem:
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


def _invoice_record(*, batch_id=None, document_id=None, sha256="a" * 64, header_fields=None, lines=()) -> NormalizedInvoiceRecord:
    header_fields = header_fields or {}

    fields = tuple(
        _field(field_name, value, NormalizedValueType.DECIMAL if isinstance(value, Decimal) else NormalizedValueType.TEXT)
        for field_name, value in header_fields.items()
        if value is not None
    )

    return NormalizedInvoiceRecord(
        invoice_record_id=uuid4(),
        batch_id=batch_id or uuid4(),
        document_id=document_id or uuid4(),
        source_name="synthetic.pdf",
        source_document_sha256=sha256,
        fields=fields,
        line_items=tuple(lines),
        normalization_version="normalization-v1",
        created_at=datetime.now(timezone.utc),
    )


def _financial_validation_result(*, batch_id, document_id, sha256="a" * 64, review_required=False) -> FinancialValidationResult:
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
        message="synthetic",
        review_required=review_required,
    )

    return FinancialValidationResult(
        batch_id=batch_id,
        document_id=document_id,
        source_name="synthetic.pdf",
        source_document_sha256=sha256,
        invoice_record_id=uuid4(),
        normalization_version="normalization-v1",
        validation_version="financial-validation-v1",
        status=status,
        checks=(),
        summary=summary,
        event=event,
        review_required=review_required,
    )


def _normalization_result(invoice_record: NormalizedInvoiceRecord) -> NormalizationResult:
    event = NormalizationEvent(
        event_type="NORMALIZATION",
        status=NormalizationStatus.SUCCEEDED,
        batch_id=invoice_record.batch_id,
        document_id=invoice_record.document_id,
        occurred_at=datetime.now(timezone.utc),
        message="synthetic",
        review_required=False,
    )
    return NormalizationResult(
        batch_id=invoice_record.batch_id,
        document_id=invoice_record.document_id,
        source_document_sha256=invoice_record.source_document_sha256,
        ocr_version="ocr-v2-paddle",
        normalization_version=invoice_record.normalization_version,
        status=NormalizationStatus.SUCCEEDED,
        invoice_record=invoice_record,
        field_candidates=(),
        event=event,
    )


def _supplier(supplier_id="SUP-001", legal_name="Acme Supplies", active=True, approved=True, aliases=()) -> SupplierRecord:
    return SupplierRecord(
        supplier_id=supplier_id, legal_name=legal_name, aliases=aliases, active=active, approved_for_payment=approved
    )


def _po_line(line_number=1, description="Widget", quantity="1", unit_price="10.00", line_total="10.00", item_code=None):
    return PurchaseOrderLine(
        line_number=line_number,
        description=description,
        quantity=Decimal(quantity),
        unit_price=Decimal(unit_price),
        line_total=Decimal(line_total),
        item_code=item_code,
    )


def _purchase_order(
    po_id="PO-1",
    po_number="1001",
    supplier_id="SUP-001",
    currency="USD",
    total="10.00",
    lines=None,
    state=PurchaseOrderState.OPEN,
    requires_gr=True,
):
    return PurchaseOrderRecord(
        purchase_order_id=po_id,
        purchase_order_number=po_number,
        supplier_id=supplier_id,
        currency=currency,
        order_date=date(2024, 1, 1),
        total_amount=Decimal(total),
        lines=tuple(lines if lines is not None else [_po_line()]),
        state=state,
        requires_goods_receipt=requires_gr,
    )


def _gr_line(receipt_line_number=1, po_line_number=1, quantity_received="1", item_code=None, description=None):
    return GoodsReceiptLine(
        receipt_line_number=receipt_line_number,
        purchase_order_line_number=po_line_number,
        quantity_received=Decimal(quantity_received),
        item_code=item_code,
        description=description,
    )


def _goods_receipt(
    gr_id="GR-1", gr_number="GR-0001", po_number="1001", supplier_id="SUP-001", lines=None, state=GoodsReceiptState.POSTED
):
    return GoodsReceiptRecord(
        goods_receipt_id=gr_id,
        goods_receipt_number=gr_number,
        purchase_order_number=po_number,
        supplier_id=supplier_id,
        receipt_date=date(2024, 1, 5),
        lines=tuple(lines if lines is not None else [_gr_line()]),
        state=state,
    )


@pytest.fixture
def config(tmp_path):
    return MatchingConfig(artifact_root=tmp_path)


# ============================================================
# Supplier resolution safety tests
# ============================================================


def test_missing_supplier_name_routes_to_review():
    invoice_record = _invoice_record(header_fields={})
    resolution = resolve_approved_supplier(invoice_record, (_supplier(),), config=MatchingConfig(artifact_root=Path("/tmp/x")))
    assert resolution.status == ResolutionStatus.NOT_FOUND
    assert resolution.matched_supplier_id is None
    assert resolution.review_required is True
    assert "SUPPLIER_NAME_MISSING" in resolution.review_reasons


def test_inactive_supplier_requires_review(config):
    invoice_record = _invoice_record(header_fields={InvoiceFieldName.SUPPLIER_NAME: "Acme Supplies"})
    supplier = _supplier(active=False)
    resolution = resolve_approved_supplier(invoice_record, (supplier,), config=config)
    assert resolution.status == ResolutionStatus.CONFLICT
    assert resolution.review_required is True
    assert "SUPPLIER_INACTIVE" in resolution.review_reasons
    assert resolution.matched_supplier_id == supplier.supplier_id


def test_unapproved_supplier_requires_review(config):
    invoice_record = _invoice_record(header_fields={InvoiceFieldName.SUPPLIER_NAME: "Acme Supplies"})
    supplier = _supplier(approved=False)
    resolution = resolve_approved_supplier(invoice_record, (supplier,), config=config)
    assert resolution.status == ResolutionStatus.CONFLICT
    assert resolution.review_required is True
    assert "SUPPLIER_NOT_APPROVED_FOR_PAYMENT" in resolution.review_reasons


def test_ambiguous_suppliers_require_review(config):
    invoice_record = _invoice_record(header_fields={InvoiceFieldName.SUPPLIER_NAME: "Acme Industries Group"})
    suppliers = (
        _supplier(supplier_id="SUP-001", legal_name="Acme Industries Group Ltd"),
        _supplier(supplier_id="SUP-002", legal_name="Acme Industries Group Inc"),
    )
    resolution = resolve_approved_supplier(invoice_record, suppliers, config=config)
    assert resolution.status == ResolutionStatus.AMBIGUOUS
    assert resolution.review_required is True
    assert "AMBIGUOUS_SUPPLIER_MATCH" in resolution.review_reasons
    assert resolution.matched_supplier_id is None


def test_supplier_below_threshold_is_not_found(config):
    invoice_record = _invoice_record(header_fields={InvoiceFieldName.SUPPLIER_NAME: "Totally Unrelated Business"})
    resolution = resolve_approved_supplier(invoice_record, (_supplier(legal_name="Acme Supplies"),), config=config)
    assert resolution.status == ResolutionStatus.NOT_FOUND
    assert "APPROVED_SUPPLIER_NOT_FOUND" in resolution.review_reasons


def test_exact_name_match_succeeds(config):
    invoice_record = _invoice_record(header_fields={InvoiceFieldName.SUPPLIER_NAME: "Acme Supplies"})
    supplier = _supplier()
    resolution = resolve_approved_supplier(invoice_record, (supplier,), config=config)
    assert resolution.status == ResolutionStatus.MATCHED
    assert resolution.matched_supplier_id == supplier.supplier_id
    assert resolution.review_required is False


def test_duplicate_supplier_ids_fail_closed():
    suppliers = (_supplier(supplier_id="SUP-001"), _supplier(supplier_id="SUP-001", legal_name="Duplicate"))
    with pytest.raises(ValueError, match="Duplicate supplier ID"):
        get_supplier_by_id("SUP-001", suppliers)


# ============================================================
# Purchase-order retrieval safety tests
# ============================================================


def test_unreferenced_invoice_is_distinct_from_missing_referenced_po(config):
    unreferenced = _invoice_record(header_fields={})
    referenced_but_missing = _invoice_record(header_fields={InvoiceFieldName.PURCHASE_ORDER_NUMBER: "UNKNOWN-PO"})

    from ap_agent.models.matching import SupplierResolution, SupplierMatchMethod

    no_supplier = SupplierResolution(
        status=ResolutionStatus.NOT_FOUND,
        matched_supplier_id=None,
        match_method=SupplierMatchMethod.NONE,
        candidate_supplier_ids=(),
        score=None,
        review_required=True,
        review_reasons=("SUPPLIER_NAME_MISSING",),
    )

    unreferenced_resolution = retrieve_referenced_purchase_order(unreferenced, (), no_supplier)
    missing_resolution = retrieve_referenced_purchase_order(referenced_but_missing, (), no_supplier)

    assert unreferenced_resolution.status == ResolutionStatus.NOT_REFERENCED
    assert unreferenced_resolution.review_required is False

    assert missing_resolution.status == ResolutionStatus.NOT_FOUND
    assert missing_resolution.review_required is True
    assert "REFERENCED_PURCHASE_ORDER_NOT_FOUND" in missing_resolution.review_reasons


def test_duplicate_po_references_fail_closed_as_ambiguous(config):
    from ap_agent.models.matching import SupplierResolution, SupplierMatchMethod

    no_supplier = SupplierResolution(
        status=ResolutionStatus.NOT_FOUND,
        matched_supplier_id=None,
        match_method=SupplierMatchMethod.NONE,
        candidate_supplier_ids=(),
        score=None,
        review_required=True,
        review_reasons=(),
    )

    invoice_record = _invoice_record(header_fields={InvoiceFieldName.PURCHASE_ORDER_NUMBER: "1001"})
    purchase_orders = (_purchase_order(po_id="PO-A", po_number="1001"), _purchase_order(po_id="PO-B", po_number="1001"))

    resolution = retrieve_referenced_purchase_order(invoice_record, purchase_orders, no_supplier)
    assert resolution.status == ResolutionStatus.AMBIGUOUS
    assert resolution.review_required is True
    assert "DUPLICATE_PURCHASE_ORDER_REFERENCE" in resolution.review_reasons


def test_duplicate_po_ids_fail_closed():
    purchase_orders = (_purchase_order(po_id="PO-1"), _purchase_order(po_id="PO-1", po_number="9999"))
    with pytest.raises(ValueError, match="Duplicate purchase-order ID"):
        get_purchase_order_by_id("PO-1", purchase_orders)


def test_po_supplier_conflict_requires_review(config):
    from ap_agent.models.matching import SupplierResolution, SupplierMatchMethod

    matched_supplier = SupplierResolution(
        status=ResolutionStatus.MATCHED,
        matched_supplier_id="SUP-999",
        match_method=SupplierMatchMethod.EXACT_NAME,
        candidate_supplier_ids=("SUP-999",),
        score=Decimal("1.0000"),
        review_required=False,
        review_reasons=(),
    )

    invoice_record = _invoice_record(header_fields={InvoiceFieldName.PURCHASE_ORDER_NUMBER: "1001"})
    purchase_orders = (_purchase_order(po_id="PO-1", po_number="1001", supplier_id="SUP-001"),)

    resolution = retrieve_referenced_purchase_order(invoice_record, purchase_orders, matched_supplier)
    assert resolution.status == ResolutionStatus.CONFLICT
    assert "PURCHASE_ORDER_SUPPLIER_CONFLICT" in resolution.review_reasons


@pytest.mark.parametrize("state,reason", [(PurchaseOrderState.CANCELLED, "PURCHASE_ORDER_CANCELLED"), (PurchaseOrderState.CLOSED, "PURCHASE_ORDER_CLOSED")])
def test_cancelled_or_closed_pos_require_review(config, state, reason):
    from ap_agent.models.matching import SupplierResolution, SupplierMatchMethod

    no_supplier = SupplierResolution(
        status=ResolutionStatus.NOT_FOUND,
        matched_supplier_id=None,
        match_method=SupplierMatchMethod.NONE,
        candidate_supplier_ids=(),
        score=None,
        review_required=True,
        review_reasons=(),
    )

    invoice_record = _invoice_record(header_fields={InvoiceFieldName.PURCHASE_ORDER_NUMBER: "1001"})
    purchase_orders = (_purchase_order(po_id="PO-1", po_number="1001", state=state),)

    resolution = retrieve_referenced_purchase_order(invoice_record, purchase_orders, no_supplier)
    assert resolution.status == ResolutionStatus.CONFLICT
    assert reason in resolution.review_reasons


def test_short_po_reference_matches_by_normalized_identifier():
    purchase_orders = (_purchase_order(po_id="PO-1", po_number="99"),)
    assert len(find_purchase_orders_by_reference("PO Number: 99", purchase_orders)) == 1
    assert len(find_purchase_orders_by_reference("99", purchase_orders)) == 1
    assert not find_purchase_orders_by_reference("UNKNOWN-PO", purchase_orders)


# ============================================================
# Goods-receipt retrieval safety tests
# ============================================================


def test_reversed_receipts_are_excluded(config):
    po_resolution_status = ResolutionStatus.MATCHED
    from ap_agent.models.matching import PurchaseOrderResolution

    po_resolution = PurchaseOrderResolution(
        status=po_resolution_status, purchase_order_id="PO-1", purchase_order_number="1001", review_required=False, review_reasons=()
    )
    purchase_orders = (_purchase_order(po_id="PO-1", po_number="1001"),)
    goods_receipts = (_goods_receipt(gr_id="GR-1", po_number="1001", state=GoodsReceiptState.REVERSED),)

    resolution = retrieve_goods_receipts(po_resolution, purchase_orders, goods_receipts)
    assert resolution.status == ResolutionStatus.NOT_FOUND
    assert "GOODS_RECEIPT_REVERSED" in resolution.review_reasons


def test_missing_required_receipt_requires_review(config):
    from ap_agent.models.matching import PurchaseOrderResolution

    po_resolution = PurchaseOrderResolution(
        status=ResolutionStatus.MATCHED, purchase_order_id="PO-1", purchase_order_number="1001", review_required=False, review_reasons=()
    )
    purchase_orders = (_purchase_order(po_id="PO-1", po_number="1001"),)

    resolution = retrieve_goods_receipts(po_resolution, purchase_orders, ())
    assert resolution.status == ResolutionStatus.NOT_FOUND
    assert "REQUIRED_GOODS_RECEIPT_NOT_FOUND" in resolution.review_reasons
    assert resolution.review_required is True


def test_receipt_supplier_conflict_requires_review(config):
    from ap_agent.models.matching import PurchaseOrderResolution

    po_resolution = PurchaseOrderResolution(
        status=ResolutionStatus.MATCHED, purchase_order_id="PO-1", purchase_order_number="1001", review_required=False, review_reasons=()
    )
    purchase_orders = (_purchase_order(po_id="PO-1", po_number="1001", supplier_id="SUP-001"),)
    goods_receipts = (_goods_receipt(gr_id="GR-1", po_number="1001", supplier_id="SUP-999"),)

    resolution = retrieve_goods_receipts(po_resolution, purchase_orders, goods_receipts)
    assert resolution.status == ResolutionStatus.CONFLICT
    assert "GOODS_RECEIPT_SUPPLIER_CONFLICT" in resolution.review_reasons


def test_receipt_line_referencing_nonexistent_po_line_requires_review(config):
    from ap_agent.models.matching import PurchaseOrderResolution

    po_resolution = PurchaseOrderResolution(
        status=ResolutionStatus.MATCHED, purchase_order_id="PO-1", purchase_order_number="1001", review_required=False, review_reasons=()
    )
    purchase_orders = (_purchase_order(po_id="PO-1", po_number="1001", lines=[_po_line(line_number=1)]),)
    goods_receipts = (_goods_receipt(gr_id="GR-1", po_number="1001", lines=[_gr_line(po_line_number=99)]),)

    resolution = retrieve_goods_receipts(po_resolution, purchase_orders, goods_receipts)
    assert resolution.status == ResolutionStatus.CONFLICT
    assert "GOODS_RECEIPT_LINE_NOT_IN_PO" in resolution.review_reasons


def test_two_way_matching_selected_when_receipt_not_required():
    from ap_agent.models.matching import PurchaseOrderResolution

    po_resolution = PurchaseOrderResolution(
        status=ResolutionStatus.MATCHED, purchase_order_id="PO-1", purchase_order_number="1001", review_required=False, review_reasons=()
    )
    purchase_orders = (_purchase_order(po_id="PO-1", po_number="1001", requires_gr=False),)
    assert determine_match_mode(po_resolution, purchase_orders) == MatchMode.TWO_WAY


def test_undetermined_match_mode_when_no_matched_po():
    from ap_agent.models.matching import PurchaseOrderResolution

    po_resolution = PurchaseOrderResolution(
        status=ResolutionStatus.NOT_REFERENCED, purchase_order_id=None, purchase_order_number=None, review_required=False, review_reasons=()
    )
    assert determine_match_mode(po_resolution, ()) == MatchMode.UNDETERMINED


# ============================================================
# Line matching and tolerance safety tests
# ============================================================


def test_overbilled_quantity_fails_closed(config):
    purchase_order = _purchase_order(po_id="PO-1", lines=[_po_line(quantity="1")])
    invoice_line = _line_item(1, description="Widget", quantity=Decimal("5"), unit_price=Decimal("10.00"), amount=Decimal("50.00"))

    line_match = evaluate_invoice_line_match(
        document_id=uuid4(),
        invoice_line=invoice_line,
        purchase_order=purchase_order,
        purchase_order_line=purchase_order.lines[0],
        description_score=Decimal("1.0000"),
        received_quantities={1: Decimal("1")},
        match_mode=MatchMode.THREE_WAY,
        config=config,
    )
    assert line_match.quantity_status == MatchCheckStatus.FAILED
    assert line_match.review_required is True
    assert "INVOICE_QUANTITY_EXCEEDS_ORDER_OR_RECEIPT" in line_match.review_reasons


def test_missing_invoice_line_total_remains_missing_not_inferred(config):
    purchase_order = _purchase_order(po_id="PO-1", lines=[_po_line(quantity="1", unit_price="10.00")])
    invoice_line = _line_item(1, description="Widget", quantity=Decimal("1"), unit_price=Decimal("10.00"), amount=None)

    line_match = evaluate_invoice_line_match(
        document_id=uuid4(),
        invoice_line=invoice_line,
        purchase_order=purchase_order,
        purchase_order_line=purchase_order.lines[0],
        description_score=Decimal("1.0000"),
        received_quantities={1: Decimal("1")},
        match_mode=MatchMode.THREE_WAY,
        config=config,
    )
    assert line_match.observed_line_total is None
    assert line_match.line_total_status == MatchCheckStatus.REVIEW_REQUIRED
    assert "INVOICE_LINE_TOTAL_MISSING" in line_match.review_reasons


def test_price_tolerance_boundary_is_exact(config):
    purchase_order = _purchase_order(po_id="PO-1", lines=[_po_line(quantity="1", unit_price="10.00")])

    # Exactly at the tolerance (0.01) must pass.
    at_boundary = _line_item(1, description="Widget", quantity=Decimal("1"), unit_price=Decimal("10.01"), amount=Decimal("10.01"))
    result_at_boundary = evaluate_invoice_line_match(
        document_id=uuid4(),
        invoice_line=at_boundary,
        purchase_order=purchase_order,
        purchase_order_line=purchase_order.lines[0],
        description_score=Decimal("1.0000"),
        received_quantities={1: Decimal("1")},
        match_mode=MatchMode.THREE_WAY,
        config=config,
    )
    assert result_at_boundary.unit_price_status == MatchCheckStatus.PASSED

    # Just past the tolerance must fail.
    past_boundary = _line_item(1, description="Widget", quantity=Decimal("1"), unit_price=Decimal("10.02"), amount=Decimal("10.02"))
    result_past_boundary = evaluate_invoice_line_match(
        document_id=uuid4(),
        invoice_line=past_boundary,
        purchase_order=purchase_order,
        purchase_order_line=purchase_order.lines[0],
        description_score=Decimal("1.0000"),
        received_quantities={1: Decimal("1")},
        match_mode=MatchMode.THREE_WAY,
        config=config,
    )
    assert result_past_boundary.unit_price_status == MatchCheckStatus.FAILED


def test_values_within_tolerance_boundary_is_inclusive():
    assert values_within_tolerance(Decimal("10.01"), Decimal("10.00"), Decimal("0.01")) is True
    assert values_within_tolerance(Decimal("10.02"), Decimal("10.00"), Decimal("0.01")) is False


def test_po_total_tolerance_boundary_is_exact(config):
    from ap_agent.tools.matching import evaluate_po_total_status

    purchase_order = _purchase_order(total="100.00")

    at_boundary = _invoice_record(header_fields={InvoiceFieldName.SUBTOTAL: Decimal("100.01")})
    review_reasons: list[str] = []
    status, expected_total, observed_total, variance = evaluate_po_total_status(
        at_boundary, purchase_order, config=config, review_reasons=review_reasons
    )
    assert status == MatchCheckStatus.PASSED
    assert variance == Decimal("0.01")

    past_boundary = _invoice_record(header_fields={InvoiceFieldName.SUBTOTAL: Decimal("100.02")})
    review_reasons = []
    status, *_ = evaluate_po_total_status(past_boundary, purchase_order, config=config, review_reasons=review_reasons)
    assert status == MatchCheckStatus.FAILED
    assert "PO_TOTAL_VARIANCE_EXCEEDS_TOLERANCE" in review_reasons


def test_po_ownership_never_fills_a_missing_supplier_name(config):
    """A PO's own supplier_id must never be copied into an unresolved
    invoice supplier (non-inference safeguard, task §3/§7)."""

    invoice_record = _invoice_record(header_fields={InvoiceFieldName.PURCHASE_ORDER_NUMBER: "1001"})
    no_supplier = resolve_approved_supplier(invoice_record, (), config=config)
    purchase_orders = (_purchase_order(po_id="PO-1", po_number="1001", supplier_id="SUP-777"),)

    po_resolution = retrieve_referenced_purchase_order(invoice_record, purchase_orders, no_supplier)

    assert po_resolution.status == ResolutionStatus.MATCHED
    assert no_supplier.matched_supplier_id is None
    assert no_supplier.status == ResolutionStatus.NOT_FOUND


# ============================================================
# Phase 4/5 -> Phase 6 integrity bridge
# ============================================================


def test_build_matching_input_rejects_tampered_source_hash():
    invoice_record = _invoice_record()
    normalization_result = _normalization_result(invoice_record)
    financial_result = _financial_validation_result(
        batch_id=normalization_result.batch_id, document_id=normalization_result.document_id, sha256="0" * 64
    )

    with pytest.raises(MatchingIntegrityError, match="source hashes differ"):
        build_matching_input(normalization_result, financial_result, reference_data=None)


def test_build_matching_input_rejects_cross_document_batch_mismatch():
    invoice_record = _invoice_record()
    normalization_result = _normalization_result(invoice_record)
    financial_result = _financial_validation_result(
        batch_id=uuid4(), document_id=normalization_result.document_id, sha256=invoice_record.source_document_sha256
    )

    with pytest.raises(MatchingIntegrityError):
        build_matching_input(normalization_result, financial_result, reference_data=None)


def test_build_matching_input_rejects_missing_invoice_record():
    from ap_agent.models.normalization import NormalizationEvent

    batch_id, document_id = uuid4(), uuid4()
    event = NormalizationEvent(
        event_type="NORMALIZATION",
        status=NormalizationStatus.FAILED,
        batch_id=batch_id,
        document_id=document_id,
        occurred_at=datetime.now(timezone.utc),
        message="failed",
        review_required=True,
    )
    normalization_result = NormalizationResult(
        batch_id=batch_id,
        document_id=document_id,
        source_document_sha256="a" * 64,
        ocr_version="ocr-v2-paddle",
        normalization_version="normalization-v1",
        status=NormalizationStatus.FAILED,
        invoice_record=None,
        field_candidates=(),
        event=event,
    )
    financial_result = _financial_validation_result(batch_id=batch_id, document_id=document_id, sha256="a" * 64)

    with pytest.raises(MatchingIntegrityError):
        build_matching_input(normalization_result, financial_result, reference_data=None)


def test_build_matching_input_accepts_a_consistent_pair():
    invoice_record = _invoice_record()
    normalization_result = _normalization_result(invoice_record)
    financial_result = _financial_validation_result(
        batch_id=normalization_result.batch_id,
        document_id=normalization_result.document_id,
        sha256=invoice_record.source_document_sha256,
    )

    from ap_agent.models.matching import ReferenceDataBundle

    matching_input = build_matching_input(
        normalization_result, financial_result, reference_data=ReferenceDataBundle((), (), ())
    )
    assert matching_input.batch_id == normalization_result.batch_id
    assert matching_input.document_id == normalization_result.document_id


# ============================================================
# Determinism and cross-document isolation
# ============================================================


def test_matching_result_ids_are_deterministic():
    invoice_record = _invoice_record(sha256="e" * 64)
    financial_result = _financial_validation_result(
        batch_id=invoice_record.batch_id, document_id=invoice_record.document_id, sha256="e" * 64
    )

    from ap_agent.models.matching import ReferenceDataBundle

    matching_input = MatchingInput(
        batch_id=invoice_record.batch_id,
        document_id=invoice_record.document_id,
        source_name=invoice_record.source_name,
        source_document_sha256="e" * 64,
        normalized_invoice=invoice_record,
        financial_validation=financial_result,
        reference_data=ReferenceDataBundle((), (), ()),
    )

    first_id = create_matching_result_id(matching_input, "matching-v1")
    second_id = create_matching_result_id(matching_input, "matching-v1")
    assert first_id == second_id


def test_line_match_ids_are_deterministic_and_document_scoped():
    document_id = uuid4()
    other_document_id = uuid4()

    first = create_line_match_id(document_id, 1, "PO-1", 1, "matching-v1")
    second = create_line_match_id(document_id, 1, "PO-1", 1, "matching-v1")
    different_document = create_line_match_id(other_document_id, 1, "PO-1", 1, "matching-v1")

    assert first == second
    assert first != different_document


def test_equivalent_reruns_of_process_invoice_matching_produce_identical_business_content(config):
    invoice_record = _invoice_record(
        header_fields={InvoiceFieldName.SUPPLIER_NAME: "Acme Supplies", InvoiceFieldName.CURRENCY: "USD"},
    )
    financial_result = _financial_validation_result(
        batch_id=invoice_record.batch_id, document_id=invoice_record.document_id, sha256=invoice_record.source_document_sha256
    )

    from ap_agent.models.matching import ReferenceDataBundle

    reference_data = ReferenceDataBundle(suppliers=(_supplier(),), purchase_orders=(), goods_receipts=())

    matching_input = MatchingInput(
        batch_id=invoice_record.batch_id,
        document_id=invoice_record.document_id,
        source_name=invoice_record.source_name,
        source_document_sha256=invoice_record.source_document_sha256,
        normalized_invoice=invoice_record,
        financial_validation=financial_result,
        reference_data=reference_data,
    )

    first_result = process_invoice_matching(matching_input, config=config)
    second_result = process_invoice_matching(matching_input, config=config)

    assert first_result.matching_result_id == second_result.matching_result_id
    assert first_result.status == second_result.status
    assert first_result.review_reasons == second_result.review_reasons
