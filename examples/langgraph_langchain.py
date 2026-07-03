"""OpenOnce + LangGraph / LangChain — durable tools with pause-and-approve.

Run offline (no LLM needed):  uv run python examples/langgraph_langchain.py

Two integration levels:

1. LangGraph graphs  -> use `effect_tool` (the dedicated adapter).
   - dedup scope binds to the graph's thread_id automatically
   - ApprovalPending maps onto LangGraph's interrupt(): the graph PAUSES,
     a human resumes with Command(resume=...), the node replays, and the
     effect executes exactly once.

2. Plain LangChain `@tool` -> no adapter needed. An @oo.effect-wrapped
   function is a plain callable; stack the decorators.
"""

from typing import TypedDict

from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command

import openonce
from openonce.integrations.langgraph import effect_tool

oo = openonce.OpenOnce(policy=openonce.require_approval_for(["stripe.*"]))

SENT: list[str] = []  # the "external world"


# ── 1. LangGraph: durable effect + LangGraph tool in one decorator ──────────


@effect_tool(oo, tool="stripe.refund", idempotency_fields=["charge"])
def refund(charge: str) -> str:
    """Refund a Stripe charge."""
    SENT.append(charge)
    return f"refunded {charge}"


class State(TypedDict, total=False):
    charge: str
    result: str


def act(state: State) -> State:
    return {"result": refund.invoke({"charge": state["charge"]})}


builder = StateGraph(State)
builder.add_node("act", act)
builder.add_edge(START, "act")
builder.add_edge("act", END)
graph = builder.compile(checkpointer=MemorySaver())  # checkpointer required
config = {"configurable": {"thread_id": "customer-42"}}

# The refund needs approval -> the graph pauses instead of executing:
paused = graph.invoke({"charge": "ch_1"}, config)
payload = paused["__interrupt__"][0].value
print(f"1) graph paused for approval: effect={payload['effect_id']} tool={payload['tool']}")
assert SENT == []  # nothing left the building

# A human approves; LangGraph replays the node; OpenOnce approvals are
# re-entrant, so the replayed call proceeds — exactly once:
done = graph.invoke(Command(resume={"approved": True, "by": "eric"}), config)
print(f"   after approval: {done['result']!r}, external calls = {SENT}")

# Same thread re-invokes with the same intent -> replayed, not re-refunded:
again = graph.invoke({"charge": "ch_1"}, config)
assert SENT == ["ch_1"]
print(f"2) same thread, same intent: replayed ({again['result']!r}), still one refund")


# ── 2. Plain LangChain @tool: just stack the decorators ─────────────────────

from langchain_core.tools import tool  # noqa: E402


@tool
@oo.effect(idempotency_fields=["to", "subject"])
def send_email(to: str, subject: str, body: str) -> str:
    """Send an email."""
    SENT.append(to)
    return f"sent to {to}"


with oo.scope("run-1"):
    a = send_email.invoke({"to": "a@b.c", "subject": "hi", "body": "v1"})
    b = send_email.invoke({"to": "a@b.c", "subject": "hi", "body": "v2 reworded"})
assert a == b and SENT.count("a@b.c") == 1
print("3) plain @tool + @oo.effect stacked: LLM rewrote the body, one send")
