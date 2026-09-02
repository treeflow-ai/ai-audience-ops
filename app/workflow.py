from __future__ import annotations

from enum import StrEnum


class WorkflowState(StrEnum):
    """Lifecycle states for an audience request.

    Values intentionally match the strings already stored in the database and
    exposed by the API, so adopting the enum is backwards compatible with
    existing persisted rows and clients.
    """

    EVALUATING = "EVALUATING"
    BLOCKED = "BLOCKED"
    REVIEW_REQUIRED = "REVIEW_REQUIRED"
    READY_TO_SYNC = "READY_TO_SYNC"
    APPROVED = "APPROVED"
    SYNC_FAILED = "SYNC_FAILED"
    SYNCED = "SYNCED"

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
