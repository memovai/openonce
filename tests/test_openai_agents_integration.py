"""OpenAI Agents SDK integration tests against the REAL SDK tool machinery
(FunctionTool.on_invoke_tool), no LLM required.
"""

from __future__ import annotations

import asyncio
import json

import pytest

pytest.importorskip("agents")

from agents.tool_context import ToolContext
from agents.usage import Usage

from openonce import EffectState, OpenOnce, require_approval_for
from openonce.integrations.openai_agents import (
    OpenOnceRunContext,
    effect_function_tool,
)


def invoke(tool, run_context: object, args: dict) -> str:
    """Drive the tool exactly as the SDK runner would."""
    payload = json.dumps(args)
    ctx = ToolContext(
        context=run_context,
        usage=Usage(),
        tool_name=tool.name,
        tool_call_id="call_1",
        tool_arguments=payload,
    )
    return asyncio.run(tool.on_invoke_tool(ctx, payload))


def make_tool(oo: OpenOnce, calls: list[str]):
    @effect_function_tool(oo, tool="stripe.refund", idempotency_fields=["charge"])
    def refund(charge: str) -> str:
        """Refund a Stripe charge."""
        calls.append(charge)
        return f"refunded {charge}"

    return refund


class TestSchema:
    def test_schema_comes_from_handler_not_wrapper(self) -> None:
        oo = OpenOnce()
        tool = make_tool(oo, [])
        assert tool.name == "refund"
        assert tool.description == "Refund a Stripe charge."
        props = tool.params_json_schema["properties"]
        assert list(props) == ["charge"]  # ctx is injected, not exposed


class TestDedup:
    def test_same_scope_dedupes(self) -> None:
        oo = OpenOnce()
        calls: list[str] = []
        tool = make_tool(oo, calls)
        run_ctx = OpenOnceRunContext(openonce_scope="conv-1")

        r1 = invoke(tool, run_ctx, {"charge": "ch_1"})
        r2 = invoke(tool, run_ctx, {"charge": "ch_1"})
        assert r1 == r2 == "refunded ch_1"
        assert calls == ["ch_1"]

    def test_dict_context_scope_works(self) -> None:
        oo = OpenOnce()
        calls: list[str] = []
        tool = make_tool(oo, calls)

        invoke(tool, {"openonce_scope": "conv-1"}, {"charge": "ch_1"})
        invoke(tool, {"openonce_scope": "conv-2"}, {"charge": "ch_1"})
        assert calls == ["ch_1", "ch_1"]  # different conversations, two intents

    def test_missing_scope_is_a_loud_error(self) -> None:
        oo = OpenOnce()
        tool = make_tool(oo, [])
        out = invoke(tool, object(), {"charge": "ch_1"})
        # The SDK routes tool errors through its failure handler -> message string.
        assert "openonce_scope" in out or "scope" in out.lower()


class TestModelReadableSignals:
    def test_approval_flow_via_json_signal(self) -> None:
        oo = OpenOnce(policy=require_approval_for(["stripe.*"]))
        calls: list[str] = []
        tool = make_tool(oo, calls)
        run_ctx = OpenOnceRunContext(openonce_scope="conv-1")

        out = json.loads(invoke(tool, run_ctx, {"charge": "ch_1"}))
        assert out["openonce"] == "approval_required"
        assert calls == []
        assert "same arguments" in out["instructions"]

        oo.approve(out["effect_id"], by="eric")
        # The model calls again with the same args — executes exactly once.
        assert invoke(tool, run_ctx, {"charge": "ch_1"}) == "refunded ch_1"
        assert calls == ["ch_1"]
        assert oo.get(out["effect_id"]).state == EffectState.COMMITTED

    def test_denied_signal(self) -> None:
        oo = OpenOnce(policy=require_approval_for(["stripe.*"]))
        tool = make_tool(oo, [])
        run_ctx = OpenOnceRunContext(openonce_scope="conv-1")

        out = json.loads(invoke(tool, run_ctx, {"charge": "ch_1"}))
        oo.deny(out["effect_id"], reason="wrong customer")

        out2 = json.loads(invoke(tool, run_ctx, {"charge": "ch_1"}))
        assert out2["openonce"] == "denied"
        assert out2["reason"] == "wrong customer"

    def test_unknown_outcome_signal_tells_model_not_to_retry(self) -> None:
        oo = OpenOnce()
        run_ctx = OpenOnceRunContext(openonce_scope="conv-1")

        @effect_function_tool(oo, tool="pay.charge")
        def charge(amount_cents: int) -> str:
            """Charge the customer."""
            raise TimeoutError("read timed out after send")

        out = json.loads(invoke(charge, run_ctx, {"amount_cents": 500}))
        assert out["openonce"] == "outcome_unknown"
        assert "Do NOT retry" in out["instructions"]
        assert oo.get(out["effect_id"]).state == EffectState.UNKNOWN
