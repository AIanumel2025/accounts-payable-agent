"""Phase 6 (supplier, purchase-order and goods-receipt matching) processing
functions: the Phase 4/5 -> Phase 6 typed bridge, supplier resolution,
purchase-order and goods-receipt retrieval, match-mode selection, line
matching and tolerance evaluation, invoice-level summary, orchestration and
artifact persistence.

Source cells (M7 task §2/§4; docs/modularisation_map.md extended for M7):

  - Cell 77 ("PHASE 6 — CELL 1"): contracts, already extracted to
    `ap_agent.models.matching` and `ap_agent.config.settings.MatchingConfig`.
  - Cell 78 ("PHASE 6 — CELL 2"): comparison helpers
    (`normalize_business_text`, `normalize_reference_identifier`,
    `similarity_score`), the Phase 4 field-access bridge
    (`unwrap_normalized_value`, `invoice_field_name_text`,
    `get_invoice_field_value`), supplier resolution
    (`supplier_record_score`, `identify_supplier_match_method`,
    `resolve_approved_supplier`) and referenced-PO retrieval
    (`find_purchase_orders_by_reference`, `retrieve_referenced_purchase_order`).
    Cell 78's own controlled prototype supplier/PO repository
    (`prototype_suppliers`, `prototype_purchase_orders`) and its
    module-level `phase_6_reference_data` instance are **not** extracted
    here (decisions D-2/D-3: fixture-specific reference data never enters
    `src/`; it lives under `tests/fixtures/reference_data/`, loaded by
    `ap_agent.adapters.reference_data_adapter`). `get_supplier_by_id` is
    extracted as the generic repository-lookup function it is.
  - Cell 79 ("PHASE 6 — CELL 3"): goods-receipt retrieval
    (`get_purchase_order_by_id`, `find_goods_receipts_by_po_reference`,
    `aggregate_received_quantities`, `retrieve_goods_receipts`) and
    match-mode selection (`determine_match_mode`). Its own prototype
    goods-receipt repository is not extracted, for the same D-2/D-3 reason.
  - Cell 80 ("PHASE 6 — CELL 4"): deterministic line-match identifiers,
    value-conversion helpers (`normalized_field_value`, `decimal_value`,
    `quantize_money`, `quantize_quantity`, `values_within_tolerance`),
    invoice-line access helpers, deterministic PO-line selection
    (`rank_purchase_order_lines`, `select_purchase_order_line`) and line
    evaluation (`evaluate_invoice_line_match`, `match_invoice_lines`). This
    cell's own `append_unique_reason` is Phase 6's binding (does not skip
    falsy reasons, like Phase 5's -- kept as its own private module-scoped
    helper per D-9's per-tool-module naming policy, not imported from
    `tools.financial_validation`).
  - Cell 82 ("PHASE 6 — CELL 5"): the Phase 4/5 -> 6 integrity bridge
    (`build_matching_input`), summary helpers, orchestration
    (`process_invoice_matching`) and persistence (`persist_matching_result`).
    This module's own `phase_6_json_safe` moved to
    `ap_agent.artifacts.serialization` (D-8: a fourth, distinct JSON-safe
    converter, not merged with `phase_5_json_safe`). `write_text_idempotently`
    is kept here (Q-6 precedent: phase-specific persistence stays in the
    phase's own tools module) and upgraded to raise `MatchingIntegrityError`
    instead of the notebook's bare `RuntimeError`/`ValueError`, mirroring
    the `NormalizationIntegrityError`/`FinancialValidationIntegrityError`
    precedent from M5/M6 (same cases fail, structured reason instead of a
    bare exception).

Explicit configuration (task §6; CLAUDE.md's top-level rule). The notebook
read a single hidden global `matching_config` from nearly every Phase 6
function (cell 77). Every function below that used it takes an explicit
`config: MatchingConfig` keyword parameter instead; there is no
module-level config *instance* in this module. `aggregate_received_quantities`
additionally takes `config` (the notebook read the module constant
`QUANTITY_QUANTIZATION` directly, which is numerically identical to
`MatchingConfig.quantity_quantization`'s default) so that a caller-supplied
non-default quantization is actually honoured, consistent with the rest of
this module's config threading.

Non-inference safeguards (task §3/§7, preserved verbatim from the
notebook): a supplier is never inferred from a matched purchase order
(`resolve_approved_supplier` only ever reads the invoice's own
SUPPLIER_NAME/SUPPLIER_ADDRESS fields); a missing invoice line total is
never computed from quantity * unit price (`evaluate_invoice_line_match`
only ever reads `invoice_line_amount` for the *observed* total -- the
*expected* total is computed from the PO line, never the reverse); Phase 5
review state is never downgraded (`process_invoice_matching` only ever adds
`INHERITED_FINANCIAL_VALIDATION_REVIEW` and ORs it into `review_required`,
never clears an upstream reason).
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import unicodedata
from dataclasses import fields, is_dataclass
from datetime import date, datetime, timezone
from decimal import ROUND_HALF_UP, Decimal
from difflib import SequenceMatcher
from enum import Enum
from pathlib import Path
from typing import Any, Optional
from uuid import NAMESPACE_URL, UUID, uuid5

from ap_agent.artifacts.serialization import phase_6_json_safe
from ap_agent.config.settings import MatchingConfig
from ap_agent.exceptions import MatchingIntegrityError
from ap_agent.models.matching import (
    GoodsReceiptRecord,
    GoodsReceiptResolution,
    GoodsReceiptState,
    InvoiceMatchSummary,
    LineMatchResult,
    MatchCheckStatus,
    MatchingEvent,
    MatchingInput,
    MatchingResult,
    MatchingStatus,
    MatchMode,
    PurchaseOrderLine,
    PurchaseOrderRecord,
    PurchaseOrderResolution,
    PurchaseOrderState,
    ReferenceDataBundle,
    ResolutionStatus,
    SupplierMatchMethod,
    SupplierRecord,
    SupplierResolution,
)
from ap_agent.models.normalization import NormalizationResult, NormalizedInvoiceRecord
from ap_agent.models.validation import FinancialValidationResult

__all__ = [
    "phase_6_utc_now",
    "LINE_DESCRIPTION_MATCH_THRESHOLD",
    "normalize_business_text",
    "normalize_reference_identifier",
    "similarity_score",
    "unwrap_normalized_value",
    "invoice_field_name_text",
    "get_invoice_field_value",
    "get_supplier_by_id",
    "find_purchase_orders_by_reference",
    "supplier_record_score",
    "identify_supplier_match_method",
    "resolve_approved_supplier",
    "retrieve_referenced_purchase_order",
    "get_purchase_order_by_id",
    "find_goods_receipts_by_po_reference",
    "aggregate_received_quantities",
    "retrieve_goods_receipts",
    "determine_match_mode",
    "create_line_match_id",
    "normalized_field_value",
    "decimal_value",
    "quantize_money",
    "quantize_quantity",
    "values_within_tolerance",
    "append_unique_reason",
    "invoice_line_description",
    "invoice_line_quantity",
    "invoice_line_unit_price",
    "invoice_line_amount",
    "rank_purchase_order_lines",
    "select_purchase_order_line",
    "evaluate_invoice_line_match",
    "match_invoice_lines",
    "result_status_text",
    "result_requires_review",
    "build_matching_input",
    "create_matching_result_id",
    "resolution_check_status",
    "append_reasons",
    "find_resolved_purchase_order",
    "resolved_goods_receipts",
    "evaluate_currency_status",
    "evaluate_line_matches_status",
    "evaluate_po_total_status",
    "build_invoice_match_summary",
    "process_invoice_matching",
    "canonical_json_text",
    "sha256_text",
    "write_text_idempotently",
    "build_reference_snapshot",
    "phase_6_artifact_directory",
    "persist_matching_result",
]


def phase_6_utc_now() -> datetime:
    return datetime.now(timezone.utc)


LINE_DESCRIPTION_MATCH_THRESHOLD = Decimal("0.75")

REFERENCE_LABEL_PATTERN = re.compile(
    r"^(?:PO|P\.O\.|PURCHASE\s+ORDER|ORDER\s+ID)"
    r"(?:\s+NUMBER|\s+NO\.?)?\s*[:#-]*\s*",
    flags=re.IGNORECASE,
)


# ============================================================
# 1. Comparison helpers (notebook cell 78)
# ============================================================


def normalize_business_text(value: Any) -> str:
    if value is None:
        return ""

    text = unicodedata.normalize("NFKD", str(value))
    text = "".join(character for character in text if not unicodedata.combining(character))
    text = re.sub(r"[^A-Za-z0-9]+", " ", text)

    return " ".join(text.upper().split())


def normalize_reference_identifier(value: Any) -> str:
    if value is None:
        return ""

    text = str(value).strip()
    text = REFERENCE_LABEL_PATTERN.sub("", text)

    return re.sub(r"[^A-Za-z0-9]", "", text).upper()


def similarity_score(first_value: Any, second_value: Any) -> Decimal:
    first_key = normalize_business_text(first_value)
    second_key = normalize_business_text(second_value)

    if not first_key or not second_key:
        return Decimal("0")

    if first_key == second_key:
        return Decimal("1.0000")

    ratio = SequenceMatcher(None, first_key, second_key).ratio()

    return Decimal(str(ratio)).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)


# ============================================================
# 2. Phase 4 field-access bridge (notebook cell 78)
# ============================================================


def unwrap_normalized_value(normalized_value: Any) -> Any:
    if normalized_value is None:
        return None

    if isinstance(normalized_value, (str, int, float, Decimal, date, datetime, UUID)):
        return normalized_value

    if isinstance(normalized_value, Enum):
        return normalized_value.value

    if isinstance(normalized_value, dict):
        for key in ("value", "canonical_value", "text_value", "decimal_value", "date_value", "currency_code"):
            candidate = normalized_value.get(key)

            if candidate is not None:
                return candidate

    for attribute_name in ("value", "canonical_value", "text_value", "decimal_value", "date_value", "currency_code"):
        if not hasattr(normalized_value, attribute_name):
            continue

        candidate = getattr(normalized_value, attribute_name)

        if candidate is not None:
            return candidate

    if is_dataclass(normalized_value):
        ignored_fields = {"value_type", "type", "raw_value"}

        candidates = [
            getattr(normalized_value, field_definition.name)
            for field_definition in fields(normalized_value)
            if (
                field_definition.name not in ignored_fields
                and getattr(normalized_value, field_definition.name) is not None
            )
        ]

        if len(candidates) == 1:
            return candidates[0]

    return str(normalized_value)


def invoice_field_name_text(field_name: Any) -> str:
    if isinstance(field_name, Enum):
        return str(field_name.value).upper()

    return str(field_name).upper()


def get_invoice_field_value(invoice_record: NormalizedInvoiceRecord, requested_field_name: str) -> Any:
    requested_key = str(requested_field_name).upper()

    for invoice_field in invoice_record.fields:
        current_key = invoice_field_name_text(invoice_field.field_name)

        if current_key != requested_key:
            continue

        value = unwrap_normalized_value(invoice_field.normalized_value)

        if value is not None and str(value).strip():
            return value

    return None


# ============================================================
# 3. Repository lookups (notebook cells 78-79; generic, no fixture data)
# ============================================================


def get_supplier_by_id(supplier_id: str, suppliers: tuple[SupplierRecord, ...]) -> Optional[SupplierRecord]:
    matches = [supplier for supplier in suppliers if supplier.supplier_id == supplier_id]

    if len(matches) > 1:
        raise ValueError(f"Duplicate supplier ID detected: {supplier_id}")

    return matches[0] if matches else None


def find_purchase_orders_by_reference(
    purchase_order_reference: Any,
    purchase_orders: tuple[PurchaseOrderRecord, ...],
) -> tuple[PurchaseOrderRecord, ...]:
    reference_key = normalize_reference_identifier(purchase_order_reference)

    if not reference_key:
        return ()

    return tuple(
        purchase_order
        for purchase_order in purchase_orders
        if normalize_reference_identifier(purchase_order.purchase_order_number) == reference_key
    )


def get_purchase_order_by_id(
    purchase_order_id: str, purchase_orders: tuple[PurchaseOrderRecord, ...]
) -> Optional[PurchaseOrderRecord]:
    matches = [
        purchase_order for purchase_order in purchase_orders if purchase_order.purchase_order_id == purchase_order_id
    ]

    if len(matches) > 1:
        raise ValueError(f"Duplicate purchase-order ID detected: {purchase_order_id}")

    return matches[0] if matches else None


def find_goods_receipts_by_po_reference(
    purchase_order_number: str,
    goods_receipts: tuple[GoodsReceiptRecord, ...],
    include_reversed: bool = False,
) -> tuple[GoodsReceiptRecord, ...]:
    purchase_order_key = normalize_reference_identifier(purchase_order_number)

    if not purchase_order_key:
        return ()

    matches = []

    for goods_receipt in goods_receipts:
        receipt_po_key = normalize_reference_identifier(goods_receipt.purchase_order_number)

        if receipt_po_key != purchase_order_key:
            continue

        if not include_reversed and goods_receipt.state == GoodsReceiptState.REVERSED:
            continue

        matches.append(goods_receipt)

    return tuple(matches)


def aggregate_received_quantities(
    goods_receipts: tuple[GoodsReceiptRecord, ...], *, config: MatchingConfig
) -> dict[int, Decimal]:
    received_quantities: dict[int, Decimal] = {}

    for goods_receipt in goods_receipts:
        if goods_receipt.state != GoodsReceiptState.POSTED:
            continue

        for receipt_line in goods_receipt.lines:
            po_line_number = receipt_line.purchase_order_line_number
            current_quantity = received_quantities.get(po_line_number, Decimal("0"))

            received_quantities[po_line_number] = (current_quantity + receipt_line.quantity_received).quantize(
                config.quantity_quantization, rounding=ROUND_HALF_UP
            )

    return received_quantities


# ============================================================
# 4. Supplier resolution (notebook cell 78)
# ============================================================


def supplier_record_score(supplier_name: str, supplier_address: Optional[str], supplier: SupplierRecord) -> Decimal:
    name_scores = [
        similarity_score(supplier_name, supplier.legal_name),
        *(similarity_score(supplier_name, alias) for alias in supplier.aliases),
    ]

    best_name_score = max(name_scores, default=Decimal("0"))

    if supplier_address and supplier.addresses:
        best_address_score = max(similarity_score(supplier_address, address) for address in supplier.addresses)

        if best_address_score >= Decimal("0.90"):
            best_name_score = min(Decimal("1.0000"), best_name_score + Decimal("0.0300"))

    return best_name_score.quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)


def identify_supplier_match_method(supplier_name: str, supplier: SupplierRecord) -> SupplierMatchMethod:
    supplier_key = normalize_business_text(supplier_name)

    if supplier_key == normalize_business_text(supplier.legal_name):
        return SupplierMatchMethod.EXACT_NAME

    if any(supplier_key == normalize_business_text(alias) for alias in supplier.aliases):
        return SupplierMatchMethod.ALIAS

    return SupplierMatchMethod.NORMALIZED_NAME


def resolve_approved_supplier(
    invoice_record: NormalizedInvoiceRecord,
    suppliers: tuple[SupplierRecord, ...],
    *,
    config: MatchingConfig,
) -> SupplierResolution:
    supplier_name = get_invoice_field_value(invoice_record, "SUPPLIER_NAME")
    supplier_address = get_invoice_field_value(invoice_record, "SUPPLIER_ADDRESS")

    if supplier_name is None:
        return SupplierResolution(
            status=ResolutionStatus.NOT_FOUND,
            matched_supplier_id=None,
            match_method=SupplierMatchMethod.NONE,
            candidate_supplier_ids=(),
            score=None,
            review_required=True,
            review_reasons=("SUPPLIER_NAME_MISSING",),
        )

    scored_suppliers = sorted(
        (
            (
                supplier_record_score(
                    str(supplier_name),
                    str(supplier_address) if supplier_address is not None else None,
                    supplier,
                ),
                supplier,
            )
            for supplier in suppliers
        ),
        key=lambda item: (item[0], item[1].supplier_id),
        reverse=True,
    )

    scored_suppliers = scored_suppliers[: config.maximum_supplier_candidates]

    eligible_candidates = [
        (score, supplier) for score, supplier in scored_suppliers if score >= config.supplier_match_threshold
    ]

    candidate_ids = tuple(supplier.supplier_id for _, supplier in eligible_candidates)

    if not eligible_candidates:
        return SupplierResolution(
            status=ResolutionStatus.NOT_FOUND,
            matched_supplier_id=None,
            match_method=SupplierMatchMethod.NONE,
            candidate_supplier_ids=tuple(supplier.supplier_id for _, supplier in scored_suppliers),
            score=scored_suppliers[0][0] if scored_suppliers else None,
            review_required=True,
            review_reasons=("APPROVED_SUPPLIER_NOT_FOUND",),
        )

    best_score, best_supplier = eligible_candidates[0]

    if len(eligible_candidates) > 1:
        second_score = eligible_candidates[1][0]

        if best_score - second_score <= config.supplier_ambiguity_margin:
            return SupplierResolution(
                status=ResolutionStatus.AMBIGUOUS,
                matched_supplier_id=None,
                match_method=SupplierMatchMethod.NONE,
                candidate_supplier_ids=candidate_ids,
                score=best_score,
                review_required=True,
                review_reasons=("AMBIGUOUS_SUPPLIER_MATCH",),
            )

    match_method = identify_supplier_match_method(str(supplier_name), best_supplier)

    if not best_supplier.active:
        return SupplierResolution(
            status=ResolutionStatus.CONFLICT,
            matched_supplier_id=best_supplier.supplier_id,
            match_method=match_method,
            candidate_supplier_ids=(best_supplier.supplier_id,),
            score=best_score,
            review_required=True,
            review_reasons=("SUPPLIER_INACTIVE",),
        )

    if not best_supplier.approved_for_payment:
        return SupplierResolution(
            status=ResolutionStatus.CONFLICT,
            matched_supplier_id=best_supplier.supplier_id,
            match_method=match_method,
            candidate_supplier_ids=(best_supplier.supplier_id,),
            score=best_score,
            review_required=True,
            review_reasons=("SUPPLIER_NOT_APPROVED_FOR_PAYMENT",),
        )

    return SupplierResolution(
        status=ResolutionStatus.MATCHED,
        matched_supplier_id=best_supplier.supplier_id,
        match_method=match_method,
        candidate_supplier_ids=(best_supplier.supplier_id,),
        score=best_score,
        review_required=False,
        review_reasons=(),
    )


# ============================================================
# 5. Purchase-order retrieval (notebook cell 78)
# ============================================================


def retrieve_referenced_purchase_order(
    invoice_record: NormalizedInvoiceRecord,
    purchase_orders: tuple[PurchaseOrderRecord, ...],
    supplier_resolution: SupplierResolution,
) -> PurchaseOrderResolution:
    purchase_order_reference = get_invoice_field_value(invoice_record, "PURCHASE_ORDER_NUMBER")

    if purchase_order_reference is None:
        return PurchaseOrderResolution(
            status=ResolutionStatus.NOT_REFERENCED,
            purchase_order_id=None,
            purchase_order_number=None,
            review_required=False,
            review_reasons=(),
        )

    matching_orders = find_purchase_orders_by_reference(purchase_order_reference, purchase_orders)

    if not matching_orders:
        return PurchaseOrderResolution(
            status=ResolutionStatus.NOT_FOUND,
            purchase_order_id=None,
            purchase_order_number=str(purchase_order_reference),
            review_required=True,
            review_reasons=("REFERENCED_PURCHASE_ORDER_NOT_FOUND",),
        )

    if len(matching_orders) > 1:
        return PurchaseOrderResolution(
            status=ResolutionStatus.AMBIGUOUS,
            purchase_order_id=None,
            purchase_order_number=str(purchase_order_reference),
            review_required=True,
            review_reasons=("DUPLICATE_PURCHASE_ORDER_REFERENCE",),
        )

    purchase_order = matching_orders[0]

    if (
        supplier_resolution.matched_supplier_id is not None
        and purchase_order.supplier_id != supplier_resolution.matched_supplier_id
    ):
        return PurchaseOrderResolution(
            status=ResolutionStatus.CONFLICT,
            purchase_order_id=purchase_order.purchase_order_id,
            purchase_order_number=purchase_order.purchase_order_number,
            review_required=True,
            review_reasons=("PURCHASE_ORDER_SUPPLIER_CONFLICT",),
        )

    if purchase_order.state == PurchaseOrderState.CANCELLED:
        return PurchaseOrderResolution(
            status=ResolutionStatus.CONFLICT,
            purchase_order_id=purchase_order.purchase_order_id,
            purchase_order_number=purchase_order.purchase_order_number,
            review_required=True,
            review_reasons=("PURCHASE_ORDER_CANCELLED",),
        )

    if purchase_order.state == PurchaseOrderState.CLOSED:
        return PurchaseOrderResolution(
            status=ResolutionStatus.CONFLICT,
            purchase_order_id=purchase_order.purchase_order_id,
            purchase_order_number=purchase_order.purchase_order_number,
            review_required=True,
            review_reasons=("PURCHASE_ORDER_CLOSED",),
        )

    return PurchaseOrderResolution(
        status=ResolutionStatus.MATCHED,
        purchase_order_id=purchase_order.purchase_order_id,
        purchase_order_number=purchase_order.purchase_order_number,
        review_required=False,
        review_reasons=(),
    )


# ============================================================
# 6. Goods-receipt retrieval and match-mode selection (notebook cell 79)
# ============================================================


def retrieve_goods_receipts(
    purchase_order_resolution: PurchaseOrderResolution,
    purchase_orders: tuple[PurchaseOrderRecord, ...],
    goods_receipts: tuple[GoodsReceiptRecord, ...],
) -> GoodsReceiptResolution:
    if purchase_order_resolution.status != ResolutionStatus.MATCHED:
        return GoodsReceiptResolution(
            status=ResolutionStatus.NOT_REFERENCED, goods_receipt_ids=(), review_required=False, review_reasons=()
        )

    purchase_order_id = purchase_order_resolution.purchase_order_id

    if purchase_order_id is None:
        return GoodsReceiptResolution(
            status=ResolutionStatus.CONFLICT,
            goods_receipt_ids=(),
            review_required=True,
            review_reasons=("MATCHED_PO_ID_MISSING",),
        )

    purchase_order = get_purchase_order_by_id(purchase_order_id, purchase_orders)

    if purchase_order is None:
        return GoodsReceiptResolution(
            status=ResolutionStatus.CONFLICT,
            goods_receipt_ids=(),
            review_required=True,
            review_reasons=("MATCHED_PO_NOT_IN_REPOSITORY",),
        )

    if not purchase_order.requires_goods_receipt:
        return GoodsReceiptResolution(
            status=ResolutionStatus.NOT_REFERENCED, goods_receipt_ids=(), review_required=False, review_reasons=()
        )

    all_receipts = find_goods_receipts_by_po_reference(
        purchase_order.purchase_order_number, goods_receipts, include_reversed=True
    )

    posted_receipts = tuple(receipt for receipt in all_receipts if receipt.state == GoodsReceiptState.POSTED)

    if not posted_receipts:
        has_reversed_receipt = any(receipt.state == GoodsReceiptState.REVERSED for receipt in all_receipts)

        reason = "GOODS_RECEIPT_REVERSED" if has_reversed_receipt else "REQUIRED_GOODS_RECEIPT_NOT_FOUND"

        return GoodsReceiptResolution(
            status=ResolutionStatus.NOT_FOUND, goods_receipt_ids=(), review_required=True, review_reasons=(reason,)
        )

    conflicting_supplier_receipts = [
        receipt.goods_receipt_id for receipt in posted_receipts if receipt.supplier_id != purchase_order.supplier_id
    ]

    if conflicting_supplier_receipts:
        return GoodsReceiptResolution(
            status=ResolutionStatus.CONFLICT,
            goods_receipt_ids=tuple(conflicting_supplier_receipts),
            review_required=True,
            review_reasons=("GOODS_RECEIPT_SUPPLIER_CONFLICT",),
        )

    valid_po_line_numbers = {purchase_order_line.line_number for purchase_order_line in purchase_order.lines}

    invalid_receipt_line_references = [
        (receipt.goods_receipt_id, receipt_line.purchase_order_line_number)
        for receipt in posted_receipts
        for receipt_line in receipt.lines
        if receipt_line.purchase_order_line_number not in valid_po_line_numbers
    ]

    if invalid_receipt_line_references:
        return GoodsReceiptResolution(
            status=ResolutionStatus.CONFLICT,
            goods_receipt_ids=tuple(receipt.goods_receipt_id for receipt in posted_receipts),
            review_required=True,
            review_reasons=("GOODS_RECEIPT_LINE_NOT_IN_PO",),
        )

    return GoodsReceiptResolution(
        status=ResolutionStatus.MATCHED,
        goods_receipt_ids=tuple(receipt.goods_receipt_id for receipt in posted_receipts),
        review_required=False,
        review_reasons=(),
    )


def determine_match_mode(
    purchase_order_resolution: PurchaseOrderResolution, purchase_orders: tuple[PurchaseOrderRecord, ...]
) -> MatchMode:
    if purchase_order_resolution.status != ResolutionStatus.MATCHED:
        return MatchMode.UNDETERMINED

    purchase_order_id = purchase_order_resolution.purchase_order_id

    if purchase_order_id is None:
        return MatchMode.UNDETERMINED

    purchase_order = get_purchase_order_by_id(purchase_order_id, purchase_orders)

    if purchase_order is None:
        return MatchMode.UNDETERMINED

    if purchase_order.requires_goods_receipt:
        return MatchMode.THREE_WAY

    return MatchMode.TWO_WAY


# ============================================================
# 7. Line matching and tolerance evaluation (notebook cell 80)
# ============================================================


def create_line_match_id(
    document_id: UUID,
    invoice_line_number: int,
    purchase_order_id: str,
    purchase_order_line_number: Optional[int],
    matching_version: str,
) -> UUID:
    identity_text = "|".join(
        (
            matching_version,
            str(document_id),
            "line-match",
            str(invoice_line_number),
            str(purchase_order_id),
            str(purchase_order_line_number),
        )
    )

    return uuid5(NAMESPACE_URL, identity_text)


def normalized_field_value(normalized_field: Any) -> Any:
    if normalized_field is None:
        return None

    normalized_value = getattr(normalized_field, "normalized_value", normalized_field)

    return unwrap_normalized_value(normalized_value)


def decimal_value(value: Any) -> Optional[Decimal]:
    value = unwrap_normalized_value(value)

    if value is None:
        return None

    if isinstance(value, Decimal):
        return value

    if isinstance(value, bool):
        return None

    text = str(value).strip()

    if not text:
        return None

    negative_parentheses = text.startswith("(") and text.endswith(")")

    text = text.replace(",", "")
    text = re.sub(r"[^0-9.\-]", "", text)

    if text in {"", "-", ".", "-."}:
        return None

    try:
        parsed_value = Decimal(text)
    except Exception:
        return None

    if negative_parentheses:
        parsed_value = -abs(parsed_value)

    return parsed_value


def quantize_money(value: Decimal, *, config: MatchingConfig) -> Decimal:
    return value.quantize(config.monetary_quantization, rounding=ROUND_HALF_UP)


def quantize_quantity(value: Decimal, *, config: MatchingConfig) -> Decimal:
    return value.quantize(config.quantity_quantization, rounding=ROUND_HALF_UP)


def values_within_tolerance(observed: Decimal, expected: Decimal, tolerance: Decimal) -> bool:
    return abs(observed - expected) <= tolerance


def append_unique_reason(reasons: list[str], reason: str) -> None:
    """Phase 6's own binding (notebook cell 80, reused by cell 82): does
    not skip falsy reasons, like Phase 5's `append_unique_reason` -- kept
    as a private helper of this module rather than imported from
    `ap_agent.tools.financial_validation` (D-9-style per-tool-module
    naming policy, docs/modularisation_map.md §3.2)."""

    if reason not in reasons:
        reasons.append(reason)


def invoice_line_description(invoice_line: Any) -> Optional[str]:
    value = normalized_field_value(getattr(invoice_line, "description", None))

    if value is None:
        return None

    text = str(value).strip()

    return text or None


def invoice_line_quantity(invoice_line: Any) -> Optional[Decimal]:
    return decimal_value(normalized_field_value(getattr(invoice_line, "quantity", None)))


def invoice_line_unit_price(invoice_line: Any) -> Optional[Decimal]:
    return decimal_value(normalized_field_value(getattr(invoice_line, "unit_price", None)))


def invoice_line_amount(invoice_line: Any) -> Optional[Decimal]:
    return decimal_value(normalized_field_value(getattr(invoice_line, "amount", None)))


def rank_purchase_order_lines(
    invoice_line: Any, purchase_order_lines: tuple[PurchaseOrderLine, ...]
) -> tuple[tuple[Decimal, PurchaseOrderLine], ...]:
    description = invoice_line_description(invoice_line)

    ranked_lines = [
        (similarity_score(description, purchase_order_line.description), purchase_order_line)
        for purchase_order_line in purchase_order_lines
    ]

    return tuple(sorted(ranked_lines, key=lambda item: (item[0], -item[1].line_number), reverse=True))


def select_purchase_order_line(
    invoice_line: Any, available_po_lines: tuple[PurchaseOrderLine, ...]
) -> tuple[Optional[PurchaseOrderLine], Decimal, str]:
    if not available_po_lines:
        return (None, Decimal("0"), "NONE")

    description = invoice_line_description(invoice_line)
    ranked_lines = rank_purchase_order_lines(invoice_line, available_po_lines)
    best_score, best_line = ranked_lines[0]

    if description is not None and best_score >= LINE_DESCRIPTION_MATCH_THRESHOLD:
        return (best_line, best_score, "DESCRIPTION")

    invoice_line_number = getattr(invoice_line, "line_number", None)

    positional_matches = [
        purchase_order_line
        for purchase_order_line in available_po_lines
        if purchase_order_line.line_number == invoice_line_number
    ]

    if len(positional_matches) == 1:
        positional_line = positional_matches[0]
        positional_score = similarity_score(description, positional_line.description)

        return (positional_line, positional_score, "LINE_NUMBER")

    return (None, best_score, "NONE")


def evaluate_invoice_line_match(
    document_id: UUID,
    invoice_line: Any,
    purchase_order: PurchaseOrderRecord,
    purchase_order_line: Optional[PurchaseOrderLine],
    description_score: Decimal,
    received_quantities: dict[int, Decimal],
    match_mode: MatchMode,
    *,
    config: MatchingConfig,
) -> LineMatchResult:
    invoice_line_number = int(getattr(invoice_line, "line_number", 0))

    review_reasons: list[str] = []

    observed_quantity = invoice_line_quantity(invoice_line)
    observed_unit_price = invoice_line_unit_price(invoice_line)
    observed_line_total = invoice_line_amount(invoice_line)

    if purchase_order_line is None:
        append_unique_reason(review_reasons, "PURCHASE_ORDER_LINE_NOT_FOUND")

        return LineMatchResult(
            line_match_id=create_line_match_id(
                document_id=document_id,
                invoice_line_number=invoice_line_number,
                purchase_order_id=purchase_order.purchase_order_id,
                purchase_order_line_number=None,
                matching_version=config.matching_version,
            ),
            invoice_line_number=invoice_line_number,
            purchase_order_line_number=None,
            description_status=MatchCheckStatus.REVIEW_REQUIRED,
            quantity_status=MatchCheckStatus.REVIEW_REQUIRED,
            unit_price_status=MatchCheckStatus.REVIEW_REQUIRED,
            line_total_status=MatchCheckStatus.REVIEW_REQUIRED,
            expected_quantity=None,
            observed_quantity=observed_quantity,
            quantity_variance=None,
            expected_unit_price=None,
            observed_unit_price=observed_unit_price,
            unit_price_variance=None,
            expected_line_total=None,
            observed_line_total=observed_line_total,
            line_total_variance=None,
            review_required=True,
            review_reasons=tuple(review_reasons),
        )

    # Description check
    if description_score >= LINE_DESCRIPTION_MATCH_THRESHOLD:
        description_status = MatchCheckStatus.PASSED
    else:
        description_status = MatchCheckStatus.REVIEW_REQUIRED
        append_unique_reason(review_reasons, "LINE_DESCRIPTION_LOW_SIMILARITY")

    # Quantity check
    ordered_quantity = quantize_quantity(purchase_order_line.quantity, config=config)
    expected_quantity = ordered_quantity
    quantity_variance = None

    if observed_quantity is None:
        quantity_status = MatchCheckStatus.REVIEW_REQUIRED
        append_unique_reason(review_reasons, "INVOICE_LINE_QUANTITY_MISSING")
    else:
        observed_quantity = quantize_quantity(observed_quantity, config=config)

        if match_mode == MatchMode.THREE_WAY:
            received_quantity = received_quantities.get(purchase_order_line.line_number)

            if received_quantity is None:
                quantity_status = MatchCheckStatus.REVIEW_REQUIRED
                expected_quantity = None
                append_unique_reason(review_reasons, "RECEIVED_QUANTITY_MISSING")
            else:
                received_quantity = quantize_quantity(received_quantity, config=config)
                expected_quantity = min(ordered_quantity, received_quantity)

                quantity_variance = (observed_quantity - expected_quantity).quantize(
                    config.quantity_quantization, rounding=ROUND_HALF_UP
                )

                invoice_within_order = observed_quantity <= (ordered_quantity + config.quantity_tolerance)
                invoice_within_receipt = observed_quantity <= (received_quantity + config.quantity_tolerance)

                if invoice_within_order and invoice_within_receipt:
                    quantity_status = MatchCheckStatus.PASSED
                else:
                    quantity_status = MatchCheckStatus.FAILED
                    append_unique_reason(review_reasons, "INVOICE_QUANTITY_EXCEEDS_ORDER_OR_RECEIPT")
        else:
            expected_quantity = ordered_quantity

            quantity_variance = (observed_quantity - ordered_quantity).quantize(
                config.quantity_quantization, rounding=ROUND_HALF_UP
            )

            if observed_quantity <= (ordered_quantity + config.quantity_tolerance):
                quantity_status = MatchCheckStatus.PASSED
            else:
                quantity_status = MatchCheckStatus.FAILED
                append_unique_reason(review_reasons, "INVOICE_QUANTITY_EXCEEDS_PURCHASE_ORDER")

    # Unit-price check
    expected_unit_price = quantize_money(purchase_order_line.unit_price, config=config)
    unit_price_variance = None

    if observed_unit_price is None:
        unit_price_status = MatchCheckStatus.REVIEW_REQUIRED
        append_unique_reason(review_reasons, "INVOICE_LINE_UNIT_PRICE_MISSING")
    else:
        observed_unit_price = quantize_money(observed_unit_price, config=config)

        unit_price_variance = (observed_unit_price - expected_unit_price).quantize(
            config.monetary_quantization, rounding=ROUND_HALF_UP
        )

        if values_within_tolerance(
            observed=observed_unit_price, expected=expected_unit_price, tolerance=config.price_tolerance
        ):
            unit_price_status = MatchCheckStatus.PASSED
        else:
            unit_price_status = MatchCheckStatus.FAILED
            append_unique_reason(review_reasons, "UNIT_PRICE_VARIANCE_EXCEEDS_TOLERANCE")

    # Line-total check
    if observed_quantity is not None and expected_unit_price is not None:
        expected_line_total = quantize_money(observed_quantity * expected_unit_price, config=config)
    else:
        expected_line_total = quantize_money(purchase_order_line.line_total, config=config)

    line_total_variance = None

    if observed_line_total is None:
        line_total_status = MatchCheckStatus.REVIEW_REQUIRED
        append_unique_reason(review_reasons, "INVOICE_LINE_TOTAL_MISSING")
    else:
        observed_line_total = quantize_money(observed_line_total, config=config)

        line_total_variance = (observed_line_total - expected_line_total).quantize(
            config.monetary_quantization, rounding=ROUND_HALF_UP
        )

        if values_within_tolerance(
            observed=observed_line_total, expected=expected_line_total, tolerance=config.total_tolerance
        ):
            line_total_status = MatchCheckStatus.PASSED
        else:
            line_total_status = MatchCheckStatus.FAILED
            append_unique_reason(review_reasons, "LINE_TOTAL_VARIANCE_EXCEEDS_TOLERANCE")

    review_required = any(
        status in {MatchCheckStatus.FAILED, MatchCheckStatus.REVIEW_REQUIRED}
        for status in (description_status, quantity_status, unit_price_status, line_total_status)
    )

    return LineMatchResult(
        line_match_id=create_line_match_id(
            document_id=document_id,
            invoice_line_number=invoice_line_number,
            purchase_order_id=purchase_order.purchase_order_id,
            purchase_order_line_number=purchase_order_line.line_number,
            matching_version=config.matching_version,
        ),
        invoice_line_number=invoice_line_number,
        purchase_order_line_number=purchase_order_line.line_number,
        description_status=description_status,
        quantity_status=quantity_status,
        unit_price_status=unit_price_status,
        line_total_status=line_total_status,
        expected_quantity=expected_quantity,
        observed_quantity=observed_quantity,
        quantity_variance=quantity_variance,
        expected_unit_price=expected_unit_price,
        observed_unit_price=observed_unit_price,
        unit_price_variance=unit_price_variance,
        expected_line_total=expected_line_total,
        observed_line_total=observed_line_total,
        line_total_variance=line_total_variance,
        review_required=review_required,
        review_reasons=tuple(review_reasons),
    )


def match_invoice_lines(
    document_id: UUID,
    invoice_record: NormalizedInvoiceRecord,
    purchase_order: PurchaseOrderRecord,
    goods_receipts: tuple[GoodsReceiptRecord, ...],
    match_mode: MatchMode,
    *,
    config: MatchingConfig,
) -> tuple[LineMatchResult, ...]:
    available_po_lines = list(purchase_order.lines)
    received_quantities = aggregate_received_quantities(goods_receipts, config=config)

    line_match_results = []

    invoice_lines = sorted(invoice_record.line_items, key=lambda line: line.line_number)

    for invoice_line in invoice_lines:
        selected_po_line, description_score, _selection_method = select_purchase_order_line(
            invoice_line, tuple(available_po_lines)
        )

        line_match_result = evaluate_invoice_line_match(
            document_id=document_id,
            invoice_line=invoice_line,
            purchase_order=purchase_order,
            purchase_order_line=selected_po_line,
            description_score=description_score,
            received_quantities=received_quantities,
            match_mode=match_mode,
            config=config,
        )

        line_match_results.append(line_match_result)

        if selected_po_line is not None:
            available_po_lines = [
                purchase_order_line
                for purchase_order_line in available_po_lines
                if purchase_order_line.line_number != selected_po_line.line_number
            ]

    return tuple(line_match_results)


# ============================================================
# 8. Phase 4/5 -> Phase 6 integrity bridge (notebook cell 82)
# ============================================================


def result_status_text(result: Any) -> str:
    status = getattr(result, "status", None)

    if isinstance(status, Enum):
        return str(status.value).upper()

    return str(status).upper()


def result_requires_review(result: Any) -> bool:
    direct_review = bool(getattr(result, "review_required", False))
    summary = getattr(result, "summary", None)
    summary_review = bool(getattr(summary, "review_required", False))
    event = getattr(result, "event", None)
    event_review = bool(getattr(event, "review_required", False))

    return direct_review or summary_review or event_review or result_status_text(result) in {
        "REVIEW_REQUIRED",
        "FAILED",
    }


def build_matching_input(
    normalization_result: NormalizationResult,
    financial_validation_result: FinancialValidationResult,
    reference_data: ReferenceDataBundle,
) -> MatchingInput:
    """Validate and bridge one Phase 4 result and its Phase 5 result into
    Phase 6 (notebook cell 82's `build_matching_input`). Fails closed with
    `MatchingIntegrityError` -- a documented, stricter replacement for the
    notebook's own bare `ValueError`s, mirroring the
    `NormalizationIntegrityError`/`FinancialValidationIntegrityError`
    precedent from M5/M6 (task §3.G: reject missing, stale, tampered or
    cross-document inputs)."""

    invoice_record = normalization_result.invoice_record

    if invoice_record is None:
        raise MatchingIntegrityError(
            "NORMALIZATION_RECORD_MISSING",
            {
                "batch_id": str(normalization_result.batch_id),
                "document_id": str(normalization_result.document_id),
            },
        )

    if normalization_result.batch_id != financial_validation_result.batch_id:
        raise MatchingIntegrityError(
            "BATCH_IDENTITY_MISMATCH",
            {
                "normalization_batch_id": str(normalization_result.batch_id),
                "financial_validation_batch_id": str(financial_validation_result.batch_id),
            },
        )

    if normalization_result.document_id != financial_validation_result.document_id:
        raise MatchingIntegrityError(
            "DOCUMENT_IDENTITY_MISMATCH",
            {
                "normalization_document_id": str(normalization_result.document_id),
                "financial_validation_document_id": str(financial_validation_result.document_id),
            },
        )

    if normalization_result.source_document_sha256 != financial_validation_result.source_document_sha256:
        raise MatchingIntegrityError(
            "SOURCE_HASH_MISMATCH: Phase 4 and Phase 5 source hashes differ.",
            {
                "normalization_source_document_sha256": normalization_result.source_document_sha256,
                "financial_validation_source_document_sha256": financial_validation_result.source_document_sha256,
            },
        )

    if invoice_record.document_id != normalization_result.document_id:
        raise MatchingIntegrityError(
            "INVOICE_RECORD_DOCUMENT_IDENTITY_MISMATCH",
            {
                "invoice_record_document_id": str(invoice_record.document_id),
                "normalization_result_document_id": str(normalization_result.document_id),
            },
        )

    if invoice_record.source_document_sha256 != normalization_result.source_document_sha256:
        raise MatchingIntegrityError(
            "INVOICE_RECORD_SOURCE_HASH_MISMATCH",
            {
                "invoice_record_source_document_sha256": invoice_record.source_document_sha256,
                "normalization_result_source_document_sha256": normalization_result.source_document_sha256,
            },
        )

    return MatchingInput(
        batch_id=normalization_result.batch_id,
        document_id=normalization_result.document_id,
        source_name=invoice_record.source_name,
        source_document_sha256=normalization_result.source_document_sha256,
        normalized_invoice=invoice_record,
        financial_validation=financial_validation_result,
        reference_data=reference_data,
    )


# ============================================================
# 9. Invoice-level summary and orchestration (notebook cell 82)
# ============================================================


def create_matching_result_id(matching_input: MatchingInput, matching_version: str) -> UUID:
    identity_text = "|".join(
        (
            matching_version,
            str(matching_input.batch_id),
            str(matching_input.document_id),
            matching_input.source_document_sha256,
            "invoice-matching-result",
        )
    )

    return uuid5(NAMESPACE_URL, identity_text)


def resolution_check_status(resolution_status: ResolutionStatus) -> MatchCheckStatus:
    if resolution_status == ResolutionStatus.MATCHED:
        return MatchCheckStatus.PASSED

    if resolution_status == ResolutionStatus.NOT_REFERENCED:
        return MatchCheckStatus.NOT_APPLICABLE

    if resolution_status in {ResolutionStatus.NOT_FOUND, ResolutionStatus.AMBIGUOUS, ResolutionStatus.CONFLICT}:
        return MatchCheckStatus.REVIEW_REQUIRED

    return MatchCheckStatus.REVIEW_REQUIRED


def append_reasons(destination: list[str], new_reasons: tuple[str, ...]) -> None:
    for reason in new_reasons:
        append_unique_reason(destination, reason)


def find_resolved_purchase_order(
    resolution: PurchaseOrderResolution, purchase_orders: tuple[PurchaseOrderRecord, ...]
) -> Optional[PurchaseOrderRecord]:
    if resolution.status != ResolutionStatus.MATCHED or resolution.purchase_order_id is None:
        return None

    return get_purchase_order_by_id(resolution.purchase_order_id, purchase_orders)


def resolved_goods_receipts(
    resolution: GoodsReceiptResolution, goods_receipts: tuple[GoodsReceiptRecord, ...]
) -> tuple[GoodsReceiptRecord, ...]:
    selected_ids = set(resolution.goods_receipt_ids)

    return tuple(receipt for receipt in goods_receipts if receipt.goods_receipt_id in selected_ids)


def evaluate_currency_status(
    invoice_record: NormalizedInvoiceRecord,
    purchase_order: Optional[PurchaseOrderRecord],
    review_reasons: list[str],
) -> MatchCheckStatus:
    if purchase_order is None:
        return MatchCheckStatus.NOT_APPLICABLE

    invoice_currency = get_invoice_field_value(invoice_record, "CURRENCY")

    if invoice_currency is None:
        append_unique_reason(review_reasons, "INVOICE_CURRENCY_MISSING")
        return MatchCheckStatus.REVIEW_REQUIRED

    invoice_currency_key = normalize_business_text(invoice_currency)
    purchase_order_currency_key = normalize_business_text(purchase_order.currency)

    if invoice_currency_key == purchase_order_currency_key:
        return MatchCheckStatus.PASSED

    append_unique_reason(review_reasons, "INVOICE_PO_CURRENCY_CONFLICT")

    return MatchCheckStatus.FAILED


def evaluate_line_matches_status(
    line_matches: tuple[LineMatchResult, ...],
    purchase_order: Optional[PurchaseOrderRecord],
    review_reasons: list[str],
) -> MatchCheckStatus:
    if purchase_order is None:
        return MatchCheckStatus.NOT_APPLICABLE

    if not line_matches:
        append_unique_reason(review_reasons, "NO_INVOICE_LINES_AVAILABLE_FOR_MATCHING")
        return MatchCheckStatus.REVIEW_REQUIRED

    line_statuses = [
        status
        for line_match in line_matches
        for status in (
            line_match.description_status,
            line_match.quantity_status,
            line_match.unit_price_status,
            line_match.line_total_status,
        )
    ]

    for line_match in line_matches:
        append_reasons(review_reasons, line_match.review_reasons)

    if MatchCheckStatus.FAILED in line_statuses:
        return MatchCheckStatus.FAILED

    if MatchCheckStatus.REVIEW_REQUIRED in line_statuses:
        return MatchCheckStatus.REVIEW_REQUIRED

    return MatchCheckStatus.PASSED


def evaluate_po_total_status(
    invoice_record: NormalizedInvoiceRecord,
    purchase_order: Optional[PurchaseOrderRecord],
    *,
    config: MatchingConfig,
    review_reasons: list[str],
) -> tuple[MatchCheckStatus, Optional[Decimal], Optional[Decimal], Optional[Decimal]]:
    if purchase_order is None:
        return (MatchCheckStatus.NOT_APPLICABLE, None, None, None)

    # The PO total is compared with the invoice subtotal, not the final
    # payable total, because tax, shipping and discounts may legitimately
    # alter the final invoice total (task §5.F).
    observed_total = decimal_value(get_invoice_field_value(invoice_record, "SUBTOTAL"))

    if observed_total is None:
        append_unique_reason(review_reasons, "INVOICE_SUBTOTAL_MISSING_FOR_PO_MATCH")

        return (
            MatchCheckStatus.REVIEW_REQUIRED,
            quantize_money(purchase_order.total_amount, config=config),
            None,
            None,
        )

    expected_total = quantize_money(purchase_order.total_amount, config=config)
    observed_total = quantize_money(observed_total, config=config)

    variance = (observed_total - expected_total).quantize(config.monetary_quantization, rounding=ROUND_HALF_UP)

    if values_within_tolerance(observed=observed_total, expected=expected_total, tolerance=config.total_tolerance):
        status = MatchCheckStatus.PASSED
    else:
        status = MatchCheckStatus.FAILED
        append_unique_reason(review_reasons, "PO_TOTAL_VARIANCE_EXCEEDS_TOLERANCE")

    return (status, expected_total, observed_total, variance)


def build_invoice_match_summary(
    match_mode: MatchMode,
    supplier_resolution: SupplierResolution,
    purchase_order_resolution: PurchaseOrderResolution,
    goods_receipt_resolution: GoodsReceiptResolution,
    invoice_record: NormalizedInvoiceRecord,
    purchase_order: Optional[PurchaseOrderRecord],
    line_matches: tuple[LineMatchResult, ...],
    *,
    config: MatchingConfig,
) -> InvoiceMatchSummary:
    review_reasons: list[str] = []

    append_reasons(review_reasons, supplier_resolution.review_reasons)
    append_reasons(review_reasons, purchase_order_resolution.review_reasons)
    append_reasons(review_reasons, goods_receipt_resolution.review_reasons)

    supplier_status = resolution_check_status(supplier_resolution.status)
    purchase_order_status = resolution_check_status(purchase_order_resolution.status)

    if match_mode == MatchMode.THREE_WAY:
        goods_receipt_status = resolution_check_status(goods_receipt_resolution.status)
    else:
        goods_receipt_status = MatchCheckStatus.NOT_APPLICABLE

    currency_status = evaluate_currency_status(invoice_record, purchase_order, review_reasons)
    line_items_status = evaluate_line_matches_status(line_matches, purchase_order, review_reasons)

    invoice_total_status, expected_total, observed_total, total_variance = evaluate_po_total_status(
        invoice_record, purchase_order, config=config, review_reasons=review_reasons
    )

    check_statuses = (
        supplier_status,
        purchase_order_status,
        goods_receipt_status,
        currency_status,
        line_items_status,
        invoice_total_status,
    )

    checks_passed = sum(status == MatchCheckStatus.PASSED for status in check_statuses)
    checks_failed = sum(status == MatchCheckStatus.FAILED for status in check_statuses)
    checks_requiring_review = sum(status == MatchCheckStatus.REVIEW_REQUIRED for status in check_statuses)
    checks_skipped = sum(
        status in {MatchCheckStatus.NOT_APPLICABLE, MatchCheckStatus.SKIPPED} for status in check_statuses
    )

    review_required = checks_failed > 0 or checks_requiring_review > 0

    return InvoiceMatchSummary(
        match_mode=match_mode,
        supplier_status=supplier_status,
        purchase_order_status=purchase_order_status,
        goods_receipt_status=goods_receipt_status,
        currency_status=currency_status,
        line_items_status=line_items_status,
        invoice_total_status=invoice_total_status,
        expected_total=expected_total,
        observed_total=observed_total,
        total_variance=total_variance,
        checks_performed=len(check_statuses),
        checks_passed=checks_passed,
        checks_failed=checks_failed,
        checks_requiring_review=checks_requiring_review,
        checks_skipped=checks_skipped,
        review_required=review_required,
        review_reasons=tuple(review_reasons),
    )


def process_invoice_matching(matching_input: MatchingInput, *, config: MatchingConfig) -> MatchingResult:
    """Public Phase 6 entry point (notebook cell 82's `process_invoice_matching`).
    Does not persist; call `persist_matching_result` separately, matching
    the Phase 5 `process_financial_validation`/`persist_financial_validation_result`
    split."""

    result_id = create_matching_result_id(matching_input, config.matching_version)
    event_time = phase_6_utc_now()

    try:
        supplier_resolution = resolve_approved_supplier(
            matching_input.normalized_invoice, matching_input.reference_data.suppliers, config=config
        )

        purchase_order_resolution = retrieve_referenced_purchase_order(
            matching_input.normalized_invoice, matching_input.reference_data.purchase_orders, supplier_resolution
        )

        match_mode = determine_match_mode(purchase_order_resolution, matching_input.reference_data.purchase_orders)

        goods_receipt_resolution = retrieve_goods_receipts(
            purchase_order_resolution,
            matching_input.reference_data.purchase_orders,
            matching_input.reference_data.goods_receipts,
        )

        purchase_order = find_resolved_purchase_order(
            purchase_order_resolution, matching_input.reference_data.purchase_orders
        )

        selected_receipts = resolved_goods_receipts(goods_receipt_resolution, matching_input.reference_data.goods_receipts)

        if purchase_order is None:
            line_matches: tuple[LineMatchResult, ...] = ()
        else:
            line_matches = match_invoice_lines(
                document_id=matching_input.document_id,
                invoice_record=matching_input.normalized_invoice,
                purchase_order=purchase_order,
                goods_receipts=selected_receipts,
                match_mode=match_mode,
                config=config,
            )

        summary = build_invoice_match_summary(
            match_mode=match_mode,
            supplier_resolution=supplier_resolution,
            purchase_order_resolution=purchase_order_resolution,
            goods_receipt_resolution=goods_receipt_resolution,
            invoice_record=matching_input.normalized_invoice,
            purchase_order=purchase_order,
            line_matches=line_matches,
            config=config,
        )

        review_reasons = list(summary.review_reasons)

        inherited_review = result_requires_review(matching_input.financial_validation)

        if inherited_review:
            append_unique_reason(review_reasons, "INHERITED_FINANCIAL_VALIDATION_REVIEW")

        review_required = (
            inherited_review
            or supplier_resolution.review_required
            or purchase_order_resolution.review_required
            or goods_receipt_resolution.review_required
            or summary.review_required
        )

        final_status = MatchingStatus.REVIEW_REQUIRED if review_required else MatchingStatus.SUCCEEDED

        event = MatchingEvent(
            event_type="REFERENCE_DATA_MATCHING",
            status=final_status,
            batch_id=matching_input.batch_id,
            document_id=matching_input.document_id,
            occurred_at=event_time,
            message=(
                "Invoice matching completed."
                if not review_required
                else "Invoice matching completed with human review required."
            ),
            review_required=review_required,
        )

        return MatchingResult(
            matching_result_id=result_id,
            batch_id=matching_input.batch_id,
            document_id=matching_input.document_id,
            source_name=matching_input.source_name,
            source_document_sha256=matching_input.source_document_sha256,
            matching_version=config.matching_version,
            status=final_status,
            supplier_resolution=supplier_resolution,
            purchase_order_resolution=purchase_order_resolution,
            goods_receipt_resolution=goods_receipt_resolution,
            line_matches=line_matches,
            summary=summary,
            event=event,
            review_required=review_required,
            review_reasons=tuple(review_reasons),
            errors=(),
        )

    except Exception as error:
        event = MatchingEvent(
            event_type="REFERENCE_DATA_MATCHING",
            status=MatchingStatus.FAILED,
            batch_id=matching_input.batch_id,
            document_id=matching_input.document_id,
            occurred_at=event_time,
            message="Invoice matching failed closed.",
            review_required=True,
        )

        return MatchingResult(
            matching_result_id=result_id,
            batch_id=matching_input.batch_id,
            document_id=matching_input.document_id,
            source_name=matching_input.source_name,
            source_document_sha256=matching_input.source_document_sha256,
            matching_version=config.matching_version,
            status=MatchingStatus.FAILED,
            supplier_resolution=None,
            purchase_order_resolution=None,
            goods_receipt_resolution=None,
            line_matches=(),
            summary=None,
            event=event,
            review_required=True,
            review_reasons=("MATCHING_PROCESSING_FAILED",),
            errors=(f"{type(error).__name__}: {error}",),
        )


# ============================================================
# 10. Serialization and persistence (notebook cell 82)
# ============================================================


def canonical_json_text(value: Any) -> str:
    return json.dumps(phase_6_json_safe(value), indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def write_text_idempotently(destination: Path, content: str) -> None:
    """Write `content` to `destination` idempotently: an identical rewrite
    is a silent no-op, a genuine collision fails closed (notebook cell 82's
    `write_text_idempotently`, already idempotent in the notebook -- a
    verbatim port, not a new M7 deviation -- but raising the structured
    `MatchingIntegrityError` instead of the notebook's bare `RuntimeError`,
    mirroring the M5/M6 precedent)."""

    destination.parent.mkdir(parents=True, exist_ok=True)

    if destination.exists():
        existing_content = destination.read_text(encoding="utf-8")

        if existing_content == content:
            return

        raise MatchingIntegrityError("PHASE_6_ARTIFACT_COLLISION", {"path": str(destination)})

    temporary_path = destination.with_name(destination.name + ".tmp")
    temporary_path.write_text(content, encoding="utf-8")
    os.replace(temporary_path, destination)


def build_reference_snapshot(result: MatchingResult, reference_data: ReferenceDataBundle) -> dict[str, Any]:
    supplier = None
    purchase_order = None
    receipts: tuple[GoodsReceiptRecord, ...] = ()

    if result.supplier_resolution is not None and result.supplier_resolution.matched_supplier_id is not None:
        supplier = get_supplier_by_id(result.supplier_resolution.matched_supplier_id, reference_data.suppliers)

    if result.purchase_order_resolution is not None and result.purchase_order_resolution.purchase_order_id is not None:
        purchase_order = get_purchase_order_by_id(
            result.purchase_order_resolution.purchase_order_id, reference_data.purchase_orders
        )

    if result.goods_receipt_resolution is not None:
        receipts = resolved_goods_receipts(result.goods_receipt_resolution, reference_data.goods_receipts)

    return {"supplier": supplier, "purchase_order": purchase_order, "goods_receipts": receipts}


def phase_6_artifact_directory(result: MatchingResult, *, config: MatchingConfig) -> Path:
    """Return the deterministic artifact directory for one document and
    matching version (mirrors `phase_5_artifact_directory`)."""

    return config.artifact_root / str(result.batch_id) / str(result.document_id) / config.matching_version


def persist_matching_result(
    result: MatchingResult, reference_data: ReferenceDataBundle, *, config: MatchingConfig
) -> Path:
    """Persist one Phase 6 result, its event, its line matches and a
    reference-data snapshot, plus an integrity manifest (notebook cell 82's
    `persist_matching_result`). The manifest file is named `manifest.json`,
    matching the notebook exactly -- a deliberate choice to keep, not
    Phase 5's `artifact_manifest.json` naming (an inconsistency already
    present between the notebook's own Phase 5 and Phase 6 cells, not
    introduced by extraction; flagged, not silently resolved, per
    docs/m7_phase_6_reference_matching_report.md)."""

    artifact_directory = phase_6_artifact_directory(result, config=config)

    result_text = canonical_json_text(result)
    event_text = canonical_json_text(result.event)

    line_matches_text = "".join(
        json.dumps(phase_6_json_safe(line_match), sort_keys=True, ensure_ascii=False) + "\n"
        for line_match in result.line_matches
    )

    reference_snapshot_text = canonical_json_text(build_reference_snapshot(result, reference_data))

    artifact_contents = {
        "matching_result.json": result_text,
        "matching_event.json": event_text,
        "line_matches.jsonl": line_matches_text,
        "reference_snapshot.json": reference_snapshot_text,
    }

    for filename, content in artifact_contents.items():
        write_text_idempotently(artifact_directory / filename, content)

    manifest = {
        "matching_result_id": str(result.matching_result_id),
        "batch_id": str(result.batch_id),
        "document_id": str(result.document_id),
        "source_document_sha256": result.source_document_sha256,
        "matching_version": result.matching_version,
        "artifacts": {
            filename: {"sha256": sha256_text(content), "bytes": len(content.encode("utf-8"))}
            for filename, content in artifact_contents.items()
        },
    }

    write_text_idempotently(artifact_directory / "manifest.json", canonical_json_text(manifest))

    return artifact_directory
