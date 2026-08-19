import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import Settings
from app.llm import MockIntentParser
from app.main import create_app
from app.models import AudienceMember, AuditEvent, Student
from app.schemas import AudienceIntent
from app.seed import seed_synthetic_data
from app.services import AudienceService

RAW = "Give me all emails of students who completed Class A in the last year. I want to export them to Excel for a promotional campaign."
VALID = "Please create an audience for promoting Class C. Include students who completed Class A within the last 90 days, have taken Class B, match our career advancement learner profile, and are eligible to receive marketing emails. Exclude anyone who has already enrolled in Class C. Manager is Jane Smith."


def test_blocked_form_redirect_no_detached_instance_error(demo):
    settings, engine = demo
    app = create_app(settings=settings, engine=engine)
    with TestClient(app) as client:
        response = client.post(
            "/requests",
            data={
                "text": RAW,
                "requested_by": "Alex Rivera — Marketing",
                "marketing_provider": "mock_mailchimp",
            },
            follow_redirects=False,
        )
        assert response.status_code == 303
        assert response.headers["location"].startswith("/requests/")

        detail = client.get(response.headers["location"])
        assert detail.status_code == 200
        assert "BLOCKED" in detail.text
        assert "Request blocked" in detail.text


def test_blocked_api_response_serializes_after_commit(demo):
    settings, engine = demo
    app = create_app(settings=settings, engine=engine)
    with TestClient(app) as client:
        response = client.post(
            "/api/requests",
            json={
                "text": RAW,
                "requested_by": "Alex Rivera — Marketing",
                "marketing_provider": "mock_mailchimp",
            },
        )
        assert response.status_code == 201
        payload = response.json()
        assert payload["status"] == "BLOCKED"
        assert payload["eligible_count"] == 0
        assert any(event["event_type"] == "REQUEST_BLOCKED" for event in payload["audit_events"])


@pytest.mark.parametrize(
    "text",
    [
        "Download all student email addresses to CSV for outreach.",
        "Show me the emails of all students in a spreadsheet.",
        "Provide student email addresses as an Excel export.",
        "I need an email list exported to Excel.",
    ],
)
def test_mock_parser_recognizes_raw_email_exposure_variants(text):
    intent = MockIntentParser().parse(text)
    assert intent.raw_email_export is True
    assert intent.request_type == "raw_export"


class UnsafeControlParser:
    def parse(self, _text: str) -> AudienceIntent:
        return AudienceIntent(
            target_course="Class C",
            marketing_consent_required=False,
            active_account_required=False,
            exclude_suppressed=False,
            exclude_target_course=False,
            confidence=0.99,
        )


def test_application_forces_mandatory_governance_controls(demo):
    settings, engine = demo
    with Session(engine) as session:
        service = AudienceService(session, settings)
        service.parser = UnsafeControlParser()
        item = service.create_request(
            "Promote Class C to an eligible audience.",
            "Alex Rivera — Marketing",
            "mock_mailchimp",
        )
        intent = json.loads(item.intent_json)
        assert intent["marketing_consent_required"] is True
        assert intent["active_account_required"] is True
        assert intent["exclude_suppressed"] is True
        assert intent["exclude_target_course"] is True

        member_ids = [member.student_id for member in item.members]
        if member_ids:
            violating = session.scalar(
                select(func.count())
                .select_from(Student)
                .where(
                    Student.id.in_(member_ids),
                    (
                        Student.marketing_consent.is_(False)
                        | Student.active.is_(False)
                        | Student.email_suppressed.is_(True)
                    ),
                )
            )
            assert violating == 0


def test_force_seed_clears_request_children_and_can_be_repeated(demo):
    settings, engine = demo
    with Session(engine) as session:
        service = AudienceService(session, settings)
        first = service.create_request(VALID, "Alex Rivera — Marketing", "mock_mailchimp")
        assert first.members
        assert first.events

        seed_synthetic_data(session, count=settings.synthetic_student_count, force=True)

        assert session.scalar(select(func.count()).select_from(AudienceMember)) == 0
        assert session.scalar(select(func.count()).select_from(AuditEvent)) == 0

        second = AudienceService(session, settings).create_request(
            RAW, "Alex Rivera — Marketing", "mock_mailchimp"
        )
        event_types = [event.event_type for event in second.events]
        assert event_types.count("REQUEST_SUBMITTED") == 1
        assert event_types.count("REQUEST_BLOCKED") == 1
