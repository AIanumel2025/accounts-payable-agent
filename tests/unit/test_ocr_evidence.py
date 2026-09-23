"""Unit tests for `ap_agent.tools.ocr_evidence`: deterministic evidence-ID
formulas and bounding-box utilities shared by both OCR adapters (notebook
cell 36).
"""

from uuid import uuid4

import pytest

from ap_agent.models.ocr import BoundingBox
from ap_agent.tools.ocr_evidence import (
    combine_bounding_boxes,
    create_line_evidence_id,
    create_token_evidence_id,
    validate_bounding_box,
)

pytestmark = pytest.mark.unit


def _box(x=0, y=0, width=10, height=10):
    return BoundingBox(x=x, y=y, width=width, height=height)


# --- deterministic IDs -------------------------------------------------------


def test_create_token_evidence_id_is_deterministic():
    document_id = uuid4()
    box = _box()

    first = create_token_evidence_id(
        document_id=document_id,
        page_number=1,
        reading_order=1,
        text="TOTAL",
        bounding_box=box,
        processed_image_sha256="a" * 64,
        ocr_version="ocr-v2-paddle",
    )
    second = create_token_evidence_id(
        document_id=document_id,
        page_number=1,
        reading_order=1,
        text="TOTAL",
        bounding_box=box,
        processed_image_sha256="a" * 64,
        ocr_version="ocr-v2-paddle",
    )

    assert first == second


@pytest.mark.parametrize(
    "field,value",
    [
        ("page_number", 2),
        ("reading_order", 2),
        ("text", "OTHER"),
        ("processed_image_sha256", "b" * 64),
        ("ocr_version", "ocr-v3"),
    ],
)
def test_create_token_evidence_id_changes_with_any_identity_component(field, value):
    document_id = uuid4()
    base_kwargs = dict(
        document_id=document_id,
        page_number=1,
        reading_order=1,
        text="TOTAL",
        bounding_box=_box(),
        processed_image_sha256="a" * 64,
        ocr_version="ocr-v2-paddle",
    )
    baseline = create_token_evidence_id(**base_kwargs)

    changed_kwargs = dict(base_kwargs)
    changed_kwargs[field] = value
    changed = create_token_evidence_id(**changed_kwargs)

    assert baseline != changed


def test_create_token_evidence_id_changes_with_bounding_box():
    document_id = uuid4()
    baseline = create_token_evidence_id(
        document_id=document_id,
        page_number=1,
        reading_order=1,
        text="TOTAL",
        bounding_box=_box(x=0),
        processed_image_sha256="a" * 64,
        ocr_version="ocr-v2-paddle",
    )
    moved = create_token_evidence_id(
        document_id=document_id,
        page_number=1,
        reading_order=1,
        text="TOTAL",
        bounding_box=_box(x=50),
        processed_image_sha256="a" * 64,
        ocr_version="ocr-v2-paddle",
    )
    assert baseline != moved


def test_token_and_line_evidence_ids_differ_for_identical_inputs():
    """The 'ocr-token'/'ocr-line' prefix is part of the identity material,
    so a token and a line with otherwise identical inputs get different
    IDs."""
    document_id = uuid4()
    kwargs = dict(
        document_id=document_id,
        page_number=1,
        reading_order=1,
        text="TOTAL",
        bounding_box=_box(),
        processed_image_sha256="a" * 64,
        ocr_version="ocr-v2-paddle",
    )
    token_id = create_token_evidence_id(**kwargs)
    line_id = create_line_evidence_id(**kwargs)
    assert token_id != line_id


def test_evidence_ids_are_unique_across_documents():
    box = _box()
    first = create_token_evidence_id(
        document_id=uuid4(),
        page_number=1,
        reading_order=1,
        text="TOTAL",
        bounding_box=box,
        processed_image_sha256="a" * 64,
        ocr_version="ocr-v2-paddle",
    )
    second = create_token_evidence_id(
        document_id=uuid4(),
        page_number=1,
        reading_order=1,
        text="TOTAL",
        bounding_box=box,
        processed_image_sha256="a" * 64,
        ocr_version="ocr-v2-paddle",
    )
    assert first != second


# --- bounding-box utilities ---------------------------------------------------


def test_combine_bounding_boxes_returns_the_enclosing_rectangle():
    boxes = [_box(x=0, y=0, width=10, height=10), _box(x=20, y=5, width=5, height=15)]
    combined = combine_bounding_boxes(boxes)
    assert combined == BoundingBox(x=0, y=0, width=25, height=20)


def test_combine_bounding_boxes_of_a_single_box_returns_it_unchanged():
    box = _box(x=3, y=4, width=10, height=12)
    assert combine_bounding_boxes([box]) == box


def test_combine_bounding_boxes_raises_on_empty_list():
    with pytest.raises(ValueError):
        combine_bounding_boxes([])


def test_validate_bounding_box_accepts_a_box_within_bounds():
    validate_bounding_box(_box(x=0, y=0, width=10, height=10), image_width=100, image_height=100)


def test_validate_bounding_box_accepts_a_box_touching_the_edge():
    validate_bounding_box(_box(x=90, y=90, width=10, height=10), image_width=100, image_height=100)


@pytest.mark.parametrize(
    "box",
    [
        BoundingBox(x=-1, y=0, width=10, height=10),
        BoundingBox(x=0, y=-1, width=10, height=10),
        BoundingBox(x=0, y=0, width=0, height=10),
        BoundingBox(x=0, y=0, width=10, height=0),
        BoundingBox(x=0, y=0, width=10, height=-5),
    ],
)
def test_validate_bounding_box_rejects_invalid_geometry(box):
    with pytest.raises(ValueError):
        validate_bounding_box(box, image_width=100, image_height=100)


def test_validate_bounding_box_rejects_a_box_exceeding_image_width():
    with pytest.raises(ValueError):
        validate_bounding_box(_box(x=95, y=0, width=10, height=10), image_width=100, image_height=100)


def test_validate_bounding_box_rejects_a_box_exceeding_image_height():
    with pytest.raises(ValueError):
        validate_bounding_box(_box(x=0, y=95, width=10, height=10), image_width=100, image_height=100)
