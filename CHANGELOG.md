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
- Automatic `NOT_HAPPENED` reconciliation now clears any stale `result`
  projection before re-arming the effect, matching the manual
  `resolve_not_happened()` path.
- Provider-contract preflight consults the ledger first: existing effects
  always replay / join / surface their parked state regardless of
  enforcement configuration; preflight only gates the admission of new
  intents (and a preflight failure leaves nothing in the ledger, so the
  same key can be retried after fixing the contract).
- `Reconciler.register()` without `capability=` no longer deletes a
  constructor-supplied capability pin, and neither Runtime nor Reconciler
  alias (or mutate) the caller's `provider_capabilities` dict.
- `approve()` accepts `HUMAN_REVIEW` again (in addition to
  `REQUIRES_APPROVAL`) — the generic human re-arm path;
  `resolve_happened`/`resolve_not_happened` are the evidence-carrying
  alternatives, not replacements.

### Added
- OpenAI Agents adapter: `dedup="call"` — the LLM's `tool_call_id` narrows
  the scope so each model decision is its own effect; retries of the same
  call still dedupe. Auditable via `record.scope`. Default `"intent"`
  remains (required for approval flows).
- Provider capability matrix: `openonce.providers.capabilities` and
  `openonce providers` expose the built-in reconciliation tiers, grace
  windows, probe basis, and whether a miss may safely auto-rearm.
- Reconciler no-prober escalations now include matching provider capability
  guidance, so human review notes explain the missing prober, miss semantics,
  handler requirements, required idempotency fields, and required receipt
  fields.
- Reconciler defaults now use provider capability grace windows when no
  explicit `grace_seconds` is supplied; operator-provided `grace_seconds` and
  `grace_overrides` still take precedence.
- Reconciler capability enforcement (blockers, receipt-evidence checks,
  `can_auto_rearm_on_miss` gating) — **opt-in only**: it applies when the
  operator pins a capability (`register(..., capability=...)` or
  `provider_capabilities`) or sets a strict resolution flag. Without an
  explicit opt-in, baseline behavior is unchanged: registered probers are
  trusted, `NOT_HAPPENED` re-arms, `HAPPENED` commits. Builtin capability
  patterns matching a tool name never silently change deployed behavior
  on upgrade.
- `openonce providers --json` exposes the provider capability matrix as a
  stable machine-readable schema for CI, UIs, and external agent harnesses.
- `openonce providers <tool> --require-known --require-auto-rearm` turns the
  matrix into a CI policy gate: unknown tools fail, and tools with any
  non-authoritative capability fail the auto-rearm gate.
- `openonce providers <tool> --capability <name>` and
  `Reconciler.register(..., capability=...)` disambiguate shared tool names
  like `email.send`, so an authoritative provider-API prober can opt into
  safe auto-rearm without weakening the default SMTP-safe behavior.
- Custom provider capability files: `openonce.load_capabilities_file()`,
  `Reconciler(extra_capabilities=...)`, and
  `openonce providers --capability-file openonce-providers.json` let teams
  keep provider safety knowledge in reviewable JSON and use it in both CI and
  runtime reconciliation.
- `openonce reconcile --capability-file openonce-providers.json` and repeated
  `--provider-capability TOOL=CAPABILITY` pins now feed the same provider
  safety knowledge into the CLI daemon, closing the gap between CI policy gates
  and runtime reconciliation.
- `Reconciler(require_capability_for_resolution=True)` and
  `openonce reconcile --require-provider-capability` add a production
  fail-closed mode: `HAPPENED` and `NOT_HAPPENED` probes cannot resolve unknown
  tools without matching reviewed provider capability metadata.
- `Reconciler(require_conformance_for_resolution=True)` and
  `openonce reconcile --conformance-file ... --require-conformance` let the
  runtime daemon fail closed unless matching provider conformance evidence
  passes for the current capability fingerprint.
- Runtime reconciliation now enforces the conformance invariant that
  `HAPPENED` probes must include a non-empty receipt; receiptless hits route to
  `HUMAN_REVIEW` instead of committing without external evidence.
- Provider capabilities can now declare `required_receipt_fields`; both
  conformance validation and runtime reconciliation enforce those fields before
  accepting `HAPPENED` as externally proven.
- Provider capabilities can also declare `required_receipt_source_fields`;
  conformance validation and runtime reconciliation enforce those natural-key
  source contracts before accepting `HAPPENED`, and capability validation
  requires each source field to be part of `required_idempotency_fields`.
- `OpenOnce(enforce_provider_receipts=True)` adds opt-in handler success-path
  enforcement for the same provider receipt contracts. Successful handler
  returns are recorded, checked against required receipt fields and receipt
  source contracts, and parked in `HUMAN_REVIEW` instead of committed when the
  receipt cannot prove the provider object belongs to the effect.
- `OpenOnce(require_provider_capability_for_receipts=True)` makes receipt
  enforcement fail closed for unknown tools too, so enabling hot-path receipt
  checks cannot silently skip provider validation for uncovered side effects.
- Hot-path provider receipt enforcement now runs a preflight before the handler
  executes: unknown tools are rejected when capabilities are required, required
  handler args must be present, and natural-key `required_idempotency_fields`
  must be in the derived key material.
- Provider receipt contracts now reject missing required handler args in the
  shared receipt validator too, so reconciler, manual resolution, and runtime
  receipt enforcement all fail closed for historical records missing natural
  key inputs.
- Hot-path provider receipt preflight now rejects opaque explicit
  `idempotency_key` values for capabilities that declare
  `required_idempotency_fields`; natural-key contracts must use derived
  `idempotency_fields` so OpenOnce can verify the key material.
- `OpenOnce.resolve_happened(...)` and
  `openonce resolve-happened --receipt-json ...` let humans close
  `UNKNOWN`, `HUMAN_REVIEW`, or stuck `RECEIPT_RECORDED` effects by committing
  reviewed external evidence without re-executing the side effect. When
  provider receipt enforcement is enabled, manual receipts must satisfy the
  same required fields and source contracts before commit.
- `OpenOnce.resolve_not_happened(...)` and `openonce resolve-not-happened`
  make the human retry path explicit: `UNKNOWN`/`HUMAN_REVIEW` effects only
  re-arm after a human records why the external effect did not happen. Optional
  auto-rearm/provider-capability gates let manual retry decisions obey the same
  provider matrix used by the reconciler.
- Manual `resolve_not_happened(require_auto_rearm=True)` now fails closed for
  unknown tools instead of inheriting the reconciler's backward-compatible
  unknown-tool retry default.
- The provider readiness gate now requires capabilities with a named prober to
  declare `required_receipt_fields`, so reviewed provider knowledge includes
  the external receipt fields that prove `HAPPENED`.
- `openonce providers <tool> --requirements` now shows static handler args,
  idempotency fields, and receipt fields alongside handler requirements.
- Provider conformance harness: `openonce.providers.conformance`,
  `openonce.assert_conformance(...)`, and
  `openonce providers --conformance-plan` define the standard prober scenarios
  each capability must prove: happened with receipt, young miss, mature miss,
  and ambiguous reads.
- Provider conformance evidence gate: `openonce.load_conformance_file()`,
  `openonce.validate_conformance_evidence()`, and
  `openonce providers --conformance-file openonce-conformance.json` with
  `--require-conformance` validate recorded prober scenario outcomes in CI.
- JSON conformance evidence scenarios must include a non-empty, non-TODO
  `detail`, so passing evidence records how each fixture was exercised instead
  of only naming the expected outcome.
- Conformance evidence for capabilities with `required_args` must now record
  every required source arg in `source_args`, so natural-key fixtures such as
  GitHub PRs preserve the full `owner/repo/head` proof input.
- Programmatic `validate_conformance()` / `assert_conformance()` now enforce
  the same complete `source_args` requirement as JSON evidence files, so test
  harnesses cannot prove only the receipt-source subset of a natural key.
- Programmatic `CapabilityConformanceEvidence` now fails validation if its
  internal capability name does not match the matrix key it is attached to.
- Conformance evidence is now bound to the capability fingerprint emitted by
  `openonce providers <tool> --json`, so changes to miss semantics, grace
  windows, or handler contracts make stale evidence fail.
- `openonce providers <tool> --conformance-template --json` emits a fill-in
  conformance evidence scaffold with the current capability fingerprint,
  reducing manual scenario/fingerprint drift while keeping unfilled templates
  invalid as evidence.
- `openonce providers <tool> --conformance-file-template` and
  `openonce providers --scan src --conformance-file-template` emit a complete
  `openonce-conformance.json` scaffold for the matching capability set.
- Provider audit scanner: `openonce.scan_effect_tools()` and
  `openonce providers --scan src --require-known --require-auto-rearm` find
  literal or simple module-constant `tool` declarations on OpenOnce decorators
  and apply provider policy/conformance gates across the codebase.
- Structured handler contracts: capabilities can now declare required handler
  args and `idempotency_fields`; `openonce providers --scan src` with
  `--require-handler-contract` verifies those contracts statically, catching
  natural-key drift such as a GitHub PR prober missing `head`.
- Capability validation now requires every `required_idempotency_fields` entry
  to be a declared `required_args` handler input, preventing impossible
  static contracts from entering the matrix.
- Capability validation now rejects duplicate required handler args,
  idempotency fields, receipt fields, and receipt source fields, preventing
  ambiguous provider contracts from entering fingerprints or release gates.
- Static receipt contracts: `openonce providers --scan src` with
  `--require-receipt-contract` verifies that literal handler return dicts,
  `dict(...)` constructors, or once-assigned unmutated local receipt variables
  contain each capability's `required_receipt_fields`, keeping committed
  receipts aligned with prober receipts.
- Receipt source contracts: capabilities can now declare
  `required_receipt_source_fields`; the built-in GitHub PR contract requires
  the returned `head` receipt field to come from the handler's `head` arg, so
  static audits catch hard-coded or unrelated natural-key receipts while
  keeping the source tied to the effect's idempotency key.
- Static receipt source audits now verify that the required source is an actual
  handler argument, not merely a same-named local variable in the function body.
- Static receipt source audits also reject handler args that are reassigned or
  mutated before being returned as provider evidence, preserving the original
  natural-key proof.
- Static receipt source audits now treat `match`/`case` pattern bindings as
  reassignments too, so a case pattern cannot shadow a handler arg or local
  receipt variable and still satisfy receipt proof.
- Local receipt variables only satisfy static receipt audits when assigned once
  at function-body level before the return; branch-local or post-return
  assignments are treated as dynamic.
- Static receipt audits now reject duplicate literal return receipt fields, so
  a later duplicate key cannot override the field that satisfied source proof.
- Static provider audits now invalidate module-level string/list constants when
  later top-level imports, functions, classes, loops, deletes, or assignments
  rebind the same name, including inside conditional, exception, and match
  branches, preventing stale constants from proving tools, idempotency fields,
  or receipt keys.
- Decorator constants are now evaluated from the module constants available
  before the decorator line, so later definitions cannot prove `tool` or
  `idempotency_fields`, while later rebindings do not invalidate an already
  evaluated decorator.
- Scan proof gates now fail on unmatched tools: `--require-conformance`,
  `--require-reviewed`, `--require-handler-contract`, and
  `--require-receipt-contract` cannot pass with an empty capability match set.
- Provider backlog suggestions: `openonce providers --scan src` with
  `--suggest-capabilities --json` emits conservative
  `tier_3_non_authoritative` custom capability stubs for unknown literal tools,
  so uncovered side effects become reviewable provider-knowledge work.
- Provider readiness gate: `openonce providers --capability-file openonce-providers.json`
  with `--validate-only --require-reviewed` fails TODO capability stubs and
  auto-rearm entries without a named prober, keeping backlog drafts out of
  release gates.
- GitHub Actions now runs `openonce providers --validate-only --require-reviewed`,
  so built-in provider capability regressions fail CI before release.
- `openonce providers --capability-file openonce-providers.json --validate-only`
  validates provider safety knowledge without querying a specific tool, and the
  merged matrix now rejects duplicate capability names so `--capability <name>`
  stays unambiguous.
- `GitHubPullRequestProber` now verifies returned PR `head` metadata and treats
  mismatched or duplicate natural-key results as `INCONCLUSIVE`, preventing a
  bad wrapper or ignored filter from turning an unrelated PR into proof that an
  effect happened.

### Changed
- `approve()` / `openonce approve` now apply only to pending pre-execution
  approvals. Outcome review retries should use `resolve_not_happened()` so a
  potentially happened effect is not retried by a generic approval action.

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
