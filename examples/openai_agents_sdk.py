"""OpenOnce + OpenAI Agents SDK — durable function tools with model-readable signals.

Run offline (no API key needed):  uv run python examples/openai_agents_sdk.py

`effect_function_tool` wraps a handler as a durable effect AND an Agents SDK
FunctionTool. Control flow becomes structured JSON the model reads and acts on:

- approval_required -> the model tells the user, and after a human approves it
  calls the tool again with the same arguments (re-entrant: executes once).
- outcome_unknown   -> the model is instructed NOT to retry; the reconciler
  resolves it against the provider.
"""

import asyncio
import json

from agents.tool_context import ToolContext
from agents.usage import Usage

import openonce
from openonce.integrations.openai_agents import OpenOnceRunContext, effect_function_tool

oo = openonce.OpenOnce(policy=openonce.require_approval_for(["stripe.*"]))
CHARGED: list[str] = []  # the "external world"


@effect_function_tool(oo, tool="stripe.refund", idempotency_fields=["charge"])
def refund(charge: str) -> str:
    """Refund a Stripe charge."""
    CHARGED.append(charge)
    return f"refunded {charge}"


# ── Real usage (needs OPENAI_API_KEY): hand the tool to an Agent ─────────────
#
#   from agents import Agent, Runner
#
#   agent = Agent(name="support", instructions="Help with refunds.", tools=[refund])
#   result = Runner.run_sync(
#       agent,
#       "refund charge ch_1",
#       context=OpenOnceRunContext(openonce_scope="conv-123"),  # dedup scope
#   )
#
# ── Offline demo: drive the tool exactly as the SDK runner would ─────────────


def invoke(tool, run_context, args: dict) -> str:
    payload = json.dumps(args)
    ctx = ToolContext(
        context=run_context,
        usage=Usage(),
        tool_name=tool.name,
        tool_call_id="call_1",
        tool_arguments=payload,
    )
    return asyncio.run(tool.on_invoke_tool(ctx, payload))


run_ctx = OpenOnceRunContext(openonce_scope="conv-123")

# 1) The refund needs approval — the model receives a JSON signal, not an error:
out = json.loads(invoke(refund, run_ctx, {"charge": "ch_1"}))
print(f"1) tool output to the model: {out['openonce']} (effect={out['effect_id']})")
print(f"   instructions the model reads: {out['instructions'][:60]}...")
assert CHARGED == []

# 2) A human approves (CLI, dashboard, oo.approve). The model calls again
#    with the same arguments — same key, executes exactly once:
oo.approve(out["effect_id"], by="eric")
print(f"2) after approval, same call: {invoke(refund, run_ctx, {'charge': 'ch_1'})!r}")
assert CHARGED == ["ch_1"]

# 3) Any further duplicate replays the receipt:
invoke(refund, run_ctx, {"charge": "ch_1"})
assert CHARGED == ["ch_1"]
print(f"3) duplicate replayed; external calls = {CHARGED}")
