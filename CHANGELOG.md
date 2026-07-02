# Changelog

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
