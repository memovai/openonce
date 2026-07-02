# Changelog

## Unreleased

### Fixed (full-codebase review findings)
- **Critical**: third-party HTTP timeouts (requests/httpx/urllib3/aiohttp)
  were classified as definitive failures because they don't subclass builtin
  `TimeoutError` — "maybe happened" was cached and replayed as "didn't
  happen". Classification now walks `type(exc).__mro__` class names;
  connect-phase failures auto-retry. `classify_exception` is public API.
- Probers measured lag windows from `created_at`; a long approval delay
  could turn a still-propagating miss into a false `NOT_HAPPENED`
  (duplicate email on the authoritative path). Now measured from `updated_at`.
- Handler results are canonicalized (JSON round-trip) so the first caller
  and every replay observe the identical value; non-serializable results
  degrade to `repr()` with a `RuntimeWarning` for everyone consistently.

### Added
- OpenAI Agents adapter: `dedup="call"` — the LLM's `tool_call_id` narrows
  the scope so each model decision is its own effect; retries of the same
  call still dedupe. Auditable via `record.scope`. Default `"intent"`
  remains (required for approval flows).

## 0.1.0 — 2026-07-02

Initial release: durable side effects for AI agent tool calls.
Honest guarantee — at-least-once execution + idempotency + reconciliation.

### Core
- Effect ledger: explicit state machine with a frozen transition table;
  `UNKNOWN` is a first-class state resolved only by probing the external
  world or by a human — never by a blind retry.
- Idempotency keys: explicit caller keys first-class; derived keys via
  RFC 8785-compatible canonicalization + `idempotency_fields` intent
  whitelist. Floats and >2**53 ints rejected (cross-language key drift).
  Same key + different payload rejected (Stripe parameter-mismatch).
- Results cached and replayed for duplicate keys — failures included
  (a 400 stays a 400).
- Three failure classes: definitive failure (replay), `RetryableEffectError`
  (re-execute up to `max_attempts`), `UnknownOutcomeError`/timeouts (park).
- Atomic-phase execution engine with first-writer-wins admission, leases,
  and a state-driven join path for duplicate callers and lost CAS races.
- Re-entrant approvals: approve, then retry the same call — executes once.
- Reconciler: grace periods (per-tool overrides), prober-driven resolution,
  escalation to `HUMAN_REVIEW` whenever the world can't be read.
- Async handlers: identical semantics; the event loop is never blocked.

### Stores
- `InMemoryStore` (reference/spec), `SQLiteStore` (zero-infra durable),
  `PostgresStore` (production; `openonce[postgres]`). The entire semantic
  test suite runs against all three.

### Providers
- `StripeProber` — tier-1 reference (native idempotency keys): metadata
  search, indexing-lag honesty, 24h provider-key dedup backstop.
- `GitHubPullRequestProber` — tier-2 reference (natural business key):
  authoritative list-by-head read.

### Integrations
- LangGraph: `effect_tool` — thread-scoped dedup; `ApprovalPending` maps
  onto `interrupt()`; node-replay and re-entrant approvals interlock.
- OpenAI Agents SDK: `effect_function_tool` — control-flow signals become
  structured JSON tool outputs the model reads and acts on.

### CLI
- `openonce ls / review / show / approve / deny / reconcile [--watch]`;
  `--db` accepts a SQLite path or Postgres DSN.
