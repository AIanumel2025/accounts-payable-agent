"""Unit tests for `ap_agent.adapters.paddleocr_adapter` (notebook cells
41, 44). Every test mocks the PaddleOCR prediction result and/or engine —
no OCR model download or GPU/CPU inference happens here. The real,
network-dependent PaddleOCR run is `tests/integration/test_phase_3_ocr.py`
(marked `requires_paddle`).
"""

from pathlib import Path
from uuid import uuid4

import numpy as np
import pytest

from ap_agent.adapters.paddleocr_adapter import (
    build_paddle_evidence,
    create_engine,
    extract_paddle_ocr_page,
    extract_paddle_preprocessed_image,
    extract_paddle_result_payload,
    get_paddleocr_version,
    paddle_box_to_bounding_box,
    polygon_to_bounding_box,
)
from ap_agent.artifacts.filesystem import calculate_file_sha256
from ap_agent.config.settings import OCRConfig, PaddleEngineOptions
from ap_agent.models.ocr import OCRPageInput, OCRStatus

pytestmark = pytest.mark.unit


class FakePredictionResult:
    def __init__(self, payload, img=None):
        self.json = payload
        self.img = img if img is not None else {}


class FakeEngine:
    def __init__(self, results):
        self._results = results
        self.calls = []

    def predict(self, path):
        self.calls.append(path)
        return self._results


def _ocr_config(tmp_path, **overrides):
    return OCRConfig(
        artifact_root=tmp_path / "ocr",
        ocr_engine_name="paddleocr",
        ocr_version="ocr-v2-paddle",
        **overrides,
    )


def _write_image(tmp_path, name="page.png", size=(400, 300)):
    import cv2

    image = np.full((size[1], size[0], 3), 255, dtype=np.uint8)
    path = tmp_path / name
    cv2.imwrite(str(path), image)
    return path


# --- extract_paddle_result_payload --------------------------------------------


def test_extract_paddle_result_payload_unwraps_a_res_key():
    result = FakePredictionResult({"res": {"rec_texts": ["A"]}})
    payload = extract_paddle_result_payload(result)
    assert payload == {"rec_texts": ["A"]}


def test_extract_paddle_result_payload_parses_a_json_string():
    result = FakePredictionResult('{"res": {"rec_texts": ["A"]}}')
    payload = extract_paddle_result_payload(result)
    assert payload == {"rec_texts": ["A"]}


def test_extract_paddle_result_payload_calls_a_callable_json_attribute():
    result = FakePredictionResult(lambda: {"rec_texts": ["A"]})
    payload = extract_paddle_result_payload(result)
    assert payload == {"rec_texts": ["A"]}


def test_extract_paddle_result_payload_raises_on_unsupported_shape():
    result = FakePredictionResult(["not", "a", "dict"])
    with pytest.raises(TypeError):
        extract_paddle_result_payload(result)


def test_extract_paddle_result_payload_converts_numpy_values_to_json_safe():
    result = FakePredictionResult({"res": {"rec_scores": [np.float32(0.95)]}})
    payload = extract_paddle_result_payload(result)
    assert payload["rec_scores"] == [pytest.approx(0.95, abs=1e-4)]
    assert isinstance(payload["rec_scores"][0], float)


# --- extract_paddle_preprocessed_image ----------------------------------------


def test_extract_paddle_preprocessed_image_returns_pil_image_when_provided(tmp_path):
    from PIL import Image

    fallback_path = _write_image(tmp_path)
    pil_image = Image.new("RGB", (10, 10), color=(1, 2, 3))
    result = FakePredictionResult({}, img={"preprocessed_img": pil_image})

    returned = extract_paddle_preprocessed_image(result, fallback_path)
    assert returned.size == (10, 10)
    assert returned.mode == "RGB"


def test_extract_paddle_preprocessed_image_converts_a_numpy_array(tmp_path):
    fallback_path = _write_image(tmp_path)
    array = np.zeros((20, 30, 3), dtype=np.uint8)
    result = FakePredictionResult({}, img={"preprocessed_img": array})

    returned = extract_paddle_preprocessed_image(result, fallback_path)
    assert returned.size == (30, 20)  # PIL size is (width, height)


def test_extract_paddle_preprocessed_image_converts_a_grayscale_array(tmp_path):
    fallback_path = _write_image(tmp_path)
    array = np.zeros((20, 30), dtype=np.uint8)
    result = FakePredictionResult({}, img={"preprocessed_img": array})

    returned = extract_paddle_preprocessed_image(result, fallback_path)
    assert returned.mode == "RGB"


def test_extract_paddle_preprocessed_image_falls_back_to_the_phase_2_page(tmp_path):
    fallback_path = _write_image(tmp_path, size=(50, 60))
    result = FakePredictionResult({}, img={})

    returned = extract_paddle_preprocessed_image(result, fallback_path)
    assert returned.size == (50, 60)


def test_extract_paddle_preprocessed_image_calls_a_callable_img_attribute(tmp_path):
    fallback_path = _write_image(tmp_path)
    array = np.zeros((5, 5, 3), dtype=np.uint8)
    result = FakePredictionResult({}, img=lambda: {"preprocessed_img": array})

    returned = extract_paddle_preprocessed_image(result, fallback_path)
    assert returned.size == (5, 5)


# --- bounding-box conversion ---------------------------------------------------


def test_paddle_box_to_bounding_box_converts_a_rectangle():
    box = paddle_box_to_bounding_box([10, 20, 110, 220], image_width=1000, image_height=1000)
    assert (box.x, box.y, box.width, box.height) == (10, 20, 100, 200)


def test_paddle_box_to_bounding_box_clips_to_image_bounds():
    box = paddle_box_to_bounding_box([-5, -5, 2000, 2000], image_width=100, image_height=100)
    assert box.x == 0
    assert box.y == 0
    assert box.right <= 100
    assert box.bottom <= 100


def test_paddle_box_to_bounding_box_requires_four_coordinates():
    with pytest.raises(ValueError):
        paddle_box_to_bounding_box([1, 2, 3], image_width=100, image_height=100)


def test_polygon_to_bounding_box_takes_the_enclosing_rectangle():
    polygon = [[10, 20], [110, 20], [110, 220], [10, 220]]
    box = polygon_to_bounding_box(polygon, image_width=1000, image_height=1000)
    assert (box.x, box.y, box.width, box.height) == (10, 20, 100, 200)


def test_polygon_to_bounding_box_rejects_an_empty_polygon():
    with pytest.raises(ValueError):
        polygon_to_bounding_box([], image_width=100, image_height=100)


# --- build_paddle_evidence -----------------------------------------------------


def _page_input():
    return OCRPageInput(
        page_number=1,
        processed_image_path=Path("processed.png"),
        processed_image_sha256="a" * 64,
    )


def test_build_paddle_evidence_converts_confidence_to_a_0_100_scale():
    payload = {
        "rec_texts": ["TOTAL"],
        "rec_scores": [0.995],
        "rec_boxes": [[0, 0, 50, 20]],
    }
    tokens, lines = build_paddle_evidence(
        payload, document_id=uuid4(), page_input=_page_input(),
        evidence_image_sha256="b" * 64, image_width=500, image_height=500,
        config=_ocr_config_no_root(),
    )
    assert tokens[0].confidence == 99.5
    assert lines[0].mean_confidence == 99.5


def _ocr_config_no_root():
    return OCRConfig(artifact_root=Path("/tmp/unused"), ocr_version="ocr-v2-paddle")


def test_build_paddle_evidence_skips_blank_text_regions():
    payload = {
        "rec_texts": ["", "REAL"],
        "rec_scores": [0.9, 0.9],
        "rec_boxes": [[0, 0, 10, 10], [20, 20, 40, 40]],
    }
    tokens, lines = build_paddle_evidence(
        payload, document_id=uuid4(), page_input=_page_input(),
        evidence_image_sha256="b" * 64, image_width=500, image_height=500,
        config=_ocr_config_no_root(),
    )
    assert len(tokens) == 1
    assert tokens[0].text == "REAL"


def test_build_paddle_evidence_orders_by_y_then_x():
    payload = {
        "rec_texts": ["BOTTOM", "TOP_RIGHT", "TOP_LEFT"],
        "rec_scores": [0.9, 0.9, 0.9],
        "rec_boxes": [[0, 100, 10, 110], [50, 0, 60, 10], [0, 0, 10, 10]],
    }
    tokens, _ = build_paddle_evidence(
        payload, document_id=uuid4(), page_input=_page_input(),
        evidence_image_sha256="b" * 64, image_width=500, image_height=500,
        config=_ocr_config_no_root(),
    )
    assert [t.text for t in tokens] == ["TOP_LEFT", "TOP_RIGHT", "BOTTOM"]


def test_build_paddle_evidence_uses_polygons_when_boxes_absent():
    payload = {
        "rec_texts": ["POLY"],
        "rec_scores": [0.9],
        "rec_polys": [[[0, 0], [10, 0], [10, 10], [0, 10]]],
    }
    tokens, _ = build_paddle_evidence(
        payload, document_id=uuid4(), page_input=_page_input(),
        evidence_image_sha256="b" * 64, image_width=500, image_height=500,
        config=_ocr_config_no_root(),
    )
    assert len(tokens) == 1
    assert tokens[0].bounding_box.width == 10


def test_build_paddle_evidence_raises_on_text_score_count_mismatch():
    payload = {"rec_texts": ["A", "B"], "rec_scores": [0.9], "rec_boxes": [[0, 0, 5, 5]]}
    with pytest.raises(ValueError):
        build_paddle_evidence(
            payload, document_id=uuid4(), page_input=_page_input(),
            evidence_image_sha256="b" * 64, image_width=500, image_height=500,
            config=_ocr_config_no_root(),
        )


def test_build_paddle_evidence_token_id_uses_paddle_suffixed_ocr_version():
    """Evidence IDs use config.ocr_version + '-paddle' (R-09, preserved)."""
    from ap_agent.tools.ocr_evidence import create_token_evidence_id

    payload = {"rec_texts": ["X"], "rec_scores": [0.9], "rec_boxes": [[0, 0, 10, 10]]}
    document_id = uuid4()
    page_input = _page_input()
    config = _ocr_config_no_root()

    tokens, _ = build_paddle_evidence(
        payload, document_id=document_id, page_input=page_input,
        evidence_image_sha256="c" * 64, image_width=500, image_height=500,
        config=config,
    )

    from ap_agent.models.ocr import BoundingBox

    expected_id = create_token_evidence_id(
        document_id=document_id,
        page_number=1,
        reading_order=1,
        text="X",
        bounding_box=BoundingBox(x=0, y=0, width=10, height=10),
        processed_image_sha256="c" * 64,
        ocr_version=config.ocr_version + "-paddle",
    )
    assert tokens[0].evidence_id == expected_id


# --- extract_paddle_ocr_page (routing-level, mocked engine) --------------------


def test_extract_paddle_ocr_page_verifies_source_hash(tmp_path):
    image_path = _write_image(tmp_path)
    page_input = OCRPageInput(
        page_number=1,
        processed_image_path=image_path,
        processed_image_sha256="0" * 64,
    )
    config = _ocr_config(tmp_path)
    engine = FakeEngine([FakePredictionResult({"res": {}})])

    with pytest.raises(ValueError, match="SHA-256"):
        extract_paddle_ocr_page(
            document_id=uuid4(), page_input=page_input, config=config,
            evidence_image_destination=tmp_path / "evidence.png",
            engine=engine, engine_version="3.7.0",
        )


def test_extract_paddle_ocr_page_raises_when_engine_returns_multiple_results(tmp_path):
    image_path = _write_image(tmp_path)
    page_input = OCRPageInput(
        page_number=1,
        processed_image_path=image_path,
        processed_image_sha256=calculate_file_sha256(image_path),
    )
    config = _ocr_config(tmp_path)
    engine = FakeEngine([FakePredictionResult({"res": {}}), FakePredictionResult({"res": {}})])

    with pytest.raises(ValueError, match="one PaddleOCR result"):
        extract_paddle_ocr_page(
            document_id=uuid4(), page_input=page_input, config=config,
            evidence_image_destination=tmp_path / "evidence.png",
            engine=engine, engine_version="3.7.0",
        )


def test_extract_paddle_ocr_page_builds_a_succeeded_result(tmp_path):
    image_path = _write_image(tmp_path, size=(400, 300))
    page_input = OCRPageInput(
        page_number=1,
        processed_image_path=image_path,
        processed_image_sha256=calculate_file_sha256(image_path),
    )
    config = _ocr_config(tmp_path)
    payload = {
        "res": {
            "rec_texts": [
                "INVOICE",
                "NUMBER 12345",
                "SUPPLIER NAME HERE",
                "CUSTOMER NAME HERE",
                "TOTAL 123.45",
            ],
            "rec_scores": [0.99, 0.98, 0.97, 0.96, 0.95],
            "rec_boxes": [
                [10, 10, 100, 30],
                [10, 40, 120, 60],
                [10, 70, 150, 90],
                [10, 100, 150, 120],
                [10, 130, 120, 150],
            ],
        }
    }
    engine = FakeEngine([FakePredictionResult(payload, img={})])
    evidence_destination = tmp_path / "page_001" / "evidence_image.png"

    page_result, raw_payload = extract_paddle_ocr_page(
        document_id=uuid4(), page_input=page_input, config=config,
        evidence_image_destination=evidence_destination,
        engine=engine, engine_version="3.7.0",
    )

    assert page_result.ocr_engine == "paddleocr"
    assert page_result.ocr_engine_version == "3.7.0"
    assert page_result.status == OCRStatus.SUCCEEDED
    assert len(page_result.tokens) == 5
    assert evidence_destination.exists()
    assert page_result.evidence_image_sha256 == calculate_file_sha256(evidence_destination)
    assert engine.calls == [str(image_path)]
    assert isinstance(raw_payload, dict)


def test_extract_paddle_ocr_page_flags_low_confidence_pages(tmp_path):
    image_path = _write_image(tmp_path)
    page_input = OCRPageInput(
        page_number=1,
        processed_image_path=image_path,
        processed_image_sha256=calculate_file_sha256(image_path),
    )
    config = _ocr_config(tmp_path)
    # Enough tokens/characters to pass those gates, but low confidence.
    payload = {
        "res": {
            "rec_texts": [f"word{i}" for i in range(10)],
            "rec_scores": [0.3] * 10,
            "rec_boxes": [[i * 10, 0, i * 10 + 8, 10] for i in range(10)],
        }
    }
    engine = FakeEngine([FakePredictionResult(payload, img={})])

    page_result, _ = extract_paddle_ocr_page(
        document_id=uuid4(), page_input=page_input, config=config,
        evidence_image_destination=tmp_path / "evidence.png",
        engine=engine, engine_version="3.7.0",
    )

    assert page_result.status == OCRStatus.REVIEW_REQUIRED
    assert "LOW_PAGE_CONFIDENCE" in page_result.review_reasons


# --- create_engine / get_paddleocr_version -------------------------------------


def test_create_engine_uses_the_cell_44_default_options(monkeypatch):
    captured = {}

    class FakePaddleOCR:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    device_calls = []
    monkeypatch.setattr("paddle.set_device", lambda device: device_calls.append(device))
    monkeypatch.setattr("paddleocr.PaddleOCR", FakePaddleOCR)

    engine = create_engine()

    assert isinstance(engine, FakePaddleOCR)
    assert device_calls == ["cpu"]
    assert captured == {
        "lang": "en",
        "device": "cpu",
        "use_doc_orientation_classify": False,
        "use_doc_unwarping": True,
        "use_textline_orientation": True,
        "enable_mkldnn": False,
        "enable_hpi": False,
        "cpu_threads": 4,
    }


def test_create_engine_honors_custom_options(monkeypatch):
    captured = {}
    monkeypatch.setattr("paddle.set_device", lambda device: None)
    monkeypatch.setattr(
        "paddleocr.PaddleOCR", lambda **kwargs: captured.update(kwargs) or object()
    )

    create_engine(PaddleEngineOptions(cpu_threads=8, enable_mkldnn=True))

    assert captured["cpu_threads"] == 8
    assert captured["enable_mkldnn"] is True


def test_get_paddleocr_version_returns_a_nonempty_string():
    version = get_paddleocr_version()
    assert isinstance(version, str)
    assert version
