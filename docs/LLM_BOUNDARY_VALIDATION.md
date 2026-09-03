# LLM boundary validation

The LLM is treated as an untrusted parser, not as an authorization or query engine.
A model response is accepted only after it passes every gate below.

1. **Provider shape** — OpenAI Structured Outputs parses into a closed Pydantic model (`extra="forbid"`) whose fields are all required and strictly typed.
2. **Resource bounds** — the client has explicit timeout/retry/output-token limits and the parsed object has an application-side serialized-size cap.
3. **Application-owned controls** — consent, active-account, suppression, and target-course exclusion flags must all be `true`; application code reasserts them after validation.
4. **Grounding** — course selectors, learner profile, manager, and time windows must be traceable to the original user request. Matching is normalized and token-boundary aware rather than substring based.
5. **Cross-field semantics** — sensitive export classification fields must agree and the target course cannot simultaneously be a positive selector while target-course exclusion is mandatory.
6. **Sensitive-intent backstop** — raw email export has a deterministic detector. The LLM can make the classification stricter but cannot downgrade it.
7. **Authoritative references** — before policy/query execution, course names and learner profiles are checked against database-backed catalogs and canonicalized to stored values.
8. **Fail closed** — boundary failures are retried once from the original user text only. If validation still fails, a stable `LLMBoundaryError` is raised without embedding provider output.

## Residual trust boundaries

Manager identity is only checked for grounding because this reference implementation has no authoritative SSO/RBAC/organization directory. A production deployment should resolve the requester and approving manager from trusted identity/organization data rather than accepting a name from natural-language text.

Grounding is deliberately conservative. If product requirements later add aliases, multilingual requests, or richer course identifiers, extend the deterministic normalization/catalog layer rather than weakening the boundary to accept arbitrary model values.
