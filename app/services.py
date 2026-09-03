from __future__ import annotations

import json
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from .adapters.marketing import Recipient, get_marketing_adapter
from .config import Settings
from .llm import get_intent_parser
from .models import AudienceMember, AudienceRequest, AuditEvent, Student
from .policy import PolicyEngine
from .query_engine import AudienceQueryEngine
from .retrieval import PolicyRetriever
from .schemas import PolicyCheck
from .workflow import WorkflowState




class MarketingSyncError(RuntimeError):
    """A downstream marketing adapter failed or is not safely configured."""

class AudienceService:
    def __init__(self, session: Session, settings: Settings):
        self.session = session
        self.settings = settings
        self.parser = get_intent_parser(settings)
        self.retriever = PolicyRetriever(settings.policy_dir)
        self.policy = PolicyEngine(settings.approval_threshold)

    def _audit(self, request: AudienceRequest, event_type: str, actor: str, detail: str) -> None:
        self.session.add(AuditEvent(request_id=request.id, event_type=event_type, actor=actor, detail=detail))

    def _next_key(self) -> str:
        today = datetime.now(timezone.utc).strftime("%Y%m%d")
        last_id = self.session.scalar(select(AudienceRequest.id).order_by(AudienceRequest.id.desc()).limit(1)) or 0
        return f"AUD-{today}-{last_id + 1:04d}"

    def create_request(self, text: str, requested_by: str, marketing_provider: str) -> AudienceRequest:
        intent = self.parser.parse(text)
        # Mandatory governance controls are owned by application code, not by
        # the LLM/parser. Normalize them before policy evaluation or querying.
        intent.marketing_consent_required = True
        intent.active_account_required = True
        intent.exclude_suppressed = True
        intent.exclude_target_course = True
        retrieved = self.retriever.search(text)
        checks = self.policy.pre_query(intent)
        blocked = any(c.result == "BLOCK" for c in checks)

        request = AudienceRequest(
            request_key=self._next_key(),
            raw_request=text,
            requested_by=requested_by,
            manager=intent.manager,
            marketing_provider=marketing_provider,
            status=WorkflowState.BLOCKED if blocked else WorkflowState.EVALUATING,
            risk_level="HIGH" if blocked else "LOW",
            confidence=round(intent.confidence * 100),
            eligible_count=0,
            intent_json=intent.model_dump_json(indent=2),
            policy_json=json.dumps([c.model_dump() for c in checks], indent=2),
            funnel_json="[]",
            retrieved_policy_json=json.dumps([p.model_dump() for p in retrieved], indent=2),
        )
        self.session.add(request)
        self.session.flush()
        self._audit(request, "REQUEST_SUBMITTED", requested_by, "Natural-language audience request submitted.")
        self._audit(request, "INTENT_EXTRACTED", "Intent parser", f"Intent extracted with {request.confidence}% confidence.")
        self._audit(request, "POLICY_RETRIEVED", "Policy retrieval", f"Retrieved {len(retrieved)} relevant policy sections.")

        if blocked:
            self._audit(request, "REQUEST_BLOCKED", "Policy engine", self._blocked_summary(checks))
            request_id = request.id
            self.session.commit()
            # Re-load after commit so callers never receive an expired ORM
            # instance. This is especially important for the BLOCKED path,
            # which previously triggered DetachedInstanceError in form routes.
            return self.get_request(request_id)

        member_ids, funnel = AudienceQueryEngine(self.session).run(intent)
        request.eligible_count = len(member_ids)
        request.funnel_json = json.dumps([f.model_dump() for f in funnel], indent=2)
        self._audit(request, "AUDIENCE_EVALUATED", "Audience query engine", f"{len(member_ids):,} eligible students after governed filters.")

        post_checks = self.policy.post_query(intent, len(member_ids))
        checks.extend(post_checks)
        request.policy_json = json.dumps([c.model_dump() for c in checks], indent=2)

        if any(c.result == "BLOCK" for c in post_checks):
            request.status = request.status.transition_to(WorkflowState.BLOCKED)
            request.risk_level = "HIGH"
            self._audit(request, "REQUEST_BLOCKED", "Policy engine", self._blocked_summary(post_checks))
        elif any(c.result == "REVIEW" for c in post_checks):
            request.status = request.status.transition_to(WorkflowState.REVIEW_REQUIRED)
            request.risk_level = "MEDIUM"
            self._audit(request, "APPROVAL_REQUIRED", "Policy engine", f"Manager approval required from {intent.manager}.")
        else:
            request.status = request.status.transition_to(WorkflowState.READY_TO_SYNC)
            request.risk_level = "LOW"
            self._audit(request, "AUTO_RELEASE_ELIGIBLE", "Policy engine", "All deterministic checks passed; audience may be synced.")

        if request.status is not WorkflowState.BLOCKED:
            self.session.add_all([AudienceMember(request_id=request.id, student_id=sid) for sid in member_ids])

        self.session.commit()
        return self.get_request(request.id)

    def approve(self, request_id: int, approver: str) -> AudienceRequest:
        request = self.get_request(request_id)
        if not request.status.requires_approval:
            raise ValueError(f"Request is not awaiting approval (status={request.status.value}).")
        if request.manager and approver.strip().lower() != request.manager.strip().lower():
            raise ValueError(f"Approval must come from the identified manager: {request.manager}.")
        request.status = request.status.transition_to(WorkflowState.APPROVED)
        request.approved_by = approver
        request.approved_at = datetime.now(timezone.utc)
        self._audit(request, "APPROVED", approver, f"Audience of {request.eligible_count:,} recipients approved for release.")
        self.session.commit()
        return self.get_request(request_id)

    def sync(self, request_id: int) -> AudienceRequest:
        request = self.get_request(request_id)
        if not request.status.can_sync:
            raise ValueError(f"Request cannot be synced from status={request.status.value}.")

        students = self.session.scalars(
            select(Student)
            .join(AudienceMember, AudienceMember.student_id == Student.id)
            .where(AudienceMember.request_id == request_id)
            .order_by(Student.id)
        ).all()
        recipients = [
            Recipient(
                external_id=s.external_id,
                email=s.email,
                first_name=s.first_name,
                last_name=s.last_name,
            )
            for s in students
        ]
        adapter = get_marketing_adapter(request.marketing_provider, self.settings)
        try:
            result = adapter.sync(request.request_key, recipients)
        except Exception as exc:
            request.status = request.status.transition_to(WorkflowState.SYNC_FAILED)
            request.sync_detail = str(exc)
            self._audit(request, "SYNC_FAILED", request.marketing_provider, str(exc))
            self.session.commit()
            raise MarketingSyncError(str(exc)) from exc

        request.status = request.status.transition_to(WorkflowState.SYNCED)
        request.external_segment_id = result.external_segment_id
        request.sync_detail = result.detail
        self._audit(
            request,
            "MARKETING_SYNCED",
            result.provider,
            f"Synced {result.synced_count:,} governed recipients to {result.external_segment_id}.",
        )
        self.session.commit()
        return self.get_request(request_id)

    def get_request(self, request_id: int) -> AudienceRequest:
        request = self.session.scalar(
            select(AudienceRequest)
            .where(AudienceRequest.id == request_id)
            .options(selectinload(AudienceRequest.events), selectinload(AudienceRequest.members))
        )
        if request is None:
            raise LookupError("Audience request not found")
        request.events.sort(key=lambda e: e.created_at)
        return request

    def list_requests(self, limit: int = 30) -> list[AudienceRequest]:
        return list(self.session.scalars(
            select(AudienceRequest).order_by(AudienceRequest.created_at.desc()).limit(limit)
        ).all())

    @staticmethod
    def _blocked_summary(checks: list[PolicyCheck]) -> str:
        messages = [c.message for c in checks if c.result == "BLOCK"]
        return " ".join(messages) or "Request blocked by policy."
