"""EmailMessageIdProber tests: deterministic key, tier-honest miss handling."""

from __future__ import annotations

import json

from openonce.providers.base import ProbeOutcome
from openonce.providers.email import (
    EmailMessageIdProber,
    make_message_id,
    message_id_for_record,
)
from openonce.records import EffectRecord
from openonce.runtime import EffectContext
from openonce.state import EffectState


def make_record(clock) -> EffectRecord:
    return EffectRecord(
        effect_id="eff_9",
        idempotency_key="oo1_k",
        tool="email.send",
        state=EffectState.UNKNOWN,
        args_fingerprint="fp",
        args_json=json.dumps({"to": "a@b.c", "subject": "hi"}),
        scope="run1",
        provider_key="eff_9:email.send",
        created_at=clock.now,
        updated_at=clock.now,
    )


class TestMessageId:
    def test_handler_and_prober_derive_the_same_id(self, clock) -> None:
        ctx = EffectContext(
            effect_id="eff_9", idempotency_key="k", provider_key="eff_9:email.send", attempt=1
        )
        assert make_message_id(ctx) == message_id_for_record(make_record(clock))
        assert make_message_id(ctx) == "<eff_9@openonce.local>"


class TestEmailProber:
    def test_found_is_happened(self, clock) -> None:
        prober = EmailMessageIdProber(lambda mid: {"uids": "42"}, clock=clock)
        result = prober.probe(make_record(clock))
        assert result.outcome is ProbeOutcome.HAPPENED
        assert result.receipt["message_id"] == "<eff_9@openonce.local>"

    def test_young_miss_is_inconclusive(self, clock) -> None:
        prober = EmailMessageIdProber(
            lambda mid: None, sent_store_is_authoritative=True, clock=clock
        )
        record = make_record(clock)
        clock.advance(10)
        assert prober.probe(record).outcome is ProbeOutcome.INCONCLUSIVE

    def test_authoritative_store_miss_is_not_happened(self, clock) -> None:
        prober = EmailMessageIdProber(
            lambda mid: None, sent_store_is_authoritative=True, clock=clock
        )
        record = make_record(clock)
        clock.advance(600)
        result = prober.probe(record)
        assert result.outcome is ProbeOutcome.NOT_HAPPENED

    def test_approval_delay_does_not_cause_false_not_happened(self, clock) -> None:
        """The authoritative path has no provider-key backstop, so a false
        NOT_HAPPENED here means a duplicate email. Age from updated_at."""
        prober = EmailMessageIdProber(
            lambda mid: None, sent_store_is_authoritative=True, clock=clock
        )
        record = make_record(clock)
        clock.advance(7200)  # long approval delay
        record = record.with_(updated_at=clock.now)  # send attempt was just now
        clock.advance(10)
        assert prober.probe(record).outcome is ProbeOutcome.INCONCLUSIVE

    def test_bare_smtp_miss_stays_inconclusive_forever(self, clock) -> None:
        """Tier-3 honesty: without an authoritative sent store, a miss can
        never prove the mail wasn't delivered — a human decides."""
        prober = EmailMessageIdProber(lambda mid: None, clock=clock)
        record = make_record(clock)
        clock.advance(10_000)
        result = prober.probe(record)
        assert result.outcome is ProbeOutcome.INCONCLUSIVE
        assert "human" in result.detail
