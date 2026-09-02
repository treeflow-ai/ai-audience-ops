import json

from sqlalchemy.orm import Session

from app.llm import MockIntentParser
from app.services import AudienceService
from app.workflow import WorkflowState

VALID = "Please create an audience for promoting Class C. Include students who completed Class A within the last 90 days, have taken Class B, match our career advancement learner profile, and are eligible to receive marketing emails. Exclude anyone who has already enrolled in Class C. Manager is Jane Smith."
RAW = "Give me all emails of students who completed Class A in the last year. I want to export them to Excel for a promotional campaign."
LARGE = "Promote Class C to everyone who took Class A or Class B during the last 2 years. Manager is Jane Smith."


def test_parser_extracts_primary_demo_scenario():
    intent = MockIntentParser().parse(VALID)
    assert intent.target_course == "Class C"
    assert intent.completed_course.course == "Class A"
    assert intent.completed_course.within_days == 90
    assert "Class B" in intent.taken_courses
    assert intent.learner_profile == "career_advancement"
    assert intent.manager == "Jane Smith"
    assert intent.raw_email_export is False


def test_compliant_audience_is_ready_and_privacy_preserving(demo):
    settings, engine = demo
    with Session(engine) as session:
        item = AudienceService(session, settings).create_request(VALID, "Alex Rivera — Marketing", "mock_mailchimp")
        assert item.status is WorkflowState.READY_TO_SYNC
        assert item.eligible_count > 0
        assert item.members
        rendered = json.dumps(json.loads(item.policy_json))
        assert "DATA-03" in rendered


def test_raw_email_export_is_blocked_before_query(demo):
    settings, engine = demo
    with Session(engine) as session:
        item = AudienceService(session, settings).create_request(RAW, "Alex Rivera — Marketing", "mock_mailchimp")
        assert item.status is WorkflowState.BLOCKED
        assert item.eligible_count == 0
        assert not item.members
        assert any(e.event_type == "REQUEST_BLOCKED" for e in item.events)


def test_large_audience_requires_manager_approval(demo):
    settings, engine = demo
    with Session(engine) as session:
        service = AudienceService(session, settings)
        item = service.create_request(LARGE, "Alex Rivera — Marketing", "mock_constantcontact")
        assert item.status is WorkflowState.REVIEW_REQUIRED
        assert item.eligible_count > settings.approval_threshold
        approved = service.approve(item.id, "Jane Smith")
        assert approved.status is WorkflowState.APPROVED


def test_mock_sync_does_not_write_emails(demo):
    settings, engine = demo
    with Session(engine) as session:
        service = AudienceService(session, settings)
        item = service.create_request(VALID, "Alex Rivera — Marketing", "mock_mailchimp")
        synced = service.sync(item.id)
        assert synced.status is WorkflowState.SYNCED
        log = settings.mock_sync_log.read_text(encoding="utf-8")
        assert "@example.edu" not in log
        assert "WP-" not in log
        assert "recipient_ids" not in log
        assert "recipient_count" in log
