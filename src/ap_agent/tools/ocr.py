"""Phase 3 (OCR) processing functions: the Phase 2 -> Phase 3 bridge,
document-level orchestration, PaddleOCR-primary/Tesseract-fallback
provider routing, persistence and the critical TOTAL-value completeness
guard.

Source: notebook cell 38 ("PHASE 3 — CELL 3") for `build_ocr_document_directory`;
cell 43 ("PHASE 3 — CORRECTION CELL 4C") for `phase_3_paddle_config` (a
config *instance*, not extracted — see `docs/modularisation_map.md` §1.3)
and `persist_routed_ocr_page`; cell 44 ("PHASE 3 — RUNTIME COMPATIBILITY
CORRECTION") for the final, active `extract_routed_ocr_page`; cell 47
("PHASE 3 — REPLACEMENT CELL 4E") for the TOTAL completeness guard
(`MONEY_PATTERN`, `TOTAL_PATTERN`, `TOTAL_VALUE_MISSING_REASON`,
`_box_coordinates`, `_append_reason`, `_safe_replace`,
`page_has_total_without_value`, `apply_total_completeness_guard`) and the
public `process_ocr_document` wrapper (active-definition table §2
`tools/ocr.py`).

**Persistence-order correction (task §7; decisions D-4/D-5).** The
notebook wrote `ocr_document_result.json`/`ocr_event.json`/
`ocr_page_result.json` *inside* `process_ocr_document@43`, then applied the
TOTAL completeness guard only in memory, in a thin wrapper
(`process_ocr_document@47`) — so a warped-invoice-style document could be
SUCCEEDED on disk and REVIEW_REQUIRED in the object the notebook actually
used (R-04, a known, documented defect). This module fixes the pipeline
order instead of reproducing the defect: `process_ocr_document` here runs
every page through the provider router, builds the pre-guard document
result **without persisting anything**, applies
`apply_total_completeness_guard` to that in-memory result, and only then
persists the guarded, final result. The persisted JSON and the returned
object are therefore always the same object — never a stale pre-guard
snapshot. `extract_routed_ocr_page`, `persist_routed_ocr_page` and the
guard functions themselves are otherwise byte-for-byte the notebook's
logic; only *when* persistence happens changed.

**Hidden-global correction (§5.1).** `extract_routed_ocr_page` and
`process_ocr_document` take the PaddleOCR `engine` (built once by
`ap_agent.adapters.paddleocr_adapter.create_engine`, per the task brief's
"one reusable provider instance per controlled process") as an explicit
parameter, never as a module global.
"""

from __future__ import annotations

import re
from dataclasses import fields, is_dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import UUID

from ap_agent.adapters.paddleocr_adapter import extract_paddle_ocr_page
from ap_agent.adapters.tesseract_adapter import extract_tesseract_fallback_page
from ap_agent.artifacts.filesystem import (
    calculate_file_sha256,
    save_pil_image_atomically,
    write_json_atomically,
    write_text_atomically,
)
from ap_agent.artifacts.serialization import (
    evidence_line_to_dict,
    make_json_compatible,
    ocr_document_result_to_dict,
    ocr_event_to_dict,
    ocr_page_result_to_dict,
    ocr_token_to_dict,
)
from ap_agent.config.settings import OCRConfig
from ap_agent.models.ocr import (
    OCRDocumentInput,
    OCRDocumentResult,
    OCREvent,
    OCRPageInput,
    OCRPageResult,
    OCRStatus,
)
from ap_agent.models.preprocessing import PreprocessingResult, PreprocessingStatus

__all__ = [
    "build_ocr_document_input",
    "build_ocr_document_directory",
    "extract_routed_ocr_page",
    "persist_routed_ocr_page",
    "MONEY_PATTERN",
    "TOTAL_PATTERN",
    "TOTAL_VALUE_MISSING_REASON",
    "page_has_total_without_value",
    "apply_total_completeness_guard",
    "process_ocr_document",
]


# ---------------------------------------------------------
# Phase 2 -> Phase 3 bridge (explicit and typed; §5.2 rule 2)
# ---------------------------------------------------------

def build_ocr_document_input(
    preprocessing_result: PreprocessingResult,
    preprocessing_version: str,
) -> OCRDocumentInput:
    """Build the Phase 2 -> Phase 3 bridge (notebook cell 48, §5.2 rule 2).

    `preprocessing_version` is not a field of `PreprocessingResult` (it is
    read from `PreprocessingConfig.preprocessing_version` in the notebook,
    a hidden global there); the caller passes it explicitly here — the
    same config instance used to produce `preprocessing_result`.
    """

    if preprocessing_result.status is PreprocessingStatus.FAILED:
        raise ValueError(
            "Cannot build an OCR input from a failed preprocessing result."
        )

    return OCRDocumentInput(
        batch_id=preprocessing_result.batch_id,
        document_id=preprocessing_result.document_id,
        source_document_sha256=preprocessing_result.source_sha256,
        preprocessing_version=preprocessing_version,
        pages=tuple(
            OCRPageInput(
                page_number=page.page_number,
                processed_image_path=page.processed_image_path,
                processed_image_sha256=page.processed_image_sha256,
                preprocessing_review_required=page.quality.review_required,
            )
            for page in preprocessing_result.pages
        ),
    )


# ---------------------------------------------------------
# 3. Artifact-directory construction (notebook cell 38)
# ---------------------------------------------------------

def build_ocr_document_directory(
    document_input: OCRDocumentInput,
    config: OCRConfig,
) -> Path:
    return (
        config.artifact_root
        / str(document_input.batch_id)
        / str(document_input.document_id)
        / config.ocr_version
    )


# ---------------------------------------------------------
# 4. Provider-routing function (final, active: notebook cell 44)
# ---------------------------------------------------------

def extract_routed_ocr_page(
    document_id: UUID,
    page_input: OCRPageInput,
    config: OCRConfig,
    evidence_image_destination: Path,
    engine: Any,
    engine_version: str,
) -> tuple[OCRPageResult, dict]:
    try:
        page_result, raw_payload = (
            extract_paddle_ocr_page(
                document_id=document_id,
                page_input=page_input,
                config=config,
                evidence_image_destination=(
                    evidence_image_destination
                ),
                engine=engine,
                engine_version=engine_version,
            )
        )

        return (
            page_result,
            {
                "selected_provider": (
                    "paddleocr"
                ),
                "fallback_used": False,
                "payload": raw_payload,
            },
        )

    except Exception as primary_error:
        try:
            fallback_result, fallback_raw = (
                extract_tesseract_fallback_page(
                    document_id=document_id,
                    page_input=page_input,
                    config=config,
                )
            )

            # Preserve a byte-stable PNG copy of the
            # exact image used for fallback coordinates.
            from PIL import Image

            with Image.open(
                fallback_result.evidence_image_path
            ) as fallback_image:
                save_pil_image_atomically(
                    image=(
                        fallback_image
                        .convert("RGB")
                    ),
                    destination=(
                        evidence_image_destination
                    ),
                )

            fallback_evidence_sha256 = (
                calculate_file_sha256(
                    evidence_image_destination
                )
            )

            fallback_reasons = tuple(
                dict.fromkeys(
                    list(
                        fallback_result
                        .review_reasons
                    )
                    + [
                        "PRIMARY_PROVIDER_FAILED"
                    ]
                )
            )

            fallback_result = replace(
                fallback_result,
                evidence_image_path=(
                    evidence_image_destination
                ),
                evidence_image_sha256=(
                    fallback_evidence_sha256
                ),
                status=(
                    OCRStatus.REVIEW_REQUIRED
                ),
                review_reasons=(
                    fallback_reasons
                ),
            )

            return (
                fallback_result,
                {
                    "selected_provider": (
                        "tesseract-fallback"
                    ),
                    "fallback_used": True,
                    "primary_provider": (
                        "paddleocr"
                    ),
                    "primary_error": (
                        f"{type(primary_error).__name__}: "
                        f"{primary_error}"
                    ),
                    "payload": fallback_raw,
                },
            )

        except Exception as fallback_error:
            raise RuntimeError(
                "Both OCR providers failed. "
                "PaddleOCR error: "
                f"{type(primary_error).__name__}: "
                f"{primary_error}. "
                "Tesseract error: "
                f"{type(fallback_error).__name__}: "
                f"{fallback_error}."
            ) from fallback_error


# ---------------------------------------------------------
# 5. Persist one routed OCR page (notebook cell 43)
# ---------------------------------------------------------

def persist_routed_ocr_page(
    document_directory: Path,
    page_result: OCRPageResult,
    raw_provider_output: dict,
) -> None:
    page_directory = (
        document_directory
        / f"page_"
          f"{page_result.page_number:03d}"
    )

    write_text_atomically(
        destination=(
            page_directory
            / "page_text.txt"
        ),
        text=page_result.page_text,
    )

    write_json_atomically(
        destination=(
            page_directory
            / "tokens.json"
        ),
        payload={
            "page_number": (
                page_result.page_number
            ),
            "coordinate_image": str(
                page_result
                .evidence_image_path
            ),
            "token_count": len(
                page_result.tokens
            ),
            "tokens": [
                ocr_token_to_dict(token)
                for token
                in page_result.tokens
            ],
        },
    )

    write_json_atomically(
        destination=(
            page_directory
            / "evidence_lines.json"
        ),
        payload={
            "page_number": (
                page_result.page_number
            ),
            "coordinate_image": str(
                page_result
                .evidence_image_path
            ),
            "evidence_line_count": len(
                page_result.evidence_lines
            ),
            "evidence_lines": [
                evidence_line_to_dict(line)
                for line
                in page_result.evidence_lines
            ],
        },
    )

    write_json_atomically(
        destination=(
            page_directory
            / "ocr_page_result.json"
        ),
        payload=ocr_page_result_to_dict(
            page_result
        ),
    )

    write_json_atomically(
        destination=(
            page_directory
            / "raw_provider_output.json"
        ),
        payload=make_json_compatible(
            raw_provider_output
        ),
    )


# ---------------------------------------------------------
# 6. Critical TOTAL-value completeness guard (notebook cell 47)
# ---------------------------------------------------------

MONEY_PATTERN = re.compile(
    r"(?<!\d)[$£€]?\s*-?\d[\d,\s]*[.,]\d{2}(?!\d)"
)

TOTAL_PATTERN = re.compile(
    r"(?<!SUB)\bTOTAL\b",
    flags=re.IGNORECASE,
)

TOTAL_VALUE_MISSING_REASON = (
    "CRITICAL_TOTAL_VALUE_MISSING"
)


def _box_coordinates(box: Any) -> tuple[int, int, int, int]:
    x = int(getattr(box, "x", getattr(box, "left", 0)))
    y = int(getattr(box, "y", getattr(box, "top", 0)))

    width = getattr(box, "width", None)
    height = getattr(box, "height", None)

    if width is None:
        width = int(getattr(box, "right", x)) - x

    if height is None:
        height = int(getattr(box, "bottom", y)) - y

    return x, y, int(width), int(height)


def _append_reason(existing: Any, reason: str) -> Any:
    current = list(existing or [])

    if reason not in current:
        current.append(reason)

    if isinstance(existing, tuple):
        return tuple(current)

    return current


def _safe_replace(instance: Any, **updates: Any) -> Any:
    if not is_dataclass(instance):
        for name, value in updates.items():
            if hasattr(instance, name):
                setattr(instance, name, value)
        return instance

    available_fields = {
        field.name for field in fields(instance)
    }

    valid_updates = {
        name: value
        for name, value in updates.items()
        if name in available_fields
    }

    return replace(instance, **valid_updates)


def page_has_total_without_value(page_result: Any) -> bool:
    tokens = list(getattr(page_result, "tokens", []))

    total_tokens = [
        token
        for token in tokens
        if TOTAL_PATTERN.search(
            str(getattr(token, "text", ""))
        )
    ]

    if not total_tokens:
        return False

    grand_total_token = max(
        total_tokens,
        key=lambda token: _box_coordinates(
            token.bounding_box
        )[1],
    )

    label_text = str(
        getattr(grand_total_token, "text", "")
    )

    if MONEY_PATTERN.search(label_text):
        return False

    label_x, label_y, label_width, label_height = (
        _box_coordinates(
            grand_total_token.bounding_box
        )
    )

    label_right = label_x + label_width
    label_center_y = label_y + label_height / 2

    for token in tokens:
        if token is grand_total_token:
            continue

        token_text = str(getattr(token, "text", ""))

        if not MONEY_PATTERN.search(token_text):
            continue

        token_x, token_y, token_width, token_height = (
            _box_coordinates(token.bounding_box)
        )

        token_center_y = token_y + token_height / 2

        same_line = abs(
            token_center_y - label_center_y
        ) <= max(
            label_height,
            token_height,
        ) * 1.25

        positioned_after_label = (
            token_x >= label_right - 10
        )

        if same_line and positioned_after_label:
            return False

    return True


def apply_total_completeness_guard(
    document_result: Any,
) -> Any:

    original_pages = list(
        getattr(document_result, "pages", [])
    )

    original_status = getattr(
        document_result,
        "status",
    )

    existing_event = getattr(
        document_result,
        "event",
        None,
    )

    updated_pages = []
    guard_triggered = False

    document_requires_review = bool(
        getattr(
            document_result,
            "review_required",
            False,
        )
        or getattr(
            document_result,
            "requires_review",
            False,
        )
        or getattr(
            existing_event,
            "review_required",
            False,
        )
        or (
            original_status
            != OCRStatus.SUCCEEDED
        )
    )

    for page_result in original_pages:
        missing_total_value = (
            page_has_total_without_value(
                page_result
            )
        )

        if not missing_total_value:
            updated_pages.append(
                page_result
            )
            continue

        guard_triggered = True
        document_requires_review = True

        page_reasons = _append_reason(
            getattr(
                page_result,
                "review_reasons",
                [],
            ),
            TOTAL_VALUE_MISSING_REASON,
        )

        page_event = getattr(
            page_result,
            "event",
            None,
        )

        if page_event is not None:
            page_event = _safe_replace(
                page_event,
                status=(
                    OCRStatus.REVIEW_REQUIRED
                ),
                review_required=True,
                requires_review=True,
            )

        updated_page = _safe_replace(
            page_result,
            status=OCRStatus.REVIEW_REQUIRED,
            review_required=True,
            requires_review=True,
            review_reasons=page_reasons,
            event=page_event,
        )

        updated_pages.append(
            updated_page
        )

    if isinstance(
        getattr(
            document_result,
            "pages",
            [],
        ),
        tuple,
    ):
        updated_pages = tuple(
            updated_pages
        )

    document_reasons = getattr(
        document_result,
        "review_reasons",
        [],
    )

    if guard_triggered:
        document_reasons = _append_reason(
            document_reasons,
            TOTAL_VALUE_MISSING_REASON,
        )

    # A genuine technical failure must remain FAILED.
    if original_status == OCRStatus.FAILED:
        final_status = OCRStatus.FAILED

    elif document_requires_review:
        final_status = (
            OCRStatus.REVIEW_REQUIRED
        )

    else:
        final_status = original_status

    document_event = existing_event

    if document_event is not None:
        document_event = _safe_replace(
            document_event,
            status=final_status,
            review_required=(
                document_requires_review
            ),
            requires_review=(
                document_requires_review
            ),
        )

    return _safe_replace(
        document_result,
        pages=updated_pages,
        status=final_status,
        review_required=(
            document_requires_review
        ),
        requires_review=(
            document_requires_review
        ),
        review_reasons=document_reasons,
        event=document_event,
    )


# ---------------------------------------------------------
# 7. Document-level orchestration (corrected persistence order)
# ---------------------------------------------------------

def _run_ocr_pages(
    document_input: OCRDocumentInput,
    config: OCRConfig,
    engine: Any,
    engine_version: str,
    document_directory: Path,
) -> tuple[OCRDocumentResult, dict[int, dict]]:
    """Notebook cell 43's page-processing and status-determination logic,
    verbatim, minus in-loop persistence (deferred to after the guard)."""

    processed_pages: list[OCRPageResult] = []
    raw_outputs_by_page: dict[int, dict] = {}

    try:
        if not document_input.pages:
            raise ValueError(
                "OCR input contains no pages."
            )

        page_numbers = [
            page.page_number
            for page in document_input.pages
        ]

        if len(page_numbers) != len(
            set(page_numbers)
        ):
            raise ValueError(
                "OCR input contains duplicate "
                "page numbers."
            )

        if any(
            page_number < 1
            for page_number in page_numbers
        ):
            raise ValueError(
                "OCR page numbers must begin at 1."
            )

        ordered_pages = sorted(
            document_input.pages,
            key=lambda page: page.page_number,
        )

        for page_input in ordered_pages:
            page_directory = (
                document_directory
                / f"page_"
                  f"{page_input.page_number:03d}"
            )

            evidence_image_destination = (
                page_directory
                / "evidence_image.png"
            )

            page_result, raw_output = (
                extract_routed_ocr_page(
                    document_id=(
                        document_input.document_id
                    ),
                    page_input=page_input,
                    config=config,
                    evidence_image_destination=(
                        evidence_image_destination
                    ),
                    engine=engine,
                    engine_version=engine_version,
                )
            )

            processed_pages.append(
                page_result
            )
            raw_outputs_by_page[
                page_input.page_number
            ] = raw_output

        review_pages = [
            page
            for page in processed_pages
            if (
                page.status
                == OCRStatus.REVIEW_REQUIRED
            )
        ]

        if review_pages:
            final_status = (
                OCRStatus.REVIEW_REQUIRED
            )

            message = (
                "OCR completed, but "
                f"{len(review_pages)} of "
                f"{len(processed_pages)} pages "
                "require review."
            )
        else:
            final_status = (
                OCRStatus.SUCCEEDED
            )

            message = (
                "OCR completed successfully "
                f"for {len(processed_pages)} "
                "pages."
            )

        event = OCREvent(
            event_type="OCR_EXTRACTION",
            status=final_status,
            batch_id=document_input.batch_id,
            document_id=(
                document_input.document_id
            ),
            occurred_at=datetime.now(
                timezone.utc
            ),
            message=message,
            review_required=(
                final_status
                != OCRStatus.SUCCEEDED
            ),
        )

        result = OCRDocumentResult(
            batch_id=document_input.batch_id,
            document_id=(
                document_input.document_id
            ),
            source_document_sha256=(
                document_input
                .source_document_sha256
            ),
            preprocessing_version=(
                document_input
                .preprocessing_version
            ),
            status=final_status,
            pages=tuple(processed_pages),
            event=event,
            errors=(),
        )

        return result, raw_outputs_by_page

    except Exception as exc:
        error_message = (
            f"{type(exc).__name__}: {exc}"
        )

        failure_event = OCREvent(
            event_type="OCR_EXTRACTION",
            status=OCRStatus.FAILED,
            batch_id=document_input.batch_id,
            document_id=(
                document_input.document_id
            ),
            occurred_at=datetime.now(
                timezone.utc
            ),
            message=error_message,
            review_required=True,
        )

        failure_result = OCRDocumentResult(
            batch_id=document_input.batch_id,
            document_id=(
                document_input.document_id
            ),
            source_document_sha256=(
                document_input
                .source_document_sha256
            ),
            preprocessing_version=(
                document_input
                .preprocessing_version
            ),
            status=OCRStatus.FAILED,
            pages=tuple(processed_pages),
            event=failure_event,
            errors=(error_message,),
        )

        return failure_result, raw_outputs_by_page


def _persist_ocr_document_result(
    document_directory: Path,
    result: OCRDocumentResult,
    raw_outputs_by_page: dict[int, dict],
) -> None:
    """Persist the final, guarded result — never a pre-guard snapshot."""

    for page_result in result.pages:
        persist_routed_ocr_page(
            document_directory=document_directory,
            page_result=page_result,
            raw_provider_output=raw_outputs_by_page.get(
                page_result.page_number, {}
            ),
        )

    write_json_atomically(
        destination=(
            document_directory
            / "ocr_document_result.json"
        ),
        payload=ocr_document_result_to_dict(result),
    )

    write_json_atomically(
        destination=(
            document_directory
            / "ocr_event.json"
        ),
        payload=ocr_event_to_dict(result.event),
    )


def process_ocr_document(
    document_input: OCRDocumentInput,
    config: OCRConfig,
    engine: Any,
    engine_version: str | None = None,
) -> OCRDocumentResult:
    """Public Phase 3 entry point.

    Pipeline (task §7, corrected persistence order): provider result ->
    normalized evidence -> confidence routing -> TOTAL completeness guard
    -> final synchronized result -> artifact persistence. The object
    returned here is always exactly what was persisted to disk.
    """

    if engine_version is None:
        from ap_agent.adapters.paddleocr_adapter import get_paddleocr_version

        engine_version = get_paddleocr_version()

    document_directory = build_ocr_document_directory(
        document_input=document_input,
        config=config,
    )

    pre_guard_result, raw_outputs_by_page = _run_ocr_pages(
        document_input=document_input,
        config=config,
        engine=engine,
        engine_version=engine_version,
        document_directory=document_directory,
    )

    final_result = apply_total_completeness_guard(pre_guard_result)

    _persist_ocr_document_result(
        document_directory=document_directory,
        result=final_result,
        raw_outputs_by_page=raw_outputs_by_page,
    )

    return final_result
