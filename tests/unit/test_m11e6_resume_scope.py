"""M11E.6: "resume already requested" belongs to the CURRENT decision of the CURRENT review case, never to the workflow.

Live defect: a downstream review case (opened when the resumed stages still needed review) could be approved but never resumed,
because any `WORKFLOW_RESUME_REQUESTED` event anywhere in the workflow timeline made `already_requested` true."""

from __future__ import annotations

import uuid
from dataclasses import replace

import pytest

from ap_agent.models.interface import (
    HumanReviewDisposition,
    InterfaceTimelineEvent,
    ReviewCaseStatus,
)
from tests.unit.test_review_capabilities import NOW, _actor, _caps, _context, _detail, _Decision

pytestmark = pytest.mark.unit


def _resume_event():
    return InterfaceTimelineEvent(
        event_id="e", event_type="WORKFLOW_RESUME_REQUESTED", stage=None, status="IN_PROGRESS",
        actor_id="rev-1", message="m", occurred_at=NOW,
    )


def _resolved(decision, *, requested=()):
    return replace(_context(ReviewCaseStatus.RESOLVED, "rev-1"), resume_requested_decision_ids=tuple(requested))


def test_a_resume_requested_for_an_earlier_case_does_not_make_the_current_case_ineligible():
    earlier = _Decision(HumanReviewDisposition.CORRECTED)  # case A's decision, already resumed
    current = _Decision(HumanReviewDisposition.APPROVED)  # case B's decision
    # The workflow timeline holds case A's resume event ...
    detail = _detail([current], [_resume_event()])
    # ... and the durable association names only case A's decision, which is not one of case B's.
    caps = _caps(context=_resolved(current, requested=[str(earlier.decision_id)]), detail=detail)

    assert caps["resume"]["already_requested"] is False
    assert caps["resume"]["eligible"] is True and caps["resume"]["ineligible_reason"] is None
    assert caps["resume"]["decision_id"] == current.decision_id


def test_a_resume_timeline_event_alone_no_longer_blocks():
    decision = _Decision(HumanReviewDisposition.APPROVED)
    caps = _caps(context=_resolved(decision), detail=_detail([decision], [_resume_event(), _resume_event()]))

    assert caps["resume"]["eligible"] is True


@pytest.mark.parametrize("disposition", [HumanReviewDisposition.APPROVED, HumanReviewDisposition.CORRECTED])
def test_a_resume_for_this_cases_own_decision_blocks_a_duplicate(disposition):
    decision = _Decision(disposition)
    caps = _caps(context=_resolved(decision, requested=[str(decision.decision_id)]), detail=_detail([decision]))

    assert caps["resume"]["already_requested"] is True
    assert caps["resume"]["eligible"] is False and caps["resume"]["ineligible_reason"] == "RESUME_ALREADY_REQUESTED"


def test_only_the_latest_decision_counts_and_an_old_decisions_resume_does_not_grant_or_block():
    old, latest = _Decision(HumanReviewDisposition.CORRECTED), _Decision(HumanReviewDisposition.APPROVED)

    # the old decision of this case was resumed; the latest has not been
    free = _caps(context=_resolved(latest, requested=[str(old.decision_id)]), detail=_detail([old, latest]))
    blocked = _caps(context=_resolved(latest, requested=[str(latest.decision_id)]), detail=_detail([old, latest]))

    assert free["resume"]["eligible"] is True and blocked["resume"]["ineligible_reason"] == "RESUME_ALREADY_REQUESTED"


def test_foreign_ids_cannot_grant_eligibility_or_change_other_rules():
    decision = _Decision(HumanReviewDisposition.APPROVED)
    foreign = [str(uuid.uuid4()), "not-a-uuid", ""]

    assert _caps(context=_resolved(decision, requested=foreign), detail=_detail([decision]))["resume"]["eligible"] is True

    # ineligibility reasons that do not depend on resume history are unchanged
    assert _caps(actor=_actor(actor_id="rev-2"), context=_resolved(decision, requested=foreign), detail=_detail([decision]))["resume"][
        "ineligible_reason"
    ] == "DECISION_ACTOR_MISMATCH"
    assert _caps(context=replace(_resolved(decision), case_status=ReviewCaseStatus.IN_REVIEW), detail=_detail([decision]))["resume"][
        "ineligible_reason"
    ] == "RESOLVED_CASE_REQUIRED"
    assert _caps(context=_resolved(decision), detail=_detail([]))["resume"]["ineligible_reason"] == "REVIEW_DECISION_REQUIRED"


def test_a_case_without_a_decision_is_never_marked_requested():
    caps = _caps(context=_resolved(None, requested=[str(uuid.uuid4())]), detail=_detail([]))

    assert caps["resume"]["already_requested"] is False and caps["resume"]["decision_id"] is None
