"""Tesseract OCR adapter (fallback provider).

Source: notebook cell 36 ("PHASE 3 — CELL 2") for `run_tesseract_page`,
`build_ocr_tokens`, `build_evidence_lines`, `build_raw_ocr_rows`; cell 43
("PHASE 3 — CORRECTION CELL 4C") for the final, active
`extract_tesseract_fallback_page` (active-definition table §2
`adapters/tesseract_adapter.py`).

`extract_ocr_page@36` (the cell-36 orchestrator that called these helpers
directly) is SUPERSEDED and not extracted: its only caller,
`process_ocr_document@38`, is itself superseded by the routed orchestrator
in `ap_agent.tools.ocr` (§3.1 of the modularisation map).

Depends only on `ap_agent.tools.ocr_evidence` (leaf module) and
`ap_agent.artifacts.filesystem.calculate_file_sha256`, per the acyclic
dependency graph in `docs/modularisation_map.md` §4.3 (avoids the C-1
`tools/ocr` <-> `adapters/*` cycle risk).

`pytesseract` and `PIL.Image` are imported lazily inside the functions
that need them (CLAUDE.md); no local Tesseract engine version probe runs
at import time (the notebook's `TESSERACT_VERSION = str(pytesseract.get_tesseract_version())`
hidden global is replaced by `get_tesseract_version()`, called lazily —
§5.1 of the modularisation map: "Adapter attribute read lazily from
`pytesseract.get_tesseract_version()`").
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from statistics import mean
from uuid import UUID

from ap_agent.artifacts.filesystem import calculate_file_sha256
from ap_agent.config.settings import OCRConfig
from ap_agent.models.ocr import (
    BoundingBox,
    EvidenceLine,
    OCRPageInput,
    OCRPageResult,
    OCRStatus,
    OCRToken,
)
from ap_agent.tools.ocr_evidence import (
    combine_bounding_boxes,
    create_line_evidence_id,
    create_token_evidence_id,
    validate_bounding_box,
)

__all__ = [
    "get_tesseract_version",
    "run_tesseract_page",
    "build_ocr_tokens",
    "build_evidence_lines",
    "build_raw_ocr_rows",
    "extract_tesseract_fallback_page",
]


def get_tesseract_version() -> str:
    import pytesseract

    return str(pytesseract.get_tesseract_version())


# ---------------------------------------------------------
# 3. Raw Tesseract extraction
# ---------------------------------------------------------

def run_tesseract_page(
    image_path: Path,
    config: OCRConfig,
) -> dict:
    import pytesseract
    from PIL import Image

    if not image_path.is_file():
        raise FileNotFoundError(
            f"OCR image not found: {image_path}"
        )

    with Image.open(image_path) as image:
        ocr_data = pytesseract.image_to_data(
            image,
            lang=config.language,
            config=config.tesseract_config_string,
            output_type=pytesseract.Output.DICT,
        )

    required_keys = {
        "text",
        "conf",
        "left",
        "top",
        "width",
        "height",
        "block_num",
        "par_num",
        "line_num",
        "word_num",
    }

    missing_keys = required_keys - set(
        ocr_data.keys()
    )

    if missing_keys:
        raise ValueError(
            "Tesseract output is missing required keys: "
            f"{sorted(missing_keys)}"
        )

    row_count = len(ocr_data["text"])

    for key in required_keys:
        if len(ocr_data[key]) != row_count:
            raise ValueError(
                "Tesseract output columns have "
                "inconsistent lengths."
            )

    return ocr_data


# ---------------------------------------------------------
# 4. Build word-level evidence tokens
# ---------------------------------------------------------

def build_ocr_tokens(
    ocr_data: dict,
    document_id: UUID,
    page_input: OCRPageInput,
    image_width: int,
    image_height: int,
    config: OCRConfig,
) -> tuple[OCRToken, ...]:
    tokens = []
    reading_order = 0

    row_count = len(ocr_data["text"])

    for index in range(row_count):
        text = str(
            ocr_data["text"][index]
        ).strip()

        try:
            confidence = float(
                ocr_data["conf"][index]
            )
        except (TypeError, ValueError):
            confidence = -1.0

        # Tesseract uses confidence -1 for structural
        # rows that do not represent recognized words.
        if not text or confidence < 0:
            continue

        width = int(
            ocr_data["width"][index]
        )

        height = int(
            ocr_data["height"][index]
        )

        if width <= 0 or height <= 0:
            continue

        reading_order += 1

        bounding_box = BoundingBox(
            x=int(ocr_data["left"][index]),
            y=int(ocr_data["top"][index]),
            width=width,
            height=height,
        )

        validate_bounding_box(
            bounding_box=bounding_box,
            image_width=image_width,
            image_height=image_height,
        )

        evidence_id = create_token_evidence_id(
            document_id=document_id,
            page_number=page_input.page_number,
            reading_order=reading_order,
            text=text,
            bounding_box=bounding_box,
            processed_image_sha256=(
                page_input.processed_image_sha256
            ),
            ocr_version=config.ocr_version,
        )

        token = OCRToken(
            evidence_id=evidence_id,
            page_number=page_input.page_number,
            reading_order=reading_order,
            text=text,
            confidence=round(
                confidence,
                2,
            ),
            bounding_box=bounding_box,
            block_number=int(
                ocr_data["block_num"][index]
            ),
            paragraph_number=int(
                ocr_data["par_num"][index]
            ),
            line_number=int(
                ocr_data["line_num"][index]
            ),
            word_number=int(
                ocr_data["word_num"][index]
            ),
        )

        tokens.append(token)

    return tuple(tokens)


# ---------------------------------------------------------
# 5. Group tokens into evidence lines
# ---------------------------------------------------------

def build_evidence_lines(
    tokens: tuple[OCRToken, ...],
    document_id: UUID,
    page_input: OCRPageInput,
    config: OCRConfig,
) -> tuple[EvidenceLine, ...]:
    grouped_tokens = defaultdict(list)

    for token in tokens:
        line_key = (
            token.block_number,
            token.paragraph_number,
            token.line_number,
        )

        grouped_tokens[line_key].append(
            token
        )

    ordered_groups = sorted(
        grouped_tokens.values(),
        key=lambda group: min(
            token.reading_order
            for token in group
        ),
    )

    evidence_lines = []

    for line_reading_order, group in enumerate(
        ordered_groups,
        start=1,
    ):
        ordered_tokens = sorted(
            group,
            key=lambda token: (
                token.reading_order,
                token.bounding_box.x,
            ),
        )

        line_text = " ".join(
            token.text
            for token in ordered_tokens
        ).strip()

        if not line_text:
            continue

        line_box = combine_bounding_boxes(
            [
                token.bounding_box
                for token in ordered_tokens
            ]
        )

        line_confidence = round(
            mean(
                token.confidence
                for token in ordered_tokens
            ),
            2,
        )

        line_id = create_line_evidence_id(
            document_id=document_id,
            page_number=page_input.page_number,
            reading_order=line_reading_order,
            text=line_text,
            bounding_box=line_box,
            processed_image_sha256=(
                page_input.processed_image_sha256
            ),
            ocr_version=config.ocr_version,
        )

        evidence_line = EvidenceLine(
            evidence_line_id=line_id,
            page_number=page_input.page_number,
            reading_order=line_reading_order,
            text=line_text,
            mean_confidence=line_confidence,
            bounding_box=line_box,
            token_evidence_ids=tuple(
                token.evidence_id
                for token in ordered_tokens
            ),
        )

        evidence_lines.append(
            evidence_line
        )

    return tuple(evidence_lines)


# ---------------------------------------------------------
# 6. Convert raw OCR output into serializable rows
# ---------------------------------------------------------

def build_raw_ocr_rows(
    ocr_data: dict,
) -> list[dict]:
    rows = []
    row_count = len(ocr_data["text"])

    for index in range(row_count):
        row = {
            key: (
                value[index]
                if index < len(value)
                else None
            )
            for key, value in ocr_data.items()
        }

        rows.append(row)

    return rows


# ---------------------------------------------------------
# Final, active fallback adapter (notebook cell 43)
# ---------------------------------------------------------

def extract_tesseract_fallback_page(
    document_id: UUID,
    page_input: OCRPageInput,
    config: OCRConfig,
) -> tuple[OCRPageResult, list[dict]]:
    from PIL import Image

    image_path = Path(
        page_input.processed_image_path
    )

    if not image_path.is_file():
        raise FileNotFoundError(
            f"Processed page not found: "
            f"{image_path}"
        )

    actual_image_sha256 = (
        calculate_file_sha256(
            image_path
        )
    )

    if (
        actual_image_sha256
        != page_input.processed_image_sha256
    ):
        raise ValueError(
            "Processed page SHA-256 does not "
            "match the Phase 2 evidence record."
        )

    with Image.open(image_path) as image:
        image_width, image_height = (
            image.size
        )

    ocr_data = run_tesseract_page(
        image_path=image_path,
        config=config,
    )

    tokens = build_ocr_tokens(
        ocr_data=ocr_data,
        document_id=document_id,
        page_input=page_input,
        image_width=image_width,
        image_height=image_height,
        config=config,
    )

    evidence_lines = build_evidence_lines(
        tokens=tokens,
        document_id=document_id,
        page_input=page_input,
        config=config,
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
            actual_image_sha256
        ),
        evidence_image_path=image_path,
        evidence_image_sha256=(
            actual_image_sha256
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
        ocr_engine="tesseract-fallback",
        ocr_engine_version=(
            get_tesseract_version()
        ),
        ocr_configuration=(
            config.tesseract_config_string
        ),
        processed_at=datetime.now(
            timezone.utc
        ),
    )

    return (
        page_result,
        build_raw_ocr_rows(ocr_data),
    )
