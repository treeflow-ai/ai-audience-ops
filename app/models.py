from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class Student(Base):
    __tablename__ = "students"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    external_id: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    first_name: Mapped[str] = mapped_column(String(80))
    last_name: Mapped[str] = mapped_column(String(80))
    learner_profile: Mapped[str] = mapped_column(String(64), index=True)
    marketing_consent: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    email_suppressed: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)

    enrollments: Mapped[list["Enrollment"]] = relationship(back_populates="student", cascade="all, delete-orphan")


class Course(Base):
    __tablename__ = "courses"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    external_id: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(120), unique=True, index=True)

    enrollments: Mapped[list["Enrollment"]] = relationship(back_populates="course", cascade="all, delete-orphan")


class Enrollment(Base):
    __tablename__ = "enrollments"
    __table_args__ = (UniqueConstraint("student_id", "course_id", name="uq_student_course"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    student_id: Mapped[int] = mapped_column(ForeignKey("students.id", ondelete="CASCADE"), index=True)
    course_id: Mapped[int] = mapped_column(ForeignKey("courses.id", ondelete="CASCADE"), index=True)
    enrolled_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    status: Mapped[str] = mapped_column(String(24), default="enrolled", index=True)

    student: Mapped[Student] = relationship(back_populates="enrollments")
    course: Mapped[Course] = relationship(back_populates="enrollments")


class AudienceRequest(Base):
    __tablename__ = "audience_requests"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    request_key: Mapped[str] = mapped_column(String(40), unique=True, index=True)
    raw_request: Mapped[str] = mapped_column(Text)
    requested_by: Mapped[str] = mapped_column(String(120))
    manager: Mapped[str | None] = mapped_column(String(120), nullable=True)
    marketing_provider: Mapped[str] = mapped_column(String(40), default="mock_mailchimp")
    status: Mapped[str] = mapped_column(String(32), index=True)
    risk_level: Mapped[str] = mapped_column(String(16), default="LOW")
    confidence: Mapped[int] = mapped_column(Integer, default=0)
    eligible_count: Mapped[int] = mapped_column(Integer, default=0)
    intent_json: Mapped[str] = mapped_column(Text)
    policy_json: Mapped[str] = mapped_column(Text)
    funnel_json: Mapped[str] = mapped_column(Text)
    retrieved_policy_json: Mapped[str] = mapped_column(Text)
    approved_by: Mapped[str | None] = mapped_column(String(120), nullable=True)
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    external_segment_id: Mapped[str | None] = mapped_column(String(120), nullable=True)
    sync_detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)

    members: Mapped[list["AudienceMember"]] = relationship(back_populates="request", cascade="all, delete-orphan")
    events: Mapped[list["AuditEvent"]] = relationship(back_populates="request", cascade="all, delete-orphan")


class AudienceMember(Base):
    __tablename__ = "audience_members"
    __table_args__ = (UniqueConstraint("request_id", "student_id", name="uq_request_student"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    request_id: Mapped[int] = mapped_column(ForeignKey("audience_requests.id", ondelete="CASCADE"), index=True)
    student_id: Mapped[int] = mapped_column(ForeignKey("students.id", ondelete="CASCADE"), index=True)

    request: Mapped[AudienceRequest] = relationship(back_populates="members")
    student: Mapped[Student] = relationship()


class AuditEvent(Base):
    __tablename__ = "audit_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    request_id: Mapped[int] = mapped_column(ForeignKey("audience_requests.id", ondelete="CASCADE"), index=True)
    event_type: Mapped[str] = mapped_column(String(50), index=True)
    actor: Mapped[str] = mapped_column(String(120))
    detail: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)

    request: Mapped[AudienceRequest] = relationship(back_populates="events")
