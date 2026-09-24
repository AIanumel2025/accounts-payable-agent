"""Contract tests for `ap_agent.models.matching` (M7 task §2/§4).

Confirms every required Phase 6 enum/dataclass exists with the field
shape the notebook's cell 77 defines, and that `MatchingConfig` follows
the same explicit-config, no-hidden-global-instance pattern as
`NormalizationConfig`/`FinancialValidationConfig`.
"""

from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest

from ap_agent.config.settings import MatchingConfig
from ap_agent.models.matching import (
    GoodsReceiptLine,
    GoodsReceiptRecord,
    GoodsReceiptState,
    MatchCheckStatus,
    MatchingEvent,
    MatchingResult,
    MatchingStatus,
    MatchMode,
    PurchaseOrderLine,
    PurchaseOrderRecord,
    PurchaseOrderState,
    ReferenceDataBundle,
    ResolutionStatus,
    SupplierMatchMethod,
    SupplierRecord,
)

pytestmark = pytest.mark.unit


def test_matching_status_members():
    assert [member.value for member in MatchingStatus] == ["SUCCEEDED", "REVIEW_REQUIRED", "FAILED"]


def test_match_check_status_members():
    assert [member.value for member in MatchCheckStatus] == [
        "PASSED",
        "FAILED",
        "REVIEW_REQUIRED",
        "NOT_APPLICABLE",
        "SKIPPED",
    ]


def test_match_mode_members():
    assert [member.value for member in MatchMode] == ["TWO_WAY", "THREE_WAY", "UNDETERMINED"]


def test_resolution_status_members():
    assert [member.value for member in ResolutionStatus] == [
        "MATCHED",
        "NOT_FOUND",
        "AMBIGUOUS",
        "NOT_REFERENCED",
        "CONFLICT",
    ]


def test_supplier_match_method_members():
    assert [member.value for member in SupplierMatchMethod] == [
        "SUPPLIER_ID",
        "TAX_IDENTIFIER",
        "EXACT_NAME",
        "ALIAS",
        "NORMALIZED_NAME",
        "NONE",
    ]


def test_purchase_order_state_members():
    assert [member.value for member in PurchaseOrderState] == [
        "OPEN",
        "PARTIALLY_RECEIVED",
        "FULLY_RECEIVED",
        "CLOSED",
        "CANCELLED",
    ]


def test_goods_receipt_state_members():
    assert [member.value for member in GoodsReceiptState] == ["POSTED", "REVERSED"]


def test_supplier_record_defaults():
    supplier = SupplierRecord(supplier_id="SUP-001", legal_name="Acme")
    assert supplier.aliases == ()
    assert supplier.active is True
    assert supplier.approved_for_payment is True


def test_purchase_order_record_round_trip():
    line = PurchaseOrderLine(
        line_number=1, description="Widget", quantity=Decimal("1"), unit_price=Decimal("1.00"), line_total=Decimal("1.00")
    )
    purchase_order = PurchaseOrderRecord(
        purchase_order_id="PO-1",
        purchase_order_number="99",
        supplier_id="SUP-001",
        currency="USD",
        order_date=date(2024, 1, 1),
        total_amount=Decimal("1.00"),
        lines=(line,),
    )
    assert purchase_order.state == PurchaseOrderState.OPEN
    assert purchase_order.requires_goods_receipt is True
    assert purchase_order.lines[0].line_total == Decimal("1.00")


def test_goods_receipt_record_round_trip():
    line = GoodsReceiptLine(receipt_line_number=1, purchase_order_line_number=1, quantity_received=Decimal("1"))
    receipt = GoodsReceiptRecord(
        goods_receipt_id="GR-1",
        goods_receipt_number="GR-0001",
        purchase_order_number="99",
        supplier_id="SUP-001",
        receipt_date=date(2024, 1, 2),
        lines=(line,),
    )
    assert receipt.state == GoodsReceiptState.POSTED


def test_reference_data_bundle_is_a_plain_aggregate():
    bundle = ReferenceDataBundle(suppliers=(), purchase_orders=(), goods_receipts=())
    assert bundle.suppliers == ()
    assert bundle.purchase_orders == ()
    assert bundle.goods_receipts == ()


def test_matching_result_defaults_review_reasons_and_errors_to_empty_tuple():
    event = MatchingEvent(
        event_type="REFERENCE_DATA_MATCHING",
        status=MatchingStatus.SUCCEEDED,
        batch_id=uuid4(),
        document_id=uuid4(),
        occurred_at=datetime.now(timezone.utc),
        message="ok",
        review_required=False,
    )
    result = MatchingResult(
        matching_result_id=uuid4(),
        batch_id=uuid4(),
        document_id=uuid4(),
        source_name="x.pdf",
        source_document_sha256="a" * 64,
        matching_version="matching-v1",
        status=MatchingStatus.SUCCEEDED,
        supplier_resolution=None,
        purchase_order_resolution=None,
        goods_receipt_resolution=None,
        line_matches=(),
        summary=None,
        event=event,
        review_required=False,
    )
    assert result.review_reasons == ()
    assert result.errors == ()


def test_matching_config_defaults_match_the_notebook_validated_instance():
    config = MatchingConfig(artifact_root=Path("/tmp/ap-agent-matching"))

    assert config.matching_version == "matching-v1"
    assert config.monetary_quantization == Decimal("0.01")
    assert config.quantity_quantization == Decimal("0.0001")
    assert config.price_tolerance == Decimal("0.01")
    assert config.quantity_tolerance == Decimal("0.0001")
    assert config.total_tolerance == Decimal("0.01")
    assert config.supplier_match_threshold == Decimal("0.90")
    assert config.supplier_ambiguity_margin == Decimal("0.05")
    assert config.maximum_supplier_candidates == 5
    assert config.allow_two_way_matching is True
    assert config.preserve_reference_snapshots is True


def test_matching_config_is_frozen():
    config = MatchingConfig(artifact_root=Path("/tmp/ap-agent-matching"))
    with pytest.raises(Exception):
        config.matching_version = "matching-v2"  # type: ignore[misc]
