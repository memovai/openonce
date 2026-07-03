"""OpenOnce client: the developer-facing surface.

    import openonce

    oo = openonce.OpenOnce("openonce.db")

    @oo.effect(tool="github.create_pr", idempotency_fields=["owner", "repo", "head"])
    def create_pr(owner: str, repo: str, head: str, title: str, body: str) -> dict: ...

    with oo.scope("run-2026-07-02-a"):
        create_pr(owner="acme", repo="api", head="fix-login", title="Fix login", body="...")
        create_pr(
            owner="acme", repo="api", head="fix-login", title="Fix login", body="reworded"
        )  # replayed

Scopes are mandatory for derived keys: deduplicating on (tool, args) across all
runs forever silently drops intended effects (two runs may legitimately want
the same call). An explicit idempotency_key opts out of scoping.
"""

from __future__ import annotations

import asyncio
import contextvars
import functools
import json
import time
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from typing import Any, TypeVar

from .errors import ScopeRequiredError
from .policy import Policy, allow_all
from .providers.capabilities import (
    ProviderCapability,
    can_auto_rearm_on_miss,
    capabilities_for_tool,
    capability_guidance_for_tool,
    provider_receipt_contract_failures,
)
from .records import EffectRecord, EffectResult, JournalEntry
from .runtime import (
    Runtime,
    _pop_effect_context,
    _push_effect_context,
    current_effect,
    is_async_callable,
)
from .state import EffectState as S
from .store.base import Store
from .store.sqlite import SQLiteStore

__all__ = ["OpenOnce", "current_effect", "current_scope"]

F = TypeVar("F", bound=Callable[..., Any])

_scope: contextvars.ContextVar[str | None] = contextvars.ContextVar("openonce_scope", default=None)


def current_scope() -> str | None:
    """The ambient run scope, if one is set (integrations use this to let an
    explicit ``oo.scope(...)`` override their own scope derivation)."""
    return _scope.get()


class OpenOnce:
    def __init__(
        self,
        store: Store | str = ":memory:",
        *,
        policy: Policy = allow_all,
        lease_seconds: float = 60.0,
        wait_timeout: float = 30.0,
        extra_capabilities: tuple[ProviderCapability, ...] | None = None,
        provider_capabilities: dict[str, str] | None = None,
        enforce_provider_receipts: bool = False,
        require_provider_capability_for_receipts: bool = False,
        clock: Callable[[], float] = time.time,
    ) -> None:
        if isinstance(store, str):
            store = SQLiteStore(store, clock=clock)
        self.store: Store = store
        self.runtime = Runtime(
            store,
            policy=policy,
            lease_seconds=lease_seconds,
            wait_timeout=wait_timeout,
            extra_capabilities=extra_capabilities,
            provider_capabilities=provider_capabilities,
            enforce_provider_receipts=enforce_provider_receipts,
            require_provider_capability_for_receipts=require_provider_capability_for_receipts,
            clock=clock,
        )

    # -- scoping ----------------------------------------------------------

    @contextmanager
    def scope(self, run_id: str) -> Iterator[None]:
        """Bind derived idempotency keys to this run. Nesting replaces."""
        token = _scope.set(run_id)
        try:
            yield
        finally:
            _scope.reset(token)

    # -- the decorator ------------------------------------------------------

    def effect(
        self,
        *,
        tool: str,
        idempotency_fields: list[str] | None = None,
        max_attempts: int = 3,
    ) -> Callable[[F], F]:
        """Wrap a tool handler as a durable effect.

        The wrapped function accepts an extra keyword-only ``idempotency_key``
        to pass an explicit key (which then doesn't require a scope).
        Handler args must be passed as keywords — they are the key material.
        """

        def check_and_resolve_scope(
            fn_name: str, posargs: tuple[Any, ...], idempotency_key: str | None
        ) -> str:
            if posargs:
                raise TypeError(
                    f"{fn_name}: pass arguments as keywords — they are the "
                    f"idempotency key material and need stable names"
                )
            scope = _scope.get()
            if idempotency_key is None and scope is None:
                raise ScopeRequiredError(
                    f"{tool}: derived idempotency keys need a run scope. "
                    f'Wrap the call in `with oo.scope("run-id"):` or pass '
                    f"an explicit idempotency_key."
                )
            return scope or "_explicit_key"

        def decorator(fn: F) -> F:
            if is_async_callable(fn):

                @functools.wraps(fn)
                async def async_wrapper(
                    *posargs: Any, idempotency_key: str | None = None, **kwargs: Any
                ) -> Any:
                    scope = check_and_resolve_scope(fn.__name__, posargs, idempotency_key)
                    loop = asyncio.get_running_loop()

                    def shim(**kw: Any) -> Any:
                        # Runs in the worker thread at the exact point the engine
                        # invokes the handler; ship the coroutine back to the
                        # caller's loop, carrying the effect context along.
                        ectx = current_effect()

                        async def with_ctx() -> Any:
                            token = _push_effect_context(ectx)
                            try:
                                return await fn(**kw)
                            finally:
                                _pop_effect_context(token)

                        return asyncio.run_coroutine_threadsafe(with_ctx(), loop).result()

                    # The ledger machinery (short DB transactions + possible
                    # wait-for-duplicate polling) blocks a to_thread worker,
                    # never the event loop.
                    return await asyncio.to_thread(
                        self.runtime.execute,
                        shim,
                        tool=tool,
                        args=kwargs,
                        scope=scope,
                        idempotency_key=idempotency_key,
                        idempotency_fields=idempotency_fields,
                        max_attempts=max_attempts,
                    )

                return async_wrapper  # type: ignore[return-value]

            @functools.wraps(fn)
            def wrapper(*posargs: Any, idempotency_key: str | None = None, **kwargs: Any) -> Any:
                scope = check_and_resolve_scope(fn.__name__, posargs, idempotency_key)
                return self.runtime.execute(
                    fn,
                    tool=tool,
                    args=kwargs,
                    scope=scope,
                    idempotency_key=idempotency_key,
                    idempotency_fields=idempotency_fields,
                    max_attempts=max_attempts,
                )

            return wrapper  # type: ignore[return-value]

        return decorator

    # -- approvals & review -------------------------------------------------

    def approve(self, effect_id: str, *, by: str = "human") -> EffectRecord:
        """Approve a parked effect: REQUIRES_APPROVAL or HUMAN_REVIEW → APPROVED.

        HUMAN_REVIEW stays approvable — "a human re-arms a reviewed effect" is
        the generic resolution path; resolve_happened/resolve_not_happened are
        the richer, evidence-carrying alternatives, not replacements.
        """
        rec = self.store.transition(
            effect_id,
            {S.REQUIRES_APPROVAL, S.HUMAN_REVIEW},
            S.APPROVED,
            payload={"approved_by": by},
        )
        if rec is None:
            raise ValueError(f"effect {effect_id} is not awaiting approval/review")
        return rec

    def deny(self, effect_id: str, *, by: str = "human", reason: str = "") -> EffectRecord:
        rec = self.store.transition(
            effect_id,
            {S.REQUIRES_APPROVAL},
            S.DENIED,
            payload={"denied_by": by, "reason": reason},
            set_fields={"note": reason or None},
        )
        if rec is None:
            raise ValueError(f"effect {effect_id} is not awaiting approval")
        return rec

    def resolve_happened(
        self,
        effect_id: str,
        *,
        receipt: Mapping[str, Any],
        by: str = "human",
        reason: str = "",
        require_provider_capability: bool | None = None,
    ) -> EffectRecord:
        """Human resolution: the external effect happened; commit a receipt.

        This is intentionally separate from ``approve()``. Approval means
        "allow the next identical call to execute"; resolving as happened means
        "do not execute again; commit the external evidence I reviewed."
        """
        value = _canonical_manual_receipt(receipt)
        rec = self.store.get(effect_id)
        if rec is None:
            raise ValueError(f"effect {effect_id} does not exist")
        if require_provider_capability is None:
            require_provider_capability = self.runtime.require_provider_capability_for_receipts
        contract_failures = self._manual_receipt_contract_failures(
            rec,
            value,
            require_provider_capability=require_provider_capability,
        )
        if contract_failures:
            raise ValueError(
                f"manual receipt for {rec.tool!r} does not satisfy provider "
                f"contract: {'; '.join(contract_failures)}"
            )

        payload: dict[str, object] = {
            "resolution": "happened",
            "resolved_by": by,
            "receipt": value,
        }
        if reason:
            payload["reason"] = reason
        set_fields: dict[str, object] = {"result": EffectResult(ok=True, value=value)}
        if reason:
            set_fields["note"] = reason

        if rec.state is S.UNKNOWN:
            receipted = self.store.transition(
                effect_id,
                {S.UNKNOWN},
                S.RECEIPT_RECORDED,
                set_fields=set_fields,
                payload=payload,
            )
            if receipted is None:
                raise ValueError(f"effect {effect_id} is not awaiting outcome review")
            committed = self.store.transition(
                receipted.effect_id,
                {S.RECEIPT_RECORDED},
                S.COMMITTED,
                payload={"resolution": "human_happened_committed", "resolved_by": by},
            )
            if committed is None:
                raise ValueError(f"effect {effect_id} could not be committed")
            return committed

        if rec.state in (S.HUMAN_REVIEW, S.RECEIPT_RECORDED):
            committed = self.store.transition(
                effect_id,
                {rec.state},
                S.COMMITTED,
                set_fields=set_fields,
                payload=payload,
            )
            if committed is None:
                raise ValueError(f"effect {effect_id} is not awaiting outcome review")
            return committed

        raise ValueError(f"effect {effect_id} is not awaiting outcome review")

    def resolve_not_happened(
        self,
        effect_id: str,
        *,
        by: str = "human",
        reason: str = "",
        require_auto_rearm: bool = False,
        require_provider_capability: bool = False,
    ) -> EffectRecord:
        """Human resolution: the external effect did not happen; allow retry.

        This is the explicit retry path for ``UNKNOWN``/``HUMAN_REVIEW``. It is
        separate from ``approve()``, which only grants pre-execution approval.
        """
        rec = self.store.get(effect_id)
        if rec is None:
            raise ValueError(f"effect {effect_id} does not exist")
        if rec.state not in (S.UNKNOWN, S.HUMAN_REVIEW):
            raise ValueError(f"effect {effect_id} is not awaiting outcome review")
        reason = reason.strip()
        if not reason:
            raise ValueError("reason is required when resolving an effect as not happened")
        blocker = self._manual_not_happened_blocker(
            rec,
            require_auto_rearm=require_auto_rearm,
            require_provider_capability=require_provider_capability,
        )
        if blocker is not None:
            raise ValueError(blocker)

        payload: dict[str, object] = {
            "resolution": "not_happened",
            "resolved_by": by,
            "reason": reason,
        }
        if rec.attempt >= rec.max_attempts:
            failed = self.store.transition(
                effect_id,
                {rec.state},
                S.FAILED,
                set_fields={
                    "result": EffectResult(
                        ok=False,
                        error="did not happen; attempts exhausted",
                        error_type="AttemptsExhausted",
                    ),
                    "note": reason,
                },
                payload=payload,
            )
            if failed is None:
                raise ValueError(f"effect {effect_id} is not awaiting outcome review")
            return failed

        rearmed = self.store.transition(
            effect_id,
            {rec.state},
            S.APPROVED,
            set_fields={"result": None, "note": reason},
            payload=payload,
        )
        if rearmed is None:
            raise ValueError(f"effect {effect_id} is not awaiting outcome review")
        return rearmed

    def _manual_not_happened_blocker(
        self,
        rec: EffectRecord,
        *,
        require_auto_rearm: bool,
        require_provider_capability: bool,
    ) -> str | None:
        if not require_auto_rearm and not require_provider_capability:
            return None
        capability = self.runtime.provider_capabilities.get(rec.tool)
        matches = capabilities_for_tool(
            rec.tool,
            capability,
            capabilities=self.runtime.capabilities,
        )
        if (require_provider_capability or require_auto_rearm) and not matches:
            if capability is not None:
                return f"no provider capability {capability!r} matches {rec.tool!r}"
            return f"no provider capability matches {rec.tool!r}"
        if require_auto_rearm and not can_auto_rearm_on_miss(
            rec.tool,
            capability,
            capabilities=self.runtime.capabilities,
        ):
            guidance = capability_guidance_for_tool(
                rec.tool,
                capability,
                capabilities=self.runtime.capabilities,
            )
            return f"{rec.tool!r} is not safe to auto-rearm on a miss. {guidance}"
        return None

    def _manual_receipt_contract_failures(
        self,
        rec: EffectRecord,
        receipt: Mapping[str, object],
        *,
        require_provider_capability: bool,
    ) -> tuple[str, ...]:
        capability = self.runtime.provider_capabilities.get(rec.tool)
        matches = capabilities_for_tool(
            rec.tool,
            capability,
            capabilities=self.runtime.capabilities,
        )
        if require_provider_capability and not matches:
            if capability is not None:
                return (f"no provider capability {capability!r} matches {rec.tool!r}",)
            return (f"no provider capability matches {rec.tool!r}",)
        if not self.runtime.enforce_provider_receipts:
            return ()
        return provider_receipt_contract_failures(
            rec.tool,
            rec.args(),
            receipt,
            capability,
            capabilities=self.runtime.capabilities,
        )

    # -- introspection (the receipts) ----------------------------------------

    def get(self, effect_id: str) -> EffectRecord | None:
        return self.store.get(effect_id)

    def journal(self, effect_id: str) -> list[JournalEntry]:
        return self.store.journal(effect_id)

    def pending_review(self) -> list[EffectRecord]:
        """Everything a human needs to look at: approvals + unresolved outcomes."""
        now = self.runtime.clock()
        return self.store.scan_states(
            {S.REQUIRES_APPROVAL, S.HUMAN_REVIEW, S.UNKNOWN}, updated_before=now + 1
        )


def _canonical_manual_receipt(receipt: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(receipt, Mapping) or not receipt:
        raise ValueError("receipt must be a non-empty object")
    bad_keys = [key for key in receipt if not isinstance(key, str) or not key]
    if bad_keys:
        raise ValueError("receipt keys must be non-empty strings")
    try:
        value = json.loads(json.dumps(dict(receipt)))
    except (TypeError, ValueError) as exc:
        raise ValueError("receipt must be JSON-serializable") from exc
    if not isinstance(value, dict) or not value:
        raise ValueError("receipt must be a non-empty object")
    return value
