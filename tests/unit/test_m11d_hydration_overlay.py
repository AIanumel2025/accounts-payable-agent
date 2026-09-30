"""Typed hydration of stored phase payloads and correction-overlay application."""

from __future__ import annotations

import copy
import json
import uuid
from datetime import date
from decimal import Decimal

import pytest

from ap_agent.config.settings import FinancialValidationConfig, MatchingConfig, NormalizationConfig
from ap_agent.adapters.reference_data_adapter import load_reference_data_bundle
from ap_agent.exceptions import ResumeIntegrityError
from ap_agent.models.normalization import InvoiceFieldName as F
from ap_agent.models.normalization import NormalizedValueType as V
from ap_agent.serialization.hydration import (
    HydrationError,
    hydrate_financial_validation_result,
    hydrate_matching_result,
    hydrate_normalization_result,
)
from ap_agent.serialization.memory_json import memory_json_safe
from ap_agent.services.correction_overlay import apply_correction_overlay, corrected_field_names, parse_overlay
from ap_agent.tools.financial_validation import build_validation_input, process_financial_validation
from ap_agent.tools.matching import build_matching_input, process_invoice_matching
from ap_agent.tools.normalization import persist_normalization_result
from tests.support.m11d_seed import REFERENCE_DATA_DIRECTORY, make_normalization_result

pytestmark = pytest.mark.unit

DECISION = uuid.uuid4()


@pytest.fixture()
def phases(tmp_path):
    batch_id, document_id = uuid.uuid4(), uuid.uuid4()
    normalization = make_normalization_result(
        batch_id=batch_id, document_id=document_id, source_sha256="a" * 64,
        header={F.TOTAL_AMOUNT: (Decimal("58.710"), V.DECIMAL)}, seed="unit",
    )
    config = NormalizationConfig(artifact_root=tmp_path / "p4")
    persist_normalization_result(
        normalization_input=type("I", (), {"batch_id": batch_id, "document_id": document_id})(),
        result=normalization, config=config,
    )
    financial = process_financial_validation(
        build_validation_input(normalization, normalization_config=config),
        config=FinancialValidationConfig(artifact_root=tmp_path / "p5"),
    )
    matching_input = build_matching_input(normalization, financial, load_reference_data_bundle(REFERENCE_DATA_DIRECTORY))
    matching = process_invoice_matching(matching_input, config=MatchingConfig(artifact_root=tmp_path / "p6"))
    return normalization, financial, matching


def test_real_phase_results_round_trip_exactly(phases):
    normalization, financial, matching = phases

    assert hydrate_normalization_result(memory_json_safe(normalization)) == normalization
    assert hydrate_financial_validation_result(memory_json_safe(financial)) == financial
    assert hydrate_matching_result(memory_json_safe(matching)) == matching


def test_decimals_dates_and_enums_keep_their_exact_type_and_text(phases):
    normalization, _, _ = phases
    hydrated = hydrate_normalization_result(memory_json_safe(normalization))
    by_name = {field.field_name: field for field in hydrated.invoice_record.fields}

    assert by_name[F.TOTAL_AMOUNT].normalized_value == Decimal("58.710")
    assert str(by_name[F.TOTAL_AMOUNT].normalized_value) == "58.710"  # trailing zero kept: never via float
    assert by_name[F.INVOICE_DATE].normalized_value == date(2012, 3, 2)
    assert by_name[F.CURRENCY].value_type is V.CURRENCY_CODE


@pytest.mark.parametrize(
    "mutate",
    [
        lambda p: p.pop("batch_id"),
        lambda p: p.update(unexpected="x"),
        lambda p: p.update(status="NOT_A_STATUS"),
        lambda p: p["invoice_record"].update(created_at="not-a-date"),
        lambda p: p["invoice_record"]["fields"][0].update(confidence="high"),
        lambda p: p["invoice_record"]["fields"][5].update(normalized_value="12,50"),
        lambda p: p.update(invoice_record=[]),
        lambda p: p["invoice_record"].update(fields={}),
    ],
)
def test_malformed_payloads_fail_closed(phases, mutate):
    payload = copy.deepcopy(memory_json_safe(phases[0]))
    mutate(payload)

    with pytest.raises(HydrationError):
        hydrate_normalization_result(payload)


def test_a_payload_that_hydrates_but_does_not_round_trip_is_refused(phases):
    payload = copy.deepcopy(memory_json_safe(phases[0]))
    payload["invoice_record"]["fields"][5]["normalized_value"] = "58.710000"  # same number, different text
    # An unverifiable normalization would silently change the stored evidence:
    # only an identical re-serialisation is accepted.
    hydrate_normalization_result(payload)  # exact decimal text round-trips
    payload["invoice_record"]["fields"][0]["raw_value"] = None

    with pytest.raises(HydrationError):
        hydrate_normalization_result(payload)


def _overlay(corrections):
    return {"decision_id": str(DECISION), "corrections": corrections}


def _total_correction(**overrides):
    correction = {
        "field_name": "TOTAL_AMOUNT", "line_number": None, "previous_value": "58.710", "corrected_value": "48.71",
        "reason": "Checked against the paper invoice.", "evidence_reference_ids": ["ev-1"],
    }
    correction.update(overrides)
    return correction


def test_overlay_corrects_a_typed_deep_copy_and_records_provenance(phases):
    normalization, _, _ = phases
    before = memory_json_safe(normalization)

    corrected = apply_correction_overlay(normalization, _overlay([_total_correction()]), decision_id=DECISION)

    assert memory_json_safe(normalization) == before  # the input (and so the stored original) is untouched
    field = corrected.invoice_record.get_field(F.TOTAL_AMOUNT)
    assert field.normalized_value == Decimal("48.71") and field.review_required is False
    assert field.normalization_notes[0] == "HUMAN_REVIEW_CORRECTION"
    assert f"decision_id={DECISION}" in field.normalization_notes
    assert "previous_value=58.710" in field.normalization_notes
    assert "evidence_reference_ids=ev-1" in field.normalization_notes
    # Inherited review flags are resolved by the decision; nothing else changed.
    assert corrected.invoice_record.review_required is False and corrected.review_reasons == ()
    assert corrected.invoice_record.get_field(F.SUPPLIER_NAME) == normalization.invoice_record.get_field(F.SUPPLIER_NAME)


def test_overlay_corrects_a_line_field_and_creates_a_missing_one(phases):
    normalization, _, _ = phases
    correction = {
        "field_name": "LINE_QUANTITY", "line_number": 1, "previous_value": "1", "corrected_value": "2.50",
        "reason": "Delivery note says two and a half.", "evidence_reference_ids": ["ev-2"],
    }

    corrected = apply_correction_overlay(normalization, _overlay([correction]), decision_id=DECISION)

    assert corrected.invoice_record.line_items[0].quantity.normalized_value == Decimal("2.50")
    assert corrected.invoice_record.line_items[0].unit_price == normalization.invoice_record.line_items[0].unit_price


@pytest.mark.parametrize(
    ("correction", "code"),
    [
        ({"field_name": "SUPPLIER_ADDRESS", "previous_value": None}, "OVERLAY_TARGET_INVALID"),  # header field absent
        ({"field_name": "LINE_QUANTITY", "line_number": 9, "previous_value": "1"}, "OVERLAY_TARGET_INVALID"),
        ({"field_name": "TOTAL_AMOUNT", "line_number": 1}, "OVERLAY_TARGET_INVALID"),
        ({"field_name": "NOT_A_FIELD"}, "OVERLAY_TARGET_INVALID"),
        ({"previous_value": "1.00"}, "OVERLAY_PREVIOUS_VALUE_MISMATCH"),
        ({"corrected_value": "12,50"}, "OVERLAY_VALUE_INVALID"),
        ({"corrected_value": "NaN"}, "OVERLAY_VALUE_INVALID"),
        ({"corrected_value": " "}, "OVERLAY_VALUE_MISSING"),
        ({"reason": " "}, "OVERLAY_REASON_MISSING"),
        ({"evidence_reference_ids": []}, "OVERLAY_EVIDENCE_MISSING"),
    ],
)
def test_invalid_overlay_entries_fail_closed(phases, correction, code):
    normalization, _, _ = phases

    with pytest.raises(ResumeIntegrityError) as raised:
        apply_correction_overlay(normalization, _overlay([_total_correction(**correction)]), decision_id=DECISION)

    assert raised.value.code == code


def test_duplicate_targets_and_a_foreign_decision_are_refused(phases):
    normalization, _, _ = phases

    with pytest.raises(ResumeIntegrityError, match="OVERLAY_DUPLICATE_TARGET"):
        apply_correction_overlay(normalization, _overlay([_total_correction(), _total_correction()]), decision_id=DECISION)

    with pytest.raises(ResumeIntegrityError, match="OVERLAY_DECISION_MISMATCH"):
        apply_correction_overlay(normalization, _overlay([_total_correction()]), decision_id=uuid.uuid4())


def test_overlay_parsing_and_names():
    overlay = parse_overlay(json.dumps(_overlay([_total_correction()])))
    assert corrected_field_names(overlay) == ("TOTAL_AMOUNT",)

    for bad in ("not json", "[]", json.dumps({"corrections": "x"})):
        with pytest.raises(ResumeIntegrityError, match="OVERLAY_MALFORMED"):
            parse_overlay(bad)
