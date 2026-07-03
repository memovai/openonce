"""OpenOnce + Claude SDK (anthropic, manual tool-use loop) — no adapter needed.

Run offline (no API key needed):  uv run python examples/anthropic_sdk.py

In the Claude API's manual agentic loop YOU execute the tool_use blocks, so
the core decorator is the whole integration. The mapping:

- scope             -> your conversation/session id
- tool_use block id -> optionally narrows the scope to one model decision
- ctx.provider_key  -> pass through to providers that support idempotency
                       keys (e.g. Stripe's Idempotency-Key header)
"""

import json

import openonce

oo = openonce.OpenOnce(policy=openonce.require_approval_for(["stripe.*"]))
CHARGED: list[dict] = []  # the "external world"


# ── Tool handlers: the decorator IS the integration ──────────────────────────


# Claude tool names can't contain dots — give the LEDGER name explicitly so
# policies like require_approval_for(["stripe.*"]) still match:
@oo.effect(tool="stripe.refund", idempotency_fields=["charge", "amount_cents"])
def stripe_refund(charge: str, amount_cents: int) -> str:
    ctx = openonce.current_effect()
    # ctx.provider_key -> stripe.Refund.create(..., idempotency_key=ctx.provider_key)
    CHARGED.append({"charge": charge, "amount": amount_cents, "key": ctx.provider_key})
    return f"refunded {charge}"


HANDLERS = {"stripe_refund": stripe_refund}

TOOLS = [
    {  # the ordinary Claude tool schema — nothing OpenOnce-specific
        "name": "stripe_refund",
        "description": "Refund a Stripe charge. Call when the user asks for a refund.",
        "input_schema": {
            "type": "object",
            "properties": {
                "charge": {"type": "string"},
                "amount_cents": {"type": "integer"},
            },
            "required": ["charge", "amount_cents"],
        },
    }
]


def execute_tool_use(conversation_id: str, block) -> dict:
    """Dispatch one tool_use block durably; returns the tool_result block."""
    handler = HANDLERS[block.name]
    with oo.scope(f"{conversation_id}/call:{block.id}"):
        try:
            content = json.dumps(handler(**block.input))
        except openonce.ApprovalPending as p:
            content = json.dumps(
                {
                    "openonce": "approval_required",
                    "effect_id": p.effect_id,
                    "instructions": "Tell the user; after approval, call again "
                    "with the same arguments.",
                }
            )
        except openonce.EffectUnknown as u:
            content = json.dumps(
                {
                    "openonce": "outcome_unknown",
                    "effect_id": u.record.effect_id,
                    "instructions": "Do NOT retry; a reconciler will resolve it.",
                }
            )
    return {"type": "tool_result", "tool_use_id": block.id, "content": content}


# ── Real usage (needs the anthropic SDK + credentials) ───────────────────────
#
#   import anthropic
#   client = anthropic.Anthropic()
#   messages = [{"role": "user", "content": "refund ch_1 for $5"}]
#   while True:
#       response = client.messages.create(
#           model="claude-opus-4-8", max_tokens=16000,
#           tools=TOOLS, messages=messages,
#       )
#       if response.stop_reason != "tool_use":
#           break
#       messages.append({"role": "assistant", "content": response.content})
#       results = [
#           execute_tool_use("conv-123", b)
#           for b in response.content if b.type == "tool_use"
#       ]
#       messages.append({"role": "user", "content": results})  # ONE user message
#
# ── Offline demo: simulate Claude's tool_use blocks ──────────────────────────


class FakeToolUse:  # shaped like anthropic's ToolUseBlock
    def __init__(self, id: str, name: str, input: dict) -> None:
        self.id, self.name, self.input = id, name, input


call = FakeToolUse("toolu_01A", "stripe_refund", {"charge": "ch_1", "amount_cents": 500})

# 1) Policy gates the refund — Claude receives a JSON signal it can act on:
result = execute_tool_use("conv-123", call)
signal = json.loads(result["content"])
print(f"1) tool_result to Claude: {signal['openonce']} (effect={signal['effect_id']})")
assert CHARGED == []

# 2) A human approves; Claude re-issues the call (same id on its retry path,
#    or your app replays it) — same key, executes exactly once:
oo.approve(signal["effect_id"], by="eric")
result = execute_tool_use("conv-123", call)
print(f"2) after approval: {result['content']}")
assert len(CHARGED) == 1

# 3) The provider_key the handler saw is stable — the defense you pass to
#    Stripe so even a crash-then-retry cannot double-refund:
result = execute_tool_use("conv-123", call)  # duplicate -> replayed
assert len(CHARGED) == 1
print(f"3) duplicate replayed; provider_key was {CHARGED[0]['key']}")
