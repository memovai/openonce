"""OpenAI Agents SDK integration: durable effects as function tools.

Design: OpenOnce's control-flow signals are translated into **structured JSON
tool outputs the model can read and act on**, instead of opaque exceptions:

- ``ApprovalPending`` -> ``{"openonce": "approval_required", "effect_id": ...}``
  The model tells the user; once a human approves (CLI, dashboard, ...), the
  model simply calls the tool again with the same arguments — re-entrant by
  design, the same call proceeds exactly once.
- ``EffectUnknown``   -> ``{"openonce": "outcome_unknown", ...}`` with an
  explicit "do not retry" instruction: the reconciler owns resolution.
- ``EffectDenied``    -> ``{"openonce": "denied", ...}``.

Genuine business failures still raise, flowing through the SDK's normal
tool-failure handling (and replaying consistently for duplicate calls).

Scope resolution order:
1. an explicit ambient ``with oo.scope(...):``
2. ``openonce_scope`` on the run context object (attr or dict key) — pass
   ``context=OpenOnceRunContext(openonce_scope="conv-123")`` (or your own
   object carrying that field) to ``Runner.run``.
"""

from __future__ import annotations

import inspect
import json
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, TypeVar

from ..client import OpenOnce, current_scope
from ..errors import ApprovalPending, EffectDenied, EffectUnknown, ScopeRequiredError

if TYPE_CHECKING:  # pragma: no cover
    from agents import FunctionTool

F = TypeVar("F", bound=Callable[..., Any])


@dataclass
class OpenOnceRunContext:
    """Minimal run context carrying the dedup scope. Use it directly or copy
    the ``openonce_scope`` field onto your own context class."""

    openonce_scope: str


def _require_agents() -> tuple[Any, Any]:
    try:
        from agents import RunContextWrapper, function_tool
    except ImportError as exc:  # pragma: no cover
        raise ImportError(
            "openonce.integrations.openai_agents requires the OpenAI Agents SDK: "
            "pip install openai-agents"
        ) from exc
    return function_tool, RunContextWrapper


def _resolve_scope(ctx: Any, tool: str) -> str:
    ambient = current_scope()
    if ambient is not None:
        return ambient
    obj = getattr(ctx, "context", None)
    if isinstance(obj, dict):
        scope = obj.get("openonce_scope")
    else:
        scope = getattr(obj, "openonce_scope", None)
    if scope:
        return str(scope)
    raise ScopeRequiredError(
        f"{tool}: no dedup scope. Pass context=OpenOnceRunContext(openonce_scope=...) "
        f"to Runner.run, put an 'openonce_scope' field on your context object, or "
        f"wrap the run in `with oo.scope(...):`."
    )


def _signal(kind: str, effect_id: str, tool: str, **extra: Any) -> str:
    return json.dumps({"openonce": kind, "effect_id": effect_id, "tool": tool, **extra})


def effect_function_tool(
    oo: OpenOnce,
    *,
    tool: str | None = None,
    idempotency_fields: list[str] | None = None,
    max_attempts: int = 3,
    dedup: str = "intent",
    **function_tool_kwargs: Any,
) -> Callable[[F], FunctionTool]:
    """Wrap a handler as a durable effect AND an Agents SDK ``FunctionTool``.

    ``dedup`` picks the intent boundary:

    - ``"intent"`` (default): identical whitelisted args within one scope are
      ONE effect. Required for approval flows — "call again with the same
      arguments after approval" must land on the same key.
    - ``"call"``: the LLM's ``tool_call_id`` narrows the scope, so every
      distinct model decision is its own effect — retries of the *same* tool
      call (SDK retry, session resume replaying the call) still dedupe, but
      two independent decisions with identical args both execute. Use for
      tools where repetition is legitimate ("add item to cart").
      CAVEAT: incompatible with require-approval policies — the model's
      post-approval retry carries a NEW call_id and would park a fresh
      approval forever.

    Extra keyword arguments are forwarded to ``function_tool`` (e.g.
    ``strict_mode``, ``is_enabled``).
    """
    if dedup not in ("intent", "call"):
        raise ValueError(f"dedup must be 'intent' or 'call', got {dedup!r}")
    function_tool, run_context_wrapper = _require_agents()

    def decorator(fn: F) -> FunctionTool:
        tool_name = tool or fn.__name__
        durable = oo.effect(
            tool=tool_name, idempotency_fields=idempotency_fields, max_attempts=max_attempts
        )(fn)

        fn_param_names = list(inspect.signature(fn).parameters)

        def runner(ctx: Any, *args: Any, **kwargs: Any) -> Any:
            # The SDK passes parsed arguments positionally per our declared
            # signature; rebind them to names — they are the key material.
            bound = dict(zip(fn_param_names, args, strict=False)) | kwargs
            scope = _resolve_scope(ctx, tool_name)
            if dedup == "call":
                call_id = getattr(ctx, "tool_call_id", None)
                if call_id:
                    # Narrowing the SCOPE (not hashing call_id into args)
                    # keeps the decision boundary visible in record.scope
                    # for audit, with zero schema change.
                    scope = f"{scope}/call:{call_id}"
            with oo.scope(scope):
                try:
                    return durable(**bound)
                except ApprovalPending as pending:
                    return _signal(
                        "approval_required",
                        pending.effect_id,
                        tool_name,
                        instructions=(
                            "This action is parked until a human approves it. Tell the "
                            "user, and after approval call this tool again with exactly "
                            "the same arguments — it will then execute exactly once."
                        ),
                    )
                except EffectUnknown as unknown:
                    return _signal(
                        "outcome_unknown",
                        unknown.record.effect_id,
                        tool_name,
                        instructions=(
                            "The outcome of this action is ambiguous (it may have "
                            "happened). Do NOT retry or work around it; a reconciler "
                            "will resolve it against the provider. Inform the user."
                        ),
                    )
                except EffectDenied as denied:
                    return _signal(
                        "denied",
                        denied.record.effect_id,
                        tool_name,
                        reason=denied.record.note or "",
                        instructions="A human denied this action. Do not attempt it again.",
                    )

        # The SDK builds the tool schema from the function signature; expose
        # fn's parameters with a leading RunContextWrapper (excluded from the
        # schema, injected at invoke time).
        fn_sig = inspect.signature(fn)
        ctx_param = inspect.Parameter(
            "ctx",
            kind=inspect.Parameter.POSITIONAL_OR_KEYWORD,
            annotation=run_context_wrapper[Any],
        )
        runner.__signature__ = fn_sig.replace(  # type: ignore[attr-defined]
            parameters=[ctx_param, *fn_sig.parameters.values()]
        )
        runner.__name__ = fn.__name__
        runner.__doc__ = fn.__doc__
        runner.__annotations__ = {
            "ctx": run_context_wrapper[Any],
            **getattr(fn, "__annotations__", {}),
        }

        result: FunctionTool = function_tool(runner, **function_tool_kwargs)
        return result

    return decorator
