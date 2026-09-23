"""Unit tests for `ap_agent.tools.normalization.normalize_invoice_document`
and its supporting orchestration helpers (M5): review-reason uniqueness,
upstream review propagation, missing-required-field routing, line-item
grouping/ordering/limits, missing-total non-inference, deterministic IDs
and the explicit configuration dependency. Synthetic OCR contracts only
(task §14/§16) — no invoice fixtures.
"""

from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest

from ap_agent.config.settings import NormalizationConfig
from ap_agent.models.normalization import InvoiceFieldName, NormalizationInput, NormalizationStatus
from ap_agent.models.ocr import (
    BoundingBox,
    EvidenceLine,
    OCRDocumentResult,
    OCREvent,
    OCRPageResult,
    OCRStatus,
    OCRToken,
)
from ap_agent.tools.normalization import append_unique_reason, normalize_invoice_document

pytestmark = pytest.mark.unit


# --- synthetic OCR contract builders (mirrors test_normalization_tools.py) -


def _box(x, y, w, h):
    return BoundingBox(x=x, y=y, width=w, height=h)


def _token(text, x, y, w, h, order, page=1):
    return OCRToken(
        evidence_id=uuid4(),
        page_number=page,
        reading_order=order,
        text=text,
        confidence=95.0,
        bounding_box=_box(x, y, w, h),
        block_number=1,
        paragraph_number=1,
        line_number=1,
        word_number=order,
    )


def _line(text, x, y, w, h, order, tokens=(), page=1):
    return EvidenceLine(
        evidence_line_id=uuid4(),
        page_number=page,
        reading_order=order,
        text=text,
        mean_confidence=95.0,
        bounding_box=_box(x, y, w, h),
        token_evidence_ids=tuple(t.evidence_id for t in tokens),
    )


def _page(lines, tokens, page_number=1, status=OCRStatus.SUCCEEDED, review_reasons=()):
    return OCRPageResult(
        page_number=page_number,
        processed_image_path=Path("processed.png"),
        processed_image_sha256="a" * 64,
        evidence_image_path=Path("evidence.png"),
        evidence_image_sha256="b" * 64,
        page_text=" ".join(line.text for line in lines),
        tokens=tuple(tokens),
        evidence_lines=tuple(lines),
        mean_confidence=95.0,
        low_confidence_token_count=0,
        low_confidence_ratio=0.0,
        status=status,
        review_reasons=review_reasons,
        ocr_engine="paddleocr",
        ocr_engine_version="3.7.0",
        ocr_configuration="paddleocr",
        processed_at=datetime.now(timezone.utc),
    )


def _ocr_result(pages, status=OCRStatus.SUCCEEDED, batch_id=None, document_id=None, sha256="c" * 64):
    batch_id = batch_id or uuid4()
    document_id = document_id or uuid4()

    event = OCREvent(
        event_type="OCR_EXTRACTION",
        status=status,
        batch_id=batch_id,
        document_id=document_id,
        occurred_at=datetime.now(timezone.utc),
        message="ok",
        review_required=status != OCRStatus.SUCCEEDED,
    )

    return OCRDocumentResult(
        batch_id=batch_id,
        document_id=document_id,
        source_document_sha256=sha256,
        preprocessing_version="phase2-v1",
        status=status,
        pages=tuple(pages),
        event=event,
    )


def _normalization_input(ocr_result, source_name="test.pdf"):
    return NormalizationInput(
        batch_id=ocr_result.batch_id,
        document_id=ocr_result.document_id,
        source_name=source_name,
        source_document_sha256=ocr_result.source_document_sha256,
        ocr_version="phase-3:paddleocr",
        ocr_result=ocr_result,
    )


def _table(rows, start_y=100, row_height=20):
    """Build a header row (DESCRIPTION / QUANTITY / UNIT PRICE / AMOUNT)
    plus one row per (description, quantity, unit_price, amount) tuple,
    with columns positioned left to right so `_assign_row_columns`
    resolves them unambiguously."""

    lines = []
    tokens = []
    order = 100

    header_cells = [("DESCRIPTION", 0), ("QUANTITY", 300), ("UNIT PRICE", 450), ("AMOUNT", 600)]
    for text, x in header_cells:
        order += 1
        token = _token(text, x, start_y, 100, 15, order)
        tokens.append(token)
        lines.append(_line(text, x, start_y, 100, 15, order, tokens=[token]))

    y = start_y + row_height

    for description, quantity, unit_price, amount in rows:
        for value, x in (
            (description, 0),
            (quantity, 300),
            (unit_price, 450),
            (amount, 600),
        ):
            if value is None:
                continue

            order += 1
            token = _token(str(value), x, y, 100, 15, order)
            tokens.append(token)
            lines.append(_line(str(value), x, y, 100, 15, order, tokens=[token]))

        y += row_height

    return lines, tokens


@pytest.fixture
def config(tmp_path):
    return NormalizationConfig(artifact_root=tmp_path)


# --- append_unique_reason (Phase 4 binding: skips falsy reasons) -----------


def test_append_unique_reason_skips_duplicates_and_falsy_values():
    reasons = ["A"]
    append_unique_reason(reasons, "A")
    append_unique_reason(reasons, "B")
    append_unique_reason(reasons, "")
    append_unique_reason(reasons, None)

    assert reasons == ["A", "B"]


# --- upstream review propagation --------------------------------------------


def test_review_required_ocr_status_propagates_to_normalization_result(config):
    lines, tokens = _table([("Widget", "1", "10.00", "10.00")])
    page = _page(
        lines,
        tokens,
        status=OCRStatus.REVIEW_REQUIRED,
        review_reasons=("CRITICAL_TOTAL_VALUE_MISSING",),
    )
    ocr_result = _ocr_result([page], status=OCRStatus.REVIEW_REQUIRED)
    normalization_input = _normalization_input(ocr_result)

    result = normalize_invoice_document(normalization_input, config=config)

    assert result.status == NormalizationStatus.REVIEW_REQUIRED
    assert "INHERITED_OCR_STATUS:REVIEW_REQUIRED" in result.review_reasons
    assert "OCR_PAGE_1:CRITICAL_TOTAL_VALUE_MISSING" in result.review_reasons
    # Upstream and Phase 4 reasons stay distinguishable and unique.
    assert len(result.review_reasons) == len(set(result.review_reasons))


def test_succeeded_ocr_with_all_required_fields_does_not_erase_success(config):
    text_lines = {
        "SUPPLIER_NAME": ("SUPPLIER: Acme Ltd", 0, 0),
        "INVOICE_NUMBER": ("INVOICE NUMBER: 1001", 0, 20),
        "INVOICE_DATE": ("INVOICE DATE: 2024-01-15", 0, 40),
        "CURRENCY": ("USD", 0, 60),
    }

    lines = []
    tokens = []
    order = 0
    for text, x, y in text_lines.values():
        order += 1
        token = _token(text, x, y, 250, 15, order)
        tokens.append(token)
        lines.append(_line(text, x, y, 250, 15, order, tokens=[token]))

    total_text = "TOTAL: 10.00"
    order += 1
    total_token = _token(total_text, 0, 80, 150, 15, order)
    tokens.append(total_token)
    lines.append(_line(total_text, 0, 80, 150, 15, order, tokens=[total_token]))

    table_lines, table_tokens = _table([("Widget", "1", "10.00", "10.00")], start_y=200)
    lines.extend(table_lines)
    tokens.extend(table_tokens)

    page = _page(lines, tokens)
    ocr_result = _ocr_result([page])
    normalization_input = _normalization_input(ocr_result)

    result = normalize_invoice_document(normalization_input, config=config)

    assert result.status == NormalizationStatus.SUCCEEDED
    assert result.invoice_record.review_required is False
    assert result.review_reasons == ()


# --- missing required fields -------------------------------------------------


def test_missing_required_fields_are_reported_by_name(config):
    ocr_result = _ocr_result([_page([], [])])
    normalization_input = _normalization_input(ocr_result)

    result = normalize_invoice_document(normalization_input, config=config)

    assert result.status == NormalizationStatus.REVIEW_REQUIRED
    for field in config.required_fields:
        assert f"REQUIRED_FIELD_MISSING:{field.value}" in result.review_reasons


# --- missing-total non-inference --------------------------------------------


def test_missing_total_is_never_inferred_from_subtotal_plus_tax(config):
    subtotal_token = _token("SUBTOTAL: 63.45", 0, 0, 150, 15, 1)
    subtotal_line = _line("SUBTOTAL: 63.45", 0, 0, 150, 15, 1, tokens=[subtotal_token])
    tax_token = _token("TAX: 5.77", 0, 20, 150, 15, 2)
    tax_line = _line("TAX: 5.77", 0, 20, 150, 15, 2, tokens=[tax_token])

    page = _page([subtotal_line, tax_line], [subtotal_token, tax_token])
    ocr_result = _ocr_result([page])
    normalization_input = _normalization_input(ocr_result)

    result = normalize_invoice_document(normalization_input, config=config)

    total_field = result.invoice_record.get_field(InvoiceFieldName.TOTAL_AMOUNT)
    assert total_field is None
    assert "REQUIRED_FIELD_MISSING:TOTAL_AMOUNT" in result.review_reasons
    # 63.45 + 5.77 = 69.22 must never appear as the total.
    assert all(
        field.field_name != InvoiceFieldName.TOTAL_AMOUNT or field.normalized_value != Decimal("69.22")
        for field in result.invoice_record.fields
    )


# --- line-item grouping, ordering, limits and exclusions -------------------


def test_line_items_are_grouped_ordered_and_linked_to_row_evidence(config):
    lines, tokens = _table(
        [
            ("Widget A", "1", "5.00", "5.00"),
            ("Widget B", "2", "3.00", "6.00"),
        ]
    )
    page = _page(lines, tokens)
    ocr_result = _ocr_result([page])
    normalization_input = _normalization_input(ocr_result)

    result = normalize_invoice_document(normalization_input, config=config)

    assert len(result.invoice_record.line_items) == 2
    line_numbers = [item.line_number for item in result.invoice_record.line_items]
    assert line_numbers == sorted(line_numbers)
    assert result.invoice_record.line_items[0].description.normalized_value == "Widget A"
    assert result.invoice_record.line_items[1].description.normalized_value == "Widget B"


def test_total_subtotal_rows_are_excluded_from_line_items(config):
    lines, tokens = _table([("Widget A", "1", "5.00", "5.00")])

    summary_token = _token("SUBTOTAL 5.00", 0, 300, 150, 15, 900)
    summary_line = _line("SUBTOTAL 5.00", 0, 300, 150, 15, 900, tokens=[summary_token])
    lines.append(summary_line)
    tokens.append(summary_token)

    page = _page(lines, tokens)
    ocr_result = _ocr_result([page])
    normalization_input = _normalization_input(ocr_result)

    result = normalize_invoice_document(normalization_input, config=config)

    descriptions = [
        item.description.normalized_value if item.description else None
        for item in result.invoice_record.line_items
    ]
    assert "SUBTOTAL 5.00" not in descriptions
    assert len(result.invoice_record.line_items) == 1


def test_maximum_line_items_is_enforced(tmp_path):
    config = NormalizationConfig(artifact_root=tmp_path, maximum_line_items=2)

    rows = [(f"Item {i}", "1", "1.00", "1.00") for i in range(5)]
    lines, tokens = _table(rows)
    page = _page(lines, tokens)
    ocr_result = _ocr_result([page])
    normalization_input = _normalization_input(ocr_result)

    result = normalize_invoice_document(normalization_input, config=config)

    assert len(result.invoice_record.line_items) <= 2


# --- deterministic IDs and explicit configuration dependency ---------------


def test_deterministic_ids_reproduce_across_identical_reruns(config):
    lines, tokens = _table([("Widget A", "1", "5.00", "5.00")])
    page = _page(lines, tokens)
    ocr_result = _ocr_result([page])
    normalization_input = _normalization_input(ocr_result)

    first = normalize_invoice_document(normalization_input, config=config)
    second = normalize_invoice_document(normalization_input, config=config)

    assert first.invoice_record.invoice_record_id == second.invoice_record.invoice_record_id
    assert [f.field_id for f in first.invoice_record.fields] == [
        f.field_id for f in second.invoice_record.fields
    ]
    assert [li.line_item_id for li in first.invoice_record.line_items] == [
        li.line_item_id for li in second.invoice_record.line_items
    ]
    assert sorted(str(c.candidate_id) for c in first.field_candidates) == sorted(
        str(c.candidate_id) for c in second.field_candidates
    )


def test_timestamps_are_not_part_of_deterministic_ids(config):
    """The ID formula's identity material never includes `created_at` or
    `occurred_at`; two documents that differ only by when they were
    processed still get identical IDs for identical content."""

    lines, tokens = _table([("Widget A", "1", "5.00", "5.00")])
    page = _page(lines, tokens)
    ocr_result = _ocr_result([page])
    normalization_input = _normalization_input(ocr_result)

    first = normalize_invoice_document(normalization_input, config=config)
    import time

    time.sleep(0.01)
    second = normalize_invoice_document(normalization_input, config=config)

    assert first.invoice_record.created_at != second.invoice_record.created_at
    assert first.invoice_record.invoice_record_id == second.invoice_record.invoice_record_id


def test_changing_normalization_version_changes_ids_but_not_extracted_values(tmp_path):
    lines, tokens = _table([("Widget A", "1", "5.00", "5.00")])
    page = _page(lines, tokens)
    ocr_result = _ocr_result([page])
    normalization_input = _normalization_input(ocr_result)

    config_v1 = NormalizationConfig(artifact_root=tmp_path / "v1", normalization_version="normalization-v1")
    config_v2 = NormalizationConfig(artifact_root=tmp_path / "v2", normalization_version="normalization-v2")

    result_v1 = normalize_invoice_document(normalization_input, config=config_v1)
    result_v2 = normalize_invoice_document(normalization_input, config=config_v2)

    assert result_v1.invoice_record.invoice_record_id != result_v2.invoice_record.invoice_record_id
    assert result_v1.normalization_version == "normalization-v1"
    assert result_v2.normalization_version == "normalization-v2"

    v1_description = result_v1.invoice_record.line_items[0].description.normalized_value
    v2_description = result_v2.invoice_record.line_items[0].description.normalized_value
    assert v1_description == v2_description == "Widget A"


def test_explicit_configuration_is_not_a_hidden_module_global(tmp_path):
    """Two independently constructed `NormalizationConfig` instances used
    concurrently must not interfere with one another (no shared mutable
    module state)."""

    lines, tokens = _table([("Widget A", "1", "5.00", "5.00")])
    page = _page(lines, tokens)
    ocr_result = _ocr_result([page])
    normalization_input = _normalization_input(ocr_result)

    config_a = NormalizationConfig(artifact_root=tmp_path / "a", minimum_field_confidence=0.0)
    config_b = NormalizationConfig(artifact_root=tmp_path / "b", minimum_field_confidence=100.0)

    result_a = normalize_invoice_document(normalization_input, config=config_a)
    result_b = normalize_invoice_document(normalization_input, config=config_b)

    description_a = result_a.invoice_record.line_items[0].description
    description_b = result_b.invoice_record.line_items[0].description

    assert description_a.review_required is False
    assert description_b.review_required is True
    assert "LOW_FIELD_CONFIDENCE" in description_b.review_reasons
