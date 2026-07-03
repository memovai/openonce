"""Reconciler tests: crash simulation, grace period, probe-driven resolution."""

from __future__ import annotations

import pytest

from openonce import (
    CapabilityConformanceEvidence,
    CapabilityTier,
    EffectState,
    EffectUnknown,
    OpenOnce,
    ProbeOutcome,
    ProbeResult,
    ProviderCapability,
    Reconciler,
    capabilities_for_tool,
    capability_fingerprint,
)
from openonce.records import EffectRecord, EffectResult

S = EffectState


SLACK_CAPABILITY = ProviderCapability(
    name="slack_metadata",
    tool_pattern="slack.post_message",
    tier=CapabilityTier.SENDER_CONTROLLED_AUTHORITATIVE,
    key_strategy="message metadata event_payload.openonce_effect_id",
    probe_basis="conversations.history search by metadata",
    miss_semantics="not-happened after Slack history propagation",
    can_auto_rearm_on_miss=True,
    default_grace_seconds=30.0,
    prober="SlackMetadataProber",
    handler_requirements=(
        "stamp openonce effect_id into Slack message metadata",
        "grant the prober history scope for the target channel",
    ),
    risk="medium: depends on Slack retention and history permissions",
    required_receipt_fields=("ts",),
)


def stripe_conformance_evidence() -> dict[str, CapabilityConformanceEvidence]:
    [stripe] = capabilities_for_tool("stripe.charge")
    return {
        "stripe": CapabilityConformanceEvidence(
            capability="stripe",
            capability_fingerprint=capability_fingerprint(stripe),
            observations={
                "happened": ProbeResult(ProbeOutcome.HAPPENED, {"stripe_id": "pi_1"}),
                "young_miss": ProbeResult(ProbeOutcome.INCONCLUSIVE),
                "mature_miss": ProbeResult(ProbeOutcome.NOT_HAPPENED),
                "ambiguous": ProbeResult(ProbeOutcome.INCONCLUSIVE),
            },
        )
    }


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
        rec.register("pay.charge", StubProber(ProbeOutcome.HAPPENED, {"id": "pay_1"}))

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
        rec.register("email.send", StubProber(ProbeOutcome.HAPPENED, {"message_id": "msg_1"}))

        clock.advance(60)  # past the email override, under the default
        assert rec.run_once().committed == [eid]

    def test_builtin_capability_grace_applies_when_no_global_grace_is_set(
        self, make_store, clock
    ) -> None:
        oo = OpenOnce(make_store(clock), clock=clock)
        eid = park_unknown_effect(oo, tool="stripe.charge")
        rec = Reconciler(oo.store, clock=clock)
        rec.register("stripe.charge", StubProber(ProbeOutcome.HAPPENED, {"stripe_id": "pi_1"}))

        clock.advance(60)  # Stripe's built-in indexing-lag grace is 120s.
        assert rec.run_once().total() == 0
        assert oo.get(eid).state == S.UNKNOWN

        clock.advance(61)
        assert rec.run_once().committed == [eid]

    def test_explicit_global_grace_overrides_builtin_capability_grace(
        self, make_store, clock
    ) -> None:
        oo = OpenOnce(make_store(clock), clock=clock)
        eid = park_unknown_effect(oo, tool="stripe.charge")
        rec = Reconciler(oo.store, grace_seconds=0, clock=clock)
        rec.register("stripe.charge", StubProber(ProbeOutcome.HAPPENED, {"stripe_id": "pi_1"}))

        clock.advance(1)
        assert rec.run_once().committed == [eid]

    def test_custom_capability_grace_applies_when_no_global_grace_is_set(
        self, make_store, clock
    ) -> None:
        oo = OpenOnce(make_store(clock), clock=clock)
        eid = park_unknown_effect(oo, tool="slack.post_message")
        rec = Reconciler(oo.store, extra_capabilities=(SLACK_CAPABILITY,), clock=clock)
        rec.register(
            "slack.post_message",
            StubProber(ProbeOutcome.HAPPENED, {"ts": "123.456"}),
            capability="slack_metadata",
        )

        clock.advance(20)
        assert rec.run_once().total() == 0
        assert oo.get(eid).state == S.UNKNOWN

        clock.advance(11)
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

    def test_default_happened_without_receipt_still_commits(self, make_store, clock) -> None:
        """Baseline invariant: with no capability pin and no strict flags,
        a registered prober is trusted — HAPPENED commits even without a
        receipt. Enforcement is opt-in, never a silent upgrade change."""
        oo = OpenOnce(make_store(clock), clock=clock)
        eid = park_unknown_effect(oo)
        rec = Reconciler(oo.store, grace_seconds=0, clock=clock)
        rec.register("pay.charge", StubProber(ProbeOutcome.HAPPENED))

        clock.advance(1)
        report = rec.run_once()
        assert report.committed == [eid]
        assert oo.get(eid).state == S.COMMITTED

    def test_pinned_happened_without_receipt_escalates_to_human(self, make_store, clock) -> None:
        """With an explicit capability pin, HAPPENED must carry evidence."""
        oo = OpenOnce(make_store(clock), clock=clock)
        eid = park_unknown_effect(oo, tool="stripe.charge")
        rec = Reconciler(oo.store, grace_seconds=0, clock=clock)
        rec.register("stripe.charge", StubProber(ProbeOutcome.HAPPENED), capability="stripe")

        clock.advance(1)
        report = rec.run_once()

        assert report.escalated == [eid]
        assert report.committed == []
        reviewed = oo.get(eid)
        assert reviewed.state == S.HUMAN_REVIEW
        assert "did not return a receipt" in reviewed.note

    def test_happened_receipt_must_satisfy_provider_receipt_contract(
        self, make_store, clock
    ) -> None:
        oo = OpenOnce(make_store(clock), clock=clock)
        eid = park_unknown_effect(oo, tool="stripe.charge")
        rec = Reconciler(oo.store, grace_seconds=0, clock=clock)
        rec.register(
            "stripe.charge",
            StubProber(ProbeOutcome.HAPPENED, {"status": "succeeded"}),
            capability="stripe",
        )

        clock.advance(1)
        report = rec.run_once()

        assert report.escalated == [eid]
        assert report.committed == []
        reviewed = oo.get(eid)
        assert reviewed.state == S.HUMAN_REVIEW
        assert "missing required external evidence field(s): stripe_id" in reviewed.note

    def test_happened_receipt_must_satisfy_provider_receipt_source_contract(
        self, make_store, clock
    ) -> None:
        oo = OpenOnce(make_store(clock), clock=clock)

        @oo.effect(tool="github.create_pr", idempotency_fields=["owner", "repo", "head"])
        def create_pr(owner: str, repo: str, head: str) -> None:
            raise TimeoutError("ambiguous")

        with oo.scope("run1"), pytest.raises(EffectUnknown) as exc_info:
            create_pr(owner="acme", repo="api", head="fix-login")
        eid = exc_info.value.record.effect_id

        rec = Reconciler(oo.store, grace_seconds=0, clock=clock)
        rec.register(
            "github.create_pr",
            StubProber(ProbeOutcome.HAPPENED, {"number": 42, "head": "other-branch"}),
            capability="github_pr",
        )

        clock.advance(1)
        report = rec.run_once()

        assert report.escalated == [eid]
        assert report.committed == []
        reviewed = oo.get(eid)
        assert reviewed.state == S.HUMAN_REVIEW
        assert "does not satisfy required source contract" in reviewed.note
        assert "expected 'fix-login'" in reviewed.note

    def test_happened_receipt_source_contract_allows_matching_natural_key(
        self, make_store, clock
    ) -> None:
        oo = OpenOnce(make_store(clock), clock=clock)

        @oo.effect(tool="github.create_pr", idempotency_fields=["owner", "repo", "head"])
        def create_pr(owner: str, repo: str, head: str) -> None:
            raise TimeoutError("ambiguous")

        with oo.scope("run1"), pytest.raises(EffectUnknown) as exc_info:
            create_pr(owner="acme", repo="api", head="fix-login")
        eid = exc_info.value.record.effect_id

        rec = Reconciler(oo.store, grace_seconds=0, clock=clock)
        rec.register(
            "github.create_pr",
            StubProber(ProbeOutcome.HAPPENED, {"number": 42, "head": "fix-login"}),
        )

        clock.advance(1)

        assert rec.run_once().committed == [eid]
        assert oo.get(eid).state == S.COMMITTED

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

    def test_strict_resolution_requires_a_matching_capability_before_commit(
        self, make_store, clock
    ) -> None:
        oo = OpenOnce(make_store(clock), clock=clock)
        eid = park_unknown_effect(oo)
        rec = Reconciler(
            oo.store,
            grace_seconds=0,
            require_capability_for_resolution=True,
            clock=clock,
        )
        rec.register("pay.charge", StubProber(ProbeOutcome.HAPPENED, {"charge_id": "ch_42"}))

        clock.advance(1)
        report = rec.run_once()

        assert report.escalated == [eid]
        assert report.committed == []
        reviewed = oo.get(eid)
        assert reviewed.state == S.HUMAN_REVIEW
        assert "Strict resolution requires reviewed provider knowledge" in reviewed.note

    def test_strict_resolution_allows_happened_for_known_capability(
        self, make_store, clock
    ) -> None:
        oo = OpenOnce(make_store(clock), clock=clock)
        eid = park_unknown_effect(oo, tool="stripe.charge")
        rec = Reconciler(
            oo.store,
            grace_seconds=0,
            require_capability_for_resolution=True,
            clock=clock,
        )
        rec.register("stripe.charge", StubProber(ProbeOutcome.HAPPENED, {"stripe_id": "pi_1"}))

        clock.advance(1)

        assert rec.run_once().committed == [eid]
        assert oo.get(eid).state == S.COMMITTED

    def test_conformance_gate_requires_passing_evidence_before_commit(
        self, make_store, clock
    ) -> None:
        oo = OpenOnce(make_store(clock), clock=clock)
        eid = park_unknown_effect(oo, tool="stripe.charge")
        rec = Reconciler(
            oo.store,
            grace_seconds=0,
            require_conformance_for_resolution=True,
            clock=clock,
        )
        rec.register("stripe.charge", StubProber(ProbeOutcome.HAPPENED, {"stripe_id": "pi_1"}))

        clock.advance(1)
        report = rec.run_once()

        assert report.escalated == [eid]
        assert report.committed == []
        reviewed = oo.get(eid)
        assert reviewed.state == S.HUMAN_REVIEW
        assert "provider conformance is not proven" in reviewed.note
        assert "missing conformance observation" in reviewed.note

    def test_conformance_gate_allows_happened_with_passing_evidence(
        self, make_store, clock
    ) -> None:
        oo = OpenOnce(make_store(clock), clock=clock)
        eid = park_unknown_effect(oo, tool="stripe.charge")
        rec = Reconciler(
            oo.store,
            grace_seconds=0,
            conformance_evidence=stripe_conformance_evidence(),
            require_conformance_for_resolution=True,
            clock=clock,
        )
        rec.register("stripe.charge", StubProber(ProbeOutcome.HAPPENED, {"stripe_id": "pi_1"}))

        clock.advance(1)
        assert rec.run_once().committed == [eid]
        assert oo.get(eid).state == S.COMMITTED

    def test_not_happened_rearms_for_retry(self, make_store, clock) -> None:
        oo = OpenOnce(make_store(clock), clock=clock)
        eid = park_unknown_effect(oo)
        rec = Reconciler(oo.store, grace_seconds=0, clock=clock)
        rec.register("pay.charge", StubProber(ProbeOutcome.NOT_HAPPENED))

        clock.advance(1)
        assert rec.run_once().rearmed == [eid]
        assert oo.get(eid).state == S.APPROVED  # eligible for re-execution

    def test_not_happened_rearm_clears_stale_result_projection(self, make_store, clock) -> None:
        store = make_store(clock)
        stale = EffectRecord(
            effect_id="eff_stale_result",
            idempotency_key="run1:pay.charge:stale",
            tool="pay.charge",
            state=S.UNKNOWN,
            args_fingerprint="fp",
            args_json='{"amount_cents": 500}',
            scope="run1",
            provider_key="eff_stale_result:pay.charge",
            attempt=1,
            result=EffectResult(ok=True, value={"charge_id": "stale"}),
        )
        store.create_or_get(stale)
        rec = Reconciler(store, grace_seconds=0, clock=clock)
        rec.register("pay.charge", StubProber(ProbeOutcome.NOT_HAPPENED))

        assert rec.run_once().rearmed == ["eff_stale_result"]
        rearmed = store.get("eff_stale_result")
        assert rearmed.state == S.APPROVED
        assert rearmed.result is None

    def test_strict_resolution_requires_a_matching_capability_before_rearm(
        self, make_store, clock
    ) -> None:
        oo = OpenOnce(make_store(clock), clock=clock)
        eid = park_unknown_effect(oo)
        rec = Reconciler(
            oo.store,
            grace_seconds=0,
            require_capability_for_resolution=True,
            clock=clock,
        )
        rec.register("pay.charge", StubProber(ProbeOutcome.NOT_HAPPENED))

        clock.advance(1)
        report = rec.run_once()

        assert report.escalated == [eid]
        assert report.rearmed == []
        reviewed = oo.get(eid)
        assert reviewed.state == S.HUMAN_REVIEW
        assert "Strict resolution requires reviewed provider knowledge" in reviewed.note

    def test_conformance_gate_requires_passing_evidence_before_rearm(
        self, make_store, clock
    ) -> None:
        oo = OpenOnce(make_store(clock), clock=clock)
        eid = park_unknown_effect(oo, tool="stripe.charge")
        rec = Reconciler(
            oo.store,
            grace_seconds=0,
            require_conformance_for_resolution=True,
            clock=clock,
        )
        rec.register("stripe.charge", StubProber(ProbeOutcome.NOT_HAPPENED))

        clock.advance(1)
        report = rec.run_once()

        assert report.escalated == [eid]
        assert report.rearmed == []
        reviewed = oo.get(eid)
        assert reviewed.state == S.HUMAN_REVIEW
        assert "provider conformance is not proven" in reviewed.note

    def test_default_email_miss_still_rearms(self, make_store, clock) -> None:
        """Baseline invariant: with no pin and no strict flags, a NOT_HAPPENED
        probe re-arms — builtin capability patterns matching the tool name
        must not silently block deployed retries after an upgrade."""
        oo = OpenOnce(make_store(clock), clock=clock)
        eid = park_unknown_effect(oo, tool="email.send")
        rec = Reconciler(oo.store, grace_seconds=0, clock=clock)
        rec.register("email.send", StubProber(ProbeOutcome.NOT_HAPPENED))

        clock.advance(1)
        report = rec.run_once()
        assert report.rearmed == [eid]
        assert oo.get(eid).state == S.APPROVED

    def test_non_authoritative_pin_blocks_rearm_even_if_prober_says_not_happened(
        self, make_store, clock
    ) -> None:
        """Pinning the NON-authoritative capability is the operator declaring
        'my sent store can't prove absence' — then a miss must not re-arm."""
        oo = OpenOnce(make_store(clock), clock=clock)
        eid = park_unknown_effect(oo, tool="email.send")
        rec = Reconciler(oo.store, grace_seconds=0, clock=clock)
        rec.register("email.send", StubProber(ProbeOutcome.NOT_HAPPENED), capability="email_smtp")

        clock.advance(1)
        report = rec.run_once()
        assert report.escalated == [eid]
        assert report.rearmed == []
        reviewed = oo.get(eid)
        assert reviewed.state == S.HUMAN_REVIEW
        assert "not safe to auto-rearm" in reviewed.note
        assert "auto_rearm_on_miss=no" in reviewed.note

    def test_authoritative_capability_can_rearm_shared_tool_name(self, make_store, clock) -> None:
        oo = OpenOnce(make_store(clock), clock=clock)
        eid = park_unknown_effect(oo, tool="email.send")
        rec = Reconciler(oo.store, grace_seconds=0, clock=clock)
        rec.register(
            "email.send",
            StubProber(ProbeOutcome.NOT_HAPPENED),
            capability="email_provider_api",
        )

        clock.advance(1)
        report = rec.run_once()
        assert report.rearmed == [eid]
        assert report.escalated == []
        assert oo.get(eid).state == S.APPROVED

    def test_invalid_provider_capability_is_rejected(self, make_store, clock) -> None:
        oo = OpenOnce(make_store(clock), clock=clock)
        rec = Reconciler(oo.store, grace_seconds=0, clock=clock)

        with pytest.raises(ValueError, match="No provider capability 'nope'"):
            rec.register("email.send", StubProber(ProbeOutcome.HAPPENED), capability="nope")

    def test_custom_capability_can_rearm_unknown_builtin_tool(self, make_store, clock) -> None:
        oo = OpenOnce(make_store(clock), clock=clock)
        eid = park_unknown_effect(oo, tool="slack.post_message")
        rec = Reconciler(
            oo.store,
            extra_capabilities=(SLACK_CAPABILITY,),
            require_capability_for_resolution=True,
            grace_seconds=0,
            clock=clock,
        )
        rec.register(
            "slack.post_message",
            StubProber(ProbeOutcome.NOT_HAPPENED),
            capability="slack_metadata",
        )

        clock.advance(1)
        report = rec.run_once()
        assert report.rearmed == [eid]
        assert oo.get(eid).state == S.APPROVED

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

    def test_no_prober_note_includes_builtin_provider_guidance(self, make_store, clock) -> None:
        oo = OpenOnce(make_store(clock), clock=clock)
        eid = park_unknown_effect(oo, tool="github.create_pr")
        rec = Reconciler(oo.store, grace_seconds=0, clock=clock)

        clock.advance(1)
        assert rec.run_once().escalated == [eid]
        note = oo.get(eid).note
        assert "GitHubPullRequestProber" in note
        assert "auto_rearm_on_miss=yes" in note
        assert "handler_args=owner,repo,head" in note
        assert "idempotency_fields=owner,repo,head" in note
        assert "receipt_fields=number,head" in note

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
        key, fp = rt._key_and_fingerprint(
            "pay.charge",
            {"amount_cents": 500},
            scope="run1",
            idempotency_key=None,
            idempotency_fields=None,
        )
        record, created = rt._admit(
            tool="pay.charge",
            args={"amount_cents": 500},
            scope="run1",
            key=key,
            fp=fp,
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
        key, fp = rt._key_and_fingerprint(
            "t", {}, scope="r", idempotency_key=None, idempotency_fields=None
        )
        record, _ = rt._admit(tool="t", args={}, scope="r", key=key, fp=fp, max_attempts=3)
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


class TestRegisterPreservesPins:
    """Review finding: register() with no capability argument must neither
    delete a constructor-supplied pin nor mutate the caller's dict."""

    def test_baseline_register_keeps_constructor_pin(self, make_store, clock) -> None:
        oo = OpenOnce(make_store(clock), clock=clock)
        eid = park_unknown_effect(oo, tool="email.send")
        caller_pins = {"email.send": "email_provider_api"}
        rec = Reconciler(oo.store, grace_seconds=0, provider_capabilities=caller_pins, clock=clock)
        rec.register("email.send", StubProber(ProbeOutcome.NOT_HAPPENED))  # old signature

        assert rec.provider_capabilities == {"email.send": "email_provider_api"}  # pin intact
        assert caller_pins == {"email.send": "email_provider_api"}  # caller dict untouched
        assert rec.provider_capabilities is not caller_pins  # no aliasing

        clock.advance(1)
        # The authoritative pin the operator configured stays in force:
        # NOT_HAPPENED re-arms as they intended.
        report = rec.run_once()
        assert report.rearmed == [eid]

    def test_explicit_capability_still_pins(self, make_store, clock) -> None:
        oo = OpenOnce(make_store(clock), clock=clock)
        rec = Reconciler(oo.store, grace_seconds=0, clock=clock)
        rec.register("email.send", StubProber(ProbeOutcome.NOT_HAPPENED), capability="email_smtp")
        assert rec.provider_capabilities == {"email.send": "email_smtp"}
