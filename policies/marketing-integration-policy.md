# Marketing Integration Policy

## INTEGRATION-01 — Approved destinations
Governed audiences may be synchronized to approved marketing platforms such as Mailchimp or Constant Contact. The demo uses mock destinations by default.

## INTEGRATION-02 — Safe demo mode
Real marketing sync is disabled by default. Enabling a real connector requires an explicit runtime flag and provider credentials. Automated tests and public demos must not require live credentials.

## INTEGRATION-03 — Idempotent audience identity
Each audience request receives a stable audience key that is carried into the downstream segment, list, or tag so an operator can correlate marketing-platform state with the application audit trail.
