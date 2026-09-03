from __future__ import annotations

import re
from abc import ABC, abstractmethod

from pydantic import ValidationError

from .config import Settings
from .llm_boundary import (
    LLMAudienceIntent,
    LLMBoundaryError,
    MAX_LLM_ATTEMPTS,
    detect_raw_email_export,
    safe_boundary_error,
    validate_llm_intent,
)
from .schemas import AudienceIntent, CourseRule


MAX_LLM_OUTPUT_TOKENS = 1200
OPENAI_TIMEOUT_SECONDS = 20.0
OPENAI_TRANSPORT_RETRIES = 1


def _course(label: str) -> str:
    label = label.strip().upper()
    return f"Class {label[-1]}" if label[-1].isalpha() else label.title()


class IntentParser(ABC):
    @abstractmethod
    def parse(self, text: str) -> AudienceIntent:
        raise NotImplementedError


class MockIntentParser(IntentParser):
    """Deterministic parser for a credential-free public demo.

    It intentionally handles a narrow, documented request language. The real-LLM
    adapter can replace it without changing the policy/query workflow.
    """

    def parse(self, text: str) -> AudienceIntent:
        lower = text.lower()
        raw_export = detect_raw_email_export(text)

        manager = None
        m = re.search(r"manager(?:\s+is|\s*:)?\s+([A-Z][a-z]+\s+[A-Z][a-z]+)", text, re.I)
        if m:
            manager = m.group(1)

        target = None
        patterns = [
            r"promot(?:e|ing)\s+(?:the\s+)?(?:course\s+|class\s+)?([A-Z])\b",
            r"target(?:\s+course)?\s*[:=]?\s*(?:class\s+)?([A-Z])\b",
            r"campaign\s+for\s+(?:class\s+)?([A-Z])\b",
        ]
        for pattern in patterns:
            m = re.search(pattern, text, re.I)
            if m:
                target = _course(m.group(1))
                break

        completed_rule = None
        m = re.search(r"completed?\s+(?:course\s+|class\s+)?([A-Z])", text, re.I)
        if m:
            days = None
            window = re.search(r"(?:within|in)\s+(?:the\s+)?last\s+(\d+)\s+(day|days|month|months|year|years)", text, re.I)
            if window:
                n = int(window.group(1))
                unit = window.group(2).lower()
                days = n if unit.startswith("day") else n * 30 if unit.startswith("month") else n * 365
            completed_rule = CourseRule(course=_course(m.group(1)), within_days=days)

        # Special handling for "Class A or Class B" / "everyone who took ...".
        any_taken: list[str] = []
        or_match = re.search(
            r"(?:took|taken|enrolled in)\s+(?:class\s+)?([A-Z])\s+or\s+(?:class\s+)?([A-Z])",
            text,
            re.I,
        )
        if or_match:
            any_taken = [_course(or_match.group(1)), _course(or_match.group(2))]

        any_days = None
        if any_taken:
            window = re.search(r"last\s+(\d+)\s+(day|days|month|months|year|years)", text, re.I)
            if window:
                n = int(window.group(1))
                unit = window.group(2).lower()
                any_days = n if unit.startswith("day") else n * 30 if unit.startswith("month") else n * 365

        taken_courses: list[str] = []
        # Avoid double-counting the OR case and the completed-course phrase.
        for match in re.finditer(r"(?:took|taken|have taken|enrolled in)\s+(?:class\s+)?([A-Z])", text, re.I):
            course_name = _course(match.group(1))
            if course_name not in any_taken and (completed_rule is None or course_name != completed_rule.course):
                taken_courses.append(course_name)
        taken_courses = list(dict.fromkeys(taken_courses))
        # A target-course mention in an exclusion clause is not a positive eligibility criterion.
        if target in taken_courses:
            taken_courses.remove(target)

        profile = None
        if "career advancement" in lower or "career_advancement" in lower:
            profile = "career_advancement"
        elif "skill building" in lower or "skill_building" in lower:
            profile = "skill_building"
        elif "certification" in lower:
            profile = "certification"

        confidence = 0.96 if target and (completed_rule or any_taken or taken_courses) else 0.84
        if raw_export:
            confidence = max(confidence, 0.93)

        return AudienceIntent(
            request_type="raw_export" if raw_export else "marketing_audience",
            campaign_purpose=f"Promote {target}" if target else "marketing outreach",
            target_course=target,
            completed_course=completed_rule,
            taken_courses=taken_courses,
            any_taken_courses=any_taken,
            any_taken_within_days=any_days,
            learner_profile=profile,
            raw_email_export=raw_export,
            manager=manager,
            confidence=confidence,
        )


class OpenAIIntentParser(IntentParser):
    """Real-LLM parser with a fail-closed application trust boundary.

    Structured Outputs enforce the provider-facing shape. The application then
    independently validates grounding, semantic consistency, system-owned
    controls, and sensitive export intent before constructing ``AudienceIntent``.
    """

    def __init__(self, settings: Settings):
        self.settings = settings

    def parse(self, text: str) -> AudienceIntent:
        from openai import OpenAI

        client = OpenAI(
            timeout=OPENAI_TIMEOUT_SECONDS,
            max_retries=OPENAI_TRANSPORT_RETRIES,
        )
        instructions = """
You convert a marketing audience request into a constrained intent object.
Extract only criteria explicitly present in the user's request. Never invent a
course, learner profile, manager, time window, or audience filter.

The following application-owned controls MUST be true in your output:
marketing_consent_required, active_account_required, exclude_suppressed,
exclude_target_course.

Set raw_email_export=true and request_type='raw_export' when the user asks to
list, export, download, reveal, or otherwise expose raw student email addresses.
For explicit time windows, convert years to 365 days and months to 30 days.
Use null or empty lists when a value is not stated. Confidence is descriptive
only and never grants permission.
""".strip()

        last_boundary_error: LLMBoundaryError | None = None
        for attempt in range(MAX_LLM_ATTEMPTS):
            attempt_instructions = instructions
            if attempt:
                attempt_instructions += (
                    "\n\nThe previous attempt failed deterministic boundary validation. "
                    "Re-read the original request and return only explicitly grounded criteria."
                )

            try:
                response = client.responses.parse(
                    model=self.settings.openai_model,
                    instructions=attempt_instructions,
                    input=text,
                    text_format=LLMAudienceIntent,
                    max_output_tokens=MAX_LLM_OUTPUT_TOKENS,
                    store=False,
                )

                if getattr(response, "status", None) == "incomplete":
                    raise LLMBoundaryError(
                        "incomplete_model_response",
                        "Model response was incomplete and was not accepted",
                    )

                candidate = getattr(response, "output_parsed", None)
                if candidate is None:
                    # Covers refusals and responses with no parseable structured
                    # output. Provider text is intentionally not copied to errors.
                    raise LLMBoundaryError(
                        "missing_structured_output",
                        "Model did not return an acceptable structured intent",
                    )

                return validate_llm_intent(text, candidate)
            except (LLMBoundaryError, ValidationError) as exc:
                last_boundary_error = safe_boundary_error(exc)

        assert last_boundary_error is not None
        raise last_boundary_error

def get_intent_parser(settings: Settings) -> IntentParser:
    if settings.llm_provider == "openai":
        return OpenAIIntentParser(settings)
    if settings.llm_provider == "mock":
        return MockIntentParser()
    raise ValueError(f"Unsupported LLM_PROVIDER: {settings.llm_provider}")
