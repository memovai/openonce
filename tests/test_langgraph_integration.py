"""LangGraph integration tests against the REAL langgraph runtime:
graph pause on approval, human resume, node-replay dedup, thread scoping.
"""

from __future__ import annotations

from typing import TypedDict

import pytest

pytest.importorskip("langgraph")

from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command

from openonce import EffectDenied, EffectState, OpenOnce, require_approval_for
from openonce.integrations.langgraph import effect_tool


class State(TypedDict, total=False):
    charge: str
    result: str


def build_graph(oo: OpenOnce, calls: list[str]):
    """One-node graph whose node invokes a durable refund tool."""

    @effect_tool(oo, tool="stripe.refund", idempotency_fields=["charge"])
    def refund(charge: str) -> str:
        """Refund a Stripe charge."""
        calls.append(charge)
        return f"refunded {charge}"

    def act(state: State) -> State:
        out = refund.invoke({"charge": state["charge"]})
        return {"result": out}

    builder = StateGraph(State)
    builder.add_node("act", act)
    builder.add_edge(START, "act")
    builder.add_edge("act", END)
    return builder.compile(checkpointer=MemorySaver())


class TestApprovalInterrupt:
    def test_pause_approve_resume_executes_once(self) -> None:
        oo = OpenOnce(policy=require_approval_for(["stripe.*"]))
        calls: list[str] = []
        graph = build_graph(oo, calls)
        cfg = {"configurable": {"thread_id": "t1"}}

        # 1) The graph pauses instead of refunding.
        paused = graph.invoke({"charge": "ch_1"}, cfg)
        assert "__interrupt__" in paused
        payload = paused["__interrupt__"][0].value
        assert payload["type"] == "openonce_approval"
        assert payload["tool"] == "stripe.refund"
        assert calls == []

        # 2) A human approves; the node replays; the effect executes once.
        done = graph.invoke(Command(resume={"approved": True, "by": "eric"}), cfg)
        assert done["result"] == "refunded ch_1"
        assert calls == ["ch_1"]

        # 3) The ledger agrees.
        rec = oo.get(payload["effect_id"])
        assert rec.state == EffectState.COMMITTED

    def test_pause_deny_resume_raises(self) -> None:
        oo = OpenOnce(policy=require_approval_for(["stripe.*"]))
        calls: list[str] = []
        graph = build_graph(oo, calls)
        cfg = {"configurable": {"thread_id": "t1"}}

        paused = graph.invoke({"charge": "ch_1"}, cfg)
        eid = paused["__interrupt__"][0].value["effect_id"]

        with pytest.raises(EffectDenied):
            graph.invoke(Command(resume={"approved": False, "reason": "wrong customer"}), cfg)
        assert calls == []
        assert oo.get(eid).state == EffectState.DENIED

    def test_unrecognized_resume_payload_denies_by_default(self) -> None:
        oo = OpenOnce(policy=require_approval_for(["stripe.*"]))
        graph = build_graph(oo, [])
        cfg = {"configurable": {"thread_id": "t1"}}
        graph.invoke({"charge": "ch_1"}, cfg)
        with pytest.raises(EffectDenied):
            graph.invoke(Command(resume="lgtm???"), cfg)


class TestThreadScoping:
    def test_same_thread_dedupes_across_invocations(self) -> None:
        oo = OpenOnce()  # allow_all: no approval in the way
        calls: list[str] = []
        graph = build_graph(oo, calls)
        cfg = {"configurable": {"thread_id": "t1"}}

        r1 = graph.invoke({"charge": "ch_9"}, cfg)
        r2 = graph.invoke({"charge": "ch_9"}, cfg)  # same thread, same intent
        assert r1["result"] == r2["result"] == "refunded ch_9"
        assert calls == ["ch_9"]  # replayed, not re-refunded

    def test_different_threads_reexecute(self) -> None:
        oo = OpenOnce()
        calls: list[str] = []
        graph = build_graph(oo, calls)

        graph.invoke({"charge": "ch_9"}, {"configurable": {"thread_id": "t1"}})
        graph.invoke({"charge": "ch_9"}, {"configurable": {"thread_id": "t2"}})
        assert calls == ["ch_9", "ch_9"]  # two threads = two intents

    def test_explicit_scope_wins_over_thread(self) -> None:
        oo = OpenOnce()
        calls: list[str] = []
        graph = build_graph(oo, calls)

        with oo.scope("pinned"):
            graph.invoke({"charge": "ch_9"}, {"configurable": {"thread_id": "t1"}})
            graph.invoke({"charge": "ch_9"}, {"configurable": {"thread_id": "t2"}})
        assert calls == ["ch_9"]  # one scope = one intent, thread ids ignored
