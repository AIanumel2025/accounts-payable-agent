"""Unit tests for the Phase 5 (financial validation) processing functions in
`ap_agent.tools.financial_validation` (M6): Decimal utilities, deterministic
IDs, evidence-linked operands, the check builder, header-level checks
(required fields, monetary validity, date consistency, currency
consistency) and arithmetic checks (line items, subtotal, invoice total).
Synthetic `NormalizedInvoiceRecord` contracts only (task §18) -- no invoice
fixtures.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest

from ap_agent.config.settings import FinancialValidationConfig
from ap_agent.models.normalization import (
    EvidenceReference,
    EvidenceReferenceType,
    ExtractionMethod,
    InvoiceFieldName,
    NormalizedInvoiceField,
    NormalizedInvoiceRecord,
    NormalizedLineItem,
    NormalizedValueType,
)
from ap_agent.models.ocr import BoundingBox
from ap_agent.models.validation import (
    ValidationCheckStatus,
    ValidationCheckType,
    ValidationInput,
    ValidationSeverity,
)
from ap_agent.tools.financial_validation import (
    build_field_operand,
    build_line_item_operand,
    build_validation_check,
    canonical_operand_value,
    create_validation_id,
    decimal_difference,
    evidence_ids_from_field,
    extract_currency_signals,
    normalized_date_or_none,
    quantize_money,
    run_arithmetic_validation_checks,
    run_header_validation_checks,
    to_decimal_or_none,
    validate_currency_consistency,
    validate_date_consistency,
    validate_invoice_total,
    validate_line_item_arithmetic,
    validate_line_items_to_subtotal,
    validate_monetary_values,
    validate_required_financial_fields,
    values_within_tolerance,
)

pytestmark = pytest.mark.unit


# --------------------------------------------------------------------------
# Synthetic contract builders
# --------------------------------------------------------------------------


def _config(**overrides) -> FinancialValidationConfig:
    return FinancialValidationConfig(artifact_root=Path("/tmp/ap-agent-phase5"), **overrides)


def _evidence_reference() -> EvidenceReference:
    return EvidenceReference(
        reference_id=uuid4(),
        reference_type=EvidenceReferenceType.TOKEN,
        page_number=1,
        reading_order=1,
        raw_text="69.22",
        confidence=95.0,
        bounding_box=BoundingBox(x=0, y=0, width=10, height=10),
    )


def _field(
    field_name: InvoiceFieldName,
    normalized_value,
    *,
    raw_value: str | None = None,
    value_type: NormalizedValueType = NormalizedValueType.TEXT,
    evidence_references: tuple[EvidenceReference, ...] = (),
) -> NormalizedInvoiceField:
    return NormalizedInvoiceField(
        field_id=uuid4(),
        field_name=field_name,
        raw_value=raw_value if raw_value is not None else ("" if normalized_value is None else str(normalized_value)),
        normalized_value=normalized_value,
        value_type=value_type,
        confidence=95.0,
        extraction_method=ExtractionMethod.LABEL_VALUE,
        evidence_references=evidence_references,
    )


def _line_item(
    line_number: int,
    *,
    quantity=None,
    unit_price=None,
    amount=None,
    description=None,
    currency: str | None = None,
) -> NormalizedLineItem:
    return NormalizedLineItem(
        line_item_id=uuid4(),
        line_number=line_number,
        description=_field(InvoiceFieldName.LINE_DESCRIPTION, description) if description is not None else None,
        quantity=(
            _field(InvoiceFieldName.LINE_QUANTITY, quantity, value_type=NormalizedValueType.DECIMAL)
            if quantity is not None
            else None
        ),
        unit_price=(
            _field(InvoiceFieldName.LINE_UNIT_PRICE, unit_price, value_type=NormalizedValueType.DECIMAL)
            if unit_price is not None
            else None
        ),
        amount=(
            _field(InvoiceFieldName.LINE_AMOUNT, amount, value_type=NormalizedValueType.DECIMAL)
            if amount is not None
            else None
        ),
        currency=currency,
        confidence=95.0,
        evidence_references=(),
    )


def _record(
    fields: tuple[NormalizedInvoiceField, ...] = (),
    line_items: tuple[NormalizedLineItem, ...] = (),
    *,
    review_required: bool = False,
    review_reasons: tuple[str, ...] = (),
) -> NormalizedInvoiceRecord:
    return NormalizedInvoiceRecord(
        invoice_record_id=uuid4(),
        batch_id=uuid4(),
        document_id=uuid4(),
        source_name="invoice.pdf",
        source_document_sha256="a" * 64,
        fields=fields,
        line_items=line_items,
        normalization_version="normalization-v1",
        created_at=datetime.now(timezone.utc),
        review_required=review_required,
        review_reasons=review_reasons,
    )


def _validation_input(record: NormalizedInvoiceRecord) -> ValidationInput:
    return ValidationInput(
        batch_id=record.batch_id,
        document_id=record.document_id,
        source_name=record.source_name,
        source_document_sha256=record.source_document_sha256,
        normalization_version=record.normalization_version,
        invoice_record=record,
    )


def _header_check(checks, check_type: ValidationCheckType):
    return next(check for check in checks if check.check_type == check_type)


# --------------------------------------------------------------------------
# Decimal utilities
# --------------------------------------------------------------------------


def test_to_decimal_or_none_handles_supported_types():
    assert to_decimal_or_none(None) is None
    assert to_decimal_or_none(True) is None
    assert to_decimal_or_none(False) is None
    assert to_decimal_or_none(Decimal("1.5")) == Decimal("1.5")
    assert to_decimal_or_none(3) == Decimal(3)
    assert to_decimal_or_none(1.5) == Decimal("1.5")
    assert to_decimal_or_none("  1,234.56 ") == Decimal("1234.56")
    assert to_decimal_or_none("$69.22") == Decimal("69.22")
    assert to_decimal_or_none("£10.00") == Decimal("10.00")
    assert to_decimal_or_none("€10.00") == Decimal("10.00")


def test_to_decimal_or_none_rejects_malformed_or_empty_values():
    assert to_decimal_or_none("") is None
    assert to_decimal_or_none("   ") is None
    assert to_decimal_or_none("not-a-number") is None
    assert to_decimal_or_none({"amount": 1}) is None
    assert to_decimal_or_none(date(2024, 1, 1)) is None


def test_quantize_money_rounds_half_up():
    assert quantize_money(Decimal("1.005")) == Decimal("1.01")
    assert quantize_money(Decimal("1.004")) == Decimal("1.00")
    assert quantize_money(Decimal("1")) == Decimal("1.00")


def test_decimal_difference_is_absolute_and_quantized():
    assert decimal_difference(Decimal("10.02"), Decimal("10.01")) == Decimal("0.01")
    assert decimal_difference(Decimal("10.01"), Decimal("10.02")) == Decimal("0.01")
    assert decimal_difference(Decimal("5.00"), Decimal("5.00")) == Decimal("0.00")


def test_values_within_tolerance_boundaries():
    config = _config()

    assert values_within_tolerance(Decimal("69.22"), Decimal("69.220"), config=config)
    assert values_within_tolerance(Decimal("69.22"), Decimal("69.23"), config=config)  # exactly at 0.01
    assert not values_within_tolerance(Decimal("69.22"), Decimal("69.25"), config=config)
    assert values_within_tolerance(Decimal("69.22"), Decimal("69.25"), Decimal("0.05"), config=config)


# --------------------------------------------------------------------------
# Deterministic validation IDs and explicit configuration
# --------------------------------------------------------------------------


def test_create_validation_id_is_deterministic_and_identity_sensitive():
    config = _config()
    document_id = uuid4()

    first_id = create_validation_id(config, "check", document_id, "LINE_ITEM_ARITHMETIC", "line-1")
    second_id = create_validation_id(config, "check", document_id, "LINE_ITEM_ARITHMETIC", "line-1")
    different_id = create_validation_id(config, "check", document_id, "LINE_ITEM_ARITHMETIC", "line-2")

    assert first_id == second_id
    assert first_id != different_id


def test_create_validation_id_is_timestamp_independent():
    config = _config()
    document_id = uuid4()

    import time

    first_id = create_validation_id(config, "check", document_id, "a")
    time.sleep(0.01)
    second_id = create_validation_id(config, "check", document_id, "a")

    assert first_id == second_id


def test_changing_validation_version_changes_ids_only():
    document_id = uuid4()

    default_config = _config()
    other_config = _config(validation_version="financial-validation-v2")

    default_id = create_validation_id(default_config, "check", document_id, "a")
    other_id = create_validation_id(other_config, "check", document_id, "a")

    assert default_id != other_id


def test_config_instances_do_not_share_mutable_state():
    first = _config(monetary_tolerance=Decimal("0.05"))
    second = _config()

    assert first.monetary_tolerance == Decimal("0.05")
    assert second.monetary_tolerance == Decimal("0.01")


# --------------------------------------------------------------------------
# Evidence-linked operands and the check builder
# --------------------------------------------------------------------------


def test_build_field_operand_carries_evidence_references():
    evidence = _evidence_reference()
    record = _record((_field(InvoiceFieldName.TOTAL_AMOUNT, Decimal("69.22"), evidence_references=(evidence,)),))

    operand = build_field_operand(record, InvoiceFieldName.TOTAL_AMOUNT)

    assert operand.value == "69.22"
    assert operand.evidence_reference_ids == (evidence.reference_id,)
    assert evidence_ids_from_field(record.get_field(InvoiceFieldName.TOTAL_AMOUNT)) == (evidence.reference_id,)


def test_build_field_operand_missing_field_has_no_value():
    record = _record(())

    operand = build_field_operand(record, InvoiceFieldName.TOTAL_AMOUNT)

    assert operand.value is None
    assert operand.field_id is None
    assert operand.evidence_reference_ids == ()


def test_build_line_item_operand_rejects_unsupported_attribute():
    line_item = _line_item(1, quantity=Decimal("1"))

    with pytest.raises(ValueError):
        build_line_item_operand(line_item, "description")


def test_canonical_operand_value_formats_decimals_and_dates():
    assert canonical_operand_value(None) is None
    assert canonical_operand_value(Decimal("69.220")) == "69.220"
    assert canonical_operand_value(date(2024, 1, 2)) == "2024-01-02"
    assert canonical_operand_value("USD") == "USD"


def test_build_validation_check_deduplicates_reason_codes_and_marks_review():
    config = _config()
    document_id = uuid4()

    check = build_validation_check(
        document_id=document_id,
        check_type=ValidationCheckType.MONETARY_VALUE_VALIDITY,
        scope_key="invoice",
        status=ValidationCheckStatus.FAILED,
        severity=ValidationSeverity.ERROR,
        message="invalid",
        reason_codes=("A", "A", "B"),
        config=config,
    )

    assert check.review_required is True
    assert check.reason_codes == ("A", "B")

    passed_check = build_validation_check(
        document_id=document_id,
        check_type=ValidationCheckType.MONETARY_VALUE_VALIDITY,
        scope_key="invoice",
        status=ValidationCheckStatus.PASSED,
        severity=ValidationSeverity.INFORMATION,
        message="ok",
        config=config,
    )

    assert passed_check.review_required is False


# --------------------------------------------------------------------------
# Required financial fields
# --------------------------------------------------------------------------


def test_required_financial_fields_passes_when_present():
    config = _config()
    record = _record(
        (
            _field(InvoiceFieldName.CURRENCY, "USD"),
            _field(InvoiceFieldName.TOTAL_AMOUNT, Decimal("69.22")),
        )
    )

    check = validate_required_financial_fields(_validation_input(record), config=config)

    assert check.status == ValidationCheckStatus.PASSED


def test_required_financial_fields_flags_missing_fields_for_review():
    config = _config()
    record = _record(())

    check = validate_required_financial_fields(_validation_input(record), config=config)

    assert check.status == ValidationCheckStatus.REVIEW_REQUIRED
    assert "REQUIRED_FINANCIAL_FIELD_MISSING:CURRENCY" in check.reason_codes
    assert "REQUIRED_FINANCIAL_FIELD_MISSING:TOTAL_AMOUNT" in check.reason_codes


# --------------------------------------------------------------------------
# Monetary-value validity
# --------------------------------------------------------------------------


def test_monetary_values_pass_for_valid_amounts():
    config = _config()
    record = _record((_field(InvoiceFieldName.SUBTOTAL, Decimal("10.00")),))

    check = validate_monetary_values(_validation_input(record), config=config)

    assert check.status == ValidationCheckStatus.PASSED


def test_monetary_values_reject_negative_amount_outside_discount():
    config = _config()
    record = _record((_field(InvoiceFieldName.SUBTOTAL, Decimal("-10.00")),))

    check = validate_monetary_values(_validation_input(record), config=config)

    assert check.status == ValidationCheckStatus.FAILED
    assert "UNEXPECTED_NEGATIVE_VALUE:SUBTOTAL" in check.reason_codes


def test_monetary_values_allow_negative_discount():
    config = _config()
    record = _record((_field(InvoiceFieldName.DISCOUNT_AMOUNT, Decimal("-12.54")),))

    check = validate_monetary_values(_validation_input(record), config=config)

    assert check.status == ValidationCheckStatus.PASSED


def test_monetary_values_flag_amount_exceeding_configured_limit():
    config = _config(maximum_absolute_amount=Decimal("100.00"))
    record = _record((_field(InvoiceFieldName.SUBTOTAL, Decimal("1000.00")),))

    check = validate_monetary_values(_validation_input(record), config=config)

    assert check.status == ValidationCheckStatus.FAILED
    assert "MONETARY_VALUE_EXCEEDS_LIMIT:SUBTOTAL" in check.reason_codes


def test_monetary_values_flag_invalid_line_amount():
    config = _config()
    line_item = _line_item(1, quantity=Decimal("1"), unit_price=Decimal("1"), amount=None)
    invalid_amount_field = _field(InvoiceFieldName.LINE_AMOUNT, None, raw_value="not-a-number")
    line_item = NormalizedLineItem(
        line_item_id=line_item.line_item_id,
        line_number=1,
        description=None,
        quantity=line_item.quantity,
        unit_price=line_item.unit_price,
        amount=invalid_amount_field,
        currency=None,
        confidence=95.0,
        evidence_references=(),
    )
    record = _record((), (line_item,))

    check = validate_monetary_values(_validation_input(record), config=config)

    assert check.status == ValidationCheckStatus.FAILED
    assert "INVALID_LINE_MONETARY_VALUE:1:amount" in check.reason_codes


# --------------------------------------------------------------------------
# Date consistency
# --------------------------------------------------------------------------


def test_normalized_date_or_none_supports_date_datetime_and_iso_string():
    assert normalized_date_or_none(date(2024, 1, 1)) == date(2024, 1, 1)
    assert normalized_date_or_none(datetime(2024, 1, 1, 12, 0, tzinfo=timezone.utc)) == date(2024, 1, 1)
    assert normalized_date_or_none("2024-01-01") == date(2024, 1, 1)
    assert normalized_date_or_none("not-a-date") is None
    assert normalized_date_or_none(None) is None


def test_date_consistency_not_applicable_when_both_dates_absent():
    config = _config()
    record = _record(())

    check = validate_date_consistency(_validation_input(record), config=config)

    assert check.status == ValidationCheckStatus.NOT_APPLICABLE


def test_date_consistency_not_applicable_when_due_date_absent():
    config = _config()
    record = _record((_field(InvoiceFieldName.INVOICE_DATE, date(2024, 1, 1)),))

    check = validate_date_consistency(_validation_input(record), config=config)

    assert check.status == ValidationCheckStatus.NOT_APPLICABLE


def test_date_consistency_passes_when_due_date_after_invoice_date():
    config = _config()
    record = _record(
        (
            _field(InvoiceFieldName.INVOICE_DATE, date(2024, 1, 1)),
            _field(InvoiceFieldName.DUE_DATE, date(2024, 2, 1)),
        )
    )

    check = validate_date_consistency(_validation_input(record), config=config)

    assert check.status == ValidationCheckStatus.PASSED


def test_date_consistency_fails_when_due_date_before_invoice_date():
    config = _config()
    record = _record(
        (
            _field(InvoiceFieldName.INVOICE_DATE, date(2000, 4, 12)),
            _field(InvoiceFieldName.DUE_DATE, date(1998, 3, 15)),
        )
    )

    check = validate_date_consistency(_validation_input(record), config=config)

    assert check.status == ValidationCheckStatus.FAILED
    assert "DUE_DATE_BEFORE_INVOICE_DATE" in check.reason_codes


# --------------------------------------------------------------------------
# Currency consistency
# --------------------------------------------------------------------------


def test_currency_consistency_passes_with_no_conflicting_evidence():
    config = _config()
    record = _record(
        (
            _field(InvoiceFieldName.CURRENCY, "USD"),
            _field(InvoiceFieldName.TOTAL_AMOUNT, Decimal("69.22"), raw_value="USD 69.22"),
        )
    )

    check = validate_currency_consistency(_validation_input(record), config=config)

    assert check.status == ValidationCheckStatus.PASSED
    assert extract_currency_signals(record) == {"USD"}


def test_currency_consistency_requires_review_when_currency_missing():
    config = _config()
    record = _record(())

    check = validate_currency_consistency(_validation_input(record), config=config)

    assert check.status == ValidationCheckStatus.REVIEW_REQUIRED
    assert "CURRENCY_MISSING" in check.reason_codes


def test_currency_consistency_requires_review_for_unsupported_currency():
    config = _config()
    record = _record((_field(InvoiceFieldName.CURRENCY, "INR"),))

    check = validate_currency_consistency(_validation_input(record), config=config)

    assert check.status == ValidationCheckStatus.REVIEW_REQUIRED
    assert "UNSUPPORTED_CURRENCY" in check.reason_codes


def test_currency_consistency_fails_on_conflicting_evidence():
    config = _config()
    record = _record(
        (
            _field(InvoiceFieldName.CURRENCY, "EUR"),
            _field(InvoiceFieldName.TOTAL_AMOUNT, Decimal("873.58"), raw_value="$873.58"),
        )
    )

    check = validate_currency_consistency(_validation_input(record), config=config)

    assert check.status == ValidationCheckStatus.FAILED
    assert "CONFLICTING_CURRENCY_EVIDENCE" in check.reason_codes


# --------------------------------------------------------------------------
# Line-item arithmetic
# --------------------------------------------------------------------------


def test_line_item_arithmetic_not_applicable_without_line_items():
    config = _config()
    record = _record(())

    checks = validate_line_item_arithmetic(_validation_input(record), config=config)

    assert len(checks) == 1
    assert checks[0].status == ValidationCheckStatus.NOT_APPLICABLE


def test_line_item_arithmetic_passes_when_amount_matches():
    config = _config()
    line_item = _line_item(1, quantity=Decimal("10"), unit_price=Decimal("0.65"), amount=Decimal("6.50"))
    record = _record((), (line_item,))

    checks = validate_line_item_arithmetic(_validation_input(record), config=config)

    assert len(checks) == 1
    assert checks[0].status == ValidationCheckStatus.PASSED
    assert checks[0].expected_value == "6.50"


def test_line_item_arithmetic_fails_when_amount_mismatches():
    config = _config()
    line_item = _line_item(1, quantity=Decimal("10"), unit_price=Decimal("0.65"), amount=Decimal("9.99"))
    record = _record((), (line_item,))

    checks = validate_line_item_arithmetic(_validation_input(record), config=config)

    assert checks[0].status == ValidationCheckStatus.FAILED
    assert "LINE_AMOUNT_MISMATCH" in checks[0].reason_codes


def test_line_item_arithmetic_skips_when_amount_missing():
    config = _config()
    line_item = _line_item(1, quantity=Decimal("3.00"), unit_price=Decimal("50.47"), amount=None)
    record = _record((), (line_item,))

    checks = validate_line_item_arithmetic(_validation_input(record), config=config)

    assert checks[0].status == ValidationCheckStatus.SKIPPED
    assert "LINE_ARITHMETIC_PREREQUISITE_MISSING:LINE_AMOUNT" in checks[0].reason_codes


def test_line_item_arithmetic_preserves_line_ordering():
    config = _config()
    line_items = (
        _line_item(1, quantity=Decimal("1"), unit_price=Decimal("1.00"), amount=Decimal("1.00")),
        _line_item(2, quantity=Decimal("2"), unit_price=Decimal("2.00"), amount=Decimal("4.00")),
        _line_item(3, quantity=Decimal("3"), unit_price=Decimal("3.00"), amount=Decimal("9.00")),
    )
    record = _record((), line_items)

    checks = validate_line_item_arithmetic(_validation_input(record), config=config)

    assert [check.message.split()[1] for check in checks] == ["1", "2", "3"]


# --------------------------------------------------------------------------
# Line-items-to-subtotal reconciliation
# --------------------------------------------------------------------------


def test_subtotal_reconciliation_not_applicable_without_line_items():
    config = _config()
    record = _record((_field(InvoiceFieldName.SUBTOTAL, Decimal("10.00")),))

    check = validate_line_items_to_subtotal(_validation_input(record), config=config)

    assert check.status == ValidationCheckStatus.NOT_APPLICABLE


def test_subtotal_reconciliation_skips_when_subtotal_missing():
    config = _config()
    line_item = _line_item(1, quantity=Decimal("1"), unit_price=Decimal("1"), amount=Decimal("1.00"))
    record = _record((), (line_item,))

    check = validate_line_items_to_subtotal(_validation_input(record), config=config)

    assert check.status == ValidationCheckStatus.SKIPPED
    assert "SUBTOTAL_MISSING" in check.reason_codes


def test_subtotal_reconciliation_skips_when_a_line_amount_is_missing():
    config = _config()
    line_items = (
        _line_item(1, quantity=Decimal("1"), unit_price=Decimal("1"), amount=Decimal("1.00")),
        _line_item(2, quantity=Decimal("1"), unit_price=Decimal("1"), amount=None),
    )
    record = _record((_field(InvoiceFieldName.SUBTOTAL, Decimal("1.00")),), line_items)

    check = validate_line_items_to_subtotal(_validation_input(record), config=config)

    assert check.status == ValidationCheckStatus.SKIPPED
    assert "LINE_AMOUNT_MISSING:2" in check.reason_codes


def test_subtotal_reconciliation_passes_when_sum_matches():
    config = _config()
    line_items = (
        _line_item(1, quantity=Decimal("1"), unit_price=Decimal("1"), amount=Decimal("6.50")),
        _line_item(2, quantity=Decimal("1"), unit_price=Decimal("1"), amount=Decimal("3.40")),
    )
    record = _record((_field(InvoiceFieldName.SUBTOTAL, Decimal("9.90")),), line_items)

    check = validate_line_items_to_subtotal(_validation_input(record), config=config)

    assert check.status == ValidationCheckStatus.PASSED


def test_subtotal_reconciliation_fails_when_sum_mismatches():
    config = _config()
    line_items = (_line_item(1, quantity=Decimal("1"), unit_price=Decimal("1"), amount=Decimal("6.50")),)
    record = _record((_field(InvoiceFieldName.SUBTOTAL, Decimal("100.00")),), line_items)

    check = validate_line_items_to_subtotal(_validation_input(record), config=config)

    assert check.status == ValidationCheckStatus.FAILED
    assert "LINE_ITEMS_SUBTOTAL_MISMATCH" in check.reason_codes


# --------------------------------------------------------------------------
# Invoice-total reconciliation
# --------------------------------------------------------------------------


def test_invoice_total_skipped_when_subtotal_missing():
    config = _config()
    record = _record((_field(InvoiceFieldName.TOTAL_AMOUNT, Decimal("10.00")),))

    check = validate_invoice_total(_validation_input(record), config=config)

    assert check.status == ValidationCheckStatus.SKIPPED
    assert "SUBTOTAL_REQUIRED_FOR_TOTAL_CHECK" in check.reason_codes


def test_invoice_total_passes_with_tax_only():
    config = _config()
    record = _record(
        (
            _field(InvoiceFieldName.SUBTOTAL, Decimal("63.45")),
            _field(InvoiceFieldName.TAX_AMOUNT, Decimal("5.77")),
            _field(InvoiceFieldName.TOTAL_AMOUNT, Decimal("69.22")),
        )
    )

    check = validate_invoice_total(_validation_input(record), config=config)

    assert check.status == ValidationCheckStatus.PASSED
    assert check.expected_value == "69.22"
    assert check.observed_value == "69.22"


def test_invoice_total_applies_discount_as_absolute_value():
    config = _config()
    record = _record(
        (
            _field(InvoiceFieldName.SUBTOTAL, Decimal("858.86")),
            _field(InvoiceFieldName.TAX_AMOUNT, Decimal("36.45")),
            _field(InvoiceFieldName.DISCOUNT_AMOUNT, Decimal("-12.54")),
            _field(InvoiceFieldName.TOTAL_AMOUNT, Decimal("873.58")),
        )
    )

    check = validate_invoice_total(_validation_input(record), config=config)

    assert check.status == ValidationCheckStatus.FAILED
    assert check.expected_value == "882.77"
    assert check.observed_value == "873.58"
    assert check.difference == Decimal("9.19")
    assert "INVOICE_TOTAL_MISMATCH" in check.reason_codes


def test_invoice_total_applies_shipping_and_positive_discount():
    config = _config()
    record = _record(
        (
            _field(InvoiceFieldName.SUBTOTAL, Decimal("48.71")),
            _field(InvoiceFieldName.DISCOUNT_AMOUNT, Decimal("9.74")),
            _field(InvoiceFieldName.SHIPPING_AMOUNT, Decimal("11.13")),
            _field(InvoiceFieldName.TOTAL_AMOUNT, Decimal("50.10")),
        )
    )

    check = validate_invoice_total(_validation_input(record), config=config)

    assert check.status == ValidationCheckStatus.PASSED
    assert check.expected_value == "50.10"


def test_invoice_total_review_required_when_total_missing_and_never_inferred():
    config = _config()
    record = _record(
        (
            _field(InvoiceFieldName.SUBTOTAL, Decimal("63.45")),
            _field(InvoiceFieldName.TAX_AMOUNT, Decimal("5.77")),
        )
    )

    check = validate_invoice_total(_validation_input(record), config=config)

    assert check.status == ValidationCheckStatus.REVIEW_REQUIRED
    assert check.expected_value == "69.22"
    assert check.observed_value is None
    assert "TOTAL_AMOUNT_MISSING" in check.reason_codes
    assert "CALCULATED_TOTAL_NOT_PERSISTED" in check.reason_codes

    # The calculated expectation must never be written back into the record.
    assert record.get_field(InvoiceFieldName.TOTAL_AMOUNT) is None


def test_invoice_total_review_required_for_invalid_optional_adjustment():
    config = _config()
    # A non-None normalized value that Decimal cannot parse: `operand.value`
    # (derived from `normalized_value`) is therefore not None, but
    # `to_decimal_or_none(operand.value)` is -- the exact condition
    # `validate_invoice_total` treats as an invalid (not merely absent)
    # optional adjustment.
    invalid_tax_field = _field(InvoiceFieldName.TAX_AMOUNT, "not-a-decimal", raw_value="not-a-decimal")
    record = NormalizedInvoiceRecord(
        invoice_record_id=uuid4(),
        batch_id=uuid4(),
        document_id=uuid4(),
        source_name="invoice.pdf",
        source_document_sha256="a" * 64,
        fields=(
            _field(InvoiceFieldName.SUBTOTAL, Decimal("10.00")),
            invalid_tax_field,
            _field(InvoiceFieldName.TOTAL_AMOUNT, Decimal("10.00")),
        ),
        line_items=(),
        normalization_version="normalization-v1",
        created_at=datetime.now(timezone.utc),
    )

    check = validate_invoice_total(_validation_input(record), config=config)

    assert check.status == ValidationCheckStatus.REVIEW_REQUIRED
    assert "INVALID_TOTAL_ADJUSTMENT:TAX_AMOUNT" in check.reason_codes


def test_invoice_total_absent_optional_operands_default_to_zero():
    config = _config()
    record = _record(
        (
            _field(InvoiceFieldName.SUBTOTAL, Decimal("100.00")),
            _field(InvoiceFieldName.TOTAL_AMOUNT, Decimal("100.00")),
        )
    )

    check = validate_invoice_total(_validation_input(record), config=config)

    assert check.status == ValidationCheckStatus.PASSED
    assert check.expected_value == "100.00"


# --------------------------------------------------------------------------
# Check ordering
# --------------------------------------------------------------------------


def test_header_checks_run_in_fixed_order():
    config = _config()
    record = _record((_field(InvoiceFieldName.CURRENCY, "USD"), _field(InvoiceFieldName.TOTAL_AMOUNT, Decimal("1.00"))))

    checks = run_header_validation_checks(_validation_input(record), config=config)

    assert [check.check_type for check in checks] == [
        ValidationCheckType.INHERITED_REVIEW,
        ValidationCheckType.REQUIRED_FINANCIAL_FIELDS,
        ValidationCheckType.MONETARY_VALUE_VALIDITY,
        ValidationCheckType.DATE_CONSISTENCY,
        ValidationCheckType.CURRENCY_CONSISTENCY,
    ]


def test_arithmetic_checks_run_line_items_then_subtotal_then_total():
    config = _config()
    line_items = (
        _line_item(1, quantity=Decimal("1"), unit_price=Decimal("1"), amount=Decimal("1.00")),
        _line_item(2, quantity=Decimal("1"), unit_price=Decimal("1"), amount=Decimal("1.00")),
    )
    record = _record(
        (_field(InvoiceFieldName.SUBTOTAL, Decimal("2.00")), _field(InvoiceFieldName.TOTAL_AMOUNT, Decimal("2.00"))),
        line_items,
    )

    checks = run_arithmetic_validation_checks(_validation_input(record), config=config)

    assert [check.check_type for check in checks] == [
        ValidationCheckType.LINE_ITEM_ARITHMETIC,
        ValidationCheckType.LINE_ITEM_ARITHMETIC,
        ValidationCheckType.LINE_ITEMS_TO_SUBTOTAL,
        ValidationCheckType.INVOICE_TOTAL_RECONCILIATION,
    ]

    check_ids = [check.check_id for check in checks]
    assert len(check_ids) == len(set(check_ids))
