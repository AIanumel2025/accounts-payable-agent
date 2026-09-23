"""Unit tests for Phase 5 (financial validation) artifact persistence in
`ap_agent.tools.financial_validation` (M6): checks-JSONL persistence,
result/event/manifest persistence, idempotent reruns, different-byte
collisions, and cross-document evidence isolation of persisted artifacts.
Synthetic contracts only (task §18) -- no invoice fixtures.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest

from ap_agent.config.settings import FinancialValidationConfig
from ap_agent.exceptions import FinancialValidationIntegrityError
from ap_agent.models.normalization import (
    ExtractionMethod,
    InvoiceFieldName,
    NormalizedInvoiceField,
    NormalizedInvoiceRecord,
    NormalizedValueType,
)
from ap_agent.models.validation import ValidationInput
from ap_agent.tools.financial_validation import (
    persist_financial_validation_result,
    phase_5_artifact_directory,
    process_financial_validation,
    validate_persisted_phase_5_artifacts,
)

pytestmark = pytest.mark.unit


def _config(tmp_path: Path, **overrides) -> FinancialValidationConfig:
    return FinancialValidationConfig(artifact_root=tmp_path, **overrides)


def _field(field_name, normalized_value) -> NormalizedInvoiceField:
    return NormalizedInvoiceField(
        field_id=uuid4(),
        field_name=field_name,
        raw_value=str(normalized_value),
        normalized_value=normalized_value,
        value_type=NormalizedValueType.TEXT,
        confidence=95.0,
        extraction_method=ExtractionMethod.LABEL_VALUE,
        evidence_references=(),
    )


def _record(**overrides) -> NormalizedInvoiceRecord:
    defaults = dict(
        invoice_record_id=uuid4(),
        batch_id=uuid4(),
        document_id=uuid4(),
        source_name="invoice.pdf",
        source_document_sha256="a" * 64,
        fields=(
            _field(InvoiceFieldName.CURRENCY, "USD"),
            _field(InvoiceFieldName.SUBTOTAL, Decimal("63.45")),
            _field(InvoiceFieldName.TAX_AMOUNT, Decimal("5.77")),
            _field(InvoiceFieldName.TOTAL_AMOUNT, Decimal("69.22")),
        ),
        line_items=(),
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


def test_persist_writes_all_four_artifacts_and_they_validate(tmp_path):
    config = _config(tmp_path)
    record = _record()
    result = process_financial_validation(_validation_input(record), config=config)

    artifact_paths = persist_financial_validation_result(result, config=config)

    assert set(artifact_paths) == {"directory", "result", "checks", "event", "manifest"}
    for key in ("result", "checks", "event", "manifest"):
        assert artifact_paths[key].is_file()

    validate_persisted_phase_5_artifacts(result, artifact_paths)


def test_persisted_checks_jsonl_has_one_line_per_check(tmp_path):
    config = _config(tmp_path)
    record = _record()
    result = process_financial_validation(_validation_input(record), config=config)

    artifact_paths = persist_financial_validation_result(result, config=config)

    lines = [line for line in artifact_paths["checks"].read_text(encoding="utf-8").splitlines() if line.strip()]
    assert len(lines) == len(result.checks)

    parsed = [json.loads(line) for line in lines]
    assert {entry["check_id"] for entry in parsed} == {str(check.check_id) for check in result.checks}


def test_persisted_result_and_event_agree_with_in_memory_result(tmp_path):
    config = _config(tmp_path)
    record = _record()
    result = process_financial_validation(_validation_input(record), config=config)

    artifact_paths = persist_financial_validation_result(result, config=config)

    persisted_result = json.loads(artifact_paths["result"].read_text(encoding="utf-8"))
    persisted_event = json.loads(artifact_paths["event"].read_text(encoding="utf-8"))
    persisted_manifest = json.loads(artifact_paths["manifest"].read_text(encoding="utf-8"))

    assert persisted_result["document_id"] == str(result.document_id)
    assert persisted_result["status"] == result.status.value
    assert persisted_event["status"] == result.status.value
    assert persisted_manifest["check_count"] == len(result.checks)
    assert persisted_manifest["source_document_sha256"] == result.source_document_sha256


def test_persist_is_idempotent_on_identical_rerun(tmp_path):
    config = _config(tmp_path)
    record = _record()
    result = process_financial_validation(_validation_input(record), config=config)

    first_paths = persist_financial_validation_result(result, config=config)
    hashes_before = {
        name: path.read_bytes() for name, path in first_paths.items() if name != "directory"
    }

    # Reprocess (a new event.occurred_at, identical business content) and
    # persist again: this must be a silent no-op, not a rewrite.
    reprocessed_result = process_financial_validation(_validation_input(record), config=config)
    second_paths = persist_financial_validation_result(reprocessed_result, config=config)

    hashes_after = {
        name: path.read_bytes() for name, path in second_paths.items() if name != "directory"
    }

    assert hashes_before == hashes_after
    validate_persisted_phase_5_artifacts(result, second_paths)


def test_persist_fails_closed_on_a_genuine_collision(tmp_path):
    config = _config(tmp_path)
    record = _record()
    result = process_financial_validation(_validation_input(record), config=config)
    persist_financial_validation_result(result, config=config)

    # A different record hashing to the same artifact directory (forced by
    # constructing a second result that shares batch/document/version but
    # has genuinely different business content) must fail closed.
    other_record = _record(
        invoice_record_id=record.invoice_record_id,
        batch_id=record.batch_id,
        document_id=record.document_id,
        fields=(
            _field(InvoiceFieldName.CURRENCY, "USD"),
            _field(InvoiceFieldName.SUBTOTAL, Decimal("999.00")),
            _field(InvoiceFieldName.TOTAL_AMOUNT, Decimal("999.00")),
        ),
    )
    other_result = process_financial_validation(_validation_input(other_record), config=config)

    with pytest.raises(FinancialValidationIntegrityError) as excinfo:
        persist_financial_validation_result(other_result, config=config)

    assert excinfo.value.reason == "PHASE_5_ARTIFACT_COLLISION"


def test_persist_rejects_a_validation_version_mismatch(tmp_path):
    config = _config(tmp_path)
    record = _record()
    result = process_financial_validation(_validation_input(record), config=config)

    other_config = _config(tmp_path, validation_version="financial-validation-v2")

    with pytest.raises(ValueError, match="VALIDATION_VERSION_MISMATCH"):
        persist_financial_validation_result(result, config=other_config)


def test_cross_document_artifacts_stay_isolated(tmp_path):
    config = _config(tmp_path)

    first_record = _record()
    second_record = _record()

    first_result = process_financial_validation(_validation_input(first_record), config=config)
    second_result = process_financial_validation(_validation_input(second_record), config=config)

    first_paths = persist_financial_validation_result(first_result, config=config)
    second_paths = persist_financial_validation_result(second_result, config=config)

    assert first_paths["directory"] != second_paths["directory"]
    assert str(second_result.document_id) not in str(first_paths["directory"])
    assert str(first_result.document_id) not in str(second_paths["directory"])

    all_check_ids = [check.check_id for check in (*first_result.checks, *second_result.checks)]
    assert len(all_check_ids) == len(set(all_check_ids))


def test_phase_5_artifact_directory_is_document_isolated_and_versioned(tmp_path):
    config = _config(tmp_path)
    record = _record()
    result = process_financial_validation(_validation_input(record), config=config)

    directory = phase_5_artifact_directory(result, config=config)

    assert directory == tmp_path / str(result.batch_id) / str(result.document_id) / "financial-validation-v1"
