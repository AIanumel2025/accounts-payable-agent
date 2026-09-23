"""M2 contract tests for Phase 4 (normalization) models.

Contracts only. Candidate-extraction and normalisation functions (cells
53-63) are out of scope until the processing-extraction milestone, with
one narrow exception: `clean_ocr_text` and `create_comparison_key` (cell
53) are pure text-normalisation helpers extracted so that the public
`OCREvidenceIndex.search()` contract method is operational, rather than
predictably raising `NameError`.
"""

import dataclasses
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest

from ap_agent.config.settings import NormalizationConfig
from ap_agent.models.normalization import (
    CandidateSelection,
    EvidenceReference,
    EvidenceReferenceType,
    ExtractionMethod,
    InvoiceFieldCandidate,
    InvoiceFieldName,
    LineItemCandidateGroup,
    NormalizationEvent,
    NormalizationResult,
    NormalizationStatus,
    NormalizedInvoiceField,
    NormalizedInvoiceRecord,
    NormalizedLineItem,
    NormalizedValueType,
    OCREvidenceIndex,
    clean_ocr_text,
    create_comparison_key,
)


def test_normalization_status_members():
    assert [member.value for member in NormalizationStatus] == [
        "SUCCEEDED",
        "REVIEW_REQUIRED",
        "FAILED",
    ]


def test_invoice_field_name_has_nineteen_members():
    assert len(InvoiceFieldName) == 19
    assert InvoiceFieldName.TOTAL_AMOUNT.value == "TOTAL_AMOUNT"
    assert InvoiceFieldName.PURCHASE_ORDER_NUMBER.value == "PURCHASE_ORDER_NUMBER"


def test_normalization_config_defaults():
    config = NormalizationConfig(artifact_root=Path("/tmp/ap-agent"))
    assert config.normalization_version == "normalization-v1"
    assert config.monetary_quantization == Decimal("0.01")
    assert config.maximum_line_items == 500
    assert config.required_fields == (
        InvoiceFieldName.SUPPLIER_NAME,
        InvoiceFieldName.INVOICE_NUMBER,
        InvoiceFieldName.INVOICE_DATE,
        InvoiceFieldName.CURRENCY,
        InvoiceFieldName.TOTAL_AMOUNT,
    )


def test_normalization_config_instances_are_independent():
    first = NormalizationConfig(
        artifact_root=Path("/tmp/a"), normalization_version="custom-v9"
    )
    second = NormalizationConfig(artifact_root=Path("/tmp/b"))
    assert first.normalization_version == "custom-v9"
    assert second.normalization_version == "normalization-v1"

    with pytest.raises(dataclasses.FrozenInstanceError):
        second.normalization_version = "mutated"


def test_evidence_reference_bounding_box_annotation_is_not_enforced():
    """Documented deviation (R-09): the field is annotated `BoundingBox` but
    the validated Phase 4 code populates it with a plain 4-tuple. Plain
    dataclasses do not validate field types at runtime, so both shapes are
    accepted here, matching the notebook's actual behaviour."""

    reference = EvidenceReference(
        reference_id=uuid4(),
        reference_type=EvidenceReferenceType.TOKEN,
        page_number=1,
        reading_order=0,
        raw_text="TOTAL",
        confidence=99.0,
        bounding_box=(0, 0, 10, 10),
    )
    assert reference.bounding_box == (0, 0, 10, 10)


def test_invoice_field_candidate_and_normalized_field_roundtrip():
    reference = EvidenceReference(
        reference_id=uuid4(),
        reference_type=EvidenceReferenceType.LINE,
        page_number=1,
        reading_order=0,
        raw_text="TOTAL 69.22",
        confidence=99.0,
        bounding_box=(0, 0, 10, 10),
    )
    candidate = InvoiceFieldCandidate(
        candidate_id=uuid4(),
        field_name=InvoiceFieldName.TOTAL_AMOUNT,
        raw_value="69.22",
        proposed_value=Decimal("69.22"),
        value_type=NormalizedValueType.DECIMAL,
        confidence=95.0,
        extraction_method=ExtractionMethod.LABEL_VALUE,
        evidence_references=(reference,),
    )
    assert candidate.review_required is False
    assert candidate.review_reasons == ()

    normalized = NormalizedInvoiceField(
        field_id=uuid4(),
        field_name=InvoiceFieldName.TOTAL_AMOUNT,
        raw_value="69.22",
        normalized_value=Decimal("69.22"),
        value_type=NormalizedValueType.DECIMAL,
        confidence=95.0,
        extraction_method=ExtractionMethod.LABEL_VALUE,
        evidence_references=(reference,),
    )
    assert normalized.normalization_notes == ()


def test_normalized_invoice_record_get_field():
    batch_id = uuid4()
    document_id = uuid4()
    reference = EvidenceReference(
        reference_id=uuid4(),
        reference_type=EvidenceReferenceType.LINE,
        page_number=1,
        reading_order=0,
        raw_text="EUR",
        confidence=90.0,
        bounding_box=(0, 0, 5, 5),
    )
    currency_field = NormalizedInvoiceField(
        field_id=uuid4(),
        field_name=InvoiceFieldName.CURRENCY,
        raw_value="EUR",
        normalized_value="EUR",
        value_type=NormalizedValueType.CURRENCY_CODE,
        confidence=90.0,
        extraction_method=ExtractionMethod.REGULAR_EXPRESSION,
        evidence_references=(reference,),
    )
    record = NormalizedInvoiceRecord(
        invoice_record_id=uuid4(),
        batch_id=batch_id,
        document_id=document_id,
        source_name="invoice.pdf",
        source_document_sha256="a" * 64,
        fields=(currency_field,),
        line_items=(),
        normalization_version="normalization-v1",
        created_at=datetime.now(timezone.utc),
    )
    assert record.get_field(InvoiceFieldName.CURRENCY) is currency_field
    assert record.get_field(InvoiceFieldName.TOTAL_AMOUNT) is None

    with pytest.raises(dataclasses.FrozenInstanceError):
        record.review_required = True


def test_normalized_line_item_allows_none_fields():
    line_item = NormalizedLineItem(
        line_item_id=uuid4(),
        line_number=1,
        description=None,
        quantity=None,
        unit_price=None,
        amount=None,
        currency=None,
        confidence=0.0,
        evidence_references=(),
    )
    assert line_item.description is None
    assert line_item.review_reasons == ()


def test_normalization_result_defaults():
    batch_id = uuid4()
    document_id = uuid4()
    event = NormalizationEvent(
        event_type="phase4.normalization",
        status=NormalizationStatus.SUCCEEDED,
        batch_id=batch_id,
        document_id=document_id,
        occurred_at=datetime.now(timezone.utc),
        message="ok",
        review_required=False,
    )
    result = NormalizationResult(
        batch_id=batch_id,
        document_id=document_id,
        source_document_sha256="b" * 64,
        ocr_version="phase-3:paddleocr",
        normalization_version="normalization-v1",
        status=NormalizationStatus.SUCCEEDED,
        invoice_record=None,
        field_candidates=(),
        event=event,
    )
    assert result.review_reasons == ()
    assert result.errors == ()


def test_ocr_evidence_index_get_and_page():
    reference = EvidenceReference(
        reference_id=uuid4(),
        reference_type=EvidenceReferenceType.TOKEN,
        page_number=1,
        reading_order=0,
        raw_text="TOTAL",
        confidence=99.0,
        bounding_box=(0, 0, 10, 10),
    )
    index = OCREvidenceIndex(
        document_id=uuid4(),
        references_by_id={str(reference.reference_id): reference},
        token_references=(reference,),
        line_references=(),
        references_by_page={1: (reference,)},
    )
    assert index.get(reference.reference_id) is reference
    assert index.get(uuid4()) is None
    assert index.page(1) == (reference,)
    assert index.page(2) == ()


def test_clean_ocr_text_handles_none_and_collapses_whitespace():
    """Verbatim notebook behaviour (cell 53): `None` maps to `""`; runs of
    whitespace (including non-breaking spaces, via NFKC normalisation)
    collapse to a single space; leading/trailing whitespace is stripped;
    case and punctuation are otherwise preserved."""

    assert clean_ocr_text(None) == ""
    assert clean_ocr_text("") == ""
    assert clean_ocr_text("  TOTAL:   $69.22  ") == "TOTAL: $69.22"
    assert clean_ocr_text("Line1\n\nLine2\t\tLine3") == "Line1 Line2 Line3"
    # NFKC normalisation folds a non-breaking space into an ordinary one,
    # which WHITESPACE_PATTERN then collapses.
    assert clean_ocr_text("Snyder, Hammond") == "Snyder, Hammond"
    assert clean_ocr_text(42) == "42"


def test_create_comparison_key_uppercases_and_strips_non_alphanumerics():
    """Verbatim notebook behaviour (cell 53): case-insensitive, punctuation
    and whitespace are removed entirely (not just collapsed), and `None`
    or an empty value produces an empty key."""

    assert create_comparison_key("Total:") == "TOTAL"
    assert create_comparison_key("  total  ") == "TOTAL"
    assert create_comparison_key("Bill To") == "BILLTO"
    assert create_comparison_key("TOTAL") == create_comparison_key("  ToTaL:  ")
    assert create_comparison_key(None) == ""
    assert create_comparison_key("") == ""
    assert create_comparison_key("---") == ""


def test_ocr_evidence_index_search_executes_successfully():
    reference = EvidenceReference(
        reference_id=uuid4(),
        reference_type=EvidenceReferenceType.LINE,
        page_number=1,
        reading_order=0,
        raw_text="TOTAL: $69.22",
        confidence=99.0,
        bounding_box=(0, 0, 10, 10),
    )
    index = OCREvidenceIndex(
        document_id=uuid4(),
        references_by_id={str(reference.reference_id): reference},
        token_references=(),
        line_references=(reference,),
        references_by_page={1: (reference,)},
    )

    # Non-exact, case-insensitive, punctuation-insensitive substring match.
    assert index.search("total") == (reference,)
    assert index.search("$69.22") == (reference,)

    # An empty or all-punctuation search key matches nothing, per the
    # notebook's `if not search_key: return tuple()` short-circuit.
    assert index.search("") == ()
    assert index.search("   ") == ()
    assert index.search("---") == ()

    # A value with no matching reference returns the notebook-compatible
    # empty result, not an error.
    assert index.search("nonexistent") == ()

    # reference_type filtering still applies alongside the fixed search.
    assert index.search("total", reference_type=EvidenceReferenceType.TOKEN) == ()
    assert index.search("total", reference_type=EvidenceReferenceType.LINE) == (
        reference,
    )


def test_ocr_evidence_index_search_exact_matches():
    reference = EvidenceReference(
        reference_id=uuid4(),
        reference_type=EvidenceReferenceType.TOKEN,
        page_number=1,
        reading_order=0,
        raw_text="TOTAL",
        confidence=99.0,
        bounding_box=(0, 0, 10, 10),
    )
    index = OCREvidenceIndex(
        document_id=uuid4(),
        references_by_id={str(reference.reference_id): reference},
        token_references=(reference,),
        line_references=(),
        references_by_page={1: (reference,)},
    )

    # Exact comparison-key equality: "TOTAL" matches "TOTAL" exactly.
    assert index.search("TOTAL", exact=True) == (reference,)
    assert index.search("total", exact=True) == (reference,)

    # A comparison key that is only a substring does not match exactly.
    assert index.search("TOT", exact=True) == ()
    assert index.search("TOT", exact=False) == (reference,)


def test_ocr_evidence_index_get_page_and_search_are_independent():
    """`get()`, `page()` and `search()` must each work on their own: none
    is required to make another succeed, and none mutates the index."""

    reference = EvidenceReference(
        reference_id=uuid4(),
        reference_type=EvidenceReferenceType.TOKEN,
        page_number=1,
        reading_order=0,
        raw_text="TOTAL",
        confidence=99.0,
        bounding_box=(0, 0, 10, 10),
    )
    index = OCREvidenceIndex(
        document_id=uuid4(),
        references_by_id={str(reference.reference_id): reference},
        token_references=(reference,),
        line_references=(),
        references_by_page={1: (reference,)},
    )

    before = dict(index.references_by_id)

    # Call in an order that does not favour any single method, and confirm
    # each returns its own correct, independent result.
    assert index.search("TOTAL") == (reference,)
    assert index.page(1) == (reference,)
    assert index.get(reference.reference_id) is reference
    assert index.page(1) == (reference,)
    assert index.get(reference.reference_id) is reference
    assert index.search("TOTAL") == (reference,)

    assert index.references_by_id == before


def test_ocr_evidence_index_instances_do_not_share_mutable_state():
    first_reference = EvidenceReference(
        reference_id=uuid4(),
        reference_type=EvidenceReferenceType.TOKEN,
        page_number=1,
        reading_order=0,
        raw_text="ALPHA",
        confidence=99.0,
        bounding_box=(0, 0, 10, 10),
    )
    second_reference = EvidenceReference(
        reference_id=uuid4(),
        reference_type=EvidenceReferenceType.TOKEN,
        page_number=1,
        reading_order=0,
        raw_text="BETA",
        confidence=99.0,
        bounding_box=(0, 0, 10, 10),
    )

    first_index = OCREvidenceIndex(
        document_id=uuid4(),
        references_by_id={str(first_reference.reference_id): first_reference},
        token_references=(first_reference,),
        line_references=(),
        references_by_page={1: (first_reference,)},
    )
    second_index = OCREvidenceIndex(
        document_id=uuid4(),
        references_by_id={str(second_reference.reference_id): second_reference},
        token_references=(second_reference,),
        line_references=(),
        references_by_page={1: (second_reference,)},
    )

    assert first_index.search("alpha") == (first_reference,)
    assert first_index.search("beta") == ()
    assert second_index.search("beta") == (second_reference,)
    assert second_index.search("alpha") == ()

    assert first_index.references_by_id is not second_index.references_by_id
    assert first_index.get(second_reference.reference_id) is None
    assert second_index.get(first_reference.reference_id) is None

    with pytest.raises(dataclasses.FrozenInstanceError):
        first_index.document_id = second_index.document_id


def test_candidate_selection_and_line_item_group_instantiate():
    reference = EvidenceReference(
        reference_id=uuid4(),
        reference_type=EvidenceReferenceType.LINE,
        page_number=1,
        reading_order=0,
        raw_text="Widget",
        confidence=88.0,
        bounding_box=(0, 0, 10, 10),
    )
    candidate = InvoiceFieldCandidate(
        candidate_id=uuid4(),
        field_name=InvoiceFieldName.LINE_DESCRIPTION,
        raw_value="Widget",
        proposed_value="Widget",
        value_type=NormalizedValueType.TEXT,
        confidence=88.0,
        extraction_method=ExtractionMethod.TABLE_STRUCTURE,
        evidence_references=(reference,),
    )
    selection = CandidateSelection(
        field_name=InvoiceFieldName.LINE_DESCRIPTION,
        selected_candidate=candidate,
        all_candidates=(candidate,),
        ambiguous=False,
        review_reasons=(),
    )
    assert selection.selected_candidate is candidate

    group = LineItemCandidateGroup(
        group_id=uuid4(),
        page_number=1,
        row_number=1,
        description=candidate,
        quantity=None,
        unit_price=None,
        amount=None,
        evidence_references=(reference,),
        confidence=88.0,
        review_required=False,
        review_reasons=(),
    )
    assert group.description is candidate
