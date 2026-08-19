# Architecture

## Design goal

Demonstrate how an AI feature can sit inside an existing business workflow without becoming the authority for data access, privacy, approvals, or downstream side effects.

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

### 3. Deterministic authorization

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
- marketing consent;
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

For `REVIEW_REQUIRED`, only the manager parsed from the original request may approve in this demo. Identity itself is still free-text, which is why production hardening requires authenticated server-side identity and RBAC.

### 6. Downstream integration

Marketing adapters receive the governed audience only after deterministic filters and any required approval.

The mock adapter writes aggregate-only local events. Real adapters require an explicit enable flag, credentials, and a recipient cap.

The Constant Contact example uses an asynchronous bulk-import API. Because the local domain state uses a terminal `SYNCED` status, the reference adapter polls the returned activity until completion (or failure/timeout) before reporting success. A production system should move that polling into a durable asynchronous job.

## Idempotency and correlation

Each request gets a stable `AUD-YYYYMMDD-NNNN` key. Mock segment IDs and real marketing metadata reuse this key so an operator can correlate downstream state with the local audit trail.

The local sequential key generator is sufficient for this single-process demo, but it is not a distributed sequence allocator. A multi-worker production version should use database-backed sequencing or a collision-resistant identifier and connector-specific idempotency keys.

## Why no arbitrary SQL generation

A model-generated SQL approach would make it harder to prove that consent and suppression are always enforced. Here the parser outputs only known fields in a Pydantic schema, and the query engine owns the actual data-access implementation.

That separation is the central architectural point of the project.
