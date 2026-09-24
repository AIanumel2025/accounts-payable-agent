"""Phase 6 (supplier, purchase-order and goods-receipt matching) contracts.

Source: notebook cell 77 ("PHASE 6 — CELL 1", "Reference-data and matching
contracts"), active-definition table extended for M7
(docs/modularisation_map.md). `MatchingConfig` is a configuration dataclass
and lives in `ap_agent.config.settings`, matching where
`NormalizationConfig`/`FinancialValidationConfig` live; it is re-exported
here for convenience only where the module docstring says so elsewhere.

Reference-data contracts (`SupplierRecord`, `PurchaseOrderRecord`,
`GoodsReceiptRecord`, `ReferenceDataBundle`) are **generic repository
shapes**: this module defines no fixture-specific instances of them
(CLAUDE.md: "Fixtures never enter production code" / decisions D-2, D-3).
The notebook's own `prototype_suppliers`/`prototype_purchase_orders`/
`prototype_goods_receipts` (cell 78/79) are controlled prototype data built
to match the four notebook fixtures exactly; that data lives only under
`tests/fixtures/reference_data/` and is loaded by
`ap_agent.adapters.reference_data_adapter`, never here.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from typing import Optional
from uuid import UUID

from ap_agent.models.normalization import NormalizedInvoiceRecord
from ap_agent.models.validation import FinancialValidationResult

__all__ = [
    "MatchingStatus",
    "MatchCheckStatus",
    "MatchMode",
    "ResolutionStatus",
    "SupplierMatchMethod",
    "PurchaseOrderState",
    "GoodsReceiptState",
    "SupplierRecord",
    "PurchaseOrderLine",
    "PurchaseOrderRecord",
    "GoodsReceiptLine",
    "GoodsReceiptRecord",
    "ReferenceDataBundle",
    "MatchingInput",
    "SupplierResolution",
    "PurchaseOrderResolution",
    "GoodsReceiptResolution",
    "LineMatchResult",
    "InvoiceMatchSummary",
    "MatchingEvent",
    "MatchingResult",
]


# ------------------------------------------------------------
# Status enumerations (notebook cell 77)
# ------------------------------------------------------------


class MatchingStatus(str, Enum):
    SUCCEEDED = "SUCCEEDED"
    REVIEW_REQUIRED = "REVIEW_REQUIRED"
    FAILED = "FAILED"


class MatchCheckStatus(str, Enum):
    PASSED = "PASSED"
    FAILED = "FAILED"
    REVIEW_REQUIRED = "REVIEW_REQUIRED"
    NOT_APPLICABLE = "NOT_APPLICABLE"
    SKIPPED = "SKIPPED"


class MatchMode(str, Enum):
    TWO_WAY = "TWO_WAY"
    THREE_WAY = "THREE_WAY"
    UNDETERMINED = "UNDETERMINED"


class ResolutionStatus(str, Enum):
    MATCHED = "MATCHED"
    NOT_FOUND = "NOT_FOUND"
    AMBIGUOUS = "AMBIGUOUS"
    NOT_REFERENCED = "NOT_REFERENCED"
    CONFLICT = "CONFLICT"


class SupplierMatchMethod(str, Enum):
    SUPPLIER_ID = "SUPPLIER_ID"
    TAX_IDENTIFIER = "TAX_IDENTIFIER"
    EXACT_NAME = "EXACT_NAME"
    ALIAS = "ALIAS"
    NORMALIZED_NAME = "NORMALIZED_NAME"
    NONE = "NONE"


class PurchaseOrderState(str, Enum):
    OPEN = "OPEN"
    PARTIALLY_RECEIVED = "PARTIALLY_RECEIVED"
    FULLY_RECEIVED = "FULLY_RECEIVED"
    CLOSED = "CLOSED"
    CANCELLED = "CANCELLED"


class GoodsReceiptState(str, Enum):
    POSTED = "POSTED"
    REVERSED = "REVERSED"


# ------------------------------------------------------------
# Authoritative reference-data contracts (generic; no fixture data)
# ------------------------------------------------------------


@dataclass(frozen=True)
class SupplierRecord:
    supplier_id: str
    legal_name: str
    aliases: tuple[str, ...] = ()
    tax_identifier: Optional[str] = None
    addresses: tuple[str, ...] = ()
    default_currency: Optional[str] = None
    active: bool = True
    approved_for_payment: bool = True


@dataclass(frozen=True)
class PurchaseOrderLine:
    line_number: int
    description: str
    quantity: Decimal
    unit_price: Decimal
    line_total: Decimal
    item_code: Optional[str] = None


@dataclass(frozen=True)
class PurchaseOrderRecord:
    purchase_order_id: str
    purchase_order_number: str
    supplier_id: str
    currency: str
    order_date: date
    total_amount: Decimal
    lines: tuple[PurchaseOrderLine, ...]
    state: PurchaseOrderState = PurchaseOrderState.OPEN
    requires_goods_receipt: bool = True


@dataclass(frozen=True)
class GoodsReceiptLine:
    receipt_line_number: int
    purchase_order_line_number: int
    quantity_received: Decimal
    item_code: Optional[str] = None
    description: Optional[str] = None


@dataclass(frozen=True)
class GoodsReceiptRecord:
    goods_receipt_id: str
    goods_receipt_number: str
    purchase_order_number: str
    supplier_id: str
    receipt_date: date
    lines: tuple[GoodsReceiptLine, ...]
    state: GoodsReceiptState = GoodsReceiptState.POSTED


@dataclass(frozen=True)
class ReferenceDataBundle:
    suppliers: tuple[SupplierRecord, ...]
    purchase_orders: tuple[PurchaseOrderRecord, ...]
    goods_receipts: tuple[GoodsReceiptRecord, ...]


# ------------------------------------------------------------
# Phase 6 input contract (Phase 4 -> 5 -> 6 bridge)
# ------------------------------------------------------------


@dataclass(frozen=True)
class MatchingInput:
    batch_id: UUID
    document_id: UUID
    source_name: str
    source_document_sha256: str
    normalized_invoice: NormalizedInvoiceRecord
    financial_validation: FinancialValidationResult
    reference_data: ReferenceDataBundle


# ------------------------------------------------------------
# Resolution contracts
# ------------------------------------------------------------


@dataclass(frozen=True)
class SupplierResolution:
    status: ResolutionStatus
    matched_supplier_id: Optional[str]
    match_method: SupplierMatchMethod
    candidate_supplier_ids: tuple[str, ...]
    score: Optional[Decimal]
    review_required: bool
    review_reasons: tuple[str, ...]


@dataclass(frozen=True)
class PurchaseOrderResolution:
    status: ResolutionStatus
    purchase_order_id: Optional[str]
    purchase_order_number: Optional[str]
    review_required: bool
    review_reasons: tuple[str, ...]


@dataclass(frozen=True)
class GoodsReceiptResolution:
    status: ResolutionStatus
    goods_receipt_ids: tuple[str, ...]
    review_required: bool
    review_reasons: tuple[str, ...]


# ------------------------------------------------------------
# Matching-result contracts
# ------------------------------------------------------------


@dataclass(frozen=True)
class LineMatchResult:
    line_match_id: UUID
    invoice_line_number: int
    purchase_order_line_number: Optional[int]
    description_status: MatchCheckStatus
    quantity_status: MatchCheckStatus
    unit_price_status: MatchCheckStatus
    line_total_status: MatchCheckStatus
    expected_quantity: Optional[Decimal]
    observed_quantity: Optional[Decimal]
    quantity_variance: Optional[Decimal]
    expected_unit_price: Optional[Decimal]
    observed_unit_price: Optional[Decimal]
    unit_price_variance: Optional[Decimal]
    expected_line_total: Optional[Decimal]
    observed_line_total: Optional[Decimal]
    line_total_variance: Optional[Decimal]
    review_required: bool
    review_reasons: tuple[str, ...]


@dataclass(frozen=True)
class InvoiceMatchSummary:
    match_mode: MatchMode
    supplier_status: MatchCheckStatus
    purchase_order_status: MatchCheckStatus
    goods_receipt_status: MatchCheckStatus
    currency_status: MatchCheckStatus
    line_items_status: MatchCheckStatus
    invoice_total_status: MatchCheckStatus
    expected_total: Optional[Decimal]
    observed_total: Optional[Decimal]
    total_variance: Optional[Decimal]
    checks_performed: int
    checks_passed: int
    checks_failed: int
    checks_requiring_review: int
    checks_skipped: int
    review_required: bool
    review_reasons: tuple[str, ...]


@dataclass(frozen=True)
class MatchingEvent:
    event_type: str
    status: MatchingStatus
    batch_id: UUID
    document_id: UUID
    occurred_at: datetime
    message: str
    review_required: bool


@dataclass(frozen=True)
class MatchingResult:
    matching_result_id: UUID
    batch_id: UUID
    document_id: UUID
    source_name: str
    source_document_sha256: str
    matching_version: str
    status: MatchingStatus
    supplier_resolution: Optional[SupplierResolution]
    purchase_order_resolution: Optional[PurchaseOrderResolution]
    goods_receipt_resolution: Optional[GoodsReceiptResolution]
    line_matches: tuple[LineMatchResult, ...]
    summary: Optional[InvoiceMatchSummary]
    event: MatchingEvent
    review_required: bool
    review_reasons: tuple[str, ...] = field(default_factory=tuple)
    errors: tuple[str, ...] = field(default_factory=tuple)
