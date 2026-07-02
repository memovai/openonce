"""Execution engine: atomic phases around one external side effect.

The phase boundaries (each a committed ledger transition) are the crash
contract — borrowed from Stripe-style "atomic phases" (rocket-rides-atomic):

    create_or_get (PLANNED)          <- first-writer-wins on the UNIQUE key
    policy -> APPROVED / REQUIRES_APPROVAL / DENIED
    APPROVED -> STARTED (+lease)     <- recovery point: "we are about to call out"
    ... the one non-atomic thing: the external call ...
    STARTED -> RECEIPT_RECORDED      <- recovery point: "we know what happened"
    RECEIPT_RECORDED -> COMMITTED

Crash between STARTED and RECEIPT_RECORDED == UNKNOWN outcome. The runtime
never resolves that itself; the reconciler probes the external world.

Duplicate callers never execute: they replay the cached result (successes AND
failures — a 400 stays a 400), or wait on an in-flight execution.
"""

from __future__ import annotations

import contextvars
import inspect
import json
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from .errors import (
    ApprovalPending,
    EffectDenied,
    EffectInFlight,
    EffectUnknown,
    IdempotencyMismatch,
    ReplayedFailure,
    RetryableEffectError,
    UnknownOutcomeError,
)
from .keys import derive_key, fingerprint, select_fields
from .policy import Decision, Policy, Verdict, allow_all
from .records import EffectRecord, EffectResult, new_effect_id
from .state import REPLAYABLE
from .state import EffectState as S
from .store.base import Store

#: Exception types treated as outcome-ambiguous by default: the request may
#: have reached the provider. ConnectionRefusedError is NOT here — refused
#: means nothing was sent, which is retryable, not unknown.
DEFAULT_UNKNOWN_EXCEPTIONS: tuple[type[BaseException], ...] = (
    TimeoutError,
    ConnectionResetError,
    BrokenPipeError,
)


@dataclass(frozen=True)
class EffectContext:
    """Ambient context available inside a handler via ``current_effect()``."""

    effect_id: str
    idempotency_key: str
    #: Pass this to the provider (e.g. Stripe's Idempotency-Key header).
    #: It is the only hard defense against duplicates once a request has
    #: left the process.
    provider_key: str
    attempt: int


_current: contextvars.ContextVar[EffectContext | None] = contextvars.ContextVar(
    "openonce_current_effect", default=None
)


def current_effect() -> EffectContext | None:
    """The effect being executed on this (logical) thread, if any."""
    return _current.get()


class Runtime:
    def __init__(
        self,
        store: Store,
        *,
        policy: Policy = allow_all,
        lease_seconds: float = 60.0,
        wait_timeout: float = 30.0,
        wait_poll_interval: float = 0.05,
        unknown_exceptions: tuple[type[BaseException], ...] = DEFAULT_UNKNOWN_EXCEPTIONS,
        worker_id: str | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.store = store
        self.policy = policy
        self.lease_seconds = lease_seconds
        self.wait_timeout = wait_timeout
        self.wait_poll_interval = wait_poll_interval
        self.unknown_exceptions = unknown_exceptions
        self.worker_id = worker_id or f"w_{uuid.uuid4().hex[:12]}"
        self.clock = clock

    # ------------------------------------------------------------------ #

    def execute(
        self,
        fn: Callable[..., Any],
        *,
        tool: str,
        args: dict[str, Any],
        scope: str,
        idempotency_key: str | None = None,
        idempotency_fields: list[str] | None = None,
        max_attempts: int = 3,
    ) -> Any:
        """Run ``fn(**args)`` as a durable effect. Returns fn's value, or the
        cached value for a duplicate key. Raises the errors documented in
        :mod:`openonce.errors`."""
        record, created = self._admit(
            tool=tool,
            args=args,
            scope=scope,
            idempotency_key=idempotency_key,
            idempotency_fields=idempotency_fields,
            max_attempts=max_attempts,
        )
        if not created:
            record = self._handle_duplicate(record, args, idempotency_key, idempotency_fields)
            if record.state in REPLAYABLE:
                return self._replay(record)
            # APPROVED: approval was granted (or a pre-STARTED crash left it
            # armed) — this caller takes over execution. Re-entrant by design.
            return self._run_attempt(fn, record, args)

        record = self._gate(record)
        return self._run_attempt(fn, record, args)

    # -- phase 1: admission -------------------------------------------- #

    def _admit(
        self,
        *,
        tool: str,
        args: dict[str, Any],
        scope: str,
        idempotency_key: str | None,
        idempotency_fields: list[str] | None,
        max_attempts: int,
    ) -> tuple[EffectRecord, bool]:
        if idempotency_key is not None:
            key = idempotency_key
            # Explicit key: fingerprint covers ALL args (Stripe semantics —
            # same key + different params is caller error).
            fp = fingerprint(args)
        else:
            key = derive_key(tool, args, scope=scope, fields=idempotency_fields)
            # Derived key: fingerprint covers exactly the key material, so
            # whitelisted-field matches replay even if noise fields differ.
            fp = fingerprint(select_fields(args, idempotency_fields))

        effect_id = new_effect_id()
        record = EffectRecord(
            effect_id=effect_id,
            idempotency_key=key,
            tool=tool,
            state=S.PLANNED,
            args_fingerprint=fp,
            args_json=json.dumps(args, default=str),
            scope=scope,
            # Derived from OUR id, not the caller's key: stable across retries
            # of this effect, unique across effects.
            provider_key=f"{effect_id}:{tool}",
            max_attempts=max_attempts,
        )
        return self.store.create_or_get(record)

    # -- duplicate path -------------------------------------------------- #

    def _handle_duplicate(
        self,
        record: EffectRecord,
        args: dict[str, Any],
        idempotency_key: str | None,
        idempotency_fields: list[str] | None,
    ) -> EffectRecord:
        expected_fp = (
            fingerprint(args)
            if idempotency_key is not None
            else fingerprint(select_fields(args, idempotency_fields))
        )
        if record.args_fingerprint != expected_fp:
            raise IdempotencyMismatch(record.idempotency_key)

        if record.state in REPLAYABLE:
            return record
        if record.state == S.REQUIRES_APPROVAL:
            raise ApprovalPending(record)
        if record.state in (S.DENIED, S.CANCELED):
            raise EffectDenied(record)
        if record.state == S.UNKNOWN or record.state == S.HUMAN_REVIEW:
            raise EffectUnknown(record)
        if record.state == S.APPROVED:
            # Approved but not started (e.g. approval granted after a previous
            # ApprovalPending, or a crash before STARTED). This caller may run it.
            return record

        # PLANNED / STARTED / RECEIPT_RECORDED: someone is (or was) executing.
        deadline = self.clock() + self.wait_timeout
        while self.clock() < deadline:
            fresh = self.store.get(record.effect_id)
            assert fresh is not None
            if fresh.state in REPLAYABLE:
                return fresh
            if fresh.state in (S.UNKNOWN, S.HUMAN_REVIEW):
                raise EffectUnknown(fresh)
            if fresh.state == S.STARTED and self._lease_expired(fresh):
                # Executor died mid-call: outcome ambiguous. Park it.
                parked = self.store.transition(
                    fresh.effect_id,
                    {S.STARTED},
                    S.UNKNOWN,
                    payload={"reason": "lease expired while in flight"},
                )
                raise EffectUnknown(parked or fresh)
            time.sleep(self.wait_poll_interval)
        raise EffectInFlight(self.store.get(record.effect_id) or record)

    def _replay(self, record: EffectRecord) -> Any:
        assert record.result is not None, "replayable record must carry a result"
        if record.result.ok:
            return record.result.value
        raise ReplayedFailure(record, record.result.error or "unknown error")

    # -- phase 2: policy gate -------------------------------------------- #

    def _gate(self, record: EffectRecord) -> EffectRecord:
        decision: Decision = self.policy(record)
        if decision.verdict == Verdict.ALLOW:
            updated = self.store.transition(
                record.effect_id,
                {S.PLANNED},
                S.APPROVED,
                payload={"policy": "allow", "reason": decision.reason},
            )
        elif decision.verdict == Verdict.REQUIRE_APPROVAL:
            updated = self.store.transition(
                record.effect_id,
                {S.PLANNED},
                S.REQUIRES_APPROVAL,
                payload={"policy": "require_approval", "reason": decision.reason},
                set_fields={"note": decision.reason},
            )
            raise ApprovalPending(updated or record)
        else:
            updated = self.store.transition(
                record.effect_id,
                {S.PLANNED},
                S.DENIED,
                payload={"policy": "deny", "reason": decision.reason},
                set_fields={"note": decision.reason},
            )
            raise EffectDenied(updated or record)
        assert updated is not None, "PLANNED->APPROVED raced; single admitter holds PLANNED"
        return updated

    # -- phase 3: the attempt -------------------------------------------- #

    def _run_attempt(
        self, fn: Callable[..., Any], record: EffectRecord, args: dict[str, Any]
    ) -> Any:
        started = self.store.transition(
            record.effect_id,
            {S.APPROVED},
            S.STARTED,
            set_fields={
                "attempt": record.attempt + 1,
                "lease_expires_at": self.clock() + self.lease_seconds,
            },
            lease_owner=self.worker_id,
            payload={"worker": self.worker_id, "attempt": record.attempt + 1},
        )
        if started is None:
            # Lost the race to another worker holding APPROVED.
            fresh = self.store.get(record.effect_id)
            raise EffectInFlight(fresh or record)

        ctx = EffectContext(
            effect_id=started.effect_id,
            idempotency_key=started.idempotency_key,
            provider_key=started.provider_key,
            attempt=started.attempt,
        )
        token = _current.set(ctx)
        try:
            value = fn(**args)
        except RetryableEffectError as exc:
            # Definitively did not happen. Re-arm if attempts remain.
            if started.attempt < started.max_attempts:
                self.store.transition(
                    started.effect_id,
                    {S.STARTED},
                    S.UNKNOWN,  # via UNKNOWN->APPROVED to keep the table honest
                    payload={"error": str(exc), "class": "retryable"},
                    require_lease_owner=self.worker_id,
                )
                rearmed = self.store.transition(
                    started.effect_id,
                    {S.UNKNOWN},
                    S.APPROVED,
                    payload={"reason": "retryable error; re-armed", "attempt": started.attempt},
                )
                if rearmed is not None:
                    return self._run_attempt(fn, rearmed, args)
            return self._record_failure(started, exc)
        except UnknownOutcomeError as exc:
            parked = self.store.transition(
                started.effect_id,
                {S.STARTED},
                S.UNKNOWN,
                payload={"error": str(exc), "class": "unknown"},
                require_lease_owner=self.worker_id,
            )
            raise EffectUnknown(parked or started, exc) from exc
        except self.unknown_exceptions as exc:
            parked = self.store.transition(
                started.effect_id,
                {S.STARTED},
                S.UNKNOWN,
                payload={"error": repr(exc), "class": "unknown(default)"},
                require_lease_owner=self.worker_id,
            )
            raise EffectUnknown(parked or started, exc) from exc
        except Exception as exc:
            return self._record_failure(started, exc)
        finally:
            _current.reset(token)

        # Success: receipt, then commit. Two transitions, two journal entries —
        # verification hooks slot between them later without a schema change.
        result = EffectResult(ok=True, value=_jsonable(value))
        receipted = self.store.transition(
            started.effect_id,
            {S.STARTED},
            S.RECEIPT_RECORDED,
            set_fields={"result": result},
            require_lease_owner=self.worker_id,
            payload={"receipt": "handler returned"},
        )
        if receipted is None:
            # Our lease was taken (e.g. reconciler already resolved us).
            fresh = self.store.get(started.effect_id)
            if fresh is not None and fresh.state in REPLAYABLE:
                return self._replay(fresh)
            raise EffectUnknown(fresh or started)
        self.store.transition(receipted.effect_id, {S.RECEIPT_RECORDED}, S.COMMITTED, payload={})
        return value

    def _record_failure(self, record: EffectRecord, exc: BaseException) -> Any:
        result = EffectResult(ok=False, error=str(exc), error_type=type(exc).__name__)
        self.store.transition(
            record.effect_id,
            {S.STARTED},
            S.FAILED,
            set_fields={"result": result},
            require_lease_owner=self.worker_id,
            payload={"error": str(exc), "error_type": type(exc).__name__},
        )
        raise exc

    # ------------------------------------------------------------------ #

    def _lease_expired(self, record: EffectRecord) -> bool:
        return record.lease_expires_at is not None and record.lease_expires_at <= self.clock()


def _jsonable(value: Any) -> Any:
    """Ensure the cached result is JSON-serializable (it will be replayed)."""
    try:
        json.dumps(value)
        return value
    except (TypeError, ValueError):
        return repr(value)


def is_async_callable(fn: Callable[..., Any]) -> bool:
    return inspect.iscoroutinefunction(fn) or inspect.iscoroutinefunction(
        getattr(fn, "__call__", None)  # noqa: B004
    )
