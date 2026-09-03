from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy.orm import Session

from app.config import Settings
from app.db import build_engine, init_db
from app.seed import seed_synthetic_data
from app.services import AudienceService
from app.workflow import WorkflowState

SCENARIOS = [
    (
        "Compliant audience",
        "Please create an audience for promoting Class C. Include students who completed Class A within the last 90 days, have taken Class B, match our career advancement learner profile, and are eligible to receive marketing emails. Exclude anyone who has already enrolled in Class C. Manager is Jane Smith.",
    ),
    (
        "Raw-email request",
        "Give me all emails of students who completed Class A in the last year. I want to export them to Excel for a promotional campaign.",
    ),
    (
        "Large audience / approval",
        "Promote Class C to everyone who took Class A or Class B during the last 2 years. Manager is Jane Smith.",
    ),
]


def main() -> None:
    settings = Settings()
    engine = build_engine(settings.database_url)
    init_db(engine)
    with Session(engine) as session:
        seed_synthetic_data(session, count=settings.synthetic_student_count)
        service = AudienceService(session, settings)
        for title, prompt in SCENARIOS:
            item = service.create_request(prompt, "Alex Rivera — Marketing", "mock_mailchimp")
            print(f"\n=== {title} ===")
            print(f"{item.request_key}: status={item.status.value}, risk={item.risk_level}, eligible={item.eligible_count:,}")
            if item.status is WorkflowState.REVIEW_REQUIRED:
                item = service.approve(item.id, "Jane Smith")
                print(f"approved -> {item.status.value}")
            if item.status in {WorkflowState.READY_TO_SYNC, WorkflowState.APPROVED}:
                item = service.sync(item.id)
                print(f"sync -> {item.status.value}, destination={item.external_segment_id}")


if __name__ == "__main__":
    main()
