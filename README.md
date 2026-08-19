# AI Audience Ops

**Governed AI workflow for turning natural-language marketing requests into LearnDash audience segments without exposing raw student contact data.**

A marketing user describes an audience in plain English. The system converts that request into a constrained intent, applies deterministic policy and data-access controls, evaluates synthetic LearnDash-style activity, routes large audiences to human approval, and syncs only governed recipients to a marketing adapter.

> AI interprets business language. Application code owns authorization, privacy, approval, and downstream side effects.

## Demo videos

| Short overview | Engineering walkthrough |
|---|---|
| [![AI Audience Ops short demo](https://img.youtube.com/vi/9STwVwbaNr0/hqdefault.jpg)](https://youtu.be/9STwVwbaNr0) | [![AI Audience Ops engineering demo](https://img.youtube.com/vi/eUC1dkTPuW0/hqdefault.jpg)](https://youtu.be/eUC1dkTPuW0) |
| **~2 min:** problem, architecture boundary, and three proof points | **~4 min:** architecture, governance, deterministic filtering, approval, and sync |

**Start with the short overview.** The engineering walkthrough goes deeper into why the LLM is intentionally kept outside the authorization and execution boundary.

## What the project demonstrates

- Natural-language request → validated `AudienceIntent` schema.
- Deterministic consent, suppression, active-account, and target-course controls.
- Pre-query refusal of raw-email export requests.
- Explainable audience funnel over **12,000 deterministic synthetic students**.
- Human approval for audiences above a configurable threshold.
- Privacy-preserving mock Mailchimp / Constant Contact sync by default.
- Optional guarded adapters for OpenAI, LearnDash, Mailchimp, and Constant Contact.
- Audit history for request, intent, policy, evaluation, approval, and sync state.
- Automated pytest coverage and GitHub Actions CI.

The default public-demo path is credential-free: a deterministic mock intent parser, local policy retrieval, SQLite, synthetic data, and mock marketing adapters. The optional OpenAI parser uses the same downstream policy/query workflow, so switching parsers does not give the model direct database or marketing-platform authority.

## Run locally

### Option A — Python

Requires Python 3.11+.

```bash
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
python -m pip install -e ".[dev]"
cp .env.example .env               # Windows PowerShell: Copy-Item .env.example .env
uvicorn app.main:app --reload
```

Open <http://127.0.0.1:8000>.

The first launch creates and seeds the local SQLite database automatically.

### Option B — Docker

No `.env` file is required for the default mock demo:

```bash
docker compose up --build
```

Then open <http://127.0.0.1:8000>.

If you want to override defaults or enable optional integrations, copy `.env.example` to `.env` first and edit the values.

## Three built-in scenarios

### 1. Compliant audience

A Class C campaign requests learners who recently completed Class A, took Class B, match the `career_advancement` profile, and are eligible for marketing.

With the default deterministic dataset:

```text
READY_TO_SYNC
416 eligible recipients
```

The audience can be sent to the mock marketing adapter without displaying raw email addresses in the review UI.

### 2. Raw email export

The user asks for student email addresses to export to Excel.

```text
BLOCKED
0 eligible recipients
```

The request is refused **before audience evaluation**. Marketing segmentation is supported; unrestricted contact-data export is not.

### 3. Large audience / human approval

A broader Class C request targets anyone who took Class A or Class B during the last two years.

```text
REVIEW_REQUIRED
6,709 eligible recipients
```

The workflow cannot sync until the identified manager approves it. After approval, the mock adapter records a governed sync event.

Run all three from the CLI:

```bash
python scripts/run_demo.py
```

## Architecture

```mermaid
flowchart LR
    U[Marketing request] --> P[Intent parser]
    P --> S[Constrained AudienceIntent]
    S --> R[Policy retrieval]
    S --> G[Deterministic policy engine]
    G -->|blocked| B[Block + audit]
    G --> Q[Audience query engine]
    L[(Synthetic LearnDash-style data)] --> Q
    Q --> F[Explainable funnel]
    F --> T{Over approval threshold?}
    T -->|yes| H[Manager approval]
    T -->|no| M[Marketing adapter]
    H --> M
    M --> MC[Mailchimp / mock]
    M --> CC[Constant Contact / mock]
    P --> A[(Audit trail)]
    R --> A
    G --> A
    Q --> A
    H --> A
    M --> A
```

See [ARCHITECTURE.md](ARCHITECTURE.md) for the trust boundaries and state model.

## Key engineering decisions

### AI interprets; code authorizes

The model boundary is deliberately narrow. A parser may produce a validated intent object, but it cannot execute arbitrary SQL, turn off consent/suppression controls, approve its own request, or directly write to a marketing system. Mandatory controls are normalized by application code after interpretation.

### Policy retrieval is explanatory, not enforcement

`app/retrieval.py` performs small local lexical retrieval over the Markdown policy set so the UI can show relevant policy context. The actual allow/block/review decision is implemented separately in deterministic code. This is intentionally described as **policy retrieval**, not as a claim that the retrieved text itself authorizes access.

### The review UI is aggregate-only

The dashboard shows criteria, counts, policy results, approval state, and audit metadata. It does not display eligible students' raw email addresses. Mock sync logs are aggregate-only and do not persist contact-level identifiers.

### External systems sit behind adapters

The domain workflow depends on adapters rather than vendor-specific calls. The repository includes:

- `LearnDashClient` for documented LearnDash course-user and user-course REST routes;
- `MockMarketingAdapter` for credential-free public demos;
- `MailchimpAdapter` for governed list-member upsert + tagging; and
- `ConstantContactAdapter` for governed V3 list + JSON bulk-import flows.

Real marketing synchronization is **disabled by default** and capped by `REAL_SYNC_MAX_RECIPIENTS`. Constant Contact bulk import is asynchronous at the API level, so the example adapter waits for the activity to complete (or fail/timeout) before reporting the local workflow as synced.

## Project structure

```text
ai-audience-ops/
├── app/
│   ├── adapters/
│   │   ├── learndash.py
│   │   └── marketing.py
│   ├── templates/
│   ├── static/
│   ├── config.py
│   ├── db.py
│   ├── llm.py
│   ├── main.py
│   ├── models.py
│   ├── policy.py
│   ├── query_engine.py
│   ├── retrieval.py
│   ├── schemas.py
│   ├── seed.py
│   └── services.py
├── policies/
├── scripts/
├── tests/
├── docs/
├── .github/workflows/test.yml
├── .dockerignore
├── Dockerfile
├── docker-compose.yml
└── pyproject.toml
```

## API

FastAPI exposes interactive OpenAPI docs at `/docs`.

Main endpoints:

```text
POST /api/requests
GET  /api/requests
GET  /api/requests/{id}
POST /api/requests/{id}/approve
POST /api/requests/{id}/sync
GET  /health
```

Example:

```bash
curl -X POST http://127.0.0.1:8000/api/requests \
  -H 'content-type: application/json' \
  -d '{
    "text": "Please create an audience for promoting Class C. Include students who completed Class A within the last 90 days, have taken Class B, match our career advancement learner profile, and are eligible to receive marketing emails. Exclude anyone who has already enrolled in Class C. Manager is Jane Smith.",
    "requested_by": "Alex Rivera — Marketing",
    "marketing_provider": "mock_mailchimp"
  }'
```

## Optional OpenAI intent parser

The default `LLM_PROVIDER=mock` is deterministic and requires no credentials.

To use an OpenAI model only for intent interpretation:

```bash
python -m pip install -e ".[llm]"
export LLM_PROVIDER=openai
export OPENAI_API_KEY=...
export OPENAI_MODEL=gpt-5.5
```

The adapter uses the OpenAI Responses API. Returned JSON is validated into `AudienceIntent`, then mandatory system controls are re-applied before any policy or query step. The model never receives SQL execution authority.

Never commit `.env` or API keys.

## Optional LearnDash integration

The included client aligns with documented LearnDash REST endpoints such as:

```text
GET /wp-json/ldlms/v1/sfwd-courses/{course_id}/users
GET /wp-json/ldlms/v1/users/{user_id}/courses
```

The public demo intentionally does **not** pretend that a generic LearnDash installation contains organization-specific consent, learner-profile, or completion-date fields. A real deployment would map those fields from WordPress user meta, LearnDash activity/reporting data, a CRM, or a reporting warehouse.

See [docs/LEARNDASH-INTEGRATION.md](docs/LEARNDASH-INTEGRATION.md).

## Optional real marketing integrations

Real sync requires both provider credentials and:

```bash
export ALLOW_REAL_MARKETING_SYNC=true
```

The default cap is 500 recipients:

```bash
export REAL_SYNC_MAX_RECIPIENTS=500
```

### Mailchimp

Configure:

```text
MAILCHIMP_API_KEY
MAILCHIMP_SERVER_PREFIX
MAILCHIMP_LIST_ID
```

The adapter upserts already-governed contacts and attaches an `audience:AUD-...` tag. It does not intentionally overwrite an existing member's subscription state; `status_if_new` is used only when creating a new member.

### Constant Contact

Configure:

```text
CONSTANT_CONTACT_ACCESS_TOKEN
CONSTANT_CONTACT_LIST_ID                 # optional; otherwise create a list
CONSTANT_CONTACT_ACTIVITY_TIMEOUT_SECONDS=60
```

The adapter submits the V3 JSON bulk-import activity and polls `/activities/{activity_id}` until completion. Real production use would normally move this work to an asynchronous job/queue rather than holding a web request open.

## Testing

```bash
pytest
```

Tests cover:

- primary intent extraction;
- compliant audience creation and privacy-preserving mock sync;
- raw-email blocking before query execution;
- regression coverage for the detached SQLAlchemy session bug;
- application-owned consent/suppression/account/target-course controls;
- manager approval rules;
- real-sync safety guards;
- configuration/schema validation;
- repeatable database reset/seed behavior; and
- privacy-preserving mock log output.

GitHub Actions runs the suite on Python 3.11, 3.12, and 3.13 without secrets. Dependabot is configured for monthly Python and GitHub Actions dependency updates.

## Synthetic data

`app/seed.py` creates deterministic student profiles, consent/suppression flags, account status, and LearnDash-style enrollments/completions. Synthetic email addresses use `example.edu`; no real student records are included.

Reset the demo:

```bash
make reset
```

## Deliberate scope limits

This is a reference implementation, not a compliance certification or a production deployment. It intentionally does not include:

- real student data;
- SSO/RBAC or production identity management;
- security-grade immutable audit storage;
- database migrations/backups;
- async connector queues/retries/dead-letter handling;
- organization-specific privacy/legal rules; or
- production monitoring/alerting.

See [SECURITY.md](SECURITY.md) for the production-hardening boundary.

## Reference documentation

- [LearnDash REST API](https://developers.learndash.com/rest-api/v1/)
- [Mailchimp Marketing API](https://mailchimp.com/developer/marketing/api/)
- [Constant Contact V3 API](https://developer.constantcontact.com/api_reference/index.html)
- [OpenAI Python library / Responses API](https://github.com/openai/openai-python)

## License

MIT
