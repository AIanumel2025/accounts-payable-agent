"""Unit tests for the Phase 4 -> Phase 5 typed bridge
(`build_validation_input`) and the Phase 5 orchestration functions in
`ap_agent.tools.financial_validation` (M6): artifact-integrity
verification, inherited review, reason-code deduplication, status routing,
result/event synchronization, and the distinction between a processing
(execution/integrity) failure and an ordinary financial-rule failure.
Synthetic `NormalizedInvoiceRecord`/`NormalizationResult` contracts only
(task §18) -- no invoice fixtures.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest

from ap_agent.artifacts.filesystem import write_json_safe_atomically
from ap_agent.config.settings import FinancialValidationConfig, NormalizationConfig
from ap_agent.exceptions import FinancialValidationIntegrityError
from ap_agent.models.normalization import (
    InvoiceFieldName,
    NormalizationEvent,
    NormalizationResult,
    NormalizationStatus,
    NormalizedInvoiceField,
    NormalizedInvoiceRecord,
    NormalizedValueType,
    ExtractionMethod,
)
from ap_agent.models.validation import ValidationCheckStatus, ValidationStatus
from ap_agent.tools.financial_validation import (
    append_unique_reason,
    build_validation_input,
    collect_validation_review_reasons,
    determine_validation_status,
    process_financial_validation,
    summarize_checks_fail_closed,
    validate_inherited_review,
    validate_phase_5_input_integrity,
)
from ap_agent.tools.financial_validation import build_validation_check
from ap_agent.models.validation import ValidationCheckType, ValidationInput, ValidationSeverity

pytestmark = pytest.mark.unit


# --------------------------------------------------------------------------
# Synthetic contract builders
# --------------------------------------------------------------------------


def _financial_config(**overrides) -> FinancialValidationConfig:
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


def _invoice_record(
    *,
    batch_id=None,
    document_id=None,
    sha256="a" * 64,
    normalization_version="normalization-v1",
    fields=(),
    review_required=False,
    review_reasons=(),
) -> NormalizedInvoiceRecord:
    return NormalizedInvoiceRecord(
        invoice_record_id=uuid4(),
        batch_id=batch_id or uuid4(),
        document_id=document_id or uuid4(),
        source_name="invoice.pdf",
        source_document_sha256=sha256,
        fields=fields,
        line_items=(),
        normalization_version=normalization_version,
        created_at=datetime.now(timezone.utc),
        review_required=review_required,
        review_reasons=review_reasons,
    )


def _normalization_result(invoice_record: NormalizedInvoiceRecord) -> NormalizationResult:
    event = NormalizationEvent(
        event_type="NORMALIZATION",
        status=(NormalizationStatus.REVIEW_REQUIRED if invoice_record.review_required else NormalizationStatus.SUCCEEDED),
        batch_id=invoice_record.batch_id,
        document_id=invoice_record.document_id,
        occurred_at=datetime.now(timezone.utc),
        message="ok",
        review_required=invoice_record.review_required,
    )

    return NormalizationResult(
        batch_id=invoice_record.batch_id,
        document_id=invoice_record.document_id,
        source_document_sha256=invoice_record.source_document_sha256,
        ocr_version="phase-3:paddleocr",
        normalization_version=invoice_record.normalization_version,
        status=event.status,
        invoice_record=invoice_record,
        field_candidates=(),
        event=event,
        review_reasons=invoice_record.review_reasons,
        errors=(),
    )


def _persist(result: NormalizationResult, normalization_config: NormalizationConfig) -> Path:
    document_directory = (
        normalization_config.artifact_root
        / str(result.batch_id)
        / str(result.document_id)
        / result.normalization_version
    )
    destination = document_directory / "normalization_result.json"
    write_json_safe_atomically(destination, result)
    return destination


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
# Phase 4 -> Phase 5 bridge
# --------------------------------------------------------------------------


def test_build_validation_input_succeeds_against_a_real_persisted_result(tmp_path):
    normalization_config = NormalizationConfig(artifact_root=tmp_path)
    record = _invoice_record(fields=(_field(InvoiceFieldName.CURRENCY, "USD"),))
    result = _normalization_result(record)
    _persist(result, normalization_config)

    validation_input = build_validation_input(result, normalization_config=normalization_config)

    assert validation_input.invoice_record is record
    assert validation_input.document_id == record.document_id
    assert validation_input.source_document_sha256 == record.source_document_sha256


def test_build_validation_input_rejects_missing_invoice_record(tmp_path):
    normalization_config = NormalizationConfig(artifact_root=tmp_path)
    batch_id, document_id = uuid4(), uuid4()

    event = NormalizationEvent(
        event_type="NORMALIZATION",
        status=NormalizationStatus.FAILED,
        batch_id=batch_id,
        document_id=document_id,
        occurred_at=datetime.now(timezone.utc),
        message="failed",
        review_required=True,
    )

    failed_result = NormalizationResult(
        batch_id=batch_id,
        document_id=document_id,
        source_document_sha256="a" * 64,
        ocr_version="phase-3:paddleocr",
        normalization_version="normalization-v1",
        status=NormalizationStatus.FAILED,
        invoice_record=None,
        field_candidates=(),
        event=event,
        review_reasons=("NORMALIZATION_FAILED",),
        errors=("boom",),
    )

    with pytest.raises(FinancialValidationIntegrityError) as excinfo:
        build_validation_input(failed_result, normalization_config=normalization_config)

    assert excinfo.value.reason == "NORMALIZATION_RECORD_MISSING"


def test_build_validation_input_rejects_a_stale_unpersisted_result(tmp_path):
    normalization_config = NormalizationConfig(artifact_root=tmp_path)
    record = _invoice_record()
    result = _normalization_result(record)
    # Never persisted.

    with pytest.raises(FinancialValidationIntegrityError) as excinfo:
        build_validation_input(result, normalization_config=normalization_config)

    assert excinfo.value.reason == "NORMALIZATION_ARTIFACT_MISSING"


def test_build_validation_input_rejects_a_tampered_artifact(tmp_path):
    normalization_config = NormalizationConfig(artifact_root=tmp_path)
    record = _invoice_record()
    result = _normalization_result(record)
    destination = _persist(result, normalization_config)

    # Tamper with the persisted artifact after the fact.
    destination.write_text(destination.read_text(encoding="utf-8").replace("normalization-v1", "normalization-v9"))

    with pytest.raises(FinancialValidationIntegrityError) as excinfo:
        build_validation_input(result, normalization_config=normalization_config)

    assert excinfo.value.reason == "NORMALIZATION_ARTIFACT_HASH_MISMATCH"


def test_build_validation_input_rejects_cross_document_invoice_record(tmp_path):
    normalization_config = NormalizationConfig(artifact_root=tmp_path)
    record = _invoice_record()
    result = _normalization_result(record)
    _persist(result, normalization_config)

    other_record = _invoice_record(batch_id=result.batch_id, document_id=uuid4())
    cross_document_result = NormalizationResult(
        batch_id=result.batch_id,
        document_id=result.document_id,
        source_document_sha256=result.source_document_sha256,
        ocr_version=result.ocr_version,
        normalization_version=result.normalization_version,
        status=result.status,
        invoice_record=other_record,
        field_candidates=(),
        event=result.event,
        review_reasons=(),
        errors=(),
    )

    with pytest.raises(FinancialValidationIntegrityError) as excinfo:
        build_validation_input(cross_document_result, normalization_config=normalization_config)

    assert excinfo.value.reason == "DOCUMENT_IDENTITY_MISMATCH"


def test_build_validation_input_rejects_sha256_mismatch(tmp_path):
    normalization_config = NormalizationConfig(artifact_root=tmp_path)
    record = _invoice_record()
    result = _normalization_result(record)
    _persist(result, normalization_config)

    mismatched_record = NormalizedInvoiceRecord(
        invoice_record_id=record.invoice_record_id,
        batch_id=record.batch_id,
        document_id=record.document_id,
        source_name=record.source_name,
        source_document_sha256="f" * 64,
        fields=(),
        line_items=(),
        normalization_version=record.normalization_version,
        created_at=record.created_at,
    )
    mismatched_result = NormalizationResult(
        batch_id=result.batch_id,
        document_id=result.document_id,
        source_document_sha256=result.source_document_sha256,
        ocr_version=result.ocr_version,
        normalization_version=result.normalization_version,
        status=result.status,
        invoice_record=mismatched_record,
        field_candidates=(),
        event=result.event,
        review_reasons=(),
        errors=(),
    )

    with pytest.raises(FinancialValidationIntegrityError) as excinfo:
        build_validation_input(mismatched_result, normalization_config=normalization_config)

    assert excinfo.value.reason == "SOURCE_DOCUMENT_SHA256_MISMATCH"


# --------------------------------------------------------------------------
# Inherited review and reason-code deduplication
# --------------------------------------------------------------------------


def test_inherited_review_preserved_when_upstream_review_required():
    config = _financial_config()
    record = _invoice_record(review_required=True, review_reasons=("REQUIRED_FIELD_MISSING:SUPPLIER_NAME",))

    check = validate_inherited_review(_validation_input(record), config=config)

    assert check.status == ValidationCheckStatus.REVIEW_REQUIRED
    assert "INHERITED_REVIEW_REQUIRED" in check.reason_codes
    assert "REQUIRED_FIELD_MISSING:SUPPLIER_NAME" in check.reason_codes


def test_inherited_review_passes_when_no_upstream_review():
    config = _financial_config()
    record = _invoice_record(review_required=False)

    check = validate_inherited_review(_validation_input(record), config=config)

    assert check.status == ValidationCheckStatus.PASSED


def test_append_unique_reason_does_not_skip_falsy_values():
    """The Phase 5 binding is distinct from the Phase 4 binding (D-9): it
    does not skip a falsy (empty-string) reason."""

    reasons: list[str] = []
    append_unique_reason(reasons, "")
    append_unique_reason(reasons, "")
    append_unique_reason(reasons, "A")
    append_unique_reason(reasons, "A")

    assert reasons == ["", "A"]


def test_collect_validation_review_reasons_deduplicates_and_preserves_order():
    config = _financial_config()
    record = _invoice_record(review_required=True, review_reasons=("UPSTREAM_REASON",))
    document_id = record.document_id

    failed_check = build_validation_check(
        document_id=document_id,
        check_type=ValidationCheckType.INVOICE_TOTAL_RECONCILIATION,
        scope_key="invoice",
        status=ValidationCheckStatus.FAILED,
        severity=ValidationSeverity.ERROR,
        message="mismatch",
        reason_codes=("INVOICE_TOTAL_MISMATCH", "INVOICE_TOTAL_MISMATCH"),
        config=config,
    )

    no_reason_check = build_validation_check(
        document_id=document_id,
        check_type=ValidationCheckType.CURRENCY_CONSISTENCY,
        scope_key="invoice",
        status=ValidationCheckStatus.REVIEW_REQUIRED,
        severity=ValidationSeverity.ERROR,
        message="review",
        config=config,
    )

    reasons = collect_validation_review_reasons(record, (failed_check, no_reason_check))

    assert reasons == (
        "UPSTREAM_REASON",
        "INVOICE_TOTAL_MISMATCH",
        "VALIDATION_CHECK_REQUIRES_REVIEW:CURRENCY_CONSISTENCY",
    )


# --------------------------------------------------------------------------
# Summary counts and status routing
# --------------------------------------------------------------------------


def test_summarize_checks_fail_closed_treats_skipped_as_review():
    config = _financial_config()
    document_id = uuid4()

    skipped_check = build_validation_check(
        document_id=document_id,
        check_type=ValidationCheckType.LINE_ITEMS_TO_SUBTOTAL,
        scope_key="invoice",
        status=ValidationCheckStatus.SKIPPED,
        severity=ValidationSeverity.WARNING,
        message="skipped",
        config=config,
    )

    summary = summarize_checks_fail_closed((skipped_check,))

    assert summary.skipped_checks == 1
    assert summary.review_required is True


def test_determine_validation_status_routes_to_review_on_any_failing_check():
    record = _invoice_record(review_required=False)
    config = _financial_config()

    passed_check = build_validation_check(
        document_id=record.document_id,
        check_type=ValidationCheckType.MONETARY_VALUE_VALIDITY,
        scope_key="invoice",
        status=ValidationCheckStatus.PASSED,
        severity=ValidationSeverity.INFORMATION,
        message="ok",
        config=config,
    )

    assert determine_validation_status(record, (passed_check,)) == ValidationStatus.SUCCEEDED

    failed_check = build_validation_check(
        document_id=record.document_id,
        check_type=ValidationCheckType.MONETARY_VALUE_VALIDITY,
        scope_key="invoice",
        status=ValidationCheckStatus.FAILED,
        severity=ValidationSeverity.ERROR,
        message="bad",
        config=config,
    )

    assert determine_validation_status(record, (passed_check, failed_check)) == ValidationStatus.REVIEW_REQUIRED


def test_determine_validation_status_never_downgrades_inherited_review():
    record = _invoice_record(review_required=True, review_reasons=("X",))
    config = _financial_config()

    passed_check = build_validation_check(
        document_id=record.document_id,
        check_type=ValidationCheckType.MONETARY_VALUE_VALIDITY,
        scope_key="invoice",
        status=ValidationCheckStatus.PASSED,
        severity=ValidationSeverity.INFORMATION,
        message="ok",
        config=config,
    )

    assert determine_validation_status(record, (passed_check,)) == ValidationStatus.REVIEW_REQUIRED


# --------------------------------------------------------------------------
# End-to-end process_financial_validation: routing, event sync, failure
# distinction
# --------------------------------------------------------------------------


def test_process_financial_validation_succeeds_for_a_clean_invoice():
    config = _financial_config()
    record = _invoice_record(
        fields=(
            _field(InvoiceFieldName.CURRENCY, "USD"),
            _field(InvoiceFieldName.SUBTOTAL, Decimal("63.45")),
            _field(InvoiceFieldName.TAX_AMOUNT, Decimal("5.77")),
            _field(InvoiceFieldName.TOTAL_AMOUNT, Decimal("69.22")),
        )
    )

    result = process_financial_validation(_validation_input(record), config=config)

    assert result.status == ValidationStatus.SUCCEEDED
    assert result.review_required is False
    assert result.review_reasons == ()
    assert result.errors == ()
    assert result.event.status == result.status
    assert result.event.review_required == result.review_required
    assert result.summary.total_checks == len(result.checks)
    assert len(result.checks) == 8  # 5 header + 1 no-line-items (NOT_APPLICABLE) + subtotal + total


def test_process_financial_validation_routes_a_financial_rule_failure_to_review_not_processing_failure():
    """A mathematically inconsistent total produces a FAILED *check*, but
    the FinancialValidationResult itself is REVIEW_REQUIRED, not FAILED:
    financial-rule failure and processing failure are distinct (task §6)."""

    config = _financial_config()
    record = _invoice_record(
        fields=(
            _field(InvoiceFieldName.CURRENCY, "EUR"),
            _field(InvoiceFieldName.SUBTOTAL, Decimal("858.86")),
            _field(InvoiceFieldName.TAX_AMOUNT, Decimal("36.45")),
            _field(InvoiceFieldName.DISCOUNT_AMOUNT, Decimal("-12.54")),
            _field(InvoiceFieldName.TOTAL_AMOUNT, Decimal("873.58")),
        )
    )

    result = process_financial_validation(_validation_input(record), config=config)

    assert result.status == ValidationStatus.REVIEW_REQUIRED
    assert result.review_required is True
    assert "INVOICE_TOTAL_MISMATCH" in result.review_reasons
    assert result.errors == ()  # not a processing failure


def test_process_financial_validation_fails_closed_on_integrity_error_distinctly_from_financial_rules():
    """An execution/integrity error (bad SHA-256) produces status FAILED
    with populated `errors`, unlike an ordinary financial-rule failure."""

    config = _financial_config()
    record = _invoice_record()
    validation_input = _validation_input(record)

    tampered_input = ValidationInput(
        batch_id=validation_input.batch_id,
        document_id=validation_input.document_id,
        source_name=validation_input.source_name,
        source_document_sha256="0" * 64,
        normalization_version=validation_input.normalization_version,
        invoice_record=record,
    )

    result = process_financial_validation(tampered_input, config=config)

    assert result.status == ValidationStatus.FAILED
    assert result.review_required is True
    assert result.checks == ()
    assert result.summary.review_required is True
    assert "PHASE_5_EXECUTION_FAILURE" in result.review_reasons
    assert any("PHASE_5_SOURCE_SHA256_MISMATCH" in error for error in result.errors)
    assert result.event.status == ValidationStatus.FAILED
    assert result.event.review_required is True


def test_validate_phase_5_input_integrity_raises_on_every_mismatch():
    record = _invoice_record()
    validation_input = _validation_input(record)

    validate_phase_5_input_integrity(validation_input)  # no error

    with pytest.raises(ValueError, match="PHASE_5_BATCH_ID_MISMATCH"):
        validate_phase_5_input_integrity(
            ValidationInput(
                batch_id=uuid4(),
                document_id=validation_input.document_id,
                source_name=validation_input.source_name,
                source_document_sha256=validation_input.source_document_sha256,
                normalization_version=validation_input.normalization_version,
                invoice_record=record,
            )
        )
