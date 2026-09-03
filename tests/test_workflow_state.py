from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.models import AudienceRequest, Base
from app.workflow import WorkflowState


def _new_request(status: WorkflowState) -> AudienceRequest:
    return AudienceRequest(
        request_key="AUD-ENUM-0001",
        raw_request="enum test",
        requested_by="test",
        marketing_provider="mock_mailchimp",
        status=status,
        intent_json="{}",
        policy_json="[]",
        funnel_json="[]",
        retrieved_policy_json="[]",
    )


def test_workflow_state_behavior():
    assert WorkflowState.REVIEW_REQUIRED.requires_approval
    assert not WorkflowState.READY_TO_SYNC.requires_approval
    assert WorkflowState.READY_TO_SYNC.can_sync
    assert WorkflowState.APPROVED.can_sync
    assert WorkflowState.SYNC_FAILED.can_sync
    assert not WorkflowState.BLOCKED.can_sync


def test_workflow_state_round_trips_as_enum_and_keeps_string_storage():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        request = _new_request(WorkflowState.READY_TO_SYNC)
        session.add(request)
        session.commit()
        request_id = request.id

    with engine.connect() as connection:
        raw_status = connection.exec_driver_sql(
            "SELECT status FROM audience_requests WHERE id = ?", (request_id,)
        ).scalar_one()
    assert raw_status == WorkflowState.READY_TO_SYNC.value

    with Session(engine) as session:
        request = session.get(AudienceRequest, request_id)
        assert request is not None
        assert request.status is WorkflowState.READY_TO_SYNC


def test_existing_string_statuses_are_loaded_as_enum():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        request = _new_request(WorkflowState.EVALUATING)
        session.add(request)
        session.commit()
        request_id = request.id

    with engine.begin() as connection:
        connection.exec_driver_sql(
            "UPDATE audience_requests SET status = ? WHERE id = ?",
            (WorkflowState.APPROVED.value, request_id),
        )

    with Session(engine) as session:
        request = session.get(AudienceRequest, request_id)
        assert request is not None
        assert request.status is WorkflowState.APPROVED
