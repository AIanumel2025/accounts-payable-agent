"""Phase 2 (preprocessing) processing functions.

Source: notebook cell 26 ("PHASE 2 — CELL 1") for image loading, PDF
rendering, deskew and enhancement utilities; cell 28 ("PHASE 2 — CELL 2")
for `build_preprocessing_directory` and the `preprocess_document`
orchestrator; cell 29 ("PHASE 2 — QUALITY-GATE CORRECTION") for the final,
active `assess_page_quality` (`docs/modularisation_map.md` §2, §3.1). The
cell-26 `assess_page_quality` is SUPERSEDED by cell 29 and is not extracted
(§3.1: "Extract @29 only").

`calculate_file_sha256`, `write_json_atomically` and `save_png_atomically`
are Phase 2/3 artifact writers and live in `ap_agent.artifacts.filesystem`
(imported here, not redefined). `page_quality_to_dict`,
`preprocessing_event_to_dict` and `preprocessed_page_to_dict` live in
`ap_agent.artifacts.serialization`.

No heavy or optional dependency (`cv2`, `numpy`, `PIL`, `pymupdf`) is
imported at module level: each is imported lazily inside the function that
needs it (CLAUDE.md). `render_pdf_pages` uses `import pymupdf` directly,
not the deprecated `import fitz` alias the notebook used.

Phase 1 → Phase 2 bridge (deviation from the notebook, decision D-6):
`build_preprocessing_input` constructs the modular `PreprocessingInput` from
a Phase 1 `IngestionResult`, using the **preserved artifact** (`stored_path`)
and the **Phase 1 registered SHA-256** (`identity.sha256`) rather than the
notebook's re-read of the originally uploaded `/content` path (§5.2 rule 1
of the modularisation map, superseded by D-6). `preprocess_document` then
re-verifies that hash against the artifact's current bytes and fails closed
on any mismatch — this check is unchanged, verbatim notebook behaviour; only
the bridge that decides which path and hash to feed it is new.
"""

from __future__ import annotations

import math
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

from ap_agent.artifacts.filesystem import (
    calculate_file_sha256,
    save_png_atomically,
    write_json_atomically,
)
from ap_agent.artifacts.serialization import (
    page_quality_to_dict,
    preprocessed_page_to_dict,
    preprocessing_event_to_dict,
)
from ap_agent.config.settings import PreprocessingConfig
from ap_agent.models.ingestion import IngestionResult
from ap_agent.models.preprocessing import (
    PageQuality,
    PreprocessedPage,
    PreprocessingEvent,
    PreprocessingInput,
    PreprocessingResult,
    PreprocessingStatus,
)

if TYPE_CHECKING:
    import numpy as np

__all__ = [
    "build_preprocessing_input",
    "load_image_with_orientation",
    "render_pdf_pages",
    "load_document_pages",
    "detect_skew_angle",
    "rotate_without_cropping",
    "deskew_image",
    "enhance_page_image",
    "assess_page_quality",
    "build_preprocessing_directory",
    "preprocess_document",
]


def build_preprocessing_input(
    ingestion_result: IngestionResult,
) -> PreprocessingInput:
    """Build the explicit, typed Phase 1 -> Phase 2 bridge.

    Consumes the immutable artifact `preserve_original_document` (Phase 1)
    already wrote to content-addressed storage, and the SHA-256 Phase 1
    registered for it (`identity.sha256`) — never the raw path the document
    was originally uploaded at. This is decision D-6: it supersedes the
    notebook's own Phase 1 -> Phase 2 bridge (cell 31), which re-read the
    uploaded `/content` file directly.
    """

    return PreprocessingInput(
        batch_id=ingestion_result.identity.batch_id,
        document_id=ingestion_result.identity.document_id,
        source_path=ingestion_result.stored_path,
        source_sha256=ingestion_result.identity.sha256,
    )


# ---------------------------------------------------------
# 5. Image loading and PDF rendering
# ---------------------------------------------------------

def load_image_with_orientation(
    image_path: Path,
) -> "np.ndarray":
    """
    Load an image, apply its EXIF orientation and return BGR pixels.
    """

    import cv2
    import numpy as np
    from PIL import Image, ImageOps

    with Image.open(image_path) as pil_image:
        corrected_image = ImageOps.exif_transpose(pil_image)
        rgb_image = corrected_image.convert("RGB")
        rgb_array = np.asarray(rgb_image)

    return cv2.cvtColor(rgb_array, cv2.COLOR_RGB2BGR)


def render_pdf_pages(
    pdf_path: Path,
    dpi: int,
) -> list["np.ndarray"]:
    """
    Render every PDF page into a BGR image.
    """

    import cv2
    import numpy as np
    import pymupdf

    rendered_pages = []
    zoom = dpi / 72.0
    transformation = pymupdf.Matrix(zoom, zoom)

    with pymupdf.open(pdf_path) as pdf_document:
        if pdf_document.page_count == 0:
            raise ValueError("The PDF contains no pages.")

        for pdf_page in pdf_document:
            pixmap = pdf_page.get_pixmap(
                matrix=transformation,
                alpha=False,
            )

            page_array = np.frombuffer(
                pixmap.samples,
                dtype=np.uint8,
            ).reshape(
                pixmap.height,
                pixmap.width,
                pixmap.n,
            )

            if pixmap.n == 3:
                bgr_page = cv2.cvtColor(
                    page_array,
                    cv2.COLOR_RGB2BGR,
                )
            elif pixmap.n == 4:
                bgr_page = cv2.cvtColor(
                    page_array,
                    cv2.COLOR_RGBA2BGR,
                )
            else:
                raise ValueError(
                    f"Unsupported PDF channel count: {pixmap.n}"
                )

            rendered_pages.append(bgr_page)

    return rendered_pages


def load_document_pages(
    source_path: Path,
    config: PreprocessingConfig,
) -> list["np.ndarray"]:
    suffix = source_path.suffix.lower()

    if suffix == ".pdf":
        return render_pdf_pages(
            pdf_path=source_path,
            dpi=config.pdf_dpi,
        )

    if suffix in {".png", ".jpg", ".jpeg", ".tif", ".tiff"}:
        return [
            load_image_with_orientation(source_path)
        ]

    raise ValueError(
        f"Unsupported preprocessing format: {suffix}"
    )


# ---------------------------------------------------------
# 6. Deskew utilities
# ---------------------------------------------------------

def detect_skew_angle(image: "np.ndarray") -> float:
    """
    Estimate document skew from foreground pixels.

    A positive return value means the correction should rotate
    counter-clockwise. A negative value means clockwise.
    """

    import cv2
    import numpy as np

    grayscale = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)

    inverted_binary = cv2.threshold(
        grayscale,
        0,
        255,
        cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU,
    )[1]

    coordinates = np.column_stack(
        np.where(inverted_binary > 0)
    )

    if len(coordinates) < 100:
        return 0.0

    raw_angle = cv2.minAreaRect(coordinates)[-1]

    if raw_angle < -45:
        correction_angle = -(90 + raw_angle)
    else:
        correction_angle = -raw_angle

    if abs(correction_angle) > 45:
        return 0.0

    return float(correction_angle)


def rotate_without_cropping(
    image: "np.ndarray",
    angle: float,
) -> "np.ndarray":
    """
    Rotate an image while expanding the canvas to avoid cropping.
    """

    import cv2

    height, width = image.shape[:2]
    centre = (width / 2.0, height / 2.0)

    rotation_matrix = cv2.getRotationMatrix2D(
        centre,
        angle,
        1.0,
    )

    cosine = abs(rotation_matrix[0, 0])
    sine = abs(rotation_matrix[0, 1])

    expanded_width = int(
        (height * sine) + (width * cosine)
    )
    expanded_height = int(
        (height * cosine) + (width * sine)
    )

    rotation_matrix[0, 2] += (
        expanded_width / 2.0
    ) - centre[0]

    rotation_matrix[1, 2] += (
        expanded_height / 2.0
    ) - centre[1]

    return cv2.warpAffine(
        image,
        rotation_matrix,
        (expanded_width, expanded_height),
        flags=cv2.INTER_CUBIC,
        borderMode=cv2.BORDER_CONSTANT,
        borderValue=(255, 255, 255),
    )


def deskew_image(
    image: "np.ndarray",
    maximum_angle: float,
) -> tuple["np.ndarray", float]:
    angle = detect_skew_angle(image)

    if math.isclose(angle, 0.0, abs_tol=0.05):
        return image.copy(), 0.0

    if abs(angle) > maximum_angle:
        # Large angles may indicate perspective distortion rather
        # than ordinary document skew. Preserve the image and flag it.
        return image.copy(), angle

    return rotate_without_cropping(image, angle), angle


# ---------------------------------------------------------
# 7. Enhancement and quality assessment
# ---------------------------------------------------------

def enhance_page_image(
    image: "np.ndarray",
    config: PreprocessingConfig,
) -> "np.ndarray":
    import cv2

    grayscale = cv2.cvtColor(
        image,
        cv2.COLOR_BGR2GRAY,
    )

    enhanced = grayscale

    if config.enable_denoising:
        enhanced = cv2.fastNlMeansDenoising(
            enhanced,
            None,
            h=config.denoising_strength,
            templateWindowSize=7,
            searchWindowSize=21,
        )

    if config.enable_clahe:
        clahe = cv2.createCLAHE(
            clipLimit=config.clahe_clip_limit,
            tileGridSize=config.clahe_tile_grid_size,
        )
        enhanced = clahe.apply(enhanced)

    return enhanced


def assess_page_quality(
    image: "np.ndarray",
    page_number: int,
    detected_skew_angle: float,
    config: PreprocessingConfig,
) -> PageQuality:
    """Final, active quality gate (notebook cell 29).

    Supersedes the cell-26 definition: `LOW_RESOLUTION` uses fixed 500x700
    thresholds (not `config.minimum_width`/`minimum_height`, §3.1/R-09 —
    preserved as-is, not "fixed"), and `POSSIBLY_BLANK_OR_OVEREXPOSED` is
    foreground-aware (only fires when almost no dark pixels are present),
    replacing the old unconditional `TOO_BRIGHT` flag.
    """

    import cv2
    import numpy as np

    if image.ndim == 3:
        grayscale = cv2.cvtColor(
            image,
            cv2.COLOR_BGR2GRAY,
        )
    else:
        grayscale = image

    height, width = grayscale.shape[:2]

    brightness = float(np.mean(grayscale))
    contrast = float(np.std(grayscale))

    blur = float(
        cv2.Laplacian(
            grayscale,
            cv2.CV_64F,
        ).var()
    )

    # Percentage of pixels containing likely foreground content.
    # This prevents legitimate white invoice backgrounds from
    # automatically being classified as TOO_BRIGHT.
    dark_pixel_ratio = float(
        np.mean(grayscale < 200)
    )

    flags = []

    # Treat resolution as inadequate only when the page is
    # materially smaller than a typical screen-resolution document.
    if width < 500 or height < 700:
        flags.append("LOW_RESOLUTION")

    if brightness < config.minimum_brightness:
        flags.append("TOO_DARK")

    # High mean brightness is only a problem when almost no
    # foreground content is present.
    if (
        brightness > config.maximum_brightness
        and dark_pixel_ratio < 0.005
    ):
        flags.append("POSSIBLY_BLANK_OR_OVEREXPOSED")

    if contrast < config.minimum_contrast_score:
        flags.append("LOW_CONTRAST")

    if blur < config.minimum_blur_score:
        flags.append("POSSIBLE_BLUR")

    if (
        abs(detected_skew_angle)
        > config.maximum_deskew_angle
    ):
        flags.append(
            "EXCESSIVE_SKEW_OR_PERSPECTIVE"
        )

    return PageQuality(
        page_number=page_number,
        width=width,
        height=height,
        brightness_score=round(brightness, 2),
        contrast_score=round(contrast, 2),
        blur_score=round(blur, 2),
        detected_skew_angle=round(
            detected_skew_angle,
            2,
        ),
        quality_flags=tuple(flags),
        review_required=bool(flags),
    )


# ---------------------------------------------------------
# Orchestration
# ---------------------------------------------------------

def build_preprocessing_directory(
    preprocessing_input: PreprocessingInput,
    config: PreprocessingConfig,
) -> Path:
    return (
        config.artifact_root
        / str(preprocessing_input.batch_id)
        / str(preprocessing_input.document_id)
        / config.preprocessing_version
    )


def preprocess_document(
    preprocessing_input: PreprocessingInput,
    config: PreprocessingConfig,
) -> PreprocessingResult:
    """
    Preprocess one accepted invoice document.

    The function:

    1. Verifies the source still matches its Phase 1 SHA-256.
    2. Loads an image or renders every PDF page.
    3. Preserves the unmodified rendered page.
    4. Detects and corrects ordinary skew.
    5. Applies deterministic enhancement.
    6. Calculates page-level quality metrics.
    7. Preserves processed artifacts and metadata.
    8. Returns a structured, fail-closed result.
    """

    source_path = Path(
        preprocessing_input.source_path
    )

    output_directory = build_preprocessing_directory(
        preprocessing_input=preprocessing_input,
        config=config,
    )

    pages_directory = output_directory / "pages"
    event_path = output_directory / "preprocessing_event.json"
    result_path = output_directory / "preprocessing_result.json"

    try:
        # -------------------------------------------------
        # Validate input and evidence identity
        # -------------------------------------------------

        if not source_path.is_file():
            raise FileNotFoundError(
                f"Source document not found: {source_path}"
            )

        actual_source_sha256 = calculate_file_sha256(
            source_path
        )

        if (
            actual_source_sha256
            != preprocessing_input.source_sha256
        ):
            raise ValueError(
                "Source SHA-256 no longer matches the "
                "Phase 1 ingestion identity."
            )

        # -------------------------------------------------
        # Load image or render PDF pages
        # -------------------------------------------------

        rendered_pages = load_document_pages(
            source_path=source_path,
            config=config,
        )

        if not rendered_pages:
            raise ValueError(
                "Document produced no renderable pages."
            )

        processed_pages = []

        # -------------------------------------------------
        # Process each page independently
        # -------------------------------------------------

        for page_number, rendered_page in enumerate(
            rendered_pages,
            start=1,
        ):
            page_directory = (
                pages_directory
                / f"page_{page_number:03d}"
            )

            original_render_path = (
                page_directory
                / "original_render.png"
            )

            processed_image_path = (
                page_directory
                / "processed.png"
            )

            page_metadata_path = (
                page_directory
                / "quality.json"
            )

            # Preserve the page before preprocessing.
            save_png_atomically(
                image=rendered_page,
                destination=original_render_path,
            )

            # Detect and correct ordinary skew.
            if config.enable_deskew:
                deskewed_page, detected_angle = (
                    deskew_image(
                        image=rendered_page,
                        maximum_angle=(
                            config.maximum_deskew_angle
                        ),
                    )
                )
            else:
                deskewed_page = rendered_page.copy()
                detected_angle = 0.0

            # Produce the OCR-ready derivative.
            enhanced_page = enhance_page_image(
                image=deskewed_page,
                config=config,
            )

            save_png_atomically(
                image=enhanced_page,
                destination=processed_image_path,
            )

            # Assess the final OCR-ready image.
            quality = assess_page_quality(
                image=enhanced_page,
                page_number=page_number,
                detected_skew_angle=detected_angle,
                config=config,
            )

            preprocessed_page = PreprocessedPage(
                page_number=page_number,
                original_render_path=(
                    original_render_path
                ),
                processed_image_path=(
                    processed_image_path
                ),
                original_render_sha256=(
                    calculate_file_sha256(
                        original_render_path
                    )
                ),
                processed_image_sha256=(
                    calculate_file_sha256(
                        processed_image_path
                    )
                ),
                quality=quality,
            )

            processed_pages.append(
                preprocessed_page
            )

            write_json_atomically(
                destination=page_metadata_path,
                payload=page_quality_to_dict(
                    quality
                ),
            )

        # -------------------------------------------------
        # Determine document-level status
        # -------------------------------------------------

        review_required = any(
            page.quality.review_required
            for page in processed_pages
        )

        if review_required:
            final_status = (
                PreprocessingStatus.REVIEW_REQUIRED
            )
            message = (
                "Preprocessing completed, but at least "
                "one page failed a quality threshold."
            )
        else:
            final_status = (
                PreprocessingStatus.SUCCEEDED
            )
            message = (
                "Preprocessing completed successfully."
            )

        event = PreprocessingEvent(
            event_type="PREPROCESSING",
            status=final_status,
            batch_id=preprocessing_input.batch_id,
            document_id=preprocessing_input.document_id,
            occurred_at=datetime.now(timezone.utc),
            message=message,
            review_required=review_required,
        )

        result = PreprocessingResult(
            batch_id=preprocessing_input.batch_id,
            document_id=preprocessing_input.document_id,
            source_path=source_path,
            source_sha256=actual_source_sha256,
            status=final_status,
            pages=tuple(processed_pages),
            event=event,
            errors=(),
        )

        result_payload = {
            "batch_id": str(result.batch_id),
            "document_id": str(result.document_id),
            "source_path": str(result.source_path),
            "source_sha256": result.source_sha256,
            "status": result.status.value,
            "page_count": len(result.pages),
            "pages": [
                preprocessed_page_to_dict(page)
                for page in result.pages
            ],
            "event": preprocessing_event_to_dict(
                result.event
            ),
            "errors": list(result.errors),
            "preprocessing_version": (
                config.preprocessing_version
            ),
        }

        write_json_atomically(
            destination=result_path,
            payload=result_payload,
        )

        write_json_atomically(
            destination=event_path,
            payload=preprocessing_event_to_dict(
                event
            ),
        )

        return result

    except Exception as exc:
        # -------------------------------------------------
        # Fail closed
        # -------------------------------------------------

        error_message = (
            f"{type(exc).__name__}: {exc}"
        )

        failure_event = PreprocessingEvent(
            event_type="PREPROCESSING",
            status=PreprocessingStatus.FAILED,
            batch_id=preprocessing_input.batch_id,
            document_id=preprocessing_input.document_id,
            occurred_at=datetime.now(timezone.utc),
            message=error_message,
            review_required=True,
        )

        failure_result = PreprocessingResult(
            batch_id=preprocessing_input.batch_id,
            document_id=preprocessing_input.document_id,
            source_path=source_path,
            source_sha256=(
                preprocessing_input.source_sha256
            ),
            status=PreprocessingStatus.FAILED,
            pages=(),
            event=failure_event,
            errors=(error_message,),
        )

        failure_payload = {
            "batch_id": str(
                failure_result.batch_id
            ),
            "document_id": str(
                failure_result.document_id
            ),
            "source_path": str(
                failure_result.source_path
            ),
            "source_sha256": (
                failure_result.source_sha256
            ),
            "status": (
                failure_result.status.value
            ),
            "page_count": 0,
            "pages": [],
            "event": preprocessing_event_to_dict(
                failure_event
            ),
            "errors": list(
                failure_result.errors
            ),
            "preprocessing_version": (
                config.preprocessing_version
            ),
        }

        write_json_atomically(
            destination=result_path,
            payload=failure_payload,
        )

        write_json_atomically(
            destination=event_path,
            payload=preprocessing_event_to_dict(
                failure_event
            ),
        )

        return failure_result
