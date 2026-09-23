"""Unit tests for the Phase 4 typed bridge, artifact persistence, the
artifact-collision guard, the invalid-OCR-artifact-hash fail-closed path,
JSON-safe conversion and cross-document artifact isolation (M5, task
§3/§12/§14/§17). Synthetic OCR/preprocessing contracts only.
"""

import json
from dataclasses import replace
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest

from ap_agent.artifacts.filesystem import write_json_atomically, write_json_safe_atomically
from ap_agent.artifacts.serialization import convert_to_json_safe, ocr_document_result_to_dict
from ap_agent.config.settings import NormalizationConfig, OCRConfig
from ap_agent.exceptions import NormalizationIntegrityError
from ap_agent.models.normalization import (
    EvidenceReference,
    EvidenceReferenceType,
    ExtractionMethod,
    InvoiceFieldName,
    NormalizationEvent,
    NormalizationInput,
    NormalizationResult,
    NormalizationStatus,
    NormalizedInvoiceField,
    NormalizedInvoiceRecord,
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
from ap_agent.models.preprocessing import (
    PageQuality,
    PreprocessedPage,
    PreprocessingEvent,
    PreprocessingResult,
    PreprocessingStatus,
)
from ap_agent.tools.normalization import (
    build_normalization_input,
    normalize_invoice_document,
    persist_normalization_result,
    phase_4_document_directory,
    verify_ocr_result_artifact_integrity,
)
from ap_agent.tools.ocr import build_ocr_document_directory

pytestmark = pytest.mark.unit


# --- synthetic OCR contract builders ----------------------------------------


def _box(x, y, w, h):
    return BoundingBox(x=x, y=y, width=w, height=h)


def _token(text, x, y, w, h, order):
    return OCRToken(
        evidence_id=uuid4(),
        page_number=1,
        reading_order=order,
        text=text,
        confidence=95.0,
        bounding_box=_box(x, y, w, h),
        block_number=1,
        paragraph_number=1,
        line_number=1,
        word_number=order,
    )


def _line(text, x, y, w, h, order, tokens=()):
    return EvidenceLine(
        evidence_line_id=uuid4(),
        page_number=1,
        reading_order=order,
        text=text,
        mean_confidence=95.0,
        bounding_box=_box(x, y, w, h),
        token_evidence_ids=tuple(t.evidence_id for t in tokens),
    )


def _page(lines=(), tokens=()):
    return OCRPageResult(
        page_number=1,
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
        status=OCRStatus.SUCCEEDED,
        review_reasons=(),
        ocr_engine="paddleocr",
        ocr_engine_version="3.7.0",
        ocr_configuration="paddleocr",
        processed_at=datetime.now(timezone.utc),
    )


def _preprocessing_result(batch_id, document_id, source_path="invoice.pdf", sha256="s" * 64):
    quality = PageQuality(
        page_number=1,
        width=1000,
        height=1200,
        brightness_score=100.0,
        contrast_score=50.0,
        blur_score=100.0,
        detected_skew_angle=0.0,
        quality_flags=(),
        review_required=False,
    )
    page = PreprocessedPage(
        page_number=1,
        original_render_path=Path("render.png"),
        processed_image_path=Path("processed.png"),
        original_render_sha256="r" * 64,
        processed_image_sha256="p" * 64,
        quality=quality,
    )
    event = PreprocessingEvent(
        event_type="PREPROCESSING",
        status=PreprocessingStatus.SUCCEEDED,
        batch_id=batch_id,
        document_id=document_id,
        occurred_at=datetime.now(timezone.utc),
        message="ok",
        review_required=False,
    )
    return PreprocessingResult(
        batch_id=batch_id,
        document_id=document_id,
        source_path=Path(source_path),
        source_sha256=sha256,
        status=PreprocessingStatus.SUCCEEDED,
        pages=(page,),
        event=event,
    )


@pytest.fixture
def config(tmp_path):
    return NormalizationConfig(artifact_root=tmp_path / "phase4")


@pytest.fixture
def ocr_config(tmp_path):
    return OCRConfig(artifact_root=tmp_path / "phase3", ocr_engine_name="paddleocr", ocr_version="ocr-v2-paddle")


def _persist_synthetic_ocr_result(batch_id, document_id, ocr_config, sha256):
    """Build a synthetic, already-guarded `OCRDocumentResult` and persist it
    at exactly the path and byte format `ap_agent.tools.ocr.process_ocr_document`
    uses (`ocr_document_result_to_dict` + the Phase 2/3 `write_json_atomically`
    binding), without depending on a real OCR provider — this is a fast,
    dependency-isolated unit test, not an OCR integration test."""

    token = _token("TOTAL: 10.00", 0, 0, 10, 10, 1)
    line = _line("TOTAL: 10.00", 0, 0, 10, 10, 1, tokens=[token])
    page = _page([line], [token])
    page = replace(page, ocr_engine="tesseract")

    event = OCREvent(
        event_type="OCR_EXTRACTION",
        status=OCRStatus.SUCCEEDED,
        batch_id=batch_id,
        document_id=document_id,
        occurred_at=datetime.now(timezone.utc),
        message="ok",
        review_required=False,
    )
    ocr_result = OCRDocumentResult(
        batch_id=batch_id,
        document_id=document_id,
        source_document_sha256=sha256,
        preprocessing_version="phase2-v1",
        status=OCRStatus.SUCCEEDED,
        pages=(page,),
        event=event,
    )

    from ap_agent.models.ocr import OCRDocumentInput, OCRPageInput

    document_input = OCRDocumentInput(
        batch_id=batch_id,
        document_id=document_id,
        source_document_sha256=sha256,
        preprocessing_version="phase2-v1",
        pages=(
            OCRPageInput(
                page_number=1,
                processed_image_path=Path("processed.png"),
                processed_image_sha256="p" * 64,
                preprocessing_review_required=False,
            ),
        ),
    )
    document_directory = build_ocr_document_directory(document_input, ocr_config)
    write_json_atomically(
        document_directory / "ocr_document_result.json", ocr_document_result_to_dict(ocr_result)
    )

    return ocr_result

    return document_input


# --- build_normalization_input: identity, integrity and rejection ----------


def test_build_normalization_input_rejects_document_id_mismatch(config, ocr_config):
    batch_id, document_id = uuid4(), uuid4()
    preprocessing_result = _preprocessing_result(batch_id, document_id)
    ocr_result = OCRDocumentResult(
        batch_id=batch_id,
        document_id=uuid4(),  # mismatched
        source_document_sha256=preprocessing_result.source_sha256,
        preprocessing_version="phase2-v1",
        status=OCRStatus.SUCCEEDED,
        pages=(_page(),),
        event=OCREvent(
            event_type="OCR_EXTRACTION",
            status=OCRStatus.SUCCEEDED,
            batch_id=batch_id,
            document_id=uuid4(),
            occurred_at=datetime.now(timezone.utc),
            message="ok",
            review_required=False,
        ),
    )

    with pytest.raises(NormalizationIntegrityError) as excinfo:
        build_normalization_input(
            ocr_result=ocr_result, preprocessing_result=preprocessing_result, ocr_config=ocr_config
        )

    assert excinfo.value.reason == "DOCUMENT_IDENTITY_MISMATCH"


def test_build_normalization_input_rejects_source_sha256_mismatch(config, ocr_config):
    batch_id, document_id = uuid4(), uuid4()
    preprocessing_result = _preprocessing_result(batch_id, document_id, sha256="s" * 64)
    event = OCREvent(
        event_type="OCR_EXTRACTION",
        status=OCRStatus.SUCCEEDED,
        batch_id=batch_id,
        document_id=document_id,
        occurred_at=datetime.now(timezone.utc),
        message="ok",
        review_required=False,
    )
    ocr_result = OCRDocumentResult(
        batch_id=batch_id,
        document_id=document_id,
        source_document_sha256="0" * 64,  # mismatched
        preprocessing_version="phase2-v1",
        status=OCRStatus.SUCCEEDED,
        pages=(_page(),),
        event=event,
    )

    with pytest.raises(NormalizationIntegrityError) as excinfo:
        build_normalization_input(
            ocr_result=ocr_result, preprocessing_result=preprocessing_result, ocr_config=ocr_config
        )

    assert excinfo.value.reason == "SOURCE_DOCUMENT_SHA256_MISMATCH"


def test_build_normalization_input_rejects_a_missing_artifact(config, ocr_config):
    batch_id, document_id = uuid4(), uuid4()
    preprocessing_result = _preprocessing_result(batch_id, document_id, sha256="s" * 64)
    event = OCREvent(
        event_type="OCR_EXTRACTION",
        status=OCRStatus.SUCCEEDED,
        batch_id=batch_id,
        document_id=document_id,
        occurred_at=datetime.now(timezone.utc),
        message="ok",
        review_required=False,
    )
    ocr_result = OCRDocumentResult(
        batch_id=batch_id,
        document_id=document_id,
        source_document_sha256="s" * 64,
        preprocessing_version="phase2-v1",
        status=OCRStatus.SUCCEEDED,
        pages=(_page(),),
        event=event,
    )

    # No OCR artifact was ever persisted for this batch/document.
    with pytest.raises(NormalizationIntegrityError) as excinfo:
        build_normalization_input(
            ocr_result=ocr_result, preprocessing_result=preprocessing_result, ocr_config=ocr_config
        )

    assert excinfo.value.reason == "OCR_ARTIFACT_MISSING"


def test_build_normalization_input_succeeds_against_a_real_persisted_ocr_result(config, ocr_config):
    """The bridge's real-world path: given an OCR result that was actually
    persisted at its expected artifact path (exactly what
    `ap_agent.tools.ocr.process_ocr_document` always does — it never
    persists a stale pre-guard snapshot, D-4/D-5), the bridge accepts it and
    derives `source_name`/`ocr_version` exactly as the notebook did."""

    batch_id, document_id = uuid4(), uuid4()
    preprocessing_result = _preprocessing_result(
        batch_id, document_id, source_path="/uploads/My Invoice.pdf", sha256="s" * 64
    )
    ocr_result = _persist_synthetic_ocr_result(batch_id, document_id, ocr_config, sha256="s" * 64)

    normalization_input = build_normalization_input(
        ocr_result=ocr_result, preprocessing_result=preprocessing_result, ocr_config=ocr_config
    )

    assert normalization_input.source_name == "My Invoice.pdf"
    assert normalization_input.ocr_version == "phase-3:tesseract"
    assert normalization_input.source_document_sha256 == "s" * 64
    assert normalization_input.ocr_result is ocr_result


def test_build_normalization_input_rejects_a_tampered_artifact(config, ocr_config):
    """A persisted artifact that no longer matches the in-memory OCR result
    (e.g. hand-edited, or a stale copy from a previous run) must fail
    closed rather than silently being trusted."""

    batch_id, document_id = uuid4(), uuid4()
    preprocessing_result = _preprocessing_result(batch_id, document_id, sha256="s" * 64)
    ocr_result = _persist_synthetic_ocr_result(batch_id, document_id, ocr_config, sha256="s" * 64)

    document_directory = (
        ocr_config.artifact_root / str(batch_id) / str(document_id) / ocr_config.ocr_version
    )
    artifact_path = document_directory / "ocr_document_result.json"
    tampered = json.loads(artifact_path.read_text(encoding="utf-8"))
    tampered["status"] = "REVIEW_REQUIRED"
    artifact_path.write_text(json.dumps(tampered, indent=2), encoding="utf-8")

    with pytest.raises(NormalizationIntegrityError) as excinfo:
        build_normalization_input(
            ocr_result=ocr_result, preprocessing_result=preprocessing_result, ocr_config=ocr_config
        )

    assert excinfo.value.reason == "OCR_ARTIFACT_HASH_MISMATCH"


def test_verify_ocr_result_artifact_integrity_is_reusable_standalone(ocr_config):
    batch_id, document_id = uuid4(), uuid4()
    ocr_result = _persist_synthetic_ocr_result(batch_id, document_id, ocr_config, sha256="s" * 64)

    artifact_path = verify_ocr_result_artifact_integrity(ocr_result, ocr_config)
    assert artifact_path.is_file()


# --- artifact persistence, collision guard, idempotent rerun ---------------


def _invoice_record(batch_id, document_id, source_name="doc.pdf", sha256="s" * 64, total=Decimal("10.00")):
    reference = EvidenceReference(
        reference_id=uuid4(),
        reference_type=EvidenceReferenceType.LINE,
        page_number=1,
        reading_order=1,
        raw_text="TOTAL: 10.00",
        confidence=95.0,
        bounding_box=_box(0, 0, 10, 10),
    )
    field = NormalizedInvoiceField(
        field_id=uuid4(),
        field_name=InvoiceFieldName.TOTAL_AMOUNT,
        raw_value="10.00",
        normalized_value=total,
        value_type=NormalizedValueType.DECIMAL,
        confidence=95.0,
        extraction_method=ExtractionMethod.LABEL_VALUE,
        evidence_references=(reference,),
    )
    return NormalizedInvoiceRecord(
        invoice_record_id=uuid4(),
        batch_id=batch_id,
        document_id=document_id,
        source_name=source_name,
        source_document_sha256=sha256,
        fields=(field,),
        line_items=(),
        normalization_version="normalization-v1",
        created_at=datetime.now(timezone.utc),
    )


def _result_for_record(record):
    event = NormalizationEvent(
        event_type="NORMALIZATION",
        status=NormalizationStatus.SUCCEEDED,
        batch_id=record.batch_id,
        document_id=record.document_id,
        occurred_at=datetime.now(timezone.utc),
        message="ok",
        review_required=False,
    )
    return NormalizationResult(
        batch_id=record.batch_id,
        document_id=record.document_id,
        source_document_sha256=record.source_document_sha256,
        ocr_version="phase-3:paddleocr",
        normalization_version="normalization-v1",
        status=NormalizationStatus.SUCCEEDED,
        invoice_record=record,
        field_candidates=(),
        event=event,
    )


def _normalization_input_for(record):
    return NormalizationInput(
        batch_id=record.batch_id,
        document_id=record.document_id,
        source_name=record.source_name,
        source_document_sha256=record.source_document_sha256,
        ocr_version="phase-3:paddleocr",
        ocr_result=None,  # not read by persist_normalization_result
    )


def test_persist_normalization_result_writes_the_four_required_artifacts(config):
    batch_id, document_id = uuid4(), uuid4()
    record = _invoice_record(batch_id, document_id)
    result = _result_for_record(record)
    normalization_input = _normalization_input_for(record)

    directory = persist_normalization_result(
        normalization_input=normalization_input, result=result, config=config
    )

    names = {p.name for p in directory.iterdir() if p.is_file()}
    assert names == {
        "normalization_result.json",
        "field_candidates.json",
        "normalized_invoice.json",
        "normalization_event.json",
    }

    persisted = json.loads((directory / "normalization_result.json").read_text(encoding="utf-8"))
    assert persisted["document_id"] == str(document_id)
    assert persisted["status"] == "SUCCEEDED"


def test_persist_normalization_result_is_idempotent_for_identical_content(config):
    batch_id, document_id = uuid4(), uuid4()
    record = _invoice_record(batch_id, document_id)
    result = _result_for_record(record)
    normalization_input = _normalization_input_for(record)

    directory = persist_normalization_result(normalization_input=normalization_input, result=result, config=config)
    before = (directory / "normalized_invoice.json").read_bytes()

    # Persisting the identical result object again must not raise and must
    # not be treated as a collision.
    persist_normalization_result(normalization_input=normalization_input, result=result, config=config)

    after = (directory / "normalized_invoice.json").read_bytes()
    assert before == after


def test_persist_normalization_result_fails_closed_on_a_genuine_collision(config):
    batch_id, document_id = uuid4(), uuid4()
    record = _invoice_record(batch_id, document_id, total=Decimal("10.00"))
    result = _result_for_record(record)
    normalization_input = _normalization_input_for(record)

    persist_normalization_result(normalization_input=normalization_input, result=result, config=config)

    # A different result for the same batch/document/artifact path (same
    # invoice_record_id-independent directory, different business content).
    other_record = replace(record, fields=(replace(record.fields[0], normalized_value=Decimal("99.00")),))
    other_result = _result_for_record(other_record)

    with pytest.raises(NormalizationIntegrityError) as excinfo:
        persist_normalization_result(normalization_input=normalization_input, result=other_result, config=config)

    assert excinfo.value.reason == "NORMALIZATION_ARTIFACT_COLLISION"


def test_normalize_invoice_document_rerun_is_idempotent_on_disk(config):
    """`normalize_invoice_document` run twice on the same input persists
    identical business content both times (timestamps aside) — task §14
    'idempotent rerun'."""

    from ap_agent.tools.normalization import phase_4_document_directory as _dir

    document_id, batch_id = uuid4(), uuid4()
    reference = EvidenceReference(
        reference_id=uuid4(),
        reference_type=EvidenceReferenceType.LINE,
        page_number=1,
        reading_order=1,
        raw_text="TOTAL: 10.00",
        confidence=95.0,
        bounding_box=_box(0, 0, 10, 10),
    )

    token = _token("TOTAL: 10.00", 0, 0, 10, 10, 1)
    line = _line("TOTAL: 10.00", 0, 0, 10, 10, 1, tokens=[token])
    page = _page([line], [token])

    event = OCREvent(
        event_type="OCR_EXTRACTION",
        status=OCRStatus.SUCCEEDED,
        batch_id=batch_id,
        document_id=document_id,
        occurred_at=datetime.now(timezone.utc),
        message="ok",
        review_required=False,
    )
    ocr_result = OCRDocumentResult(
        batch_id=batch_id,
        document_id=document_id,
        source_document_sha256="s" * 64,
        preprocessing_version="phase2-v1",
        status=OCRStatus.SUCCEEDED,
        pages=(page,),
        event=event,
    )
    normalization_input = NormalizationInput(
        batch_id=batch_id,
        document_id=document_id,
        source_name="doc.pdf",
        source_document_sha256="s" * 64,
        ocr_version="phase-3:paddleocr",
        ocr_result=ocr_result,
    )

    normalize_invoice_document(normalization_input, config=config)
    normalize_invoice_document(normalization_input, config=config)  # must not raise


# --- invalid-hash fail-closed path (cell 66 §9 parity) ----------------------


def test_normalize_invoice_document_fails_closed_on_invalid_source_hash(config, tmp_path):
    token = _token("TOTAL: 10.00", 0, 0, 10, 10, 1)
    line = _line("TOTAL: 10.00", 0, 0, 10, 10, 1, tokens=[token])
    page = _page([line], [token])

    batch_id, document_id = uuid4(), uuid4()
    event = OCREvent(
        event_type="OCR_EXTRACTION",
        status=OCRStatus.SUCCEEDED,
        batch_id=batch_id,
        document_id=document_id,
        occurred_at=datetime.now(timezone.utc),
        message="ok",
        review_required=False,
    )
    ocr_result = OCRDocumentResult(
        batch_id=batch_id,
        document_id=document_id,
        source_document_sha256="c" * 64,
        preprocessing_version="phase2-v1",
        status=OCRStatus.SUCCEEDED,
        pages=(page,),
        event=event,
    )

    invalid_input = NormalizationInput(
        batch_id=batch_id,
        document_id=document_id,
        source_name="doc.pdf",
        source_document_sha256="0" * 64,  # mismatched
        ocr_version="phase-3:paddleocr",
        ocr_result=ocr_result,
    )

    result = normalize_invoice_document(invalid_input, config=config)

    assert result.status == NormalizationStatus.FAILED
    assert result.invoice_record is None
    assert result.event.status == NormalizationStatus.FAILED
    assert result.event.review_required is True
    assert result.errors
    assert any("SHA-256" in error for error in result.errors)
    assert result.review_reasons == ("NORMALIZATION_FAILED",)


# --- cross-document artifact isolation --------------------------------------


def test_persisted_artifacts_stay_isolated_per_document(config):
    batch_id = uuid4()
    document_id_1, document_id_2 = uuid4(), uuid4()

    record_1 = _invoice_record(batch_id, document_id_1, source_name="doc1.pdf")
    record_2 = _invoice_record(batch_id, document_id_2, source_name="doc2.pdf")

    directory_1 = persist_normalization_result(
        normalization_input=_normalization_input_for(record_1), result=_result_for_record(record_1), config=config
    )
    directory_2 = persist_normalization_result(
        normalization_input=_normalization_input_for(record_2), result=_result_for_record(record_2), config=config
    )

    assert directory_1 != directory_2
    contents_1 = "\n".join(str(p) for p in directory_1.rglob("*"))
    assert str(document_id_2) not in contents_1


# --- JSON-safe conversion ----------------------------------------------------


def test_convert_to_json_safe_handles_dataclasses_enums_decimals_and_dates():
    batch_id, document_id = uuid4(), uuid4()
    record = _invoice_record(batch_id, document_id, total=Decimal("69.22"))

    payload = convert_to_json_safe(record)

    assert payload["batch_id"] == str(batch_id)
    assert payload["fields"][0]["normalized_value"] == "69.22"
    assert payload["fields"][0]["field_name"] == "TOTAL_AMOUNT"
    assert payload["fields"][0]["value_type"] == "DECIMAL"
    json.dumps(payload)  # must be fully JSON-serialisable


def test_write_json_safe_atomically_uses_ensure_ascii_false(tmp_path):
    destination = tmp_path / "out.json"
    write_json_safe_atomically(destination, {"name": "Café"})

    text = destination.read_text(encoding="utf-8")
    assert "Café" in text
    assert "\\u00e9" not in text
