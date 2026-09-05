# Durable sync resilience

The current sync path is a durable, resumable coordinator rather than a one-shot adapter call. It preserves the public `AudienceRequest` state contract while adding persistent job/batch state for idempotency, bounded retries, concurrency control, and failure recovery.

## State flow

```text
READY_TO_SYNC / APPROVED
        |
        v
  get-or-create SyncJob  <---- one logical job per request
        |
        +-- deterministic job idempotency key
        +-- audience membership fingerprint
        +-- atomic worker lease
        |
        v
   SyncJob.RUNNING
        |
        v
  SyncBatch[0..N]
  PENDING
     |
     +-- atomic claim + lease
     v
  IN_PROGRESS --transient--> RETRY_PENDING --backoff--> IN_PROGRESS
     |                                  |
     |                                  +-- max attempts --> FAILED
     +-- success --> SUCCEEDED

all batches SUCCEEDED  -> AudienceRequest.SYNCED
terminal batch failure -> AudienceRequest.SYNC_FAILED
POST /sync again       -> same SyncJob, preserve SUCCEEDED batches, retry failed work
```

The public workflow deliberately does **not** add a new `SYNCING` request state. In-flight state is represented by `SyncJob.status=RUNNING`, preserving existing API consumers and workflow-transition behavior.

## Idempotency

- `sync_jobs.request_id` is unique: one governed request maps to one logical release job.
- The job key is server-derived from request key + provider + deterministic audience fingerprint.
- Every batch receives a deterministic key derived from the job key and batch index.
- Repeated sync after success returns the already-completed job and does not replay downstream writes.
- Concurrent sync calls compete for an atomic job lease; only one worker can drive the job at a time.
- A crashed worker can be replaced after the lease expires. Batch leases and heartbeats prevent a healthy long-running operation from being reclaimed early.
- Successful batch checkpoints are never reset on a manual retry.
- Provider retargeting after a failed preflight is permitted only before any successful batch or checkpointed external operation creates evidence of downstream side effects.

No email addresses or contact-level identifiers are persisted in the sync job/batch tables or mock sync log.

## Retry and failure recovery

Transient failures (`408`, `409`, `425`, `429`, `5xx`, network errors, and Constant Contact polling timeout) are retried with exponential backoff using the **same batch idempotency key**.

Permanent 4xx/configuration/import failures are not blindly replayed. Unknown exceptions are treated as permanent because retrying an operation with uncertain external side effects is less safe than surfacing `SYNC_FAILED` for operator review.

`SyncBatch.attempts` is a lifetime counter. `round_attempts` resets when an operator retries a failed job, so historical observability is retained without preventing a later recovery round. `SyncJob.retry_round` records the recovery round.

A manual retry uses the existing `SyncJob`: batches already marked `SUCCEEDED` stay complete, while failed or unfinished batches are made eligible for recovery. This is checkpoint recovery, not a fresh release.

## Adapter recovery semantics

### Mock adapter

The database batch claim is the primary idempotency guard. The aggregate-only mock log additionally checks the batch idempotency key and reuses a prior result if a local crash/replay reaches the adapter again.

### Mailchimp

The adapter uses PUT/upsert for each member and applies a stable audience tag. These operations are naturally replay-safe at the contact/tag level. A synthetic operation id is checkpointed for observability.

### Constant Contact

The adapter persists the contact-list id and returned bulk `activity_id` immediately after submission and **before** polling. If polling/network processing later fails, a retry resumes polling the same activity instead of intentionally submitting a second bulk import.

Important limitation: the current Constant Contact bulk-import path does not expose a true client-supplied downstream idempotency key in this implementation. There is therefore a narrow at-least-once window between Constant Contact accepting the POST and the process successfully committing the returned `activity_id`. The checkpoint design materially narrows the failure window but does not claim strict exactly-once delivery for that vendor API.

## Real-sync safety preflight

For real providers, the coordinator first verifies that `ALLOW_REAL_MARKETING_SYNC=true`. Only after real sync is enabled does it enforce `REAL_SYNC_MAX_RECIPIENTS`. This ordering gives a stable fail-closed error when real execution has not been explicitly enabled and avoids exposing later provider/preflight conditions as the primary reason.

## Progress API

`GET /api/requests/{request_id}/sync` returns durable job and batch progress including:

- job idempotency key, provider, audience fingerprint, status, and retry round;
- total/completed batch and recipient counts;
- job lease and timestamps;
- per-batch status, size, lifetime attempts, round attempts, and idempotency key;
- checkpointed external operation ids;
- retry/lease timestamps and errors.

`GET /api/requests/{request_id}` also embeds the same sync-job structure.

## Configuration

```dotenv
SYNC_BATCH_SIZE=100
SYNC_MAX_ATTEMPTS=3
SYNC_RETRY_BACKOFF_SECONDS=0.25
SYNC_LEASE_SECONDS=120
```

`SYNC_LEASE_SECONDS` should exceed the longest single blocking downstream request between heartbeats. The adapters heartbeat around network operations; the default is intentionally longer than their 30-second HTTP request timeout.

The default mock path does not require a `.env` file. These values have defaults in `Settings`; copy `.env.example` to `.env` only when you want local overrides or optional integration credentials.

## Database behavior

The resilience update adds `sync_jobs` and `sync_batches`. `AudienceRequest.status` remains stored as VARCHAR while the ORM returns `WorkflowState`.

The repository's current `init_db()` uses `Base.metadata.create_all(engine)`, so restarting the demo creates missing new tables in an existing SQLite database without dropping existing data. This is sufficient for the demo but is **not** a general schema-migration strategy. Production deployments should introduce an explicit Alembic migration and migration tests.

## Current execution boundary

The coordinator currently executes batches synchronously inside the HTTP sync call for compatibility with the existing demo. The durable job/batch schema is intentionally worker-ready: move the loop to a queue/worker (Celery, RQ, Dramatiq, Temporal, etc.) when you need non-blocking HTTP, scheduled retries, dead-letter handling, or many concurrent sync jobs.

The durable idempotency keys, provider checkpoints, retry classification, attempt history, and lease model should remain part of that worker architecture.
