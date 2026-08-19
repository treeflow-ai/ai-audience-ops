from __future__ import annotations

import json
import re
from abc import ABC, abstractmethod

from .config import Settings
from .schemas import AudienceIntent, CourseRule


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
        raw_export = bool(
            re.search(
                r"\b(list|give|export|download|show|display|dump|extract|provide|return)\b.{0,50}"
                r"\b(email|emails|email address|email addresses)\b",
                lower,
            )
            or re.search(
                r"\b(email|emails|email address|email addresses)\b.{0,40}"
                r"\b(export|download|list|spreadsheet|excel|csv)\b",
                lower,
            )
        )

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
    """Optional real-LLM mode using the OpenAI Responses API.

    The model is used only for natural-language interpretation. It does not
    receive database credentials and never executes SQL.
    """

    def __init__(self, settings: Settings):
        self.settings = settings

    def parse(self, text: str) -> AudienceIntent:
        from openai import OpenAI

        client = OpenAI()
        schema_example = AudienceIntent(
            target_course="Class C",
            completed_course=CourseRule(course="Class A", within_days=90),
            taken_courses=["Class B"],
            learner_profile="career_advancement",
            manager="Jane Smith",
            confidence=0.95,
        ).model_dump()
        instructions = f"""
You convert a marketing audience request into constrained JSON.
Return JSON only, no markdown. Use course names like 'Class A'.
Do not invent criteria that were not requested, except these mandatory system
controls must remain true: marketing_consent_required, active_account_required,
exclude_suppressed, exclude_target_course.
Set raw_email_export=true and request_type='raw_export' if the user asks to list,
export, download, or expose raw student emails.
For 'last N years/months/days', convert the period to days.
Use null/empty values when unknown.
Example shape:
{json.dumps(schema_example, indent=2)}
""".strip()
        response = client.responses.create(
            model=self.settings.openai_model,
            instructions=instructions,
            input=text,
        )
        raw = response.output_text.strip()
        if raw.startswith("```"):
            raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw, flags=re.I | re.S)
        return AudienceIntent.model_validate(json.loads(raw))


def get_intent_parser(settings: Settings) -> IntentParser:
    if settings.llm_provider == "openai":
        return OpenAIIntentParser(settings)
    if settings.llm_provider == "mock":
        return MockIntentParser()
    raise ValueError(f"Unsupported LLM_PROVIDER: {settings.llm_provider}")
