# Architecture

## Design goal

Demonstrate how an AI feature can be integrated into a realistic business workflow without making the model the authority for privacy controls, approval decisions, data access, or downstream side effects.

This repository is a production-minded demo and reference implementation. It is designed to make trust boundaries and engineering trade-offs concrete, rather than to represent a deployed production system.

## Trust boundaries

### 1. Natural-language interpretation

`app/llm.py` converts an unstructured request into `AudienceIntent`.

Two implementations are available:

- `MockIntentParser`: deterministic, credential-free, testable.
- `OpenAIIntentParser`: optional real model via the Responses API.

Neither implementation receives database credentials or an SQL execution tool. Output is validated by Pydantic, and mandatory governance flags are re-applied by application code after parsing.

### 2. Policy retrieval

`app/retrieval.py` breaks Markdown policies into sections and retrieves relevant sections with a small lexical scorer. The UI exposes that context so an evaluator can see which governance concepts are related to the request.

This is an **explanatory retrieval layer**, not executable policy and not a claim that retrieval itself authorizes access. The allow/block/review decision is implemented separately in deterministic code.

### 3. Deterministic policy enforcement

`app/policy.py` enforces rules such as:

- no raw email export;
- target course required for the course-promotion workflow;
- consent mandatory;
- target-course members excluded; and
- large audiences require manager approval.

`app/query_engine.py` independently applies system-owned row filters for consent, active account, suppression, and target-course exclusion.

### 4. Data access

The runnable demo uses SQLite populated by `app/seed.py`. The schema models the subset of LearnDash/WordPress data needed for the scenario:

- students / WordPress-style user identity;
- courses;
- enrollment timestamps;
- completion timestamps;
- learner profile;
- marketing consent; and
- suppression state.

A real implementation would likely combine LearnDash REST/activity data with WordPress user meta, a CRM, or an analytics/reporting datastore.

### 5. Approval state machine

```text
EVALUATING
   ├── BLOCKED
   ├── READY_TO_SYNC ───────────────┐
   └── REVIEW_REQUIRED → APPROVED ──┤
                                    ↓
                                  SYNCED
                                    ↑
                                 SYNC_FAILED
```

The public `AudienceRequest` workflow deliberately does not add a `SYNCING` state. In-flight execution is represented by the durable `SyncJob` and `SyncBatch` records. A request that reaches `SYNC_FAILED` can be retried; successful batch checkpoints are preserved and the same logical job is resumed.

For `REVIEW_REQUIRED`, only the manager parsed from the original request may approve in this demo. Identity itself is still free-text, which is why production hardening requires authenticated server-side identity and RBAC.

### 6. Durable downstream integration

Marketing adapters receive the governed audience only after deterministic filters and any required approval. `AudienceService.sync()` coordinates downstream side effects instead of treating synchronization as one indivisible adapter call.

For each request, the service creates or reuses one persistent `SyncJob`, binds it to the selected provider and an audience-membership fingerprint, divides recipients into deterministic `SyncBatch` slices, and commits successful batches as recovery checkpoints. A repeated sync after success is a no-op; a retry after failure resumes unfinished/failed work rather than replaying completed batches.

```text
READY_TO_SYNC / APPROVED / SYNC_FAILED
              |
              v
       get or reuse SyncJob
              |
       acquire job lease
              |
              v
         SyncJob.RUNNING
              |
              v
       SyncBatch 0..N
 PENDING -> IN_PROGRESS -> SUCCEEDED
              |
              +-> RETRY_PENDING -> IN_PROGRESS
              |
              +-> FAILED

all batches SUCCEEDED -> AudienceRequest.SYNCED
terminal batch failure -> AudienceRequest.SYNC_FAILED
```

Transient failures such as retryable HTTP statuses and network errors are retried with exponential backoff up to the configured per-round attempt limit. Unknown exceptions and permanent connector/configuration failures fail closed to `SYNC_FAILED` for operator review. Manual retry starts another recovery round while retaining lifetime attempt counts.

Job and batch leases reduce duplicate concurrent execution. Leases can be reclaimed after expiry, while adapter heartbeats extend active work. This design is worker-ready, although the current demo still runs batch coordination synchronously inside the HTTP request.

### 7. Provider-specific recovery semantics

The mock adapter de-duplicates its aggregate local log by batch idempotency key.

Mailchimp uses member PUT/upsert plus a stable audience tag, so replaying a batch is naturally safe for the operations used by this demo. A stable operation identifier is checkpointed for observability.

Constant Contact uses an asynchronous JSON bulk-import activity. The implementation persists the list id and returned `activity_id` before polling. If polling later fails or times out, a retry resumes the same activity instead of intentionally submitting another import. Because the provider path used here does not expose a true client-supplied idempotency key, a narrow at-least-once failure window remains between provider acceptance and local persistence of the activity id.

## Idempotency, correlation, and immutable audience snapshot

Each audience request gets a stable `AUD-YYYYMMDD-NNNN` key. Synchronization adds two stronger identifiers:

- a deterministic `SyncJob.idempotency_key` derived from the request key, provider, and audience fingerprint; and
- a deterministic per-batch idempotency key derived from the job key and batch index.

`sync_jobs.request_id` is unique, so repeated sync calls do not create multiple logical release jobs for one governed request. `SyncBatch` rows preserve completion and provider-operation checkpoints across failures.

The request-key generator itself remains a single-process demo mechanism rather than a distributed sequence allocator. A production multi-writer deployment should use database-backed sequencing or collision-resistant request identifiers.

## Observability

`GET /api/requests/{request_id}/sync` exposes durable sync progress: job status, provider, audience fingerprint, batch counts, recipient counts, retry round, attempt counters, checkpointed external operation ids, retry timestamps, lease timestamps, and errors. The regular request API also embeds the sync-job summary.

No email addresses or contact-level identifiers are stored in the sync job/batch tables or in the mock sync log.

## Why no arbitrary SQL generation

A model-generated SQL approach would make it harder to prove that consent and suppression are always enforced. Here the parser outputs only known fields in a Pydantic schema, and the query engine owns the actual data-access implementation.

That separation is the central architectural point of the project.

For implementation-level retry and recovery details, see `docs/SYNC_RESILIENCE.md`.
