# OpenOnce

**Durable side effects for AI agent tool calls.**

Your agent sends an email, opens a PR, issues a refund — then the process crashes, the
LLM retries, or two workers race. Did the effect happen? Should it run again?

OpenOnce wraps a tool call in a crash-safe lifecycle so that:

- **duplicates replay instead of re-executing** — same intent, same receipt, including
  failures (a 400 stays a 400);
- **ambiguous outcomes are never blindly retried** — a timeout mid-charge parks the
  effect as `UNKNOWN` and a reconciler resolves it against the *external world*;
- **every effect leaves an auditable receipt trail** — an append-only journal of every
  state transition, approval, and probe.

It is **not** a workflow engine. No orchestration, no task queues, no server — a library
and a SQLite/Postgres table, embedded in whatever agent framework you already use.

## The honest guarantee

Exactly-once side effects against external systems are physically impossible from a
local process. OpenOnce gives you the strongest thing that exists:
**at-least-once execution + idempotency + reconciliation.**

```
ToolCallPlanned → PolicyChecked → (ApprovalGranted) → Started
              → ReceiptRecorded → Committed

crash between Started and ReceiptRecorded?
              → Unknown → probe the provider → Commit / Re-arm / HumanReview
```

## Quickstart

```python
import openonce

oo = openonce.OpenOnce("openonce.db")   # or ":memory:" — zero infrastructure

@oo.effect(tool="github.create_pr", idempotency_fields=["owner", "repo", "title"])
def create_pr(owner: str, repo: str, title: str, body: str) -> dict:
    ...  # your real tool code

with oo.scope("run-2026-07-02-a"):          # binds dedup to this agent run
    create_pr(owner="acme", repo="api", title="Fix login", body="...")
    # LLM retries with a reworded body — same intent fingerprint, replayed:
    create_pr(owner="acme", repo="api", title="Fix login", body="reworded")
```

`idempotency_fields` is the **intent fingerprint**: only those fields enter the key, so
LLM noise (prose, timestamps) doesn't defeat dedup. Everything else about key
derivation is strict — RFC 8785 canonicalization, floats rejected (use integer minor
units), and derived keys require a run scope so two *runs* that legitimately want the
same call aren't silently collapsed.

### Approvals — re-entrant by design

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

No separate resume path: the agent just retries the call, hits the same idempotency
key, and proceeds.

### Unknown outcomes — the reason this library exists

```python
@oo.effect(tool="stripe.charge")
def charge(amount_cents: int) -> dict:
    ctx = openonce.current_effect()
    # Pass ctx.provider_key as Stripe's Idempotency-Key header: the one hard
    # defense against duplicates once a request has left the process.
    return stripe.Charge.create(..., idempotency_key=ctx.provider_key)
```

If `charge` times out after the request was sent, the effect parks in `UNKNOWN`.
A `Reconciler` (run it from cron, a thread, wherever) probes the provider:

```python
rec = openonce.Reconciler(oo.store, grace_seconds=300)
rec.register("stripe.charge", StripeChargeProber(api_key))
rec.run_once()   # HAPPENED → commit with receipt; NOT_HAPPENED → re-arm;
                 # inconclusive / no prober → human review. Never a blind retry.
```

Or run it as a daemon straight from the CLI (`PROBERS` is a
`dict[str, Prober]` in your code):

```console
$ openonce --db openonce.db reconcile --probers myapp.probers:PROBERS --watch
```

`--db` also accepts a Postgres DSN — the production store
(`pip install openonce[postgres]`) passes the identical semantic test suite
as SQLite and the in-memory reference, including multi-worker
first-writer-wins.

### Three failure classes, explicitly

| class | meaning | behavior |
|---|---|---|
| any exception | definitively failed (business error) | cached & **replayed** for the same key |
| `RetryableEffectError` | definitively did **not** happen | re-executed up to `max_attempts` |
| `UnknownOutcomeError` / timeouts | **may** have happened | parked `UNKNOWN`, reconciled, never blind-retried |

### Stripe: the tier-1 story, end to end

```python
from openonce.providers.stripe import StripeProber, effect_metadata

@oo.effect(tool="stripe.charge", idempotency_fields=["customer", "amount_cents"])
def charge(customer: str, amount_cents: int) -> dict:
    ctx = openonce.current_effect()
    return stripe.PaymentIntent.create(
        amount=amount_cents, currency="usd", customer=customer,
        idempotency_key=ctx.provider_key,       # Stripe dedupes for 24h
        metadata=effect_metadata(ctx),          # the probe target
    )

rec.register("stripe.charge", StripeProber.from_api_key(STRIPE_KEY))
```

Layered defense: even a wrong `NOT_HAPPENED` probe verdict is survivable,
because re-execution reuses the same `provider_key` and Stripe replays the
original response instead of double-charging. The prober is honest about
Stripe Search's indexing lag — a young miss is `INCONCLUSIVE`, not
`NOT_HAPPENED`.

### LangGraph — one decorator, both problems

LangGraph checkpointing re-runs nodes on resume, re-executing side effects
(its docs tell you to "use idempotency keys"). `effect_tool` is that layer:

```python
from openonce.integrations.langgraph import effect_tool

@effect_tool(oo, tool="stripe.refund", idempotency_fields=["charge"])
def refund(charge: str) -> str:
    """Refund a Stripe charge."""
    ...

# scope binds to the LangGraph thread_id automatically; approvals map onto
# interrupt(): the graph pauses, and a human resumes with
graph.invoke(Command(resume={"approved": True, "by": "eric"}), config)
```

The two replay semantics interlock: LangGraph re-runs the node from the top;
OpenOnce approvals are re-entrant (same call, same key, proceeds once
approved) — so the replayed tool call is exactly the right thing, verified
against the real LangGraph runtime in the test suite.

### OpenAI Agents SDK — signals the model can read

```python
from openonce.integrations.openai_agents import OpenOnceRunContext, effect_function_tool

@effect_function_tool(oo, tool="stripe.refund", idempotency_fields=["charge"])
def refund(charge: str) -> str:
    """Refund a Stripe charge."""
    ...

result = Runner.run_sync(agent, "refund ch_1",
                         context=OpenOnceRunContext(openonce_scope="conv-123"))
```

Control-flow becomes **structured JSON tool outputs the model acts on**:
`approval_required` tells it to inform the user and call again with the same
arguments after approval (re-entrant — executes exactly once);
`outcome_unknown` explicitly instructs *do NOT retry*, the reconciler owns it.
Safety semantics, translated into model-readable protocol.

### The receipts, visible

```console
$ openonce --db openonce.db review
* eff_76cd…  requires_approval  stripe.refund   attempt 0/3  2026-07-02 12:01:07Z
1 effect(s) need a human. approve/deny by effect_id.

$ openonce --db openonce.db approve eff_76cd… --by eric
approved eff_76cd… — the agent's next identical call will execute it

$ openonce --db openonce.db show eff_fd61…
* eff_fd61…  committed  pay.charge  attempt 1/3  2026-07-02 12:03:22Z
  journal:
    2026-07-02 12:01:07Z        planned -> approved
    2026-07-02 12:01:07Z       approved -> started
    2026-07-02 12:01:09Z        started -> unknown   {"error": "TimeoutError(...)"}
    2026-07-02 12:03:21Z        unknown -> receipt_recorded  {"probe": "happened", ...}
    2026-07-02 12:03:22Z receipt_recorded -> committed
```

## Install

```bash
pip install openonce          # stdlib-only core, SQLite included
pip install openonce[postgres]  # Postgres store (coming)
```

## Status

Alpha. The core semantics (dedup, replay, approval, UNKNOWN/reconcile, first-writer-wins
under concurrency) are tested against both the in-memory and SQLite stores. On the
roadmap: Postgres store, provider probe library (Stripe/GitHub/email), LangGraph &
OpenAI Agents adapters, MCP proxy.

## Design lineage

Stripe's idempotency keys, AWS Builders' Library (client request IDs, parameter-mismatch
rejection), brandur's rocket-rides-atomic (atomic phases + completer), Temporal's
Activity semantics (at-least-once + "make it idempotent") — scaled down to a library.
Temporal replays *code*; OpenOnce replays *data*. See `PLAN.md` for the full design.
