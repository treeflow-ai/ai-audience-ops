from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError
from sqlalchemy.orm import Session

from app.adapters.learndash import LearnDashClient
from app.adapters.marketing import ConstantContactAdapter, Recipient
from app.config import Settings
from app.schemas import AudienceIntent, CourseRule, CreateRequestPayload
from app.services import AudienceService

VALID = (
    "Please create an audience for promoting Class C. Include students who completed "
    "Class A within the last 90 days, have taken Class B, match our career advancement "
    "learner profile, and are eligible to receive marketing emails. Exclude anyone who "
    "has already enrolled in Class C. Manager is Jane Smith."
)
LARGE = "Promote Class C to everyone who took Class A or Class B during the last 2 years. Manager is Jane Smith."


def test_settings_reads_environment_when_instance_is_created(monkeypatch, tmp_path: Path):
    first = tmp_path / "first.db"
    second = tmp_path / "second.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{first}")
    assert Settings().database_url.endswith("first.db")
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{second}")
    assert Settings().database_url.endswith("second.db")


def test_settings_rejects_unknown_llm_provider():
    with pytest.raises(ValueError, match="LLM_PROVIDER"):
        Settings(llm_provider="opena1")


def test_schema_rejects_invalid_windows_confidence_and_oversized_input():
    with pytest.raises(ValidationError):
        CourseRule(course="Class A", within_days=0)
    with pytest.raises(ValidationError):
        AudienceIntent(confidence=1.5)
    with pytest.raises(ValidationError):
        CreateRequestPayload(text="x" * 4001)


def test_wrong_manager_cannot_approve_large_audience(demo):
    settings, engine = demo
    with Session(engine) as session:
        service = AudienceService(session, settings)
        item = service.create_request(LARGE, "Alex Rivera — Marketing", "mock_mailchimp")
        assert item.status == "REVIEW_REQUIRED"
        with pytest.raises(ValueError, match="identified manager"):
            service.approve(item.id, "Not Jane")


def test_real_sync_is_disabled_before_any_vendor_call(demo):
    settings, engine = demo
    with Session(engine) as session:
        service = AudienceService(session, settings)
        item = service.create_request(VALID, "Alex Rivera — Marketing", "mailchimp")
        assert item.status == "READY_TO_SYNC"
        with pytest.raises(RuntimeError, match="Real marketing sync is disabled"):
            service.sync(item.id)
        failed = service.get_request(item.id)
        assert failed.status == "SYNC_FAILED"
        assert "Real marketing sync is disabled" in (failed.sync_detail or "")


class _FakeResponse:
    def __init__(self, payload: dict):
        self._payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return self._payload


class _ConstantContactClient:
    def __init__(self, *args, **kwargs):
        self.get_calls = 0

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def post(self, path: str, json: dict):
        assert path == "/activities/contacts_json_import"
        assert json["import_data"][0]["email"] == "student@example.edu"
        assert json["list_ids"] == ["list-123"]
        return _FakeResponse({"activity_id": "activity-1", "state": "initialized"})

    def get(self, path: str):
        assert path == "/activities/activity-1"
        self.get_calls += 1
        return _FakeResponse(
            {
                "activity_id": "activity-1",
                "state": "completed",
                "percent_done": 100,
                "completed_at": "2026-08-18T12:00:00Z",
                "activity_errors": [],
                "status": {"error_count": 0},
            }
        )


def test_constant_contact_waits_for_activity_completion(monkeypatch, tmp_path: Path):
    from app.adapters import marketing

    monkeypatch.setattr(marketing.httpx, "Client", _ConstantContactClient)
    settings = Settings(
        allow_real_marketing_sync=True,
        real_sync_max_recipients=10,
        constant_contact_access_token="token",
        constant_contact_list_id="list-123",
        constant_contact_activity_timeout_seconds=2,
        mock_sync_log=tmp_path / "sync.jsonl",
    )
    result = ConstantContactAdapter(settings).sync(
        "AUD-TEST",
        [Recipient("WP-1", "student@example.edu", "Avery", "Chen")],
    )
    assert result.synced_count == 1
    assert "completed successfully" in result.detail


class _LearnDashClient:
    def __init__(self, *args, **kwargs):
        pass

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def get(self, url: str, params: dict):
        assert params["per_page"] == 2
        if params["page"] == 1:
            return _FakeResponse({"course_ids": [101, 102]})
        if params["page"] == 2:
            return _FakeResponse({"course_ids": [103]})
        return _FakeResponse({"course_ids": []})


def test_learndash_user_courses_are_paginated(monkeypatch):
    from app.adapters import learndash

    monkeypatch.setattr(learndash.httpx, "Client", _LearnDashClient)
    settings = Settings(learndash_base_url="https://example.com")
    client = LearnDashClient(settings)
    assert client.list_user_courses(7, per_page=2) == [101, 102, 103]


def test_api_reports_disabled_real_sync_as_gateway_error(demo):
    from fastapi.testclient import TestClient
    from app.main import create_app

    settings, engine = demo
    app = create_app(settings=settings, engine=engine)
    with TestClient(app) as client:
        created = client.post(
            "/api/requests",
            json={
                "text": VALID,
                "requested_by": "Alex Rivera — Marketing",
                "marketing_provider": "mailchimp",
            },
        )
        assert created.status_code == 201
        request_id = created.json()["id"]
        response = client.post(f"/api/requests/{request_id}/sync")
        assert response.status_code == 502
        assert "Real marketing sync is disabled" in response.json()["detail"]


def test_app_recovers_from_courses_only_partial_initialization(tmp_path: Path):
    from fastapi.testclient import TestClient
    from sqlalchemy import func, select
    from sqlalchemy.orm import Session

    from app.db import build_engine, init_db
    from app.main import create_app
    from app.models import Course, Student

    db_path = tmp_path / "partial.db"
    settings = Settings(
        database_url=f"sqlite:///{db_path}",
        synthetic_student_count=25,
        policy_dir=Path("policies"),
        mock_sync_log=tmp_path / "sync.jsonl",
    )
    engine = build_engine(settings.database_url)
    init_db(engine)
    with Session(engine) as session:
        session.add(Course(id=101, external_id="LD-101", name="Class A"))
        session.commit()

    # The lifespan should not mistake the presence of one course for a fully
    # seeded database. The idempotent seed path rebuilds the missing demo data.
    with TestClient(create_app(settings=settings, engine=engine)) as client:
        assert client.get("/health").status_code == 200

    with Session(engine) as session:
        assert session.scalar(select(func.count()).select_from(Student)) == 25
