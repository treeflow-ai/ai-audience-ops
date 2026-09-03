from itertools import product

import pytest
from sqlalchemy.orm import Session

from app.services import AudienceService, MarketingSyncError
from app.workflow import WorkflowState


VALID = (
    "Please create an audience for promoting Class C. Include students who completed "
    "Class A within the last 90 days, have taken Class B, match our career advancement "
    "learner profile, and are eligible to receive marketing emails. Exclude anyone who "
    "has already enrolled in Class C. Manager is Jane Smith."
)
LARGE = (
    "Promote Class C to everyone who took Class A or Class B during the last 2 years. "
    "Manager is Jane Smith."
)
LARGE_NO_MANAGER = (
    "Promote Class C to everyone who took Class A or Class B during the last 2 years."
)
RAW = (
    "Give me all emails of students who completed Class A in the last year. "
    "I want to export them to Excel for a promotional campaign."
)


LEGAL_TRANSITIONS = {
    (WorkflowState.EVALUATING, WorkflowState.BLOCKED),
    (WorkflowState.EVALUATING, WorkflowState.REVIEW_REQUIRED),
    (WorkflowState.EVALUATING, WorkflowState.READY_TO_SYNC),
    (WorkflowState.REVIEW_REQUIRED, WorkflowState.APPROVED),
    (WorkflowState.READY_TO_SYNC, WorkflowState.SYNC_FAILED),
    (WorkflowState.READY_TO_SYNC, WorkflowState.SYNCED),
    (WorkflowState.APPROVED, WorkflowState.SYNC_FAILED),
    (WorkflowState.APPROVED, WorkflowState.SYNCED),
    (WorkflowState.SYNC_FAILED, WorkflowState.SYNC_FAILED),
    (WorkflowState.SYNC_FAILED, WorkflowState.SYNCED),
}


@pytest.mark.parametrize("source,target", product(WorkflowState, repeat=2))
def test_workflow_state_transition_matrix(source: WorkflowState, target: WorkflowState):
    if (source, target) in LEGAL_TRANSITIONS:
        assert source.transition_to(target) is target
    else:
        with pytest.raises(ValueError, match="Illegal workflow state transition"):
            source.transition_to(target)


def _record_transitions(monkeypatch):
    seen: list[tuple[WorkflowState, WorkflowState]] = []
    original = WorkflowState.transition_to

    def recording_transition(self: WorkflowState, target: WorkflowState) -> WorkflowState:
        seen.append((self, target))
        return original(self, target)

    monkeypatch.setattr(WorkflowState, "transition_to", recording_transition)
    return seen


def test_all_business_transition_paths_use_transition_to(demo, monkeypatch):
    settings, engine = demo
    seen = _record_transitions(monkeypatch)

    with Session(engine) as session:
        service = AudienceService(session, settings)

        ready = service.create_request(VALID, "test", "mock_mailchimp")
        assert ready.status is WorkflowState.READY_TO_SYNC
        synced = service.sync(ready.id)
        assert synced.status is WorkflowState.SYNCED

        blocked = service.create_request(LARGE_NO_MANAGER, "test", "mock_mailchimp")
        assert blocked.status is WorkflowState.BLOCKED

        review = service.create_request(LARGE, "test", "mock_mailchimp")
        assert review.status is WorkflowState.REVIEW_REQUIRED
        approved = service.approve(review.id, "Jane Smith")
        assert approved.status is WorkflowState.APPROVED
        approved_synced = service.sync(approved.id)
        assert approved_synced.status is WorkflowState.SYNCED

        ready_failure = service.create_request(VALID, "test", "mailchimp")
        assert ready_failure.status is WorkflowState.READY_TO_SYNC
        with pytest.raises(MarketingSyncError, match="Real marketing sync is disabled"):
            service.sync(ready_failure.id)
        failed = service.get_request(ready_failure.id)
        assert failed.status is WorkflowState.SYNC_FAILED

        with pytest.raises(MarketingSyncError, match="Real marketing sync is disabled"):
            service.sync(failed.id)
        failed_again = service.get_request(failed.id)
        assert failed_again.status is WorkflowState.SYNC_FAILED

        failed_again.marketing_provider = "mock_mailchimp"
        session.commit()
        retried = service.sync(failed_again.id)
        assert retried.status is WorkflowState.SYNCED

        review_failure = service.create_request(LARGE, "test", "mailchimp")
        approved_failure = service.approve(review_failure.id, "Jane Smith")
        assert approved_failure.status is WorkflowState.APPROVED
        with pytest.raises(MarketingSyncError, match="Real marketing sync is disabled"):
            service.sync(approved_failure.id)
        assert service.get_request(approved_failure.id).status is WorkflowState.SYNC_FAILED

    assert set(seen) == LEGAL_TRANSITIONS


def test_pre_query_block_is_an_initial_state_not_a_transition(demo, monkeypatch):
    settings, engine = demo
    seen = _record_transitions(monkeypatch)

    with Session(engine) as session:
        blocked = AudienceService(session, settings).create_request(
            RAW, "test", "mock_mailchimp"
        )

    assert blocked.status is WorkflowState.BLOCKED
    assert seen == []
