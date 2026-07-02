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

```python
import openonce

oo = openonce.OpenOnce("openonce.db")   # or ":memory:" — zero infrastructure

@oo.effect(tool="github.create_pr", idempotency_fields=["owner", "repo", "title"])
def create_pr(owner: str, repo: str, title: str, body: str) -> dict:
    ...  # your real tool code

with oo.scope("run-2026-07-02-a"):          # binds dedup to this agent run
    create_pr(owner="acme", repo="api", title="Fix login", body="...")
    # The LLM retries with a reworded body — same intent fingerprint, replayed:
    create_pr(owner="acme", repo="api", title="Fix login", body="reworded")
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
rec = openonce.Reconciler(oo.store, grace_seconds=300)
rec.register("stripe.charge", StripeProber.from_api_key(STRIPE_KEY))
rec.run_once()   # HAPPENED → commit with receipt; NOT_HAPPENED → re-arm;
                 # inconclusive / no prober → human review. Never a blind retry.
```

Or run it as a daemon straight from the CLI:

```console
$ openonce --db openonce.db reconcile --probers myapp.probers:PROBERS --watch
```

## Providers: three honesty tiers

Probing "did this effect actually happen?" is provider-specific knowledge.
The bundled probers are reference implementations of the three tiers:

| tier | example | probe basis | a miss means |
|---|---|---|---|
| 1 — native idempotency keys | **Stripe** | metadata search (+24h provider-key dedup backstop) | inconclusive inside the index lag window, then not-happened |
| 2 — natural business key | **GitHub PR** (`owner`, `repo`, `head`) | authoritative primary-store read | genuinely not-happened |
| 2/3 — sender-controlled key | **Email** (deterministic `Message-ID`) | sent-store search | not-happened only if the sent store is authoritative; bare SMTP escalates to a human, forever |

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
1 effect(s) need a human. approve/deny by effect_id.

$ openonce --db openonce.db show eff_fd61…
  journal:
    2026-07-02 12:01:07Z        planned -> approved
    2026-07-02 12:01:07Z       approved -> started
    2026-07-02 12:01:09Z        started -> unknown   {"error": "TimeoutError(...)"}
    2026-07-02 12:03:21Z        unknown -> receipt_recorded  {"probe": "happened", ...}
    2026-07-02 12:03:22Z receipt_recorded -> committed
```

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
