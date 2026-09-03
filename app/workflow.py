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

    def transition_to(self, target: "WorkflowState") -> "WorkflowState":
        """Return ``target`` when this lifecycle transition is legal.

        State construction is intentionally separate: callers creating a new
        request choose its initial state directly. All changes to an existing
        request should pass through this method.
        """
        if target not in _ALLOWED_TRANSITIONS[self]:
            raise ValueError(
                f"Illegal workflow state transition: {self.value} -> {target.value}."
            )
        return target

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


_ALLOWED_TRANSITIONS: dict[WorkflowState, frozenset[WorkflowState]] = {
    WorkflowState.EVALUATING: frozenset({
        WorkflowState.BLOCKED,
        WorkflowState.REVIEW_REQUIRED,
        WorkflowState.READY_TO_SYNC,
    }),
    WorkflowState.BLOCKED: frozenset(),
    WorkflowState.REVIEW_REQUIRED: frozenset({WorkflowState.APPROVED}),
    WorkflowState.READY_TO_SYNC: frozenset({
        WorkflowState.SYNC_FAILED,
        WorkflowState.SYNCED,
    }),
    WorkflowState.APPROVED: frozenset({
        WorkflowState.SYNC_FAILED,
        WorkflowState.SYNCED,
    }),
    WorkflowState.SYNC_FAILED: frozenset({
        WorkflowState.SYNC_FAILED,
        WorkflowState.SYNCED,
    }),
    WorkflowState.SYNCED: frozenset(),
}
