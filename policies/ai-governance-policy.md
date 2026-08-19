# AI Governance Policy

## AI-01 — Model output is not executable authority
The language model may translate natural language into a constrained audience-intent schema. It may not execute arbitrary SQL, alter consent flags, approve its own request, or bypass deterministic policy controls.

## AI-02 — Deterministic enforcement
Consent, suppression, target-course exclusion, approval thresholds, and raw-data-export restrictions are enforced by application code after AI interpretation.

## AI-03 — Explainability and audit
The system must retain the original request, structured intent, relevant policy context, deterministic checks, audience funnel counts, approvals, and downstream sync result in an audit trail.
