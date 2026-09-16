from __future__ import annotations

from collections import defaultdict

from .marketing import (
    Heartbeat,
    MarketingAdapter,
    OperationCheckpoint,
    PermanentMarketingAdapterError,
    Recipient,
    RetryableMarketingAdapterError,
    SyncResult,
)

DEMO_FAILURE_MODES = {
    "transient_once",
    "terminal_once",
}


class FailureInjectingMarketingAdapter(MarketingAdapter):
    """Demo-only wrapper that injects deterministic provider failures.

    The second distinct batch is selected as the target so the terminal
    failure scenario always leaves at least one committed checkpoint that
    can be visibly preserved during manual recovery.
    """

    def __init__(self, delegate: MarketingAdapter, mode: str):
        if mode not in DEMO_FAILURE_MODES:
            raise ValueError(f"Unsupported demo failure mode: {mode}")

        self.delegate = delegate
        self.mode = mode
        self._seen_keys: list[str] = []
        self._attempts: dict[str, int] = defaultdict(int)

    def sync_batch(
        self,
        audience_key: str,
        recipients: list[Recipient],
        *,
        idempotency_key: str,
        external_segment_id: str | None = None,
        external_operation_id: str | None = None,
        checkpoint_operation: OperationCheckpoint | None = None,
        heartbeat: Heartbeat | None = None,
    ) -> SyncResult:
        if idempotency_key not in self._seen_keys:
            self._seen_keys.append(idempotency_key)

        self._attempts[idempotency_key] += 1
        batch_position = self._seen_keys.index(idempotency_key)
        attempt = self._attempts[idempotency_key]

        if batch_position == 1:
            if self.mode == "transient_once" and attempt == 1:
                raise RetryableMarketingAdapterError(
                    "Demo injection: simulated transient provider failure (HTTP 429)."
                )

            if self.mode == "terminal_once":
                raise PermanentMarketingAdapterError(
                    "Demo injection: simulated provider failure requiring operator recovery."
                )

        return self.delegate.sync_batch(
            audience_key,
            recipients,
            idempotency_key=idempotency_key,
            external_segment_id=external_segment_id,
            external_operation_id=external_operation_id,
            checkpoint_operation=checkpoint_operation,
            heartbeat=heartbeat,
        )
