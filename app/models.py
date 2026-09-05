from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship
from sqlalchemy.types import TypeDecorator

from .workflow import WorkflowState


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class WorkflowStateType(TypeDecorator):
    """Persist WorkflowState as VARCHAR while exposing the enum to application code."""

    impl = String(32)
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        if isinstance(value, WorkflowState):
            return value.value
        return WorkflowState(value).value

    def process_result_value(self, value, dialect):
        if value is None:
            return None
        return WorkflowState(value)


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
    status: Mapped[WorkflowState] = mapped_column(WorkflowStateType(), index=True)
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
    sync_job: Mapped["SyncJob | None"] = relationship(
        back_populates="request", cascade="all, delete-orphan", uselist=False
    )


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


class SyncJob(Base):
    """One durable logical sync per governed audience request.

    request_id is unique on purpose: retries must resume the same logical job,
    never create another downstream release for the same approved snapshot.
    """

    __tablename__ = "sync_jobs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    request_id: Mapped[int] = mapped_column(
        ForeignKey("audience_requests.id", ondelete="CASCADE"), unique=True, index=True
    )
    idempotency_key: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    provider: Mapped[str] = mapped_column(String(40), index=True)
    audience_fingerprint: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(24), default="PENDING", index=True)
    batch_size: Mapped[int] = mapped_column(Integer)
    total_recipients: Mapped[int] = mapped_column(Integer)
    total_batches: Mapped[int] = mapped_column(Integer)
    completed_recipients: Mapped[int] = mapped_column(Integer, default=0)
    completed_batches: Mapped[int] = mapped_column(Integer, default=0)
    retry_round: Mapped[int] = mapped_column(Integer, default=0)
    worker_token: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    external_segment_id: Mapped[str | None] = mapped_column(String(160), nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)

    request: Mapped[AudienceRequest] = relationship(back_populates="sync_job")
    batches: Mapped[list["SyncBatch"]] = relationship(
        back_populates="job", cascade="all, delete-orphan", order_by="SyncBatch.batch_index"
    )


class SyncBatch(Base):
    """Persistent checkpoint for a deterministic slice of the approved audience."""

    __tablename__ = "sync_batches"
    __table_args__ = (
        UniqueConstraint("job_id", "batch_index", name="uq_sync_job_batch"),
        UniqueConstraint("idempotency_key", name="uq_sync_batch_idempotency"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("sync_jobs.id", ondelete="CASCADE"), index=True)
    batch_index: Mapped[int] = mapped_column(Integer)
    start_offset: Mapped[int] = mapped_column(Integer)
    size: Mapped[int] = mapped_column(Integer)
    idempotency_key: Mapped[str] = mapped_column(String(64), index=True)
    status: Mapped[str] = mapped_column(String(24), default="PENDING", index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    round_attempts: Mapped[int] = mapped_column(Integer, default=0)
    synced_count: Mapped[int] = mapped_column(Integer, default=0)
    external_operation_id: Mapped[str | None] = mapped_column(String(160), nullable=True)
    external_segment_id: Mapped[str | None] = mapped_column(String(160), nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    job: Mapped[SyncJob] = relationship(back_populates="batches")
