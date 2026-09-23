"""PaddleOCR adapter (primary provider).

Source: notebook cell 41 ("PHASE 3 — CORRECTION CELL 4B") for
`extract_paddle_result_payload`, `extract_paddle_preprocessed_image`,
`paddle_box_to_bounding_box`, `polygon_to_bounding_box`,
`build_paddle_evidence`, `extract_paddle_ocr_page`; cell 44 ("PHASE 3 —
RUNTIME COMPATIBILITY CORRECTION") for the final, active engine
construction options (active-definition table §2
`adapters/paddleocr_adapter.py`).

Depends only on `ap_agent.tools.ocr_evidence` (leaf module) and
`ap_agent.artifacts` writers/serialisers, per the acyclic dependency graph
in `docs/modularisation_map.md` §4.3 (avoids the C-1
`tools/ocr` <-> `adapters/*` cycle risk).

Hidden-global correction (§5.1 of the modularisation map): the notebook's
`extract_paddle_ocr_page` read the module globals `paddle_ocr_engine`
(built once, at cell 44) and `PADDLEOCR_VERSION` (probed once, at cell 40).
Both are explicit parameters here (`engine`, `engine_version`) instead —
"Pass an engine object ... into the router. Never build it at import
time." `create_engine` is the adapter factory the map calls for; nothing
constructs a `PaddleOCR` instance, calls `paddle.set_device`, or imports
`paddle`/`paddleocr` at module import time (CLAUDE.md, C-5).
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from statistics import mean
from typing import Any, TYPE_CHECKING
from uuid import UUID

from ap_agent.artifacts.filesystem import (
    calculate_file_sha256,
    save_pil_image_atomically,
)
from ap_agent.artifacts.serialization import make_json_compatible
from ap_agent.config.settings import OCRConfig, PaddleEngineOptions
from ap_agent.models.ocr import (
    BoundingBox,
    EvidenceLine,
    OCRPageInput,
    OCRPageResult,
    OCRStatus,
    OCRToken,
)
from ap_agent.tools.ocr_evidence import (
    create_line_evidence_id,
    create_token_evidence_id,
    validate_bounding_box,
)

if TYPE_CHECKING:
    from PIL import Image

__all__ = [
    "create_engine",
    "get_paddleocr_version",
    "extract_paddle_result_payload",
    "extract_paddle_preprocessed_image",
    "paddle_box_to_bounding_box",
    "polygon_to_bounding_box",
    "build_paddle_evidence",
    "extract_paddle_ocr_page",
]


def create_engine(options: PaddleEngineOptions | None = None) -> Any:
    """Build one PaddleOCR engine instance, explicitly.

    Uses the final, active options from notebook cell 44 by default (not
    cell 40's superseded construction, §1.3 of the modularisation map).
    The `paddle.set_device(...)` side effect (notebook cell 40) moves
    inside this factory (§6.1): it never runs at import time, and it never
    runs more than once per engine unless the caller calls this factory
    again.
    """

    import paddle
    from paddleocr import PaddleOCR

    if options is None:
        options = PaddleEngineOptions()

    paddle.set_device(options.device)

    return PaddleOCR(
        lang=options.language,
        device=options.device,
        use_doc_orientation_classify=options.use_doc_orientation_classify,
        use_doc_unwarping=options.use_doc_unwarping,
        use_textline_orientation=options.use_textline_orientation,
        enable_mkldnn=options.enable_mkldnn,
        enable_hpi=options.enable_hpi,
        cpu_threads=options.cpu_threads,
    )


def get_paddleocr_version() -> str:
    import paddleocr

    return str(paddleocr.__version__)


# ---------------------------------------------------------
# 2. Convert arrays and provider objects to JSON-safe values
# ---------------------------------------------------------
# (make_json_compatible lives in ap_agent.artifacts.serialization; imported
# above, not redefined here.)


# ---------------------------------------------------------
# 4. Normalize the current PaddleOCR result object
# ---------------------------------------------------------

def extract_paddle_result_payload(
    prediction_result: Any,
) -> dict:
    import json

    payload = prediction_result.json

    if callable(payload):
        payload = payload()

    if isinstance(payload, str):
        payload = json.loads(payload)

    payload = make_json_compatible(
        payload
    )

    if (
        isinstance(payload, dict)
        and "res" in payload
        and isinstance(payload["res"], dict)
    ):
        payload = payload["res"]

    if not isinstance(payload, dict):
        raise TypeError(
            "PaddleOCR returned an unsupported "
            "result payload."
        )

    return payload


def extract_paddle_preprocessed_image(
    prediction_result: Any,
    fallback_image_path: Path,
) -> "Image.Image":
    import numpy as np
    from PIL import Image

    image_collection = prediction_result.img

    if callable(image_collection):
        image_collection = (
            image_collection()
        )

    if isinstance(image_collection, dict):
        preprocessed_image = (
            image_collection.get(
                "preprocessed_img"
            )
        )

        if preprocessed_image is not None:
            if isinstance(
                preprocessed_image,
                Image.Image,
            ):
                return (
                    preprocessed_image
                    .convert("RGB")
                )

            if isinstance(
                preprocessed_image,
                np.ndarray,
            ):
                array = preprocessed_image

                if array.ndim == 2:
                    return Image.fromarray(
                        array
                    ).convert("RGB")

                return Image.fromarray(
                    array
                ).convert("RGB")

    # Clean documents may not require an unwarped
    # derivative. Preserve the Phase 2 page in that case.
    with Image.open(
        fallback_image_path
    ) as fallback_image:
        return fallback_image.convert(
            "RGB"
        ).copy()


# ---------------------------------------------------------
# 5. Normalize PaddleOCR bounding regions
# ---------------------------------------------------------

def paddle_box_to_bounding_box(
    box: list,
    image_width: int,
    image_height: int,
) -> BoundingBox:
    if len(box) != 4:
        raise ValueError(
            "PaddleOCR rectangular box must "
            "contain four coordinates."
        )

    x_min, y_min, x_max, y_max = [
        int(round(float(value)))
        for value in box
    ]

    # Clip minor provider rounding differences to
    # the exact evidence-image boundary.
    x_min = max(
        0,
        min(x_min, image_width - 1),
    )

    y_min = max(
        0,
        min(y_min, image_height - 1),
    )

    x_max = max(
        x_min + 1,
        min(x_max, image_width),
    )

    y_max = max(
        y_min + 1,
        min(y_max, image_height),
    )

    bounding_box = BoundingBox(
        x=x_min,
        y=y_min,
        width=x_max - x_min,
        height=y_max - y_min,
    )

    validate_bounding_box(
        bounding_box=bounding_box,
        image_width=image_width,
        image_height=image_height,
    )

    return bounding_box


def polygon_to_bounding_box(
    polygon: list,
    image_width: int,
    image_height: int,
) -> BoundingBox:
    if not polygon:
        raise ValueError(
            "PaddleOCR polygon is empty."
        )

    x_values = [
        float(point[0])
        for point in polygon
    ]

    y_values = [
        float(point[1])
        for point in polygon
    ]

    return paddle_box_to_bounding_box(
        box=[
            min(x_values),
            min(y_values),
            max(x_values),
            max(y_values),
        ],
        image_width=image_width,
        image_height=image_height,
    )


# ---------------------------------------------------------
# 6. Build PaddleOCR evidence tokens and lines
# ---------------------------------------------------------

def build_paddle_evidence(
    payload: dict,
    document_id: UUID,
    page_input: OCRPageInput,
    evidence_image_sha256: str,
    image_width: int,
    image_height: int,
    config: OCRConfig,
) -> tuple[
    tuple[OCRToken, ...],
    tuple[EvidenceLine, ...],
]:
    recognized_texts = list(
        payload.get("rec_texts") or []
    )

    recognition_scores = list(
        payload.get("rec_scores") or []
    )

    rectangular_boxes = list(
        payload.get("rec_boxes") or []
    )

    polygons = list(
        payload.get("rec_polys") or []
    )

    if len(recognized_texts) != len(
        recognition_scores
    ):
        raise ValueError(
            "PaddleOCR text and confidence "
            "counts do not match."
        )

    if not rectangular_boxes and polygons:
        rectangular_boxes = [
            None
            for _ in polygons
        ]

    if len(recognized_texts) != len(
        rectangular_boxes
    ):
        raise ValueError(
            "PaddleOCR text and bounding-box "
            "counts do not match."
        )

    raw_regions = []

    for index, text_value in enumerate(
        recognized_texts
    ):
        text = str(text_value).strip()

        if not text:
            continue

        provider_score = float(
            recognition_scores[index]
        )

        # PaddleOCR reports confidence on a
        # 0-1 scale. Our contract uses 0-100.
        confidence = round(
            max(
                0.0,
                min(provider_score, 1.0),
            )
            * 100,
            2,
        )

        provider_box = (
            rectangular_boxes[index]
        )

        if provider_box is not None:
            bounding_box = (
                paddle_box_to_bounding_box(
                    box=provider_box,
                    image_width=image_width,
                    image_height=image_height,
                )
            )
        elif index < len(polygons):
            bounding_box = (
                polygon_to_bounding_box(
                    polygon=polygons[index],
                    image_width=image_width,
                    image_height=image_height,
                )
            )
        else:
            raise ValueError(
                "PaddleOCR result has no "
                "coordinate region."
            )

        raw_regions.append(
            {
                "text": text,
                "confidence": confidence,
                "bounding_box": bounding_box,
                "provider_index": index,
            }
        )

    # Establish deterministic reading order.
    ordered_regions = sorted(
        raw_regions,
        key=lambda region: (
            region["bounding_box"].y,
            region["bounding_box"].x,
            region["provider_index"],
        ),
    )

    tokens = []
    evidence_lines = []

    for reading_order, region in enumerate(
        ordered_regions,
        start=1,
    ):
        text = region["text"]
        confidence = region["confidence"]
        bounding_box = region[
            "bounding_box"
        ]

        token_id = create_token_evidence_id(
            document_id=document_id,
            page_number=page_input.page_number,
            reading_order=reading_order,
            text=text,
            bounding_box=bounding_box,
            processed_image_sha256=(
                evidence_image_sha256
            ),
            ocr_version=(
                config.ocr_version
                + "-paddle"
            ),
        )

        # PaddleOCR recognizes a complete detected
        # text region. We retain that region as one
        # evidence token rather than inventing
        # word-level coordinates.
        token = OCRToken(
            evidence_id=token_id,
            page_number=page_input.page_number,
            reading_order=reading_order,
            text=text,
            confidence=confidence,
            bounding_box=bounding_box,
            block_number=reading_order,
            paragraph_number=1,
            line_number=reading_order,
            word_number=1,
        )

        line_id = create_line_evidence_id(
            document_id=document_id,
            page_number=page_input.page_number,
            reading_order=reading_order,
            text=text,
            bounding_box=bounding_box,
            processed_image_sha256=(
                evidence_image_sha256
            ),
            ocr_version=(
                config.ocr_version
                + "-paddle"
            ),
        )

        evidence_line = EvidenceLine(
            evidence_line_id=line_id,
            page_number=page_input.page_number,
            reading_order=reading_order,
            text=text,
            mean_confidence=confidence,
            bounding_box=bounding_box,
            token_evidence_ids=(
                token_id,
            ),
        )

        tokens.append(token)
        evidence_lines.append(
            evidence_line
        )

    return (
        tuple(tokens),
        tuple(evidence_lines),
    )


# ---------------------------------------------------------
# 7. Extract one page with local PaddleOCR
# ---------------------------------------------------------

def extract_paddle_ocr_page(
    document_id: UUID,
    page_input: OCRPageInput,
    config: OCRConfig,
    evidence_image_destination: Path,
    engine: Any,
    engine_version: str,
) -> tuple[OCRPageResult, dict]:
    image_path = Path(
        page_input.processed_image_path
    )

    if not image_path.is_file():
        raise FileNotFoundError(
            f"Processed page not found: "
            f"{image_path}"
        )

    actual_input_sha256 = (
        calculate_file_sha256(
            image_path
        )
    )

    if (
        actual_input_sha256
        != page_input.processed_image_sha256
    ):
        raise ValueError(
            "Processed page SHA-256 does not "
            "match the Phase 2 evidence record."
        )

    prediction_results = list(
        engine.predict(
            str(image_path)
        )
    )

    if len(prediction_results) != 1:
        raise ValueError(
            "Expected one PaddleOCR result "
            "for one page, received "
            f"{len(prediction_results)}."
        )

    prediction_result = (
        prediction_results[0]
    )

    payload = extract_paddle_result_payload(
        prediction_result
    )

    evidence_image = (
        extract_paddle_preprocessed_image(
            prediction_result=(
                prediction_result
            ),
            fallback_image_path=image_path,
        )
    )

    save_pil_image_atomically(
        image=evidence_image,
        destination=(
            evidence_image_destination
        ),
    )

    evidence_image_sha256 = (
        calculate_file_sha256(
            evidence_image_destination
        )
    )

    image_width, image_height = (
        evidence_image.size
    )

    tokens, evidence_lines = (
        build_paddle_evidence(
            payload=payload,
            document_id=document_id,
            page_input=page_input,
            evidence_image_sha256=(
                evidence_image_sha256
            ),
            image_width=image_width,
            image_height=image_height,
            config=config,
        )
    )

    page_text = "\n".join(
        line.text
        for line in evidence_lines
    ).strip()

    if tokens:
        mean_confidence = round(
            mean(
                token.confidence
                for token in tokens
            ),
            2,
        )

        low_confidence_token_count = sum(
            token.confidence
            < config.minimum_token_confidence
            for token in tokens
        )

        low_confidence_ratio = round(
            low_confidence_token_count
            / len(tokens),
            4,
        )
    else:
        mean_confidence = 0.0
        low_confidence_token_count = 0
        low_confidence_ratio = 1.0

    meaningful_character_count = len(
        "".join(
            character
            for character in page_text
            if not character.isspace()
        )
    )

    review_reasons = []

    if (
        page_input
        .preprocessing_review_required
    ):
        review_reasons.append(
            "PREPROCESSING_REVIEW_REQUIRED"
        )

    if (
        len(tokens)
        < config.minimum_meaningful_tokens
    ):
        review_reasons.append(
            "INSUFFICIENT_OCR_TOKENS"
        )

    if (
        meaningful_character_count
        < config.minimum_meaningful_characters
    ):
        review_reasons.append(
            "INSUFFICIENT_OCR_TEXT"
        )

    if (
        tokens
        and mean_confidence
        < config.minimum_page_mean_confidence
    ):
        review_reasons.append(
            "LOW_PAGE_CONFIDENCE"
        )

    if (
        tokens
        and low_confidence_ratio
        > config.maximum_low_confidence_ratio
    ):
        review_reasons.append(
            "EXCESSIVE_LOW_CONFIDENCE_TOKENS"
        )

    status = (
        OCRStatus.REVIEW_REQUIRED
        if review_reasons
        else OCRStatus.SUCCEEDED
    )

    page_result = OCRPageResult(
        page_number=page_input.page_number,
        processed_image_path=image_path,
        processed_image_sha256=(
            actual_input_sha256
        ),
        evidence_image_path=(
            evidence_image_destination
        ),
        evidence_image_sha256=(
            evidence_image_sha256
        ),
        page_text=page_text,
        tokens=tokens,
        evidence_lines=evidence_lines,
        mean_confidence=mean_confidence,
        low_confidence_token_count=(
            low_confidence_token_count
        ),
        low_confidence_ratio=(
            low_confidence_ratio
        ),
        status=status,
        review_reasons=tuple(
            review_reasons
        ),
        ocr_engine="paddleocr",
        ocr_engine_version=(
            engine_version
        ),
        ocr_configuration=(
            "lang=en;"
            "doc_orientation=false;"
            "doc_unwarping=true;"
            "textline_orientation=true"
        ),
        processed_at=datetime.now(
            timezone.utc
        ),
    )

    return (
        page_result,
        make_json_compatible(payload),
    )
