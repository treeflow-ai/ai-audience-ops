# Changelog

## 0.1.2

Public-repository review and integration-hardening release.

- Added published short and engineering demo links to the README and demo guide.
- Removed generated/local runtime artifacts from the public package and expanded `.gitignore` / `.dockerignore`.
- Made Docker Compose runnable in default mock mode without requiring a local `.env` file.
- Changed mock sync logs to aggregate-only output with no contact-level identifiers.
- Added per-instance environment loading and fail-fast configuration validation.
- Added Pydantic bounds for request length, date windows, identity fields, and confidence.
- Renamed misleading `POLICY RAG` / `AI confidence` UI labels to `POLICY RETRIEVAL` / `Intent confidence`.
- Changed mock audit actor wording from `AI intent parser` to `Intent parser`.
- Added pagination to the LearnDash user-courses adapter.
- Made synthetic seeding atomic and resilient to a courses-only partial initialization.
- Replaced broad route-level exception handling with explicit 400/404/502 error classes so server bugs are not disguised as user errors.
- Updated the Constant Contact adapter to reuse a stable list on retry and wait for asynchronous bulk-import completion before reporting `SYNCED`.
- Expanded regression/safety tests and GitHub Actions coverage to Python 3.11–3.13; added Dependabot configuration.
- Removed private positioning notes from the public repository package.

## 0.1.1

Bug-fix and hardening release for the reference implementation.

- Fixed the BLOCKED-request web flow that could raise SQLAlchemy `DetachedInstanceError` after commit.
- Re-loads committed BLOCKED requests before returning them and captures redirect IDs while the session is active.
- Expanded deterministic raw-email-export detection for common download/show/spreadsheet wording.
- Enforces consent, active-account, suppression, and target-course exclusion as application-owned controls even if a parser/LLM attempts to disable them.
- Enables SQLite foreign-key enforcement.
- Makes forced synthetic-data reseeding remove dependent audit and audience-member rows before parent records.
- Added regression tests for form/API BLOCKED flows, governance-control enforcement, raw-export variants, and repeatable reseeding.
