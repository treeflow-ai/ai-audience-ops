# Demo Guide

## Published videos

- **Short overview (~2 min):** https://youtu.be/9STwVwbaNr0
- **Engineering walkthrough (~4 min):** https://youtu.be/eUC1dkTPuW0

The short video is the fastest introduction. The engineering walkthrough focuses on trust boundaries, deterministic enforcement, and the three workflow paths.

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
6. the mock sync succeeds without writing contact-level identifiers to the local mock log.

### Scenario 2 — prohibited raw-email export

Use the raw-email preset.

The expected result is `BLOCKED` before audience evaluation. Call out `DATA-03` and the `REQUEST_BLOCKED` audit event.

### Scenario 3 — human approval

Use the large-audience preset.

The expected result is **6,709 eligible recipients** and `REVIEW_REQUIRED`. Approve as Jane Smith, then sync.

## Engineering takeaway

The central design decision is the separation between probabilistic interpretation and deterministic authority:

- the parser interprets business language;
- application code owns policy and data-access controls;
- large audiences require an explicit human state transition; and
- downstream writes occur only after the governed audience has been established.
