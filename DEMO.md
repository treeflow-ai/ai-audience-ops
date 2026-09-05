# Demo Guide

## Published videos

- **Short overview (~2 min):** https://youtu.be/9STwVwbaNr0
- **Engineering walkthrough (~4 min):** https://youtu.be/eUC1dkTPuW0

The published videos predate the durable Idempotency + Retry/Failure Recovery update. They still demonstrate the core trust boundary, deterministic enforcement, and three business workflow paths; use the sections below and `docs/SYNC_RESILIENCE.md` for the newest sync behavior.

## Local click-through demo

Start the app, open the dashboard, and use the three built-in prompts.

### Scenario 1 — compliant request

Use the first preset.

Show that:

1. the request becomes a constrained intent;
2. policy context is retrieved for explanation;
3. deterministic controls force consent, active-account, suppression, and target-course exclusion;
4. the audience funnel explains the reduction from 12,000 synthetic students;
5. the default result is **416 eligible recipients** and `READY_TO_SYNC`;
6. sync creates one durable logical `SyncJob` and deterministic batch checkpoints;
7. the mock sync succeeds without writing contact-level identifiers to the local mock log; and
8. clicking sync again after success does not replay completed mock batches.

For API inspection, call `GET /api/requests/{id}/sync` after synchronization to show job status, batch progress, idempotency keys, attempt counters, and provider-operation checkpoints.

### Scenario 2 — prohibited raw-email export

Use the raw-email preset.

The expected result is `BLOCKED` before audience evaluation. Call out `DATA-03` and the `REQUEST_BLOCKED` audit event.

### Scenario 3 — human approval

Use the large-audience preset.

The expected result is **6,709 eligible recipients** and `REVIEW_REQUIRED`. Approve as Jane Smith, then sync. The release is still processed through the same durable job/batch coordinator after approval.

## Failure-recovery engineering demo

The normal UI uses a successful mock adapter, so the clearest repeatable proof of retry/recovery behavior is the test suite:

```bash
python -m pytest -q tests/test_sync_resilience.py
```

The resilience tests demonstrate that:

- repeating a successful sync reuses the same job and does not replay completed mock batches;
- a transient failure retries the same batch with the same idempotency key;
- a failed batch can be recovered without replaying a previously successful batch;
- a second worker cannot acquire an unexpired job lease;
- an expired lease can be reclaimed; and
- a provider can be retargeted after a failed preflight only when no downstream side effect has been checkpointed.

## Engineering takeaway

The central design decision is the separation between probabilistic interpretation and deterministic authority:

- the parser interprets business language;
- application code owns policy and data-access controls;
- large audiences require an explicit human state transition;
- the sync coordinator owns idempotency, retry classification, leases, and durable checkpoints; and
- downstream writes occur only after the governed audience has been established.
