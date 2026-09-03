from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.llm_boundary import (
    LLMAudienceIntent,
    LLMBoundaryError,
    detect_raw_email_export,
    validate_domain_references,
    validate_llm_intent,
)
from app.schemas import AudienceIntent


VALID_TEXT = (
    "Please create an audience for promoting Class C. Include students who completed "
    "Class A within the last 90 days, have taken Class B, match our career advancement "
    "learner profile, and are eligible to receive marketing emails. Exclude anyone who "
    "has already enrolled in Class C. Manager is Jane Smith."
)


def candidate(**overrides):
    payload = {
        "request_type": "marketing_audience",
        "campaign_purpose": "Promote Class C",
        "target_course": "Class C",
        "completed_course": {"course": "Class A", "within_days": 90},
        "taken_courses": ["Class B"],
        "any_taken_courses": [],
        "any_taken_within_days": None,
        "learner_profile": "career_advancement",
        "marketing_consent_required": True,
        "active_account_required": True,
        "exclude_suppressed": True,
        "exclude_target_course": True,
        "raw_email_export": False,
        "manager": "Jane Smith",
        "confidence": 0.95,
    }
    payload.update(overrides)
    return payload


def test_valid_grounded_intent_is_accepted_and_copied_to_domain_model():
    intent = validate_llm_intent(VALID_TEXT, candidate())

    assert isinstance(intent, AudienceIntent)
    assert intent.target_course == "Class C"
    assert intent.completed_course.course == "Class A"
    assert intent.completed_course.within_days == 90
    assert intent.taken_courses == ["Class B"]
    assert intent.learner_profile == "career_advancement"
    assert intent.manager == "Jane Smith"


def test_closed_schema_rejects_unknown_fields():
    with pytest.raises(LLMBoundaryError) as exc:
        validate_llm_intent(VALID_TEXT, candidate(sql="DROP TABLE students"))

    assert exc.value.code == "schema_validation_failed"


def test_strict_schema_rejects_type_coercion():
    with pytest.raises(LLMBoundaryError) as exc:
        validate_llm_intent(VALID_TEXT, candidate(confidence="0.95"))

    assert exc.value.code == "schema_validation_failed"


def test_provider_schema_requires_all_fields():
    payload = candidate()
    payload.pop("confidence")

    with pytest.raises(ValidationError):
        LLMAudienceIntent.model_validate(payload)


def test_model_cannot_disable_system_owned_controls():
    with pytest.raises(LLMBoundaryError) as exc:
        validate_llm_intent(VALID_TEXT, candidate(marketing_consent_required=False))

    assert exc.value.code == "system_control_tampering"


def test_hallucinated_course_is_rejected_even_when_json_is_valid():
    with pytest.raises(LLMBoundaryError) as exc:
        validate_llm_intent(VALID_TEXT, candidate(taken_courses=["Class Z"]))

    assert exc.value.code == "ungrounded_criterion"


def test_grounding_uses_token_boundaries_not_substrings():
    text = "Promote Class C to students who completed Class AB in the last 90 days."
    payload = candidate(
        completed_course={"course": "Class A", "within_days": 90},
        taken_courses=[],
        learner_profile=None,
        manager=None,
    )

    with pytest.raises(LLMBoundaryError) as exc:
        validate_llm_intent(text, payload)

    assert exc.value.code == "ungrounded_criterion"


def test_hallucinated_manager_is_rejected_without_prefix_match():
    text = VALID_TEXT.replace("Jane Smith.", "Jane Smithson.")

    with pytest.raises(LLMBoundaryError) as exc:
        validate_llm_intent(text, candidate(manager="Jane Smith"))

    assert exc.value.code == "ungrounded_criterion"


def test_hallucinated_time_window_is_rejected():
    with pytest.raises(LLMBoundaryError) as exc:
        validate_llm_intent(
            VALID_TEXT,
            candidate(completed_course={"course": "Class A", "within_days": 30}),
        )

    assert exc.value.code == "ungrounded_time_window"


def test_implicit_last_year_is_grounded_as_365_days():
    text = "Promote Class C to students who completed Class A in the last year."
    payload = candidate(
        completed_course={"course": "Class A", "within_days": 365},
        taken_courses=[],
        learner_profile=None,
        manager=None,
    )

    intent = validate_llm_intent(text, payload)
    assert intent.completed_course.within_days == 365


def test_target_course_cannot_also_be_positive_selector():
    with pytest.raises(LLMBoundaryError) as exc:
        validate_llm_intent(VALID_TEXT, candidate(taken_courses=["Class C"]))

    assert exc.value.code == "contradictory_course_criteria"


def test_sensitive_request_type_fields_must_be_consistent():
    with pytest.raises(LLMBoundaryError) as exc:
        validate_llm_intent(
            VALID_TEXT,
            candidate(request_type="raw_export", raw_email_export=False),
        )

    assert exc.value.code == "inconsistent_sensitive_request_type"


def test_raw_email_export_is_deterministically_upgraded_if_model_misses_it():
    text = "Please export the email addresses for students in Class C to CSV. Manager is Jane Smith."
    payload = candidate(
        campaign_purpose="Class C outreach",
        completed_course=None,
        taken_courses=[],
        learner_profile=None,
        request_type="marketing_audience",
        raw_email_export=False,
    )

    intent = validate_llm_intent(text, payload)

    assert detect_raw_email_export(text) is True
    assert intent.request_type == "raw_export"
    assert intent.raw_email_export is True


def test_control_characters_are_rejected_at_provider_boundary():
    with pytest.raises(LLMBoundaryError) as exc:
        validate_llm_intent(VALID_TEXT, candidate(manager="Jane\x00Smith"))

    assert exc.value.code == "schema_validation_failed"


def test_authoritative_catalog_rejects_unknown_course_even_if_user_named_it():
    text = "Promote Class Z to students who took Class A."
    payload = candidate(
        campaign_purpose="Promote Class Z",
        target_course="Class Z",
        completed_course=None,
        taken_courses=["Class A"],
        learner_profile=None,
        manager=None,
    )
    intent = validate_llm_intent(text, payload)

    with pytest.raises(LLMBoundaryError) as exc:
        validate_domain_references(
            intent,
            known_courses={"Class A", "Class B", "Class C"},
            known_profiles={"career_advancement"},
        )

    assert exc.value.code == "unknown_course_reference"


def test_authoritative_catalog_rejects_unknown_profile():
    text = "Promote Class C to the mystery cohort learner profile."
    payload = candidate(
        target_course="Class C",
        completed_course=None,
        taken_courses=[],
        learner_profile="mystery_cohort",
        manager=None,
    )
    intent = validate_llm_intent(text, payload)

    with pytest.raises(LLMBoundaryError) as exc:
        validate_domain_references(
            intent,
            known_courses={"Class C"},
            known_profiles={"career_advancement"},
        )

    assert exc.value.code == "unknown_profile_reference"


def test_authoritative_catalog_canonicalizes_values_before_querying():
    text = "Promote class c to students who took class a and match career advancement."
    payload = candidate(
        campaign_purpose="Promote class c",
        target_course="class c",
        completed_course=None,
        taken_courses=["class a"],
        learner_profile="career advancement",
        manager=None,
    )
    intent = validate_llm_intent(text, payload)
    canonical = validate_domain_references(
        intent,
        known_courses={"Class A", "Class C"},
        known_profiles={"career_advancement"},
    )

    assert canonical.target_course == "Class C"
    assert canonical.taken_courses == ["Class A"]
    assert canonical.learner_profile == "career_advancement"
