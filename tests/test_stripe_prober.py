"""StripeProber tests: metadata hit, indexing-lag caution, duplicate anomaly."""

from __future__ import annotations

import json

from openonce.providers.base import ProbeOutcome
from openonce.providers.stripe import METADATA_KEY, StripeProber, effect_metadata
from openonce.records import EffectRecord
from openonce.runtime import EffectContext
from openonce.state import EffectState


def make_record(clock, effect_id: str = "eff_1") -> EffectRecord:
    return EffectRecord(
        effect_id=effect_id,
        idempotency_key="oo1_k",
        tool="stripe.charge",
        state=EffectState.UNKNOWN,
        args_fingerprint="fp",
        args_json=json.dumps({"amount": 500}),
        scope="run1",
        provider_key=f"{effect_id}:stripe.charge",
        attempt=1,
        created_at=clock.now,
        updated_at=clock.now,
    )


class RecordingSearch:
    def __init__(self, hits: list[dict]) -> None:
        self.hits = hits
        self.queries: list[tuple[str, str]] = []

    def __call__(self, resource: str, query: str) -> list[dict]:
        self.queries.append((resource, query))
        return self.hits


class TestStripeProber:
    def test_metadata_hit_is_happened_with_receipt(self, clock) -> None:
        search = RecordingSearch([{"id": "pi_9", "status": "succeeded", "amount": 500}])
        prober = StripeProber(search, clock=clock)
        result = prober.probe(make_record(clock))

        assert result.outcome is ProbeOutcome.HAPPENED
        assert result.receipt == {
            "stripe_id": "pi_9",
            "status": "succeeded",
            "amount": 500,
            "resource": "payment_intents",
        }
        # The query targets our metadata key and the exact effect_id.
        resource, query = search.queries[0]
        assert resource == "payment_intents"
        assert query == f'metadata["{METADATA_KEY}"]:"eff_1"'

    def test_miss_inside_indexing_lag_is_inconclusive(self, clock) -> None:
        prober = StripeProber(RecordingSearch([]), indexing_lag_seconds=120, clock=clock)
        record = make_record(clock)
        clock.advance(30)  # young: search index may simply not have it yet
        result = prober.probe(record)
        assert result.outcome is ProbeOutcome.INCONCLUSIVE
        assert "indexing lag" in result.detail

    def test_approval_delay_does_not_eat_the_indexing_window(self, clock) -> None:
        """Review finding #2: an effect that sat in approval for hours, then
        executed and crashed seconds ago, is still inside the indexing window.
        Age must be measured from updated_at (UNKNOWN parking), not created_at."""
        prober = StripeProber(RecordingSearch([]), indexing_lag_seconds=120, clock=clock)
        record = make_record(clock)  # created now...
        clock.advance(7200)  # ...approved 2h later, executed, crashed:
        record = record.with_(updated_at=clock.now)  # parked UNKNOWN just now
        clock.advance(30)  # probe 30s after the send attempt
        result = prober.probe(record)
        assert result.outcome is ProbeOutcome.INCONCLUSIVE  # NOT a false NOT_HAPPENED

    def test_miss_past_indexing_lag_is_not_happened(self, clock) -> None:
        prober = StripeProber(RecordingSearch([]), indexing_lag_seconds=120, clock=clock)
        record = make_record(clock)
        clock.advance(600)
        result = prober.probe(record)
        assert result.outcome is ProbeOutcome.NOT_HAPPENED
        assert "provider_key" in result.detail  # documents the 24h dedup backstop

    def test_multiple_hits_escalate(self, clock) -> None:
        search = RecordingSearch([{"id": "pi_1"}, {"id": "pi_2"}])
        prober = StripeProber(search, clock=clock)
        result = prober.probe(make_record(clock))
        assert result.outcome is ProbeOutcome.INCONCLUSIVE
        assert "expected 1" in result.detail

    def test_effect_metadata_helper(self) -> None:
        ctx = EffectContext(
            effect_id="eff_7", idempotency_key="k", provider_key="eff_7:t", attempt=1
        )
        assert effect_metadata(ctx) == {METADATA_KEY: "eff_7"}
