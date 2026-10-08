"""M11E.4 (real hosted finding): a genuinely MISSING header field (SUPPLIER_NAME_MISSING) could not be supplied.

`build_command_capabilities` listed a correctable header only when it already existed, `validate_review_command` answered
HEADER_FIELD_NOT_PRESENT and `apply_correction_overlay` refused an absent header target. These tests pin the corrected
contract: an explicitly configured missing header is offered (current_value null), accepted only as an insertion
(previous_value null), and inserted into the DERIVED normalization record with the right type, provenance and confidence."""

from __future__ import annotations

import json
import uuid
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from ap_agent.exceptions import ResumeIntegrityError
from ap_agent.models.interface import (
    HumanReviewDisposition,
    InterfaceActor,
    InterfaceRole,
    ReviewAction,
    ReviewCaseStatus,
    ReviewCommand,
    ReviewCommandContext,
    ReviewFieldCorrection,
    default_interface_config,
)
from ap_agent.models.normalization import InvoiceFieldName as F
from ap_agent.models.normalization import NormalizedValueType as V
from ap_agent.serialization.hydration import hydrate_normalization_result
from ap_agent.serialization.memory_json import memory_json_safe
from ap_agent.services.correction_overlay import apply_correction_overlay, header_value_type, parse_overlay
from ap_agent.services.review_capabilities import build_command_capabilities
from ap_agent.services.review_commands import validate_review_command
from ap_agent.tools.normalization import normalize_candidate_value
from tests.support.m11d_seed import make_normalization_result
from tests.unit.test_review_capabilities import _detail

pytestmark = pytest.mark.unit

TENANT, WORKFLOW, BATCH, DOCUMENT, CASE = (uuid.uuid4() for _ in range(5))
NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)
DECISION = uuid.uuid4()
CONFIG = default_interface_config()
REVIEWER = InterfaceActor(actor_id="rev-1", tenant_id=TENANT, role=InterfaceRole.AP_REVIEWER, authenticated_at=NOW - timedelta(seconds=1))


def _context(*, header=((F.INVOICE_NUMBER, "INV-1"),), status=ReviewCaseStatus.IN_REVIEW) -> ReviewCommandContext:
    return ReviewCommandContext(
        tenant_id=TENANT, workflow_id=WORKFLOW, batch_id=BATCH, document_id=DOCUMENT, review_case_id=CASE,
        case_status=status, assigned_reviewer_id="rev-1", review_revision=2, workflow_revision=3,
        header_field_values=tuple(header), known_invoice_line_numbers=(1,), available_evidence_reference_ids=("ev-1", "ev-2"),
    )


def _correction(field=F.SUPPLIER_NAME, *, previous=None, corrected="SuperStore", line_number=None, evidence=("ev-1",), reason="Read from the paper invoice."):
    return ReviewFieldCorrection(
        field_name=field, line_number=line_number, previous_value=previous, corrected_value=corrected,
        reason=reason, evidence_reference_ids=tuple(evidence),
    )


def _command(context, *corrections) -> ReviewCommand:
    return ReviewCommand(
        command_id=uuid.uuid5(uuid.NAMESPACE_URL, "m11e4"), idempotency_key="m11e4-correct-key-1", tenant_id=TENANT,
        workflow_id=WORKFLOW, batch_id=BATCH, document_id=DOCUMENT, review_case_id=CASE, actor=REVIEWER,
        action=ReviewAction.CORRECT, disposition=HumanReviewDisposition.CORRECTED,
        observed_review_revision=context.review_revision, observed_workflow_revision=context.workflow_revision,
        reason_codes=("REVIEWER_DECISION",), notes=None, corrections=tuple(corrections), requested_at=NOW,
    )


def _errors(*corrections, context=None) -> tuple[str, ...]:
    context = context or _context()
    return validate_review_command(_command(context, *corrections), context, CONFIG)


# -- the selector: a missing header is an editable target -----------------------------------------------------------------------


def test_a_missing_supplier_name_is_offered_as_an_editable_target_with_a_null_current_value():
    capabilities = build_command_capabilities(
        actor=REVIEWER, context=_context(), detail=_detail(), normalized_payload=None, config=CONFIG, writes_enabled=True
    )
    headers = {item["field_name"]: item["current_value"] for item in capabilities["correction_policy"]["header_fields"]}

    assert headers[F.SUPPLIER_NAME] is None and headers[F.INVOICE_NUMBER] == "INV-1"
    assert set(headers) == set(CONFIG.correctable_header_fields)


def test_only_explicitly_configured_headers_are_offered_even_when_missing():
    narrow = replace(CONFIG, correctable_header_fields=(F.INVOICE_NUMBER,))
    capabilities = build_command_capabilities(
        actor=REVIEWER, context=_context(), detail=_detail(), normalized_payload=None, config=narrow, writes_enabled=True
    )

    assert [item["field_name"] for item in capabilities["correction_policy"]["header_fields"]] == [F.INVOICE_NUMBER]
    assert "SUPPLIER_NAME" not in json.dumps(capabilities["correction_policy"], default=str)


# -- validation ---------------------------------------------------------------------------------------------------------------------


def test_a_missing_supplier_name_can_be_supplied_with_a_null_previous_value():
    assert _errors(_correction()) == ()


def test_a_missing_header_that_is_not_configured_is_rejected():
    narrow = replace(CONFIG, correctable_header_fields=(F.INVOICE_NUMBER,))
    context = _context()

    assert "CORRECTION_FIELD_NOT_ALLOWED" in validate_review_command(_command(context, _correction()), context, narrow)


@pytest.mark.parametrize("forged", ["", "Acme", "SuperStore", "<missing>", "None", "null"])
def test_a_forged_non_null_previous_value_for_a_missing_header_is_rejected(forged):
    assert "PREVIOUS_VALUE_MISMATCH" in _errors(_correction(previous=forged))


def test_a_present_header_still_requires_the_exact_stored_previous_value():
    assert _errors(_correction(F.INVOICE_NUMBER, previous="INV-1", corrected="INV-2")) == ()
    assert "PREVIOUS_VALUE_MISMATCH" in _errors(_correction(F.INVOICE_NUMBER, previous="wrong", corrected="INV-2"))
    assert "PREVIOUS_VALUE_MISMATCH" in _errors(_correction(F.INVOICE_NUMBER, previous=None, corrected="INV-2"))


def test_line_number_misuse_on_a_header_is_still_rejected():
    assert "HEADER_CORRECTION_LINE_NUMBER_PROHIBITED" in _errors(_correction(line_number=1))


def test_duplicate_targets_are_still_rejected():
    assert "DUPLICATE_CORRECTION_TARGET" in _errors(_correction(), _correction(corrected="Other"))


def test_arbitrary_fields_are_still_rejected():
    arbitrary = replace(CONFIG, correctable_header_fields=(F.INVOICE_NUMBER,))
    context = _context()

    assert "CORRECTION_FIELD_NOT_ALLOWED" in validate_review_command(_command(context, _correction(F.TOTAL_AMOUNT, corrected="1")), context, arbitrary)


def test_stale_revisions_unsupported_evidence_and_blank_values_are_still_rejected():
    context = _context()
    stale = replace(_command(context, _correction()), observed_review_revision=context.review_revision + 1)

    assert "STALE_REVIEW_REVISION" in validate_review_command(stale, context, CONFIG)
    assert "UNKNOWN_EVIDENCE_REFERENCE" in _errors(_correction(evidence=("ev-nope",)))
    assert "CORRECTION_EVIDENCE_REQUIRED" in _errors(_correction(evidence=()))
    assert "CORRECTED_VALUE_MISSING" in _errors(_correction(corrected="   "))
    assert "CORRECTION_REASON_REQUIRED" in _errors(_correction(reason=" "))


def test_a_field_stored_as_present_but_null_accepts_a_null_previous_value():
    context = _context(header=((F.INVOICE_NUMBER, "INV-1"), (F.SUPPLIER_NAME, None)))

    assert validate_review_command(_command(context, _correction()), context, CONFIG) == ()


# -- the overlay: insertion into the derived record, never the original --------------------------------------------------------------


def _overlay(*corrections, decision=DECISION) -> dict:
    return parse_overlay(json.dumps({"decision_id": str(decision), "corrections": list(corrections)}))


def _entry(field="SUPPLIER_NAME", *, previous=None, corrected="SuperStore", line_number=None):
    return {
        "field_name": field, "line_number": line_number, "previous_value": previous, "corrected_value": corrected,
        "reason": "Read from the paper invoice.", "evidence_reference_ids": ["ev-1"],
    }


@pytest.fixture()
def normalization():
    return make_normalization_result(
        batch_id=uuid.uuid4(), document_id=uuid.uuid4(), source_sha256="a" * 64, seed="m11e4", omit_header=(F.SUPPLIER_NAME,)
    )


def test_the_seeded_normalization_really_lacks_the_supplier(normalization):
    assert normalization.invoice_record.get_field(F.SUPPLIER_NAME) is None


def test_the_overlay_inserts_the_missing_supplier_into_a_derived_record(normalization):
    before = memory_json_safe(normalization)
    original_names = [field.field_name for field in normalization.invoice_record.fields]

    corrected = apply_correction_overlay(normalization, _overlay(_entry()), decision_id=DECISION)
    supplier = corrected.invoice_record.get_field(F.SUPPLIER_NAME)

    assert supplier is not None and supplier.normalized_value == "SuperStore" and supplier.raw_value == "SuperStore"
    assert supplier.value_type is V.TEXT
    assert supplier.confidence == 100.0 and supplier.review_required is False and supplier.review_reasons == ()
    assert supplier.normalization_notes[0] == "HUMAN_REVIEW_CORRECTION"
    assert f"decision_id={DECISION}" in supplier.normalization_notes and "previous_value=<missing>" in supplier.normalization_notes
    assert any(note.startswith("reason=") for note in supplier.normalization_notes)
    assert any(note.startswith("evidence_reference_ids=") for note in supplier.normalization_notes)

    # append-only: the existing fields keep their order and the new one is appended
    assert [field.field_name for field in corrected.invoice_record.fields] == original_names + [F.SUPPLIER_NAME]
    assert corrected.invoice_record.review_required is False and corrected.invoice_record.review_reasons == ()
    assert memory_json_safe(normalization) == before  # the input (the stored original) is never mutated


def test_the_inserted_field_survives_serialisation_and_hydration(normalization):
    corrected = apply_correction_overlay(normalization, _overlay(_entry()), decision_id=DECISION)

    assert hydrate_normalization_result(memory_json_safe(corrected)) == corrected


def test_the_overlay_is_deterministic_and_the_field_id_is_stable(normalization):
    first = apply_correction_overlay(normalization, _overlay(_entry()), decision_id=DECISION)
    second = apply_correction_overlay(normalization, _overlay(_entry()), decision_id=DECISION)

    assert first.invoice_record.get_field(F.SUPPLIER_NAME).field_id == second.invoice_record.get_field(F.SUPPLIER_NAME).field_id


def test_a_forged_previous_value_for_a_missing_header_is_refused_by_the_overlay(normalization):
    with pytest.raises(ResumeIntegrityError, match="OVERLAY_PREVIOUS_VALUE_MISMATCH"):
        apply_correction_overlay(normalization, _overlay(_entry(previous="Acme")), decision_id=DECISION)


def test_the_overlay_still_refuses_line_numbers_duplicates_and_blank_values_on_a_missing_header(normalization):
    with pytest.raises(ResumeIntegrityError, match="OVERLAY_TARGET_INVALID"):
        apply_correction_overlay(normalization, _overlay(_entry(line_number=1)), decision_id=DECISION)

    with pytest.raises(ResumeIntegrityError, match="OVERLAY_DUPLICATE_TARGET"):
        apply_correction_overlay(normalization, _overlay(_entry(), _entry(corrected="Other")), decision_id=DECISION)

    with pytest.raises(ResumeIntegrityError, match="OVERLAY_VALUE_MISSING"):
        apply_correction_overlay(normalization, _overlay(_entry(corrected="  ")), decision_id=DECISION)

    with pytest.raises(ResumeIntegrityError, match="OVERLAY_DECISION_MISMATCH"):
        apply_correction_overlay(normalization, _overlay(_entry(), decision=uuid.uuid4()), decision_id=DECISION)


def test_inserting_one_missing_and_correcting_one_present_header_together(normalization):
    corrected = apply_correction_overlay(
        normalization, _overlay(_entry(), _entry("INVOICE_NUMBER", previous="INV-1001", corrected="INV-2000")), decision_id=DECISION
    )

    assert corrected.invoice_record.get_field(F.SUPPLIER_NAME).normalized_value == "SuperStore"
    assert corrected.invoice_record.get_field(F.INVOICE_NUMBER).normalized_value == "INV-2000"


# -- canonical value types ----------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("field", "text", "expected_type", "expected_value"),
    [
        (F.SUPPLIER_NAME, "SuperStore", V.TEXT, "SuperStore"),
        (F.SUPPLIER_ADDRESS, "1 High St", V.TEXT, "1 High St"),
        (F.DUE_DATE, "2012-04-01", V.DATE, date(2012, 4, 1)),
        (F.CURRENCY, "usd", V.CURRENCY_CODE, "USD"),
        (F.SHIPPING_AMOUNT, "5.10", V.DECIMAL, Decimal("5.10")),
        (F.DISCOUNT_AMOUNT, "1", V.DECIMAL, Decimal("1")),
        (F.PAYMENT_TERMS, "Net 30", V.TEXT, "Net 30"),
    ],
)
def test_an_inserted_header_gets_the_canonical_value_type(field, text, expected_type, expected_value):
    normalization = make_normalization_result(
        batch_id=uuid.uuid4(), document_id=uuid.uuid4(), source_sha256="b" * 64, seed=f"types-{field.value}", omit_header=(field,)
    )
    inserted = apply_correction_overlay(normalization, _overlay(_entry(field.value, corrected=text)), decision_id=DECISION)
    field_record = inserted.invoice_record.get_field(field)

    assert field_record.value_type is expected_type and field_record.normalized_value == expected_value


def test_the_inserted_value_type_matches_the_normaliser_for_every_header_field(tmp_path):
    # guards against drift between correction_overlay.header_value_type and ap_agent.tools.normalization
    from ap_agent.config.settings import NormalizationConfig

    for field in F:
        expected = header_value_type(field)

        if expected is None:  # a line sub-field
            continue

        sample = {V.DECIMAL: "1.00", V.DATE: "2012-04-01", V.CURRENCY_CODE: "USD", V.TEXT: "Sample"}[expected]
        _, normaliser_type = normalize_candidate_value(field, sample, config=NormalizationConfig(artifact_root=tmp_path))

        assert normaliser_type is expected, field


def test_a_non_decimal_or_bad_date_for_an_inserted_header_is_refused():
    normalization = make_normalization_result(
        batch_id=uuid.uuid4(), document_id=uuid.uuid4(), source_sha256="c" * 64, seed="bad", omit_header=(F.DUE_DATE, F.SHIPPING_AMOUNT)
    )

    with pytest.raises(ResumeIntegrityError, match="OVERLAY_VALUE_INVALID"):
        apply_correction_overlay(normalization, _overlay(_entry("DUE_DATE", corrected="01/04/2012")), decision_id=DECISION)

    with pytest.raises(ResumeIntegrityError, match="OVERLAY_VALUE_INVALID"):
        apply_correction_overlay(normalization, _overlay(_entry("SHIPPING_AMOUNT", corrected="5,10")), decision_id=DECISION)
