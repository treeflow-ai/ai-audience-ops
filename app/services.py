from __future__ import annotations

import hashlib
import json
import math
import time
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import and_, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from .adapters.marketing import (
    PermanentMarketingAdapterError,
    Recipient,
    RetryableMarketingAdapterError,
    get_marketing_adapter,
)
from .config import Settings
from .llm import get_intent_parser
from .llm_boundary import validate_domain_references
from .models import AudienceMember, AudienceRequest, AuditEvent, Course, Student, SyncBatch, SyncJob
from .policy import PolicyEngine
from .query_engine import AudienceQueryEngine
from .retrieval import PolicyRetriever
from .schemas import PolicyCheck
from .workflow import WorkflowState


class MarketingSyncError(RuntimeError):
    """A downstream marketing adapter failed or is not safely configured."""


class SyncLeaseLostError(MarketingSyncError):
    """This request no longer owns the durable sync job/batch lease."""


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
        intent = validate_domain_references(
            intent,
            known_courses=self.session.scalars(
                select(Course.name).order_by(Course.name)
            ).all(),
            known_profiles=self.session.scalars(
                select(Student.learner_profile)
                .where(Student.learner_profile.is_not(None))
                .distinct()
                .order_by(Student.learner_profile)
            ).all(),
        )
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
            return self.get_request(request_id)

        member_ids, funnel = AudienceQueryEngine(self.session).run(intent)
        request.eligible_count = len(member_ids)
        request.funnel_json = json.dumps([f.model_dump() for f in funnel], indent=2)
        self._audit(
            request,
            "AUDIENCE_EVALUATED",
            "Audience query engine",
            f"{len(member_ids):,} eligible students after governed filters.",
        )
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
            self._audit(
                request,
                "AUTO_RELEASE_ELIGIBLE",
                "Policy engine",
                "All deterministic checks passed; audience may be synced.",
            )
        if request.status is not WorkflowState.BLOCKED:
            self.session.add_all([AudienceMember(request_id=request.id, student_id=sid) for sid in member_ids])

        request_id = request.id
        self.session.commit()
        return self.get_request(request_id)

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
        """Run or resume one durable, idempotent sync job.

        The HTTP call may execute several batches synchronously, but progress is
        committed after every batch and after the downstream operation id is
        known. A later call can therefore recover after process/network failure.
        """

        request = self.get_request(request_id)

        # A repeated sync after success is a no-op and returns the durable result.
        if request.sync_job and request.sync_job.status == "SUCCEEDED":
            self._reconcile_success(request, request.sync_job)
            self.session.commit()
            return self.get_request(request_id)

        # workflow state guard
        if not request.status.can_sync:
            raise ValueError(f"Request cannot be synced from status={request.status.value}.")

        # create/resume durable SyncJob + idempotency key
        member_ids = self._member_ids(request_id)
        fingerprint = self._audience_fingerprint(member_ids)
        job = self._ensure_sync_job(request, member_ids, fingerprint)

        # Collapse concurrent/repeated POST /sync calls to a single active worker.
        # A crashed worker's lease expires, allowing a later call to resume.
        worker_token = uuid.uuid4().hex
        if not self._acquire_job_lease(job.id, worker_token):
            return self.get_request(request_id)

        request = self.get_request(request_id)
        job = request.sync_job
        assert job is not None

        # Failed jobs are manually retryable through the same POST /sync call.
        # The job/idempotency key is preserved; only failed batch attempt budgets
        # are reset. Successful checkpoints are never replayed.
        if job.status == "FAILED" and request.status is WorkflowState.SYNC_FAILED:
            self._reset_failed_job(request, job)
            request = self.get_request(request_id)
            job = request.sync_job
            assert job is not None

        # The public request state remains APPROVED/READY_TO_SYNC while work is
        # in flight; the durable SyncJob carries RUNNING/PENDING/FAILED/SUCCEEDED.
        # This preserves the existing API state-machine contract.
        if job.started_at is None:
            job.started_at = datetime.now(timezone.utc)
        job.status = "RUNNING"
        self._renew_job_lease(job, worker_token)
        self.session.commit()

        request = self.get_request(request_id)
        job = request.sync_job
        assert job is not None
        self._verify_snapshot_or_fail(request, job, member_ids, fingerprint)
        self._preflight_total_cap_or_fail(request, job)

        request.sync_detail = self._progress_detail(job)
        self._audit(
            request,
            "SYNC_STARTED" if job.retry_round == 0 else "SYNC_RESUMED",
            request.marketing_provider,
            f"Durable sync job {job.id} running with idempotency key {job.idempotency_key[:12]}…",
        )
        self.session.commit()

        adapter = get_marketing_adapter(request.marketing_provider, self.settings)

        while True:
            request = self.get_request(request_id)
            job = request.sync_job
            assert job is not None
            batch = self._claim_next_batch(job.id, worker_token)
            if batch is None:
                break
            try:
                self._run_batch(request_id, job.id, batch.id, adapter, worker_token)
            except SyncLeaseLostError:
                self.session.rollback()
                return self.get_request(request_id)

        return self._finalize_or_report_progress(request_id, worker_token)

    def _ensure_sync_job(
        self,
        request: AudienceRequest,
        member_ids: list[int],
        fingerprint: str,
    ) -> SyncJob:
        if request.sync_job is not None:
            job = request.sync_job
            if job.provider != request.marketing_provider:
                self._retarget_failed_job_if_safe(request, job, fingerprint)
            return job

        idempotency_key = hashlib.sha256(
            f"audience-sync:v1:{request.request_key}:{request.marketing_provider}:{fingerprint}".encode("utf-8")
        ).hexdigest()
        batch_size = self.settings.sync_batch_size
        total_batches = math.ceil(len(member_ids) / batch_size) if member_ids else 0
        job = SyncJob(
            request_id=request.id,
            idempotency_key=idempotency_key,
            provider=request.marketing_provider,
            audience_fingerprint=fingerprint,
            status="PENDING",
            batch_size=batch_size,
            total_recipients=len(member_ids),
            total_batches=total_batches,
        )
        self.session.add(job)
        try:
            self.session.flush()
            for batch_index in range(total_batches):
                start = batch_index * batch_size
                size = min(batch_size, len(member_ids) - start)
                batch_key = hashlib.sha256(f"{idempotency_key}:{batch_index}".encode("utf-8")).hexdigest()
                self.session.add(
                    SyncBatch(
                        job_id=job.id,
                        batch_index=batch_index,
                        start_offset=start,
                        size=size,
                        idempotency_key=batch_key,
                        status="PENDING",
                    )
                )
            self._audit(
                request,
                "SYNC_JOB_CREATED",
                "Sync coordinator",
                f"Created job {job.id} with {total_batches} durable batch checkpoint(s).",
            )
            self.session.commit()
        except IntegrityError:
            # Concurrent POST /sync calls race here. The unique request_id and
            # idempotency key collapse them into the same durable job.
            self.session.rollback()

        loaded = self._get_job_for_request(request.id)
        if loaded is None:
            raise MarketingSyncError("Failed to create or recover the sync job.")
        if loaded.audience_fingerprint != fingerprint or loaded.provider != request.marketing_provider:
            raise MarketingSyncError("Existing sync job does not match the current governed audience snapshot.")
        return loaded

    def _acquire_job_lease(self, job_id: int, worker_token: str) -> bool:
        now = datetime.now(timezone.utc)
        result = self.session.execute(
            update(SyncJob)
            .where(
                SyncJob.id == job_id,
                SyncJob.status != "SUCCEEDED",
                or_(
                    SyncJob.worker_token.is_(None),
                    SyncJob.lease_expires_at.is_(None),
                    SyncJob.lease_expires_at <= now,
                ),
            )
            .values(
                worker_token=worker_token,
                lease_expires_at=now + timedelta(seconds=self.settings.sync_lease_seconds),
            )
            .execution_options(synchronize_session=False)
        )
        acquired = result.rowcount == 1
        self.session.commit()
        return acquired

    def _renew_job_lease(self, job: SyncJob, worker_token: str) -> None:
        if job.worker_token != worker_token:
            raise SyncLeaseLostError("Sync job lease was lost to another worker.")
        job.lease_expires_at = datetime.now(timezone.utc) + timedelta(
            seconds=self.settings.sync_lease_seconds
        )

    def _heartbeat(self, job_id: int, batch_id: int, worker_token: str) -> None:
        job = self.session.get(SyncJob, job_id)
        batch = self.session.get(SyncBatch, batch_id)
        if job is None or batch is None or job.worker_token != worker_token:
            raise SyncLeaseLostError("Sync worker lease is no longer valid.")
        expires = datetime.now(timezone.utc) + timedelta(seconds=self.settings.sync_lease_seconds)
        job.lease_expires_at = expires
        batch.lease_expires_at = expires
        self.session.commit()

    def _retarget_failed_job_if_safe(
        self,
        request: AudienceRequest,
        job: SyncJob,
        fingerprint: str,
    ) -> None:
        """Allow provider correction only when no downstream side effect was checkpointed.

        This preserves the repository's existing recovery behavior (e.g. a disabled
        real provider can be switched to a mock) without permitting a partially
        released audience to jump providers.
        """

        if job.status != "FAILED" or any(
            batch.status == "SUCCEEDED" or batch.external_operation_id or batch.synced_count
            for batch in job.batches
        ):
            raise MarketingSyncError(
                "Marketing provider changed after sync side effects may have occurred; "
                "create a new governed audience request instead."
            )
        if fingerprint != job.audience_fingerprint:
            raise MarketingSyncError("Cannot change provider for a stale audience snapshot.")

        new_key = hashlib.sha256(
            f"audience-sync:v1:{request.request_key}:{request.marketing_provider}:{fingerprint}".encode("utf-8")
        ).hexdigest()
        job.provider = request.marketing_provider
        job.idempotency_key = new_key
        job.last_error = None
        for batch in job.batches:
            batch.idempotency_key = hashlib.sha256(
                f"{new_key}:{batch.batch_index}".encode("utf-8")
            ).hexdigest()
            batch.external_segment_id = None
            batch.last_error = None
        self._audit(
            request,
            "SYNC_PROVIDER_RETARGETED",
            request.requested_by,
            f"Failed sync job {job.id} retargeted to {request.marketing_provider} before any downstream checkpoint.",
        )
        self.session.commit()

    def _claim_next_batch(self, job_id: int, worker_token: str) -> SyncBatch | None:
        now = datetime.now(timezone.utc)
        job = self.session.get(SyncJob, job_id)
        if job is None or job.worker_token != worker_token:
            raise SyncLeaseLostError("Sync job lease was lost before claiming the next batch.")
        self._renew_job_lease(job, worker_token)
        self.session.commit()
        claimable = or_(
            and_(
                SyncBatch.status.in_(["PENDING", "RETRY_PENDING"]),
                or_(SyncBatch.next_attempt_at.is_(None), SyncBatch.next_attempt_at <= now),
            ),
            and_(SyncBatch.status == "IN_PROGRESS", SyncBatch.lease_expires_at <= now),
        )
        candidate_ids = list(
            self.session.scalars(
                select(SyncBatch.id)
                .where(SyncBatch.job_id == job_id, claimable)
                .order_by(SyncBatch.batch_index)
                .limit(8)
            ).all()
        )
        for batch_id in candidate_ids:
            result = self.session.execute(
                update(SyncBatch)
                .where(SyncBatch.id == batch_id, claimable)
                .values(
                    status="IN_PROGRESS",
                    attempts=SyncBatch.attempts + 1,
                    round_attempts=SyncBatch.round_attempts + 1,
                    lease_expires_at=now + timedelta(seconds=self.settings.sync_lease_seconds),
                    next_attempt_at=None,
                )
                .execution_options(synchronize_session=False)
            )
            if result.rowcount == 1:
                self.session.commit()
                return self.session.get(SyncBatch, batch_id)
        self.session.commit()
        return None

    def _run_batch(
        self, request_id: int, job_id: int, batch_id: int, adapter, worker_token: str
    ) -> None:
        batch = self.session.get(SyncBatch, batch_id)
        job = self.session.get(SyncJob, job_id)
        request = self.session.get(AudienceRequest, request_id)
        if batch is None or job is None or request is None:
            raise MarketingSyncError("Sync job disappeared while processing a batch.")

        recipients = self._recipients_for_batch(request_id, batch.start_offset, batch.size)
        if len(recipients) != batch.size:
            self._mark_job_failed(
                request,
                job,
                batch,
                "Audience membership changed after the sync snapshot was created.",
            )
            raise MarketingSyncError(job.last_error or "Audience snapshot changed.")

        def checkpoint_operation(operation_id: str, segment_id: str | None) -> None:
            # Persist the downstream operation reference before waiting for its
            # completion. Recovery can poll/resume rather than blindly submit again.
            self._heartbeat(job_id, batch_id, worker_token)
            checkpoint_batch = self.session.get(SyncBatch, batch_id)
            checkpoint_job = self.session.get(SyncJob, job_id)
            if checkpoint_batch is None or checkpoint_job is None:
                raise MarketingSyncError("Cannot checkpoint downstream operation; sync batch no longer exists.")
            checkpoint_batch.external_operation_id = operation_id
            if segment_id:
                checkpoint_batch.external_segment_id = segment_id
                checkpoint_job.external_segment_id = checkpoint_job.external_segment_id or segment_id
            self.session.commit()

        try:
            result = adapter.sync_batch(
                request.request_key,
                recipients,
                idempotency_key=batch.idempotency_key,
                external_segment_id=batch.external_segment_id or job.external_segment_id,
                external_operation_id=batch.external_operation_id,
                checkpoint_operation=checkpoint_operation,
                heartbeat=lambda: self._heartbeat(job_id, batch_id, worker_token),
            )
        except SyncLeaseLostError:
            raise
        except RetryableMarketingAdapterError as exc:
            self._handle_retryable_batch_failure(request_id, job_id, batch_id, str(exc))
            return
        except PermanentMarketingAdapterError as exc:
            request = self.session.get(AudienceRequest, request_id)
            job = self.session.get(SyncJob, job_id)
            batch = self.session.get(SyncBatch, batch_id)
            assert request is not None and job is not None and batch is not None
            self._mark_job_failed(request, job, batch, str(exc))
            raise MarketingSyncError(str(exc)) from exc
        except Exception as exc:
            # Unknown exceptions are treated as permanent to avoid replaying a
            # possibly successful external side effect without evidence that it
            # is safe to retry.
            request = self.session.get(AudienceRequest, request_id)
            job = self.session.get(SyncJob, job_id)
            batch = self.session.get(SyncBatch, batch_id)
            assert request is not None and job is not None and batch is not None
            self._mark_job_failed(request, job, batch, str(exc))
            raise MarketingSyncError(str(exc)) from exc

        batch = self.session.get(SyncBatch, batch_id)
        job = self.session.get(SyncJob, job_id)
        request = self.session.get(AudienceRequest, request_id)
        assert batch is not None and job is not None and request is not None
        batch.status = "SUCCEEDED"
        batch.synced_count = result.synced_count
        batch.external_operation_id = result.external_operation_id or batch.external_operation_id
        batch.external_segment_id = result.external_segment_id
        batch.last_error = None
        batch.lease_expires_at = None
        batch.next_attempt_at = None
        batch.completed_at = datetime.now(timezone.utc)
        job.external_segment_id = job.external_segment_id or result.external_segment_id
        self._refresh_job_progress(job)
        request.sync_detail = self._progress_detail(job)
        self._audit(
            request,
            "SYNC_BATCH_SUCCEEDED",
            result.provider,
            f"Batch {batch.batch_index + 1}/{job.total_batches} committed: {result.synced_count} recipient(s).",
        )
        self.session.commit()

    def _handle_retryable_batch_failure(self, request_id: int, job_id: int, batch_id: int, detail: str) -> None:
        request = self.session.get(AudienceRequest, request_id)
        job = self.session.get(SyncJob, job_id)
        batch = self.session.get(SyncBatch, batch_id)
        assert request is not None and job is not None and batch is not None

        if batch.round_attempts >= self.settings.sync_max_attempts:
            self._mark_job_failed(request, job, batch, detail)
            raise MarketingSyncError(detail)

        delay = self.settings.sync_retry_backoff_seconds * (2 ** max(batch.round_attempts - 1, 0))
        batch.status = "RETRY_PENDING"
        batch.last_error = detail
        batch.lease_expires_at = None
        batch.next_attempt_at = datetime.now(timezone.utc) + timedelta(seconds=delay)
        job.last_error = detail
        self._audit(
            request,
            "SYNC_BATCH_RETRY",
            request.marketing_provider,
            f"Batch {batch.batch_index + 1}/{job.total_batches} attempt {batch.round_attempts} "
            f"(lifetime {batch.attempts}) failed; retry scheduled.",
        )
        self.session.commit()
        if delay > 0:
            time.sleep(delay)

    def _mark_job_failed(
        self,
        request: AudienceRequest,
        job: SyncJob,
        batch: SyncBatch | None,
        detail: str,
    ) -> None:
        if batch is not None:
            batch.status = "FAILED"
            batch.last_error = detail
            batch.lease_expires_at = None
            batch.next_attempt_at = None
        job.status = "FAILED"
        job.last_error = detail
        job.completed_at = datetime.now(timezone.utc)
        job.worker_token = None
        job.lease_expires_at = None
        self._refresh_job_progress(job)
        request.status = request.status.transition_to(WorkflowState.SYNC_FAILED)
        request.sync_detail = detail
        self._audit(request, "SYNC_FAILED", request.marketing_provider, detail)
        self.session.commit()

    def _reset_failed_job(self, request: AudienceRequest, job: SyncJob) -> None:
        for batch in job.batches:
            if batch.status == "FAILED":
                batch.status = "PENDING"
                batch.round_attempts = 0
                batch.last_error = None
                batch.next_attempt_at = None
                batch.lease_expires_at = None
        job.status = "PENDING"
        job.retry_round += 1
        job.last_error = None
        job.completed_at = None
        self._audit(
            request,
            "SYNC_RETRY_REQUESTED",
            request.requested_by,
            f"Retrying failed sync job {job.id} with the same idempotency key; successful batches are preserved.",
        )
        self.session.commit()

    def _finalize_or_report_progress(self, request_id: int, worker_token: str) -> AudienceRequest:
        request = self.get_request(request_id)
        job = request.sync_job
        assert job is not None
        self._refresh_job_progress(job)

        if any(batch.status == "FAILED" for batch in job.batches):
            detail = job.last_error or "One or more sync batches failed."
            self._mark_job_failed(request, job, None, detail)
            raise MarketingSyncError(detail)

        if job.completed_batches == job.total_batches:
            job.status = "SUCCEEDED"
            job.completed_at = datetime.now(timezone.utc)
            job.last_error = None
            job.worker_token = None
            job.lease_expires_at = None
            if request.status is not WorkflowState.SYNCED:
                request.status = request.status.transition_to(WorkflowState.SYNCED)
            request.external_segment_id = job.external_segment_id
            request.sync_detail = (
                f"Durable sync completed: {job.completed_recipients:,}/{job.total_recipients:,} recipients "
                f"across {job.total_batches} batch(es); idempotency={job.idempotency_key[:12]}…."
            )
            self._audit(
                request,
                "MARKETING_SYNCED",
                request.marketing_provider,
                f"Synced {job.completed_recipients:,} governed recipients with durable batch checkpoints.",
            )
            self.session.commit()
            return self.get_request(request_id)

        job.status = "RUNNING"
        if job.worker_token == worker_token:
            job.worker_token = None
            job.lease_expires_at = None
        request.sync_detail = self._progress_detail(job)
        self.session.commit()
        return self.get_request(request_id)

    def _verify_snapshot_or_fail(
        self,
        request: AudienceRequest,
        job: SyncJob,
        member_ids: list[int],
        fingerprint: str,
    ) -> None:
        if len(member_ids) == job.total_recipients and fingerprint == job.audience_fingerprint:
            return
        self._mark_job_failed(
            request,
            job,
            None,
            "Governed audience membership changed after sync job creation; refusing to continue a stale snapshot.",
        )
        raise MarketingSyncError(job.last_error or "Audience snapshot changed.")

    def _preflight_total_cap_or_fail(self, request: AudienceRequest, job: SyncJob) -> None:
        if request.marketing_provider not in {"mailchimp", "constantcontact"}:
            return

        # Keep the safety-gate ordering consistent with the real adapters: when
        # real delivery is disabled, that is the primary reason the sync cannot
        # proceed. Only evaluate the aggregate recipient cap after the operator
        # has explicitly enabled real marketing synchronization.
        if not self.settings.allow_real_marketing_sync:
            detail = "Real marketing sync is disabled. Set ALLOW_REAL_MARKETING_SYNC=true explicitly."
            self._mark_job_failed(request, job, None, detail)
            raise MarketingSyncError(detail)

        if job.total_recipients > self.settings.real_sync_max_recipients:
            detail = (
                f"Real sync recipient count {job.total_recipients} exceeds "
                f"REAL_SYNC_MAX_RECIPIENTS={self.settings.real_sync_max_recipients}."
            )
            self._mark_job_failed(request, job, None, detail)
            raise MarketingSyncError(detail)

    def _reconcile_success(self, request: AudienceRequest, job: SyncJob) -> None:
        # If a previous request committed the job but crashed before the request
        # status update, make the aggregate state converge without replaying sync.
        if request.status is not WorkflowState.SYNCED:
            request.status = request.status.transition_to(WorkflowState.SYNCED)
        request.external_segment_id = job.external_segment_id
        request.sync_detail = (
            f"Durable sync already completed; reused idempotent job {job.id} "
            f"({job.completed_recipients:,} recipients)."
        )

    def _refresh_job_progress(self, job: SyncJob) -> None:
        # Refresh from DB because another worker may have completed a different batch.
        batches = list(
            self.session.scalars(select(SyncBatch).where(SyncBatch.job_id == job.id).order_by(SyncBatch.batch_index)).all()
        )
        job.completed_batches = sum(1 for b in batches if b.status == "SUCCEEDED")
        job.completed_recipients = sum(b.synced_count for b in batches if b.status == "SUCCEEDED")

    def _progress_detail(self, job: SyncJob) -> str:
        return (
            f"Sync job {job.id}: {job.completed_batches}/{job.total_batches} batches, "
            f"{job.completed_recipients}/{job.total_recipients} recipients complete."
        )

    def _member_ids(self, request_id: int) -> list[int]:
        return list(
            self.session.scalars(
                select(AudienceMember.student_id)
                .where(AudienceMember.request_id == request_id)
                .order_by(AudienceMember.student_id)
            ).all()
        )

    @staticmethod
    def _audience_fingerprint(member_ids: list[int]) -> str:
        payload = ",".join(str(member_id) for member_id in member_ids)
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def _recipients_for_batch(self, request_id: int, start_offset: int, size: int) -> list[Recipient]:
        students = self.session.scalars(
            select(Student)
            .join(AudienceMember, AudienceMember.student_id == Student.id)
            .where(AudienceMember.request_id == request_id)
            .order_by(Student.id)
            .offset(start_offset)
            .limit(size)
        ).all()
        return [
            Recipient(
                external_id=s.external_id,
                email=s.email,
                first_name=s.first_name,
                last_name=s.last_name,
            )
            for s in students
        ]

    def _get_job_for_request(self, request_id: int) -> SyncJob | None:
        return self.session.scalar(
            select(SyncJob)
            .where(SyncJob.request_id == request_id)
            .options(selectinload(SyncJob.batches))
        )

    def get_request(self, request_id: int) -> AudienceRequest:
        request = self.session.scalar(
            select(AudienceRequest)
            .where(AudienceRequest.id == request_id)
            .options(
                selectinload(AudienceRequest.events),
                selectinload(AudienceRequest.members),
                selectinload(AudienceRequest.sync_job).selectinload(SyncJob.batches),
            )
        )
        if request is None:
            raise LookupError("Audience request not found")
        request.events.sort(key=lambda e: e.created_at)
        return request

    def list_requests(self, limit: int = 30) -> list[AudienceRequest]:
        return list(
            self.session.scalars(
                select(AudienceRequest).order_by(AudienceRequest.created_at.desc()).limit(limit)
            ).all()
        )

    @staticmethod
    def _blocked_summary(checks: list[PolicyCheck]) -> str:
        messages = [c.message for c in checks if c.result == "BLOCK"]
        return " ".join(messages) or "Request blocked by policy."
