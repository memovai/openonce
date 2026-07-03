"""Reconciler: drives UNKNOWN outcomes to the truth.

Design (see PLAN.md §3, lineage: rocket-rides-atomic's completer):

- Runs out-of-band, never on the hot path.
- Grace period first: effects younger than ``grace_seconds`` are left alone —
  the original caller gets first shot at retrying/resolving.
- STARTED with an expired lease -> UNKNOWN (the executor died mid-call).
- UNKNOWN is resolved by *probing the external world*, per tool:
      HAPPENED       -> RECEIPT_RECORDED -> COMMITTED (with the probe's receipt)
      NOT_HAPPENED   -> APPROVED/FAILED only if the provider capability allows it
      INCONCLUSIVE   -> HUMAN_REVIEW
- No prober registered for the tool -> HUMAN_REVIEW. Never a blind retry.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field

from .providers.base import ProbeOutcome, Prober
from .providers.capabilities import (
    ProviderCapability,
    can_auto_rearm_on_miss,
    capabilities_for_tool,
    capability_guidance_for_tool,
    capability_matrix,
    default_grace_for_tool,
    minimum_grace_seconds,
    provider_receipt_contract_failures,
)
from .providers.conformance import (
    CapabilityConformanceEvidence,
    ConformanceReport,
    validate_conformance_evidence,
)
from .records import EffectRecord, EffectResult
from .state import EffectState as S
from .store.base import Store

DEFAULT_GRACE_SECONDS = 300.0


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
        extra_capabilities: tuple[ProviderCapability, ...] | None = None,
        provider_capabilities: dict[str, str] | None = None,
        require_capability_for_resolution: bool = False,
        conformance_evidence: Mapping[str, CapabilityConformanceEvidence] | None = None,
        require_conformance_for_resolution: bool = False,
        grace_seconds: float | None = None,
        grace_overrides: dict[str, float] | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.store = store
        self.probers = probers or {}
        self.capabilities = capability_matrix(extra_capabilities or ())
        # Copy: this dict is mutated by register(); never alias caller state.
        self.provider_capabilities = dict(provider_capabilities or {})
        for tool, capability in self.provider_capabilities.items():
            self._validate_provider_capability(tool, capability)
        self.require_capability_for_resolution = require_capability_for_resolution
        self.conformance_evidence = conformance_evidence or {}
        self.require_conformance_for_resolution = require_conformance_for_resolution
        self.grace_seconds = DEFAULT_GRACE_SECONDS if grace_seconds is None else grace_seconds
        self._use_capability_grace = grace_seconds is None
        self.grace_overrides = grace_overrides or {}
        self.clock = clock

    def register(self, tool: str, prober: Prober, *, capability: str | None = None) -> None:
        """Register a prober; optionally pin the provider capability.

        ``capability=None`` means "no opinion": a pin supplied via the
        constructor's ``provider_capabilities`` is left intact — never
        silently deleted by a baseline-signature call.
        """
        if capability is not None:
            self._validate_provider_capability(tool, capability)
            self.provider_capabilities[tool] = capability
        self.probers[tool] = prober

    # ------------------------------------------------------------------ #

    def run_once(self) -> ReconcileReport:
        report = ReconcileReport()
        now = self.clock()
        # Scan with the smallest configured grace; exact per-tool grace is
        # applied per record in _past_grace.
        max_grace_cutoff = now - self._scan_grace_floor()

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
        capability = self.provider_capabilities.get(rec.tool)
        if prober is None:
            guidance = capability_guidance_for_tool(
                rec.tool, capability, capabilities=self.capabilities
            )
            self._escalate(
                rec,
                report,
                detail=f"no prober registered for {rec.tool!r}. {guidance}",
            )
            return

        try:
            result = prober.probe(rec)
        except Exception as exc:  # a broken prober must not crash the loop
            self._escalate(rec, report, detail=f"prober raised: {exc!r}")
            return

        # Capability enforcement applies only when the operator opted in:
        # an explicit pin for this tool, or one of the strict resolution
        # flags. With none of them, the baseline contract holds — probers are
        # trusted as registered, NOT_HAPPENED re-arms, HAPPENED commits.
        # Builtin capability *patterns* matching a tool name must never
        # silently change deployed behavior on upgrade.
        enforce = (
            capability is not None
            or self.require_capability_for_resolution
            or self.require_conformance_for_resolution
        )

        capability_matches = capabilities_for_tool(
            rec.tool, capability, capabilities=self.capabilities
        )
        if enforce and result.outcome in (ProbeOutcome.HAPPENED, ProbeOutcome.NOT_HAPPENED):
            blocker = self._definitive_resolution_blocker(
                rec.tool,
                capability,
                capability_matches,
                result.outcome,
            )
            if blocker is not None:
                self._escalate(rec, report, detail=blocker)
                return

        if result.outcome is ProbeOutcome.HAPPENED:
            if enforce and not result.receipt:
                self._escalate(
                    rec,
                    report,
                    detail=(
                        f"probe said happened for {rec.tool!r}, but did not return "
                        "a receipt with external evidence"
                    ),
                )
                return
            if enforce:
                receipt_contract_failures = provider_receipt_contract_failures(
                    rec.tool,
                    rec.args(),
                    result.receipt or {},
                    capability,
                    capabilities=self.capabilities,
                )
                if receipt_contract_failures:
                    detail = _receipt_contract_detail(rec.tool, receipt_contract_failures)
                    self._escalate(rec, report, detail=detail)
                    return
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
            if enforce and not can_auto_rearm_on_miss(
                rec.tool, capability, capabilities=self.capabilities
            ):
                guidance = capability_guidance_for_tool(
                    rec.tool, capability, capabilities=self.capabilities
                )
                self._escalate(
                    rec,
                    report,
                    detail=(
                        f"probe said not_happened, but {rec.tool!r} is not safe "
                        f"to auto-rearm on a miss. {guidance}"
                    ),
                )
                return
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
                    set_fields={"result": None},
                    payload={"probe": "not_happened", "detail": result.detail},
                )
                if rearmed is not None:
                    report.rearmed.append(rec.effect_id)
        else:
            self._escalate(rec, report, detail=result.detail or "probe inconclusive")

    def _definitive_resolution_blocker(
        self,
        tool: str,
        capability: str | None,
        capability_matches: tuple[ProviderCapability, ...],
        outcome: ProbeOutcome,
    ) -> str | None:
        if (
            self.require_capability_for_resolution or self.require_conformance_for_resolution
        ) and not capability_matches:
            guidance = capability_guidance_for_tool(
                tool, capability, capabilities=self.capabilities
            )
            extra = (
                " Conformance-gated resolution also requires passing provider conformance evidence."
                if self.require_conformance_for_resolution
                else ""
            )
            return (
                f"probe said {outcome.value}, but {tool!r} has no matching "
                f"provider capability. Strict resolution requires reviewed provider "
                f"knowledge.{extra} {guidance}"
            )

        if not self.require_conformance_for_resolution:
            return None

        failures = _conformance_failure_reasons(
            validate_conformance_evidence(
                self.capabilities,
                self.conformance_evidence,
                required_capabilities=tuple(cap.name for cap in capability_matches),
            )
        )
        if not failures:
            return None
        return (
            f"probe said {outcome.value}, but provider conformance is not proven "
            f"for {tool!r}: {failures[0]}"
        )

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
        grace = self._grace_for_tool(rec.tool)
        return rec.updated_at <= now - grace

    def _grace_for_tool(self, tool: str) -> float:
        override = self.grace_overrides.get(tool)
        if override is not None:
            return override
        if self._use_capability_grace:
            capability_grace = default_grace_for_tool(
                tool,
                self.provider_capabilities.get(tool),
                capabilities=self.capabilities,
            )
            if capability_grace is not None:
                return capability_grace
        return self.grace_seconds

    def _scan_grace_floor(self) -> float:
        values = [self.grace_seconds, *self.grace_overrides.values()]
        if self._use_capability_grace:
            values.append(minimum_grace_seconds(self.capabilities))
        return min(values)

    def _validate_provider_capability(self, tool: str, capability: str) -> None:
        if capabilities_for_tool(tool, capability, capabilities=self.capabilities):
            return
        raise ValueError(
            capability_guidance_for_tool(tool, capability, capabilities=self.capabilities)
        )


def _conformance_failure_reasons(reports: tuple[ConformanceReport, ...]) -> tuple[str, ...]:
    return tuple(
        f"{report.capability}.{failure.scenario}: {failure.reason}"
        for report in reports
        for failure in report.failures
    )


def _receipt_contract_detail(tool: str, failures: tuple[str, ...]) -> str:
    missing = next(
        (
            failure
            for failure in failures
            if failure.startswith("missing required external evidence field(s):")
        ),
        None,
    )
    if missing is not None:
        return f"probe said happened for {tool!r}, but the receipt is {missing}"

    source_failures = tuple(
        failure
        for failure in failures
        if " expected " in failure or "requires missing handler arg" in failure
    )
    if source_failures:
        return (
            f"probe said happened for {tool!r}, but the receipt does not satisfy "
            f"required source contract(s): {'; '.join(source_failures)}"
        )

    return (
        f"probe said happened for {tool!r}, but the receipt does not satisfy "
        f"required provider contract: {'; '.join(failures)}"
    )
