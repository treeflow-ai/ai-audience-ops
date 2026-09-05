from __future__ import annotations

from enum import Enum


class WorkflowState(str, Enum):
    EVALUATING = "EVALUATING"
    BLOCKED = "BLOCKED"
    REVIEW_REQUIRED = "REVIEW_REQUIRED"
    READY_TO_SYNC = "READY_TO_SYNC"
    APPROVED = "APPROVED"
    SYNCED = "SYNCED"
    SYNC_FAILED = "SYNC_FAILED"

    @property
    def requires_approval(self) -> bool:
        return self is WorkflowState.REVIEW_REQUIRED

    @property
    def can_sync(self) -> bool:
        return self in {
            WorkflowState.READY_TO_SYNC,
            WorkflowState.APPROVED,
            WorkflowState.SYNC_FAILED,
        }

    def transition_to(self, target: "WorkflowState") -> "WorkflowState":
        # Preserve the public request-state contract. Durable in-flight state is
        # tracked by SyncJob.status=RUNNING rather than adding a public SYNCING
        # state, so existing API clients and transition tests remain compatible.
        legal = {
            WorkflowState.EVALUATING: {
                WorkflowState.BLOCKED,
                WorkflowState.REVIEW_REQUIRED,
                WorkflowState.READY_TO_SYNC,
            },
            WorkflowState.REVIEW_REQUIRED: {WorkflowState.APPROVED},
            WorkflowState.READY_TO_SYNC: {
                WorkflowState.SYNC_FAILED,
                WorkflowState.SYNCED,
            },
            WorkflowState.APPROVED: {
                WorkflowState.SYNC_FAILED,
                WorkflowState.SYNCED,
            },
            WorkflowState.SYNC_FAILED: {
                WorkflowState.SYNC_FAILED,
                WorkflowState.SYNCED,
            },
        }
        if target not in legal.get(self, set()):
            raise ValueError(f"Illegal workflow state transition: {self.value} -> {target.value}")
        return target
