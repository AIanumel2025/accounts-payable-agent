"""M2 contract tests for Phase 4 (normalization) models.

Contracts only. Candidate-extraction and normalisation functions (cells
53-63) are out of scope until the processing-extraction milestone.
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


def test_ocr_evidence_index_search_needs_deferred_normalization_tools():
    """Documented deviation: `search()` calls `create_comparison_key`, a
    Phase 4 tool function deferred to the processing-extraction milestone.
    Until `tools/normalization.py` lands, calling `search()` fails with
    `NameError`; `get()` and `page()` do not depend on it."""

    index = OCREvidenceIndex(
        document_id=uuid4(),
        references_by_id={},
        token_references=(),
        line_references=(),
        references_by_page={},
    )
    with pytest.raises(NameError):
        index.search("TOTAL")


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
