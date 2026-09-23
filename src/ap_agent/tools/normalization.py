"""Phase 4 (invoice field extraction and normalisation) processing
functions: the Phase 3 -> Phase 4 typed bridge, deterministic ID
generation, OCR-evidence conversion, field- and line-item-candidate
extraction, candidate ranking and selection, value normalisation,
document-level orchestration and artifact persistence.

Source cells and correction/replacement precedence (task §1, §4; active-
definition table §2 `tools/normalization.py`; modularisation map §3.2):

  - Cell 51 ("PHASE 4 — CELL 1"): contracts (already extracted to
    `ap_agent.models.normalization` and `ap_agent.config.settings` in M2).
  - Cell 53 ("PHASE 4 — CELL 2"): `create_normalization_id`,
    `extract_bounding_box`, `_standardize_number_text`,
    `parse_decimal_value`, `normalize_currency_code`, `normalize_date_value`.
    `clean_ocr_text`/`create_comparison_key` were extracted to
    `models/normalization.py` in M2 (re-exported here, not duplicated —
    CLAUDE.md). Cell 53's `token_to_evidence_reference`,
    `line_to_evidence_reference`, `OCREvidenceIndex`,
    `build_ocr_evidence_index`, `calculate_combined_confidence` and
    `normalize_monetary_value` are SUPERSEDED by cell 54/60 below and are
    not extracted (§3.1 of the modularisation map: cell 53's evidence
    functions pass `evidence_id=`, which `EvidenceReference` does not
    accept).
  - Cell 54 ("PHASE 4 — CORRECTION CELL 2A"): the active
    `token_to_evidence_reference`, `line_to_evidence_reference`,
    `build_ocr_evidence_index`, `calculate_combined_confidence`.
  - Cell 55 ("PHASE 4 — REPLACEMENT CELL 3"): `_reference_geometry`,
    `_same_visual_row`, `reference_matches_label`, `reference_is_valid_label`,
    `locate_label_references`, `extract_inline_value`,
    `extract_inline_candidates`, `extract_spatial_candidates`,
    `deduplicate_candidates`, `extract_document_field_candidates`. Cell 55's
    `normalize_candidate_value`, `value_is_compatible`,
    `create_field_candidate`, `find_spatial_value_references`,
    `extract_currency_candidates` and `select_best_candidate` are
    SUPERSEDED by cell 60/61 and are not extracted.
  - Cell 57 ("PHASE 4 — CELL 4"): `create_text_candidate`,
    `find_value_below_label`, `_looks_like_party_name`,
    `extract_header_party_candidates`, `_reference_matches_any_alias`,
    `_find_table_bottom`, `_group_references_by_row`,
    `create_line_field_candidate`, `extract_page_line_item_candidates`,
    `extract_document_line_item_candidates`, `extract_all_invoice_candidates`.
    Cell 57's own `extract_labelled_text_candidates` is the **base**
    implementation — still executed (the notebook captures it as
    `_phase_4_labelled_text_before_4b` before cell 61 rebinds the name) and
    delegated to by cell 61's wrapper for SUPPLIER_NAME/CUSTOMER_NAME; it is
    extracted here as `_extract_labelled_text_candidates_base` (§3.2: "late-
    binding note"). Cell 57's `find_line_item_headers` and
    `_assign_row_columns` are SUPERSEDED by cell 61.
  - Cell 60 ("PHASE 4 — CORRECTION CELL 4A"): the active
    `normalize_monetary_value`, `normalize_invoice_number_value`,
    `normalize_candidate_value`, `value_is_compatible`,
    `create_field_candidate`, `extract_currency_candidates`,
    `candidate_ranking_key`, `select_best_candidate`. Cell 60's own
    `find_spatial_value_references` is SUPERSEDED by cell 61.
  - Cell 61 ("PHASE 4 — CORRECTION CELL 4B"): the active
    `is_standalone_monetary_reference`, `find_spatial_value_references`,
    `description_header_priority`, `find_line_item_headers`,
    `_assign_row_columns`, and the public `extract_labelled_text_candidates`
    wrapper (delegates to `_extract_labelled_text_candidates_base` for
    SUPPLIER_NAME/CUSTOMER_NAME; handles PAYMENT_TERMS and the two address
    fields itself).
  - Cell 63 ("PHASE 4 — CELL 5"): `append_unique_reason` (the Phase 4
    binding — skips falsy reasons; kept separate from the Phase 5 binding
    per decision D-9, task §10), `status_text`, `candidate_to_normalized_field`,
    `select_document_fields`, `convert_line_item_group`,
    `get_selected_currency`, `phase_4_document_directory`,
    `persist_normalization_result`, `collect_inherited_ocr_reasons`, and
    the public entry point `normalize_invoice_document`. Cell 63's
    `convert_to_json_safe` moved to `ap_agent.artifacts.serialization`, and
    its `write_json_atomically` moved to `ap_agent.artifacts.filesystem` as
    `write_json_safe_atomically` (distinct name from the Phase 2/3 writer —
    decision D-8, §3.2).

Explicit configuration (task §6; decisions D-11, R-05). The notebook read a
single hidden global `normalization_config` from 11 call sites (map §5.1).
Every function that used it here takes an explicit `config:
NormalizationConfig` parameter instead; there is no module-level config
*instance*. `docs/m5_phase_4_normalization_report.md` proves this produces
identical deterministic IDs to the notebook's hidden-global version.

Bounding-box type-consistency correction (task §7; R-09). The notebook's
`extract_bounding_box` returns a plain `(x, y, width, height)` tuple, and
the notebook's evidence-reference builders stored that tuple directly in
`EvidenceReference.bounding_box`, even though the contract's field is
annotated `BoundingBox` (M2 documented this as a deviation, not a defect,
since plain dataclasses do not validate field types at runtime). This
module resolves the deferred M2 observation: `token_to_evidence_reference`
and `line_to_evidence_reference` below wrap `extract_bounding_box`'s tuple
in a real `ap_agent.models.ocr.BoundingBox` instance, preserving the exact
coordinate values and ordering (so every deterministic ID that folds
evidence-reference identity into its hash — via `reference.reference_id`,
never the bounding-box values themselves — is unaffected). The one
notebook function that treated the field as an iterable 4-tuple,
`_reference_geometry`, is adapted (not rewritten) to read `.x`, `.y`,
`.width`, `.height` off the `BoundingBox` object and still return the same
`(x, y, width, height)` tuple of ints every caller already expects — a
type-consistency correction, not a semantic change.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import defaultdict
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from pathlib import Path
from statistics import median
from typing import Any, Iterable
from uuid import UUID, uuid5

from ap_agent.artifacts.filesystem import calculate_file_sha256, write_json_safe_atomically
from ap_agent.artifacts.serialization import convert_to_json_safe, ocr_document_result_to_dict
from ap_agent.config.settings import NormalizationConfig, OCRConfig
from ap_agent.exceptions import NormalizationIntegrityError
from ap_agent.models.normalization import (
    CandidateSelection,
    EvidenceReference,
    EvidenceReferenceType,
    ExtractionMethod,
    InvoiceFieldCandidate,
    InvoiceFieldName,
    LineItemCandidateGroup,
    NormalizationEvent,
    NormalizationInput,
    NormalizationResult,
    NormalizationStatus,
    NormalizedInvoiceField,
    NormalizedInvoiceRecord,
    NormalizedLineItem,
    NormalizedValue,
    NormalizedValueType,
    OCREvidenceIndex,
    clean_ocr_text,
    create_comparison_key,
)
from ap_agent.models.ocr import BoundingBox, OCRDocumentResult, OCRPageResult, OCRStatus
from ap_agent.models.preprocessing import PreprocessingResult
from ap_agent.tools.ocr_evidence import validate_bounding_box

__all__ = [
    "build_normalization_input",
    "verify_ocr_result_artifact_integrity",
    "verify_page_evidence_within_image_bounds",
    "normalization_utc_now",
    "create_normalization_id",
    "extract_bounding_box",
    "token_to_evidence_reference",
    "line_to_evidence_reference",
    "build_ocr_evidence_index",
    "calculate_combined_confidence",
    "parse_decimal_value",
    "normalize_monetary_value",
    "normalize_currency_code",
    "normalize_date_value",
    "normalize_invoice_number_value",
    "normalize_candidate_value",
    "value_is_compatible",
    "is_standalone_monetary_reference",
    "reference_matches_label",
    "reference_is_valid_label",
    "locate_label_references",
    "extract_inline_value",
    "create_field_candidate",
    "create_text_candidate",
    "extract_inline_candidates",
    "find_spatial_value_references",
    "extract_spatial_candidates",
    "find_value_below_label",
    "extract_currency_candidates",
    "deduplicate_candidates",
    "candidate_ranking_key",
    "select_best_candidate",
    "extract_document_field_candidates",
    "extract_header_party_candidates",
    "find_line_item_headers",
    "create_line_field_candidate",
    "extract_page_line_item_candidates",
    "extract_document_line_item_candidates",
    "extract_labelled_text_candidates",
    "extract_all_invoice_candidates",
    "candidate_to_normalized_field",
    "select_document_fields",
    "convert_line_item_group",
    "get_selected_currency",
    "collect_inherited_ocr_reasons",
    "phase_4_document_directory",
    "persist_normalization_result",
    "normalize_invoice_document",
    "FIELD_LABELS",
    "DOCUMENT_LEVEL_FIELDS",
    "EXTRACTABLE_LABELLED_FIELDS",
    "MONETARY_FIELDS",
    "DATE_FIELDS",
    "STRICT_ROW_FIELDS",
    "LINE_COLUMN_ALIASES",
]


# ============================================================
# 1. Phase 3 -> Phase 4 typed bridge (M5, task §3)
#
# New in M5: the notebook built `NormalizationInput` inline, as notebook
# orchestration code at the end of cell 63 (§5.2 rule 3 of the
# modularisation map), reading two hidden globals
# (`preprocessing_results`, `ocr_document_results`). This function is the
# explicit, typed replacement, with the integrity checks the task brief
# requires that the notebook itself never performed.
# ============================================================


def verify_ocr_result_artifact_integrity(
    ocr_result: OCRDocumentResult,
    ocr_config: OCRConfig,
) -> Path:
    """Verify the persisted Phase 3 artifact exists and matches `ocr_result`.

    `ap_agent.tools.ocr.process_ocr_document` always persists exactly the
    object it returns — the final, guarded result (decisions D-4/D-5; the
    stale pre-guard persistence defect, R-04, was already corrected in M4).
    Re-serialising `ocr_result` the same way and comparing its hash against
    the persisted file's hash therefore verifies both that the artifact
    exists and that `ocr_result` is not a stale or reconstructed object
    that diverges from what Phase 3 actually wrote to disk — it can never
    be the notebook's stale pre-guard result, because that object was never
    persisted under this path in the first place. Fails closed
    (`NormalizationIntegrityError`) on either problem.
    """

    document_directory = (
        ocr_config.artifact_root
        / str(ocr_result.batch_id)
        / str(ocr_result.document_id)
        / ocr_config.ocr_version
    )

    artifact_path = document_directory / "ocr_document_result.json"

    if not artifact_path.is_file():
        raise NormalizationIntegrityError(
            "OCR_ARTIFACT_MISSING",
            {"path": str(artifact_path)},
        )

    persisted_hash = calculate_file_sha256(artifact_path)

    expected_bytes = json.dumps(
        ocr_document_result_to_dict(ocr_result), indent=2, default=str
    ).encode("utf-8")
    expected_hash = hashlib.sha256(expected_bytes).hexdigest()

    if persisted_hash != expected_hash:
        raise NormalizationIntegrityError(
            "OCR_ARTIFACT_HASH_MISMATCH",
            {
                "path": str(artifact_path),
                "persisted_hash": persisted_hash,
                "expected_hash": expected_hash,
            },
        )

    return artifact_path


def verify_page_evidence_within_image_bounds(page_result: OCRPageResult) -> None:
    """Verify every token and evidence-line bounding box on this page stays
    within the evidence image's actual pixel dimensions (task §7).

    Phase 3's adapters already validate this at OCR construction time (both
    `paddleocr_adapter.py` and `tesseract_adapter.py` call
    `validate_bounding_box` against the evidence image before returning),
    so this is a defence-in-depth re-check for callers who want to confirm
    the evidence image on disk still matches the coordinates the OCR result
    claims — not a mandatory step of `normalize_invoice_document` (which
    never opens image files; keeping it that way lets unit tests build
    synthetic `OCRPageResult` objects with placeholder, non-existent image
    paths, per CLAUDE.md's fixtures-stay-in-tests rule and task §14).
    """

    from PIL import Image

    with Image.open(page_result.evidence_image_path) as evidence_image:
        image_width, image_height = evidence_image.size

    for token in page_result.tokens:
        validate_bounding_box(token.bounding_box, image_width, image_height)

    for evidence_line in page_result.evidence_lines:
        validate_bounding_box(evidence_line.bounding_box, image_width, image_height)


def build_normalization_input(
    *,
    ocr_result: OCRDocumentResult,
    preprocessing_result: PreprocessingResult,
    ocr_config: OCRConfig,
) -> NormalizationInput:
    """Build the Phase 3 -> Phase 4 bridge (notebook cell 63, §5.2 rule 3),
    with explicit integrity verification (task §3).

    Checks, in order (each fails closed with `NormalizationIntegrityError`):
      1. document identity continuity: `ocr_result` and `preprocessing_result`
         name the same batch and document;
      2. the OCR result belongs to the expected batch and document (its own
         `event` object is internally consistent with the top-level IDs);
      3. source-document SHA-256 continuity (rejects a mismatched result);
      4. the persisted OCR artifact exists and its hash matches `ocr_result`
         (`verify_ocr_result_artifact_integrity` — rejects a stale result).

    `source_name` and `ocr_version` are derived exactly as the notebook did:
    `source_name = Path(preprocessing_result.source_path).name`;
    `ocr_version = "phase-3:" + ",".join(sorted({page.ocr_engine for page
    in ocr_result.pages}))`.
    """

    if ocr_result.document_id != preprocessing_result.document_id:
        raise NormalizationIntegrityError(
            "DOCUMENT_IDENTITY_MISMATCH",
            {
                "ocr_document_id": str(ocr_result.document_id),
                "preprocessing_document_id": str(preprocessing_result.document_id),
            },
        )

    if ocr_result.batch_id != preprocessing_result.batch_id:
        raise NormalizationIntegrityError(
            "BATCH_IDENTITY_MISMATCH",
            {
                "ocr_batch_id": str(ocr_result.batch_id),
                "preprocessing_batch_id": str(preprocessing_result.batch_id),
            },
        )

    if (
        ocr_result.event.batch_id != ocr_result.batch_id
        or ocr_result.event.document_id != ocr_result.document_id
    ):
        raise NormalizationIntegrityError(
            "OCR_RESULT_EVENT_IDENTITY_MISMATCH",
            {
                "ocr_batch_id": str(ocr_result.batch_id),
                "ocr_document_id": str(ocr_result.document_id),
                "event_batch_id": str(ocr_result.event.batch_id),
                "event_document_id": str(ocr_result.event.document_id),
            },
        )

    if ocr_result.source_document_sha256 != preprocessing_result.source_sha256:
        raise NormalizationIntegrityError(
            "SOURCE_DOCUMENT_SHA256_MISMATCH",
            {
                "ocr_source_document_sha256": ocr_result.source_document_sha256,
                "preprocessing_source_sha256": preprocessing_result.source_sha256,
            },
        )

    verify_ocr_result_artifact_integrity(ocr_result, ocr_config)

    source_name = Path(preprocessing_result.source_path).name

    page_engines = sorted({page.ocr_engine for page in ocr_result.pages})
    ocr_version = "phase-3:" + ",".join(page_engines)

    return NormalizationInput(
        batch_id=ocr_result.batch_id,
        document_id=ocr_result.document_id,
        source_name=source_name,
        source_document_sha256=ocr_result.source_document_sha256,
        ocr_version=ocr_version,
        ocr_result=ocr_result,
    )


# ============================================================
# 2. Deterministic Phase 4 identifiers (notebook cell 53)
# ============================================================

PHASE_4_NAMESPACE = UUID("29465f12-8bd7-4b86-aab7-691e89386f55")


def normalization_utc_now():
    from datetime import datetime, timezone

    return datetime.now(timezone.utc)


def create_normalization_id(
    config: NormalizationConfig,
    entity_type: str,
    document_id: UUID | str,
    *identity_parts: Any,
) -> UUID:
    """Create a reproducible Phase 4 UUID.

    The same configured `normalization_version`, entity type, document ID
    and identity parts always produce the same UUID (task §6:
    `config.normalization_version` replaces the notebook's hidden global
    `normalization_config.normalization_version`; timestamps never enter
    this formula).
    """

    identity_text = "|".join(
        [
            config.normalization_version,
            str(entity_type).strip().lower(),
            str(document_id),
            *[str(part).strip() for part in identity_parts],
        ]
    )

    return uuid5(PHASE_4_NAMESPACE, identity_text)


# ============================================================
# 3. Bounding-box and evidence-reference conversion
#    (cell 53 for extract_bounding_box; cell 54 for the rest)
# ============================================================


def extract_bounding_box(bounding_box: Any) -> tuple[int, int, int, int]:
    """Convert a Phase 3 bounding box into `(x, y, width, height)`."""

    if bounding_box is None:
        return (0, 0, 0, 0)

    x = int(getattr(bounding_box, "x", getattr(bounding_box, "left", 0)))
    y = int(getattr(bounding_box, "y", getattr(bounding_box, "top", 0)))

    width = getattr(bounding_box, "width", None)
    height = getattr(bounding_box, "height", None)

    if width is None:
        width = int(getattr(bounding_box, "right", x)) - x

    if height is None:
        height = int(getattr(bounding_box, "bottom", y)) - y

    return (x, y, max(0, int(width)), max(0, int(height)))


def token_to_evidence_reference(token: Any) -> EvidenceReference:
    """Convert a Phase 3 OCR token into a Phase 4 evidence reference
    without modifying the original evidence.

    Bounding-box type-consistency correction (task §7): wraps
    `extract_bounding_box`'s tuple in a real `BoundingBox` instance instead
    of storing the tuple directly, preserving the exact coordinate values.
    """

    x, y, width, height = extract_bounding_box(token.bounding_box)

    return EvidenceReference(
        reference_id=token.evidence_id,
        reference_type=EvidenceReferenceType.TOKEN,
        page_number=int(token.page_number),
        reading_order=int(token.reading_order),
        raw_text=clean_ocr_text(token.text),
        confidence=float(token.confidence),
        bounding_box=BoundingBox(x=x, y=y, width=width, height=height),
    )


def line_to_evidence_reference(evidence_line: Any) -> EvidenceReference:
    """Convert a Phase 3 evidence line into a Phase 4 evidence reference."""

    x, y, width, height = extract_bounding_box(evidence_line.bounding_box)

    return EvidenceReference(
        reference_id=evidence_line.evidence_line_id,
        reference_type=EvidenceReferenceType.LINE,
        page_number=int(evidence_line.page_number),
        reading_order=int(evidence_line.reading_order),
        raw_text=clean_ocr_text(evidence_line.text),
        confidence=float(evidence_line.mean_confidence),
        bounding_box=BoundingBox(x=x, y=y, width=width, height=height),
    )


def build_ocr_evidence_index(normalization_input: NormalizationInput) -> OCREvidenceIndex:
    """Build a searchable, document-isolated evidence index from one Phase
    3 OCR result."""

    ocr_result = normalization_input.ocr_result

    if str(ocr_result.document_id) != str(normalization_input.document_id):
        raise ValueError(
            "OCR result document ID does not match the Phase 4 input document ID."
        )

    references_by_id: dict[str, EvidenceReference] = {}
    token_references: list[EvidenceReference] = []
    line_references: list[EvidenceReference] = []
    references_by_page: dict[int, list[EvidenceReference]] = defaultdict(list)

    for page_result in ocr_result.pages:
        if int(page_result.page_number) < 1:
            raise ValueError("OCR page numbers must begin at 1.")

        for token in page_result.tokens:
            reference = token_to_evidence_reference(token)
            reference_key = str(reference.reference_id)

            if reference_key in references_by_id:
                raise ValueError(f"Duplicate OCR evidence identifier: {reference_key}")

            references_by_id[reference_key] = reference
            token_references.append(reference)
            references_by_page[reference.page_number].append(reference)

        for evidence_line in page_result.evidence_lines:
            reference = line_to_evidence_reference(evidence_line)
            reference_key = str(reference.reference_id)

            if reference_key in references_by_id:
                raise ValueError(f"Duplicate OCR evidence identifier: {reference_key}")

            references_by_id[reference_key] = reference
            line_references.append(reference)
            references_by_page[reference.page_number].append(reference)

    sorted_page_references = {
        page_number: tuple(
            sorted(
                page_references,
                key=lambda reference: (
                    reference.reading_order,
                    reference.reference_type.value,
                    str(reference.reference_id),
                ),
            )
        )
        for page_number, page_references in references_by_page.items()
    }

    return OCREvidenceIndex(
        document_id=normalization_input.document_id,
        references_by_id=references_by_id,
        token_references=tuple(
            sorted(
                token_references,
                key=lambda reference: (
                    reference.page_number,
                    reference.reading_order,
                    str(reference.reference_id),
                ),
            )
        ),
        line_references=tuple(
            sorted(
                line_references,
                key=lambda reference: (
                    reference.page_number,
                    reference.reading_order,
                    str(reference.reference_id),
                ),
            )
        ),
        references_by_page=sorted_page_references,
    )


def calculate_combined_confidence(references: Iterable[EvidenceReference]) -> float:
    """Return the mean confidence of unique evidence references."""

    unique_references = {str(reference.reference_id): reference for reference in references}

    if not unique_references:
        return 0.0

    confidence_values = [
        max(0.0, min(100.0, float(reference.confidence)))
        for reference in unique_references.values()
    ]

    return round(sum(confidence_values) / len(confidence_values), 2)


# ============================================================
# 4. Decimal, monetary, currency, date and invoice-number
#    normalisation (cell 53 base + cell 60 corrections)
# ============================================================

NUMBER_PATTERN = re.compile(
    r"""
    [-+]?
    (?:
        \d{1,3}(?:[,\s.]\d{3})+
        |
        \d+
    )
    (?:[.,]\d+)?
    """,
    flags=re.VERBOSE,
)


def _standardize_number_text(number_text: str) -> str:
    """Convert supported thousands and decimal separators into Python
    Decimal notation."""

    value = number_text.strip().replace(" ", "")

    comma_count = value.count(",")
    period_count = value.count(".")

    if comma_count and period_count:
        if value.rfind(",") > value.rfind("."):
            value = value.replace(".", "")
            value = value.replace(",", ".")
        else:
            value = value.replace(",", "")

        return value

    if comma_count:
        final_group_length = len(value.rsplit(",", 1)[-1])

        if final_group_length in {1, 2}:
            value = value.replace(".", "")
            value = value.replace(",", ".")
        else:
            value = value.replace(",", "")

        return value

    if period_count > 1:
        final_group_length = len(value.rsplit(".", 1)[-1])

        if final_group_length in {1, 2}:
            pieces = value.split(".")
            value = "".join(pieces[:-1]) + "." + pieces[-1]
        else:
            value = value.replace(".", "")

    return value


def parse_decimal_value(raw_value: Any) -> Decimal | None:
    """Parse one numeric OCR value without applying financial validation or
    arithmetic."""

    text = clean_ocr_text(raw_value)

    if not text:
        return None

    is_negative = "(-)" in text or bool(re.search(r"\(\s*[$£€]?\s*\d", text))

    match = NUMBER_PATTERN.search(text)

    if match is None:
        return None

    standardized = _standardize_number_text(match.group(0))

    try:
        value = Decimal(standardized)
    except InvalidOperation:
        return None

    if is_negative and value > 0:
        value = -value

    return value


def normalize_monetary_value(raw_value: Any, *, config: NormalizationConfig) -> Decimal | None:
    """Extract the final supported monetary amount (notebook cell 60,
    supersedes cell 53's version).

    Percentage values are excluded. This prevents values such as VAT 4.24%
    from being selected instead of 36.45. When a financial summary line
    contains a tax or discount percentage before the actual amount, the
    final non-percentage number is the value attached to the field.
    """

    text = clean_ocr_text(raw_value)

    if not text:
        return None

    parsed_values = []

    for match in NUMBER_PATTERN.finditer(text):
        number_text = match.group(0)

        suffix = text[match.end() : match.end() + 4].lstrip()

        if suffix.startswith("%"):
            continue

        standardized = _standardize_number_text(number_text)

        try:
            value = Decimal(standardized)
        except InvalidOperation:
            continue

        prefix = text[max(0, match.start() - 8) : match.start()]
        local_suffix = text[match.end() : min(len(text), match.end() + 3)]

        explicitly_negative = bool(re.search(r"\(-\)\s*[$£€¥₹₦]?\s*$", prefix))
        parenthesized_amount = bool(
            re.search(r"\(\s*[$£€¥₹₦]?\s*$", prefix) and re.match(r"\s*\)", local_suffix)
        )
        leading_negative = bool(re.search(r"-\s*[$£€¥₹₦]?\s*$", prefix))

        if explicitly_negative or parenthesized_amount or leading_negative:
            value = -abs(value)

        parsed_values.append(value)

    if not parsed_values:
        return None

    selected_value = parsed_values[-1]

    return selected_value.quantize(config.monetary_quantization, rounding=ROUND_HALF_UP)


CURRENCY_CODES = {
    "USD", "EUR", "GBP", "CAD", "AUD", "NZD", "CHF", "JPY",
    "CNY", "RMB", "INR", "NGN", "ZAR", "KES", "GHS", "AED",
}

CURRENCY_ALIASES = {"RMB": "CNY"}

CURRENCY_SYMBOLS = {
    "$": "USD", "US$": "USD", "£": "GBP", "€": "EUR",
    "¥": "JPY", "₹": "INR", "₦": "NGN",
}


def normalize_currency_code(raw_value: Any, *, default: str | None = None) -> str | None:
    """Normalize an explicit currency code or symbol.

    A default is used only when the caller supplies one.
    """

    text = clean_ocr_text(raw_value).upper()

    for code in sorted(CURRENCY_CODES, key=len, reverse=True):
        if re.search(rf"(?<![A-Z]){re.escape(code)}(?![A-Z])", text):
            return CURRENCY_ALIASES.get(code, code)

    for symbol, code in sorted(CURRENCY_SYMBOLS.items(), key=lambda item: len(item[0]), reverse=True):
        if symbol.upper() in text:
            return code

    if default is None:
        return None

    normalized_default = clean_ocr_text(default).upper()
    normalized_default = CURRENCY_ALIASES.get(normalized_default, normalized_default)

    if normalized_default not in CURRENCY_CODES:
        raise ValueError(f"Unsupported default currency code: {default}")

    return normalized_default


UNAMBIGUOUS_DATE_FORMATS = (
    "%Y-%m-%d", "%Y.%m.%d", "%d-%b-%Y", "%d-%B-%Y", "%d %b %Y",
    "%d %B %Y", "%b %d %Y", "%B %d %Y", "%b %d, %Y", "%B %d, %Y",
)

NUMERIC_DATE_PATTERN = re.compile(r"(?<!\d)(\d{1,2})[/-](\d{1,2})[/-](\d{4})(?!\d)")


def normalize_date_value(raw_value: Any, *, day_first: bool | None = None):
    """Normalize an invoice date.

    Ambiguous numeric dates such as 03/06/2012 are rejected unless
    `day_first` is explicitly supplied.
    """

    from datetime import date, datetime

    text = clean_ocr_text(raw_value)

    if not text:
        return None

    text_without_label = re.sub(
        r"""
        ^\s*
        (?:
            INVOICE\s+DATE
            |
            ISSUE\s+DATE
            |
            DUE\s+DATE
            |
            DATE
        )
        \s*[:\-]?\s*
        """,
        "",
        text,
        flags=re.IGNORECASE | re.VERBOSE,
    )

    for date_format in UNAMBIGUOUS_DATE_FORMATS:
        try:
            return datetime.strptime(text_without_label, date_format).date()
        except ValueError:
            continue

    numeric_match = NUMERIC_DATE_PATTERN.search(text_without_label)

    if numeric_match is None:
        return None

    first = int(numeric_match.group(1))
    second = int(numeric_match.group(2))
    year = int(numeric_match.group(3))

    if first <= 12 and second <= 12:
        if day_first is None:
            return None

        if day_first:
            day_value, month_value = first, second
        else:
            month_value, day_value = first, second

    elif first > 12:
        day_value, month_value = first, second

    else:
        month_value, day_value = first, second

    try:
        return date(year, month_value, day_value)
    except ValueError:
        return None


INVOICE_NUMBER_LABEL_PATTERN = re.compile(
    r"""
    ^\s*
    (?:
        INVOICE\s*(?:NUMBER|NO|NUM|\#)
    )
    \s*[:#=\-–—]?\s*
    """,
    flags=re.IGNORECASE | re.VERBOSE,
)


def normalize_invoice_number_value(raw_value: Any) -> str | None:
    """Normalize an explicit invoice identifier while rejecting dates,
    amounts and labelled date values (notebook cell 60)."""

    text = clean_ocr_text(raw_value)

    if not text:
        return None

    comparison_key = create_comparison_key(text)

    if any(
        marker in comparison_key
        for marker in ("INVOICEDATE", "ISSUEDATE", "DUEDATE", "EXPIRYDATE")
    ):
        return None

    text_without_label = INVOICE_NUMBER_LABEL_PATTERN.sub("", text).strip()

    if normalize_date_value(text_without_label) is not None:
        return None

    hash_match = re.fullmatch(
        r"#\s*([A-Z0-9][A-Z0-9\-/]{2,})", text_without_label, flags=re.IGNORECASE
    )

    if hash_match:
        return hash_match.group(1).upper()

    compact_value = re.sub(r"\s+", "", text_without_label)
    compact_value = compact_value.lstrip("#")

    if re.fullmatch(r"(?=.*\d)[A-Z0-9][A-Z0-9\-/]{2,}", compact_value, flags=re.IGNORECASE) is None:
        return None

    if re.fullmatch(r"\d+[.,]\d{1,2}", compact_value):
        return None

    return compact_value.upper()


MONETARY_FIELDS = {
    InvoiceFieldName.SUBTOTAL,
    InvoiceFieldName.TAX_AMOUNT,
    InvoiceFieldName.DISCOUNT_AMOUNT,
    InvoiceFieldName.SHIPPING_AMOUNT,
    InvoiceFieldName.TOTAL_AMOUNT,
}

DATE_FIELDS = {InvoiceFieldName.INVOICE_DATE, InvoiceFieldName.DUE_DATE}


def normalize_candidate_value(
    field_name: InvoiceFieldName, raw_value: str, *, config: NormalizationConfig
) -> tuple[NormalizedValue, NormalizedValueType]:
    """Final, active field-specific value normalisation (notebook cell 60,
    overrides cell 55)."""

    if field_name in MONETARY_FIELDS:
        return (normalize_monetary_value(raw_value, config=config), NormalizedValueType.DECIMAL)

    if field_name in DATE_FIELDS:
        return (normalize_date_value(raw_value), NormalizedValueType.DATE)

    if field_name == InvoiceFieldName.INVOICE_NUMBER:
        return (normalize_invoice_number_value(raw_value), NormalizedValueType.TEXT)

    if field_name == InvoiceFieldName.CURRENCY:
        return (normalize_currency_code(raw_value), NormalizedValueType.CURRENCY_CODE)

    return (clean_ocr_text(raw_value), NormalizedValueType.TEXT)


PURCHASE_ORDER_PATTERN = re.compile(r"[A-Z0-9][A-Z0-9\-/]{3,}", flags=re.IGNORECASE)


def value_is_compatible(
    field_name: InvoiceFieldName, raw_value: str, *, config: NormalizationConfig
) -> bool:
    """Final, active field-compatibility check (notebook cell 60, overrides
    cell 55)."""

    cleaned_value = clean_ocr_text(raw_value)

    if not cleaned_value:
        return False

    if field_name in MONETARY_FIELDS:
        return normalize_monetary_value(cleaned_value, config=config) is not None

    if field_name in DATE_FIELDS:
        return normalize_date_value(cleaned_value) is not None

    if field_name == InvoiceFieldName.INVOICE_NUMBER:
        return normalize_invoice_number_value(cleaned_value) is not None

    if field_name == InvoiceFieldName.PURCHASE_ORDER_NUMBER:
        if normalize_date_value(cleaned_value) is not None:
            return False

        return bool(PURCHASE_ORDER_PATTERN.fullmatch(re.sub(r"\s+", "", cleaned_value)))

    if field_name == InvoiceFieldName.CURRENCY:
        return normalize_currency_code(cleaned_value) is not None

    return True


CURRENCY_CODE_PATTERN = re.compile(
    r"""
    (?<![A-Z])
    (
        USD|EUR|GBP|CAD|AUD|NZD|CHF|JPY|
        CNY|RMB|INR|NGN|ZAR|KES|GHS|AED
    )
    (?![A-Z])
    """,
    flags=re.IGNORECASE | re.VERBOSE,
)


def is_standalone_monetary_reference(raw_text: str, *, config: NormalizationConfig) -> bool:
    """Spatial monetary evidence should contain a number and optional
    currency notation, but not unrelated identifiers such as bank swift
    codes (notebook cell 61)."""

    text = clean_ocr_text(raw_text)

    if normalize_monetary_value(text, config=config) is None:
        return False

    remaining_text = CURRENCY_CODE_PATTERN.sub("", text)

    for symbol in CURRENCY_SYMBOLS:
        remaining_text = remaining_text.replace(symbol, "")

    remaining_text = re.sub(r"[\d\s.,()+\-]", "", remaining_text)

    return not remaining_text.strip()


# ============================================================
# 5. Evidence geometry (notebook cell 55; adapted per the
#    bounding-box type-consistency correction above)
# ============================================================


def _reference_geometry(reference: EvidenceReference) -> tuple[int, int, int, int]:
    box = reference.bounding_box
    return (int(box.x), int(box.y), int(box.width), int(box.height))


def _same_visual_row(first: EvidenceReference, second: EvidenceReference) -> bool:
    _, first_y, _, first_height = _reference_geometry(first)
    _, second_y, _, second_height = _reference_geometry(second)

    first_center_y = first_y + first_height / 2
    second_center_y = second_y + second_height / 2

    tolerance = max(first_height, second_height, 20) * 1.5

    return abs(first_center_y - second_center_y) <= tolerance


def _horizontal_distance(
    label_reference: EvidenceReference, value_reference: EvidenceReference
) -> float:
    label_x, _, label_width, _ = _reference_geometry(label_reference)
    value_x, _, _, _ = _reference_geometry(value_reference)

    label_right = label_x + label_width

    return float(abs(value_x - label_right))


def reference_matches_label(reference: EvidenceReference, label: str) -> bool:
    reference_key = create_comparison_key(reference.raw_text)
    label_key = create_comparison_key(label)

    if not reference_key or not label_key:
        return False

    return label_key in reference_key


# ============================================================
# 6. Invoice-field labels and shared constants
#    (cell 55 base, merged with cell 57's party/terms labels —
#    §5.1: "one module-level literal with the merged content")
# ============================================================

FIELD_LABELS: dict[InvoiceFieldName, tuple[str, ...]] = {
    InvoiceFieldName.INVOICE_NUMBER: ("INVOICE NUMBER", "INVOICE NO", "INVOICE NUM", "INVOICE #"),
    InvoiceFieldName.INVOICE_DATE: ("INVOICE DATE", "ISSUE DATE", "DATE"),
    InvoiceFieldName.DUE_DATE: ("DUE DATE", "EXPIRY DATE", "PAYMENT DUE"),
    InvoiceFieldName.PURCHASE_ORDER_NUMBER: (
        "PURCHASE ORDER NUMBER", "PURCHASE ORDER", "PO NUMBER", "PO NO", "ORDER ID",
    ),
    InvoiceFieldName.SUBTOTAL: ("SUB TOTAL", "SUBTOTAL", "NET TOTAL"),
    InvoiceFieldName.TAX_AMOUNT: ("TAX", "VAT", "GST"),
    InvoiceFieldName.DISCOUNT_AMOUNT: ("DISCOUNT",),
    InvoiceFieldName.SHIPPING_AMOUNT: ("SHIPPING", "DELIVERY", "FREIGHT"),
    InvoiceFieldName.TOTAL_AMOUNT: (
        "GRAND TOTAL", "AMOUNT DUE", "BALANCE DUE", "INVOICE TOTAL", "TOTAL",
    ),
    InvoiceFieldName.SUPPLIER_NAME: ("SUPPLIER", "VENDOR", "FROM", "SOLD BY"),
    InvoiceFieldName.CUSTOMER_NAME: ("BILL TO", "CUSTOMER", "BUYER"),
    InvoiceFieldName.SUPPLIER_ADDRESS: ("SUPPLIER ADDRESS", "VENDOR ADDRESS", "FROM ADDRESS"),
    InvoiceFieldName.CUSTOMER_ADDRESS: ("BILLING ADDRESS", "CUSTOMER ADDRESS", "SHIP TO"),
    InvoiceFieldName.PAYMENT_TERMS: ("PAYMENT TERMS", "TERMS"),
}

EXPLICIT_CURRENCY_PATTERN = re.compile(
    r"""
    (?:
        USD|EUR|GBP|CAD|AUD|NZD|CHF|JPY|
        CNY|RMB|INR|NGN|ZAR|KES|GHS|AED
    )
    |
    [$£€¥₹₦]
    """,
    flags=re.IGNORECASE | re.VERBOSE,
)

STRICT_ROW_FIELDS = {
    InvoiceFieldName.INVOICE_NUMBER,
    InvoiceFieldName.INVOICE_DATE,
    InvoiceFieldName.DUE_DATE,
    InvoiceFieldName.SUBTOTAL,
    InvoiceFieldName.TAX_AMOUNT,
    InvoiceFieldName.DISCOUNT_AMOUNT,
    InvoiceFieldName.SHIPPING_AMOUNT,
    InvoiceFieldName.TOTAL_AMOUNT,
}

EXTRACTABLE_LABELLED_FIELDS = (
    InvoiceFieldName.INVOICE_NUMBER,
    InvoiceFieldName.INVOICE_DATE,
    InvoiceFieldName.DUE_DATE,
    InvoiceFieldName.PURCHASE_ORDER_NUMBER,
    InvoiceFieldName.SUBTOTAL,
    InvoiceFieldName.TAX_AMOUNT,
    InvoiceFieldName.DISCOUNT_AMOUNT,
    InvoiceFieldName.SHIPPING_AMOUNT,
    InvoiceFieldName.TOTAL_AMOUNT,
)

HEADER_EXCLUSION_MARKERS = (
    "INVOICE", "DATE", "DUE", "EXPIRY", "NUMBER", "TOTAL", "SUBTOTAL",
    "TAX", "VAT", "GST", "EMAIL", "WWW", "HTTP", "TEL", "PHONE", "ADDRESS",
    "BILLTO", "SHIPTO", "BUYER", "CUSTOMER", "SUPPLIER", "VENDOR", "ITEM",
    "DESCRIPTION", "QUANTITY", "PRICE", "AMOUNT",
)

LINE_COLUMN_ALIASES = {
    InvoiceFieldName.LINE_DESCRIPTION: ("DESCRIPTION", "ITEM", "ITEMS"),
    InvoiceFieldName.LINE_QUANTITY: ("QUANTITY", "QTY"),
    InvoiceFieldName.LINE_UNIT_PRICE: ("UNIT PRICE", "RATE", "PRICE"),
    InvoiceFieldName.LINE_AMOUNT: ("AMOUNT", "LINE TOTAL", "TOTAL"),
}

TABLE_END_MARKERS = (
    "SUBTOTAL", "SUB TOTAL", "NET TOTAL", "GRAND TOTAL", "AMOUNT DUE", "BALANCE DUE",
)

ITEM_CODE_PATTERN = re.compile(r"^[A-Z0-9][A-Z0-9\-/]{1,19}$", flags=re.IGNORECASE)

OTHER_FIELD_LABEL_MARKERS = (
    "ORDERID", "PONUMBER", "PURCHASEORDER", "INVOICENUMBER", "INVOICEDATE",
    "ISSUEDATE", "DUEDATE", "EXPIRYDATE", "SUBTOTAL", "GRANDTOTAL",
    "BALANCEDUE", "AMOUNTDUE",
)

EXTRACTION_METHOD_PRIORITY = {
    ExtractionMethod.LABEL_VALUE: 5,
    ExtractionMethod.TABLE_STRUCTURE: 4,
    ExtractionMethod.REGULAR_EXPRESSION: 3,
    ExtractionMethod.SPATIAL_PROXIMITY: 2,
    ExtractionMethod.COMBINED_RULES: 1,
}

DOCUMENT_LEVEL_FIELDS = (
    InvoiceFieldName.SUPPLIER_NAME,
    InvoiceFieldName.SUPPLIER_ADDRESS,
    InvoiceFieldName.CUSTOMER_NAME,
    InvoiceFieldName.CUSTOMER_ADDRESS,
    InvoiceFieldName.INVOICE_NUMBER,
    InvoiceFieldName.INVOICE_DATE,
    InvoiceFieldName.DUE_DATE,
    InvoiceFieldName.PURCHASE_ORDER_NUMBER,
    InvoiceFieldName.CURRENCY,
    InvoiceFieldName.SUBTOTAL,
    InvoiceFieldName.TAX_AMOUNT,
    InvoiceFieldName.DISCOUNT_AMOUNT,
    InvoiceFieldName.SHIPPING_AMOUNT,
    InvoiceFieldName.TOTAL_AMOUNT,
    InvoiceFieldName.PAYMENT_TERMS,
)


# ============================================================
# 7. Field-aware label matching (notebook cell 55)
# ============================================================


def reference_is_valid_label(reference: EvidenceReference, field_name: InvoiceFieldName) -> bool:
    text_key = create_comparison_key(reference.raw_text)
    labels = FIELD_LABELS.get(field_name, tuple())

    if not any(create_comparison_key(label) in text_key for label in labels):
        return False

    if field_name == InvoiceFieldName.TOTAL_AMOUNT and any(
        excluded_label in text_key for excluded_label in ("SUBTOTAL", "NETTOTAL")
    ):
        return False

    if field_name == InvoiceFieldName.INVOICE_DATE and any(
        excluded_label in text_key for excluded_label in ("DUEDATE", "EXPIRYDATE", "PAYMENTDUE")
    ):
        return False

    if field_name == InvoiceFieldName.TAX_AMOUNT and "GSTIN" in text_key:
        return False

    return True


def locate_label_references(
    evidence_index: OCREvidenceIndex, field_name: InvoiceFieldName
) -> tuple[EvidenceReference, ...]:
    matches: dict[str, EvidenceReference] = {}

    for reference in evidence_index.line_references:
        if reference_is_valid_label(reference, field_name):
            matches[str(reference.reference_id)] = reference

    return tuple(
        sorted(
            matches.values(),
            key=lambda reference: (
                reference.page_number,
                reference.reading_order,
                str(reference.reference_id),
            ),
        )
    )


def extract_inline_value(raw_text: str, labels: tuple[str, ...]) -> str | None:
    """Extract a value appearing in the same OCR string as its field
    label."""

    cleaned_text = clean_ocr_text(raw_text)

    labels_by_length = sorted(labels, key=len, reverse=True)

    for label in labels_by_length:
        match = re.search(re.escape(label), cleaned_text, flags=re.IGNORECASE)

        if match is None:
            continue

        remainder = cleaned_text[match.end() :].strip()
        remainder = re.sub(r"^[\s:#=\-–—]+", "", remainder).strip()

        if remainder:
            return remainder

    return None


# ============================================================
# 8. Field-candidate creation (cell 60 for create_field_candidate,
#    cell 57 for create_text_candidate)
# ============================================================


def create_field_candidate(
    *,
    document_id: UUID | str,
    field_name: InvoiceFieldName,
    raw_value: str,
    evidence_references: tuple[EvidenceReference, ...],
    extraction_method: ExtractionMethod,
    rule_name: str,
    confidence_adjustment: float = 0.0,
    config: NormalizationConfig,
) -> InvoiceFieldCandidate | None:
    """Final, active typed-candidate creation (notebook cell 60, overrides
    cell 55: the candidate ID's identity material uses `str(proposed_value)`
    instead of the raw text)."""

    proposed_value, value_type = normalize_candidate_value(field_name, raw_value, config=config)

    if proposed_value is None:
        return None

    confidence = calculate_combined_confidence(evidence_references) + confidence_adjustment
    confidence = round(max(0.0, min(100.0, confidence)), 2)

    evidence_identity = "|".join(str(reference.reference_id) for reference in evidence_references)

    candidate_id = create_normalization_id(
        config,
        "field-candidate",
        document_id,
        field_name.value,
        str(proposed_value),
        evidence_identity,
        rule_name,
    )

    review_reasons = []

    if confidence < config.minimum_field_confidence:
        review_reasons.append("LOW_FIELD_CONFIDENCE")

    return InvoiceFieldCandidate(
        candidate_id=candidate_id,
        field_name=field_name,
        raw_value=clean_ocr_text(raw_value),
        proposed_value=proposed_value,
        value_type=value_type,
        confidence=confidence,
        extraction_method=extraction_method,
        evidence_references=evidence_references,
        review_required=bool(review_reasons),
        review_reasons=tuple(review_reasons),
    )


def create_text_candidate(
    *,
    document_id: UUID | str,
    field_name: InvoiceFieldName,
    raw_value: str,
    evidence_references: tuple[EvidenceReference, ...],
    extraction_method: ExtractionMethod,
    rule_name: str,
    confidence_adjustment: float = 0.0,
    config: NormalizationConfig,
) -> InvoiceFieldCandidate | None:
    cleaned_value = clean_ocr_text(raw_value)

    if not cleaned_value:
        return None

    confidence = calculate_combined_confidence(evidence_references) + confidence_adjustment
    confidence = round(max(0.0, min(100.0, confidence)), 2)

    review_reasons = []

    if confidence < config.minimum_field_confidence:
        review_reasons.append("LOW_FIELD_CONFIDENCE")

    evidence_identity = "|".join(str(reference.reference_id) for reference in evidence_references)

    candidate_id = create_normalization_id(
        config,
        "text-field-candidate",
        document_id,
        field_name.value,
        cleaned_value,
        evidence_identity,
        rule_name,
    )

    return InvoiceFieldCandidate(
        candidate_id=candidate_id,
        field_name=field_name,
        raw_value=cleaned_value,
        proposed_value=cleaned_value,
        value_type=NormalizedValueType.TEXT,
        confidence=confidence,
        extraction_method=extraction_method,
        evidence_references=evidence_references,
        review_required=bool(review_reasons),
        review_reasons=tuple(review_reasons),
    )


# ============================================================
# 9. Inline and spatial candidate extraction
#    (cell 55 for extract_inline_candidates/extract_spatial_candidates
#     and find_value_below_label@57; cell 61 for the final
#     find_spatial_value_references)
# ============================================================


def extract_inline_candidates(
    *,
    document_id: UUID | str,
    evidence_index: OCREvidenceIndex,
    field_name: InvoiceFieldName,
    config: NormalizationConfig,
) -> tuple[InvoiceFieldCandidate, ...]:
    labels = FIELD_LABELS.get(field_name, tuple())

    candidates = []

    for reference in locate_label_references(evidence_index, field_name):
        inline_value = extract_inline_value(reference.raw_text, labels)

        if inline_value is None:
            continue

        if not value_is_compatible(field_name, inline_value, config=config):
            continue

        candidate = create_field_candidate(
            document_id=document_id,
            field_name=field_name,
            raw_value=inline_value,
            evidence_references=(reference,),
            extraction_method=ExtractionMethod.LABEL_VALUE,
            rule_name="inline_label_value",
            confidence_adjustment=2.0,
            config=config,
        )

        if candidate is not None:
            candidates.append(candidate)

    return tuple(candidates)


def find_spatial_value_references(
    *,
    label_reference: EvidenceReference,
    evidence_index: OCREvidenceIndex,
    field_name: InvoiceFieldName,
    config: NormalizationConfig,
) -> tuple[EvidenceReference, ...]:
    """Final, active spatial-value search (notebook cell 61, overrides
    cells 55 and 60: due-date direction, standalone-money filter,
    positioned-after ranking)."""

    possible_values = []

    label_x, label_y, label_width, label_height = _reference_geometry(label_reference)
    label_center_y = label_y + label_height / 2

    for reference in evidence_index.line_references:
        if str(reference.reference_id) == str(label_reference.reference_id):
            continue

        if reference.page_number != label_reference.page_number:
            continue

        if not value_is_compatible(field_name, reference.raw_text, config=config):
            continue

        value_x, value_y, _, value_height = _reference_geometry(reference)
        value_center_y = value_y + value_height / 2

        same_row = _same_visual_row(label_reference, reference)

        if field_name in STRICT_ROW_FIELDS and not same_row:
            continue

        # An expiry or due-date value should occur after its label. This
        # prevents the issue date on the left from being reused as the due
        # date when the visible value is merely a dash.
        if field_name == InvoiceFieldName.DUE_DATE and value_x < label_x + label_width * 0.70:
            continue

        # Spatial financial values must resemble standalone amounts.
        # Inline labelled values are handled by the inline extractor
        # instead.
        if field_name in MONETARY_FIELDS and not is_standalone_monetary_reference(
            reference.raw_text, config=config
        ):
            continue

        reading_order_distance = abs(reference.reading_order - label_reference.reading_order)

        if not same_row and reading_order_distance > 2:
            continue

        vertical_distance = abs(value_center_y - label_center_y)
        horizontal_distance = abs(value_x - (label_x + label_width))
        positioned_after_label = value_x >= label_x + label_width * 0.60

        possible_values.append(
            (
                0 if positioned_after_label else 1,
                0 if same_row else 1,
                vertical_distance,
                horizontal_distance,
                reading_order_distance,
                reference,
            )
        )

    possible_values.sort(key=lambda item: (item[0], item[1], item[2], item[3], item[4], item[5].reading_order))

    maximum_candidates = (
        1
        if (field_name in STRICT_ROW_FIELDS or field_name == InvoiceFieldName.PURCHASE_ORDER_NUMBER)
        else 3
    )

    return tuple(item[5] for item in possible_values[:maximum_candidates])


def extract_spatial_candidates(
    *,
    document_id: UUID | str,
    evidence_index: OCREvidenceIndex,
    field_name: InvoiceFieldName,
    config: NormalizationConfig,
) -> tuple[InvoiceFieldCandidate, ...]:
    candidates = []

    for label_reference in locate_label_references(evidence_index, field_name):
        value_references = find_spatial_value_references(
            label_reference=label_reference,
            evidence_index=evidence_index,
            field_name=field_name,
            config=config,
        )

        for rank, value_reference in enumerate(value_references):
            confidence_adjustment = -2.0 * rank

            if _same_visual_row(label_reference, value_reference):
                confidence_adjustment += 1.0

            candidate = create_field_candidate(
                document_id=document_id,
                field_name=field_name,
                raw_value=value_reference.raw_text,
                evidence_references=(label_reference, value_reference),
                extraction_method=ExtractionMethod.SPATIAL_PROXIMITY,
                rule_name="label_spatial_value",
                confidence_adjustment=confidence_adjustment,
                config=config,
            )

            if candidate is not None:
                candidates.append(candidate)

    return tuple(candidates)


def find_value_below_label(
    *,
    label_reference: EvidenceReference,
    evidence_index: OCREvidenceIndex,
    maximum_candidates: int = 3,
) -> tuple[EvidenceReference, ...]:
    label_x, label_y, label_width, label_height = _reference_geometry(label_reference)
    label_center_x = label_x + label_width / 2

    candidates = []

    for reference in evidence_index.line_references:
        if str(reference.reference_id) == str(label_reference.reference_id):
            continue

        if reference.page_number != label_reference.page_number:
            continue

        value_x, value_y, value_width, _ = _reference_geometry(reference)

        if value_y < label_y:
            continue

        vertical_distance = value_y - (label_y + label_height)

        if vertical_distance > max(250, label_height * 6):
            continue

        value_center_x = value_x + value_width / 2
        horizontal_distance = abs(value_center_x - label_center_x)
        column_tolerance = max(label_width * 2.5, 300)

        if horizontal_distance > column_tolerance:
            continue

        value_key = create_comparison_key(reference.raw_text)

        if not value_key:
            continue

        if any(
            marker in value_key
            for marker in (
                "BILLTO", "SHIPTO", "SUPPLIER", "VENDOR", "CUSTOMER", "BUYER", "PAYMENTTERMS",
            )
        ):
            continue

        candidates.append((max(0, vertical_distance), horizontal_distance, reference.reading_order, reference))

    candidates.sort(key=lambda item: (item[0], item[1], item[2]))

    return tuple(item[3] for item in candidates[:maximum_candidates])


# ============================================================
# 10. Currency extraction, deduplication and ranking
#     (cell 60 for extract_currency_candidates, candidate_ranking_key
#      and select_best_candidate; cell 55 for deduplicate_candidates)
# ============================================================


def extract_currency_candidates(
    *, document_id: UUID | str, evidence_index: OCREvidenceIndex, config: NormalizationConfig
) -> tuple[InvoiceFieldCandidate, ...]:
    """Final, active currency-candidate extraction (notebook cell 60,
    overrides cell 55: code and summary-line confidence adjustments)."""

    candidates = []

    for reference in evidence_index.line_references:
        raw_text = reference.raw_text

        if not EXPLICIT_CURRENCY_PATTERN.search(raw_text):
            continue

        confidence_adjustment = 0.0

        if CURRENCY_CODE_PATTERN.search(raw_text):
            confidence_adjustment += 6.0

        text_key = create_comparison_key(raw_text)

        if any(
            marker in text_key
            for marker in ("GRANDTOTAL", "INVOICETOTAL", "BALANCEDUE", "AMOUNTDUE", "TOTAL", "SUBTOTAL")
        ):
            confidence_adjustment += 4.0

        candidate = create_field_candidate(
            document_id=document_id,
            field_name=InvoiceFieldName.CURRENCY,
            raw_value=raw_text,
            evidence_references=(reference,),
            extraction_method=ExtractionMethod.REGULAR_EXPRESSION,
            rule_name="explicit_currency_code_or_symbol",
            confidence_adjustment=confidence_adjustment,
            config=config,
        )

        if candidate is not None:
            candidates.append(candidate)

    return deduplicate_candidates(tuple(candidates))


def deduplicate_candidates(
    candidates: tuple[InvoiceFieldCandidate, ...],
) -> tuple[InvoiceFieldCandidate, ...]:
    best_by_value: dict[tuple[InvoiceFieldName, str], InvoiceFieldCandidate] = {}

    for candidate in candidates:
        key = (candidate.field_name, str(candidate.proposed_value))
        existing = best_by_value.get(key)

        if existing is None or candidate.confidence > existing.confidence:
            best_by_value[key] = candidate

    return tuple(
        sorted(
            best_by_value.values(),
            key=lambda candidate: (
                candidate.field_name.value,
                -candidate.confidence,
                str(candidate.candidate_id),
            ),
        )
    )


def candidate_ranking_key(candidate: InvoiceFieldCandidate) -> tuple[float, float, str]:
    return (
        -float(EXTRACTION_METHOD_PRIORITY.get(candidate.extraction_method, 0)),
        -float(candidate.confidence),
        str(candidate.candidate_id),
    )


def select_best_candidate(
    field_name: InvoiceFieldName,
    candidates: tuple[InvoiceFieldCandidate, ...],
    *,
    config: NormalizationConfig,
) -> CandidateSelection:
    """Final, active candidate selection (notebook cell 60, overrides cell
    55: ranking by extraction-method priority; ambiguity only within the
    same priority)."""

    field_candidates = [candidate for candidate in candidates if candidate.field_name == field_name]

    ranked_candidates = tuple(sorted(field_candidates, key=candidate_ranking_key))

    if not ranked_candidates:
        return CandidateSelection(
            field_name=field_name,
            selected_candidate=None,
            all_candidates=tuple(),
            ambiguous=False,
            review_reasons=("FIELD_NOT_EXTRACTED",),
        )

    selected_candidate = ranked_candidates[0]
    review_reasons = list(selected_candidate.review_reasons)
    ambiguous = False

    if len(ranked_candidates) > 1:
        second_candidate = ranked_candidates[1]

        same_method_priority = EXTRACTION_METHOD_PRIORITY.get(
            selected_candidate.extraction_method, 0
        ) == EXTRACTION_METHOD_PRIORITY.get(second_candidate.extraction_method, 0)

        different_values = selected_candidate.proposed_value != second_candidate.proposed_value
        score_difference = abs(selected_candidate.confidence - second_candidate.confidence)

        if same_method_priority and different_values and score_difference < config.ambiguity_score_margin:
            ambiguous = True
            append_unique_reason(review_reasons, "AMBIGUOUS_FIELD_CANDIDATES")

    return CandidateSelection(
        field_name=field_name,
        selected_candidate=selected_candidate,
        all_candidates=ranked_candidates,
        ambiguous=ambiguous,
        review_reasons=tuple(review_reasons),
    )


# ============================================================
# 11. Document-level header-field candidate discovery
#     (notebook cell 55)
# ============================================================


def extract_document_field_candidates(
    normalization_input: NormalizationInput,
    evidence_index: OCREvidenceIndex,
    *,
    config: NormalizationConfig,
) -> dict[InvoiceFieldName, tuple[InvoiceFieldCandidate, ...]]:
    candidates_by_field: dict[InvoiceFieldName, list[InvoiceFieldCandidate]] = defaultdict(list)

    for field_name in EXTRACTABLE_LABELLED_FIELDS:
        candidates_by_field[field_name].extend(
            extract_inline_candidates(
                document_id=normalization_input.document_id,
                evidence_index=evidence_index,
                field_name=field_name,
                config=config,
            )
        )

        candidates_by_field[field_name].extend(
            extract_spatial_candidates(
                document_id=normalization_input.document_id,
                evidence_index=evidence_index,
                field_name=field_name,
                config=config,
            )
        )

    candidates_by_field[InvoiceFieldName.CURRENCY].extend(
        extract_currency_candidates(
            document_id=normalization_input.document_id,
            evidence_index=evidence_index,
            config=config,
        )
    )

    return {
        field_name: deduplicate_candidates(tuple(field_candidates))
        for field_name, field_candidates in candidates_by_field.items()
    }


# ============================================================
# 12. Party, payment-term and address extraction (cell 57 base
#     + cell 61 final wrapper)
# ============================================================


def _extract_labelled_text_candidates_base(
    *,
    document_id: UUID | str,
    evidence_index: OCREvidenceIndex,
    field_name: InvoiceFieldName,
    config: NormalizationConfig,
) -> tuple[InvoiceFieldCandidate, ...]:
    """Notebook cell 57's `extract_labelled_text_candidates`.

    Still executed at the notebook's validated call point: the notebook
    captures this exact function as `_phase_4_labelled_text_before_4b`
    before cell 61 rebinds the public name, and cell 61's wrapper below
    delegates to it for SUPPLIER_NAME and CUSTOMER_NAME (§3.2 "late-binding
    note" of the modularisation map).
    """

    labels = FIELD_LABELS.get(field_name, tuple())

    candidates = []

    for label_reference in locate_label_references(evidence_index, field_name):
        inline_value = extract_inline_value(label_reference.raw_text, labels)

        if inline_value:
            candidate = create_text_candidate(
                document_id=document_id,
                field_name=field_name,
                raw_value=inline_value,
                evidence_references=(label_reference,),
                extraction_method=ExtractionMethod.LABEL_VALUE,
                rule_name="inline_text_label",
                confidence_adjustment=2.0,
                config=config,
            )

            if candidate is not None:
                candidates.append(candidate)

        for rank, value_reference in enumerate(
            find_value_below_label(label_reference=label_reference, evidence_index=evidence_index)
        ):
            candidate = create_text_candidate(
                document_id=document_id,
                field_name=field_name,
                raw_value=value_reference.raw_text,
                evidence_references=(label_reference, value_reference),
                extraction_method=ExtractionMethod.SPATIAL_PROXIMITY,
                rule_name="text_below_label",
                confidence_adjustment=-2.0 * rank,
                config=config,
            )

            if candidate is not None:
                candidates.append(candidate)

    return deduplicate_candidates(tuple(candidates))


def _looks_like_party_name(raw_text: str) -> bool:
    cleaned = clean_ocr_text(raw_text)
    comparison_key = create_comparison_key(cleaned)

    if not comparison_key:
        return False

    if any(marker in comparison_key for marker in HEADER_EXCLUSION_MARKERS):
        return False

    if re.search(r"@", cleaned):
        return False

    if re.fullmatch(r"[\d\s.,+\-/#()]+", cleaned):
        return False

    words = cleaned.split()

    if not 1 <= len(words) <= 10:
        return False

    letter_count = sum(character.isalpha() for character in cleaned)

    return letter_count >= 3


def extract_header_party_candidates(
    *, document_id: UUID | str, evidence_index: OCREvidenceIndex, config: NormalizationConfig
) -> dict[InvoiceFieldName, tuple[InvoiceFieldCandidate, ...]]:
    """Unlabelled header-party fallback (notebook cell 57)."""

    result: dict[InvoiceFieldName, list[InvoiceFieldCandidate]] = {
        InvoiceFieldName.SUPPLIER_NAME: [],
        InvoiceFieldName.CUSTOMER_NAME: [],
    }

    page_one_references = list(evidence_index.page(1))

    line_references = [
        reference
        for reference in page_one_references
        if reference.reference_type == EvidenceReferenceType.LINE
    ]

    if not line_references:
        return {field_name: tuple() for field_name in result}

    page_width = max(
        _reference_geometry(reference)[0] + _reference_geometry(reference)[2]
        for reference in line_references
    )
    page_height = max(
        _reference_geometry(reference)[1] + _reference_geometry(reference)[3]
        for reference in line_references
    )

    top_boundary = page_height * 0.35

    bill_to_y_values = [
        _reference_geometry(reference)[1]
        for reference in line_references
        if any(
            marker in create_comparison_key(reference.raw_text)
            for marker in ("BILLTO", "CUSTOMER", "BUYER")
        )
    ]

    bill_to_y = min(bill_to_y_values) if bill_to_y_values else None

    possible_references = []

    for reference in line_references:
        x, y, width, _ = _reference_geometry(reference)

        if y > top_boundary:
            continue

        if bill_to_y is not None and y >= bill_to_y:
            continue

        if not _looks_like_party_name(reference.raw_text):
            continue

        possible_references.append(reference)

    left_references = []
    right_references = []

    for reference in possible_references:
        x, _, width, _ = _reference_geometry(reference)
        center_x = x + width / 2

        if center_x < page_width / 2:
            left_references.append(reference)
        else:
            right_references.append(reference)

    left_references.sort(key=lambda reference: (_reference_geometry(reference)[1], _reference_geometry(reference)[0]))
    right_references.sort(key=lambda reference: (_reference_geometry(reference)[1], _reference_geometry(reference)[0]))

    if left_references:
        reference = left_references[0]

        candidate = create_text_candidate(
            document_id=document_id,
            field_name=InvoiceFieldName.SUPPLIER_NAME,
            raw_value=reference.raw_text,
            evidence_references=(reference,),
            extraction_method=ExtractionMethod.COMBINED_RULES,
            rule_name="unlabelled_left_header_party",
            confidence_adjustment=-8.0,
            config=config,
        )

        if candidate is not None:
            result[InvoiceFieldName.SUPPLIER_NAME].append(candidate)

    if right_references:
        reference = right_references[0]

        candidate = create_text_candidate(
            document_id=document_id,
            field_name=InvoiceFieldName.CUSTOMER_NAME,
            raw_value=reference.raw_text,
            evidence_references=(reference,),
            extraction_method=ExtractionMethod.COMBINED_RULES,
            rule_name="unlabelled_right_header_party",
            confidence_adjustment=-8.0,
            config=config,
        )

        if candidate is not None:
            result[InvoiceFieldName.CUSTOMER_NAME].append(candidate)

    return {
        field_name: deduplicate_candidates(tuple(candidates))
        for field_name, candidates in result.items()
    }


# ============================================================
# 13. Line-item header detection, row grouping and column
#     assignment (cell 57 base + cell 61 final corrections)
# ============================================================


def _reference_matches_any_alias(reference: EvidenceReference, aliases: tuple[str, ...]) -> bool:
    reference_key = create_comparison_key(reference.raw_text)
    return any(reference_key == create_comparison_key(alias) for alias in aliases)


def description_header_priority(reference: EvidenceReference) -> int:
    key = create_comparison_key(reference.raw_text)

    if key == "DESCRIPTION":
        return 0

    if key in {"ITEM", "ITEMS"}:
        return 1

    return 2


def find_line_item_headers(
    evidence_index: OCREvidenceIndex, page_number: int
) -> dict[InvoiceFieldName, EvidenceReference]:
    """Final, active line-item header detection (notebook cell 61, overrides
    cell 57: ranks candidate header rows by Description-header priority)."""

    references = [
        reference
        for reference in evidence_index.page(page_number)
        if reference.reference_type == EvidenceReferenceType.LINE
    ]

    description_headers = [
        reference
        for reference in references
        if _reference_matches_any_alias(
            reference, LINE_COLUMN_ALIASES[InvoiceFieldName.LINE_DESCRIPTION]
        )
    ]

    best_headers: dict[InvoiceFieldName, EvidenceReference] = {}
    best_ranking = None

    for description_header in description_headers:
        _, description_y, _, description_height = _reference_geometry(description_header)

        candidate_headers = {InvoiceFieldName.LINE_DESCRIPTION: description_header}
        total_y_distance = 0.0

        for field_name, aliases in LINE_COLUMN_ALIASES.items():
            if field_name == InvoiceFieldName.LINE_DESCRIPTION:
                continue

            matches = []

            for reference in references:
                if not _reference_matches_any_alias(reference, aliases):
                    continue

                _, reference_y, _, reference_height = _reference_geometry(reference)

                y_distance = abs(
                    (reference_y + reference_height / 2) - (description_y + description_height / 2)
                )

                if y_distance <= max(description_height, reference_height, 25) * 2:
                    matches.append((y_distance, reference))

            if matches:
                matches.sort(key=lambda item: (item[0], item[1].reading_order))
                candidate_headers[field_name] = matches[0][1]
                total_y_distance += matches[0][0]

        ranking = (
            -len(candidate_headers),
            description_header_priority(description_header),
            total_y_distance,
            description_header.reading_order,
        )

        if best_ranking is None or ranking < best_ranking:
            best_ranking = ranking
            best_headers = candidate_headers

    if len(best_headers) < 3:
        return {}

    return best_headers


def _find_table_bottom(references: list[EvidenceReference], header_bottom: int) -> int:
    possible_bottoms = []

    for reference in references:
        _, y, _, _ = _reference_geometry(reference)

        if y <= header_bottom:
            continue

        text_key = create_comparison_key(reference.raw_text)

        if any(create_comparison_key(marker) in text_key for marker in TABLE_END_MARKERS):
            possible_bottoms.append(y)

    if possible_bottoms:
        return min(possible_bottoms)

    return max(_reference_geometry(reference)[1] + _reference_geometry(reference)[3] for reference in references)


def _group_references_by_row(references: list[EvidenceReference]) -> list[list[EvidenceReference]]:
    if not references:
        return []

    heights = [max(1, _reference_geometry(reference)[3]) for reference in references]
    row_tolerance = max(12.0, median(heights) * 0.65)

    ordered = sorted(
        references,
        key=lambda reference: (_reference_geometry(reference)[1], _reference_geometry(reference)[0]),
    )

    rows: list[dict[str, Any]] = []

    for reference in ordered:
        _, y, _, height = _reference_geometry(reference)
        center_y = y + height / 2

        if not rows:
            rows.append({"center_y": center_y, "references": [reference]})
            continue

        nearest_row = min(rows, key=lambda row: abs(row["center_y"] - center_y))

        if abs(nearest_row["center_y"] - center_y) <= row_tolerance:
            nearest_row["references"].append(reference)

            row_centers = [
                _reference_geometry(item)[1] + _reference_geometry(item)[3] / 2
                for item in nearest_row["references"]
            ]
            nearest_row["center_y"] = sum(row_centers) / len(row_centers)
        else:
            rows.append({"center_y": center_y, "references": [reference]})

    rows.sort(key=lambda row: row["center_y"])

    return [
        sorted(row["references"], key=lambda reference: _reference_geometry(reference)[0])
        for row in rows
    ]


def _assign_row_columns(
    row_references: list[EvidenceReference], headers: dict[InvoiceFieldName, EvidenceReference]
) -> dict[InvoiceFieldName, list[EvidenceReference]]:
    """Final, active column assignment (notebook cell 61, overrides cell
    57: left-edge boundaries, standalone item-code exclusion)."""

    header_positions = [
        (_reference_geometry(reference)[0], field_name) for field_name, reference in headers.items()
    ]
    header_positions.sort(key=lambda item: item[0])

    assigned: dict[InvoiceFieldName, list[EvidenceReference]] = {
        field_name: [] for _, field_name in header_positions
    }

    description_x = next(
        x for x, field_name in header_positions if field_name == InvoiceFieldName.LINE_DESCRIPTION
    )

    boundaries = [
        (header_positions[index][0] + header_positions[index + 1][0]) / 2
        for index in range(len(header_positions) - 1)
    ]

    for reference in row_references:
        x, _, _, _ = _reference_geometry(reference)
        raw_text = clean_ocr_text(reference.raw_text)

        # The current schema has no separate SKU field. Ignore a
        # standalone code appearing to the left of the Description column
        # instead of treating it as the description or quantity.
        if x < description_x - 10 and ITEM_CODE_PATTERN.fullmatch(raw_text):
            continue

        selected_index = 0

        while selected_index < len(boundaries) and x >= boundaries[selected_index]:
            selected_index += 1

        selected_field = header_positions[selected_index][1]
        assigned[selected_field].append(reference)

    return assigned


# ============================================================
# 14. Line-item candidate construction (notebook cell 57)
# ============================================================


def create_line_field_candidate(
    *,
    document_id: UUID | str,
    field_name: InvoiceFieldName,
    references: list[EvidenceReference],
    row_number: int,
    config: NormalizationConfig,
) -> InvoiceFieldCandidate | None:
    if not references:
        return None

    ordered_references = sorted(
        references, key=lambda reference: (_reference_geometry(reference)[0], reference.reading_order)
    )

    raw_value = " ".join(reference.raw_text for reference in ordered_references)

    if field_name == InvoiceFieldName.LINE_DESCRIPTION:
        proposed_value = clean_ocr_text(raw_value)
        value_type = NormalizedValueType.TEXT
    elif field_name == InvoiceFieldName.LINE_QUANTITY:
        proposed_value = parse_decimal_value(raw_value)
        value_type = NormalizedValueType.DECIMAL
    else:
        proposed_value = normalize_monetary_value(raw_value, config=config)
        value_type = NormalizedValueType.DECIMAL

    if proposed_value is None:
        return None

    evidence_tuple = tuple(ordered_references)
    confidence = calculate_combined_confidence(evidence_tuple)

    review_reasons = []

    if confidence < config.minimum_field_confidence:
        review_reasons.append("LOW_FIELD_CONFIDENCE")

    evidence_identity = "|".join(str(reference.reference_id) for reference in evidence_tuple)

    candidate_id = create_normalization_id(
        config, "line-field-candidate", document_id, row_number, field_name.value, raw_value, evidence_identity
    )

    return InvoiceFieldCandidate(
        candidate_id=candidate_id,
        field_name=field_name,
        raw_value=clean_ocr_text(raw_value),
        proposed_value=proposed_value,
        value_type=value_type,
        confidence=confidence,
        extraction_method=ExtractionMethod.TABLE_STRUCTURE,
        evidence_references=evidence_tuple,
        review_required=bool(review_reasons),
        review_reasons=tuple(review_reasons),
    )


def extract_page_line_item_candidates(
    *, document_id: UUID | str, evidence_index: OCREvidenceIndex, page_number: int, config: NormalizationConfig
) -> tuple[LineItemCandidateGroup, ...]:
    headers = find_line_item_headers(evidence_index, page_number)

    if not headers:
        return tuple()

    page_references = [
        reference
        for reference in evidence_index.page(page_number)
        if reference.reference_type == EvidenceReferenceType.LINE
    ]

    header_bottom = max(
        _reference_geometry(reference)[1] + _reference_geometry(reference)[3]
        for reference in headers.values()
    )

    table_bottom = _find_table_bottom(page_references, header_bottom)

    header_ids = {str(reference.reference_id) for reference in headers.values()}

    body_references = []

    for reference in page_references:
        if str(reference.reference_id) in header_ids:
            continue

        _, y, _, height = _reference_geometry(reference)
        center_y = y + height / 2

        if center_y <= header_bottom or center_y >= table_bottom:
            continue

        text_key = create_comparison_key(reference.raw_text)

        if not text_key:
            continue

        if any(create_comparison_key(marker) in text_key for marker in TABLE_END_MARKERS):
            continue

        body_references.append(reference)

    grouped_rows = _group_references_by_row(body_references)

    line_groups = []

    for row_number, row_references in enumerate(grouped_rows, start=1):
        assigned_columns = _assign_row_columns(row_references, headers)

        description = create_line_field_candidate(
            document_id=document_id,
            field_name=InvoiceFieldName.LINE_DESCRIPTION,
            references=assigned_columns.get(InvoiceFieldName.LINE_DESCRIPTION, []),
            row_number=row_number,
            config=config,
        )
        quantity = create_line_field_candidate(
            document_id=document_id,
            field_name=InvoiceFieldName.LINE_QUANTITY,
            references=assigned_columns.get(InvoiceFieldName.LINE_QUANTITY, []),
            row_number=row_number,
            config=config,
        )
        unit_price = create_line_field_candidate(
            document_id=document_id,
            field_name=InvoiceFieldName.LINE_UNIT_PRICE,
            references=assigned_columns.get(InvoiceFieldName.LINE_UNIT_PRICE, []),
            row_number=row_number,
            config=config,
        )
        amount = create_line_field_candidate(
            document_id=document_id,
            field_name=InvoiceFieldName.LINE_AMOUNT,
            references=assigned_columns.get(InvoiceFieldName.LINE_AMOUNT, []),
            row_number=row_number,
            config=config,
        )

        populated_fields = [
            candidate for candidate in (description, quantity, unit_price, amount) if candidate is not None
        ]

        if len(populated_fields) < 2:
            continue

        review_reasons = []

        if description is None:
            review_reasons.append("LINE_DESCRIPTION_MISSING")

        if quantity is None:
            review_reasons.append("LINE_QUANTITY_MISSING")

        if unit_price is None and amount is None:
            review_reasons.append("LINE_FINANCIAL_VALUE_MISSING")

        for candidate in populated_fields:
            for reason in candidate.review_reasons:
                if reason not in review_reasons:
                    review_reasons.append(reason)

        unique_references = {str(reference.reference_id): reference for reference in row_references}

        evidence_tuple = tuple(
            sorted(
                unique_references.values(),
                key=lambda reference: (reference.reading_order, str(reference.reference_id)),
            )
        )

        group_id = create_normalization_id(
            config,
            "line-item-group",
            document_id,
            page_number,
            row_number,
            "|".join(str(reference.reference_id) for reference in evidence_tuple),
        )

        line_groups.append(
            LineItemCandidateGroup(
                group_id=group_id,
                page_number=page_number,
                row_number=row_number,
                description=description,
                quantity=quantity,
                unit_price=unit_price,
                amount=amount,
                evidence_references=evidence_tuple,
                confidence=calculate_combined_confidence(evidence_tuple),
                review_required=bool(review_reasons),
                review_reasons=tuple(review_reasons),
            )
        )

        if len(line_groups) >= config.maximum_line_items:
            break

    return tuple(line_groups)


def extract_document_line_item_candidates(
    *, normalization_input: NormalizationInput, evidence_index: OCREvidenceIndex, config: NormalizationConfig
) -> tuple[LineItemCandidateGroup, ...]:
    line_groups: list[LineItemCandidateGroup] = []

    for page_result in normalization_input.ocr_result.pages:
        line_groups.extend(
            extract_page_line_item_candidates(
                document_id=normalization_input.document_id,
                evidence_index=evidence_index,
                page_number=page_result.page_number,
                config=config,
            )
        )

        if len(line_groups) >= config.maximum_line_items:
            break

    return tuple(line_groups[: config.maximum_line_items])


# ============================================================
# 15. Labelled-text candidate extraction: final wrapper
#     (notebook cell 61; delegates to the cell 57 base for
#     SUPPLIER_NAME/CUSTOMER_NAME)
# ============================================================


def extract_labelled_text_candidates(
    *, document_id: UUID | str, evidence_index: OCREvidenceIndex, field_name: InvoiceFieldName, config: NormalizationConfig
) -> tuple[InvoiceFieldCandidate, ...]:
    """Final, active labelled-text extraction (notebook cell 61's public
    wrapper). Handles PAYMENT_TERMS and the two address fields itself;
    delegates everything else to `_extract_labelled_text_candidates_base`
    (the cell 57 definition — still executed, §3.2 "late-binding note")."""

    # Payment terms must be explicit. A following Order ID or unrelated
    # field must not be treated as a terms value.
    if field_name == InvoiceFieldName.PAYMENT_TERMS:
        candidates = []
        labels = FIELD_LABELS[field_name]

        for label_reference in locate_label_references(evidence_index, field_name):
            inline_value = extract_inline_value(label_reference.raw_text, labels)

            if not inline_value:
                continue

            value_key = create_comparison_key(inline_value)

            if any(marker in value_key for marker in OTHER_FIELD_LABEL_MARKERS):
                continue

            candidate = create_text_candidate(
                document_id=document_id,
                field_name=field_name,
                raw_value=inline_value,
                evidence_references=(label_reference,),
                extraction_method=ExtractionMethod.LABEL_VALUE,
                rule_name="explicit_payment_terms",
                confidence_adjustment=2.0,
                config=config,
            )

            if candidate is not None:
                candidates.append(candidate)

        return deduplicate_candidates(tuple(candidates))

    # Addresses commonly occupy several consecutive OCR lines. Combine the
    # nearby evidence into one candidate.
    if field_name in {InvoiceFieldName.SUPPLIER_ADDRESS, InvoiceFieldName.CUSTOMER_ADDRESS}:
        candidates = []
        labels = FIELD_LABELS[field_name]

        for label_reference in locate_label_references(evidence_index, field_name):
            inline_value = extract_inline_value(label_reference.raw_text, labels)

            if inline_value:
                candidate = create_text_candidate(
                    document_id=document_id,
                    field_name=field_name,
                    raw_value=inline_value,
                    evidence_references=(label_reference,),
                    extraction_method=ExtractionMethod.LABEL_VALUE,
                    rule_name="inline_address",
                    confidence_adjustment=2.0,
                    config=config,
                )

                if candidate is not None:
                    candidates.append(candidate)

            address_references = list(
                find_value_below_label(
                    label_reference=label_reference, evidence_index=evidence_index, maximum_candidates=4
                )
            )

            filtered_references = []

            for reference in address_references:
                value_key = create_comparison_key(reference.raw_text)

                if any(marker in value_key for marker in OTHER_FIELD_LABEL_MARKERS):
                    continue

                filtered_references.append(reference)

            if filtered_references:
                filtered_references.sort(
                    key=lambda reference: (_reference_geometry(reference)[1], _reference_geometry(reference)[0])
                )

                combined_address = ", ".join(
                    clean_ocr_text(reference.raw_text).rstrip(",") for reference in filtered_references
                )

                candidate = create_text_candidate(
                    document_id=document_id,
                    field_name=field_name,
                    raw_value=combined_address,
                    evidence_references=(label_reference, *filtered_references),
                    extraction_method=ExtractionMethod.COMBINED_RULES,
                    rule_name="multi_line_address",
                    config=config,
                )

                if candidate is not None:
                    candidates.append(candidate)

        return deduplicate_candidates(tuple(candidates))

    return _extract_labelled_text_candidates_base(
        document_id=document_id, evidence_index=evidence_index, field_name=field_name, config=config
    )


# ============================================================
# 16. Complete candidate discovery wrapper (notebook cell 57)
# ============================================================


def extract_all_invoice_candidates(
    normalization_input: NormalizationInput, evidence_index: OCREvidenceIndex, *, config: NormalizationConfig
) -> tuple[dict[InvoiceFieldName, tuple[InvoiceFieldCandidate, ...]], tuple[LineItemCandidateGroup, ...]]:
    field_candidates = extract_document_field_candidates(normalization_input, evidence_index, config=config)

    for field_name in (
        InvoiceFieldName.SUPPLIER_NAME,
        InvoiceFieldName.CUSTOMER_NAME,
        InvoiceFieldName.SUPPLIER_ADDRESS,
        InvoiceFieldName.CUSTOMER_ADDRESS,
        InvoiceFieldName.PAYMENT_TERMS,
    ):
        labelled_candidates = extract_labelled_text_candidates(
            document_id=normalization_input.document_id,
            evidence_index=evidence_index,
            field_name=field_name,
            config=config,
        )

        existing_candidates = field_candidates.get(field_name, tuple())
        field_candidates[field_name] = deduplicate_candidates(existing_candidates + labelled_candidates)

    header_candidates = extract_header_party_candidates(
        document_id=normalization_input.document_id, evidence_index=evidence_index, config=config
    )

    for field_name, candidates in header_candidates.items():
        field_candidates[field_name] = deduplicate_candidates(
            field_candidates.get(field_name, tuple()) + candidates
        )

    line_item_candidates = extract_document_line_item_candidates(
        normalization_input=normalization_input, evidence_index=evidence_index, config=config
    )

    return field_candidates, line_item_candidates


# ============================================================
# 17. Orchestration helpers (notebook cell 63)
# ============================================================


def append_unique_reason(reasons: list[str], reason: str) -> None:
    """Phase 4 binding: skips falsy reasons (decision D-9, task §10 — kept
    separate from the Phase 5 binding, which does not skip them)."""

    if reason and reason not in reasons:
        reasons.append(reason)


def status_text(status: Any) -> str:
    return str(getattr(status, "value", status))


def candidate_to_normalized_field(
    *,
    document_id: UUID,
    candidate: InvoiceFieldCandidate,
    selection: CandidateSelection | None = None,
    field_context: str = "document",
    config: NormalizationConfig,
) -> NormalizedInvoiceField:
    review_reasons = list(candidate.review_reasons)

    normalization_notes = [f"Selected from deterministic {field_context} extraction."]

    if selection is not None:
        for reason in selection.review_reasons:
            append_unique_reason(review_reasons, reason)

        normalization_notes.append(f"Candidate count: {len(selection.all_candidates)}.")

        if selection.ambiguous:
            normalization_notes.append(
                "Competing candidate values were within the configured ambiguity margin."
            )

    field_id = create_normalization_id(
        config, "normalized-field", document_id, field_context, candidate.field_name.value, candidate.candidate_id
    )

    return NormalizedInvoiceField(
        field_id=field_id,
        field_name=candidate.field_name,
        raw_value=candidate.raw_value,
        normalized_value=candidate.proposed_value,
        value_type=candidate.value_type,
        confidence=candidate.confidence,
        extraction_method=candidate.extraction_method,
        evidence_references=candidate.evidence_references,
        normalization_notes=tuple(normalization_notes),
        review_required=bool(review_reasons),
        review_reasons=tuple(review_reasons),
    )


def select_document_fields(
    *,
    document_id: UUID,
    candidates_by_field: dict[InvoiceFieldName, tuple[InvoiceFieldCandidate, ...]],
    config: NormalizationConfig,
) -> tuple[tuple[NormalizedInvoiceField, ...], dict[InvoiceFieldName, CandidateSelection], tuple[str, ...]]:
    normalized_fields = []
    selections: dict[InvoiceFieldName, CandidateSelection] = {}
    document_reasons: list[str] = []

    for field_name in DOCUMENT_LEVEL_FIELDS:
        selection = select_best_candidate(field_name, candidates_by_field.get(field_name, tuple()), config=config)
        selections[field_name] = selection

        if selection.selected_candidate is None:
            if field_name in config.required_fields:
                append_unique_reason(document_reasons, f"REQUIRED_FIELD_MISSING:{field_name.value}")

            continue

        normalized_field = candidate_to_normalized_field(
            document_id=document_id,
            candidate=selection.selected_candidate,
            selection=selection,
            field_context="invoice-header",
            config=config,
        )

        normalized_fields.append(normalized_field)

        if normalized_field.review_required:
            for reason in normalized_field.review_reasons:
                append_unique_reason(document_reasons, f"{field_name.value}:{reason}")

    return tuple(normalized_fields), selections, tuple(document_reasons)


def convert_line_item_group(
    *, document_id: UUID, group: LineItemCandidateGroup, invoice_currency: str | None, config: NormalizationConfig
) -> NormalizedLineItem:
    line_fields: dict[InvoiceFieldName, NormalizedInvoiceField | None] = {}

    for field_name, candidate in (
        (InvoiceFieldName.LINE_DESCRIPTION, group.description),
        (InvoiceFieldName.LINE_QUANTITY, group.quantity),
        (InvoiceFieldName.LINE_UNIT_PRICE, group.unit_price),
        (InvoiceFieldName.LINE_AMOUNT, group.amount),
    ):
        if candidate is None:
            line_fields[field_name] = None
            continue

        line_fields[field_name] = candidate_to_normalized_field(
            document_id=document_id,
            candidate=candidate,
            selection=None,
            field_context=f"line-{group.row_number}",
            config=config,
        )

    line_reasons = list(group.review_reasons)

    for normalized_field in line_fields.values():
        if normalized_field is None or not normalized_field.review_required:
            continue

        for reason in normalized_field.review_reasons:
            append_unique_reason(line_reasons, reason)

    line_item_id = create_normalization_id(
        config, "normalized-line-item", document_id, group.page_number, group.row_number, group.group_id
    )

    return NormalizedLineItem(
        line_item_id=line_item_id,
        line_number=group.row_number,
        description=line_fields[InvoiceFieldName.LINE_DESCRIPTION],
        quantity=line_fields[InvoiceFieldName.LINE_QUANTITY],
        unit_price=line_fields[InvoiceFieldName.LINE_UNIT_PRICE],
        amount=line_fields[InvoiceFieldName.LINE_AMOUNT],
        currency=invoice_currency,
        confidence=group.confidence,
        evidence_references=group.evidence_references,
        review_required=bool(line_reasons),
        review_reasons=tuple(line_reasons),
    )


def get_selected_currency(normalized_fields: tuple[NormalizedInvoiceField, ...]) -> str | None:
    for normalized_field in normalized_fields:
        if normalized_field.field_name == InvoiceFieldName.CURRENCY:
            value = normalized_field.normalized_value
            return str(value) if value is not None else None

    return None


def collect_inherited_ocr_reasons(ocr_result: OCRDocumentResult) -> tuple[str, ...]:
    reasons: list[str] = []

    if status_text(ocr_result.status) != OCRStatus.SUCCEEDED.value:
        append_unique_reason(reasons, f"INHERITED_OCR_STATUS:{status_text(ocr_result.status)}")

    if bool(getattr(ocr_result.event, "review_required", False)):
        append_unique_reason(reasons, "INHERITED_OCR_REVIEW_REQUIRED")

    for page_result in ocr_result.pages:
        for reason in page_result.review_reasons:
            append_unique_reason(reasons, f"OCR_PAGE_{page_result.page_number}:{reason}")

    return tuple(reasons)


# ============================================================
# 18. Artifact persistence (notebook cell 63)
# ============================================================


def phase_4_document_directory(normalization_input: NormalizationInput, *, config: NormalizationConfig) -> Path:
    return (
        config.artifact_root
        / str(normalization_input.batch_id)
        / str(normalization_input.document_id)
        / config.normalization_version
    )


_TIMESTAMP_KEYS = {"created_at", "occurred_at"}


def _strip_timestamps(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _strip_timestamps(nested) for key, nested in value.items() if key not in _TIMESTAMP_KEYS}

    if isinstance(value, list):
        return [_strip_timestamps(item) for item in value]

    return value


def _write_json_safe_idempotent(destination: Path, payload: Any) -> None:
    """Persist idempotently: an identical rewrite (ignoring timestamp
    fields, which always advance) is a silent no-op; a collision with
    genuinely different business content fails closed (task §12 — new in
    M5, not present in the notebook, the same category of deliberate,
    tested addition as decisions D-4/D-5)."""

    safe_payload = convert_to_json_safe(payload)

    if destination.exists():
        existing_payload = json.loads(destination.read_text(encoding="utf-8"))

        if _strip_timestamps(existing_payload) != _strip_timestamps(safe_payload):
            raise NormalizationIntegrityError(
                "NORMALIZATION_ARTIFACT_COLLISION", {"path": str(destination)}
            )

        return

    write_json_safe_atomically(destination, payload)


def persist_normalization_result(
    *, normalization_input: NormalizationInput, result: NormalizationResult, config: NormalizationConfig
) -> Path:
    document_directory = phase_4_document_directory(normalization_input, config=config)

    _write_json_safe_idempotent(document_directory / "normalization_result.json", result)
    _write_json_safe_idempotent(document_directory / "field_candidates.json", result.field_candidates)

    if result.invoice_record is not None:
        _write_json_safe_idempotent(document_directory / "normalized_invoice.json", result.invoice_record)

    _write_json_safe_idempotent(document_directory / "normalization_event.json", result.event)

    return document_directory


# ============================================================
# 19. Main Phase 4 orchestrator (notebook cell 63)
# ============================================================


def normalize_invoice_document(
    normalization_input: NormalizationInput, *, config: NormalizationConfig
) -> NormalizationResult:
    """Public Phase 4 entry point. Persists artifacts. `config` is threaded
    explicitly (task §6), replacing the notebook's hidden global
    `normalization_config`."""

    started_at = normalization_utc_now()

    field_candidates_flat: tuple[InvoiceFieldCandidate, ...] = tuple()

    try:
        ocr_result = normalization_input.ocr_result

        if str(ocr_result.batch_id) != str(normalization_input.batch_id):
            raise ValueError("OCR batch ID does not match the Phase 4 input.")

        if str(ocr_result.document_id) != str(normalization_input.document_id):
            raise ValueError("OCR document ID does not match the Phase 4 input.")

        if ocr_result.source_document_sha256 != normalization_input.source_document_sha256:
            raise ValueError("OCR source-document SHA-256 does not match the Phase 4 input.")

        evidence_index = build_ocr_evidence_index(normalization_input)

        candidates_by_field, line_item_candidate_groups = extract_all_invoice_candidates(
            normalization_input, evidence_index, config=config
        )

        field_candidates_flat = tuple(
            candidate
            for field_name in sorted(candidates_by_field, key=lambda item: item.value)
            for candidate in candidates_by_field[field_name]
        )

        normalized_fields, selections, field_review_reasons = select_document_fields(
            document_id=normalization_input.document_id, candidates_by_field=candidates_by_field, config=config
        )

        invoice_currency = get_selected_currency(normalized_fields)

        normalized_line_items = tuple(
            convert_line_item_group(
                document_id=normalization_input.document_id,
                group=group,
                invoice_currency=invoice_currency,
                config=config,
            )
            for group in line_item_candidate_groups
        )

        review_reasons = list(field_review_reasons)

        for inherited_reason in collect_inherited_ocr_reasons(ocr_result):
            append_unique_reason(review_reasons, inherited_reason)

        if not normalized_line_items:
            append_unique_reason(review_reasons, "NO_LINE_ITEMS_EXTRACTED")

        for line_item in normalized_line_items:
            if not line_item.review_required:
                continue

            for reason in line_item.review_reasons:
                append_unique_reason(review_reasons, f"LINE_{line_item.line_number}:{reason}")

        record_review_required = bool(review_reasons)

        invoice_record_id = create_normalization_id(
            config,
            "invoice-record",
            normalization_input.document_id,
            normalization_input.source_document_sha256,
            config.normalization_version,
        )

        invoice_record = NormalizedInvoiceRecord(
            invoice_record_id=invoice_record_id,
            batch_id=normalization_input.batch_id,
            document_id=normalization_input.document_id,
            source_name=normalization_input.source_name,
            source_document_sha256=normalization_input.source_document_sha256,
            fields=normalized_fields,
            line_items=normalized_line_items,
            normalization_version=config.normalization_version,
            created_at=started_at,
            review_required=record_review_required,
            review_reasons=tuple(review_reasons),
        )

        final_status = (
            NormalizationStatus.REVIEW_REQUIRED if record_review_required else NormalizationStatus.SUCCEEDED
        )

        event = NormalizationEvent(
            event_type="NORMALIZATION",
            status=final_status,
            batch_id=normalization_input.batch_id,
            document_id=normalization_input.document_id,
            occurred_at=normalization_utc_now(),
            message=(
                f"Invoice normalisation completed with {len(normalized_fields)} header fields and "
                f"{len(normalized_line_items)} line items."
            ),
            review_required=record_review_required,
        )

        result = NormalizationResult(
            batch_id=normalization_input.batch_id,
            document_id=normalization_input.document_id,
            source_document_sha256=normalization_input.source_document_sha256,
            ocr_version=normalization_input.ocr_version,
            normalization_version=config.normalization_version,
            status=final_status,
            invoice_record=invoice_record,
            field_candidates=(field_candidates_flat if config.preserve_field_candidates else tuple()),
            event=event,
            review_reasons=tuple(review_reasons),
            errors=tuple(),
        )

        persist_normalization_result(normalization_input=normalization_input, result=result, config=config)

        return result

    except Exception as error:
        error_message = f"{type(error).__name__}: {error}"

        event = NormalizationEvent(
            event_type="NORMALIZATION",
            status=NormalizationStatus.FAILED,
            batch_id=normalization_input.batch_id,
            document_id=normalization_input.document_id,
            occurred_at=normalization_utc_now(),
            message=f"Invoice normalisation failed: {error_message}",
            review_required=True,
        )

        failed_result = NormalizationResult(
            batch_id=normalization_input.batch_id,
            document_id=normalization_input.document_id,
            source_document_sha256=normalization_input.source_document_sha256,
            ocr_version=normalization_input.ocr_version,
            normalization_version=config.normalization_version,
            status=NormalizationStatus.FAILED,
            invoice_record=None,
            field_candidates=(field_candidates_flat if config.preserve_field_candidates else tuple()),
            event=event,
            review_reasons=("NORMALIZATION_FAILED",),
            errors=(error_message,),
        )

        try:
            persist_normalization_result(normalization_input=normalization_input, result=failed_result, config=config)
        except Exception:
            pass

        return failed_result
