from __future__ import annotations

import random
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from .models import AudienceMember, AudienceRequest, AuditEvent, Course, Enrollment, Student

FIRST_NAMES = [
    "Avery", "Jordan", "Taylor", "Morgan", "Riley", "Casey", "Drew", "Jamie",
    "Cameron", "Parker", "Quinn", "Reese", "Skyler", "Emerson", "Rowan", "Kai",
]
LAST_NAMES = [
    "Chen", "Smith", "Johnson", "Garcia", "Patel", "Nguyen", "Brown", "Davis",
    "Wilson", "Martinez", "Lee", "Clark", "Lewis", "Walker", "Hall", "Young",
]
PROFILES = [
    ("career_advancement", 0.46),
    ("skill_building", 0.28),
    ("certification", 0.16),
    ("exploration", 0.10),
]
COURSES = [
    (101, "LD-101", "Class A"),
    (102, "LD-102", "Class B"),
    (103, "LD-103", "Class C"),
    (104, "LD-104", "Class D"),
]


def _profile(rng: random.Random) -> str:
    x = rng.random()
    total = 0.0
    for name, weight in PROFILES:
        total += weight
        if x <= total:
            return name
    return PROFILES[-1][0]


def seed_synthetic_data(session: Session, count: int = 12000, force: bool = False) -> None:
    if session.scalar(select(Student.id).limit(1)) is not None and not force:
        return

    if force:
        # Bulk deletes bypass ORM cascades, so remove dependent rows explicitly.
        # This keeps reset/seed idempotent even when requests already have
        # audience members and audit events.
        session.execute(delete(AuditEvent))
        session.execute(delete(AudienceMember))
        session.execute(delete(AudienceRequest))
        session.execute(delete(Enrollment))
        session.execute(delete(Student))
        session.execute(delete(Course))
        session.commit()

    existing_course_ids = set(session.scalars(select(Course.id)).all())
    for course_id, external_id, name in COURSES:
        if course_id not in existing_course_ids:
            session.add(Course(id=course_id, external_id=external_id, name=name))
    session.flush()

    rng = random.Random(20260817)
    now = datetime.now(timezone.utc)
    students: list[Student] = []
    enrollments: list[Enrollment] = []

    for i in range(1, count + 1):
        first = FIRST_NAMES[(i * 7) % len(FIRST_NAMES)]
        last = LAST_NAMES[(i * 11) % len(LAST_NAMES)]
        student = Student(
            id=i,
            external_id=f"WP-{100000+i}",
            email=f"student{i:05d}@example.edu",
            first_name=first,
            last_name=last,
            learner_profile=_profile(rng),
            marketing_consent=rng.random() < 0.86,
            email_suppressed=rng.random() < 0.04,
            active=rng.random() < 0.97,
        )
        students.append(student)

        # Course A: highly adopted; completion dates intentionally span one year so
        # the 90-day demo still produces a meaningful but selective audience.
        if rng.random() < 0.90:
            completed = rng.random() < 0.78
            if completed:
                completion_days_ago = rng.randint(1, 365)
                completed_at = now - timedelta(days=completion_days_ago)
                enrolled_at = completed_at - timedelta(days=rng.randint(7, 120))
                status = "completed"
            else:
                enrolled_at = now - timedelta(days=rng.randint(1, 730))
                completed_at = None
                status = "in_progress"
            enrollments.append(Enrollment(student_id=i, course_id=101, enrolled_at=enrolled_at, completed_at=completed_at, status=status))

        # Course B: common companion course.
        if rng.random() < 0.75:
            enrolled_at = now - timedelta(days=rng.randint(1, 700))
            completed = rng.random() < 0.60
            completed_at = enrolled_at + timedelta(days=rng.randint(5, 90)) if completed else None
            if completed_at and completed_at > now:
                completed_at = now - timedelta(days=1)
            enrollments.append(Enrollment(student_id=i, course_id=102, enrolled_at=enrolled_at, completed_at=completed_at, status="completed" if completed else "in_progress"))

        # Class C is the promoted target. Existing students must be excluded.
        if rng.random() < 0.28:
            enrolled_at = now - timedelta(days=rng.randint(1, 620))
            enrollments.append(Enrollment(student_id=i, course_id=103, enrolled_at=enrolled_at, completed_at=None, status="enrolled"))

        if rng.random() < 0.30:
            enrolled_at = now - timedelta(days=rng.randint(1, 500))
            enrollments.append(Enrollment(student_id=i, course_id=104, enrolled_at=enrolled_at, completed_at=None, status="enrolled"))

        if len(students) >= 1000:
            session.add_all(students)
            session.flush()
            session.add_all(enrollments)
            session.flush()
            students.clear()
            enrollments.clear()

    if students:
        session.add_all(students)
        session.flush()
        session.add_all(enrollments)
        session.flush()

    # Commit the synthetic dataset as one transaction so an interrupted seed
    # does not leave a partially populated database that looks initialized on
    # the next startup.
    session.commit()
