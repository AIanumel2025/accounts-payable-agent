"""M11E.5: labelled, case-bound source-document evidence for human corrections (no database)."""

from __future__ import annotations

import json
import uuid
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

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
from ap_agent.models.review_evidence import (
    EVIDENCE_TYPE_EXTRACTED_FIELD,
    EVIDENCE_TYPE_SOURCE_DOCUMENT,
    SNIPPET_MAXIMUM_LENGTH,
    SOURCE_DOCUMENT_LABEL,
    field_evidence_option,
    sanitize_snippet,
    source_document_evidence_id,
    source_document_evidence_option,
)
from ap_agent.services.review_capabilities import build_command_capabilities
from ap_agent.services.review_commands import validate_review_command
from tests.unit.test_review_capabilities import _detail

pytestmark = pytest.mark.unit

DOCUMENT = uuid.uuid4()
SHA = "a1b2c3d4" * 8
TENANT, WORKFLOW, BATCH, CASE = (uuid.uuid4() for _ in range(4))
NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)
CONFIG = default_interface_config()
REVIEWER = InterfaceActor(actor_id="rev-1", tenant_id=TENANT, role=InterfaceRole.AP_REVIEWER, authenticated_at=NOW - timedelta(seconds=1))


# -- identity ---------------------------------------------------------------------------------------------------------------------


def test_the_source_evidence_id_is_stable_and_case_bound():
    first = source_document_evidence_id(DOCUMENT, SHA)

    assert first == source_document_evidence_id(DOCUMENT, SHA) == source_document_evidence_id(DOCUMENT, SHA.upper())
    assert str(uuid.UUID(first)) == first
    assert first != source_document_evidence_id(uuid.uuid4(), SHA)  # another document
    assert first != source_document_evidence_id(DOCUMENT, "b" * 64)  # same document, different stored hash


def test_the_source_evidence_id_reveals_neither_the_hash_nor_the_document_id():
    reference_id = source_document_evidence_id(DOCUMENT, SHA)

    assert SHA not in reference_id and str(DOCUMENT) not in reference_id and SHA[:8] not in reference_id.replace("-", "")


@pytest.mark.parametrize("bad", ["", "xyz", "a" * 63, "a" * 65, "g" * 64, None, 5])
def test_nothing_is_invented_for_an_invalid_stored_hash(bad):
    assert source_document_evidence_id(DOCUMENT, bad) is None
    assert source_document_evidence_option(DOCUMENT, bad) is None


def test_the_source_option_has_the_human_label_and_no_location():
    option = source_document_evidence_option(DOCUMENT, SHA)

    assert option.label == SOURCE_DOCUMENT_LABEL == "Original source invoice — SHA-256 verified"
    assert option.evidence_type == EVIDENCE_TYPE_SOURCE_DOCUMENT and option.page_number is None and option.snippet is None
    text = json.dumps(option.__dict__)
    for forbidden in ("s3://", "tenants/", "staging/", "/tmp", "http", "amazonaws", str(TENANT), SHA):
        assert forbidden not in text


# -- extracted-field descriptions ----------------------------------------------------------------------------------------------------


def test_extracted_field_options_are_described_safely():
    option = field_evidence_option("ev-1", field_name="TOTAL_AMOUNT", page_number=2, raw_text="  Total:\x00 58.71\n\t USD ")

    assert option.evidence_type == EVIDENCE_TYPE_EXTRACTED_FIELD
    assert option.label == "Extracted evidence — Total Amount, page 2" and option.page_number == 2
    assert option.snippet == "Total: 58.71 USD"  # control characters removed, whitespace collapsed


@pytest.mark.parametrize("page", [None, 0, -1, "2", True, 1.5])
def test_an_unusable_page_number_is_omitted(page):
    option = field_evidence_option("ev-1", field_name="SUPPLIER_NAME", page_number=page, raw_text=None)

    assert option.page_number is None and option.snippet is None and option.label == "Extracted evidence — Supplier Name"


def test_snippets_are_length_capped_and_empty_text_is_dropped():
    long = sanitize_snippet("word " * 100)

    assert len(long) <= SNIPPET_MAXIMUM_LENGTH and long.endswith("…")
    assert sanitize_snippet("   \x00\n ") is None and sanitize_snippet(None) is None and sanitize_snippet(12) is None


# -- capabilities ----------------------------------------------------------------------------------------------------------------------


def _context(*, document=DOCUMENT, sha=SHA, extra_ids=("ev-1",), options=None, with_source=True) -> ReviewCommandContext:
    source = source_document_evidence_option(document, sha)
    described = (field_evidence_option("ev-1", field_name="INVOICE_NUMBER", page_number=1, raw_text="INV-1"),)
    ids = set(extra_ids)

    if with_source:
        ids.add(source.reference_id)

    return ReviewCommandContext(
        tenant_id=TENANT, workflow_id=WORKFLOW, batch_id=BATCH, document_id=document, review_case_id=CASE,
        case_status=ReviewCaseStatus.IN_REVIEW, assigned_reviewer_id="rev-1", review_revision=2, workflow_revision=3,
        header_field_values=((F.INVOICE_NUMBER, "INV-1"),), known_invoice_line_numbers=(1,),
        available_evidence_reference_ids=tuple(sorted(ids)),
        evidence_options=((source,) + described) if options is None and with_source else (described if options is None else options),
    )


def _caps(context):
    return build_command_capabilities(
        actor=REVIEWER, context=context, detail=_detail(), normalized_payload=None, config=CONFIG, writes_enabled=True
    )["correction_policy"]


def test_capabilities_return_the_labelled_source_option_first():
    policy = _caps(_context())
    options = policy["evidence_options"]

    assert options[0] == {
        "reference_id": source_document_evidence_id(DOCUMENT, SHA), "evidence_type": "SOURCE_DOCUMENT",
        "label": SOURCE_DOCUMENT_LABEL, "page_number": None, "snippet": None,
    }
    assert options[1]["label"] == "Extracted evidence — Invoice Number, page 1" and options[1]["snippet"] == "INV-1"
    assert [o["reference_id"] for o in options] == sorted(policy["evidence_reference_ids"], key=lambda i: i != options[0]["reference_id"])
    assert set(policy["evidence_reference_ids"]) == {o["reference_id"] for o in options}


def test_an_id_without_a_description_gets_a_generic_label_and_an_undescribed_option_cannot_widen():
    rogue = field_evidence_option("ev-rogue", field_name="TOTAL_AMOUNT", page_number=1, raw_text="x")
    policy = _caps(_context(extra_ids=("ev-1", "ev-bare"), options=(rogue,), with_source=False))
    by_id = {o["reference_id"]: o for o in policy["evidence_options"]}

    assert by_id["ev-bare"]["label"] == "Evidence reference" and "ev-rogue" not in by_id  # not in the available set -> not offered
    assert set(by_id) == {"ev-1", "ev-bare"}


def test_the_capability_payload_exposes_no_location_credential_or_tenant():
    text = json.dumps(_caps(_context()), default=str)

    for forbidden in ("s3://", "object://", "artifact://", "tenants/", "/tmp", "amazonaws", str(TENANT), SHA, "AKIA"):
        assert forbidden not in text


# -- validation: accepted only for this case's document ------------------------------------------------------------------------------


def _command(context, *evidence) -> ReviewCommand:
    correction = ReviewFieldCorrection(
        field_name=F.SUPPLIER_NAME, line_number=None, previous_value=None, corrected_value="SuperStore",
        reason="Read from the paper invoice.", evidence_reference_ids=tuple(evidence),
    )

    return ReviewCommand(
        command_id=uuid.uuid5(uuid.NAMESPACE_URL, "m11e5"), idempotency_key="m11e5-correct-key-1", tenant_id=TENANT,
        workflow_id=WORKFLOW, batch_id=BATCH, document_id=context.document_id, review_case_id=CASE, actor=REVIEWER,
        action=ReviewAction.CORRECT, disposition=HumanReviewDisposition.CORRECTED,
        observed_review_revision=context.review_revision, observed_workflow_revision=context.workflow_revision,
        reason_codes=("REVIEWER_DECISION",), notes=None, corrections=(replace(correction),), requested_at=NOW,
    )


def _errors(context, *evidence):
    return validate_review_command(_command(context, *evidence), context, CONFIG)


def test_the_case_source_evidence_is_accepted_alone_and_with_extracted_evidence():
    context = _context()
    source = source_document_evidence_id(DOCUMENT, SHA)

    assert _errors(context, source) == () and _errors(context, source, "ev-1") == () and _errors(context, "ev-1") == ()


def test_another_documents_source_evidence_is_rejected():
    context = _context()
    other_document = source_document_evidence_id(uuid.uuid4(), SHA)
    other_hash = source_document_evidence_id(DOCUMENT, "c" * 64)

    assert "UNKNOWN_EVIDENCE_REFERENCE" in _errors(context, other_document)
    assert "UNKNOWN_EVIDENCE_REFERENCE" in _errors(context, other_hash)


@pytest.mark.parametrize("forged", [str(uuid.uuid4()), "ev-forged", SHA, str(DOCUMENT), "source", "", "SOURCE_DOCUMENT"])
def test_forged_and_free_form_evidence_ids_are_rejected(forged):
    errors = _errors(_context(), forged)

    assert "UNKNOWN_EVIDENCE_REFERENCE" in errors or "CORRECTION_EVIDENCE_REQUIRED" in errors


def test_a_mixed_valid_and_forged_list_is_rejected_and_evidence_is_still_required():
    source = source_document_evidence_id(DOCUMENT, SHA)

    assert "UNKNOWN_EVIDENCE_REFERENCE" in _errors(_context(), source, "ev-forged")
    assert "CORRECTION_EVIDENCE_REQUIRED" in _errors(_context())  # require_correction_evidence is not weakened
    assert CONFIG.require_correction_evidence is True


def test_a_case_without_a_usable_stored_hash_offers_no_source_evidence():
    context = _context(with_source=False)

    assert source_document_evidence_id(DOCUMENT, SHA) not in context.available_evidence_reference_ids
    assert "UNKNOWN_EVIDENCE_REFERENCE" in _errors(context, source_document_evidence_id(DOCUMENT, SHA))
