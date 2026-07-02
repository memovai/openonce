"""LangGraph integration: durable effects as LangGraph tools.

Why this exists: LangGraph's checkpointing is state-snapshot durability — on
resume it re-runs the node, re-executing external side effects, and its own
docs tell you to "use idempotency keys or verify existing results". This
module is that missing layer, packaged as one decorator.

The composition is exact, not approximate:

- **Scope**: derived from the LangGraph ``thread_id``, so retries and resumes
  within one thread dedupe, while different threads legitimately re-execute.
  An explicit ``oo.scope(...)`` wrapper still wins if you set one.
- **Approvals**: :class:`~openonce.ApprovalPending` maps onto LangGraph's
  ``interrupt()``. When a human resumes the graph (``Command(resume=...)``),
  LangGraph re-runs the node from the top — and because OpenOnce approvals
  are re-entrant (same call, same key, proceeds once approved), the replayed
  tool call is exactly the right thing. The two replay semantics interlock.

Usage::

    from openonce.integrations.langgraph import effect_tool

    @effect_tool(oo, tool="stripe.refund", idempotency_fields=["charge"])
    def refund(charge: str) -> str:
        \"\"\"Refund a charge.\"\"\"
        ...

    graph = builder.compile(checkpointer=checkpointer)   # checkpointer required
    result = graph.invoke(inputs, {"configurable": {"thread_id": "t-1"}})
    # on ApprovalPending the graph pauses; a human resumes with:
    #   graph.invoke(Command(resume={"approved": True, "by": "eric"}), config)
"""

from __future__ import annotations

import functools
from collections.abc import Callable
from typing import TYPE_CHECKING, Any, TypeVar

from ..client import OpenOnce, current_scope
from ..errors import ApprovalPending, EffectDenied, ScopeRequiredError

if TYPE_CHECKING:  # pragma: no cover
    from langchain_core.tools import BaseTool

F = TypeVar("F", bound=Callable[..., Any])


def _require_langgraph() -> tuple[Any, Any, Any]:
    try:
        from langchain_core.tools import tool as lc_tool
        from langgraph.config import get_config
        from langgraph.types import interrupt
    except ImportError as exc:  # pragma: no cover
        raise ImportError(
            "openonce.integrations.langgraph requires langgraph: pip install langgraph"
        ) from exc
    return lc_tool, get_config, interrupt


def _thread_scope(get_config: Any) -> str:
    """Derive the dedup scope from the LangGraph thread, unless the caller
    already set one explicitly."""
    ambient = current_scope()
    if ambient is not None:
        return ambient
    try:
        cfg = get_config()
    except Exception as exc:
        raise ScopeRequiredError(
            "effect_tool must run inside a LangGraph graph (get_config() failed) "
            "or under an explicit `with oo.scope(...):`."
        ) from exc
    thread_id = (cfg.get("configurable") or {}).get("thread_id")
    if not thread_id:
        raise ScopeRequiredError(
            "effect_tool needs a thread_id (compile the graph with a checkpointer "
            "and pass {'configurable': {'thread_id': ...}}) or an explicit oo.scope."
        )
    return f"lg:{thread_id}"


def effect_tool(
    oo: OpenOnce,
    *,
    tool: str,
    idempotency_fields: list[str] | None = None,
    max_attempts: int = 3,
    approval_via_interrupt: bool = True,
) -> Callable[[F], BaseTool]:
    """Wrap a handler as a durable effect AND a LangGraph tool in one step.

    Returns a ``BaseTool`` ready for ``ToolNode`` / ``create_react_agent``.
    The handler needs type hints and a docstring (LangChain builds the tool
    schema from them).
    """
    lc_tool, get_config, interrupt = _require_langgraph()

    def decorator(fn: F) -> BaseTool:
        durable = oo.effect(
            tool=tool, idempotency_fields=idempotency_fields, max_attempts=max_attempts
        )(fn)

        @functools.wraps(fn)
        def runner(**kwargs: Any) -> Any:
            with oo.scope(_thread_scope(get_config)):
                try:
                    return durable(**kwargs)
                except ApprovalPending as pending:
                    if not approval_via_interrupt:
                        raise
                    # Pause the graph. On resume LangGraph re-runs this node:
                    # durable(...) raises ApprovalPending again (still parked),
                    # and interrupt() now RETURNS the human's decision.
                    decision = interrupt(
                        {
                            "type": "openonce_approval",
                            "effect_id": pending.effect_id,
                            "tool": tool,
                            "args": kwargs,
                            "note": pending.record.note,
                        }
                    )
                    if _approved(decision):
                        oo.approve(pending.effect_id, by=_decided_by(decision))
                        return durable(**kwargs)  # same key -> proceeds once
                    oo.deny(
                        pending.effect_id,
                        by=_decided_by(decision),
                        reason=_reason(decision),
                    )
                    raise EffectDenied(oo.get(pending.effect_id)) from None

        wrapped: BaseTool = lc_tool(runner)
        return wrapped

    return decorator


def _approved(decision: Any) -> bool:
    if isinstance(decision, bool):
        return decision
    if isinstance(decision, dict):
        return bool(decision.get("approved"))
    return False  # unrecognized resume payloads deny by default: safe side


def _decided_by(decision: Any) -> str:
    if isinstance(decision, dict):
        return str(decision.get("by", "langgraph"))
    return "langgraph"


def _reason(decision: Any) -> str:
    if isinstance(decision, dict):
        return str(decision.get("reason", ""))
    return ""
