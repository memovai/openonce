"""Execution-semantics tests: dedup, replay (incl. failures), approval,
UNKNOWN parking, retryable re-execution, concurrency, audit trail.
Every test runs against both stores via the make_store fixture.
"""

from __future__ import annotations

import threading

import pytest

from openonce import (
    ApprovalPending,
    EffectDenied,
    EffectState,
    EffectUnknown,
    IdempotencyMismatch,
    OpenOnce,
    ReplayedFailure,
    RetryableEffectError,
    ScopeRequiredError,
    UnknownOutcomeError,
    current_effect,
    require_approval_for,
)


def make_oo(make_store, clock=None, **kw) -> OpenOnce:
    import time

    clk = clock or time.time
    return OpenOnce(make_store(clk), clock=clk, **kw)


class TestDedupAndReplay:
    def test_executes_once_and_replays(self, make_store) -> None:
        oo = make_oo(make_store)
        calls: list[int] = []

        @oo.effect(tool="email.send", idempotency_fields=["to", "subject"])
        def send(to: str, subject: str, body: str) -> str:
            calls.append(1)
            return f"sent-{len(calls)}"

        with oo.scope("run1"):
            first = send(to="a@b.c", subject="hi", body="v1")
            second = send(to="a@b.c", subject="hi", body="v2 reworded by the LLM")

        assert first == second == "sent-1"
        assert len(calls) == 1  # noise field changed, effect did not re-fire

    def test_different_scope_reexecutes(self, make_store) -> None:
        oo = make_oo(make_store)
        calls: list[int] = []

        @oo.effect(tool="t")
        def do(x: int) -> int:
            calls.append(x)
            return x

        with oo.scope("run1"):
            do(x=1)
        with oo.scope("run2"):
            do(x=1)
        assert len(calls) == 2

    def test_failure_is_cached_and_replayed(self, make_store) -> None:
        """Stripe semantics: a definitive failure replays, it does not re-execute."""
        oo = make_oo(make_store)
        calls: list[int] = []

        @oo.effect(tool="t")
        def boom() -> None:
            calls.append(1)
            raise ValueError("repo already exists")

        with oo.scope("run1"):
            with pytest.raises(ValueError, match="repo already exists"):
                boom()
            with pytest.raises(ReplayedFailure, match="repo already exists"):
                boom()
        assert len(calls) == 1

    def test_explicit_key_mismatch_rejected(self, make_store) -> None:
        oo = make_oo(make_store)

        @oo.effect(tool="t")
        def do(x: int) -> int:
            return x

        assert do(x=1, idempotency_key="k1") == 1
        with pytest.raises(IdempotencyMismatch):
            do(x=2, idempotency_key="k1")

    def test_scope_required_for_derived_keys(self, make_store) -> None:
        oo = make_oo(make_store)

        @oo.effect(tool="t")
        def do(x: int) -> int:
            return x

        with pytest.raises(ScopeRequiredError, match=r"oo\.scope"):
            do(x=1)
        assert do(x=1, idempotency_key="k") == 1  # explicit key needs no scope

    def test_positional_args_rejected(self, make_store) -> None:
        oo = make_oo(make_store)

        @oo.effect(tool="t")
        def do(x: int) -> int:
            return x

        with oo.scope("r"), pytest.raises(TypeError, match="keywords"):
            do(1)


class TestApproval:
    def test_approval_flow_is_reentrant(self, make_store) -> None:
        oo = make_oo(make_store, policy=require_approval_for(["stripe.*"]))
        calls: list[int] = []

        @oo.effect(tool="stripe.refund")
        def refund(charge: str, amount_cents: int) -> str:
            calls.append(1)
            return "refunded"

        with oo.scope("run1"):
            with pytest.raises(ApprovalPending) as exc_info:
                refund(charge="ch_1", amount_cents=500)
            assert calls == []

            oo.approve(exc_info.value.effect_id, by="eric")
            # The agent simply retries the same call — same key, picks up APPROVED.
            assert refund(charge="ch_1", amount_cents=500) == "refunded"
            assert len(calls) == 1
            # And a further duplicate replays.
            assert refund(charge="ch_1", amount_cents=500) == "refunded"
            assert len(calls) == 1

    def test_denied(self, make_store) -> None:
        oo = make_oo(make_store, policy=require_approval_for(["stripe.*"]))

        @oo.effect(tool="stripe.refund")
        def refund(charge: str) -> str:
            return "refunded"

        with oo.scope("run1"):
            with pytest.raises(ApprovalPending) as exc_info:
                refund(charge="ch_1")
            oo.deny(exc_info.value.effect_id, reason="wrong customer")
            with pytest.raises(EffectDenied):
                refund(charge="ch_1")

    def test_pending_review_lists_the_queue(self, make_store) -> None:
        oo = make_oo(make_store, policy=require_approval_for(["stripe.*"]))

        @oo.effect(tool="stripe.refund")
        def refund(charge: str) -> str:
            return "ok"

        with oo.scope("run1"), pytest.raises(ApprovalPending):
            refund(charge="ch_1")
        queue = oo.pending_review()
        assert len(queue) == 1
        assert queue[0].state == EffectState.REQUIRES_APPROVAL


class TestUnknownOutcome:
    def test_timeout_parks_as_unknown_and_never_blind_retries(self, make_store) -> None:
        oo = make_oo(make_store)
        calls: list[int] = []

        @oo.effect(tool="pay.charge")
        def charge(amount_cents: int) -> str:
            calls.append(1)
            raise TimeoutError("read timed out after send")

        with oo.scope("run1"):
            with pytest.raises(EffectUnknown):
                charge(amount_cents=500)
            # Duplicate call must NOT re-execute a maybe-happened charge.
            with pytest.raises(EffectUnknown):
                charge(amount_cents=500)
        assert len(calls) == 1
        rec = oo.pending_review()[0]
        assert rec.state == EffectState.UNKNOWN

    def test_explicit_unknown_outcome_error(self, make_store) -> None:
        oo = make_oo(make_store)

        @oo.effect(tool="t")
        def do() -> None:
            raise UnknownOutcomeError("socket closed mid-response")

        with oo.scope("r"), pytest.raises(EffectUnknown):
            do()

    def test_retryable_error_reexecutes_up_to_max(self, make_store) -> None:
        oo = make_oo(make_store)
        attempts: list[int] = []

        @oo.effect(tool="t", max_attempts=3)
        def flaky() -> str:
            ctx = current_effect()
            assert ctx is not None
            attempts.append(ctx.attempt)
            if len(attempts) < 3:
                raise RetryableEffectError("connection refused — nothing sent")
            return "ok"

        with oo.scope("r"):
            assert flaky() == "ok"
        assert attempts == [1, 2, 3]

    def test_retryable_exhaustion_fails_definitively(self, make_store) -> None:
        oo = make_oo(make_store)
        calls: list[int] = []

        @oo.effect(tool="t", max_attempts=2)
        def always_refused() -> str:
            calls.append(1)
            raise RetryableEffectError("connection refused")

        with oo.scope("r"):
            with pytest.raises(RetryableEffectError):
                always_refused()
            with pytest.raises(ReplayedFailure):
                always_refused()
        assert len(calls) == 2


class TestProviderKey:
    def test_provider_key_stable_across_retries(self, make_store) -> None:
        """The key we hand to the provider must not change between attempts —
        it is the provider-side dedup defense."""
        oo = make_oo(make_store)
        seen: list[str] = []

        @oo.effect(tool="stripe.charge", max_attempts=2)
        def charge() -> str:
            ctx = current_effect()
            assert ctx is not None
            seen.append(ctx.provider_key)
            if len(seen) < 2:
                raise RetryableEffectError("refused")
            return "ok"

        with oo.scope("r"):
            charge()
        assert len(seen) == 2
        assert seen[0] == seen[1]


class TestConcurrency:
    def test_first_writer_wins_under_threads(self, make_store) -> None:
        oo = make_oo(make_store)
        calls: list[int] = []
        results: list[str] = []
        errors: list[BaseException] = []
        barrier = threading.Barrier(8)

        @oo.effect(tool="email.send")
        def send(to: str) -> str:
            calls.append(1)
            return "sent"

        def worker() -> None:
            barrier.wait()
            try:
                with oo.scope("run1"):
                    results.append(send(to="a@b.c"))
            except BaseException as exc:
                errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors, errors
        assert len(calls) == 1  # exactly one execution
        assert results == ["sent"] * 8  # everyone got the same receipt


class TestAuditTrail:
    def test_journal_records_full_lifecycle(self, make_store) -> None:
        oo = make_oo(make_store)

        @oo.effect(tool="t")
        def do() -> str:
            return "ok"

        with oo.scope("r"):
            do()
        # Find the record via the store scan (no state filter API needed here).
        es = EffectState
        recs = oo.store.scan_states({es.COMMITTED}, updated_before=float("inf"))
        assert len(recs) == 1
        states = [e.to_state for e in oo.journal(recs[0].effect_id)]
        assert states == [es.PLANNED, es.APPROVED, es.STARTED, es.RECEIPT_RECORDED, es.COMMITTED]
