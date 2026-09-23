"""Batch-generalisation tests (task §16): the Phase 4 implementation must
not be restricted to the four committed invoice fixtures. Uses generated
synthetic OCR contracts to exercise more than four documents, multiple
pages, varying line-item counts, missing optional/required fields,
duplicate-looking values isolated by document ID, multiple currencies,
comma/symbol decimal formats and an ambiguity-threshold overload — none of
it filename- or fixture-specific (CLAUDE.md decisions D-2/D-3).
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
from ap_agent.tools.normalization import normalize_invoice_document

pytestmark = pytest.mark.unit


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


def _page(lines, tokens, page_number=1):
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
        status=OCRStatus.SUCCEEDED,
        review_reasons=(),
        ocr_engine="paddleocr",
        ocr_engine_version="3.7.0",
        ocr_configuration="paddleocr",
        processed_at=datetime.now(timezone.utc),
    )


def _ocr_result(pages, sha256):
    batch_id, document_id = uuid4(), uuid4()

    event = OCREvent(
        event_type="OCR_EXTRACTION",
        status=OCRStatus.SUCCEEDED,
        batch_id=batch_id,
        document_id=document_id,
        occurred_at=datetime.now(timezone.utc),
        message="ok",
        review_required=False,
    )

    return OCRDocumentResult(
        batch_id=batch_id,
        document_id=document_id,
        source_document_sha256=sha256,
        preprocessing_version="phase2-v1",
        status=OCRStatus.SUCCEEDED,
        pages=tuple(pages),
        event=event,
    )


def _normalization_input(ocr_result, source_name):
    return NormalizationInput(
        batch_id=ocr_result.batch_id,
        document_id=ocr_result.document_id,
        source_name=source_name,
        source_document_sha256=ocr_result.source_document_sha256,
        ocr_version="phase-3:paddleocr",
        ocr_result=ocr_result,
    )


def _header_and_table(supplier, invoice_number, currency, total, rows, page=1, y_offset=0):
    lines, tokens = [], []
    order = 0

    for text, x, y in (
        (f"SUPPLIER: {supplier}", 0, y_offset),
        (f"INVOICE NUMBER: {invoice_number}", 0, y_offset + 20),
        ("INVOICE DATE: 2024-01-15", 0, y_offset + 40),
        (currency, 0, y_offset + 60),
        (f"TOTAL: {total}", 0, y_offset + 80),
    ):
        order += 1
        token = _token(text, x, y, 250, 15, order, page=page)
        tokens.append(token)
        lines.append(_line(text, x, y, 250, 15, order, tokens=[token], page=page))

    header_cells = [("DESCRIPTION", 0), ("QUANTITY", 300), ("UNIT PRICE", 450), ("AMOUNT", 600)]
    table_y = y_offset + 120
    for text, x in header_cells:
        order += 1
        token = _token(text, x, table_y, 100, 15, order, page=page)
        tokens.append(token)
        lines.append(_line(text, x, table_y, 100, 15, order, tokens=[token], page=page))

    row_y = table_y + 20
    for description, quantity, unit_price, amount in rows:
        for value, x in ((description, 0), (quantity, 300), (unit_price, 450), (amount, 600)):
            if value is None:
                continue
            order += 1
            token = _token(str(value), x, row_y, 100, 15, order, page=page)
            tokens.append(token)
            lines.append(_line(str(value), x, row_y, 100, 15, order, tokens=[token], page=page))
        row_y += 20

    return lines, tokens


@pytest.fixture
def config(tmp_path):
    return NormalizationConfig(artifact_root=tmp_path)


def test_more_than_four_documents_normalize_independently(config):
    documents = []

    for index in range(7):
        lines, tokens = _header_and_table(
            supplier=f"Supplier {index}",
            invoice_number=f"INV-{1000 + index}",
            currency="USD",
            total=f"{10 + index}.00",
            rows=[(f"Item {index}", "1", f"{10 + index}.00", f"{10 + index}.00")],
        )
        ocr_result = _ocr_result([_page(lines, tokens)], sha256=f"{index:064d}")
        documents.append(_normalization_input(ocr_result, source_name=f"invoice-{index}.pdf"))

    results = [normalize_invoice_document(document, config=config) for document in documents]

    assert len(results) == 7
    assert len({r.invoice_record.invoice_record_id for r in results}) == 7
    for index, result in enumerate(results):
        assert result.invoice_record.get_field(InvoiceFieldName.INVOICE_NUMBER).normalized_value == f"INV-{1000 + index}"


def test_multiple_pages_are_all_considered_for_line_items(config):
    lines_1, tokens_1 = _header_and_table(
        supplier="Multi Page Co",
        invoice_number="MP-1",
        currency="USD",
        total="20.00",
        rows=[("Page 1 item", "1", "10.00", "10.00")],
        page=1,
    )
    lines_2, tokens_2 = [], []
    order = 900
    for text, x in [("DESCRIPTION", 0), ("QUANTITY", 300), ("UNIT PRICE", 450), ("AMOUNT", 600)]:
        order += 1
        token = _token(text, x, 0, 100, 15, order, page=2)
        tokens_2.append(token)
        lines_2.append(_line(text, x, 0, 100, 15, order, tokens=[token], page=2))
    for value, x in [("Page 2 item", 0), ("1", 300), ("10.00", 450), ("10.00", 600)]:
        order += 1
        token = _token(str(value), x, 20, 100, 15, order, page=2)
        tokens_2.append(token)
        lines_2.append(_line(str(value), x, 20, 100, 15, order, tokens=[token], page=2))

    ocr_result = _ocr_result([_page(lines_1, tokens_1, page_number=1), _page(lines_2, tokens_2, page_number=2)], sha256="1" * 64)
    normalization_input = _normalization_input(ocr_result, source_name="multi-page.pdf")

    result = normalize_invoice_document(normalization_input, config=config)

    descriptions = {
        item.description.normalized_value for item in result.invoice_record.line_items if item.description
    }
    assert "Page 1 item" in descriptions
    assert "Page 2 item" in descriptions


def test_varying_line_item_counts_across_documents(config):
    for count in (0, 1, 3, 10):
        rows = [(f"Item {i}", "1", "1.00", "1.00") for i in range(count)]
        lines, tokens = _header_and_table(
            supplier="Varying Co", invoice_number=f"VAR-{count}", currency="USD", total="1.00", rows=rows
        )
        ocr_result = _ocr_result([_page(lines, tokens)], sha256=f"{count:064d}")
        normalization_input = _normalization_input(ocr_result, source_name=f"varying-{count}.pdf")

        result = normalize_invoice_document(normalization_input, config=config)

        assert len(result.invoice_record.line_items) == count

        if count == 0:
            assert "NO_LINE_ITEMS_EXTRACTED" in result.review_reasons


def test_missing_optional_fields_are_absent_not_invented(config):
    lines, tokens = _header_and_table(
        supplier="Optional Co", invoice_number="OPT-1", currency="USD", total="10.00", rows=[("Item", "1", "10.00", "10.00")]
    )
    ocr_result = _ocr_result([_page(lines, tokens)], sha256="2" * 64)
    normalization_input = _normalization_input(ocr_result, source_name="optional.pdf")

    result = normalize_invoice_document(normalization_input, config=config)

    # DUE_DATE and PURCHASE_ORDER_NUMBER were never present in the OCR
    # evidence and must simply be absent, not defaulted or invented.
    assert result.invoice_record.get_field(InvoiceFieldName.DUE_DATE) is None
    assert result.invoice_record.get_field(InvoiceFieldName.PURCHASE_ORDER_NUMBER) is None


def test_missing_required_fields_route_to_review(config):
    ocr_result = _ocr_result([_page([], [])], sha256="3" * 64)
    normalization_input = _normalization_input(ocr_result, source_name="empty.pdf")

    result = normalize_invoice_document(normalization_input, config=config)

    assert result.status == NormalizationStatus.REVIEW_REQUIRED
    assert any(reason.startswith("REQUIRED_FIELD_MISSING:") for reason in result.review_reasons)


def test_duplicate_looking_values_are_isolated_by_document_id(config):
    """Two different documents that happen to share the same invoice number
    text must not collide: every ID stays namespaced by document_id."""

    results = []
    for index in range(2):
        lines, tokens = _header_and_table(
            supplier="Same Name Co", invoice_number="DUPLICATE-001", currency="USD", total="5.00", rows=[]
        )
        ocr_result = _ocr_result([_page(lines, tokens)], sha256=f"{index:064d}")
        normalization_input = _normalization_input(ocr_result, source_name=f"dup-{index}.pdf")
        results.append(normalize_invoice_document(normalization_input, config=config))

    invoice_number_field_1 = results[0].invoice_record.get_field(InvoiceFieldName.INVOICE_NUMBER)
    invoice_number_field_2 = results[1].invoice_record.get_field(InvoiceFieldName.INVOICE_NUMBER)

    assert invoice_number_field_1.normalized_value == invoice_number_field_2.normalized_value == "DUPLICATE-001"
    assert invoice_number_field_1.field_id != invoice_number_field_2.field_id
    assert results[0].invoice_record.invoice_record_id != results[1].invoice_record.invoice_record_id


@pytest.mark.parametrize("currency_text, expected_code", [("USD", "USD"), ("GBP", "GBP"), ("EUR", "EUR")])
def test_at_least_two_currencies_are_recognised(config, currency_text, expected_code):
    lines, tokens = _header_and_table(
        supplier="Currency Co", invoice_number="CUR-1", currency=currency_text, total="10.00", rows=[]
    )
    ocr_result = _ocr_result([_page(lines, tokens)], sha256="4" * 64)
    normalization_input = _normalization_input(ocr_result, source_name="currency.pdf")

    result = normalize_invoice_document(normalization_input, config=config)

    currency_field = result.invoice_record.get_field(InvoiceFieldName.CURRENCY)
    assert currency_field is not None
    assert currency_field.normalized_value == expected_code


@pytest.mark.parametrize(
    "raw_total, expected",
    [
        ("1.234,50 EUR", Decimal("1234.50")),
        ("$1,234.50", Decimal("1234.50")),
    ],
)
def test_decimal_values_with_commas_and_currency_symbols(config, raw_total, expected):
    lines, tokens = _header_and_table(
        supplier="Decimal Co", invoice_number="DEC-1", currency="USD", total=raw_total, rows=[]
    )
    ocr_result = _ocr_result([_page(lines, tokens)], sha256="5" * 64)
    normalization_input = _normalization_input(ocr_result, source_name="decimal.pdf")

    result = normalize_invoice_document(normalization_input, config=config)

    total_field = result.invoice_record.get_field(InvoiceFieldName.TOTAL_AMOUNT)
    assert total_field is not None
    assert total_field.normalized_value == expected


def test_more_candidates_than_the_ambiguity_threshold_can_resolve(tmp_path):
    """A field with many competing values within the ambiguity margin must
    stay ambiguous and reviewed, never silently pick an unsupported one."""

    config = NormalizationConfig(artifact_root=tmp_path, ambiguity_score_margin=50.0)

    lines, tokens = [], []
    order = 0
    for i, amount in enumerate(("10.00", "20.00", "30.00", "40.00", "50.00")):
        text = f"TOTAL: {amount}"
        order += 1
        token = _token(text, 0, i * 20, 150, 15, order)
        tokens.append(token)
        lines.append(_line(text, 0, i * 20, 150, 15, order, tokens=[token]))

    ocr_result = _ocr_result([_page(lines, tokens)], sha256="6" * 64)
    normalization_input = _normalization_input(ocr_result, source_name="ambiguous.pdf")

    result = normalize_invoice_document(normalization_input, config=config)

    total_field = result.invoice_record.get_field(InvoiceFieldName.TOTAL_AMOUNT)
    assert total_field is not None
    assert total_field.review_required is True
    assert "LOW_FIELD_CONFIDENCE" in total_field.review_reasons or any(
        "AMBIGUOUS" in reason for reason in result.review_reasons
    )
    assert result.status == NormalizationStatus.REVIEW_REQUIRED
