"""Provider prober conformance checks.

Capability metadata says what a provider miss means; conformance evidence
proves the prober actually behaves that way under standard scenarios.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from .base import ProbeOutcome, ProbeResult
from .capabilities import ProviderCapability, capability_fingerprint


class ConformanceScenario(StrEnum):
    HAPPENED = "happened"
    YOUNG_MISS = "young_miss"
    MATURE_MISS = "mature_miss"
    AMBIGUOUS = "ambiguous"


@dataclass(frozen=True)
class ConformanceCase:
    scenario: ConformanceScenario
    expected_outcomes: tuple[ProbeOutcome, ...]
    description: str
    fixture_guidance: str
    receipt_required: bool = False
    required_receipt_fields: tuple[str, ...] = ()
    required_receipt_source_fields: tuple[tuple[str, str], ...] = ()

    def to_dict(self) -> dict[str, object]:
        return {
            "scenario": self.scenario.value,
            "expected_outcomes": [outcome.value for outcome in self.expected_outcomes],
            "description": self.description,
            "fixture_guidance": self.fixture_guidance,
            "receipt_required": self.receipt_required,
            "required_receipt_fields": list(self.required_receipt_fields),
            "required_receipt_source_fields": {
                receipt_field: source_field
                for receipt_field, source_field in self.required_receipt_source_fields
            },
        }


@dataclass(frozen=True)
class ConformanceFailure:
    scenario: str
    expected: tuple[str, ...]
    actual: str | None
    reason: str

    def to_dict(self) -> dict[str, object]:
        return {
            "scenario": self.scenario,
            "expected": list(self.expected),
            "actual": self.actual,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class ConformanceReport:
    capability: str
    required_scenarios: tuple[str, ...]
    observed_scenarios: tuple[str, ...]
    failures: tuple[ConformanceFailure, ...]

    @property
    def passed(self) -> bool:
        return not self.failures

    def to_dict(self) -> dict[str, object]:
        return {
            "capability": self.capability,
            "passed": self.passed,
            "required_scenarios": list(self.required_scenarios),
            "observed_scenarios": list(self.observed_scenarios),
            "failures": [failure.to_dict() for failure in self.failures],
        }


@dataclass(frozen=True)
class CapabilityConformanceEvidence:
    capability: str
    observations: Mapping[str, ProbeResult]
    capability_fingerprint: str | None = None
    source_args: Mapping[str, object] | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "capability": self.capability,
            "capability_fingerprint": self.capability_fingerprint,
            "source_args": self.source_args,
            "observations": {
                scenario: {
                    "outcome": result.outcome.value,
                    "receipt": result.receipt,
                    "detail": result.detail,
                }
                for scenario, result in self.observations.items()
            },
        }


def conformance_plan(capability: ProviderCapability) -> tuple[ConformanceCase, ...]:
    """Standard scenarios a prober should prove for ``capability``.

    The plan intentionally describes fixture behavior instead of constructing
    fixtures. Stripe, GitHub, email, and team-owned providers need different
    fake provider clients, but their safety semantics are checked the same way.
    """
    cases = [
        ConformanceCase(
            scenario=ConformanceScenario.HAPPENED,
            expected_outcomes=(ProbeOutcome.HAPPENED,),
            description="provider read finds exactly this effect",
            fixture_guidance=(
                "configure the fake provider read to return one object carrying "
                "the capability key for this effect"
            ),
            receipt_required=True,
            required_receipt_fields=capability.required_receipt_fields,
            required_receipt_source_fields=capability.required_receipt_source_fields,
        )
    ]
    if capability.default_grace_seconds > 0:
        cases.append(
            ConformanceCase(
                scenario=ConformanceScenario.YOUNG_MISS,
                expected_outcomes=(ProbeOutcome.INCONCLUSIVE,),
                description="provider read misses before the grace/propagation window closes",
                fixture_guidance=(
                    "configure a miss while record.updated_at age is less than "
                    f"{capability.default_grace_seconds:.0f}s"
                ),
            )
        )

    mature_expected = (
        ProbeOutcome.NOT_HAPPENED
        if capability.can_auto_rearm_on_miss
        else ProbeOutcome.INCONCLUSIVE
    )
    mature_reason = (
        "an authoritative miss may re-arm"
        if capability.can_auto_rearm_on_miss
        else "a non-authoritative miss must stay inconclusive"
    )
    cases.append(
        ConformanceCase(
            scenario=ConformanceScenario.MATURE_MISS,
            expected_outcomes=(mature_expected,),
            description=f"provider read misses after grace; {mature_reason}",
            fixture_guidance=(
                "configure a miss while record.updated_at age is past the "
                f"{capability.default_grace_seconds:.0f}s default grace"
            ),
        )
    )
    cases.append(
        ConformanceCase(
            scenario=ConformanceScenario.AMBIGUOUS,
            expected_outcomes=(ProbeOutcome.INCONCLUSIVE,),
            description="provider read is ambiguous, duplicated, incomplete, or missing key fields",
            fixture_guidance=(
                "configure duplicate matches, insufficient natural-key fields, "
                "or another false-positive/false-negative risk"
            ),
        )
    )
    return tuple(cases)


def conformance_evidence_template(capability: ProviderCapability) -> dict[str, object]:
    """Fill-in evidence scaffold for the current capability contract."""
    return {
        "capability_fingerprint": capability_fingerprint(capability),
        "source_args": {
            source_field: "TODO: value used by the conformance fixture"
            for source_field in capability.required_args
        },
        "scenarios": {
            case.scenario.value: {
                "expected_outcomes": [outcome.value for outcome in case.expected_outcomes],
                "receipt_required": case.receipt_required,
                "required_receipt_fields": list(case.required_receipt_fields),
                "required_receipt_source_fields": {
                    receipt_field: source_field
                    for receipt_field, source_field in case.required_receipt_source_fields
                },
                "fixture_guidance": case.fixture_guidance,
                "outcome": None,
                "receipt": None,
                "detail": "TODO: record how this scenario was exercised",
            }
            for case in conformance_plan(capability)
        },
    }


def conformance_evidence_file_template(
    capabilities: Iterable[ProviderCapability],
) -> dict[str, object]:
    """Fill-in ``openonce-conformance.json`` scaffold for capability evidence."""
    return {
        "schema_version": 1,
        "capabilities": {
            capability.name: conformance_evidence_template(capability)
            for capability in capabilities
        },
    }


def validate_conformance(
    capability: ProviderCapability,
    observations: Mapping[ConformanceScenario | str, ProbeResult],
    *,
    source_args: Mapping[str, object] | None = None,
) -> ConformanceReport:
    """Check observed prober results against the standard capability plan."""
    plan = {case.scenario: case for case in conformance_plan(capability)}
    normalized: dict[ConformanceScenario, ProbeResult] = {}
    failures: list[ConformanceFailure] = []

    for raw_scenario, result in observations.items():
        try:
            scenario = _parse_scenario(raw_scenario)
        except ValueError as exc:
            failures.append(
                ConformanceFailure(
                    scenario=str(raw_scenario),
                    expected=(),
                    actual=result.outcome.value,
                    reason=str(exc),
                )
            )
            continue
        case = plan.get(scenario)
        if case is None:
            failures.append(
                ConformanceFailure(
                    scenario=scenario.value,
                    expected=(),
                    actual=result.outcome.value,
                    reason="scenario is not required for this capability",
                )
            )
            continue
        normalized[scenario] = result
        _validate_case(case, result, failures, source_args=source_args)

    for scenario, case in plan.items():
        if scenario not in normalized:
            failures.append(
                ConformanceFailure(
                    scenario=scenario.value,
                    expected=tuple(outcome.value for outcome in case.expected_outcomes),
                    actual=None,
                    reason="missing conformance observation",
                )
            )

    failures.extend(_source_arg_failures(capability, source_args))

    return ConformanceReport(
        capability=capability.name,
        required_scenarios=tuple(case.scenario.value for case in plan.values()),
        observed_scenarios=tuple(scenario.value for scenario in normalized),
        failures=tuple(failures),
    )


def assert_conformance(
    capability: ProviderCapability,
    observations: Mapping[ConformanceScenario | str, ProbeResult],
    *,
    source_args: Mapping[str, object] | None = None,
) -> None:
    """Raise ``AssertionError`` if observed prober behavior violates the plan."""
    report = validate_conformance(capability, observations, source_args=source_args)
    if report.passed:
        return
    details = "; ".join(f"{failure.scenario}: {failure.reason}" for failure in report.failures)
    raise AssertionError(f"provider capability {capability.name!r} failed conformance: {details}")


def load_conformance_file(path: str | Path) -> dict[str, CapabilityConformanceEvidence]:
    """Load provider conformance evidence from JSON.

    Shape:
        {
          "schema_version": 1,
          "capabilities": {
            "stripe": {
              "capability_fingerprint": "sha256:...",
              "scenarios": {
                "happened": {
                  "outcome": "happened",
                  "receipt": {...},
                  "detail": "fixture returned exactly one matching provider object"
                },
                "young_miss": {
                  "outcome": "inconclusive",
                  "detail": "fixture returned a miss inside the provider grace window"
                },
                "mature_miss": {
                  "outcome": "not_happened",
                  "detail": "fixture returned an authoritative miss after grace"
                },
                "ambiguous": {
                  "outcome": "inconclusive",
                  "detail": "fixture returned duplicate or incomplete matches"
                }
              }
            }
          }
        }
    """
    file_path = Path(path)
    try:
        raw = json.loads(file_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"{file_path}: invalid JSON: {exc.msg}") from exc

    if not isinstance(raw, dict):
        raise ValueError(f"{file_path}: expected a JSON object")
    if raw.get("schema_version") != 1:
        raise ValueError(f"{file_path}: schema_version must be 1")
    capabilities = raw.get("capabilities")
    if not isinstance(capabilities, dict):
        raise ValueError(f"{file_path}: capabilities must be an object")

    parsed: dict[str, CapabilityConformanceEvidence] = {}
    for capability, raw_evidence in capabilities.items():
        if not isinstance(capability, str) or not capability:
            raise ValueError(f"{file_path}: capability names must be non-empty strings")
        if not isinstance(raw_evidence, dict):
            raise ValueError(f"{file_path}: capabilities[{capability!r}] must be an object")
        fingerprint = raw_evidence.get("capability_fingerprint")
        if fingerprint is not None and (not isinstance(fingerprint, str) or not fingerprint):
            raise ValueError(
                f"{file_path}: capabilities[{capability!r}].capability_fingerprint "
                "must be a non-empty string"
            )
        source_args = raw_evidence.get("source_args")
        if source_args is not None and not isinstance(source_args, dict):
            raise ValueError(
                f"{file_path}: capabilities[{capability!r}].source_args must be an object"
            )
        scenarios = raw_evidence.get("scenarios")
        if scenarios is None:
            scenarios = {
                key: value
                for key, value in raw_evidence.items()
                if key not in {"capability_fingerprint", "source_args"}
            }
        if not isinstance(scenarios, dict):
            raise ValueError(
                f"{file_path}: capabilities[{capability!r}].scenarios must be an object"
            )
        observations: dict[str, ProbeResult] = {}
        for scenario, evidence in scenarios.items():
            if not isinstance(scenario, str) or not scenario:
                raise ValueError(
                    f"{file_path}: capabilities[{capability!r}] scenario names "
                    "must be non-empty strings"
                )
            if not isinstance(evidence, dict):
                raise ValueError(
                    f"{file_path}: capabilities[{capability!r}][{scenario!r}] must be an object"
                )
            observations[scenario] = _probe_result_from_dict(
                evidence,
                source=f"{file_path}: capabilities[{capability!r}][{scenario!r}]",
            )
        parsed[capability] = CapabilityConformanceEvidence(
            capability=capability,
            capability_fingerprint=fingerprint,
            source_args=source_args,
            observations=observations,
        )
    return parsed


def validate_conformance_evidence(
    capabilities: tuple[ProviderCapability, ...],
    evidence: Mapping[str, CapabilityConformanceEvidence],
    *,
    required_capabilities: tuple[str, ...] | None = None,
) -> tuple[ConformanceReport, ...]:
    """Validate an evidence artifact against a capability matrix."""
    by_name = {capability.name: capability for capability in capabilities}
    targets = required_capabilities if required_capabilities is not None else tuple(evidence)
    reports: list[ConformanceReport] = []

    for name in targets:
        capability = by_name.get(name)
        capability_evidence = evidence.get(name)
        observations = capability_evidence.observations if capability_evidence is not None else {}
        if capability is None:
            reports.append(_unknown_capability_report(name, observations))
            continue
        report = validate_conformance(
            capability,
            observations,
            source_args=(
                capability_evidence.source_args if capability_evidence is not None else None
            ),
        )
        evidence_failures = (
            *_evidence_identity_failures(name, capability_evidence),
            *_fingerprint_failures(capability, capability_evidence),
        )
        if evidence_failures:
            report = _append_failures(report, evidence_failures)
        reports.append(report)

    if required_capabilities is None:
        return tuple(reports)

    requested = set(required_capabilities)
    for name, capability_evidence in evidence.items():
        if name not in requested and name not in by_name:
            reports.append(_unknown_capability_report(name, capability_evidence.observations))
    return tuple(reports)


def _fingerprint_failures(
    capability: ProviderCapability,
    evidence: CapabilityConformanceEvidence | None,
) -> tuple[ConformanceFailure, ...]:
    expected = capability_fingerprint(capability)
    actual = evidence.capability_fingerprint if evidence is not None else None
    if actual == expected:
        return ()
    reason = (
        "missing capability_fingerprint; regenerate conformance evidence "
        "from the current capability plan"
        if actual is None
        else "capability_fingerprint does not match the current capability"
    )
    return (
        ConformanceFailure(
            scenario="capability_fingerprint",
            expected=(expected,),
            actual=actual,
            reason=reason,
        ),
    )


def _evidence_identity_failures(
    name: str,
    evidence: CapabilityConformanceEvidence | None,
) -> tuple[ConformanceFailure, ...]:
    if evidence is None or evidence.capability == name:
        return ()
    return (
        ConformanceFailure(
            scenario="capability",
            expected=(name,),
            actual=evidence.capability,
            reason="conformance evidence capability name does not match its matrix key",
        ),
    )


def _source_arg_failures(
    capability: ProviderCapability,
    source_args: Mapping[str, object] | None,
) -> tuple[ConformanceFailure, ...]:
    if not capability.required_args:
        return ()
    if source_args is None:
        return (
            ConformanceFailure(
                scenario="source_args",
                expected=capability.required_args,
                actual=None,
                reason=(
                    "missing source_args for required handler arg(s): "
                    f"{', '.join(capability.required_args)}"
                ),
            ),
        )

    missing = tuple(field for field in capability.required_args if field not in source_args)
    unfilled = tuple(
        field
        for field in capability.required_args
        if field in source_args and _unfilled_source_arg(source_args[field])
    )
    if not missing and not unfilled:
        return ()

    parts: list[str] = []
    if missing:
        parts.append(f"missing: {', '.join(missing)}")
    if unfilled:
        parts.append(f"unfilled: {', '.join(unfilled)}")
    return (
        ConformanceFailure(
            scenario="source_args",
            expected=capability.required_args,
            actual=", ".join(sorted(str(field) for field in source_args)),
            reason="source_args must record every required handler arg used by the fixture; "
            + "; ".join(parts),
        ),
    )


def _unfilled_source_arg(value: object) -> bool:
    if value in (None, "", [], {}):
        return True
    return isinstance(value, str) and "TODO" in value.upper()


def _append_failures(
    report: ConformanceReport,
    failures: tuple[ConformanceFailure, ...],
) -> ConformanceReport:
    return ConformanceReport(
        capability=report.capability,
        required_scenarios=report.required_scenarios,
        observed_scenarios=report.observed_scenarios,
        failures=(*report.failures, *failures),
    )


def _validate_case(
    case: ConformanceCase,
    result: ProbeResult,
    failures: list[ConformanceFailure],
    *,
    source_args: Mapping[str, object] | None,
) -> None:
    expected = tuple(outcome.value for outcome in case.expected_outcomes)
    if result.outcome not in case.expected_outcomes:
        failures.append(
            ConformanceFailure(
                scenario=case.scenario.value,
                expected=expected,
                actual=result.outcome.value,
                reason=(f"expected one of {', '.join(expected)}, got {result.outcome.value}"),
            )
        )
        return
    if case.receipt_required and not result.receipt:
        failures.append(
            ConformanceFailure(
                scenario=case.scenario.value,
                expected=expected,
                actual=result.outcome.value,
                reason="HAPPENED must include a receipt with external evidence",
            )
        )
        return
    missing_receipt_fields = _missing_receipt_fields(result.receipt, case.required_receipt_fields)
    if missing_receipt_fields:
        failures.append(
            ConformanceFailure(
                scenario=case.scenario.value,
                expected=tuple(case.required_receipt_fields),
                actual=None,
                reason=(
                    "HAPPENED receipt is missing required field(s): "
                    f"{', '.join(missing_receipt_fields)}"
                ),
            )
        )
        return
    receipt_source_mismatches = _receipt_source_mismatches(
        result.receipt,
        case.required_receipt_source_fields,
        source_args,
    )
    if receipt_source_mismatches:
        failures.append(
            ConformanceFailure(
                scenario=case.scenario.value,
                expected=tuple(
                    f"{receipt_field}<-{source_field}"
                    for receipt_field, source_field in case.required_receipt_source_fields
                ),
                actual=None,
                reason=(
                    "HAPPENED receipt does not satisfy required source contract(s): "
                    f"{'; '.join(receipt_source_mismatches)}"
                ),
            )
        )


def _probe_result_from_dict(data: Mapping[str, object], *, source: str) -> ProbeResult:
    outcome_raw = data.get("outcome")
    if not isinstance(outcome_raw, str) or not outcome_raw:
        raise ValueError(f"{source}: outcome must be a non-empty string")
    try:
        outcome = ProbeOutcome(outcome_raw)
    except ValueError as exc:
        valid = ", ".join(outcome.value for outcome in ProbeOutcome)
        raise ValueError(f"{source}: invalid outcome {outcome_raw!r}; valid: {valid}") from exc

    receipt = data.get("receipt")
    if receipt is not None and not isinstance(receipt, dict):
        raise ValueError(f"{source}: receipt must be an object or null")
    detail = data.get("detail")
    if not isinstance(detail, str) or not detail.strip():
        raise ValueError(
            f"{source}: detail must be a non-empty string describing how the scenario was exercised"
        )
    detail = detail.strip()
    if "TODO" in detail.upper():
        raise ValueError(f"{source}: detail must be filled in, not TODO")
    return ProbeResult(outcome, receipt=receipt, detail=detail)


def _missing_receipt_fields(
    receipt: Mapping[str, object] | None,
    required_fields: tuple[str, ...],
) -> tuple[str, ...]:
    if not required_fields:
        return ()
    if receipt is None:
        return required_fields
    return tuple(
        field
        for field in required_fields
        if field not in receipt or receipt[field] in (None, "", [], {})
    )


def _receipt_source_mismatches(
    receipt: Mapping[str, object] | None,
    required_sources: tuple[tuple[str, str], ...],
    source_args: Mapping[str, object] | None,
) -> tuple[str, ...]:
    if not required_sources:
        return ()
    if receipt is None:
        return tuple(f"{receipt_field} requires receipt" for receipt_field, _ in required_sources)
    if source_args is None:
        return tuple(
            f"{receipt_field} requires source_args[{source_field!r}]"
            for receipt_field, source_field in required_sources
        )
    failures: list[str] = []
    for receipt_field, source_field in required_sources:
        if source_field not in source_args:
            failures.append(f"{receipt_field} requires source_args[{source_field!r}]")
            continue
        actual = receipt.get(receipt_field)
        expected = source_args[source_field]
        if actual != expected:
            failures.append(
                f"{receipt_field} expected {expected!r} from source_args[{source_field!r}], "
                f"got {actual!r}"
            )
    return tuple(failures)


def _unknown_capability_report(
    name: str,
    observations: Mapping[str, ProbeResult],
) -> ConformanceReport:
    return ConformanceReport(
        capability=name,
        required_scenarios=(),
        observed_scenarios=tuple(observations),
        failures=(
            ConformanceFailure(
                scenario="capability",
                expected=(),
                actual=None,
                reason="no matching provider capability in the matrix",
            ),
        ),
    )


def _parse_scenario(value: ConformanceScenario | str) -> ConformanceScenario:
    if isinstance(value, ConformanceScenario):
        return value
    try:
        return ConformanceScenario(value)
    except ValueError as exc:
        valid = ", ".join(scenario.value for scenario in ConformanceScenario)
        raise ValueError(f"unknown conformance scenario {value!r}; valid: {valid}") from exc
