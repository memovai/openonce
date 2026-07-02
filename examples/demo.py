"""OpenOnce end-to-end demo: the crash-retry-reconcile story in 90 lines.

Run:  uv run python examples/demo.py
"""

from __future__ import annotations

import openonce
from openonce import ProbeOutcome, ProbeResult, Reconciler, require_approval_for
from openonce.records import EffectRecord

# --- a fake payment provider (the "external world") -------------------------

PROVIDER_LEDGER: dict[str, dict] = {}  # provider_key -> charge object
FAIL_MODE: list[str] = []  # mutate to inject failures


def provider_charge(provider_key: str, amount_cents: int) -> dict:
    """Simulates a payment API that supports idempotency keys."""
    if provider_key in PROVIDER_LEDGER:
        return PROVIDER_LEDGER[provider_key]  # provider-side dedup
    charge = {"charge_id": f"ch_{len(PROVIDER_LEDGER) + 1}", "amount": amount_cents}
    PROVIDER_LEDGER[provider_key] = charge
    if "timeout_after_send" in FAIL_MODE:
        raise TimeoutError("read timed out — but the charge WAS created")
    return charge


class ChargeProber:
    """Resolves UNKNOWN outcomes by asking the provider what actually happened."""

    def probe(self, record: EffectRecord) -> ProbeResult:
        found = PROVIDER_LEDGER.get(record.provider_key)
        if found is not None:
            return ProbeResult(ProbeOutcome.HAPPENED, receipt=found)
        return ProbeResult(ProbeOutcome.NOT_HAPPENED)


# --- wire up OpenOnce ---------------------------------------------------------

oo = openonce.OpenOnce(policy=require_approval_for(["pay.refund"]))


@oo.effect(tool="pay.charge", idempotency_fields=["customer", "amount_cents"])
def charge(customer: str, amount_cents: int, memo: str) -> dict:
    ctx = openonce.current_effect()
    assert ctx is not None
    return provider_charge(ctx.provider_key, amount_cents)


# --- 1) dedup: the LLM retries with reworded noise ---------------------------

with oo.scope("run-1"):
    a = charge(customer="cus_1", amount_cents=500, memo="coffee")
    b = charge(customer="cus_1", amount_cents=500, memo="coffee (retried, reworded)")
assert a == b and len(PROVIDER_LEDGER) == 1
print(f"1) dedup: two calls, one charge -> {a}")

# --- 2) ambiguous timeout: parked as UNKNOWN, then reconciled ----------------

FAIL_MODE.append("timeout_after_send")
with oo.scope("run-2"):
    try:
        charge(customer="cus_2", amount_cents=900, memo="lunch")
    except openonce.EffectUnknown as e:
        print(f"2) timeout mid-charge: parked {e.record.effect_id} as UNKNOWN (no blind retry)")
FAIL_MODE.clear()

rec = Reconciler(oo.store, grace_seconds=0)
rec.register("pay.charge", ChargeProber())
report = rec.run_once()
print(f"   reconciler probed the provider: committed={report.committed}")

with oo.scope("run-2"):  # the agent retries after recovery -> replayed receipt
    again = charge(customer="cus_2", amount_cents=900, memo="lunch")
assert len(PROVIDER_LEDGER) == 2  # still exactly one charge for cus_2
print(f"   retry after recovery replays the receipt -> {again}")

# --- 3) approval gate ---------------------------------------------------------


@oo.effect(tool="pay.refund")
def refund(charge_id: str) -> str:
    return f"refunded {charge_id}"


with oo.scope("run-3"):
    try:
        refund(charge_id="ch_1")
    except openonce.ApprovalPending as p:
        print(f"3) refund blocked pending approval: {p.effect_id}")
        oo.approve(p.effect_id, by="eric")
    print(f"   after approval, same call proceeds: {refund(charge_id='ch_1')!r}")

# --- 4) the receipts ----------------------------------------------------------

eid = report.committed[0]
print(f"4) audit trail for {eid}:")
for entry in oo.journal(eid):
    print(f"   {entry.from_state or '∅':>18} -> {entry.to_state:<18} {entry.payload or ''}")
