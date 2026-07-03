# OpenOnce

**Durable side effects for AI agent tool calls.**

**English** | [日本語](README.ja.md) | [中文](README.zh.md) | [Español](README.es.md)

---

Your agent sends an email, opens a PR, issues a refund — then the process
crashes, the LLM retries, or two workers race. Did the effect happen? Should
it run again?

The most dangerous window in any agent system is the one between *"the tool
call left the building"* and *"the result was recorded"*. Crash inside it and
you face the question no retry policy can answer: **did it happen?**

OpenOnce wraps a tool call in a crash-safe lifecycle so that:

- **Duplicates replay instead of re-executing.** Same intent, same receipt —
  including failures (a 400 stays a 400).
- **Ambiguous outcomes are never blindly retried.** A timeout mid-charge
  parks the effect as `UNKNOWN`; a reconciler resolves it against the
  *external world*, or a human does.
- **Every effect leaves an auditable receipt trail.** An append-only journal
  of every state transition, approval, and probe.

It is **not** a workflow engine. No orchestration, no task queues, no server —
a library and a SQLite/Postgres table, embedded in whatever agent framework
you already use.

## The honest guarantee

Exactly-once side effects against external systems are physically impossible
from a local process. OpenOnce gives you the strongest thing that exists:

> **at-least-once execution + idempotency + reconciliation**

```
Planned → PolicyChecked → (ApprovalGranted) → Started
        → ReceiptRecorded → Committed

crash between Started and ReceiptRecorded?
        → Unknown → probe the provider → Commit / Re-arm / HumanReview
```

## Install

```bash
pip install openonce             # stdlib-only core, SQLite included
pip install openonce[postgres]   # production store
```

## Quickstart

The minimal form is one decorator and one `with` — no names, no config:

```python
import openonce

oo = openonce.OpenOnce("openonce.db")   # or ":memory:" — zero infrastructure

@oo.effect                              # tool name = function name
def send_email(to: str, subject: str) -> str:
    ...

with oo.scope():                        # auto run id: in-run dedup
    send_email("a@b.c", "hi")
    send_email("a@b.c", "hi")           # LLM retry → replayed, not re-sent

protected = oo.effect(existing_tool)    # or wrap an existing callable directly
```

Dial in precision as you need it:

```python
@oo.effect(idempotency_fields=["owner", "repo", "head"])
def create_pr(owner: str, repo: str, head: str, title: str, body: str) -> dict:
    ...  # your real tool code

with oo.scope("run-2026-07-02-a"):          # stable id: crash-resume dedup too
    create_pr(owner="acme", repo="api", head="fix-login", title="Fix login", body="...")
    # The LLM retries with a reworded body — same intent fingerprint, replayed:
    create_pr(owner="acme", repo="api", head="fix-login", title="Fix login", body="reworded")
```

`idempotency_fields` is the **intent fingerprint**: only those fields enter
the key, so LLM noise (prose, timestamps) doesn't defeat dedup. Everything
else about key derivation is strict — RFC 8785 canonicalization, floats
rejected (use integer minor units), and derived keys require a run scope so
two *runs* that legitimately want the same call aren't silently collapsed.

Async handlers work identically, and the event loop is never blocked:

```python
@oo.effect(tool="email.send", idempotency_fields=["to", "subject"])
async def send(to: str, subject: str, body: str) -> str:
    ...
```

## Three failure classes, explicitly

| class | meaning | behavior |
|---|---|---|
| any exception | definitively failed (business error) | cached & **replayed** for the same key |
| `RetryableEffectError`, connect-phase network errors | definitively did **not** happen | re-executed up to `max_attempts` |
| `UnknownOutcomeError`, read-phase timeouts & disconnects | **may** have happened | parked `UNKNOWN`, reconciled, never blind-retried |

Classification understands real-world libraries: `requests.ReadTimeout`,
`httpx.TimeoutException`, and friends are recognized by class name across the
exception's MRO — a read timeout parks as `UNKNOWN`, a connect timeout
(nothing was sent) retries automatically. Fully configurable, and explicit
`raise` always wins.

## Approvals — re-entrant by design

```python
oo = openonce.OpenOnce("openonce.db",
                       policy=openonce.require_approval_for(["stripe.*"]))

try:
    refund(charge="ch_1", amount_cents=500)
except openonce.ApprovalPending as p:
    notify_human(p.effect_id)

# later, after oo.approve(effect_id):
refund(charge="ch_1", amount_cents=500)   # same call, same key → executes once
```

No separate resume path: the agent just retries the call, hits the same
idempotency key, and proceeds.

## Unknown outcomes — the reason this library exists

```python
@oo.effect(tool="stripe.charge")
def charge(amount_cents: int) -> dict:
    ctx = openonce.current_effect()
    # Pass ctx.provider_key as Stripe's Idempotency-Key header: the one hard
    # defense against duplicates once a request has left the process.
    return stripe.PaymentIntent.create(..., idempotency_key=ctx.provider_key)
```

If `charge` times out after the request was sent, the effect parks in
`UNKNOWN`. A `Reconciler` probes the provider:

```python
rec = openonce.Reconciler(oo.store)
rec.register("stripe.charge", StripeProber.from_api_key(STRIPE_KEY))
rec.run_once()   # HAPPENED → commit with receipt; NOT_HAPPENED → re-arm;
                 # inconclusive / no prober → human review. Never a blind retry.
```

When no explicit `grace_seconds` is passed, the reconciler uses the provider
capability matrix's default grace: Stripe and Email wait through their lag
windows; GitHub PR lookup can reconcile immediately. Pass `grace_seconds=` or
`grace_overrides=` when you need an explicit operator override.

If one tool name maps to more than one provider behavior, pin the capability
when registering the prober:

```python
rec.register("email.send", gmail_prober, capability="email_provider_api")
```

Teams can also load their own provider capability file and use it at runtime:

```python
extra = openonce.load_capabilities_file("openonce-providers.json")
rec = openonce.Reconciler(oo.store, extra_capabilities=extra)
rec.register("slack.post_message", slack_prober, capability="slack_metadata")
```

Or run it as a daemon straight from the CLI:

```console
$ openonce --db openonce.db reconcile \
    --probers myapp.probers:PROBERS \
    --capability-file openonce-providers.json \
    --provider-capability slack.post_message=slack_metadata \
    --require-provider-capability \
    --conformance-file openonce-conformance.json \
    --require-conformance \
    --watch
```

`--capability-file` and `--provider-capability TOOL=CAPABILITY` use the same
reviewable provider knowledge as the Python API, so the daemon enforces the
same miss semantics that passed CI. `--require-provider-capability` is the
production fail-closed mode: `HAPPENED` and `NOT_HAPPENED` probe results
cannot resolve an unknown tool unless the tool has matching reviewed provider
capability metadata.

`--conformance-file` and `--require-conformance` extend that fail-closed mode
to prober behavior: the daemon will not commit or re-arm from a definitive
probe result unless the matching capability's current fingerprint and standard
scenarios have passing evidence.

Runtime reconciliation also requires `HAPPENED` probes to return a non-empty
receipt. Without external evidence, the effect stays in human review instead
of being committed without an audit trail.

## Providers: three honesty tiers

Probing "did this effect actually happen?" is provider-specific knowledge.
The bundled probers are reference implementations of the three tiers:

| tier | example | probe basis | a miss means |
|---|---|---|---|
| 1 — native idempotency keys | **Stripe** | metadata search (+24h provider-key dedup backstop) | inconclusive inside the index lag window, then not-happened |
| 2 — natural business key | **GitHub PR** (`owner`, `repo`, `head`) | authoritative primary-store read | genuinely not-happened |
| 2/3 — sender-controlled key | **Email** (deterministic `Message-ID`) | sent-store search | not-happened only if the sent store is authoritative; bare SMTP escalates to a human, forever |

That matrix is also executable product surface:

```console
$ openonce providers
provider capability matrix
name                tool               tier                                     auto_rearm  grace_s prober
stripe              stripe.*           tier_1_native_idempotency_key            yes         120     StripeProber
github_pr           github.create_pr   tier_2_natural_business_key              yes         0       GitHubPullRequestProber
email_provider_api  email.send         tier_2_sender_controlled_authoritative   yes         120     EmailMessageIdProber(authoritative=True)
email_smtp          email.send         tier_3_non_authoritative                 no          120     EmailMessageIdProber(authoritative=False)
```

`auto_rearm=no` is the important bit: a miss is not evidence. OpenOnce keeps
the effect in the human queue instead of turning uncertainty into a duplicate
external action. The reconciler enforces this too: if a prober returns
`NOT_HAPPENED` for a known non-authoritative tool, OpenOnce escalates to
`HUMAN_REVIEW` instead of re-arming.

For a specific tool, ask for the exact handler contract:

```console
$ openonce providers stripe.charge --requirements
  requirements:
    handler:
      - pass current_effect().provider_key as Stripe Idempotency-Key
      - stamp effect_metadata(current_effect()) on the created object
    receipt fields: stripe_id
```

Ask for the conformance plan when you are writing or reviewing a prober:

```console
$ openonce providers stripe.charge --conformance-plan
  conformance:
    - happened: expect happened + receipt
      receipt fields: stripe_id
    - young_miss: expect inconclusive
    - mature_miss: expect not_happened
    - ambiguous: expect inconclusive
```

The same contract is available in tests:

```python
from openonce import ConformanceScenario, ProbeOutcome, ProbeResult, assert_conformance

assert_conformance(
    stripe_capability,
    {
        ConformanceScenario.HAPPENED: ProbeResult(
            ProbeOutcome.HAPPENED, receipt={"stripe_id": "pi_1"}
        ),
        ConformanceScenario.YOUNG_MISS: ProbeResult(ProbeOutcome.INCONCLUSIVE),
        ConformanceScenario.MATURE_MISS: ProbeResult(ProbeOutcome.NOT_HAPPENED),
        ConformanceScenario.AMBIGUOUS: ProbeResult(ProbeOutcome.INCONCLUSIVE),
    },
)
```

CI can validate the same evidence from JSON:

```json
{
  "schema_version": 1,
  "capabilities": {
    "github_pr": {
      "capability_fingerprint": "sha256:...",
      "source_args": {"owner": "acme", "repo": "api", "head": "fix-login"},
      "scenarios": {
        "happened": {
          "outcome": "happened",
          "receipt": {"number": 42, "head": "fix-login"},
          "detail": "fixture returned exactly one PR for acme:fix-login"
        },
        "mature_miss": {
          "outcome": "not_happened",
          "detail": "fixture returned no PRs for acme:fix-login"
        },
        "ambiguous": {
          "outcome": "inconclusive",
          "detail": "fixture returned duplicate PRs for acme:fix-login"
        }
      }
    }
  }
}
```

```console
$ openonce providers github.create_pr \
    --conformance-file openonce-conformance.json \
    --require-conformance
conformance: passed
```

The fingerprint is emitted by `openonce providers github.create_pr --json`.
If capability semantics change, stale evidence fails until regenerated.
To avoid hand-copying scenario names and fingerprints, generate a fill-in
`openonce-conformance.json` template from the current capability contract:

```console
$ openonce providers github.create_pr --conformance-file-template > openonce-conformance.json
```

The template includes TODO `source_args`, `outcome: null`, `receipt: null`,
and TODO `detail` fields; it is a scaffold, not passing evidence, until the
full fixture key, observed prober outputs, and fixture details are filled in.

You can audit the tools declared in a Python project instead of listing them
one by one:

```console
$ openonce providers --scan src \
    --require-known \
    --require-auto-rearm \
    --require-handler-contract \
    --require-receipt-contract \
    --conformance-file openonce-conformance.json \
    --require-conformance
scan: passed
```

The scanner recognizes literal `tool="..."` values and simple module-level
constants on `@oo.effect(...)`, `@effect_tool(...)`, and
`@effect_function_tool(...)`. Dynamic tool expressions are reported as
unauditable when a gate is enabled. `--require-handler-contract` also checks
structured provider contracts that can be proven statically; for example,
`github.create_pr` must have `owner`, `repo`, and `head` in both the handler
signature and `idempotency_fields`, and its successful probe receipt must carry
the PR number and verified head.

`--require-receipt-contract` checks the handler side of that same receipt
contract. It is intentionally conservative: every return path must be a
literal receipt dict, a `dict(...)` constructor, or an unmutated local variable
assigned once to such a dict, containing the capability's
`required_receipt_fields`. Dynamic SDK returns and mutated receipt dicts fail
the static proof until wrapped into an explicit receipt dict.

Capabilities can also require receipt fields to come from specific handler
args. The built-in `github_pr` contract requires the returned receipt `head`
to be sourced from the handler's `head` arg, so a hard-coded or unrelated PR
head cannot satisfy the static proof, conformance evidence, or runtime
reconciliation commit.
Those source args must also be declared in `required_idempotency_fields`, so
the proof ties the dedupe key, handler input, and external receipt together.

By default, OpenOnce keeps handler success behavior backward-compatible:
whatever the handler returns is recorded and replayed. If you want the hot path
to fail closed against the provider matrix too, opt in at construction time:

```python
oo = openonce.OpenOnce(
    "openonce.db",
    enforce_provider_receipts=True,
    require_provider_capability_for_receipts=True,
    provider_capabilities={"github.create_pr": "github_pr"},
)
```

With enforcement enabled, a successful handler return is recorded as the
receipt, checked against the matching capability's `required_receipt_fields`
and `required_receipt_source_fields`, then committed only if the contract
passes. A mismatch moves the effect to `HUMAN_REVIEW` after
`RECEIPT_RECORDED`, preserving the returned value for audit while blocking
automatic commit and duplicate replay.
Before the handler runs, matching capabilities also require every
`required_args` field to be present in the call and every
`required_idempotency_fields` entry to be part of the derived key material
using `idempotency_fields`. For those natural-key contracts, an opaque explicit
`idempotency_key` is rejected because OpenOnce cannot verify it contains the
provider key fields. This prevents a tool such as `github.create_pr` from
replaying a receipt for one `head` branch when a later call changes `head`.
`require_provider_capability_for_receipts=True` makes unknown tools fail closed
before the handler runs instead of silently skipping receipt-contract
validation.

After reviewing the provider directly, commit external evidence instead of
re-running the effect:

```python
oo.resolve_happened(
    effect_id,
    receipt={"stripe_id": "pi_1"},
    by="eric",
    reason="matched in Stripe dashboard",
)
```

`approve()` means "allow the next identical call to execute." `resolve_happened()`
means "it already happened; commit this receipt and replay it to duplicates."
If `OpenOnce(enforce_provider_receipts=True)` is enabled, manual receipts are
checked against the same provider receipt contract before commit. Pass
`require_provider_capability=True`, or set
`require_provider_capability_for_receipts=True` on `OpenOnce`, when unknown
tools should fail closed too.

If the human review proves the effect did not happen, re-arm explicitly:

```python
oo.resolve_not_happened(
    effect_id,
    by="eric",
    reason="not found in provider dashboard",
)
```

`approve()` is only for pending pre-execution approvals. Use
`resolve_not_happened()` for `UNKNOWN`/`HUMAN_REVIEW` effects so retries require
an explicit "did not happen" decision. `reason` is required because this path
does not have a receipt; the ledger needs the operator's evidence trail.
Pass `require_auto_rearm=True` and `require_provider_capability=True` when the
manual retry decision must obey the provider matrix's auto-rearm policy.
`require_auto_rearm=True` fails closed for unknown tools; the provider matrix
must explicitly say that a miss is safe to re-arm.

Proof gates such as `--require-conformance`, `--require-reviewed`, and
`--require-handler-contract` also fail unknown tools. No matching capability
means there is no reviewed provider contract to prove.

The scanner can generate the conformance evidence scaffold for exactly the
capabilities used by the codebase:

```console
$ openonce providers --scan src --conformance-file-template > openonce-conformance.json
```

For unknown tools, generate a conservative backlog stub:

```console
$ openonce providers --scan src --suggest-capabilities --json
```

The suggested entries default to `tier_3_non_authoritative` and
`can_auto_rearm_on_miss=false`. Treat them as review work: fill in the probe
basis, risk, handler contract, and conformance evidence before trusting a miss.

Use `--require-reviewed` in release gates so TODO stubs, auto-rearm entries
without probers, and prober entries without required receipt fields cannot pass
as finished provider knowledge:

```console
$ openonce providers --capability-file openonce-providers.json \
    --validate-only \
    --require-reviewed
readiness: passed
```

Or consume the same safety knowledge from CI, UIs, or agent harnesses:

```console
$ openonce providers email.send --json
```

Output excerpt:

```json
{
  "schema_version": 1,
  "tool": "email.send",
  "capabilities": [
    {
      "name": "email_smtp",
      "can_auto_rearm_on_miss": false,
      "miss_semantics": "inconclusive forever: sent copy can fail after delivery"
    }
  ]
}
```

For CI, turn the matrix into a hard gate:

```console
$ openonce providers stripe.charge --require-known --require-auto-rearm
policy: passed

$ openonce providers email.send --require-auto-rearm --json
{
  "policy": {
    "passed": false,
    "required": ["auto_rearm_on_miss"],
    "checks": {
      "auto_rearm_on_miss": {
        "passed": false,
        "reason": "'email.send' is not safe to auto-rearm on a miss; blocking capabilities: email_smtp"
      }
    }
  }
}
```

`--require-known` fails unknown tools. `--require-auto-rearm` fails unless every
matching built-in capability can safely convert a post-grace miss into a retry.
That conservative rule is intentional: `email.send` is blocked because the same
tool name covers both provider-API sends and bare SMTP. Disambiguate the
capability when your integration is more specific:

```console
$ openonce providers email.send --capability email_provider_api --require-auto-rearm
policy: passed
```

Repo-specific provider knowledge lives in the same schema. Example
`openonce-providers.json`:

```json
{
  "schema_version": 1,
  "capabilities": [
    {
      "name": "slack_metadata",
      "tool_pattern": "slack.post_message",
      "tier": "tier_2_sender_controlled_authoritative",
      "key_strategy": "message metadata event_payload.openonce_effect_id",
      "probe_basis": "conversations.history search by metadata",
      "miss_semantics": "not-happened after Slack history propagation",
      "can_auto_rearm_on_miss": true,
      "default_grace_seconds": 30,
      "prober": "SlackMetadataProber",
      "handler_requirements": [
        "stamp openonce effect_id into Slack message metadata",
        "grant the prober history scope for the target channel"
      ],
      "risk": "medium: depends on Slack retention and history permissions",
      "required_receipt_fields": ["ts"]
    }
  ]
}
```

Use that file as a reviewable CI gate:

```console
$ openonce providers --capability-file openonce-providers.json --validate-only
provider capability matrix valid: 5 capability(ies), 1 custom from openonce-providers.json

$ openonce providers slack.post_message \
    --capability-file openonce-providers.json \
    --require-known \
    --require-auto-rearm
policy: passed
```

If an `UNKNOWN` effect has no registered prober, the reconciler writes the
matching capability guidance into the human-review note. Operators see the
missing prober, the miss semantics, handler requirements, required
idempotency fields, required receipt fields, and receipt source contracts in
the same place they approve, deny, or investigate the effect.

`GitHubPullRequestProber` also verifies returned PR `head` metadata before it
records `HAPPENED`. If GitHub or a wrapper returns unrelated PRs, or multiple
PRs match the same natural key, the result is `INCONCLUSIVE` and stays in human
review instead of trusting a suspicious read.

## Framework integrations

**LangGraph** — one decorator makes a handler both a durable effect and a
LangGraph tool. Scope binds to the `thread_id`; `ApprovalPending` maps onto
`interrupt()`, and because OpenOnce approvals are re-entrant, LangGraph's
node-replay-on-resume is exactly the right thing:

```python
from openonce.integrations.langgraph import effect_tool

@effect_tool(oo, tool="stripe.refund", idempotency_fields=["charge"])
def refund(charge: str) -> str:
    """Refund a Stripe charge."""
    ...

graph.invoke(Command(resume={"approved": True, "by": "eric"}), config)
```

**OpenAI Agents SDK** — control-flow becomes structured JSON tool outputs the
model reads and acts on: `approval_required` teaches it to inform the user and
call again with the same arguments after approval; `outcome_unknown` instructs
*do NOT retry*. Optional `dedup="call"` scopes dedup to a single model
decision via the `tool_call_id`:

```python
from openonce.integrations.openai_agents import OpenOnceRunContext, effect_function_tool

@effect_function_tool(oo, tool="stripe.refund", idempotency_fields=["charge"])
def refund(charge: str) -> str:
    """Refund a Stripe charge."""
    ...

result = Runner.run_sync(agent, "refund ch_1",
                         context=OpenOnceRunContext(openonce_scope="conv-123"))
```

## The receipts, visible

```console
$ openonce --db openonce.db review
* eff_76cd…  requires_approval  stripe.refund   attempt 0/3  2026-07-02 12:01:07Z
1 effect(s) need a human. Use approve for pending approvals, resolve-happened to commit evidence, resolve-not-happened to retry, or deny pending approvals.

$ openonce --db openonce.db resolve-happened eff_fd61… \
    --require-receipt-contract \
    --receipt-json '{"stripe_id":"pi_1"}' \
    --by eric \
    --reason "matched in Stripe dashboard"
resolved eff_fd61… as happened — committed with manual receipt

$ openonce --db openonce.db resolve-not-happened eff_ab12… \
    --by eric \
    --require-auto-rearm \
    --reason "not found in provider dashboard"
resolved eff_ab12… as not happened — re-armed for retry

$ openonce --db openonce.db show eff_fd61…
  journal:
    2026-07-02 12:01:07Z        planned -> approved
    2026-07-02 12:01:07Z       approved -> started
    2026-07-02 12:01:09Z        started -> unknown   {"error": "TimeoutError(...)"}
    2026-07-02 12:03:21Z        unknown -> receipt_recorded  {"probe": "happened", ...}
    2026-07-02 12:03:22Z receipt_recorded -> committed
```

`--require-receipt-contract` fails if the receipt does not satisfy a matching
provider capability. Use `--capability-file` and
`--provider-capability TOOL=CAPABILITY` here for the same custom or ambiguous
providers you use with `reconcile`.
`resolve-not-happened --require-auto-rearm` uses that same provider knowledge
to block manual re-arm for non-authoritative tools.

`--db` accepts a SQLite path or a Postgres DSN.

## Design

Lineage: Stripe's idempotency keys, AWS Builders' Library (client request
IDs, parameter-mismatch rejection), brandur's rocket-rides-atomic (atomic
phases + completer), Temporal's Activity semantics (at-least-once + "make it
idempotent") — scaled down to a library.

The key divergence from Temporal: **Temporal replays code, OpenOnce replays
data.** An agent's "workflow" is LLM inference and cannot be deterministically
re-run, so OpenOnce persists at the effect level and never imposes determinism
constraints on your code.

## Status

Alpha. The semantics (dedup, replay, approval, UNKNOWN/reconcile,
first-writer-wins under concurrency) are covered by a test suite that runs
identically against the in-memory, SQLite, and Postgres stores, plus
integration tests against the real LangGraph and OpenAI Agents SDK runtimes.

## License

[MIT](LICENSE)
