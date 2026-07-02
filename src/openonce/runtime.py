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
import warnings
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

#: Third-party libraries (requests, httpx, urllib3, aiohttp) raise timeouts
#: that are NOT subclasses of builtin TimeoutError, so type matching alone
#: silently misclassifies "the request may have landed" as a definitive
#: failure — the exact bug this library exists to prevent. Classification
#: therefore also walks type(exc).__mro__ *names*. Exact class-name matches,
#: checked against every class in the MRO.
DEFAULT_UNKNOWN_EXCEPTION_NAMES: frozenset[str] = frozenset(
    {
        # requests / urllib3 — raised after the request may have been sent
        "ReadTimeout",
        "ReadTimeoutError",
        "ConnectionError",  # wraps mid-flight resets too; never assume "not sent"
        "ProtocolError",
        "RemoteDisconnected",
        "IncompleteRead",
        "ChunkedEncodingError",
        # httpx
        "TimeoutException",
        "WriteTimeout",
        "PoolTimeout",
        "ReadError",
        "WriteError",
        "NetworkError",
        # aiohttp
        "ServerTimeoutError",
        "ServerDisconnectedError",
        "ClientOSError",
        # generic
        "Timeout",
        "SocketTimeout",
    }
)

#: Connect-phase failures: the connection was never established, so nothing
#: was sent — definitively retryable. Checked BEFORE the unknown sets, since
#: e.g. requests.ConnectTimeout also has "Timeout"/"ConnectionError" in its MRO.
DEFAULT_RETRYABLE_EXCEPTION_NAMES: frozenset[str] = frozenset(
    {
        "ConnectTimeout",
        "ConnectTimeoutError",
        "ConnectError",
        "ConnectionRefusedError",
        "NewConnectionError",
        "ClientConnectorError",
        "NameResolutionError",
        "gaierror",
    }
)


def classify_exception(
    exc: BaseException,
    *,
    unknown_types: tuple[type[BaseException], ...] = DEFAULT_UNKNOWN_EXCEPTIONS,
    unknown_names: frozenset[str] = DEFAULT_UNKNOWN_EXCEPTION_NAMES,
    retryable_names: frozenset[str] = DEFAULT_RETRYABLE_EXCEPTION_NAMES,
) -> str:
    """Classify a handler exception: ``"retryable"`` / ``"unknown"`` / ``"failed"``.

    Explicit signals win; then connect-phase (retryable) name matches; then
    outcome-ambiguous type or name matches; anything else is a definitive
    business failure (cached and replayed).
    """
    if isinstance(exc, RetryableEffectError):
        return "retryable"
    if isinstance(exc, UnknownOutcomeError):
        return "unknown"
    mro_names = {cls.__name__ for cls in type(exc).__mro__}
    if mro_names & retryable_names:
        return "retryable"
    if isinstance(exc, unknown_types) or (mro_names & unknown_names):
        return "unknown"
    return "failed"


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


def _push_effect_context(ctx: EffectContext | None) -> contextvars.Token[EffectContext | None]:
    """Internal: re-establish the effect context on another execution context
    (the async wrapper ships handler coroutines across threads)."""
    return _current.set(ctx)


def _pop_effect_context(token: contextvars.Token[EffectContext | None]) -> None:
    _current.reset(token)


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
        unknown_exception_names: frozenset[str] = DEFAULT_UNKNOWN_EXCEPTION_NAMES,
        retryable_exception_names: frozenset[str] = DEFAULT_RETRYABLE_EXCEPTION_NAMES,
        worker_id: str | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.store = store
        self.policy = policy
        self.lease_seconds = lease_seconds
        self.wait_timeout = wait_timeout
        self.wait_poll_interval = wait_poll_interval
        self.unknown_exceptions = unknown_exceptions
        self.unknown_exception_names = unknown_exception_names
        self.retryable_exception_names = retryable_exception_names
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
            expected_fp = (
                fingerprint(args)
                if idempotency_key is not None
                else fingerprint(select_fields(args, idempotency_fields))
            )
            if record.args_fingerprint != expected_fp:
                raise IdempotencyMismatch(record.idempotency_key)
            return self._follow(fn, record, args)

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

    # -- join path (duplicates and lost races) ----------------------------- #

    def _follow(self, fn: Callable[..., Any], record: EffectRecord, args: dict[str, Any]) -> Any:
        """Join an effect someone else admitted: replay its result, surface its
        parked states, take over an armed APPROVED, or wait on an in-flight
        execution. Used by duplicate callers AND by workers that lose the
        APPROVED->STARTED race — losing a CAS must mean waiting, not erroring.
        """
        deadline = self.clock() + self.wait_timeout
        rec: EffectRecord | None = record
        while True:
            assert rec is not None
            if rec.state in REPLAYABLE:
                return self._replay(rec)
            if rec.state == S.REQUIRES_APPROVAL:
                raise ApprovalPending(rec)
            if rec.state in (S.DENIED, S.CANCELED):
                raise EffectDenied(rec)
            if rec.state in (S.UNKNOWN, S.HUMAN_REVIEW):
                raise EffectUnknown(rec)
            if rec.state == S.APPROVED:
                # Armed but not running (approval granted, reconciler re-arm,
                # or a crash before STARTED): this caller takes over. If it
                # loses the STARTED race, _run_attempt re-enters _follow —
                # bounded, because every re-entry means the state advanced.
                return self._run_attempt(fn, rec, args)
            if rec.state == S.STARTED and self._lease_expired(rec):
                # Executor died mid-call: outcome ambiguous. Park it.
                parked = self.store.transition(
                    rec.effect_id,
                    {S.STARTED},
                    S.UNKNOWN,
                    payload={"reason": "lease expired while in flight"},
                )
                raise EffectUnknown(parked or rec)
            # PLANNED / STARTED / RECEIPT_RECORDED: someone is executing.
            if self.clock() >= deadline:
                raise EffectInFlight(rec)
            time.sleep(self.wait_poll_interval)
            rec = self.store.get(record.effect_id)

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
            # Lost the APPROVED->STARTED race: someone else is executing this
            # exact effect. Join them — wait and replay their result.
            fresh = self.store.get(record.effect_id)
            assert fresh is not None
            return self._follow(fn, fresh, args)

        ctx = EffectContext(
            effect_id=started.effect_id,
            idempotency_key=started.idempotency_key,
            provider_key=started.provider_key,
            attempt=started.attempt,
        )
        token = _current.set(ctx)
        try:
            value = fn(**args)
        except Exception as exc:
            # KeyboardInterrupt/SystemExit deliberately propagate uncaught:
            # the record stays STARTED, its lease expires, and the reconciler
            # treats it as the crash it effectively is.
            kind = classify_exception(
                exc,
                unknown_types=self.unknown_exceptions,
                unknown_names=self.unknown_exception_names,
                retryable_names=self.retryable_exception_names,
            )
            if kind == "retryable":
                return self._retry_or_fail(fn, started, args, exc)
            if kind == "unknown":
                parked = self.store.transition(
                    started.effect_id,
                    {S.STARTED},
                    S.UNKNOWN,
                    payload={"error": repr(exc), "class": f"unknown({type(exc).__name__})"},
                    require_lease_owner=self.worker_id,
                )
                raise EffectUnknown(parked or started, exc) from exc
            return self._record_failure(started, exc)
        finally:
            _current.reset(token)

        # Success: receipt, then commit. Two transitions, two journal entries —
        # verification hooks slot between them later without a schema change.
        # The result is canonicalized (JSON round-trip) so the first caller and
        # every replay observe the IDENTICAL value.
        value = _canonical_result(value, started.tool)
        result = EffectResult(ok=True, value=value)
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

    def _retry_or_fail(
        self, fn: Callable[..., Any], started: EffectRecord, args: dict[str, Any], exc: Exception
    ) -> Any:
        """The effect definitively did not happen. Re-arm if attempts remain."""
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


def _canonical_result(value: Any, tool: str) -> Any:
    """JSON round-trip the handler's result so the first caller and every
    replay observe the identical value (tuples become lists for everyone,
    not just for replays). Non-serializable results degrade to repr() with
    a loud warning — replaying an object faithfully is impossible."""
    try:
        return json.loads(json.dumps(value))
    except (TypeError, ValueError):
        warnings.warn(
            f"{tool}: handler returned non-JSON-serializable "
            f"{type(value).__name__}; recording repr() — every caller (including "
            f"this one) receives the repr string. Return JSON-native values for "
            f"faithful replay.",
            RuntimeWarning,
            stacklevel=4,
        )
        return repr(value)


def is_async_callable(fn: Callable[..., Any]) -> bool:
    return inspect.iscoroutinefunction(fn) or inspect.iscoroutinefunction(
        getattr(fn, "__call__", None)  # noqa: B004
    )
