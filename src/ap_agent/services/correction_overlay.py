"""Apply a human-review correction overlay to a typed normalization (M11D Core).

The overlay (`WorkflowResumePlan.correction_overlay_json`) is *data*: a list
of exact header/line targets with the reviewer's previous value, corrected
value, reason and cited evidence. This module applies it to a **new**
`NormalizationResult` built with `dataclasses.replace` -- the hydrated input
(and therefore the stored original) is never mutated.

Rules (each violation raises `ResumeIntegrityError`, before any downstream
stage runs):

  - targets are exact: a known header field, or a known line number (a line
    sub-field that was never extracted may be supplied). M11E.4: a header
    field the extractor never produced (e.g. a missing SUPPLIER_NAME) may be
    INSERTED, only with `previous_value` null; it is appended to the derived
    record with the field's canonical value type, human-review provenance,
    confidence 100 and no review flag. Nothing else is invented;
  - duplicate or structurally invalid targets are rejected;
  - `previous_value` must equal the stored value, and a reason and evidence
    references are required (they are recorded on the corrected field as
    explicit human-review provenance);
  - decimals are rebuilt from their exact text (never through `float`),
    dates from ISO-8601 text;
  - the old case's inherited review flags are *resolved by the decision* and
    are cleared on the corrected record; downstream stages then raise any
    review that still applies.
"""

from __future__ import annotations

import dataclasses
import json
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any, Optional
from uuid import NAMESPACE_URL, UUID, uuid5

from ap_agent.exceptions import ResumeIntegrityError
from ap_agent.models.normalization import (
    ExtractionMethod,
    InvoiceFieldName,
    NormalizationResult,
    NormalizationStatus,
    NormalizedInvoiceField,
    NormalizedInvoiceRecord,
    NormalizedLineItem,
    NormalizedValueType,
)
from ap_agent.serialization.memory_json import optional_text

__all__ = ["parse_overlay", "apply_correction_overlay", "corrected_field_names", "header_value_type"]

_LINE_FIELD_ATTRIBUTES = {
    InvoiceFieldName.LINE_DESCRIPTION: "description",
    InvoiceFieldName.LINE_QUANTITY: "quantity",
    InvoiceFieldName.LINE_UNIT_PRICE: "unit_price",
    InvoiceFieldName.LINE_AMOUNT: "amount",
}

_DEFAULT_VALUE_TYPES = {
    InvoiceFieldName.LINE_DESCRIPTION: NormalizedValueType.TEXT,
    InvoiceFieldName.LINE_QUANTITY: NormalizedValueType.DECIMAL,
    InvoiceFieldName.LINE_UNIT_PRICE: NormalizedValueType.DECIMAL,
    InvoiceFieldName.LINE_AMOUNT: NormalizedValueType.DECIMAL,
}

_HUMAN_CONFIDENCE = 100.0

# Canonical value type of a header field inserted by a reviewer. Mirrors `ap_agent.tools.normalization.normalize_candidate_value`
# (a test asserts they agree for every header field), kept local so this module stays free of the tools layer.
_MONETARY_HEADER_FIELDS = {
    InvoiceFieldName.SUBTOTAL,
    InvoiceFieldName.TAX_AMOUNT,
    InvoiceFieldName.DISCOUNT_AMOUNT,
    InvoiceFieldName.SHIPPING_AMOUNT,
    InvoiceFieldName.TOTAL_AMOUNT,
}
_DATE_HEADER_FIELDS = {InvoiceFieldName.INVOICE_DATE, InvoiceFieldName.DUE_DATE}


def header_value_type(field_name: InvoiceFieldName) -> Optional[NormalizedValueType]:
    """The canonical value type of a header field, or None for a line sub-field (not a header)."""

    if field_name in _LINE_FIELD_ATTRIBUTES:
        return None

    if field_name in _MONETARY_HEADER_FIELDS:
        return NormalizedValueType.DECIMAL

    if field_name in _DATE_HEADER_FIELDS:
        return NormalizedValueType.DATE

    if field_name == InvoiceFieldName.CURRENCY:
        return NormalizedValueType.CURRENCY_CODE

    return NormalizedValueType.TEXT


def parse_overlay(correction_overlay_json: str) -> dict[str, Any]:
    try:
        overlay = json.loads(correction_overlay_json)
    except ValueError as error:
        raise ResumeIntegrityError("OVERLAY_MALFORMED") from error

    if not isinstance(overlay, dict) or not isinstance(overlay.get("corrections"), list):
        raise ResumeIntegrityError("OVERLAY_MALFORMED")

    return overlay


def corrected_field_names(overlay: dict[str, Any]) -> tuple[str, ...]:
    return tuple(
        str(correction.get("field_name")) for correction in overlay["corrections"] if isinstance(correction, dict)
    )


def _parse_value(text: Optional[str], value_type: NormalizedValueType) -> Any:
    if text is None:
        raise ResumeIntegrityError("OVERLAY_VALUE_MISSING")

    stripped = text.strip()

    if not stripped:
        raise ResumeIntegrityError("OVERLAY_VALUE_MISSING")

    if value_type == NormalizedValueType.DECIMAL:
        try:
            value = Decimal(stripped)
        except InvalidOperation as error:
            raise ResumeIntegrityError("OVERLAY_VALUE_INVALID") from error

        if not value.is_finite():
            raise ResumeIntegrityError("OVERLAY_VALUE_INVALID")

        return value

    if value_type == NormalizedValueType.DATE:
        try:
            return date.fromisoformat(stripped)
        except ValueError as error:
            raise ResumeIntegrityError("OVERLAY_VALUE_INVALID") from error

    if value_type == NormalizedValueType.CURRENCY_CODE:
        return stripped.upper()

    return stripped


def _provenance_notes(
    *, decision_id: UUID, previous_value: Optional[str], reason: str, evidence_ids: list[str]
) -> tuple[str, ...]:
    return (
        "HUMAN_REVIEW_CORRECTION",
        f"decision_id={decision_id}",
        f"previous_value={previous_value if previous_value is not None else '<missing>'}",
        f"reason={reason}",
        f"evidence_reference_ids={','.join(sorted(evidence_ids))}",
    )


def _corrected_field(
    *,
    original: Optional[NormalizedInvoiceField],
    field_name: InvoiceFieldName,
    line_item_id: Optional[UUID],
    decision_id: UUID,
    correction: dict[str, Any],
    evidence_ids: list[str],
) -> NormalizedInvoiceField:
    value_type = (
        original.value_type
        if original is not None
        else _DEFAULT_VALUE_TYPES.get(field_name) or header_value_type(field_name)
    )

    if value_type is None:
        raise ResumeIntegrityError("OVERLAY_TARGET_INVALID")

    corrected_text = correction.get("corrected_value")
    value = _parse_value(corrected_text, value_type)
    notes = _provenance_notes(
        decision_id=decision_id,
        previous_value=correction.get("previous_value"),
        reason=str(correction["reason"]),
        evidence_ids=evidence_ids,
    )

    if original is not None:
        return dataclasses.replace(
            original,
            raw_value=str(corrected_text).strip(),
            normalized_value=value,
            confidence=_HUMAN_CONFIDENCE,
            normalization_notes=original.normalization_notes + notes,
            review_required=False,
            review_reasons=tuple(),
        )

    return NormalizedInvoiceField(
        field_id=uuid5(NAMESPACE_URL, f"ap-agent/human-corrected-field/{decision_id}/{line_item_id}/{field_name.value}"),
        field_name=field_name,
        raw_value=str(corrected_text).strip(),
        normalized_value=value,
        value_type=value_type,
        confidence=_HUMAN_CONFIDENCE,
        extraction_method=ExtractionMethod.COMBINED_RULES,
        evidence_references=tuple(),
        normalization_notes=notes,
        review_required=False,
        review_reasons=tuple(),
    )


def apply_correction_overlay(
    normalization: NormalizationResult, overlay: dict[str, Any], *, decision_id: UUID
) -> NormalizationResult:
    """Return a corrected deep copy. With no corrections (an approval) the
    inherited review flags are still resolved, nothing else changes."""

    record = normalization.invoice_record

    if record is None:
        raise ResumeIntegrityError("NORMALIZATION_RECORD_MISSING")

    if str(overlay.get("decision_id")) != str(decision_id):
        raise ResumeIntegrityError("OVERLAY_DECISION_MISMATCH")

    header_fields = {field.field_name: field for field in record.fields}
    line_items = {line.line_number: line for line in record.line_items}

    if len(line_items) != len(record.line_items):
        raise ResumeIntegrityError("OVERLAY_TARGET_INVALID")

    seen: set[tuple[str, Optional[int]]] = set()
    new_header_fields = dict(header_fields)
    new_line_items = dict(line_items)
    inserted_header_fields: list[InvoiceFieldName] = []

    for correction in overlay["corrections"]:
        if not isinstance(correction, dict):
            raise ResumeIntegrityError("OVERLAY_MALFORMED")

        try:
            field_name = InvoiceFieldName(correction["field_name"])
        except (KeyError, ValueError) as error:
            raise ResumeIntegrityError("OVERLAY_TARGET_INVALID") from error

        line_number = correction.get("line_number")
        target = (field_name.value, line_number)

        if target in seen:
            raise ResumeIntegrityError("OVERLAY_DUPLICATE_TARGET")

        seen.add(target)

        reason = correction.get("reason")
        evidence_ids = correction.get("evidence_reference_ids")

        if not isinstance(reason, str) or not reason.strip():
            raise ResumeIntegrityError("OVERLAY_REASON_MISSING")

        if not isinstance(evidence_ids, list) or not evidence_ids or not all(isinstance(item, str) for item in evidence_ids):
            raise ResumeIntegrityError("OVERLAY_EVIDENCE_MISSING")

        if field_name in _LINE_FIELD_ATTRIBUTES:
            if not isinstance(line_number, int) or isinstance(line_number, bool) or line_number not in line_items:
                raise ResumeIntegrityError("OVERLAY_TARGET_INVALID")

            attribute = _LINE_FIELD_ATTRIBUTES[field_name]
            line = new_line_items[line_number]
            original_field: Optional[NormalizedInvoiceField] = getattr(line, attribute)

            if correction.get("previous_value") != optional_text(
                None if original_field is None else original_field.normalized_value
            ):
                # Line previous-values are advisory in M11C (only the line
                # number is validated there); a mismatch is still refused
                # here because the overlay must describe what it changes.
                raise ResumeIntegrityError("OVERLAY_PREVIOUS_VALUE_MISMATCH")

            corrected = _corrected_field(
                original=original_field, field_name=field_name, line_item_id=line.line_item_id,
                decision_id=decision_id, correction=correction, evidence_ids=evidence_ids,
            )
            new_line_items[line_number] = dataclasses.replace(line, **{attribute: corrected})
        else:
            if line_number is not None:
                raise ResumeIntegrityError("OVERLAY_TARGET_INVALID")

            if field_name not in header_fields:
                # M11E.4: insert a header field the extractor never produced -- only as an insertion (previous_value null).
                if correction.get("previous_value") is not None:
                    raise ResumeIntegrityError("OVERLAY_PREVIOUS_VALUE_MISMATCH")

                new_header_fields[field_name] = _corrected_field(
                    original=None, field_name=field_name, line_item_id=None,
                    decision_id=decision_id, correction=correction, evidence_ids=evidence_ids,
                )
                inserted_header_fields.append(field_name)
                continue

            original_field = header_fields[field_name]

            if correction.get("previous_value") != optional_text(original_field.normalized_value):
                raise ResumeIntegrityError("OVERLAY_PREVIOUS_VALUE_MISMATCH")

            new_header_fields[field_name] = _corrected_field(
                original=original_field, field_name=field_name, line_item_id=None,
                decision_id=decision_id, correction=correction, evidence_ids=evidence_ids,
            )

    corrected_record = dataclasses.replace(
        record,
        # existing fields keep their order; inserted ones are appended (append-only), in the order they were corrected
        fields=tuple(new_header_fields[field.field_name] for field in record.fields)
        + tuple(new_header_fields[name] for name in inserted_header_fields),
        line_items=tuple(new_line_items[line.line_number] for line in record.line_items),
        review_required=False,
        review_reasons=tuple(),
    )

    return dataclasses.replace(
        normalization,
        invoice_record=corrected_record,
        status=NormalizationStatus.SUCCEEDED,
        review_reasons=tuple(),
    )
