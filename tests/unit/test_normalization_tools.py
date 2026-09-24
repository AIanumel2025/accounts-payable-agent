"""Unit tests for `ap_agent.tools.normalization`'s candidate-extraction and
value-normalisation functions (M5), using synthetic OCR contracts — no
invoice fixtures (CLAUDE.md: fixtures never enter production code, and
task §14 requires generated synthetic OCR contracts for focused tests).
"""

from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest

from ap_agent.config.settings import NormalizationConfig
from ap_agent.models.normalization import (
    EvidenceReferenceType,
    ExtractionMethod,
    InvoiceFieldName,
    NormalizationInput,
    NormalizedValueType,
)
from ap_agent.models.ocr import (
    BoundingBox,
    EvidenceLine,
    OCRDocumentResult,
    OCREvent,
    OCRPageResult,
    OCRStatus,
    OCRToken,
)
from ap_agent.tools.normalization import (
    build_ocr_evidence_index,
    candidate_ranking_key,
    create_field_candidate,
    deduplicate_candidates,
    extract_all_invoice_candidates,
    extract_currency_candidates,
    extract_document_field_candidates,
    extract_header_party_candidates,
    extract_inline_candidates,
    extract_labelled_text_candidates,
    is_explicit_purchase_order_identifier,
    line_to_evidence_reference,
    normalize_currency_code,
    normalize_date_value,
    normalize_invoice_number_value,
    normalize_monetary_value,
    parse_decimal_value,
    select_best_candidate,
    token_to_evidence_reference,
    value_is_compatible,
)

pytestmark = pytest.mark.unit


# --- synthetic OCR contract builders ----------------------------------------


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


def _line(text, x, y, w, h, order, tokens=(), page=1, confidence=95.0):
    return EvidenceLine(
        evidence_line_id=uuid4(),
        page_number=page,
        reading_order=order,
        text=text,
        mean_confidence=confidence,
        bounding_box=_box(x, y, w, h),
        token_evidence_ids=tuple(t.evidence_id for t in tokens),
    )


def _page(lines, tokens=(), page_number=1, status=OCRStatus.SUCCEEDED, review_reasons=()):
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


def _ocr_result(pages, batch_id=None, document_id=None, status=OCRStatus.SUCCEEDED, sha256="c" * 64):
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


@pytest.fixture
def config(tmp_path):
    return NormalizationConfig(artifact_root=tmp_path)


# --- evidence-reference construction and bounding-box conversion -----------


def test_token_to_evidence_reference_constructs_a_real_bounding_box():
    """Task §7 type-consistency correction: the field is stored as an
    actual `BoundingBox` instance, not a plain tuple, with the exact same
    coordinate values the notebook's `extract_bounding_box` computed."""

    token = _token("TOTAL", 10, 20, 100, 30, order=1)
    reference = token_to_evidence_reference(token)

    assert isinstance(reference.bounding_box, BoundingBox)
    assert reference.bounding_box.x == 10
    assert reference.bounding_box.y == 20
    assert reference.bounding_box.width == 100
    assert reference.bounding_box.height == 30
    assert reference.reference_type == EvidenceReferenceType.TOKEN
    assert reference.reference_id == token.evidence_id
    assert reference.raw_text == "TOTAL"


def test_line_to_evidence_reference_constructs_a_real_bounding_box():
    line = _line("TOTAL: $69.22", 5, 6, 50, 12, order=2)
    reference = line_to_evidence_reference(line)

    assert isinstance(reference.bounding_box, BoundingBox)
    assert (
        reference.bounding_box.x,
        reference.bounding_box.y,
        reference.bounding_box.width,
        reference.bounding_box.height,
    ) == (5, 6, 50, 12)
    assert reference.reference_type == EvidenceReferenceType.LINE
    assert reference.reference_id == line.evidence_line_id


def test_evidence_reference_bounding_box_serializes_and_round_trips():
    from ap_agent.artifacts.serialization import bounding_box_to_dict

    token = _token("TOTAL", 1, 2, 3, 4, order=1)
    reference = token_to_evidence_reference(token)

    payload = bounding_box_to_dict(reference.bounding_box)
    assert payload == {"x": 1, "y": 2, "width": 3, "height": 4, "right": 4, "bottom": 6}

    rebuilt = BoundingBox(x=payload["x"], y=payload["y"], width=payload["width"], height=payload["height"])
    assert rebuilt == reference.bounding_box


# --- evidence indexing -------------------------------------------------------


def test_build_ocr_evidence_index_indexes_tokens_and_lines_by_page():
    t1 = _token("TOTAL", 0, 0, 10, 10, order=1)
    l1 = _line("TOTAL: 69.22", 0, 0, 50, 10, order=1, tokens=[t1])
    t2 = _token("TAX", 0, 20, 10, 10, order=2, page=2)
    l2 = _line("TAX: 5.77", 0, 20, 50, 10, order=2, tokens=[t2], page=2)

    ocr_result = _ocr_result([_page([l1], [t1], page_number=1), _page([l2], [t2], page_number=2)])
    normalization_input = _normalization_input(ocr_result)

    index = build_ocr_evidence_index(normalization_input)

    assert len(index.token_references) == 2
    assert len(index.line_references) == 2
    assert index.get(t1.evidence_id).raw_text == "TOTAL"
    assert len(index.page(1)) == 2
    assert len(index.page(2)) == 2
    assert index.page(3) == ()


def test_build_ocr_evidence_index_rejects_document_id_mismatch():
    ocr_result = _ocr_result([_page([], [])])
    normalization_input = _normalization_input(ocr_result)
    mismatched = NormalizationInput(
        batch_id=normalization_input.batch_id,
        document_id=uuid4(),
        source_name="x",
        source_document_sha256=normalization_input.source_document_sha256,
        ocr_version=normalization_input.ocr_version,
        ocr_result=ocr_result,
    )

    with pytest.raises(ValueError, match="document ID"):
        build_ocr_evidence_index(mismatched)


# --- decimal, date, currency, invoice-number normalisation -----------------


@pytest.mark.parametrize(
    "raw_value, expected",
    [
        ("$1,234.50", Decimal("1234.50")),
        ("1.234,50 EUR", Decimal("1234.50")),
        ("DISCOUNT: (-) 12.54", Decimal("-12.54")),
        ("TAX:VAT (4.24%): 36.45 EUR", Decimal("36.45")),
        ("DISCOUNT(1.46%): (-) 12.54", Decimal("-12.54")),
    ],
)
def test_normalize_monetary_value_matches_notebook_self_asserts(raw_value, expected, config):
    assert normalize_monetary_value(raw_value, config=config) == expected


def test_normalize_monetary_value_selects_final_non_percentage_amount(config):
    assert normalize_monetary_value("Tax 5.77", config=config) == Decimal("5.77")


def test_parse_decimal_value_handles_none_and_missing_numbers():
    assert parse_decimal_value(None) is None
    assert parse_decimal_value("no numbers here") is None
    assert parse_decimal_value("qty: 3") == Decimal("3")


@pytest.mark.parametrize(
    "raw_value, expected",
    [
        ("12-Apr-2000", date(2000, 4, 12)),
        ("Issue Date: 2002-06-28", date(2002, 6, 28)),
        ("Mar 06 2012", date(2012, 3, 6)),
        ("03/06/2012", None),
    ],
)
def test_normalize_date_value_matches_notebook_self_asserts(raw_value, expected):
    assert normalize_date_value(raw_value) == expected


@pytest.mark.parametrize(
    "raw_value, expected",
    [
        ("TOTAL: 873.58 EUR", "EUR"),
        ("TOTAL $69.22", "USD"),
        ("no currency evidence here", None),
    ],
)
def test_normalize_currency_code(raw_value, expected):
    assert normalize_currency_code(raw_value) == expected


@pytest.mark.parametrize(
    "raw_value, expected",
    [
        ("#308044", "308044"),
        ("# 36258", "36258"),
        ("Date: 12-Apr-2000", None),
        ("2002-06-28", None),
    ],
)
def test_normalize_invoice_number_value(raw_value, expected):
    assert normalize_invoice_number_value(raw_value) == expected


# --- labelled and regex field extraction ------------------------------------


def test_extract_inline_candidates_finds_a_value_on_the_same_line(config):
    t1 = _token("TOTAL: 873.58 EUR", 0, 0, 150, 15, order=1)
    l1 = _line("TOTAL: 873.58 EUR", 0, 0, 150, 15, order=1, tokens=[t1])
    ocr_result = _ocr_result([_page([l1], [t1])])
    normalization_input = _normalization_input(ocr_result)
    index = build_ocr_evidence_index(normalization_input)

    candidates = extract_inline_candidates(
        document_id=normalization_input.document_id,
        evidence_index=index,
        field_name=InvoiceFieldName.TOTAL_AMOUNT,
        config=config,
    )

    assert len(candidates) == 1
    assert candidates[0].proposed_value == Decimal("873.58")
    assert candidates[0].extraction_method == ExtractionMethod.LABEL_VALUE


def test_extract_currency_candidates_is_regex_based(config):
    t1 = _token("EUR", 0, 0, 30, 10, order=1)
    l1 = _line("EUR", 0, 0, 30, 10, order=1, tokens=[t1])
    ocr_result = _ocr_result([_page([l1], [t1])])
    normalization_input = _normalization_input(ocr_result)
    index = build_ocr_evidence_index(normalization_input)

    candidates = extract_currency_candidates(
        document_id=normalization_input.document_id, evidence_index=index, config=config
    )

    assert len(candidates) == 1
    assert candidates[0].field_name == InvoiceFieldName.CURRENCY
    assert candidates[0].proposed_value == "EUR"
    assert candidates[0].extraction_method == ExtractionMethod.REGULAR_EXPRESSION


def test_extract_header_party_candidates_uses_left_right_position(config):
    supplier_token = _token("Acme Supplies Ltd", 5, 5, 150, 15, order=1)
    supplier_line = _line("Acme Supplies Ltd", 5, 5, 150, 15, order=1, tokens=[supplier_token])
    customer_token = _token("Widget Purchasing Co", 800, 5, 150, 15, order=2)
    customer_line = _line(
        "Widget Purchasing Co", 800, 5, 150, 15, order=2, tokens=[customer_token]
    )

    ocr_result = _ocr_result([_page([supplier_line, customer_line], [supplier_token, customer_token])])
    normalization_input = _normalization_input(ocr_result)
    index = build_ocr_evidence_index(normalization_input)

    result = extract_header_party_candidates(
        document_id=normalization_input.document_id, evidence_index=index, config=config
    )

    assert result[InvoiceFieldName.SUPPLIER_NAME]
    assert result[InvoiceFieldName.SUPPLIER_NAME][0].proposed_value == "Acme Supplies Ltd"


def test_extract_labelled_text_candidates_rejects_order_id_as_payment_terms(config):
    label_token = _token("Payment Terms: Order ID 12345", 0, 0, 200, 15, order=1)
    label_line = _line(
        "Payment Terms: Order ID 12345", 0, 0, 200, 15, order=1, tokens=[label_token]
    )
    ocr_result = _ocr_result([_page([label_line], [label_token])])
    normalization_input = _normalization_input(ocr_result)
    index = build_ocr_evidence_index(normalization_input)

    candidates = extract_labelled_text_candidates(
        document_id=normalization_input.document_id,
        evidence_index=index,
        field_name=InvoiceFieldName.PAYMENT_TERMS,
        config=config,
    )

    assert candidates == ()


# --- subtotal / tax / discount / shipping / total field differentiation ----


@pytest.mark.parametrize(
    "field_name, label",
    [
        (InvoiceFieldName.SUBTOTAL, "SUBTOTAL"),
        (InvoiceFieldName.TAX_AMOUNT, "TAX"),
        (InvoiceFieldName.DISCOUNT_AMOUNT, "DISCOUNT"),
        (InvoiceFieldName.SHIPPING_AMOUNT, "SHIPPING"),
        (InvoiceFieldName.TOTAL_AMOUNT, "TOTAL"),
    ],
)
def test_document_field_candidates_differentiate_the_five_monetary_fields(field_name, label, config):
    text = f"{label}: 10.00"
    token = _token(text, 0, 0, 100, 15, order=1)
    line = _line(text, 0, 0, 100, 15, order=1, tokens=[token])
    ocr_result = _ocr_result([_page([line], [token])])
    normalization_input = _normalization_input(ocr_result)
    index = build_ocr_evidence_index(normalization_input)

    candidates_by_field = extract_document_field_candidates(normalization_input, index, config=config)

    matching = [c for c in candidates_by_field.get(field_name, ()) if c.proposed_value == Decimal("10.00")]
    assert matching, f"expected a {field_name.value} candidate from label {label!r}"


def test_subtotal_label_does_not_produce_a_total_amount_candidate(config):
    """SUB TOTAL must not be picked up as TOTAL_AMOUNT (reference_is_valid_label's
    TOTAL_AMOUNT exclusion list)."""

    text = "SUBTOTAL: 63.45"
    token = _token(text, 0, 0, 100, 15, order=1)
    line = _line(text, 0, 0, 100, 15, order=1, tokens=[token])
    ocr_result = _ocr_result([_page([line], [token])])
    normalization_input = _normalization_input(ocr_result)
    index = build_ocr_evidence_index(normalization_input)

    candidates_by_field = extract_document_field_candidates(normalization_input, index, config=config)

    total_candidates = candidates_by_field.get(InvoiceFieldName.TOTAL_AMOUNT, ())
    assert not any(c.proposed_value == Decimal("63.45") for c in total_candidates)


# --- candidate ranking, ambiguity and deduplication -------------------------


def test_candidate_ranking_key_prefers_label_value_over_spatial_proximity(config):
    reference = line_to_evidence_reference(_line("x", 0, 0, 1, 1, order=1))

    label_candidate = create_field_candidate(
        document_id=uuid4(),
        field_name=InvoiceFieldName.TOTAL_AMOUNT,
        raw_value="10.00",
        evidence_references=(reference,),
        extraction_method=ExtractionMethod.LABEL_VALUE,
        rule_name="r1",
        config=config,
    )
    spatial_candidate = create_field_candidate(
        document_id=uuid4(),
        field_name=InvoiceFieldName.TOTAL_AMOUNT,
        raw_value="10.00",
        evidence_references=(reference,),
        extraction_method=ExtractionMethod.SPATIAL_PROXIMITY,
        rule_name="r2",
        config=config,
    )

    ranked = sorted([spatial_candidate, label_candidate], key=candidate_ranking_key)
    assert ranked[0] is label_candidate


def test_candidate_ranking_is_deterministic_across_repeated_sorts(config):
    reference = line_to_evidence_reference(_line("x", 0, 0, 1, 1, order=1))
    candidates = [
        create_field_candidate(
            document_id=uuid4(),
            field_name=InvoiceFieldName.TOTAL_AMOUNT,
            raw_value=f"{10 + i}.00",
            evidence_references=(reference,),
            extraction_method=ExtractionMethod.SPATIAL_PROXIMITY,
            rule_name=f"r{i}",
            config=config,
        )
        for i in range(5)
    ]

    first_sort = sorted(candidates, key=candidate_ranking_key)
    second_sort = sorted(list(reversed(candidates)), key=candidate_ranking_key)

    assert [c.candidate_id for c in first_sort] == [c.candidate_id for c in second_sort]


def test_select_best_candidate_flags_ambiguity_within_margin(config):
    reference = line_to_evidence_reference(_line("x", 0, 0, 1, 1, order=1))

    candidate_a = create_field_candidate(
        document_id=uuid4(),
        field_name=InvoiceFieldName.TOTAL_AMOUNT,
        raw_value="10.00",
        evidence_references=(reference,),
        extraction_method=ExtractionMethod.SPATIAL_PROXIMITY,
        rule_name="a",
        config=config,
    )
    candidate_b = create_field_candidate(
        document_id=uuid4(),
        field_name=InvoiceFieldName.TOTAL_AMOUNT,
        raw_value="20.00",
        evidence_references=(reference,),
        extraction_method=ExtractionMethod.SPATIAL_PROXIMITY,
        rule_name="b",
        config=config,
    )

    selection = select_best_candidate(
        InvoiceFieldName.TOTAL_AMOUNT, (candidate_a, candidate_b), config=config
    )

    assert selection.ambiguous is True
    assert "AMBIGUOUS_FIELD_CANDIDATES" in selection.review_reasons


def test_select_best_candidate_no_ambiguity_outside_margin():
    config = NormalizationConfig(artifact_root=Path("/tmp/x"), ambiguity_score_margin=1.0)
    reference = line_to_evidence_reference(_line("x", 0, 0, 1, 1, order=1))

    candidate_a = create_field_candidate(
        document_id=uuid4(),
        field_name=InvoiceFieldName.TOTAL_AMOUNT,
        raw_value="10.00",
        evidence_references=(reference,),
        extraction_method=ExtractionMethod.LABEL_VALUE,
        rule_name="a",
        confidence_adjustment=20.0,
        config=config,
    )
    candidate_b = create_field_candidate(
        document_id=uuid4(),
        field_name=InvoiceFieldName.TOTAL_AMOUNT,
        raw_value="20.00",
        evidence_references=(reference,),
        extraction_method=ExtractionMethod.LABEL_VALUE,
        rule_name="b",
        config=config,
    )

    selection = select_best_candidate(
        InvoiceFieldName.TOTAL_AMOUNT, (candidate_a, candidate_b), config=config
    )

    assert selection.ambiguous is False


def test_select_best_candidate_field_not_extracted_when_no_candidates(config):
    selection = select_best_candidate(InvoiceFieldName.TOTAL_AMOUNT, (), config=config)
    assert selection.selected_candidate is None
    assert selection.review_reasons == ("FIELD_NOT_EXTRACTED",)


def test_deduplicate_candidates_keeps_highest_confidence_per_value(config):
    reference = line_to_evidence_reference(_line("x", 0, 0, 1, 1, order=1))

    low = create_field_candidate(
        document_id=uuid4(),
        field_name=InvoiceFieldName.TOTAL_AMOUNT,
        raw_value="10.00",
        evidence_references=(reference,),
        extraction_method=ExtractionMethod.SPATIAL_PROXIMITY,
        rule_name="low",
        confidence_adjustment=-50.0,
        config=config,
    )
    high = create_field_candidate(
        document_id=uuid4(),
        field_name=InvoiceFieldName.TOTAL_AMOUNT,
        raw_value="10.00",
        evidence_references=(reference,),
        extraction_method=ExtractionMethod.LABEL_VALUE,
        rule_name="high",
        config=config,
    )

    deduplicated = deduplicate_candidates((low, high))
    assert len(deduplicated) == 1
    assert deduplicated[0] is high


# --- M7 short purchase-order identifier correction (notebook cell 62,
#     "PHASE 4 — CORRECTION CELL 4C") ----------------------------------------


@pytest.mark.parametrize(
    "raw_value, expected",
    [
        ("99", True),
        ("#1042", True),
        ("CA-2012-AB10015140-40974", True),
        ("", False),
        ("   ", False),
        ("PURCHASE ORDER", False),
        ("a" * 65, False),
    ],
)
def test_is_explicit_purchase_order_identifier(raw_value, expected):
    assert is_explicit_purchase_order_identifier(raw_value) is expected


def test_short_explicit_po_value_is_compatible(config):
    assert value_is_compatible(InvoiceFieldName.PURCHASE_ORDER_NUMBER, "99", config=config) is True


def test_long_po_identifiers_remain_compatible(config):
    """The correction is additive: existing longer identifiers that already
    satisfied the pre-M7 `PURCHASE_ORDER_PATTERN` must keep working."""

    assert (
        value_is_compatible(InvoiceFieldName.PURCHASE_ORDER_NUMBER, "CA-2012-AB10015140-40974", config=config)
        is True
    )


def test_empty_and_label_only_po_values_are_rejected(config):
    assert value_is_compatible(InvoiceFieldName.PURCHASE_ORDER_NUMBER, "", config=config) is False
    assert value_is_compatible(InvoiceFieldName.PURCHASE_ORDER_NUMBER, "PO Number:", config=config) is False


def test_labelled_short_po_number_is_extracted_as_a_candidate(config):
    """Explicitly labelled `PO Number: 99` must produce a
    PURCHASE_ORDER_NUMBER candidate with proposed_value "99" (task §3)."""

    t1 = _token("PO Number: 99", 0, 0, 150, 15, order=1)
    l1 = _line("PO Number: 99", 0, 0, 150, 15, order=1, tokens=[t1])
    ocr_result = _ocr_result([_page([l1], [t1])])
    normalization_input = _normalization_input(ocr_result)
    index = build_ocr_evidence_index(normalization_input)

    candidates = extract_inline_candidates(
        document_id=normalization_input.document_id,
        evidence_index=index,
        field_name=InvoiceFieldName.PURCHASE_ORDER_NUMBER,
        config=config,
    )

    assert len(candidates) == 1
    assert candidates[0].proposed_value == "99"


def test_unlabelled_short_number_is_not_extracted_as_a_po(config):
    """An unrelated, unlabelled short number elsewhere on the page must
    never become a PURCHASE_ORDER_NUMBER candidate: the correction only
    loosens value compatibility, never label matching (task §3)."""

    t1 = _token("99", 0, 0, 30, 15, order=1)
    l1 = _line("99", 0, 0, 30, 15, order=1, tokens=[t1])
    ocr_result = _ocr_result([_page([l1], [t1])])
    normalization_input = _normalization_input(ocr_result)
    index = build_ocr_evidence_index(normalization_input)

    candidates = extract_inline_candidates(
        document_id=normalization_input.document_id,
        evidence_index=index,
        field_name=InvoiceFieldName.PURCHASE_ORDER_NUMBER,
        config=config,
    )

    assert candidates == ()
