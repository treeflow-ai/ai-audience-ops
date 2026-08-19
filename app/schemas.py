from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class CourseRule(BaseModel):
    course: str = Field(min_length=1, max_length=120)
    within_days: int | None = Field(default=None, gt=0, le=3650)


class AudienceIntent(BaseModel):
    request_type: Literal["marketing_audience", "raw_export"] = "marketing_audience"
    campaign_purpose: str = Field(default="course promotion", min_length=1, max_length=300)
    target_course: str | None = Field(default=None, max_length=120)
    completed_course: CourseRule | None = None
    taken_courses: list[str] = Field(default_factory=list, max_length=20)
    any_taken_courses: list[str] = Field(default_factory=list, max_length=20)
    any_taken_within_days: int | None = Field(default=None, gt=0, le=3650)
    learner_profile: str | None = Field(default=None, max_length=120)
    marketing_consent_required: bool = True
    active_account_required: bool = True
    exclude_suppressed: bool = True
    exclude_target_course: bool = True
    raw_email_export: bool = False
    manager: str | None = Field(default=None, max_length=120)
    confidence: float = Field(default=0.9, ge=0, le=1)


class PolicyCheck(BaseModel):
    code: str
    result: Literal["PASS", "WARN", "BLOCK", "REVIEW"]
    message: str


class FunnelStep(BaseModel):
    label: str
    before: int
    after: int
    removed: int


class RetrievedPolicy(BaseModel):
    policy_id: str
    title: str
    excerpt: str
    score: int


class CreateRequestPayload(BaseModel):
    text: str = Field(min_length=12, max_length=4000)
    requested_by: str = Field(default="Alex Rivera — Marketing", min_length=1, max_length=120)
    marketing_provider: Literal[
        "mock_mailchimp", "mock_constantcontact", "mailchimp", "constantcontact"
    ] = "mock_mailchimp"


class ApprovalPayload(BaseModel):
    approver: str = Field(default="Jane Smith", min_length=1, max_length=120)
