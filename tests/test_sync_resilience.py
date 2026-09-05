from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.adapters.marketing import PermanentMarketingAdapterError, SyncResult
from app.config import Settings
from app.models import AudienceMember, AudienceRequest, Base, Student, SyncBatch, SyncJob
from app.services import AudienceService, MarketingSyncError
from app.workflow import WorkflowState


def _service(session: Session, settings: Settings) -> AudienceService:
    # sync() does not use parser/retrieval/policy dependencies; construct the
    # service directly so this test focuses only on durable sync semantics.
    service = object.__new__(AudienceService)
    service.session = session
    service.settings = settings
    return service


def _approved_request(session: Session, count: int = 5) -> AudienceRequest:
    request = AudienceRequest(
        request_key="AUD-RESILIENCE-0001",
        raw_request="approved resilience test",
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
            external_id=f"S-{i:04d}",
            email=f"student{i}@example.edu",
            first_name="Test",
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


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        database_url="sqlite+pysqlite:///:memory:",
        mock_sync_log=tmp_path / "sync.jsonl",
        sync_batch_size=2,
        sync_max_attempts=3,
        sync_retry_backoff_seconds=0,
        sync_lease_seconds=1,
    )


def test_workflow_state_round_trips_as_enum():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        request = _approved_request(session, count=0)
        request_id = request.id
    with engine.connect() as connection:
        raw = connection.exec_driver_sql(
            "SELECT status FROM audience_requests WHERE id = ?", (request_id,)
        ).scalar_one()
        assert raw == "APPROVED"
    with Session(engine) as session:
        request = session.get(AudienceRequest, request_id)
        assert request is not None
        assert request.status is WorkflowState.APPROVED


def test_repeated_sync_reuses_same_job_and_does_not_replay_mock_batches(tmp_path: Path):
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    settings = _settings(tmp_path)

    with Session(engine) as session:
        request = _approved_request(session, count=5)
        service = _service(session, settings)
        synced = service.sync(request.id)
        assert synced.status is WorkflowState.SYNCED
        assert synced.sync_job is not None
        assert synced.sync_job.total_batches == 3
        assert synced.sync_job.completed_batches == 3
        first_job_id = synced.sync_job.id
        first_key = synced.sync_job.idempotency_key

        replay = service.sync(request.id)
        assert replay.status is WorkflowState.SYNCED
        assert replay.sync_job is not None
        assert replay.sync_job.id == first_job_id
        assert replay.sync_job.idempotency_key == first_key

    lines = settings.mock_sync_log.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 3


def test_failed_batch_recovery_preserves_successful_checkpoints(tmp_path: Path, monkeypatch):
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    settings = _settings(tmp_path)

    class FailsOnSecondBatch:
        def __init__(self):
            self.calls: list[str] = []

        def sync_batch(self, audience_key, recipients, *, idempotency_key, **kwargs):
            self.calls.append(idempotency_key)
            if len(self.calls) == 2:
                raise PermanentMarketingAdapterError("simulated permanent failure")
            return SyncResult(
                provider="mock_mailchimp",
                external_segment_id=f"seg-{audience_key}",
                synced_count=len(recipients),
                detail="ok",
                external_operation_id=f"op-{idempotency_key[:8]}",
            )

    first_adapter = FailsOnSecondBatch()
    monkeypatch.setattr("app.services.get_marketing_adapter", lambda provider, settings: first_adapter)

    with Session(engine) as session:
        request = _approved_request(session, count=5)
        service = _service(session, settings)
        with pytest.raises(MarketingSyncError, match="simulated permanent failure"):
            service.sync(request.id)

        failed = service.get_request(request.id)
        assert failed.status is WorkflowState.SYNC_FAILED
        assert failed.sync_job is not None
        statuses = [batch.status for batch in failed.sync_job.batches]
        assert statuses == ["SUCCEEDED", "FAILED", "PENDING"]
        first_batch_key = failed.sync_job.batches[0].idempotency_key

        second_calls: list[str] = []

        class SuccessfulAdapter:
            def sync_batch(self, audience_key, recipients, *, idempotency_key, **kwargs):
                second_calls.append(idempotency_key)
                return SyncResult(
                    provider="mock_mailchimp",
                    external_segment_id=f"seg-{audience_key}",
                    synced_count=len(recipients),
                    detail="ok",
                    external_operation_id=f"op-{idempotency_key[:8]}",
                )

        monkeypatch.setattr("app.services.get_marketing_adapter", lambda provider, settings: SuccessfulAdapter())
        recovered = service.sync(request.id)
        assert recovered.status is WorkflowState.SYNCED
        assert recovered.sync_job is not None
        assert recovered.sync_job.completed_batches == 3
        assert recovered.sync_job.completed_recipients == 5
        assert first_batch_key not in second_calls
        assert len(second_calls) == 2

        jobs = list(session.scalars(select(SyncJob)).all())
        assert len(jobs) == 1
        batches = list(session.scalars(select(SyncBatch).order_by(SyncBatch.batch_index)).all())
        assert [b.status for b in batches] == ["SUCCEEDED", "SUCCEEDED", "SUCCEEDED"]


def test_retryable_failure_retries_same_batch_key_in_same_job(tmp_path: Path, monkeypatch):
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    settings = _settings(tmp_path)

    class RetryOnceAdapter:
        def __init__(self):
            self.calls: list[str] = []
            self.failed = False

        def sync_batch(self, audience_key, recipients, *, idempotency_key, **kwargs):
            self.calls.append(idempotency_key)
            if not self.failed:
                self.failed = True
                from app.adapters.marketing import RetryableMarketingAdapterError

                raise RetryableMarketingAdapterError("temporary 429")
            return SyncResult(
                provider="mock_mailchimp",
                external_segment_id=f"seg-{audience_key}",
                synced_count=len(recipients),
                detail="ok",
                external_operation_id=f"op-{idempotency_key[:8]}",
            )

    adapter = RetryOnceAdapter()
    monkeypatch.setattr("app.services.get_marketing_adapter", lambda provider, settings: adapter)

    with Session(engine) as session:
        request = _approved_request(session, count=3)
        synced = _service(session, settings).sync(request.id)
        assert synced.status is WorkflowState.SYNCED
        assert synced.sync_job is not None
        first = synced.sync_job.batches[0]
        assert first.attempts == 2
        assert adapter.calls[0] == adapter.calls[1] == first.idempotency_key


def test_job_lease_blocks_duplicate_worker_until_expired(tmp_path: Path):
    from datetime import datetime, timedelta, timezone

    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    settings = _settings(tmp_path)

    with Session(engine) as session:
        request = _approved_request(session, count=2)
        service = _service(session, settings)
        member_ids = service._member_ids(request.id)
        job = service._ensure_sync_job(request, member_ids, service._audience_fingerprint(member_ids))

        assert service._acquire_job_lease(job.id, "worker-a") is True
        assert service._acquire_job_lease(job.id, "worker-b") is False

        job = session.get(SyncJob, job.id)
        assert job is not None
        job.lease_expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        session.commit()
        assert service._acquire_job_lease(job.id, "worker-b") is True


def test_failed_provider_can_be_retargeted_only_before_any_side_effect(tmp_path: Path):
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    settings = _settings(tmp_path)

    with Session(engine) as session:
        request = _approved_request(session, count=2)
        request.marketing_provider = "mailchimp"
        session.commit()
        service = _service(session, settings)

        with pytest.raises(MarketingSyncError, match="Real marketing sync is disabled"):
            service.sync(request.id)

        failed = service.get_request(request.id)
        assert failed.status is WorkflowState.SYNC_FAILED
        assert failed.sync_job is not None
        assert failed.sync_job.completed_batches == 0
        assert all(batch.external_operation_id is None for batch in failed.sync_job.batches)

        failed.marketing_provider = "mock_mailchimp"
        session.commit()
        recovered = service.sync(request.id)
        assert recovered.status is WorkflowState.SYNCED
        assert recovered.sync_job is not None
        assert recovered.sync_job.provider == "mock_mailchimp"
