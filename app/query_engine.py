from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import Course, Enrollment, Student
from .schemas import AudienceIntent, FunnelStep


class AudienceQueryEngine:
    def __init__(self, session: Session):
        self.session = session

    def _course_id(self, name: str) -> int | None:
        return self.session.scalar(select(Course.id).where(Course.name == name))

    def _enrolled_ids(self, course: str, since_days: int | None = None, completed_only: bool = False) -> set[int]:
        course_id = self._course_id(course)
        if course_id is None:
            return set()
        stmt = select(Enrollment.student_id).where(Enrollment.course_id == course_id)
        if completed_only:
            stmt = stmt.where(Enrollment.completed_at.is_not(None))
            if since_days:
                cutoff = datetime.now(timezone.utc) - timedelta(days=since_days)
                stmt = stmt.where(Enrollment.completed_at >= cutoff)
        elif since_days:
            cutoff = datetime.now(timezone.utc) - timedelta(days=since_days)
            stmt = stmt.where(Enrollment.enrolled_at >= cutoff)
        return set(self.session.scalars(stmt).all())

    @staticmethod
    def _step(label: str, current: set[int], allowed: set[int], invert: bool = False) -> tuple[set[int], FunnelStep]:
        before = len(current)
        next_ids = current - allowed if invert else current & allowed
        after = len(next_ids)
        return next_ids, FunnelStep(label=label, before=before, after=after, removed=before - after)

    def run(self, intent: AudienceIntent) -> tuple[list[int], list[FunnelStep]]:
        current = set(self.session.scalars(select(Student.id)).all())
        funnel = [FunnelStep(label="Initial student population", before=len(current), after=len(current), removed=0)]

        if intent.completed_course:
            allowed = self._enrolled_ids(
                intent.completed_course.course,
                since_days=intent.completed_course.within_days,
                completed_only=True,
            )
            label = f"Completed {intent.completed_course.course}"
            if intent.completed_course.within_days:
                label += f" within {intent.completed_course.within_days} days"
            current, step = self._step(label, current, allowed)
            funnel.append(step)

        for course in intent.taken_courses:
            allowed = self._enrolled_ids(course)
            current, step = self._step(f"Has taken {course}", current, allowed)
            funnel.append(step)

        if intent.any_taken_courses:
            allowed: set[int] = set()
            for course in intent.any_taken_courses:
                allowed |= self._enrolled_ids(course, since_days=intent.any_taken_within_days)
            label = "Took any of " + ", ".join(intent.any_taken_courses)
            if intent.any_taken_within_days:
                label += f" within {intent.any_taken_within_days} days"
            current, step = self._step(label, current, allowed)
            funnel.append(step)

        if intent.learner_profile:
            allowed = set(self.session.scalars(
                select(Student.id).where(Student.learner_profile == intent.learner_profile)
            ).all())
            current, step = self._step(f"Learner profile = {intent.learner_profile}", current, allowed)
            funnel.append(step)

        # Mandatory governance controls are system-owned, not delegated to the LLM.
        if intent.marketing_consent_required:
            allowed = set(self.session.scalars(select(Student.id).where(Student.marketing_consent.is_(True))).all())
            current, step = self._step("Active marketing consent", current, allowed)
            funnel.append(step)

        if intent.active_account_required:
            allowed = set(self.session.scalars(select(Student.id).where(Student.active.is_(True))).all())
            current, step = self._step("Active student account", current, allowed)
            funnel.append(step)

        if intent.exclude_suppressed:
            suppressed = set(self.session.scalars(select(Student.id).where(Student.email_suppressed.is_(True))).all())
            current, step = self._step("Exclude suppressed emails", current, suppressed, invert=True)
            funnel.append(step)

        if intent.exclude_target_course and intent.target_course:
            enrolled_target = self._enrolled_ids(intent.target_course)
            current, step = self._step(f"Exclude existing {intent.target_course} enrollment", current, enrolled_target, invert=True)
            funnel.append(step)

        return sorted(current), funnel
