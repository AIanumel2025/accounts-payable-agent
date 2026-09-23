"""M2 contract tests for Phase 2 (preprocessing) models, cell 26.

Contracts only: `assess_page_quality` and the rest of the preprocessing
pipeline are processing functions, out of scope until the
processing-extraction milestone.
"""

import dataclasses
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import pytest

from ap_agent.config.settings import PreprocessingConfig
from ap_agent.models.preprocessing import (
    PageQuality,
    PreprocessedPage,
    PreprocessingEvent,
    PreprocessingInput,
    PreprocessingResult,
    PreprocessingStatus,
)


def test_preprocessing_status_members():
    assert [member.value for member in PreprocessingStatus] == [
        "SUCCEEDED",
        "REVIEW_REQUIRED",
        "FAILED",
    ]


def test_preprocessing_config_defaults():
    config = PreprocessingConfig(artifact_root=Path("/tmp/ap-agent"))
    assert config.pdf_dpi == 300
    assert config.enable_deskew is True
    assert config.maximum_deskew_angle == 15.0
    assert config.clahe_tile_grid_size == (8, 8)
    assert config.minimum_width == 800
    assert config.minimum_height == 800
    assert config.preprocessing_version == "phase2-v1"


def test_preprocessing_config_is_frozen_and_instances_are_independent():
    first = PreprocessingConfig(artifact_root=Path("/tmp/a"), pdf_dpi=150)
    second = PreprocessingConfig(artifact_root=Path("/tmp/b"))
    assert first.pdf_dpi == 150
    assert second.pdf_dpi == 300

    with pytest.raises(dataclasses.FrozenInstanceError):
        second.pdf_dpi = 72


def test_page_quality_and_preprocessed_page_are_frozen():
    quality = PageQuality(
        page_number=1,
        width=1700,
        height=2200,
        brightness_score=241.49,
        contrast_score=48.95,
        blur_score=1120.34,
        detected_skew_angle=0.0,
        quality_flags=(),
        review_required=False,
    )
    page = PreprocessedPage(
        page_number=1,
        original_render_path=Path("pages/page_001/original_render.png"),
        processed_image_path=Path("pages/page_001/processed.png"),
        original_render_sha256="a" * 64,
        processed_image_sha256="b" * 64,
        quality=quality,
    )
    assert page.quality.review_required is False

    with pytest.raises(dataclasses.FrozenInstanceError):
        page.page_number = 2


def test_preprocessing_result_errors_default_to_empty_tuple():
    batch_id = uuid4()
    document_id = uuid4()
    event = PreprocessingEvent(
        event_type="phase2.preprocessing",
        status=PreprocessingStatus.SUCCEEDED,
        batch_id=batch_id,
        document_id=document_id,
        occurred_at=datetime.now(timezone.utc),
        message="ok",
        review_required=False,
    )
    result = PreprocessingResult(
        batch_id=batch_id,
        document_id=document_id,
        source_path=Path("invoice.pdf"),
        source_sha256="c" * 64,
        status=PreprocessingStatus.SUCCEEDED,
        pages=(),
        event=event,
    )
    assert result.errors == ()
    assert isinstance(result.errors, tuple)


def test_preprocessing_input_is_a_plain_frozen_dataclass():
    preprocessing_input = PreprocessingInput(
        batch_id=uuid4(),
        document_id=uuid4(),
        source_path=Path("invoice.pdf"),
        source_sha256="d" * 64,
    )
    assert dataclasses.is_dataclass(preprocessing_input)
    assert preprocessing_input.__dataclass_params__.frozen is True
