"""Batch-generalisation tests for Phase 5 (financial validation), task §20:
more than four invoices, varying line-item counts, multiple currencies,
missing optional/required fields, discounts/shipping/tax, exact tolerance
boundaries, malformed decimals, prohibited negative values, upstream review
states and duplicate-looking values isolated by document ID. Generated
`NormalizedInvoiceRecord` contracts only -- production code under `src/`
must not assume there are exactly four documents (CLAUDE.md, decision D-2).
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest

from ap_agent.config.settings import FinancialValidationConfig
from ap_agent.models.normalization import (
    ExtractionMethod,
    InvoiceFieldName,
    NormalizedInvoiceField,
    NormalizedInvoiceRecord,
    NormalizedLineItem,
    NormalizedValueType,
)
from ap_agent.models.validation import ValidationInput, ValidationStatus
from ap_agent.tools.financial_validation import (
    process_financial_validation,
    validate_invoice_total,
    validate_line_item_arithmetic,
    validate_monetary_values,
    validate_required_financial_fields,
    values_within_tolerance,
)

pytestmark = pytest.mark.unit


def _config(**overrides) -> FinancialValidationConfig:
    return FinancialValidationConfig(artifact_root=Path("/tmp/ap-agent-phase5"), **overrides)


def _field(field_name, normalized_value, raw_value=None) -> NormalizedInvoiceField:
    return NormalizedInvoiceField(
        field_id=uuid4(),
        field_name=field_name,
        raw_value=raw_value if raw_value is not None else str(normalized_value),
        normalized_value=normalized_value,
        value_type=NormalizedValueType.TEXT,
        confidence=95.0,
        extraction_method=ExtractionMethod.LABEL_VALUE,
        evidence_references=(),
    )


def _line_item(line_number, quantity, unit_price, amount) -> NormalizedLineItem:
    def _numeric(field_name, value):
        return _field(field_name, value) if value is not None else None

    return NormalizedLineItem(
        line_item_id=uuid4(),
        line_number=line_number,
        description=None,
        quantity=_numeric(InvoiceFieldName.LINE_QUANTITY, quantity),
        unit_price=_numeric(InvoiceFieldName.LINE_UNIT_PRICE, unit_price),
        amount=_numeric(InvoiceFieldName.LINE_AMOUNT, amount),
        currency=None,
        confidence=95.0,
        evidence_references=(),
    )


def _record(fields=(), line_items=(), **overrides) -> NormalizedInvoiceRecord:
    defaults = dict(
        invoice_record_id=uuid4(),
        batch_id=uuid4(),
        document_id=uuid4(),
        source_name="invoice.pdf",
        source_document_sha256="a" * 64,
        fields=fields,
        line_items=line_items,
        normalization_version="normalization-v1",
        created_at=datetime.now(timezone.utc),
    )
    defaults.update(overrides)
    return NormalizedInvoiceRecord(**defaults)


def _validation_input(record: NormalizedInvoiceRecord) -> ValidationInput:
    return ValidationInput(
        batch_id=record.batch_id,
        document_id=record.document_id,
        source_name=record.source_name,
        source_document_sha256=record.source_document_sha256,
        normalization_version=record.normalization_version,
        invoice_record=record,
    )


# --------------------------------------------------------------------------
# More than four invoices; zero, one and many line items
# --------------------------------------------------------------------------


def test_more_than_four_invoices_validate_independently():
    config = _config()

    records = [
        _record(
            fields=(
                _field(InvoiceFieldName.CURRENCY, "USD"),
                _field(InvoiceFieldName.SUBTOTAL, Decimal(f"{10 + index}.00")),
                _field(InvoiceFieldName.TOTAL_AMOUNT, Decimal(f"{10 + index}.00")),
            )
        )
        for index in range(7)
    ]

    results = [process_financial_validation(_validation_input(record), config=config) for record in records]

    assert len(results) == 7
    assert all(result.status == ValidationStatus.SUCCEEDED for result in results)

    document_ids = [result.document_id for result in results]
    assert len(document_ids) == len(set(document_ids))


@pytest.mark.parametrize("line_item_count", [0, 1, 25])
def test_line_item_counts_of_zero_one_and_many(line_item_count):
    config = _config()

    line_items = tuple(
        _line_item(number, Decimal("1"), Decimal("2.00"), Decimal("2.00"))
        for number in range(1, line_item_count + 1)
    )
    subtotal = Decimal("2.00") * line_item_count if line_item_count else Decimal("0.00")

    record = _record(
        fields=(
            _field(InvoiceFieldName.CURRENCY, "USD"),
            _field(InvoiceFieldName.SUBTOTAL, subtotal),
            _field(InvoiceFieldName.TOTAL_AMOUNT, subtotal),
        ),
        line_items=line_items,
    )

    result = process_financial_validation(_validation_input(record), config=config)

    line_checks = validate_line_item_arithmetic(_validation_input(record), config=config)
    assert len(line_checks) == max(line_item_count, 1)

    if line_item_count == 0:
        assert result.status == ValidationStatus.SUCCEEDED
    else:
        assert all(check.status.value == "PASSED" for check in line_checks)


# --------------------------------------------------------------------------
# Multiple currencies
# --------------------------------------------------------------------------


@pytest.mark.parametrize("currency", ["USD", "GBP", "EUR"])
def test_each_supported_currency_passes_consistency(currency):
    config = _config()
    record = _record(fields=(_field(InvoiceFieldName.CURRENCY, currency),))

    result = process_financial_validation(_validation_input(record), config=config)

    currency_check = next(check for check in result.checks if check.check_type.value == "CURRENCY_CONSISTENCY")
    assert currency_check.status.value == "PASSED"


# --------------------------------------------------------------------------
# Missing optional/required fields
# --------------------------------------------------------------------------


def test_missing_optional_fields_do_not_block_total_reconciliation():
    config = _config()
    record = _record(
        fields=(
            _field(InvoiceFieldName.CURRENCY, "USD"),
            _field(InvoiceFieldName.SUBTOTAL, Decimal("100.00")),
            _field(InvoiceFieldName.TOTAL_AMOUNT, Decimal("100.00")),
        )
    )

    check = validate_invoice_total(_validation_input(record), config=config)

    assert check.status.value == "PASSED"


def test_missing_required_fields_are_flagged_not_invented():
    config = _config()
    record = _record(fields=())

    check = validate_required_financial_fields(_validation_input(record), config=config)

    assert check.status.value == "REVIEW_REQUIRED"
    assert record.get_field(InvoiceFieldName.TOTAL_AMOUNT) is None
    assert record.get_field(InvoiceFieldName.CURRENCY) is None


# --------------------------------------------------------------------------
# Discounts, shipping and taxes together
# --------------------------------------------------------------------------


def test_discount_shipping_and_tax_combine_correctly():
    config = _config()
    record = _record(
        fields=(
            _field(InvoiceFieldName.CURRENCY, "USD"),
            _field(InvoiceFieldName.SUBTOTAL, Decimal("200.00")),
            _field(InvoiceFieldName.DISCOUNT_AMOUNT, Decimal("-20.00")),
            _field(InvoiceFieldName.TAX_AMOUNT, Decimal("15.00")),
            _field(InvoiceFieldName.SHIPPING_AMOUNT, Decimal("5.00")),
            _field(InvoiceFieldName.TOTAL_AMOUNT, Decimal("200.00")),
        )
    )

    check = validate_invoice_total(_validation_input(record), config=config)

    # 200.00 - 20.00 + 15.00 + 5.00 = 200.00
    assert check.status.value == "PASSED"
    assert check.expected_value == "200.00"


# --------------------------------------------------------------------------
# Exact tolerance boundaries
# --------------------------------------------------------------------------


def test_values_exactly_at_and_just_outside_the_tolerance_boundary():
    config = _config()

    assert values_within_tolerance(Decimal("100.00"), Decimal("100.01"), config=config)
    assert not values_within_tolerance(Decimal("100.00"), Decimal("100.02"), config=config)


def test_total_reconciliation_at_tolerance_boundary_passes_just_outside_fails():
    config = _config()

    passing_record = _record(
        fields=(
            _field(InvoiceFieldName.SUBTOTAL, Decimal("100.00")),
            _field(InvoiceFieldName.TOTAL_AMOUNT, Decimal("100.01")),
        )
    )
    failing_record = _record(
        fields=(
            _field(InvoiceFieldName.SUBTOTAL, Decimal("100.00")),
            _field(InvoiceFieldName.TOTAL_AMOUNT, Decimal("100.02")),
        )
    )

    assert validate_invoice_total(_validation_input(passing_record), config=config).status.value == "PASSED"
    assert validate_invoice_total(_validation_input(failing_record), config=config).status.value == "FAILED"


# --------------------------------------------------------------------------
# Malformed decimals and prohibited negative values
# --------------------------------------------------------------------------


def test_malformed_decimal_raw_values_are_flagged_invalid_not_coerced():
    config = _config()
    record = _record(fields=(_field(InvoiceFieldName.SUBTOTAL, "not-a-number", raw_value="not-a-number"),))

    check = validate_monetary_values(_validation_input(record), config=config)

    assert check.status.value == "FAILED"
    assert "INVALID_MONETARY_VALUE:SUBTOTAL" in check.reason_codes


@pytest.mark.parametrize(
    "field_name",
    [InvoiceFieldName.SUBTOTAL, InvoiceFieldName.TAX_AMOUNT, InvoiceFieldName.SHIPPING_AMOUNT, InvoiceFieldName.TOTAL_AMOUNT],
)
def test_negative_values_are_prohibited_outside_discount(field_name):
    config = _config()
    record = _record(fields=(_field(field_name, Decimal("-5.00")),))

    check = validate_monetary_values(_validation_input(record), config=config)

    assert check.status.value == "FAILED"
    assert f"UNEXPECTED_NEGATIVE_VALUE:{field_name.value}" in check.reason_codes


def test_negative_discount_is_permitted():
    config = _config()
    record = _record(fields=(_field(InvoiceFieldName.DISCOUNT_AMOUNT, Decimal("-5.00")),))

    check = validate_monetary_values(_validation_input(record), config=config)

    assert check.status.value == "PASSED"


# --------------------------------------------------------------------------
# Upstream review states
# --------------------------------------------------------------------------


@pytest.mark.parametrize("upstream_review_required", [True, False])
def test_upstream_review_state_is_preserved(upstream_review_required):
    config = _config()
    record = _record(
        fields=(
            _field(InvoiceFieldName.CURRENCY, "USD"),
            _field(InvoiceFieldName.SUBTOTAL, Decimal("10.00")),
            _field(InvoiceFieldName.TOTAL_AMOUNT, Decimal("10.00")),
        ),
        review_required=upstream_review_required,
        review_reasons=("UPSTREAM_REASON",) if upstream_review_required else (),
    )

    result = process_financial_validation(_validation_input(record), config=config)

    if upstream_review_required:
        assert result.status == ValidationStatus.REVIEW_REQUIRED
        assert "UPSTREAM_REASON" in result.review_reasons
    else:
        assert result.status == ValidationStatus.SUCCEEDED


# --------------------------------------------------------------------------
# Duplicate-looking values isolated by document ID
# --------------------------------------------------------------------------


def test_duplicate_looking_invoices_are_isolated_by_document_id():
    config = _config()
    batch_id = uuid4()

    def _duplicate_looking_record():
        return _record(
            batch_id=batch_id,
            fields=(
                _field(InvoiceFieldName.CURRENCY, "USD"),
                _field(InvoiceFieldName.SUBTOTAL, Decimal("42.00")),
                _field(InvoiceFieldName.TOTAL_AMOUNT, Decimal("42.00")),
            ),
        )

    first_record = _duplicate_looking_record()
    second_record = _duplicate_looking_record()

    assert first_record.document_id != second_record.document_id

    first_result = process_financial_validation(_validation_input(first_record), config=config)
    second_result = process_financial_validation(_validation_input(second_record), config=config)

    first_check_ids = {check.check_id for check in first_result.checks}
    second_check_ids = {check.check_id for check in second_result.checks}

    # Same business content, different document IDs -> disjoint check IDs.
    assert first_check_ids.isdisjoint(second_check_ids)
