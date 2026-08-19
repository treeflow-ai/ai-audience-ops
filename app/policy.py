from __future__ import annotations

from .schemas import AudienceIntent, PolicyCheck


class PolicyEngine:
    def __init__(self, approval_threshold: int = 5000):
        self.approval_threshold = approval_threshold

    def pre_query(self, intent: AudienceIntent) -> list[PolicyCheck]:
        checks: list[PolicyCheck] = []

        if intent.raw_email_export:
            checks.append(PolicyCheck(
                code="DATA-03",
                result="BLOCK",
                message="Marketing users may create governed segments but may not export raw student email addresses.",
            ))
        else:
            checks.append(PolicyCheck(
                code="DATA-03",
                result="PASS",
                message="No raw student email export requested; identifiers remain inside the governed workflow.",
            ))

        if intent.target_course:
            checks.append(PolicyCheck(
                code="CAMPAIGN-02",
                result="PASS",
                message=f"Campaign purpose is tied to a specific target course: {intent.target_course}.",
            ))
        else:
            checks.append(PolicyCheck(
                code="CAMPAIGN-02",
                result="BLOCK",
                message="A target course is required before a course-promotion audience can be created.",
            ))

        if intent.marketing_consent_required:
            checks.append(PolicyCheck(
                code="MARKETING-01",
                result="PASS",
                message="Active marketing consent is a mandatory system filter.",
            ))

        if intent.exclude_target_course:
            checks.append(PolicyCheck(
                code="CAMPAIGN-07",
                result="PASS",
                message="Students already enrolled in the promoted course will be excluded.",
            ))

        return checks

    def post_query(self, intent: AudienceIntent, eligible_count: int) -> list[PolicyCheck]:
        checks: list[PolicyCheck] = []
        if eligible_count > self.approval_threshold:
            if intent.manager:
                checks.append(PolicyCheck(
                    code="CAMPAIGN-04",
                    result="REVIEW",
                    message=(
                        f"Audience size {eligible_count:,} exceeds the {self.approval_threshold:,} recipient "
                        f"auto-release threshold; manager approval is required from {intent.manager}."
                    ),
                ))
            else:
                checks.append(PolicyCheck(
                    code="CAMPAIGN-05",
                    result="BLOCK",
                    message=(
                        f"Audience size {eligible_count:,} exceeds the approval threshold, but no manager was identified."
                    ),
                ))
        else:
            checks.append(PolicyCheck(
                code="CAMPAIGN-04",
                result="PASS",
                message=f"Audience size {eligible_count:,} is within the auto-release threshold.",
            ))
        return checks
