"""M2 contract tests for Phase 5 (financial validation) models.

Contracts only. `create_validation_id` and the validation-check functions
(cells 69-73) are out of scope until the processing-extraction milestone.
"""

import dataclasses
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest

from ap_agent.config.settings import FinancialValidationConfig
from ap_agent.models.normalization import InvoiceFieldName, NormalizedInvoiceRecord
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


def test_validation_status_and_check_status_members():
    assert [member.value for member in ValidationStatus] == [
        "SUCCEEDED",
        "REVIEW_REQUIRED",
        "FAILED",
    ]
    assert [member.value for member in ValidationCheckStatus] == [
        "PASSED",
        "FAILED",
        "REVIEW_REQUIRED",
        "NOT_APPLICABLE",
        "SKIPPED",
    ]


def test_validation_check_type_has_eight_members():
    assert len(ValidationCheckType) == 8
    assert (
        ValidationCheckType.INVOICE_TOTAL_RECONCILIATION.value
        == "INVOICE_TOTAL_RECONCILIATION"
    )


def test_validation_severity_members():
    assert {member.value for member in ValidationSeverity} == {
        "INFORMATION",
        "WARNING",
        "ERROR",
        "CRITICAL",
    }


def test_financial_validation_config_defaults():
    config = FinancialValidationConfig(artifact_root=Path("/tmp/ap-agent"))
    assert config.validation_version == "financial-validation-v1"
    assert config.monetary_tolerance == Decimal("0.01")
    assert config.quantity_tolerance == Decimal("0.001")
    assert config.maximum_absolute_amount == Decimal("1000000000.00")
    assert config.required_financial_fields == (
        InvoiceFieldName.CURRENCY,
        InvoiceFieldName.TOTAL_AMOUNT,
    )
    assert config.fail_closed_on_missing_total is True


def test_financial_validation_config_instances_are_independent():
    first = FinancialValidationConfig(
        artifact_root=Path("/tmp/a"), monetary_tolerance=Decimal("0.05")
    )
    second = FinancialValidationConfig(artifact_root=Path("/tmp/b"))
    assert first.monetary_tolerance == Decimal("0.05")
    assert second.monetary_tolerance == Decimal("0.01")

    with pytest.raises(dataclasses.FrozenInstanceError):
        second.monetary_tolerance = Decimal("1.00")


def test_validation_operand_defaults_to_empty_evidence_tuple():
    operand = ValidationOperand(name="invoice_total", value="69.22")
    assert operand.field_id is None
    assert operand.line_item_id is None
    assert operand.evidence_reference_ids == ()


def test_validation_check_result_defaults():
    check = ValidationCheckResult(
        check_id=uuid4(),
        check_type=ValidationCheckType.INVOICE_TOTAL_RECONCILIATION,
        status=ValidationCheckStatus.FAILED,
        severity=ValidationSeverity.ERROR,
        message="totals do not match",
    )
    assert check.operands == ()
    assert check.expected_value is None
    assert check.observed_value is None
    assert check.difference is None
    assert check.review_required is False
    assert check.reason_codes == ()


def test_validation_input_wraps_normalized_invoice_record():
    record = NormalizedInvoiceRecord(
        invoice_record_id=uuid4(),
        batch_id=uuid4(),
        document_id=uuid4(),
        source_name="invoice.pdf",
        source_document_sha256="a" * 64,
        fields=(),
        line_items=(),
        normalization_version="normalization-v1",
        created_at=datetime.now(timezone.utc),
    )
    validation_input = ValidationInput(
        batch_id=record.batch_id,
        document_id=record.document_id,
        source_name=record.source_name,
        source_document_sha256=record.source_document_sha256,
        normalization_version=record.normalization_version,
        invoice_record=record,
    )
    assert validation_input.invoice_record is record

    with pytest.raises(dataclasses.FrozenInstanceError):
        validation_input.source_name = "mutated.pdf"


def test_financial_validation_result_assembles_the_full_contract():
    batch_id = uuid4()
    document_id = uuid4()
    check = ValidationCheckResult(
        check_id=uuid4(),
        check_type=ValidationCheckType.INVOICE_TOTAL_RECONCILIATION,
        status=ValidationCheckStatus.PASSED,
        severity=ValidationSeverity.INFORMATION,
        message="totals match",
    )
    summary = FinancialValidationSummary(
        total_checks=1,
        passed_checks=1,
        failed_checks=0,
        review_required_checks=0,
        not_applicable_checks=0,
        skipped_checks=0,
        review_required=False,
    )
    event = ValidationEvent(
        event_type="phase5.validation",
        status=ValidationStatus.SUCCEEDED,
        batch_id=batch_id,
        document_id=document_id,
        occurred_at=datetime.now(timezone.utc),
        message="ok",
        review_required=False,
    )
    result = FinancialValidationResult(
        batch_id=batch_id,
        document_id=document_id,
        source_name="invoice.pdf",
        source_document_sha256="b" * 64,
        invoice_record_id=uuid4(),
        normalization_version="normalization-v1",
        validation_version="financial-validation-v1",
        status=ValidationStatus.SUCCEEDED,
        checks=(check,),
        summary=summary,
        event=event,
    )
    assert result.review_required is False
    assert result.review_reasons == ()
    assert result.errors == ()
