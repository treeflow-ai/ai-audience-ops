# Security and Privacy Notes

## Demo guarantees

- Synthetic identities only; generated email addresses use `example.edu`.
- Mock marketing synchronization is the default.
- Mock sync logs contain aggregate counts and destination metadata only; they do not persist email addresses or contact-level identifiers.
- Raw email export requests are blocked before audience evaluation.
- Consent, suppression, active-account, and target-course filters are enforced in application code.
- Real marketing synchronization requires explicit opt-in through `ALLOW_REAL_MARKETING_SYNC=true`.
- Real sync is capped by `REAL_SYNC_MAX_RECIPIENTS` after the real-sync enablement guard is satisfied.
- Secrets belong in environment variables / `.env`; `.env` is gitignored and excluded from Docker build context. The default mock demo and test suite do not require a `.env` file.
- Unknown `LLM_PROVIDER` values fail fast rather than silently falling back to another parser.

## Sync resilience and side-effect safety

The current reference implementation includes durable safeguards that were previously listed only as production follow-up work:

- one persistent `SyncJob` per governed audience request;
- deterministic job and per-batch idempotency keys;
- an audience-membership fingerprint so the job is bound to the governed recipient snapshot;
- persistent `SyncBatch` checkpoints with attempt counts and completion state;
- retries for classified transient connector failures using the same batch idempotency key;
- preservation of already-successful batches during manual failure recovery;
- job and batch leases/heartbeats to reduce duplicate concurrent workers and permit recovery after an expired lease;
- checkpointing of downstream operation identifiers where the provider exposes them; and
- a read-only sync-progress API at `GET /api/requests/{id}/sync`.

These controls reduce replay and duplicate-side-effect risk, but they are not a universal exactly-once guarantee. Mailchimp member PUT/upsert and stable tagging are replay-safe for the operations used here. Constant Contact bulk import does not expose a true client-supplied idempotency key in this implementation, so there remains a narrow at-least-once window if the provider accepts the import POST but the process fails before the returned `activity_id` can be committed locally.

Unknown connector exceptions are treated as permanent rather than blindly retried, because repeating an operation with uncertain external side effects can be less safe than surfacing `SYNC_FAILED` for operator review.

## Production hardening not included

A production deployment would additionally require:

- SSO / OAuth and role-based authorization;
- server-side identity rather than free-text `requested_by` and `approver` fields;
- encrypted secret management;
- explicit database migrations, backups, and tested restore procedures;
- immutable/security-grade audit logging;
- a durable asynchronous queue/worker for non-blocking connector execution, scheduled retries, and dead-letter handling;
- multi-instance/distributed coordination validation for the lease model under the target database;
- formal data-retention rules;
- region/jurisdiction-specific privacy and marketing-law review;
- downstream unsubscribe reconciliation;
- production monitoring, alerting, and operator runbooks;
- broader concurrency controls around non-sync workflow transitions; and
- threat modeling and penetration testing.

The optional real adapters are reference integrations, not a compliance certification or a substitute for vendor-specific operational controls. See `docs/SYNC_RESILIENCE.md` for the exact retry, idempotency, checkpoint, and recovery semantics.
