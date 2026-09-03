from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from .schemas import AudienceIntent, CourseRule

MAX_SERIALIZED_LLM_OUTPUT_BYTES = 16_384
MAX_LLM_ATTEMPTS = 2

_RAW_EMAIL_EXPORT_PATTERNS = (
    re.compile(
        r"\b(list|give|export|download|show|display|dump|extract|provide|return|reveal)\b.{0,50}"
        r"\b(email|emails|email address|email addresses)\b",
        re.I | re.S,
    ),
    re.compile(
        r"\b(email|emails|email address|email addresses)\b.{0,40}"
        r"\b(export|download|list|spreadsheet|excel|csv|dump|extract|reveal)\b",
        re.I | re.S,
    ),
)

_NUMBER_WORDS = {
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
    "ten": 10,
    "eleven": 11,
    "twelve": 12,
}


class LLMBoundaryError(ValueError):
    """Safe exception for model output rejected at the application boundary."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


class _LLMModel(BaseModel):
    # Provider-facing objects are closed and strict. All fields in the concrete
    # models intentionally have no defaults so Structured Outputs must return
    # the complete shape, including explicit nulls/empty lists.
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True, strict=True)


class LLMCourseRule(_LLMModel):
    course: str = Field(min_length=1, max_length=120)
    within_days: int | None = Field(ge=1, le=3650)

    @field_validator("course")
    @classmethod
    def validate_course(cls, value: str) -> str:
        return _safe_text(value, "course")


class LLMAudienceIntent(_LLMModel):
    request_type: Literal["marketing_audience", "raw_export"]
    campaign_purpose: str = Field(min_length=1, max_length=300)
    target_course: str | None = Field(max_length=120)
    completed_course: LLMCourseRule | None
    taken_courses: list[str] = Field(max_length=20)
    any_taken_courses: list[str] = Field(max_length=20)
    any_taken_within_days: int | None = Field(ge=1, le=3650)
    learner_profile: str | None = Field(max_length=120)
    marketing_consent_required: bool
    active_account_required: bool
    exclude_suppressed: bool
    exclude_target_course: bool
    raw_email_export: bool
    manager: str | None = Field(max_length=120)
    confidence: float = Field(ge=0, le=1)

    @field_validator("campaign_purpose", "target_course", "learner_profile", "manager")
    @classmethod
    def validate_text_fields(cls, value: str | None, info):
        if value is None:
            return None
        return _safe_text(value, info.field_name)

    @field_validator("taken_courses", "any_taken_courses")
    @classmethod
    def validate_course_lists(cls, values: list[str], info) -> list[str]:
        cleaned = [_safe_text(value, info.field_name) for value in values]
        if len({_normalize(value) for value in cleaned}) != len(cleaned):
            raise ValueError(f"{info.field_name} must not contain duplicates")
        return cleaned

    @model_validator(mode="after")
    def validate_shape_consistency(self) -> "LLMAudienceIntent":
        if self.any_taken_within_days is not None and not self.any_taken_courses:
            raise ValueError("any_taken_within_days requires any_taken_courses")
        return self


def detect_raw_email_export(text: str) -> bool:
    """Deterministic backstop for a security-sensitive intent classification."""

    return any(pattern.search(text) for pattern in _RAW_EMAIL_EXPORT_PATTERNS)


def validate_llm_intent(source_text: str, candidate: LLMAudienceIntent | dict) -> AudienceIntent:
    """Validate untrusted model output and copy only approved fields to the domain model."""

    try:
        parsed = (
            candidate
            if isinstance(candidate, LLMAudienceIntent)
            else LLMAudienceIntent.model_validate(candidate, strict=True)
        )
    except ValidationError as exc:
        raise LLMBoundaryError(
            "schema_validation_failed",
            "Model output did not match the required intent schema",
        ) from exc

    serialized_size = len(parsed.model_dump_json().encode("utf-8"))
    if serialized_size > MAX_SERIALIZED_LLM_OUTPUT_BYTES:
        raise LLMBoundaryError(
            "output_too_large",
            "Model output exceeded the allowed boundary size",
        )

    if not all(
        (
            parsed.marketing_consent_required,
            parsed.active_account_required,
            parsed.exclude_suppressed,
            parsed.exclude_target_course,
        )
    ):
        raise LLMBoundaryError(
            "system_control_tampering",
            "Model output attempted to disable application-owned governance controls",
        )

    _validate_grounding(source_text, parsed)
    _validate_semantic_consistency(parsed)

    # Raw-email export is security-sensitive. The model may make this result
    # stricter, but it can never downgrade the deterministic detector.
    raw_export = (
        detect_raw_email_export(source_text)
        or parsed.raw_email_export
        or parsed.request_type == "raw_export"
    )

    return AudienceIntent(
        request_type="raw_export" if raw_export else "marketing_audience",
        campaign_purpose=parsed.campaign_purpose,
        target_course=parsed.target_course,
        completed_course=(
            CourseRule(
                course=parsed.completed_course.course,
                within_days=parsed.completed_course.within_days,
            )
            if parsed.completed_course
            else None
        ),
        taken_courses=list(parsed.taken_courses),
        any_taken_courses=list(parsed.any_taken_courses),
        any_taken_within_days=parsed.any_taken_within_days,
        learner_profile=parsed.learner_profile,
        # These controls belong to application code, never to the model.
        marketing_consent_required=True,
        active_account_required=True,
        exclude_suppressed=True,
        exclude_target_course=True,
        raw_email_export=raw_export,
        manager=parsed.manager,
        confidence=parsed.confidence,
    )


def validate_domain_references(
    intent: AudienceIntent,
    *,
    known_courses: Iterable[str],
    known_profiles: Iterable[str],
) -> AudienceIntent:
    """Validate and canonicalize model-selected values against authoritative data."""

    course_catalog = {_normalize(value): value for value in known_courses if value}
    profile_catalog = {_normalize(value): value for value in known_profiles if value}

    def canonical_course(value: str | None) -> str | None:
        if value is None:
            return None
        canonical = course_catalog.get(_normalize(value))
        if canonical is None:
            raise LLMBoundaryError(
                "unknown_course_reference",
                "Intent references a course that is not present in the authoritative catalog",
            )
        return canonical

    canonical_profile = intent.learner_profile
    if intent.learner_profile is not None:
        canonical_profile = profile_catalog.get(_normalize(intent.learner_profile))
        if canonical_profile is None:
            raise LLMBoundaryError(
                "unknown_profile_reference",
                "Intent references a learner profile that is not present in the authoritative catalog",
            )

    return AudienceIntent(
        request_type=intent.request_type,
        campaign_purpose=intent.campaign_purpose,
        target_course=canonical_course(intent.target_course),
        completed_course=(
            CourseRule(
                course=canonical_course(intent.completed_course.course),
                within_days=intent.completed_course.within_days,
            )
            if intent.completed_course
            else None
        ),
        taken_courses=[canonical_course(value) for value in intent.taken_courses],
        any_taken_courses=[canonical_course(value) for value in intent.any_taken_courses],
        any_taken_within_days=intent.any_taken_within_days,
        learner_profile=canonical_profile,
        marketing_consent_required=True,
        active_account_required=True,
        exclude_suppressed=True,
        exclude_target_course=True,
        raw_email_export=intent.raw_email_export,
        manager=intent.manager,
        confidence=intent.confidence,
    )


def safe_boundary_error(exc: Exception) -> LLMBoundaryError:
    """Collapse validation details to stable, non-sensitive boundary errors."""

    if isinstance(exc, LLMBoundaryError):
        return exc
    if isinstance(exc, ValidationError):
        return LLMBoundaryError(
            "schema_validation_failed",
            "Model output did not match the required intent schema",
        )
    return LLMBoundaryError(
        "invalid_model_output",
        "Model output failed boundary validation",
    )


def _validate_grounding(source_text: str, intent: LLMAudienceIntent) -> None:
    grounded_fields: list[tuple[str, str | None]] = [
        ("target_course", intent.target_course),
        ("learner_profile", intent.learner_profile),
        ("manager", intent.manager),
    ]
    if intent.completed_course:
        grounded_fields.append(("completed_course.course", intent.completed_course.course))
    grounded_fields.extend(("taken_courses", value) for value in intent.taken_courses)
    grounded_fields.extend(("any_taken_courses", value) for value in intent.any_taken_courses)

    for field_name, value in grounded_fields:
        if value and not _value_is_grounded(source_text, value):
            raise LLMBoundaryError(
                "ungrounded_criterion",
                f"Model output contained an ungrounded selection criterion: {field_name}",
            )

    allowed_windows = _extract_duration_days(source_text)
    requested_windows: list[tuple[str, int]] = []
    if intent.completed_course and intent.completed_course.within_days is not None:
        requested_windows.append(("completed_course.within_days", intent.completed_course.within_days))
    if intent.any_taken_within_days is not None:
        requested_windows.append(("any_taken_within_days", intent.any_taken_within_days))

    for field_name, days in requested_windows:
        if days not in allowed_windows:
            raise LLMBoundaryError(
                "ungrounded_time_window",
                f"Model output contained an ungrounded time window: {field_name}",
            )


def _validate_semantic_consistency(intent: LLMAudienceIntent) -> None:
    if (intent.request_type == "raw_export") != intent.raw_email_export:
        raise LLMBoundaryError(
            "inconsistent_sensitive_request_type",
            "Model output contained inconsistent raw-export classification fields",
        )

    target = _normalize(intent.target_course or "")
    if not target:
        return

    positive_courses = [_normalize(value) for value in intent.taken_courses + intent.any_taken_courses]
    if intent.completed_course:
        positive_courses.append(_normalize(intent.completed_course.course))

    if target in positive_courses:
        raise LLMBoundaryError(
            "contradictory_course_criteria",
            "Target course cannot also be a positive selector while target-course exclusion is mandatory",
        )


def _value_is_grounded(source_text: str, value: str) -> bool:
    source = _normalize(source_text)
    candidate = _normalize(value)
    if not candidate:
        return False

    # Course values have an accepted alias ("Class X" vs "course X"), but a
    # bare token is not enough. This avoids substring grounding such as
    # accepting "Class A" merely because the request contains "Class AB".
    course_match = re.fullmatch(r"class ([a-z0-9][a-z0-9_-]{0,40})", candidate)
    if course_match:
        token = re.escape(course_match.group(1))
        return bool(re.search(rf"(?<!\w)(?:class|course)\s+{token}(?![\w-])", source))

    return _contains_phrase(source, candidate)


def _contains_phrase(source: str, candidate: str) -> bool:
    return bool(re.search(rf"(?<![\w-]){re.escape(candidate)}(?![\w-])", source))


def _extract_duration_days(text: str) -> set[int]:
    normalized = _normalize(text)
    values: set[int] = set()

    explicit = re.compile(
        r"\b(\d{1,4}|" + "|".join(_NUMBER_WORDS) + r")\s*(day|days|month|months|year|years)\b"
    )
    for match in explicit.finditer(normalized):
        raw_number, unit = match.groups()
        number = int(raw_number) if raw_number.isdigit() else _NUMBER_WORDS[raw_number]
        days = _duration_to_days(number, unit)
        if 1 <= days <= 3650:
            values.add(days)

    implicit = re.compile(r"\b(?:the\s+)?(?:last|past|previous)\s+(day|month|year)\b")
    for match in implicit.finditer(normalized):
        values.add(_duration_to_days(1, match.group(1)))

    return values


def _duration_to_days(number: int, unit: str) -> int:
    if unit.startswith("day"):
        return number
    if unit.startswith("month"):
        return number * 30
    return number * 365


def _safe_text(value: str, field_name: str) -> str:
    value = value.strip()
    if not value:
        raise ValueError(f"{field_name} must not be blank")
    if any(unicodedata.category(ch) == "Cc" for ch in value):
        raise ValueError(f"{field_name} must not contain control characters")
    return value


def _normalize(value: str) -> str:
    value = unicodedata.normalize("NFKC", value).casefold()
    value = value.replace("_", " ")
    value = re.sub(r"[^\w\s-]", " ", value)
    return re.sub(r"\s+", " ", value).strip()
