"""Phase 4 (normalisation) contracts.

Sources (active-definition table §2 `models/normalization.py`):
  - notebook cell 51 ("PHASE 4 — CELL 1"): enums, `NormalizedValue`,
    `NormalizationInput`, `EvidenceReference`, `InvoiceFieldCandidate`,
    `NormalizedInvoiceField`, `NormalizedLineItem`,
    `NormalizedInvoiceRecord`, `NormalizationEvent`, `NormalizationResult`.
  - notebook cell 53 ("PHASE 4 — CELL 2"): `clean_ocr_text` and
    `create_comparison_key`, narrowly extracted as a correction (see below;
    not part of the original M2 scope, which deferred all Phase 4
    functions to M3).
  - notebook cell 54 ("PHASE 4 — CORRECTION CELL 2A"): `OCREvidenceIndex`
    (supersedes the cell-53 definition; §3.1/§3.2).
  - notebook cell 55 ("PHASE 4 — REPLACEMENT CELL 3"): `CandidateSelection`.
  - notebook cell 57 ("PHASE 4 — CELL 4"): `LineItemCandidateGroup`.

`NormalizationConfig` and `normalization_utc_now` are not extracted here:
the config dataclass lives in `ap_agent.config.settings`, and
`normalization_utc_now` is a processing function deferred to the tools
extraction milestone, per the M2 scope (contracts only).

Deviation (documented, not fixed): `EvidenceReference.bounding_box` is
annotated `BoundingBox` but the validated Phase 4 code populates it with a
4-tuple (`extract_bounding_box`), not a `BoundingBox` instance (R-09 in the
modularisation map). The annotation is preserved verbatim.

Correction (M2, post-initial-extraction): `OCREvidenceIndex.search()` is a
public contract method and originally called `create_comparison_key`
without that function existing anywhere in `src/`, so calling it always
raised `NameError`. `create_comparison_key` (and the `clean_ocr_text` text
utility it depends on) are pure, dependency-free string functions — no
config, no I/O, no OCR/candidate logic, no notebook globals — so they are
narrowly extracted here, verbatim from cell 53, to make the contract
operational. No other Phase 4 processing function is extracted; the rest
of cell 53 (`extract_bounding_box`, `parse_decimal_value`,
`normalize_monetary_value`, `normalize_currency_code`,
`normalize_date_value`, `calculate_combined_confidence`,
`create_normalization_id`, `token_to_evidence_reference`,
`line_to_evidence_reference`, `build_ocr_evidence_index`) remains deferred
to `tools/normalization.py` in M3.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from typing import Any
from uuid import UUID

from ap_agent.models.ocr import BoundingBox, OCRDocumentResult

__all__ = [
    "NormalizationStatus",
    "InvoiceFieldName",
    "NormalizedValueType",
    "EvidenceReferenceType",
    "ExtractionMethod",
    "NormalizationInput",
    "EvidenceReference",
    "NormalizedValue",
    "InvoiceFieldCandidate",
    "NormalizedInvoiceField",
    "NormalizedLineItem",
    "NormalizedInvoiceRecord",
    "NormalizationEvent",
    "NormalizationResult",
    "clean_ocr_text",
    "create_comparison_key",
    "OCREvidenceIndex",
    "CandidateSelection",
    "LineItemCandidateGroup",
]


class NormalizationStatus(str, Enum):
    SUCCEEDED = "SUCCEEDED"
    REVIEW_REQUIRED = "REVIEW_REQUIRED"
    FAILED = "FAILED"


class InvoiceFieldName(str, Enum):
    SUPPLIER_NAME = "SUPPLIER_NAME"
    SUPPLIER_ADDRESS = "SUPPLIER_ADDRESS"
    CUSTOMER_NAME = "CUSTOMER_NAME"
    CUSTOMER_ADDRESS = "CUSTOMER_ADDRESS"

    INVOICE_NUMBER = "INVOICE_NUMBER"
    INVOICE_DATE = "INVOICE_DATE"
    DUE_DATE = "DUE_DATE"
    PURCHASE_ORDER_NUMBER = "PURCHASE_ORDER_NUMBER"

    CURRENCY = "CURRENCY"
    SUBTOTAL = "SUBTOTAL"
    TAX_AMOUNT = "TAX_AMOUNT"
    DISCOUNT_AMOUNT = "DISCOUNT_AMOUNT"
    SHIPPING_AMOUNT = "SHIPPING_AMOUNT"
    TOTAL_AMOUNT = "TOTAL_AMOUNT"

    PAYMENT_TERMS = "PAYMENT_TERMS"

    LINE_DESCRIPTION = "LINE_DESCRIPTION"
    LINE_QUANTITY = "LINE_QUANTITY"
    LINE_UNIT_PRICE = "LINE_UNIT_PRICE"
    LINE_AMOUNT = "LINE_AMOUNT"


class NormalizedValueType(str, Enum):
    TEXT = "TEXT"
    DATE = "DATE"
    DECIMAL = "DECIMAL"
    CURRENCY_CODE = "CURRENCY_CODE"


class EvidenceReferenceType(str, Enum):
    TOKEN = "TOKEN"
    LINE = "LINE"


class ExtractionMethod(str, Enum):
    LABEL_VALUE = "LABEL_VALUE"
    REGULAR_EXPRESSION = "REGULAR_EXPRESSION"
    SPATIAL_PROXIMITY = "SPATIAL_PROXIMITY"
    TABLE_STRUCTURE = "TABLE_STRUCTURE"
    COMBINED_RULES = "COMBINED_RULES"


@dataclass(frozen=True)
class NormalizationInput:
    batch_id: UUID
    document_id: UUID

    source_name: str
    source_document_sha256: str

    ocr_version: str
    ocr_result: OCRDocumentResult


@dataclass(frozen=True)
class EvidenceReference:
    reference_id: UUID
    reference_type: EvidenceReferenceType

    page_number: int
    reading_order: int

    raw_text: str
    confidence: float
    bounding_box: BoundingBox


NormalizedValue = str | date | Decimal | None


@dataclass(frozen=True)
class InvoiceFieldCandidate:
    candidate_id: UUID
    field_name: InvoiceFieldName

    raw_value: str
    proposed_value: NormalizedValue
    value_type: NormalizedValueType

    confidence: float
    extraction_method: ExtractionMethod

    evidence_references: tuple[EvidenceReference, ...]

    review_required: bool = False
    review_reasons: tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class NormalizedInvoiceField:
    field_id: UUID
    field_name: InvoiceFieldName

    raw_value: str
    normalized_value: NormalizedValue
    value_type: NormalizedValueType

    confidence: float
    extraction_method: ExtractionMethod

    evidence_references: tuple[EvidenceReference, ...]

    normalization_notes: tuple[str, ...] = field(default_factory=tuple)

    review_required: bool = False
    review_reasons: tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class NormalizedLineItem:
    line_item_id: UUID
    line_number: int

    description: NormalizedInvoiceField | None
    quantity: NormalizedInvoiceField | None
    unit_price: NormalizedInvoiceField | None
    amount: NormalizedInvoiceField | None

    currency: str | None

    confidence: float

    evidence_references: tuple[EvidenceReference, ...]

    review_required: bool = False
    review_reasons: tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class NormalizedInvoiceRecord:
    invoice_record_id: UUID

    batch_id: UUID
    document_id: UUID

    source_name: str
    source_document_sha256: str

    fields: tuple[NormalizedInvoiceField, ...]

    line_items: tuple[NormalizedLineItem, ...]

    normalization_version: str
    created_at: datetime

    review_required: bool = False
    review_reasons: tuple[str, ...] = field(default_factory=tuple)

    def get_field(
        self,
        field_name: InvoiceFieldName,
    ) -> NormalizedInvoiceField | None:
        return next(
            (
                invoice_field
                for invoice_field in self.fields
                if invoice_field.field_name == field_name
            ),
            None,
        )


@dataclass(frozen=True)
class NormalizationEvent:
    event_type: str
    status: NormalizationStatus

    batch_id: UUID
    document_id: UUID

    occurred_at: datetime
    message: str
    review_required: bool


@dataclass(frozen=True)
class NormalizationResult:
    batch_id: UUID
    document_id: UUID

    source_document_sha256: str
    ocr_version: str
    normalization_version: str

    status: NormalizationStatus

    invoice_record: NormalizedInvoiceRecord | None

    field_candidates: tuple[InvoiceFieldCandidate, ...]

    event: NormalizationEvent

    review_reasons: tuple[str, ...] = field(default_factory=tuple)

    errors: tuple[str, ...] = field(default_factory=tuple)


# ------------------------------------------------------------
# Pure text-normalisation helpers (notebook cell 53, "PHASE 4 — CELL 2").
#
# Extracted narrowly so that `OCREvidenceIndex.search()` below is
# operational. Both are pure functions of their argument: no config, no
# I/O, no notebook globals.
# ------------------------------------------------------------

WHITESPACE_PATTERN = re.compile(r"\s+")
NON_ALPHANUMERIC_PATTERN = re.compile(r"[^A-Z0-9]+")


def clean_ocr_text(value: Any) -> str:
    """Clean OCR text without changing its meaning."""

    if value is None:
        return ""

    text = unicodedata.normalize("NFKC", str(value))

    text = WHITESPACE_PATTERN.sub(" ", text)

    return text.strip()


def create_comparison_key(value: Any) -> str:
    """Create a case-insensitive key for label and value matching."""

    cleaned = clean_ocr_text(value).upper()

    return NON_ALPHANUMERIC_PATTERN.sub("", cleaned)


@dataclass(frozen=True)
class OCREvidenceIndex:
    document_id: UUID | str

    references_by_id: dict[str, EvidenceReference]

    token_references: tuple[EvidenceReference, ...]

    line_references: tuple[EvidenceReference, ...]

    references_by_page: dict[int, tuple[EvidenceReference, ...]]

    def get(self, reference_id: UUID | str) -> EvidenceReference | None:
        return self.references_by_id.get(str(reference_id))

    def page(self, page_number: int) -> tuple[EvidenceReference, ...]:
        return self.references_by_page.get(int(page_number), tuple())

    def search(
        self,
        text: str,
        *,
        exact: bool = False,
        reference_type: EvidenceReferenceType | None = None,
    ) -> tuple[EvidenceReference, ...]:
        search_key = create_comparison_key(text)

        if not search_key:
            return tuple()

        matches = []

        for reference in self.references_by_id.values():
            if (
                reference_type is not None
                and reference.reference_type != reference_type
            ):
                continue

            reference_key = create_comparison_key(reference.raw_text)

            if exact:
                matched = reference_key == search_key
            else:
                matched = search_key in reference_key

            if matched:
                matches.append(reference)

        return tuple(
            sorted(
                matches,
                key=lambda reference: (
                    reference.page_number,
                    reference.reading_order,
                    str(reference.reference_id),
                ),
            )
        )


@dataclass(frozen=True)
class CandidateSelection:
    field_name: InvoiceFieldName

    selected_candidate: InvoiceFieldCandidate | None

    all_candidates: tuple[InvoiceFieldCandidate, ...]

    ambiguous: bool
    review_reasons: tuple[str, ...]


@dataclass(frozen=True)
class LineItemCandidateGroup:
    group_id: UUID
    page_number: int
    row_number: int

    description: InvoiceFieldCandidate | None

    quantity: InvoiceFieldCandidate | None

    unit_price: InvoiceFieldCandidate | None

    amount: InvoiceFieldCandidate | None

    evidence_references: tuple[EvidenceReference, ...]

    confidence: float
    review_required: bool
    review_reasons: tuple[str, ...]
