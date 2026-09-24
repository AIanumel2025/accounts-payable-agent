"""Phase 5 (deterministic financial validation) processing functions: the
Phase 4 -> Phase 5 typed bridge, deterministic ID generation, Decimal
utilities, evidence-linked validation operands, header- and arithmetic-level
checks, status/routing orchestration and artifact persistence.

Source cells (task §1, §4; active-definition table §2
`tools/financial_validation.py`; modularisation map §10.5). Every function
below is extracted from exactly one notebook cell -- Phase 5 has no
superseded/replacement cells the way Phases 2-4 do (§3.1 of the
modularisation map lists no Phase 5 entries), so there is no
correction/replacement precedence to resolve here, only the explicit-
configuration threading task §5 requires:

  - Cell 68 ("PHASE 5 — CELL 1"): contracts (already extracted to
    `ap_agent.models.validation` and `ap_agent.config.settings` in M2).
    `validation_utc_now` and `create_validation_id` are the two processing
    functions deferred from that cell to this module.
  - Cell 69 ("PHASE 5 — CELL 2"): `to_decimal_or_none`, `quantize_money`,
    `decimal_difference`, `values_within_tolerance`, `get_invoice_field`,
    `get_invoice_field_value`, `get_invoice_decimal`,
    `evidence_ids_from_field`, `canonical_operand_value`,
    `build_field_operand`, `build_line_item_operand`,
    `build_validation_check`, `build_validation_input`.
    `canonical_decimal_text` moved to `ap_agent.artifacts.serialization`
    (reused by `canonical_operand_value` and `phase_5_json_safe`, not
    duplicated -- CLAUDE.md). Cell 69's `summarize_validation_checks` is
    SUPERSEDED (§3.1 of the modularisation map: functionally replaced by
    `summarize_checks_fail_closed@72`, which additionally treats SKIPPED as
    a review condition; only the cell-69 self-test called it) and is not
    extracted.
  - Cell 70 ("PHASE 5 — CELL 3"): `append_unique_reason` (the **Phase 5**
    binding -- does not skip falsy reasons; kept separate from the Phase 4
    binding in `tools/normalization.py` per decision D-9/task §7, since M1
    determined the two are not behaviourally equivalent),
    `validate_inherited_review`, `validate_required_financial_fields`,
    `validate_monetary_values`, `normalized_date_or_none`,
    `validate_date_consistency`, `extract_currency_signals`,
    `validate_currency_consistency`, `run_header_validation_checks`, and the
    constants `HEADER_MONETARY_FIELDS`, `CURRENCY_SYMBOL_MAP`,
    `SUPPORTED_CURRENCY_CODES`.
  - Cell 71 ("PHASE 5 — CELL 4"): `validate_line_item_arithmetic`,
    `validate_line_items_to_subtotal`, `validate_invoice_total`,
    `run_arithmetic_validation_checks`. (`find_preview_check` is TEST_ONLY;
    extracted to `tests/unit/test_financial_validation_tools.py` instead.)
  - Cell 72 ("PHASE 5 — CELL 5"): `validate_phase_5_input_integrity`,
    `summarize_checks_fail_closed`, `collect_validation_review_reasons`,
    `determine_validation_status`, `build_failed_validation_summary`, and
    the public entry point `process_financial_validation` (does not
    persist).
  - Cell 73 ("PHASE 5 — CELL 6"): `phase_5_artifact_directory`,
    `persist_financial_validation_result`,
    `validate_persisted_phase_5_artifacts`. `phase_5_json_safe` moved to
    `ap_agent.artifacts.serialization`; `write_text_atomic`,
    `write_json_atomic`, `write_jsonl_atomic` moved to
    `ap_agent.artifacts.filesystem`; `calculate_file_sha256` (cell 73) is
    behaviourally identical to the Phase 2/3 binding (cell 26) and is
    **not** re-defined here -- decision D-10 explicitly permits
    consolidating the two identical implementations, and
    `ap_agent.artifacts.filesystem.calculate_file_sha256` already serves
    every phase that needs it.

Explicit configuration (task §5; decisions D-11, R-05). The notebook read a
single hidden global `financial_validation_config` from 10 call sites (map
§5.1: `create_validation_id@68`, `values_within_tolerance@69`,
`validate_required_financial_fields@70`, `validate_monetary_values@70`,
`validate_line_item_arithmetic@71`, `validate_line_items_to_subtotal@71`,
`validate_invoice_total@71`, `process_financial_validation@72`,
`phase_5_artifact_directory@73`, `persist_financial_validation_result@73`).
Every function below that used it, or that calls a function that does
(`build_validation_check` and therefore every check-producing function, all
the way through `run_header_validation_checks`/
`run_arithmetic_validation_checks`), takes an explicit `config:
FinancialValidationConfig` keyword parameter instead; there is no
module-level config *instance* anywhere in this module. See
`docs/m6_phase_5_financial_validation_report.md` for the deterministic-ID
parity proof this mirrors from M5.

Phase 4 -> Phase 5 typed bridge (task §3). The notebook's own
`build_validation_input@69` was already a real function (unlike the Phase
3 -> Phase 4 bridge, which was inline orchestration code) -- it is kept
under its notebook name and its four original identity checks are kept
verbatim, but two things are new in M6: (1) those checks now fail closed
with `FinancialValidationIntegrityError` instead of a bare `ValueError`,
matching the `NormalizationIntegrityError` precedent from M5; (2) a new
`verify_normalization_result_artifact_integrity` check (task §3's "verify
normalization artifact existence and hash") is added, so a stale,
reconstructed or tampered `NormalizationResult` that was never actually
persisted by `ap_agent.tools.normalization.persist_normalization_result`
can never reach Phase 5.

Idempotent persistence (task §16/§18; new in M6, not present in the
notebook -- the same category of deliberate, tested addition as M5's
`_write_json_safe_idempotent`, decisions D-4/D-5's precedent). The
notebook's Phase 5 writers always overwrite unconditionally.
`persist_financial_validation_result` here instead compares the new payload
against any existing file at that path (ignoring `occurred_at`, which
legitimately differs between two otherwise-identical reprocessing runs):
identical content is a silent no-op; a genuine difference fails closed with
`FinancialValidationIntegrityError`.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from pathlib import Path
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid5

from ap_agent.artifacts.filesystem import (
    calculate_file_sha256,
    write_json_atomic,
    write_jsonl_atomic,
)
from ap_agent.artifacts.serialization import (
    canonical_decimal_text,
    convert_to_json_safe,
    phase_5_json_safe,
)
from ap_agent.config.settings import FinancialValidationConfig, NormalizationConfig
from ap_agent.exceptions import FinancialValidationIntegrityError
from ap_agent.models.normalization import (
    InvoiceFieldName,
    NormalizationResult,
    NormalizedInvoiceField,
    NormalizedInvoiceRecord,
    NormalizedLineItem,
)
from ap_agent.models.validation import (
    FinancialValidationResult,
    FinancialValidationSummary,
    ValidationCheckResult,
    ValidationCheckStatus,
    ValidationCheckType,
    ValidationEvent,
    ValidationInput,
    ValidationOperand,
    ValidationSeverity,
    ValidationStatus,
)

__all__ = [
    "verify_normalization_result_artifact_integrity",
    "build_validation_input",
    "validation_utc_now",
    "create_validation_id",
    "to_decimal_or_none",
    "quantize_money",
    "decimal_difference",
    "values_within_tolerance",
    "get_invoice_field",
    "get_invoice_field_value",
    "get_invoice_decimal",
    "evidence_ids_from_field",
    "canonical_operand_value",
    "build_field_operand",
    "build_line_item_operand",
    "build_validation_check",
    "append_unique_reason",
    "validate_inherited_review",
    "validate_required_financial_fields",
    "validate_monetary_values",
    "normalized_date_or_none",
    "validate_date_consistency",
    "extract_currency_signals",
    "validate_currency_consistency",
    "run_header_validation_checks",
    "validate_line_item_arithmetic",
    "validate_line_items_to_subtotal",
    "validate_invoice_total",
    "run_arithmetic_validation_checks",
    "validate_phase_5_input_integrity",
    "summarize_checks_fail_closed",
    "collect_validation_review_reasons",
    "determine_validation_status",
    "build_failed_validation_summary",
    "process_financial_validation",
    "phase_5_artifact_directory",
    "persist_financial_validation_result",
    "validate_persisted_phase_5_artifacts",
    "HEADER_MONETARY_FIELDS",
    "CURRENCY_SYMBOL_MAP",
    "SUPPORTED_CURRENCY_CODES",
]


# ============================================================
# 1. Phase 4 -> Phase 5 typed bridge (M6, task §3)
# ============================================================


def verify_normalization_result_artifact_integrity(
    normalization_result: NormalizationResult,
    normalization_config: NormalizationConfig,
) -> Path:
    """Verify the persisted Phase 4 artifact exists and matches
    `normalization_result` (task §3: "verify normalization artifact
    existence and hash").

    `ap_agent.tools.normalization.persist_normalization_result` always
    writes `normalization_result.json` with
    `ap_agent.artifacts.filesystem.write_json_safe_atomically`
    (`convert_to_json_safe` + `indent=2` + `ensure_ascii=False`).
    Re-serialising `normalization_result` the same way and comparing its
    hash against the persisted file's hash verifies both that the artifact
    exists and that `normalization_result` is not a stale, reconstructed or
    tampered object that diverges from what Phase 4 actually wrote to disk.
    Fails closed (`FinancialValidationIntegrityError`) on either problem.
    """

    document_directory = (
        normalization_config.artifact_root
        / str(normalization_result.batch_id)
        / str(normalization_result.document_id)
        / normalization_result.normalization_version
    )

    artifact_path = document_directory / "normalization_result.json"

    if not artifact_path.is_file():
        raise FinancialValidationIntegrityError(
            "NORMALIZATION_ARTIFACT_MISSING",
            {"path": str(artifact_path)},
        )

    persisted_hash = calculate_file_sha256(artifact_path)

    expected_bytes = json.dumps(
        convert_to_json_safe(normalization_result), indent=2, ensure_ascii=False
    ).encode("utf-8")
    expected_hash = hashlib.sha256(expected_bytes).hexdigest()

    if persisted_hash != expected_hash:
        raise FinancialValidationIntegrityError(
            "NORMALIZATION_ARTIFACT_HASH_MISMATCH",
            {
                "path": str(artifact_path),
                "persisted_hash": persisted_hash,
                "expected_hash": expected_hash,
            },
        )

    return artifact_path


def build_validation_input(
    normalization_result: NormalizationResult,
    *,
    normalization_config: NormalizationConfig,
) -> ValidationInput:
    """Validate and bridge one Phase 4 result into Phase 5 (notebook cell
    69's `build_validation_input`, already a real production function --
    map §5.2 rule 4).

    Carries batch ID, document ID, source-document SHA-256, normalization
    version, the final `NormalizedInvoiceRecord` and the upstream review
    state/reasons (via `invoice_record.review_required`/`review_reasons`),
    per task §3.

    Checks, in order (each fails closed with
    `FinancialValidationIntegrityError` -- a documented, stricter
    replacement for the notebook's own bare `ValueError`s, mirroring the
    `NormalizationIntegrityError` precedent from M5):

      1. the Phase 4 result actually produced a `NormalizedInvoiceRecord`
         (rejects a missing input);
      2. batch identity continuity between the result and its own record
         (rejects a cross-document input);
      3. document identity continuity (rejects a cross-document input);
      4. source-document SHA-256 continuity (rejects a mismatched input);
      5. normalization-version continuity;
      6. the persisted Phase 4 artifact exists and its hash matches
         `normalization_result` (`verify_normalization_result_artifact_integrity`
         -- rejects a stale or tampered input; new in M6, task §3).
    """

    invoice_record = normalization_result.invoice_record

    if invoice_record is None:
        raise FinancialValidationIntegrityError(
            "NORMALIZATION_RECORD_MISSING",
            {
                "batch_id": str(normalization_result.batch_id),
                "document_id": str(normalization_result.document_id),
            },
        )

    if invoice_record.batch_id != normalization_result.batch_id:
        raise FinancialValidationIntegrityError(
            "BATCH_IDENTITY_MISMATCH",
            {
                "record_batch_id": str(invoice_record.batch_id),
                "result_batch_id": str(normalization_result.batch_id),
            },
        )

    if invoice_record.document_id != normalization_result.document_id:
        raise FinancialValidationIntegrityError(
            "DOCUMENT_IDENTITY_MISMATCH",
            {
                "record_document_id": str(invoice_record.document_id),
                "result_document_id": str(normalization_result.document_id),
            },
        )

    if invoice_record.source_document_sha256 != normalization_result.source_document_sha256:
        raise FinancialValidationIntegrityError(
            "SOURCE_DOCUMENT_SHA256_MISMATCH",
            {
                "record_sha256": invoice_record.source_document_sha256,
                "result_sha256": normalization_result.source_document_sha256,
            },
        )

    if invoice_record.normalization_version != normalization_result.normalization_version:
        raise FinancialValidationIntegrityError(
            "NORMALIZATION_VERSION_MISMATCH",
            {
                "record_version": invoice_record.normalization_version,
                "result_version": normalization_result.normalization_version,
            },
        )

    verify_normalization_result_artifact_integrity(normalization_result, normalization_config)

    return ValidationInput(
        batch_id=invoice_record.batch_id,
        document_id=invoice_record.document_id,
        source_name=invoice_record.source_name,
        source_document_sha256=invoice_record.source_document_sha256,
        normalization_version=invoice_record.normalization_version,
        invoice_record=invoice_record,
    )


# ============================================================
# 2. Deterministic Phase 5 identifiers (notebook cell 68)
# ============================================================


def validation_utc_now() -> datetime:
    """Return a timezone-aware UTC timestamp."""

    from datetime import timezone

    return datetime.now(timezone.utc)


def create_validation_id(
    config: FinancialValidationConfig,
    entity_type: str,
    document_id: UUID | str,
    *identity_parts: Any,
) -> UUID:
    """Create a deterministic Phase 5 identifier.

    Identical inputs and configured `validation_version` always produce the
    same UUID (task §5: `config.validation_version` replaces the notebook's
    hidden global `financial_validation_config.validation_version`;
    timestamps never enter this formula). Uses the standard-library
    `uuid.NAMESPACE_URL`, matching the notebook exactly -- task §5 requires
    preserving this validated namespace UUID, not substituting a
    package-specific one the way Phase 4's `PHASE_4_NAMESPACE` does.
    """

    identity_text = "|".join(
        [
            config.validation_version,
            str(entity_type).strip().lower(),
            str(document_id),
            *[str(part).strip() for part in identity_parts],
        ]
    )

    return uuid5(NAMESPACE_URL, identity_text)


# ============================================================
# 3. Decimal utilities and evidence-linked operands (cell 69)
# ============================================================


def to_decimal_or_none(value: Any) -> Decimal | None:
    """Safely convert a normalized value into Decimal.

    Phase 5 must not interpret dates, booleans or arbitrary objects as
    monetary values.
    """

    if value is None:
        return None

    if isinstance(value, bool):
        return None

    if isinstance(value, Decimal):
        return value

    if isinstance(value, int):
        return Decimal(value)

    if isinstance(value, float):
        return Decimal(str(value))

    if isinstance(value, str):
        cleaned_value = value.strip().replace(",", "").replace("$", "").replace("£", "").replace("€", "")

        if not cleaned_value:
            return None

        try:
            return Decimal(cleaned_value)

        except InvalidOperation:
            return None

    return None


def quantize_money(value: Decimal) -> Decimal:
    """Quantize a monetary value using the Phase 5 policy."""

    return value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def decimal_difference(observed: Decimal, expected: Decimal) -> Decimal:
    """Return the absolute difference between two values."""

    return abs(quantize_money(observed) - quantize_money(expected))


def values_within_tolerance(
    observed: Decimal,
    expected: Decimal,
    tolerance: Decimal | None = None,
    *,
    config: FinancialValidationConfig,
) -> bool:
    """Compare monetary values using an explicit tolerance."""

    applied_tolerance = tolerance if tolerance is not None else config.monetary_tolerance

    return decimal_difference(observed, expected) <= applied_tolerance


def get_invoice_field(
    invoice_record: NormalizedInvoiceRecord,
    field_name: InvoiceFieldName,
) -> NormalizedInvoiceField | None:
    """Retrieve one normalized header field."""

    return invoice_record.get_field(field_name)


def get_invoice_field_value(
    invoice_record: NormalizedInvoiceRecord,
    field_name: InvoiceFieldName,
) -> Any:
    """Retrieve the normalized value of one header field."""

    invoice_field = get_invoice_field(invoice_record, field_name)

    if invoice_field is None:
        return None

    return invoice_field.normalized_value


def get_invoice_decimal(
    invoice_record: NormalizedInvoiceRecord,
    field_name: InvoiceFieldName,
) -> Decimal | None:
    """Retrieve a normalized header value as Decimal."""

    return to_decimal_or_none(get_invoice_field_value(invoice_record, field_name))


def evidence_ids_from_field(
    invoice_field: NormalizedInvoiceField | None,
) -> tuple[UUID, ...]:
    """Preserve the evidence references attached to a field."""

    if invoice_field is None:
        return tuple()

    return tuple(reference.reference_id for reference in invoice_field.evidence_references)


def canonical_operand_value(value: Any) -> str | None:
    """Convert an operand value into stable audit text."""

    if value is None:
        return None

    if isinstance(value, Decimal):
        return canonical_decimal_text(value)

    if isinstance(value, (date, datetime)):
        return value.isoformat()

    return str(value)


def build_field_operand(
    invoice_record: NormalizedInvoiceRecord,
    field_name: InvoiceFieldName,
    operand_name: str | None = None,
) -> ValidationOperand:
    """Build an auditable validation operand from a header field."""

    invoice_field = get_invoice_field(invoice_record, field_name)

    if invoice_field is None:
        return ValidationOperand(
            name=operand_name or field_name.value.lower(),
            value=None,
        )

    return ValidationOperand(
        name=operand_name or field_name.value.lower(),
        value=canonical_operand_value(invoice_field.normalized_value),
        field_id=invoice_field.field_id,
        line_item_id=None,
        evidence_reference_ids=evidence_ids_from_field(invoice_field),
    )


def build_line_item_operand(
    line_item: NormalizedLineItem,
    attribute_name: str,
    operand_name: str | None = None,
) -> ValidationOperand:
    """Build an operand from quantity, unit price or amount."""

    if attribute_name not in {"quantity", "unit_price", "amount"}:
        raise ValueError(f"Unsupported line-item operand: {attribute_name}")

    invoice_field = getattr(line_item, attribute_name)

    if invoice_field is None:
        return ValidationOperand(
            name=operand_name or attribute_name,
            value=None,
            line_item_id=line_item.line_item_id,
        )

    return ValidationOperand(
        name=operand_name or attribute_name,
        value=canonical_operand_value(invoice_field.normalized_value),
        field_id=invoice_field.field_id,
        line_item_id=line_item.line_item_id,
        evidence_reference_ids=evidence_ids_from_field(invoice_field),
    )


def build_validation_check(
    *,
    document_id: UUID,
    check_type: ValidationCheckType,
    scope_key: str,
    status: ValidationCheckStatus,
    severity: ValidationSeverity,
    message: str,
    operands: tuple[ValidationOperand, ...] = tuple(),
    expected_value: Any = None,
    observed_value: Any = None,
    difference: Decimal | None = None,
    tolerance: Decimal | None = None,
    reason_codes: tuple[str, ...] = tuple(),
    config: FinancialValidationConfig,
) -> ValidationCheckResult:
    """Create one deterministic validation-check result."""

    review_required = status in {
        ValidationCheckStatus.FAILED,
        ValidationCheckStatus.REVIEW_REQUIRED,
    }

    check_id = create_validation_id(config, "validation-check", document_id, check_type.value, scope_key)

    return ValidationCheckResult(
        check_id=check_id,
        check_type=check_type,
        status=status,
        severity=severity,
        message=message,
        operands=tuple(operands),
        expected_value=canonical_operand_value(expected_value),
        observed_value=canonical_operand_value(observed_value),
        difference=difference,
        tolerance=tolerance,
        review_required=review_required,
        reason_codes=tuple(dict.fromkeys(reason_codes)),
    )


# ============================================================
# 4. Header-level deterministic validation rules (cell 70)
# ============================================================

HEADER_MONETARY_FIELDS = (
    InvoiceFieldName.SUBTOTAL,
    InvoiceFieldName.TAX_AMOUNT,
    InvoiceFieldName.DISCOUNT_AMOUNT,
    InvoiceFieldName.SHIPPING_AMOUNT,
    InvoiceFieldName.TOTAL_AMOUNT,
)


CURRENCY_SYMBOL_MAP = {
    "$": "USD",
    "£": "GBP",
    "€": "EUR",
}


SUPPORTED_CURRENCY_CODES = {"USD", "GBP", "EUR"}


def append_unique_reason(reasons: list[str], reason: str) -> None:
    """Append a reason code only once.

    This is the **Phase 5** binding (notebook cell 70): unlike the Phase 4
    binding in `ap_agent.tools.normalization`, it does not skip falsy
    reasons. Decision D-9/task §7: the two are kept separate, not
    consolidated, since M1 determined they are not behaviourally
    equivalent.
    """

    if reason not in reasons:
        reasons.append(reason)


def validate_inherited_review(
    validation_input: ValidationInput,
    *,
    config: FinancialValidationConfig,
) -> ValidationCheckResult:
    """Preserve unresolved Phase 4 review requirements."""

    invoice_record = validation_input.invoice_record

    if invoice_record.review_required:
        inherited_reasons = invoice_record.review_reasons or ("INHERITED_NORMALIZATION_REVIEW",)

        return build_validation_check(
            document_id=validation_input.document_id,
            check_type=ValidationCheckType.INHERITED_REVIEW,
            scope_key="invoice",
            status=ValidationCheckStatus.REVIEW_REQUIRED,
            severity=ValidationSeverity.WARNING,
            message=(
                "The invoice retains unresolved review requirements from an earlier processing phase."
            ),
            reason_codes=tuple(dict.fromkeys(("INHERITED_REVIEW_REQUIRED", *inherited_reasons))),
            config=config,
        )

    return build_validation_check(
        document_id=validation_input.document_id,
        check_type=ValidationCheckType.INHERITED_REVIEW,
        scope_key="invoice",
        status=ValidationCheckStatus.PASSED,
        severity=ValidationSeverity.INFORMATION,
        message="No unresolved review requirement was inherited from Phase 4.",
        config=config,
    )


def validate_required_financial_fields(
    validation_input: ValidationInput,
    *,
    config: FinancialValidationConfig,
) -> ValidationCheckResult:
    """Confirm that fields required for Phase 5 financial validation are
    present and normalized."""

    invoice_record = validation_input.invoice_record

    operands = []
    missing_fields = []

    for field_name in config.required_financial_fields:
        invoice_field = get_invoice_field(invoice_record, field_name)

        operands.append(build_field_operand(invoice_record, field_name))

        if invoice_field is None or invoice_field.normalized_value is None:
            missing_fields.append(field_name.value)

    if missing_fields:
        reason_codes = tuple(f"REQUIRED_FINANCIAL_FIELD_MISSING:{name}" for name in missing_fields)

        return build_validation_check(
            document_id=validation_input.document_id,
            check_type=ValidationCheckType.REQUIRED_FINANCIAL_FIELDS,
            scope_key="invoice",
            status=ValidationCheckStatus.REVIEW_REQUIRED,
            severity=ValidationSeverity.CRITICAL,
            message="Required financial fields are missing: " + ", ".join(missing_fields) + ".",
            operands=tuple(operands),
            reason_codes=reason_codes,
            config=config,
        )

    return build_validation_check(
        document_id=validation_input.document_id,
        check_type=ValidationCheckType.REQUIRED_FINANCIAL_FIELDS,
        scope_key="invoice",
        status=ValidationCheckStatus.PASSED,
        severity=ValidationSeverity.INFORMATION,
        message="All required Phase 5 financial fields are available.",
        operands=tuple(operands),
        config=config,
    )


def validate_monetary_values(
    validation_input: ValidationInput,
    *,
    config: FinancialValidationConfig,
) -> ValidationCheckResult:
    """Validate normalized monetary values without changing them."""

    invoice_record = validation_input.invoice_record

    operands = []
    reason_codes: list[str] = []
    invalid_messages = []

    for field_name in HEADER_MONETARY_FIELDS:
        invoice_field = get_invoice_field(invoice_record, field_name)

        if invoice_field is None:
            continue

        operands.append(build_field_operand(invoice_record, field_name))

        decimal_value = to_decimal_or_none(invoice_field.normalized_value)

        if decimal_value is None:
            append_unique_reason(reason_codes, f"INVALID_MONETARY_VALUE:{field_name.value}")
            invalid_messages.append(f"{field_name.value} is not a valid decimal")
            continue

        if abs(decimal_value) > config.maximum_absolute_amount:
            append_unique_reason(reason_codes, f"MONETARY_VALUE_EXCEEDS_LIMIT:{field_name.value}")
            invalid_messages.append(f"{field_name.value} exceeds the configured amount limit")

        if decimal_value < Decimal("0") and field_name != InvoiceFieldName.DISCOUNT_AMOUNT:
            append_unique_reason(reason_codes, f"UNEXPECTED_NEGATIVE_VALUE:{field_name.value}")
            invalid_messages.append(f"{field_name.value} is unexpectedly negative")

    for line_item in invoice_record.line_items:
        for attribute_name in ("quantity", "unit_price", "amount"):
            invoice_field = getattr(line_item, attribute_name)

            if invoice_field is None:
                continue

            operands.append(
                build_line_item_operand(
                    line_item,
                    attribute_name,
                    operand_name=f"line_{line_item.line_number}_{attribute_name}",
                )
            )

            decimal_value = to_decimal_or_none(invoice_field.normalized_value)

            if decimal_value is None:
                append_unique_reason(
                    reason_codes,
                    f"INVALID_LINE_MONETARY_VALUE:{line_item.line_number}:{attribute_name}",
                )
                invalid_messages.append(f"Line {line_item.line_number} {attribute_name} is invalid")
                continue

            if decimal_value < Decimal("0"):
                append_unique_reason(
                    reason_codes,
                    f"NEGATIVE_LINE_VALUE:{line_item.line_number}:{attribute_name}",
                )
                invalid_messages.append(f"Line {line_item.line_number} {attribute_name} is negative")

            if abs(decimal_value) > config.maximum_absolute_amount:
                append_unique_reason(
                    reason_codes,
                    f"LINE_VALUE_EXCEEDS_LIMIT:{line_item.line_number}:{attribute_name}",
                )
                invalid_messages.append(
                    f"Line {line_item.line_number} {attribute_name} exceeds the configured limit"
                )

    if reason_codes:
        return build_validation_check(
            document_id=validation_input.document_id,
            check_type=ValidationCheckType.MONETARY_VALUE_VALIDITY,
            scope_key="invoice",
            status=ValidationCheckStatus.FAILED,
            severity=ValidationSeverity.ERROR,
            message="; ".join(invalid_messages),
            operands=tuple(operands),
            reason_codes=tuple(reason_codes),
            config=config,
        )

    return build_validation_check(
        document_id=validation_input.document_id,
        check_type=ValidationCheckType.MONETARY_VALUE_VALIDITY,
        scope_key="invoice",
        status=ValidationCheckStatus.PASSED,
        severity=ValidationSeverity.INFORMATION,
        message="Available monetary values use valid formats, ranges and signs.",
        operands=tuple(operands),
        config=config,
    )


def normalized_date_or_none(value: Any) -> date | None:
    """Return a normalized date without guessing formats."""

    if isinstance(value, datetime):
        return value.date()

    if isinstance(value, date):
        return value

    if isinstance(value, str):
        try:
            return date.fromisoformat(value.strip())

        except ValueError:
            return None

    return None


def validate_date_consistency(
    validation_input: ValidationInput,
    *,
    config: FinancialValidationConfig,
) -> ValidationCheckResult:
    """Validate the relationship between invoice and due dates."""

    invoice_record = validation_input.invoice_record

    invoice_date_field = get_invoice_field(invoice_record, InvoiceFieldName.INVOICE_DATE)
    due_date_field = get_invoice_field(invoice_record, InvoiceFieldName.DUE_DATE)

    operands = (
        build_field_operand(invoice_record, InvoiceFieldName.INVOICE_DATE),
        build_field_operand(invoice_record, InvoiceFieldName.DUE_DATE),
    )

    if invoice_date_field is None and due_date_field is None:
        return build_validation_check(
            document_id=validation_input.document_id,
            check_type=ValidationCheckType.DATE_CONSISTENCY,
            scope_key="invoice",
            status=ValidationCheckStatus.NOT_APPLICABLE,
            severity=ValidationSeverity.INFORMATION,
            message="No invoice-date and due-date pair is available for comparison.",
            operands=operands,
            config=config,
        )

    invoice_date_value = (
        normalized_date_or_none(invoice_date_field.normalized_value) if invoice_date_field is not None else None
    )

    due_date_value = (
        normalized_date_or_none(due_date_field.normalized_value) if due_date_field is not None else None
    )

    if invoice_date_field is not None and invoice_date_value is None:
        return build_validation_check(
            document_id=validation_input.document_id,
            check_type=ValidationCheckType.DATE_CONSISTENCY,
            scope_key="invoice",
            status=ValidationCheckStatus.REVIEW_REQUIRED,
            severity=ValidationSeverity.ERROR,
            message="The invoice date could not be validated as a calendar date.",
            operands=operands,
            reason_codes=("INVALID_INVOICE_DATE",),
            config=config,
        )

    if due_date_field is not None and due_date_value is None:
        return build_validation_check(
            document_id=validation_input.document_id,
            check_type=ValidationCheckType.DATE_CONSISTENCY,
            scope_key="invoice",
            status=ValidationCheckStatus.REVIEW_REQUIRED,
            severity=ValidationSeverity.ERROR,
            message="The due date could not be validated as a calendar date.",
            operands=operands,
            reason_codes=("INVALID_DUE_DATE",),
            config=config,
        )

    if due_date_value is None:
        return build_validation_check(
            document_id=validation_input.document_id,
            check_type=ValidationCheckType.DATE_CONSISTENCY,
            scope_key="invoice",
            status=ValidationCheckStatus.NOT_APPLICABLE,
            severity=ValidationSeverity.INFORMATION,
            message="No due date is available; the date-order check does not apply.",
            operands=operands,
            config=config,
        )

    if invoice_date_value is None:
        return build_validation_check(
            document_id=validation_input.document_id,
            check_type=ValidationCheckType.DATE_CONSISTENCY,
            scope_key="invoice",
            status=ValidationCheckStatus.REVIEW_REQUIRED,
            severity=ValidationSeverity.ERROR,
            message="A due date is present without a valid invoice date.",
            operands=operands,
            reason_codes=("INVOICE_DATE_REQUIRED_FOR_DUE_DATE",),
            config=config,
        )

    if due_date_value < invoice_date_value:
        return build_validation_check(
            document_id=validation_input.document_id,
            check_type=ValidationCheckType.DATE_CONSISTENCY,
            scope_key="invoice",
            status=ValidationCheckStatus.FAILED,
            severity=ValidationSeverity.ERROR,
            message="The due date occurs before the invoice date.",
            operands=operands,
            expected_value=invoice_date_value,
            observed_value=due_date_value,
            reason_codes=("DUE_DATE_BEFORE_INVOICE_DATE",),
            config=config,
        )

    return build_validation_check(
        document_id=validation_input.document_id,
        check_type=ValidationCheckType.DATE_CONSISTENCY,
        scope_key="invoice",
        status=ValidationCheckStatus.PASSED,
        severity=ValidationSeverity.INFORMATION,
        message="The invoice date and due date are chronologically consistent.",
        operands=operands,
        config=config,
    )


def extract_currency_signals(invoice_record: NormalizedInvoiceRecord) -> set[str]:
    """Collect explicit currency codes and symbols from normalized monetary
    evidence without inferring from geography."""

    currency_signals: set[str] = set()

    currency_field = get_invoice_field(invoice_record, InvoiceFieldName.CURRENCY)

    if currency_field is not None and currency_field.normalized_value is not None:
        currency_signals.add(str(currency_field.normalized_value).strip().upper())

    monetary_fields = [get_invoice_field(invoice_record, field_name) for field_name in HEADER_MONETARY_FIELDS]

    line_fields = []

    for line_item in invoice_record.line_items:
        if line_item.currency:
            currency_signals.add(str(line_item.currency).strip().upper())

        line_fields.extend([line_item.unit_price, line_item.amount])

    for invoice_field in monetary_fields + line_fields:
        if invoice_field is None:
            continue

        raw_value = str(invoice_field.raw_value or "")

        for symbol, currency_code in CURRENCY_SYMBOL_MAP.items():
            if symbol in raw_value:
                currency_signals.add(currency_code)

        currency_codes = re.findall(r"\b(?:USD|GBP|EUR)\b", raw_value.upper())

        currency_signals.update(currency_codes)

    return {currency for currency in currency_signals if currency}


def validate_currency_consistency(
    validation_input: ValidationInput,
    *,
    config: FinancialValidationConfig,
) -> ValidationCheckResult:
    """Validate explicit currency evidence across the invoice."""

    invoice_record = validation_input.invoice_record

    currency_field = get_invoice_field(invoice_record, InvoiceFieldName.CURRENCY)

    header_currency = (
        str(currency_field.normalized_value).strip().upper()
        if (currency_field is not None and currency_field.normalized_value is not None)
        else None
    )

    currency_signals = extract_currency_signals(invoice_record)

    operands = (build_field_operand(invoice_record, InvoiceFieldName.CURRENCY),)

    if header_currency is None:
        return build_validation_check(
            document_id=validation_input.document_id,
            check_type=ValidationCheckType.CURRENCY_CONSISTENCY,
            scope_key="invoice",
            status=ValidationCheckStatus.REVIEW_REQUIRED,
            severity=ValidationSeverity.CRITICAL,
            message="The invoice currency is missing.",
            operands=operands,
            observed_value=(", ".join(sorted(currency_signals)) or None),
            reason_codes=("CURRENCY_MISSING",),
            config=config,
        )

    if header_currency not in SUPPORTED_CURRENCY_CODES:
        return build_validation_check(
            document_id=validation_input.document_id,
            check_type=ValidationCheckType.CURRENCY_CONSISTENCY,
            scope_key="invoice",
            status=ValidationCheckStatus.REVIEW_REQUIRED,
            severity=ValidationSeverity.ERROR,
            message="The normalized currency is not currently supported.",
            operands=operands,
            observed_value=header_currency,
            reason_codes=("UNSUPPORTED_CURRENCY",),
            config=config,
        )

    conflicting_currencies = currency_signals - {header_currency}

    if conflicting_currencies:
        return build_validation_check(
            document_id=validation_input.document_id,
            check_type=ValidationCheckType.CURRENCY_CONSISTENCY,
            scope_key="invoice",
            status=ValidationCheckStatus.FAILED,
            severity=ValidationSeverity.CRITICAL,
            message="Conflicting currency evidence was detected across the invoice.",
            operands=operands,
            expected_value=header_currency,
            observed_value=", ".join(sorted(currency_signals)),
            reason_codes=("CONFLICTING_CURRENCY_EVIDENCE",),
            config=config,
        )

    return build_validation_check(
        document_id=validation_input.document_id,
        check_type=ValidationCheckType.CURRENCY_CONSISTENCY,
        scope_key="invoice",
        status=ValidationCheckStatus.PASSED,
        severity=ValidationSeverity.INFORMATION,
        message="Available currency evidence is internally consistent.",
        operands=operands,
        observed_value=header_currency,
        config=config,
    )


def run_header_validation_checks(
    validation_input: ValidationInput,
    *,
    config: FinancialValidationConfig,
) -> tuple[ValidationCheckResult, ...]:
    """Run all Phase 5 header-level checks."""

    return (
        validate_inherited_review(validation_input, config=config),
        validate_required_financial_fields(validation_input, config=config),
        validate_monetary_values(validation_input, config=config),
        validate_date_consistency(validation_input, config=config),
        validate_currency_consistency(validation_input, config=config),
    )


# ============================================================
# 5. Line-item, subtotal and invoice-total reconciliation (cell 71)
# ============================================================


def validate_line_item_arithmetic(
    validation_input: ValidationInput,
    *,
    config: FinancialValidationConfig,
) -> tuple[ValidationCheckResult, ...]:
    """Validate quantity x unit price against the stated amount for every
    line item."""

    invoice_record = validation_input.invoice_record

    if not invoice_record.line_items:
        return (
            build_validation_check(
                document_id=validation_input.document_id,
                check_type=ValidationCheckType.LINE_ITEM_ARITHMETIC,
                scope_key="no-line-items",
                status=ValidationCheckStatus.NOT_APPLICABLE,
                severity=ValidationSeverity.INFORMATION,
                message="The invoice contains no normalized line items.",
                reason_codes=("NO_LINE_ITEMS",),
                config=config,
            ),
        )

    checks = []

    for line_item in invoice_record.line_items:
        quantity_operand = build_line_item_operand(line_item, "quantity")
        unit_price_operand = build_line_item_operand(line_item, "unit_price")
        amount_operand = build_line_item_operand(line_item, "amount")

        operands = (quantity_operand, unit_price_operand, amount_operand)

        quantity = to_decimal_or_none(quantity_operand.value)
        unit_price = to_decimal_or_none(unit_price_operand.value)
        observed_amount = to_decimal_or_none(amount_operand.value)

        missing_components = []

        if quantity is None:
            missing_components.append("QUANTITY")

        if unit_price is None:
            missing_components.append("UNIT_PRICE")

        if observed_amount is None:
            missing_components.append("LINE_AMOUNT")

        if missing_components:
            checks.append(
                build_validation_check(
                    document_id=validation_input.document_id,
                    check_type=ValidationCheckType.LINE_ITEM_ARITHMETIC,
                    scope_key=f"line-{line_item.line_number}",
                    status=ValidationCheckStatus.SKIPPED,
                    severity=ValidationSeverity.WARNING,
                    message=(
                        f"Line {line_item.line_number} could not be reconciled because the "
                        "following values are missing: " + ", ".join(missing_components) + "."
                    ),
                    operands=operands,
                    reason_codes=tuple(
                        f"LINE_ARITHMETIC_PREREQUISITE_MISSING:{component}"
                        for component in missing_components
                    ),
                    config=config,
                )
            )

            continue

        expected_amount = quantize_money(quantity * unit_price)
        observed_amount = quantize_money(observed_amount)

        difference = decimal_difference(observed_amount, expected_amount)

        if values_within_tolerance(observed_amount, expected_amount, config=config):
            status = ValidationCheckStatus.PASSED
            severity = ValidationSeverity.INFORMATION
            message = f"Line {line_item.line_number} amount agrees with quantity multiplied by unit price."
            reason_codes: tuple[str, ...] = tuple()

        else:
            status = ValidationCheckStatus.FAILED
            severity = ValidationSeverity.ERROR
            message = (
                f"Line {line_item.line_number} amount does not agree with quantity multiplied by unit price."
            )
            reason_codes = ("LINE_AMOUNT_MISMATCH",)

        checks.append(
            build_validation_check(
                document_id=validation_input.document_id,
                check_type=ValidationCheckType.LINE_ITEM_ARITHMETIC,
                scope_key=f"line-{line_item.line_number}",
                status=status,
                severity=severity,
                message=message,
                operands=operands,
                expected_value=expected_amount,
                observed_value=observed_amount,
                difference=difference,
                tolerance=config.monetary_tolerance,
                reason_codes=reason_codes,
                config=config,
            )
        )

    return tuple(checks)


def validate_line_items_to_subtotal(
    validation_input: ValidationInput,
    *,
    config: FinancialValidationConfig,
) -> ValidationCheckResult:
    """Reconcile the sum of all line amounts against subtotal."""

    invoice_record = validation_input.invoice_record

    subtotal_operand = build_field_operand(invoice_record, InvoiceFieldName.SUBTOTAL)
    subtotal = to_decimal_or_none(subtotal_operand.value)

    if not invoice_record.line_items:
        return build_validation_check(
            document_id=validation_input.document_id,
            check_type=ValidationCheckType.LINE_ITEMS_TO_SUBTOTAL,
            scope_key="invoice",
            status=ValidationCheckStatus.NOT_APPLICABLE,
            severity=ValidationSeverity.INFORMATION,
            message="No line items are available for subtotal reconciliation.",
            operands=(subtotal_operand,),
            reason_codes=("NO_LINE_ITEMS",),
            config=config,
        )

    line_amount_operands = tuple(
        build_line_item_operand(
            line_item, "amount", operand_name=f"line_{line_item.line_number}_amount"
        )
        for line_item in invoice_record.line_items
    )

    operands = (*line_amount_operands, subtotal_operand)

    if subtotal is None:
        return build_validation_check(
            document_id=validation_input.document_id,
            check_type=ValidationCheckType.LINE_ITEMS_TO_SUBTOTAL,
            scope_key="invoice",
            status=ValidationCheckStatus.SKIPPED,
            severity=ValidationSeverity.WARNING,
            message="Line amounts cannot be reconciled because the subtotal is missing.",
            operands=operands,
            reason_codes=("SUBTOTAL_MISSING",),
            config=config,
        )

    missing_line_numbers = [
        line_item.line_number
        for line_item in invoice_record.line_items
        if (line_item.amount is None or to_decimal_or_none(line_item.amount.normalized_value) is None)
    ]

    if missing_line_numbers:
        return build_validation_check(
            document_id=validation_input.document_id,
            check_type=ValidationCheckType.LINE_ITEMS_TO_SUBTOTAL,
            scope_key="invoice",
            status=ValidationCheckStatus.SKIPPED,
            severity=ValidationSeverity.WARNING,
            message="The subtotal check was skipped because one or more line amounts are missing.",
            operands=operands,
            observed_value=subtotal,
            reason_codes=tuple(f"LINE_AMOUNT_MISSING:{line_number}" for line_number in missing_line_numbers),
            config=config,
        )

    line_amounts = [
        to_decimal_or_none(line_item.amount.normalized_value) for line_item in invoice_record.line_items
    ]

    calculated_subtotal = quantize_money(sum(line_amounts, Decimal("0.00")))
    observed_subtotal = quantize_money(subtotal)

    difference = decimal_difference(observed_subtotal, calculated_subtotal)

    if values_within_tolerance(observed_subtotal, calculated_subtotal, config=config):
        return build_validation_check(
            document_id=validation_input.document_id,
            check_type=ValidationCheckType.LINE_ITEMS_TO_SUBTOTAL,
            scope_key="invoice",
            status=ValidationCheckStatus.PASSED,
            severity=ValidationSeverity.INFORMATION,
            message="The sum of line amounts agrees with the invoice subtotal.",
            operands=operands,
            expected_value=calculated_subtotal,
            observed_value=observed_subtotal,
            difference=difference,
            tolerance=config.monetary_tolerance,
            config=config,
        )

    return build_validation_check(
        document_id=validation_input.document_id,
        check_type=ValidationCheckType.LINE_ITEMS_TO_SUBTOTAL,
        scope_key="invoice",
        status=ValidationCheckStatus.FAILED,
        severity=ValidationSeverity.ERROR,
        message="The sum of line amounts does not agree with the invoice subtotal.",
        operands=operands,
        expected_value=calculated_subtotal,
        observed_value=observed_subtotal,
        difference=difference,
        tolerance=config.monetary_tolerance,
        reason_codes=("LINE_ITEMS_SUBTOTAL_MISMATCH",),
        config=config,
    )


def validate_invoice_total(
    validation_input: ValidationInput,
    *,
    config: FinancialValidationConfig,
) -> ValidationCheckResult:
    """Reconcile:

    subtotal - absolute discount + tax + shipping = total

    Missing optional adjustments are treated as zero, but missing subtotal
    or total values are never inferred.
    """

    invoice_record = validation_input.invoice_record

    subtotal_operand = build_field_operand(invoice_record, InvoiceFieldName.SUBTOTAL)
    discount_operand = build_field_operand(invoice_record, InvoiceFieldName.DISCOUNT_AMOUNT)
    tax_operand = build_field_operand(invoice_record, InvoiceFieldName.TAX_AMOUNT)
    shipping_operand = build_field_operand(invoice_record, InvoiceFieldName.SHIPPING_AMOUNT)
    total_operand = build_field_operand(invoice_record, InvoiceFieldName.TOTAL_AMOUNT)

    operands = (subtotal_operand, discount_operand, tax_operand, shipping_operand, total_operand)

    subtotal = to_decimal_or_none(subtotal_operand.value)
    discount = to_decimal_or_none(discount_operand.value)
    tax = to_decimal_or_none(tax_operand.value)
    shipping = to_decimal_or_none(shipping_operand.value)
    observed_total = to_decimal_or_none(total_operand.value)

    if subtotal is None:
        return build_validation_check(
            document_id=validation_input.document_id,
            check_type=ValidationCheckType.INVOICE_TOTAL_RECONCILIATION,
            scope_key="invoice",
            status=ValidationCheckStatus.SKIPPED,
            severity=ValidationSeverity.WARNING,
            message="The invoice total cannot be reconciled because the subtotal is missing.",
            operands=operands,
            observed_value=observed_total,
            reason_codes=("SUBTOTAL_REQUIRED_FOR_TOTAL_CHECK",),
            config=config,
        )

    invalid_optional_fields = []

    optional_field_pairs = (
        (InvoiceFieldName.DISCOUNT_AMOUNT, discount_operand, discount),
        (InvoiceFieldName.TAX_AMOUNT, tax_operand, tax),
        (InvoiceFieldName.SHIPPING_AMOUNT, shipping_operand, shipping),
    )

    for field_name, operand, decimal_value in optional_field_pairs:
        if operand.value is not None and decimal_value is None:
            invalid_optional_fields.append(field_name.value)

    if invalid_optional_fields:
        return build_validation_check(
            document_id=validation_input.document_id,
            check_type=ValidationCheckType.INVOICE_TOTAL_RECONCILIATION,
            scope_key="invoice",
            status=ValidationCheckStatus.REVIEW_REQUIRED,
            severity=ValidationSeverity.ERROR,
            message="The invoice total cannot be reconciled because one or more adjustments are invalid.",
            operands=operands,
            observed_value=observed_total,
            reason_codes=tuple(f"INVALID_TOTAL_ADJUSTMENT:{field_name}" for field_name in invalid_optional_fields),
            config=config,
        )

    applied_discount = abs(discount if discount is not None else Decimal("0.00"))
    applied_tax = tax if tax is not None else Decimal("0.00")
    applied_shipping = shipping if shipping is not None else Decimal("0.00")

    calculated_total = quantize_money(subtotal - applied_discount + applied_tax + applied_shipping)

    if observed_total is None:
        return build_validation_check(
            document_id=validation_input.document_id,
            check_type=ValidationCheckType.INVOICE_TOTAL_RECONCILIATION,
            scope_key="invoice",
            status=ValidationCheckStatus.REVIEW_REQUIRED,
            severity=ValidationSeverity.CRITICAL,
            message=(
                "The expected invoice total was calculated for validation, but the source total is "
                "missing. The calculated value was not inserted into the invoice record."
            ),
            operands=operands,
            expected_value=calculated_total,
            observed_value=None,
            tolerance=config.monetary_tolerance,
            reason_codes=("TOTAL_AMOUNT_MISSING", "CALCULATED_TOTAL_NOT_PERSISTED"),
            config=config,
        )

    observed_total = quantize_money(observed_total)

    difference = decimal_difference(observed_total, calculated_total)

    if values_within_tolerance(observed_total, calculated_total, config=config):
        return build_validation_check(
            document_id=validation_input.document_id,
            check_type=ValidationCheckType.INVOICE_TOTAL_RECONCILIATION,
            scope_key="invoice",
            status=ValidationCheckStatus.PASSED,
            severity=ValidationSeverity.INFORMATION,
            message="The invoice total agrees with subtotal, discount, tax and shipping.",
            operands=operands,
            expected_value=calculated_total,
            observed_value=observed_total,
            difference=difference,
            tolerance=config.monetary_tolerance,
            config=config,
        )

    return build_validation_check(
        document_id=validation_input.document_id,
        check_type=ValidationCheckType.INVOICE_TOTAL_RECONCILIATION,
        scope_key="invoice",
        status=ValidationCheckStatus.FAILED,
        severity=ValidationSeverity.CRITICAL,
        message="The invoice total does not agree with subtotal, discount, tax and shipping.",
        operands=operands,
        expected_value=calculated_total,
        observed_value=observed_total,
        difference=difference,
        tolerance=config.monetary_tolerance,
        reason_codes=("INVOICE_TOTAL_MISMATCH",),
        config=config,
    )


def run_arithmetic_validation_checks(
    validation_input: ValidationInput,
    *,
    config: FinancialValidationConfig,
) -> tuple[ValidationCheckResult, ...]:
    """Run all deterministic arithmetic checks."""

    line_checks = validate_line_item_arithmetic(validation_input, config=config)
    subtotal_check = validate_line_items_to_subtotal(validation_input, config=config)
    total_check = validate_invoice_total(validation_input, config=config)

    return (*line_checks, subtotal_check, total_check)


# ============================================================
# 6. Orchestration and status assignment (cell 72)
# ============================================================


def validate_phase_5_input_integrity(validation_input: ValidationInput) -> None:
    """Confirm that the Phase 5 envelope and its invoice record describe the
    same immutable document.

    Kept as a plain `ValueError` (not `FinancialValidationIntegrityError`),
    verbatim from the notebook: `process_financial_validation` below
    catches it and folds its message into `FinancialValidationResult.errors`
    (task §16/§17's `PHASE_5_SOURCE_SHA256_MISMATCH` golden assertion
    depends on that exact reason string surviving into the error text, not
    on any particular exception type).
    """

    invoice_record = validation_input.invoice_record

    if validation_input.batch_id != invoice_record.batch_id:
        raise ValueError("PHASE_5_BATCH_ID_MISMATCH")

    if validation_input.document_id != invoice_record.document_id:
        raise ValueError("PHASE_5_DOCUMENT_ID_MISMATCH")

    if validation_input.source_name != invoice_record.source_name:
        raise ValueError("PHASE_5_SOURCE_NAME_MISMATCH")

    if validation_input.source_document_sha256 != invoice_record.source_document_sha256:
        raise ValueError("PHASE_5_SOURCE_SHA256_MISMATCH")

    if validation_input.normalization_version != invoice_record.normalization_version:
        raise ValueError("PHASE_5_NORMALIZATION_VERSION_MISMATCH")


def summarize_checks_fail_closed(checks: tuple[ValidationCheckResult, ...]) -> FinancialValidationSummary:
    """Summarize checks using the Phase 5 fail-closed policy.

    FAILED, REVIEW_REQUIRED and SKIPPED financial checks all require
    review. NOT_APPLICABLE does not.
    """

    review_required = any(
        check.status
        in {
            ValidationCheckStatus.FAILED,
            ValidationCheckStatus.REVIEW_REQUIRED,
            ValidationCheckStatus.SKIPPED,
        }
        for check in checks
    )

    return FinancialValidationSummary(
        total_checks=len(checks),
        passed_checks=sum(check.status == ValidationCheckStatus.PASSED for check in checks),
        failed_checks=sum(check.status == ValidationCheckStatus.FAILED for check in checks),
        review_required_checks=sum(check.status == ValidationCheckStatus.REVIEW_REQUIRED for check in checks),
        not_applicable_checks=sum(check.status == ValidationCheckStatus.NOT_APPLICABLE for check in checks),
        skipped_checks=sum(check.status == ValidationCheckStatus.SKIPPED for check in checks),
        review_required=review_required,
    )


def collect_validation_review_reasons(
    invoice_record: NormalizedInvoiceRecord,
    checks: tuple[ValidationCheckResult, ...],
) -> tuple[str, ...]:
    """Combine inherited and Phase 5 review reasons while preserving
    deterministic order."""

    reasons = list(invoice_record.review_reasons)

    for check in checks:
        if check.status not in {
            ValidationCheckStatus.FAILED,
            ValidationCheckStatus.REVIEW_REQUIRED,
            ValidationCheckStatus.SKIPPED,
        }:
            continue

        if check.reason_codes:
            for reason_code in check.reason_codes:
                if reason_code not in reasons:
                    reasons.append(reason_code)

        else:
            fallback_reason = f"VALIDATION_CHECK_REQUIRES_REVIEW:{check.check_type.value}"

            if fallback_reason not in reasons:
                reasons.append(fallback_reason)

    return tuple(reasons)


def determine_validation_status(
    invoice_record: NormalizedInvoiceRecord,
    checks: tuple[ValidationCheckResult, ...],
) -> ValidationStatus:
    """Determine the overall business-processing status.

    A failed financial check is a review condition, not a software
    execution failure.
    """

    requires_review = invoice_record.review_required or any(
        check.status
        in {
            ValidationCheckStatus.FAILED,
            ValidationCheckStatus.REVIEW_REQUIRED,
            ValidationCheckStatus.SKIPPED,
        }
        for check in checks
    )

    if requires_review:
        return ValidationStatus.REVIEW_REQUIRED

    return ValidationStatus.SUCCEEDED


def build_failed_validation_summary() -> FinancialValidationSummary:
    """Build a fail-closed summary for execution failures."""

    return FinancialValidationSummary(
        total_checks=0,
        passed_checks=0,
        failed_checks=0,
        review_required_checks=0,
        not_applicable_checks=0,
        skipped_checks=0,
        review_required=True,
    )


def process_financial_validation(
    validation_input: ValidationInput,
    *,
    config: FinancialValidationConfig,
) -> FinancialValidationResult:
    """Run the complete deterministic Phase 5 validation process for one
    normalized invoice. Public Phase 5 entry point (does not persist)."""

    invoice_record = validation_input.invoice_record

    try:
        validate_phase_5_input_integrity(validation_input)

        header_checks = run_header_validation_checks(validation_input, config=config)
        arithmetic_checks = run_arithmetic_validation_checks(validation_input, config=config)

        checks = (*header_checks, *arithmetic_checks)

        if not checks:
            raise ValueError("PHASE_5_NO_CHECKS_PRODUCED")

        check_ids = [check.check_id for check in checks]

        if len(check_ids) != len(set(check_ids)):
            raise ValueError("PHASE_5_DUPLICATE_CHECK_IDS")

        summary = summarize_checks_fail_closed(checks)
        status = determine_validation_status(invoice_record, checks)
        review_required = status == ValidationStatus.REVIEW_REQUIRED

        review_reasons = collect_validation_review_reasons(invoice_record, checks)

        if review_required and not review_reasons:
            review_reasons = ("PHASE_5_REVIEW_REQUIRED",)

        event = ValidationEvent(
            event_type="FINANCIAL_VALIDATION",
            status=status,
            batch_id=validation_input.batch_id,
            document_id=validation_input.document_id,
            occurred_at=validation_utc_now(),
            message=f"Financial validation completed with status {status.value}.",
            review_required=review_required,
        )

        return FinancialValidationResult(
            batch_id=validation_input.batch_id,
            document_id=validation_input.document_id,
            source_name=validation_input.source_name,
            source_document_sha256=validation_input.source_document_sha256,
            invoice_record_id=invoice_record.invoice_record_id,
            normalization_version=validation_input.normalization_version,
            validation_version=config.validation_version,
            status=status,
            checks=tuple(checks),
            summary=summary,
            event=event,
            review_required=review_required,
            review_reasons=review_reasons,
            errors=tuple(),
        )

    except Exception as exc:
        error_message = f"{type(exc).__name__}: {exc}"

        event = ValidationEvent(
            event_type="FINANCIAL_VALIDATION",
            status=ValidationStatus.FAILED,
            batch_id=validation_input.batch_id,
            document_id=validation_input.document_id,
            occurred_at=validation_utc_now(),
            message="Financial validation failed closed because of an execution or integrity error.",
            review_required=True,
        )

        return FinancialValidationResult(
            batch_id=validation_input.batch_id,
            document_id=validation_input.document_id,
            source_name=validation_input.source_name,
            source_document_sha256=validation_input.source_document_sha256,
            invoice_record_id=invoice_record.invoice_record_id,
            normalization_version=validation_input.normalization_version,
            validation_version=config.validation_version,
            status=ValidationStatus.FAILED,
            checks=tuple(),
            summary=build_failed_validation_summary(),
            event=event,
            review_required=True,
            review_reasons=("PHASE_5_EXECUTION_FAILURE",),
            errors=(error_message,),
        )


# ============================================================
# 7. Validation artifact and audit-event persistence (cell 73)
# ============================================================

_TIMESTAMP_KEYS = {"occurred_at"}


def _strip_timestamps(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _strip_timestamps(nested) for key, nested in value.items() if key not in _TIMESTAMP_KEYS}

    if isinstance(value, list):
        return [_strip_timestamps(item) for item in value]

    return value


def _write_json_atomic_idempotent(destination: Path, payload: Any) -> None:
    """Persist idempotently: an identical rewrite (ignoring `occurred_at`,
    which always advances between two otherwise-identical reprocessing
    runs) is a silent no-op; a genuine collision fails closed (task
    §16/§18 -- new in M6, not present in the notebook, the same category of
    deliberate, tested addition as M5's `_write_json_safe_idempotent`)."""

    safe_payload = phase_5_json_safe(payload)

    if destination.exists():
        existing_payload = json.loads(destination.read_text(encoding="utf-8"))

        if _strip_timestamps(existing_payload) != _strip_timestamps(safe_payload):
            raise FinancialValidationIntegrityError(
                "PHASE_5_ARTIFACT_COLLISION", {"path": str(destination)}
            )

        return

    write_json_atomic(destination, payload)


def _write_jsonl_atomic_idempotent(destination: Path, records: tuple[Any, ...]) -> None:
    """`validation_checks.jsonl` idempotent counterpart of
    `_write_json_atomic_idempotent`. Check records carry no timestamps, so
    an identical rewrite always compares equal line-for-line."""

    safe_records = [phase_5_json_safe(record) for record in records]

    if destination.exists():
        existing_lines = [line for line in destination.read_text(encoding="utf-8").splitlines() if line.strip()]
        existing_records = [json.loads(line) for line in existing_lines]

        if existing_records != safe_records:
            raise FinancialValidationIntegrityError(
                "PHASE_5_ARTIFACT_COLLISION", {"path": str(destination)}
            )

        return

    write_jsonl_atomic(destination, records)


def phase_5_artifact_directory(
    result: FinancialValidationResult,
    *,
    config: FinancialValidationConfig,
) -> Path:
    """Return the deterministic artifact directory for one document and
    validation version."""

    return config.artifact_root / str(result.batch_id) / str(result.document_id) / result.validation_version


def persist_financial_validation_result(
    result: FinancialValidationResult,
    *,
    config: FinancialValidationConfig,
) -> dict[str, Path]:
    """Persist one Phase 5 result, its checks, its audit event and an
    integrity manifest.

    Writes idempotently (task §16/§18): a rerun with identical business
    content is a silent no-op; a genuine collision fails closed with
    `FinancialValidationIntegrityError` -- new in M6, not present in the
    notebook (which always overwrote unconditionally).
    """

    if result.validation_version != config.validation_version:
        raise ValueError("VALIDATION_VERSION_MISMATCH")

    artifact_directory = phase_5_artifact_directory(result, config=config)
    artifact_directory.mkdir(parents=True, exist_ok=True)

    result_path = artifact_directory / "financial_validation_result.json"
    checks_path = artifact_directory / "validation_checks.jsonl"
    event_path = artifact_directory / "validation_event.json"
    manifest_path = artifact_directory / "artifact_manifest.json"

    _write_json_atomic_idempotent(result_path, result)
    _write_jsonl_atomic_idempotent(checks_path, result.checks)
    _write_json_atomic_idempotent(event_path, result.event)

    artifact_hashes = {
        result_path.name: calculate_file_sha256(result_path),
        checks_path.name: calculate_file_sha256(checks_path),
        event_path.name: calculate_file_sha256(event_path),
    }

    manifest_payload = {
        "artifact_type": "PHASE_5_FINANCIAL_VALIDATION",
        "batch_id": result.batch_id,
        "document_id": result.document_id,
        "invoice_record_id": result.invoice_record_id,
        "source_name": result.source_name,
        "source_document_sha256": result.source_document_sha256,
        "normalization_version": result.normalization_version,
        "validation_version": result.validation_version,
        "status": result.status,
        "review_required": result.review_required,
        "check_count": len(result.checks),
        "artifact_hashes": artifact_hashes,
    }

    _write_json_atomic_idempotent(manifest_path, manifest_payload)

    return {
        "directory": artifact_directory,
        "result": result_path,
        "checks": checks_path,
        "event": event_path,
        "manifest": manifest_path,
    }


def validate_persisted_phase_5_artifacts(
    result: FinancialValidationResult,
    artifact_paths: dict[str, Path],
) -> None:
    """Reopen and validate every persisted artifact."""

    required_keys = {"directory", "result", "checks", "event", "manifest"}

    if set(artifact_paths) != required_keys:
        raise AssertionError("Unexpected Phase 5 artifact set.")

    for artifact_key in ("result", "checks", "event", "manifest"):
        artifact_path = artifact_paths[artifact_key]

        if not artifact_path.is_file():
            raise AssertionError(f"Missing Phase 5 artifact: {artifact_path}")

    persisted_result = json.loads(artifact_paths["result"].read_text(encoding="utf-8"))
    persisted_event = json.loads(artifact_paths["event"].read_text(encoding="utf-8"))
    persisted_manifest = json.loads(artifact_paths["manifest"].read_text(encoding="utf-8"))

    check_lines = [line for line in artifact_paths["checks"].read_text(encoding="utf-8").splitlines() if line.strip()]
    persisted_checks = [json.loads(line) for line in check_lines]

    assert persisted_result["batch_id"] == str(result.batch_id)
    assert persisted_result["document_id"] == str(result.document_id)
    assert persisted_result["invoice_record_id"] == str(result.invoice_record_id)
    assert persisted_result["status"] == result.status.value
    assert persisted_result["review_required"] == result.review_required

    assert persisted_event["status"] == result.status.value
    assert persisted_event["review_required"] == result.review_required

    assert len(persisted_checks) == len(result.checks)

    assert persisted_manifest["check_count"] == len(result.checks)
    assert persisted_manifest["source_document_sha256"] == result.source_document_sha256

    for filename, expected_hash in persisted_manifest["artifact_hashes"].items():
        artifact_path = artifact_paths["directory"] / filename
        assert calculate_file_sha256(artifact_path) == expected_hash
