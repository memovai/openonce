"""Reconciler tests: crash simulation, grace period, probe-driven resolution."""

from __future__ import annotations

import pytest

from openonce import (
    EffectState,
    EffectUnknown,
    OpenOnce,
    ProbeOutcome,
    ProbeResult,
    Reconciler,
)
from openonce.records import EffectRecord

S = EffectState


class StubProber:
    def __init__(self, outcome: ProbeOutcome, receipt: dict | None = None) -> None:
        self.outcome = outcome
        self.receipt = receipt
        self.probed: list[str] = []

    def probe(self, record: EffectRecord) -> ProbeResult:
        self.probed.append(record.effect_id)
        return ProbeResult(outcome=self.outcome, receipt=self.receipt, detail="stub")


def park_unknown_effect(oo: OpenOnce, tool: str = "pay.charge") -> str:
    """Drive one effect into UNKNOWN via an ambiguous timeout; return effect_id."""

    @oo.effect(tool=tool)
    def charge(amount_cents: int) -> str:
        raise TimeoutError("ambiguous")

    with oo.scope("run1"), pytest.raises(EffectUnknown) as exc_info:
        charge(amount_cents=500)
    return exc_info.value.record.effect_id


class TestGracePeriod:
    def test_young_unknowns_are_left_alone(self, make_store, clock) -> None:
        oo = OpenOnce(make_store(clock), clock=clock)
        eid = park_unknown_effect(oo)
        rec = Reconciler(oo.store, grace_seconds=300, clock=clock)
        rec.register("pay.charge", StubProber(ProbeOutcome.HAPPENED))

        clock.advance(60)  # still inside grace: original caller's window
        assert rec.run_once().total() == 0
        assert oo.get(eid).state == S.UNKNOWN

        clock.advance(300)  # past grace: reconciler takes over
        report = rec.run_once()
        assert report.committed == [eid]

    def test_per_tool_grace_override(self, make_store, clock) -> None:
        oo = OpenOnce(make_store(clock), clock=clock)
        eid = park_unknown_effect(oo, tool="email.send")
        rec = Reconciler(
            oo.store,
            grace_seconds=300,
            grace_overrides={"email.send": 30},
            clock=clock,
        )
        rec.register("email.send", StubProber(ProbeOutcome.HAPPENED))

        clock.advance(60)  # past the email override, under the default
        assert rec.run_once().committed == [eid]


class TestProbeResolution:
    def test_happened_commits_with_probe_receipt(self, make_store, clock) -> None:
        oo = OpenOnce(make_store(clock), clock=clock)
        eid = park_unknown_effect(oo)
        rec = Reconciler(oo.store, grace_seconds=0, clock=clock)
        rec.register("pay.charge", StubProber(ProbeOutcome.HAPPENED, {"charge_id": "ch_42"}))

        clock.advance(1)
        assert rec.run_once().committed == [eid]
        final = oo.get(eid)
        assert final.state == S.COMMITTED
        assert final.result.ok and final.result.value == {"charge_id": "ch_42"}

    def test_happened_receipt_replays_to_duplicate_callers(self, make_store, clock) -> None:
        oo = OpenOnce(make_store(clock), clock=clock)
        calls: list[int] = []

        @oo.effect(tool="pay.charge")
        def charge(amount_cents: int) -> str:
            calls.append(1)
            raise TimeoutError("ambiguous")

        with oo.scope("run1"), pytest.raises(EffectUnknown):
            charge(amount_cents=500)

        rec = Reconciler(oo.store, grace_seconds=0, clock=clock)
        rec.register("pay.charge", StubProber(ProbeOutcome.HAPPENED, {"charge_id": "ch_42"}))
        clock.advance(1)
        rec.run_once()

        with oo.scope("run1"):
            assert charge(amount_cents=500) == {"charge_id": "ch_42"}
        assert len(calls) == 1  # never re-executed

    def test_not_happened_rearms_for_retry(self, make_store, clock) -> None:
        oo = OpenOnce(make_store(clock), clock=clock)
        eid = park_unknown_effect(oo)
        rec = Reconciler(oo.store, grace_seconds=0, clock=clock)
        rec.register("pay.charge", StubProber(ProbeOutcome.NOT_HAPPENED))

        clock.advance(1)
        assert rec.run_once().rearmed == [eid]
        assert oo.get(eid).state == S.APPROVED  # eligible for re-execution

    def test_not_happened_with_exhausted_attempts_fails(self, make_store, clock) -> None:
        oo = OpenOnce(make_store(clock), clock=clock)

        @oo.effect(tool="pay.charge", max_attempts=1)
        def charge() -> str:
            raise TimeoutError("ambiguous")

        with oo.scope("run1"), pytest.raises(EffectUnknown) as exc_info:
            charge()
        eid = exc_info.value.record.effect_id

        rec = Reconciler(oo.store, grace_seconds=0, clock=clock)
        rec.register("pay.charge", StubProber(ProbeOutcome.NOT_HAPPENED))
        clock.advance(1)
        assert rec.run_once().failed == [eid]
        assert oo.get(eid).state == S.FAILED

    def test_inconclusive_escalates_to_human(self, make_store, clock) -> None:
        oo = OpenOnce(make_store(clock), clock=clock)
        eid = park_unknown_effect(oo)
        rec = Reconciler(oo.store, grace_seconds=0, clock=clock)
        rec.register("pay.charge", StubProber(ProbeOutcome.INCONCLUSIVE))

        clock.advance(1)
        assert rec.run_once().escalated == [eid]
        assert oo.get(eid).state == S.HUMAN_REVIEW

    def test_no_prober_escalates_never_blind_retries(self, make_store, clock) -> None:
        oo = OpenOnce(make_store(clock), clock=clock)
        eid = park_unknown_effect(oo)
        rec = Reconciler(oo.store, grace_seconds=0, clock=clock)  # no probers at all

        clock.advance(1)
        assert rec.run_once().escalated == [eid]
        assert oo.get(eid).state == S.HUMAN_REVIEW
        assert "no prober" in oo.get(eid).note

    def test_broken_prober_escalates_instead_of_crashing(self, make_store, clock) -> None:
        oo = OpenOnce(make_store(clock), clock=clock)
        eid = park_unknown_effect(oo)

        class Broken:
            def probe(self, record: EffectRecord) -> ProbeResult:
                raise RuntimeError("provider API down")

        rec = Reconciler(oo.store, grace_seconds=0, clock=clock)
        rec.register("pay.charge", Broken())
        clock.advance(1)
        assert rec.run_once().escalated == [eid]


class TestCrashRecovery:
    def test_dead_executor_lease_expiry_goes_unknown_then_reconciles(
        self, make_store, clock
    ) -> None:
        """Simulate a hard crash between STARTED and RECEIPT_RECORDED: the
        record sits in STARTED with a lease that expires. The reconciler must
        park it as UNKNOWN and then resolve it by probing."""
        oo = OpenOnce(make_store(clock), clock=clock, lease_seconds=60)

        # Hand-craft the crash: admit + gate + start, then never record a receipt.
        rt = oo.runtime
        record, created = rt._admit(
            tool="pay.charge",
            args={"amount_cents": 500},
            scope="run1",
            idempotency_key=None,
            idempotency_fields=None,
            max_attempts=3,
        )
        assert created
        record = rt._gate(record)
        started = oo.store.transition(
            record.effect_id,
            {S.APPROVED},
            S.STARTED,
            set_fields={"attempt": 1, "lease_expires_at": clock() + 60},
            lease_owner="dead-worker",
        )
        assert started is not None  # ...and the process dies here.

        rec = Reconciler(oo.store, grace_seconds=300, clock=clock)
        rec.register("pay.charge", StubProber(ProbeOutcome.HAPPENED, {"charge_id": "ch_1"}))

        clock.advance(400)  # lease long expired, grace passed
        report = rec.run_once()
        # First pass parks STARTED->UNKNOWN; UNKNOWN is younger than grace now,
        # so resolution happens on a later pass — two-phase, deliberately.
        assert oo.get(record.effect_id).state == S.UNKNOWN
        clock.advance(400)
        report = rec.run_once()
        assert report.committed == [record.effect_id]
        assert oo.get(record.effect_id).state == S.COMMITTED

    def test_live_lease_is_not_stolen(self, make_store, clock) -> None:
        """A STARTED effect whose lease is still live belongs to a living worker —
        the reconciler must not touch it even past grace."""
        oo = OpenOnce(make_store(clock), clock=clock)
        rt = oo.runtime
        record, _ = rt._admit(
            tool="t",
            args={},
            scope="r",
            idempotency_key=None,
            idempotency_fields=None,
            max_attempts=3,
        )
        record = rt._gate(record)
        oo.store.transition(
            record.effect_id,
            {S.APPROVED},
            S.STARTED,
            set_fields={"attempt": 1, "lease_expires_at": clock() + 10_000},
            lease_owner="alive-worker",
        )
        rec = Reconciler(oo.store, grace_seconds=0, clock=clock)
        clock.advance(500)  # past grace but lease still live
        # advance moved the clock past the lease? No: lease = start + 10_000.
        assert rec.run_once().total() == 0
        assert oo.get(record.effect_id).state == S.STARTED
