# Marketing Integration Policy

## INTEGRATION-01 — Approved destinations
Governed audiences may be synchronized to approved marketing platforms such as Mailchimp or Constant Contact. The demo uses mock destinations by default.

## INTEGRATION-02 — Safe demo mode
Real marketing sync is disabled by default. Enabling a real connector requires an explicit runtime flag and provider credentials. Automated tests and public demos must not require live credentials.

## INTEGRATION-03 — Idempotent audience identity
Each governed audience release must have a stable application-owned identity. A request maps to one durable logical sync job, and deterministic per-batch idempotency keys must be reused when retrying the same work. Already-successful batch checkpoints must not be replayed by an operator retry.

## INTEGRATION-04 — Failure recovery
Retryable connector failures may be retried only with bounded attempts and preserved idempotency/correlation state. Permanent or unknown failures must fail closed for operator review rather than being blindly replayed. Where a provider returns an external operation identifier, the application should checkpoint it as early as safely possible so recovery can resume the accepted operation instead of submitting a duplicate.

## INTEGRATION-05 — Concurrency and observability
Only one active worker should drive a logical sync job at a time. Durable job/batch status, attempt counts, leases, errors, and external operation references should be retained for recovery and operator inspection without persisting recipient email addresses in sync-control metadata.

## INTEGRATION-06 — Provider limitations
Application-level idempotency does not imply provider-level exactly-once delivery. Connector-specific limitations must be documented. In particular, the current Constant Contact bulk-import flow has a narrow at-least-once window because the provider operation used here does not expose a true client-supplied idempotency key.
