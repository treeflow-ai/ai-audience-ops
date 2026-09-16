from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.config import Settings
from app.models import AudienceMember, AudienceRequest, Base, Student
from app.services import AudienceService, MarketingSyncError
from app.workflow import WorkflowState


def _service(session: Session, settings: Settings) -> AudienceService:
    service = object.__new__(AudienceService)
    service.session = session
    service.settings = settings
    return service


def _approved_request(session: Session, count: int = 5) -> AudienceRequest:
    request = AudienceRequest(
        request_key="AUD-FAILURE-DEMO-0001",
        raw_request="approved failure recovery demo",
        requested_by="test",
        marketing_provider="mock_mailchimp",
        status=WorkflowState.APPROVED,
        eligible_count=count,
        intent_json="{}",
        policy_json="[]",
        funnel_json="[]",
        retrieved_policy_json="[]",
        approved_by="Jane Smith",
    )
    session.add(request)
    session.flush()

    for i in range(count):
        student = Student(
            external_id=f"FD-{i:04d}",
            email=f"failure-demo-{i}@example.edu",
            first_name="Failure",
            last_name=str(i),
            learner_profile="career_advancement",
            marketing_consent=True,
            email_suppressed=False,
            active=True,
        )
        session.add(student)
        session.flush()
        session.add(AudienceMember(request_id=request.id, student_id=student.id))

    session.commit()
    return request


def _settings(tmp_path: Path, *, demo_mode: bool = True) -> Settings:
    return Settings(
        database_url="sqlite+pysqlite:///:memory:",
        mock_sync_log=tmp_path / "sync.jsonl",
        sync_batch_size=2,
        sync_max_attempts=3,
        sync_retry_backoff_seconds=0,
        sync_lease_seconds=1,
        demo_mode=demo_mode,
    )


def test_demo_transient_failure_retries_same_batch_and_succeeds(tmp_path: Path):
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        settings = _settings(tmp_path)
        request = _approved_request(session)
        service = _service(session, settings)
        synced = service.sync(request.id, demo_failure="transient_once")

        assert synced.status is WorkflowState.SYNCED
        assert synced.sync_job is not None
        assert synced.sync_job.retry_round == 0
        assert [batch.status for batch in synced.sync_job.batches] == [
            "SUCCEEDED",
            "SUCCEEDED",
            "SUCCEEDED",
        ]
        assert [batch.attempts for batch in synced.sync_job.batches] == [1, 2, 1]
        assert any(event.event_type == "SYNC_BATCH_RETRY" for event in synced.events)


def test_demo_partial_failure_preserves_checkpoint_on_operator_retry(tmp_path: Path):
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        settings = _settings(tmp_path)
        request = _approved_request(session)
        service = _service(session, settings)

        with pytest.raises(MarketingSyncError):
            service.sync(request.id, demo_failure="terminal_once")

        failed = service.get_request(request.id)
        assert failed.status is WorkflowState.SYNC_FAILED
        assert failed.sync_job is not None
        assert [batch.status for batch in failed.sync_job.batches] == [
            "SUCCEEDED",
            "FAILED",
            "PENDING",
        ]

        job_key = failed.sync_job.idempotency_key
        first_batch_key = failed.sync_job.batches[0].idempotency_key
        first_batch_attempts = failed.sync_job.batches[0].attempts

        recovered = service.sync(request.id)

        assert recovered.status is WorkflowState.SYNCED
        assert recovered.sync_job is not None
        assert recovered.sync_job.retry_round == 1
        assert recovered.sync_job.idempotency_key == job_key
        assert recovered.sync_job.batches[0].idempotency_key == first_batch_key
        assert recovered.sync_job.batches[0].attempts == first_batch_attempts == 1
        assert recovered.sync_job.batches[1].attempts == 2
        assert recovered.sync_job.batches[2].attempts == 1
        assert any(event.event_type == "SYNC_RETRY_REQUESTED" for event in recovered.events)


def test_failure_injection_is_disabled_outside_demo_mode(tmp_path: Path):
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        settings = _settings(tmp_path, demo_mode=False)
        request = _approved_request(session)
        service = _service(session, settings)

        with pytest.raises(ValueError, match="DEMO_MODE"):
            service.sync(request.id, demo_failure="transient_once")
