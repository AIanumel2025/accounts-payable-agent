"""Phase 4 (normalisation) contracts.

Sources (active-definition table §2 `models/normalization.py`):
  - notebook cell 51 ("PHASE 4 — CELL 1"): enums, `NormalizedValue`,
    `NormalizationInput`, `EvidenceReference`, `InvoiceFieldCandidate`,
    `NormalizedInvoiceField`, `NormalizedLineItem`,
    `NormalizedInvoiceRecord`, `NormalizationEvent`, `NormalizationResult`.
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

Deviation (documented, not fixed): `OCREvidenceIndex.search()` calls
`create_comparison_key`, a Phase 4 tool function deferred to the
processing-extraction milestone (`tools/normalization.py`). The method
body is preserved verbatim; calling `search()` before that function exists
in the running process raises `NameError`. `get()` and `page()` do not
depend on it and work today.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from enum import Enum
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
