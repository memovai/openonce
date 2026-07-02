"""OpenOnce client: the developer-facing surface.

    import openonce

    oo = openonce.OpenOnce("openonce.db")

    @oo.effect(tool="github.create_pr", idempotency_fields=["owner", "repo", "title"])
    def create_pr(owner: str, repo: str, title: str, body: str) -> dict: ...

    with oo.scope("run-2026-07-02-a"):
        create_pr(owner="acme", repo="api", title="Fix login", body="...")
        create_pr(owner="acme", repo="api", title="Fix login", body="reworded")  # replayed

Scopes are mandatory for derived keys: deduplicating on (tool, args) across all
runs forever silently drops intended effects (two runs may legitimately want
the same call). An explicit idempotency_key opts out of scoping.
"""

from __future__ import annotations

import contextvars
import functools
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any, TypeVar

from .errors import ScopeRequiredError
from .policy import Policy, allow_all
from .records import EffectRecord, JournalEntry
from .runtime import Runtime, current_effect, is_async_callable
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

        def decorator(fn: F) -> F:
            if is_async_callable(fn):
                raise TypeError(
                    "async handlers are not supported yet (coming with the async "
                    "runtime); wrap a sync callable or run via asyncio.to_thread"
                )

            @functools.wraps(fn)
            def wrapper(*posargs: Any, idempotency_key: str | None = None, **kwargs: Any) -> Any:
                if posargs:
                    raise TypeError(
                        f"{fn.__name__}: pass arguments as keywords — they are the "
                        f"idempotency key material and need stable names"
                    )
                scope = _scope.get()
                if idempotency_key is None and scope is None:
                    raise ScopeRequiredError(
                        f"{tool}: derived idempotency keys need a run scope. "
                        f'Wrap the call in `with oo.scope("run-id"):` or pass '
                        f"an explicit idempotency_key."
                    )
                return self.runtime.execute(
                    fn,
                    tool=tool,
                    args=kwargs,
                    scope=scope or "_explicit_key",
                    idempotency_key=idempotency_key,
                    idempotency_fields=idempotency_fields,
                    max_attempts=max_attempts,
                )

            return wrapper  # type: ignore[return-value]

        return decorator

    # -- approvals & review -------------------------------------------------

    def approve(self, effect_id: str, *, by: str = "human") -> EffectRecord:
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
