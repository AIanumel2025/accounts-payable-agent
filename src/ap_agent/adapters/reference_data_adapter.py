"""Generic JSON reference-data adapter for Phase 6 (supplier, purchase-order
and goods-receipt matching).

Loads a `ReferenceDataBundle` from a directory containing `suppliers.json`,
`purchase_orders.json` and `goods_receipts.json` (M7 task §4: "Production
code should consume typed repository interfaces or generic reference-data
inputs so that local fixtures can later be replaced by ERP/accounting
adapters"). This module contains no fixture-specific data of its own --
only the generic file-naming convention and JSON-to-dataclass parsing. The
actual controlled prototype records (the notebook's `prototype_suppliers`/
`prototype_purchase_orders`/`prototype_goods_receipts`, cells 78-79) live
under `tests/fixtures/reference_data/` (CLAUDE.md; decisions D-2/D-3), not
here. A future ERP/accounting integration would implement the same
`ReferenceDataBundle`-returning interface against a live system instead of
this JSON-file adapter, without any change to `ap_agent.tools.matching`.
"""

from __future__ import annotations

import json
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

from ap_agent.models.matching import (
    GoodsReceiptLine,
    GoodsReceiptRecord,
    GoodsReceiptState,
    PurchaseOrderLine,
    PurchaseOrderRecord,
    PurchaseOrderState,
    ReferenceDataBundle,
    SupplierRecord,
)

__all__ = ["load_reference_data_bundle"]


def _load_json_list(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []

    return json.loads(path.read_text(encoding="utf-8"))


def _supplier_from_dict(payload: dict[str, Any]) -> SupplierRecord:
    return SupplierRecord(
        supplier_id=payload["supplier_id"],
        legal_name=payload["legal_name"],
        aliases=tuple(payload.get("aliases", ())),
        tax_identifier=payload.get("tax_identifier"),
        addresses=tuple(payload.get("addresses", ())),
        default_currency=payload.get("default_currency"),
        active=payload.get("active", True),
        approved_for_payment=payload.get("approved_for_payment", True),
    )


def _purchase_order_line_from_dict(payload: dict[str, Any]) -> PurchaseOrderLine:
    return PurchaseOrderLine(
        line_number=payload["line_number"],
        description=payload["description"],
        quantity=Decimal(str(payload["quantity"])),
        unit_price=Decimal(str(payload["unit_price"])),
        line_total=Decimal(str(payload["line_total"])),
        item_code=payload.get("item_code"),
    )


def _purchase_order_from_dict(payload: dict[str, Any]) -> PurchaseOrderRecord:
    return PurchaseOrderRecord(
        purchase_order_id=payload["purchase_order_id"],
        purchase_order_number=payload["purchase_order_number"],
        supplier_id=payload["supplier_id"],
        currency=payload["currency"],
        order_date=date.fromisoformat(payload["order_date"]),
        total_amount=Decimal(str(payload["total_amount"])),
        lines=tuple(_purchase_order_line_from_dict(line) for line in payload["lines"]),
        state=PurchaseOrderState(payload.get("state", "OPEN")),
        requires_goods_receipt=payload.get("requires_goods_receipt", True),
    )


def _goods_receipt_line_from_dict(payload: dict[str, Any]) -> GoodsReceiptLine:
    return GoodsReceiptLine(
        receipt_line_number=payload["receipt_line_number"],
        purchase_order_line_number=payload["purchase_order_line_number"],
        quantity_received=Decimal(str(payload["quantity_received"])),
        item_code=payload.get("item_code"),
        description=payload.get("description"),
    )


def _goods_receipt_from_dict(payload: dict[str, Any]) -> GoodsReceiptRecord:
    return GoodsReceiptRecord(
        goods_receipt_id=payload["goods_receipt_id"],
        goods_receipt_number=payload["goods_receipt_number"],
        purchase_order_number=payload["purchase_order_number"],
        supplier_id=payload["supplier_id"],
        receipt_date=date.fromisoformat(payload["receipt_date"]),
        lines=tuple(_goods_receipt_line_from_dict(line) for line in payload["lines"]),
        state=GoodsReceiptState(payload.get("state", "POSTED")),
    )


def load_reference_data_bundle(directory: Path) -> ReferenceDataBundle:
    """Load a `ReferenceDataBundle` from `<directory>/suppliers.json`,
    `<directory>/purchase_orders.json` and `<directory>/goods_receipts.json`.
    A missing file is treated as an empty repository for that record type;
    duplicate identifiers are not de-duplicated here (fail-closed duplicate
    detection is `ap_agent.tools.matching`'s job, via `get_supplier_by_id`/
    `get_purchase_order_by_id`)."""

    suppliers = tuple(_supplier_from_dict(item) for item in _load_json_list(directory / "suppliers.json"))
    purchase_orders = tuple(
        _purchase_order_from_dict(item) for item in _load_json_list(directory / "purchase_orders.json")
    )
    goods_receipts = tuple(
        _goods_receipt_from_dict(item) for item in _load_json_list(directory / "goods_receipts.json")
    )

    return ReferenceDataBundle(suppliers=suppliers, purchase_orders=purchase_orders, goods_receipts=goods_receipts)
