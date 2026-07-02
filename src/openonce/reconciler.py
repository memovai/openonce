"""Reconciler: drives UNKNOWN outcomes to the truth.

Design (see PLAN.md §3, lineage: rocket-rides-atomic's completer):

- Runs out-of-band, never on the hot path.
- Grace period first: effects younger than ``grace_seconds`` are left alone —
  the original caller gets first shot at retrying/resolving.
- STARTED with an expired lease -> UNKNOWN (the executor died mid-call).
- UNKNOWN is resolved by *probing the external world*, per tool:
      HAPPENED       -> RECEIPT_RECORDED -> COMMITTED (with the probe's receipt)
      NOT_HAPPENED   -> APPROVED (re-eligible) or FAILED if attempts exhausted
      INCONCLUSIVE   -> HUMAN_REVIEW
- No prober registered for the tool -> HUMAN_REVIEW. Never a blind retry.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field

from .providers.base import ProbeOutcome, Prober
from .records import EffectRecord, EffectResult
from .state import EffectState as S
from .store.base import Store


@dataclass
class ReconcileReport:
    committed: list[str] = field(default_factory=list)
    rearmed: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)
    escalated: list[str] = field(default_factory=list)

    def total(self) -> int:
        return len(self.committed) + len(self.rearmed) + len(self.failed) + len(self.escalated)


class Reconciler:
    def __init__(
        self,
        store: Store,
        *,
        probers: dict[str, Prober] | None = None,
        grace_seconds: float = 300.0,
        grace_overrides: dict[str, float] | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.store = store
        self.probers = probers or {}
        self.grace_seconds = grace_seconds
        self.grace_overrides = grace_overrides or {}
        self.clock = clock

    def register(self, tool: str, prober: Prober) -> None:
        self.probers[tool] = prober

    # ------------------------------------------------------------------ #

    def run_once(self) -> ReconcileReport:
        report = ReconcileReport()
        now = self.clock()
        # Scan with the smallest configured grace; exact per-tool grace is
        # applied per record in _past_grace.
        max_grace_cutoff = now - min([self.grace_seconds, *self.grace_overrides.values()])

        # 1) Dead executors: STARTED past its lease -> UNKNOWN.
        for rec in self.store.scan_states({S.STARTED}, updated_before=max_grace_cutoff):
            if not self._past_grace(rec, now):
                continue
            if rec.lease_expires_at is not None and rec.lease_expires_at > now:
                continue  # executor is alive; leave it alone
            self.store.transition(
                rec.effect_id,
                {S.STARTED},
                S.UNKNOWN,
                payload={"reason": "reconciler: lease expired mid-flight"},
            )

        # 2) Resolve UNKNOWN against the external world.
        for rec in self.store.scan_states({S.UNKNOWN}, updated_before=max_grace_cutoff):
            if not self._past_grace(rec, now):
                continue
            self._resolve(rec, report)
        return report

    # ------------------------------------------------------------------ #

    def _resolve(self, rec: EffectRecord, report: ReconcileReport) -> None:
        prober = self.probers.get(rec.tool)
        if prober is None:
            self._escalate(rec, report, detail=f"no prober registered for {rec.tool!r}")
            return

        try:
            result = prober.probe(rec)
        except Exception as exc:  # a broken prober must not crash the loop
            self._escalate(rec, report, detail=f"prober raised: {exc!r}")
            return

        if result.outcome is ProbeOutcome.HAPPENED:
            receipted = self.store.transition(
                rec.effect_id,
                {S.UNKNOWN},
                S.RECEIPT_RECORDED,
                set_fields={"result": EffectResult(ok=True, value=result.receipt)},
                payload={"probe": "happened", "receipt": result.receipt or {}},
            )
            if receipted is not None:
                self.store.transition(rec.effect_id, {S.RECEIPT_RECORDED}, S.COMMITTED, payload={})
                report.committed.append(rec.effect_id)
        elif result.outcome is ProbeOutcome.NOT_HAPPENED:
            if rec.attempt >= rec.max_attempts:
                done = self.store.transition(
                    rec.effect_id,
                    {S.UNKNOWN},
                    S.FAILED,
                    set_fields={
                        "result": EffectResult(
                            ok=False,
                            error="did not happen; attempts exhausted",
                            error_type="AttemptsExhausted",
                        )
                    },
                    payload={"probe": "not_happened", "detail": result.detail},
                )
                if done is not None:
                    report.failed.append(rec.effect_id)
            else:
                rearmed = self.store.transition(
                    rec.effect_id,
                    {S.UNKNOWN},
                    S.APPROVED,
                    payload={"probe": "not_happened", "detail": result.detail},
                )
                if rearmed is not None:
                    report.rearmed.append(rec.effect_id)
        else:
            self._escalate(rec, report, detail=result.detail or "probe inconclusive")

    def _escalate(self, rec: EffectRecord, report: ReconcileReport, *, detail: str) -> None:
        moved = self.store.transition(
            rec.effect_id,
            {S.UNKNOWN},
            S.HUMAN_REVIEW,
            set_fields={"note": detail},
            payload={"escalated": detail},
        )
        if moved is not None:
            report.escalated.append(rec.effect_id)

    def _past_grace(self, rec: EffectRecord, now: float) -> bool:
        grace = self.grace_overrides.get(rec.tool, self.grace_seconds)
        return rec.updated_at <= now - grace
