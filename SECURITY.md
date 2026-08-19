# Security and Privacy Notes

## Demo guarantees

- Synthetic identities only; generated email addresses use `example.edu`.
- Mock marketing synchronization is the default.
- Mock sync logs contain aggregate counts and destination metadata only; they do not persist email addresses or contact-level identifiers.
- Raw email export requests are blocked before audience evaluation.
- Consent, suppression, active-account, and target-course filters are enforced in application code.
- Real marketing synchronization requires explicit opt-in through `ALLOW_REAL_MARKETING_SYNC=true`.
- Real sync is capped by `REAL_SYNC_MAX_RECIPIENTS`.
- Secrets belong in environment variables / `.env`; `.env` is gitignored and excluded from Docker build context.
- Unknown `LLM_PROVIDER` values fail fast rather than silently falling back to another parser.

## Production hardening not included

A production deployment would additionally require:

- SSO / OAuth and role-based authorization;
- server-side identity rather than free-text `requested_by` and `approver` fields;
- encrypted secret management;
- database migrations and backups;
- immutable/security-grade audit logging;
- asynchronous connector jobs, retries, rate-limit handling, idempotency controls, and dead-letter queues;
- formal data-retention rules;
- region/jurisdiction-specific privacy and marketing-law review;
- downstream unsubscribe reconciliation;
- monitoring and alerting;
- concurrency controls around workflow state transitions; and
- threat modeling and penetration testing.

The optional real adapters are reference integrations, not a compliance certification or a substitute for vendor-specific operational controls.
